"""PHI-safe query construction, PRD Section 4.3's hard rule: "queries sent
to the retriever/reranker carry clinical concepts only -- never patient
identifiers, MRNs, or verbatim chart text. Enforced in
evidence_retriever's query-construction step itself."

Two layers, not one:

1. Structural: this module's only entry point takes the resident's raw
   message text and nothing else -- no patient_id, no chart data, no tool
   output. app/graph/workers.py's evidence_retriever_node never fetches
   chart data at all, so there is nothing upstream of this call that could
   leak FHIR content into a query even by mistake.
2. An LLM call constrained (via a forced tool call, app/llm_client.py's
   tool_choice) to output a short clinical CONCEPT phrase only -- never
   asked to summarize or quote the resident's message -- followed by a
   deterministic regex backstop that strips anything UUID-shaped (this
   system's patient_id format) or a long digit run (MRN-shaped) from
   whatever the model returns. The backstop is the actual enforcement --
   a "wall, not a request" the same way ARCHITECTURE.md 3.2 already
   applies to the allergy-conflict check, just for a different risk (query
   privacy, not a missed clinical fact): a prompt instruction alone is not
   trusted to guarantee this.
"""

from __future__ import annotations

import re

from app.llm_client import THINKING_DISABLED, LLMClient, llm_call_meta, raise_if_truncated
from app.observability import TurnTrace

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Conservative on purpose: this strips any run of 6+ digits, which could
# occasionally remove a genuinely non-identifying number (e.g. a lab value
# written without a decimal point). The cost of over-scrubbing a query is
# far lower than the cost of an identifier-shaped token reaching the
# self-hosted retriever/reranker, so this errs toward stripping.
_LONG_DIGIT_RUN_RE = re.compile(r"\b\d{6,}\b")

_MAX_QUERIES = 3

_QUERY_SYSTEM_PROMPT = """\
Extract the general clinical concept(s) this message is asking about, as \
short search-query phrases (a few words each, not a sentence) -- for \
example "hyperkalemia management", "penicillin cephalosporin \
cross-reactivity", "DOAC reversal for active bleeding".

If the message spans more than one distinct clinical topic (e.g. a fever/\
sepsis-severity question that ALSO asks about antibiotic choice given an \
allergy), return one phrase per distinct topic -- up to 3. Most messages \
need only one. Never combine two distinct topics into a single phrase; \
return them as separate list entries instead.

Prefer the underlying clinical CONCEPT over a list of symptoms or a copy of \
the question, and keep each phrase short: a phrase such as "sepsis screening" \
retrieves the right guideline far more reliably than "fever productive cough \
what to screen for". For example, "fever with a productive cough -- what \
should I screen for, and what are the antibiotic considerations with a sulfa \
allergy?" is two topics: ["sepsis screening for fever", "antibiotic \
selection with a drug allergy"].

Never include: the patient's name, any identifier (MRN, patient ID), a \
specific lab value, a specific date/time, or any other patient-specific \
detail from the message -- general medical-knowledge concepts only. If the \
message contains no general clinical-knowledge question at all (e.g. it's \
a pure chart lookup, or has no clinical content), call extract_query with \
an empty list.
"""

_QUERY_TOOL = {
    "name": "extract_query",
    "description": "The clinical-concept-only search queries extracted from the resident's message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _MAX_QUERIES,
                "description": "One phrase per distinct clinical topic, up to 3. Empty if none.",
            }
        },
        "required": ["queries"],
    },
}


def _scrub(query: str) -> str:
    query = _UUID_RE.sub("", query)
    query = _LONG_DIGIT_RUN_RE.sub("", query)
    return " ".join(query.split())


def build_safe_query(
    user_message: str, *, llm: LLMClient, model: str, trace: TurnTrace, trace_parent: object = None
) -> tuple[list[str], int, int]:
    """Returns (queries, input_tokens, output_tokens) -- up to _MAX_QUERIES
    scrubbed phrases (never more; empty list if the message has no general
    clinical-knowledge question at all). Multiple phrases exist so a
    genuinely two-topic question (app/retrieval/retriever.py's
    GuidelineRetriever.retrieve() now accepts a list for exactly this) can
    retrieve both topics' guidelines, rather than depending on this one
    call picking a single phrase that happens to represent both at once.

    No temperature control here (or anywhere in this codebase): found live
    that the installed SDK version's Messages.create() has no temperature
    parameter at all (confirmed by reading the SDK source directly, not
    assumed -- an earlier attempt to pass temperature=0 here crashed
    every real call with "Messages.create() got an unexpected keyword
    argument 'temperature'"). This SDK version's closest analogue,
    output_config's "effort" level, controls reasoning depth, not
    sampling determinism, and isn't a substitute. Non-deterministic
    concept extraction (sometimes dropping one topic of a genuinely
    two-topic question across repeated runs of the identical message) is
    the confirmed cause of inconsistent retrieval for
    rag_ambiguous_topic_question_retrieves_most_relevant_not_wrong_doc;
    the multi-phrase extraction above (up to _MAX_QUERIES topics, each
    retrieved and merged rather than one phrase representing everything)
    is the actual mitigation, not a determinism fix -- there is no
    determinism lever available at this layer.

    Token counts are returned rather than folded into a NodeMetrics here,
    so the caller (evidence_retriever_node) can combine them with whatever
    else it spends (embedding/rerank calls are local model inference, not
    LLM tokens) into one per-node total, matching PRD Section 4.6's
    per-node, not per-substep, granularity."""
    messages = [{"role": "user", "content": user_message}]
    llm_handle = trace.start_llm_call(
        round_index=0,
        model=model,
        input_payload={"system": _QUERY_SYSTEM_PROMPT, "message_length": len(user_message)},
        parent=trace_parent,
    )
    response = llm.create_message(
        system=_QUERY_SYSTEM_PROMPT,
        tools=[_QUERY_TOOL],
        messages=messages,
        max_tokens=256,
        tool_choice={"type": "tool", "name": "extract_query"},
        thinking=THINKING_DISABLED,
    )
    trace.finish_llm_call(
        llm_handle,
        output_payload={"tool_calls": [tc.input for tc in response.tool_calls]},
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        **llm_call_meta(response),
    )
    # A cut-off queries list would silently drop later topics (or all of
    # them); the caller discloses this instead of retrieving on a partial set.
    raise_if_truncated(response, "search-query extraction")

    call = next((c for c in response.tool_calls if c.name == "extract_query"), None)
    raw_queries = call.input.get("queries", []) if call else []
    if not isinstance(raw_queries, list):
        raw_queries = []
    # The PHI/identifier-scrub backstop applies to EVERY phrase, not just
    # the first -- see this module's own docstring for why a prompt
    # instruction alone is never trusted for this.
    scrubbed = [_scrub(q) for q in raw_queries if isinstance(q, str)]
    queries = [q for q in scrubbed if q][:_MAX_QUERIES]
    return queries, response.input_tokens, response.output_tokens
