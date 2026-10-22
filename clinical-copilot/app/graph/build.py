"""Assembles the LangGraph topology (PRD Section 2) and exposes a
run_turn() convenience entry point shaped like ClinicalCopilotAgent.run_turn
(app/agent.py) -- same caller contract (history/tool-record continuity owned
by the caller, not this module), so app/main.py can adopt the graph path
without redesigning its own conversation-state handling.

    Resident message
          |
          v
     [Supervisor: routes]  --dashed/conditional--> [chart_data_worker]
          |                --dashed/conditional--> [intake_extractor]
          |                --dashed/conditional--> [evidence_retriever]
          |                                              |
          +----------------------<-----------------------+
          v
     [Supervisor: synthesizes + verifies]
          |
          v
    Response to resident (text + citations + review flags)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from langgraph.graph import END, START, StateGraph

from app.agent import ChartGatherResult, ClinicalCopilotAgent
from app.config import Settings
from app.fhir_client import FhirClient
from app.graph.state import CHART_DATA_WORKER, EVIDENCE_RETRIEVER, INTAKE_EXTRACTOR, CoPilotState
from app.graph.supervisor import route_node, route_targets, synthesize_node
from app.graph.workers import chart_data_worker_node, evidence_retriever_node, intake_extractor_node
from app.llm_client import PROVIDER_ERRORS, LLMClient, build_llm_client, provider_error_summary
from app.observability import TurnObserver, TurnTrace, log_event
from app.retrieval import GuidelineRetriever, get_retriever
from app.verification import ToolCallRecord

_ROUTE_NODE = "route"
_SYNTHESIZE_NODE = "synthesize"


def _crash_contained(worker_name: str, node_fn, trace: TurnTrace):
    """Wraps a worker node function so an exception it doesn't already
    handle structurally never crashes the whole turn -- PRD Section
    4.10's "no hard-block" philosophy applied to a genuine bug/outage in
    one worker, not just the honest-empty-result paths each worker
    already handles on its own (evidence_retriever_node's retrieval-
    service retry, intake_extractor_node's FhirRequestError catch, etc.
    still run first; this is the backstop for whatever's left, including
    a worker's own upstream dependency call raising something entirely
    unanticipated).

    Caught broadly (Exception), on purpose: the whole point is containing
    an UNANTICIPATED failure. Returns a partial_failures entry naming the
    worker rather than raising -- synthesize_node already discloses any
    partial_failures entry to the resident (its own existing hard rule:
    "if a section reports nothing gathered, or reports a failure, say so
    plainly"), and the OTHER workers routed this same turn are
    unaffected: this worker's own state keys (chart_facts/extracted_facts/
    retrieved_evidence) simply stay at their initial_state defaults
    (None/[]), the same representation an honestly-empty result already
    uses elsewhere."""

    def wrapped(state: CoPilotState) -> dict:
        try:
            return node_fn(state)
        except Exception as exc:  # noqa: BLE001 -- see this wrapper's own docstring
            trace.log_worker_crash(worker=worker_name, error=repr(exc))
            return {
                "partial_failures": [
                    f"{worker_name}: an unexpected error occurred -- answering without this worker's contribution"
                ]
            }

    return wrapped


def build_graph(
    *,
    llm: LLMClient,
    model: str,
    chart_agent: ClinicalCopilotAgent,
    fhir: FhirClient,
    prior_tool_records: list[ToolCallRecord] | None,
    trace: TurnTrace,
    retriever: GuidelineRetriever | None = None,
):
    """Builds and compiles one turn's graph. Takes the turn's real
    dependencies (fhir, trace, prior_tool_records) directly, rather than
    building a graph once at import time and threading them through a
    LangGraph `config` -- each real dependency here is request-scoped
    (a resident's own FhirClient, this turn's own TurnTrace), the same
    per-request-construction discipline app/main.py already applies to
    ClinicalCopilotAgent itself (never a module-level singleton holding
    request-scoped state)."""
    graph = StateGraph(CoPilotState)

    graph.add_node(_ROUTE_NODE, lambda state: route_node(state, llm=llm, model=model, trace=trace))
    graph.add_node(
        CHART_DATA_WORKER,
        _crash_contained(
            CHART_DATA_WORKER,
            lambda state: chart_data_worker_node(
                state, chart_agent=chart_agent, fhir=fhir, prior_tool_records=prior_tool_records, trace=trace
            ),
            trace,
        ),
    )
    graph.add_node(
        INTAKE_EXTRACTOR,
        _crash_contained(INTAKE_EXTRACTOR, lambda state: intake_extractor_node(state, fhir=fhir), trace),
    )
    resolved_retriever = retriever or get_retriever()
    graph.add_node(
        EVIDENCE_RETRIEVER,
        _crash_contained(
            EVIDENCE_RETRIEVER,
            lambda state: evidence_retriever_node(state, llm=llm, model=model, retriever=resolved_retriever, trace=trace),
            trace,
        ),
    )
    graph.add_node(_SYNTHESIZE_NODE, lambda state: synthesize_node(state, llm=llm, model=model, fhir=fhir, trace=trace))

    graph.add_edge(START, _ROUTE_NODE)
    graph.add_conditional_edges(_ROUTE_NODE, route_targets, [CHART_DATA_WORKER, INTAKE_EXTRACTOR, EVIDENCE_RETRIEVER])
    graph.add_edge(CHART_DATA_WORKER, _SYNTHESIZE_NODE)
    graph.add_edge(INTAKE_EXTRACTOR, _SYNTHESIZE_NODE)
    graph.add_edge(EVIDENCE_RETRIEVER, _SYNTHESIZE_NODE)
    graph.add_edge(_SYNTHESIZE_NODE, END)

    return graph.compile()


@dataclass
class GraphTurnResult:
    """run_turn()'s return shape -- deliberately parallel to
    ChatTurnResult (app/agent.py), not a reuse of it: this turn may have
    gathered chart_facts/extracted_facts/retrieved_evidence, none of which
    ChatTurnResult has a slot for, and citations/needs_review_flags are new
    (PRD Section 4.4/4.2). ChatTurnResult itself stays exactly as Week 1
    left it and is still used by evals/cases.py's Golden Set path.

    2026-09-23 correction: the claim that used to stand here -- "app/main.py's
    existing /chat path is unaffected by this module existing at all" -- was
    true when this class was first written and became false the same night,
    when /chat was switched to call run_turn() (this module) directly. Left
    uncorrected, it read as a live guarantee that this type's shape doesn't
    matter to /chat, when the opposite was true: main.py was constructing
    its response directly from this dataclass's fields (tool_calls,
    reference_notes) that were never actually added here, which crashed
    every real production /chat call with AttributeError until traced and
    fixed. Deliberately parallel to ChatTurnResult is still the right design
    (see above) -- but "parallel" must mean every field a caller actually
    reads exists on both, not just the ones that happened to get ported."""

    correlation_id: str
    response_text: str
    verification_passed: bool
    passed_domain_constraint: bool
    flagged_claims: list[str]
    enforced_warnings: list[str]
    workers_invoked: list[str]
    routing_rationale: str
    citations: list[dict] = field(default_factory=list)
    needs_review_flags: list[str] = field(default_factory=list)
    partial_failures: list[str] = field(default_factory=list)
    notices: list[dict] = field(default_factory=list)
    node_metrics: dict = field(default_factory=dict)
    updated_history: list[dict] = field(default_factory=list)
    accumulated_tool_records: list[ToolCallRecord] = field(default_factory=list)
    # 2026-09-23: found live in production -- app/main.py's /chat response
    # construction (ChatResponse.tool_calls: list[dict]) was still reading
    # a `tool_calls` attribute off this type that never existed, crashing
    # every /chat call with AttributeError. The dict-shaped per-call log
    # (tool/input/failed/latency_s) this mirrors already exists transiently
    # inside chart_data_worker's own ChartGatherResult.tool_calls -- it just
    # never made it onto the result this function actually returns. Not a
    # simple rename: accumulated_tool_records (above) is NOT an adequate
    # substitute -- it's ToolCallRecord objects, not dicts, carries no
    # per-call `input`/`latency_s`, and (per ToolCallRecord's own docstring
    # in app/tools.py) only successful calls are ever appended to it, so a
    # failed tool call would be silently invisible in the response even
    # though the resident-facing failure disclosure itself works correctly
    # through a different path (partial_failures above).
    tool_calls: list[dict] = field(default_factory=list)


_UNAVAILABLE_MESSAGE = (
    "The assistant is temporarily unavailable, so I couldn't answer this. Please try again in a few minutes. "
    "If it keeps happening, quote this reference: {correlation_id}."
)


def _assistant_unavailable_result(
    correlation_id: str,
    exc: BaseException,
    history: list[dict],
    prior_tool_records: list[ToolCallRecord] | None,
    trace: TurnTrace,
) -> "GraphTurnResult":
    log_event("llm_provider_error", correlation_id=correlation_id, **provider_error_summary(exc))
    message = _UNAVAILABLE_MESSAGE.format(correlation_id=correlation_id)
    trace.finish_turn(final_response="[assistant unavailable]")
    return GraphTurnResult(
        correlation_id=correlation_id,
        response_text=message,
        verification_passed=True,
        passed_domain_constraint=True,
        flagged_claims=[],
        enforced_warnings=[],
        workers_invoked=[],
        routing_rationale="",
        notices=[{
            "worker": "supervisor",
            "kind": "assistant_unavailable",
            "severity": "warning",
            "label": "Assistant unavailable",
            "text": "This question was not answered, and nothing from it was added to the conversation.",
        }],
        updated_history=list(history),
        accumulated_tool_records=list(prior_tool_records or []),
    )


def run_turn(
    *,
    settings: Settings,
    history: list[dict],
    user_message: str,
    patient_id: str,
    prior_tool_records: list[ToolCallRecord] | None = None,
    uploaded_document_ref: dict | None = None,
    fhir: FhirClient,
    resident_username: str | None = None,
    observer: TurnObserver | None = None,
    chart_agent: ClinicalCopilotAgent | None = None,
    retriever: GuidelineRetriever | None = None,
) -> GraphTurnResult:
    """Convenience entry point, shaped like ClinicalCopilotAgent.run_turn:
    the caller owns conversation-level continuity (history, prior_tool_records
    -- exactly app/main.py's existing _ConversationState fields) and gets it
    back updated, the same contract Week 1's agent.py already uses. One
    correlation_id/TurnTrace per call, shared by every node this turn runs
    (PRD Section 4.6: "Correlation ID becomes a parent trace (supervisor)
    with child spans (each worker)") -- built once here, not per-node.

    retriever: None (the default) resolves to the real process-wide
    GuidelineRetriever singleton (app/retrieval/get_retriever()), same as
    before this parameter existed. Overridable for the same reason
    fhir_override exists on evals/cases.py's EvalCase -- a test that needs
    to inject a specific, controlled failure (e.g. a fake retriever whose
    retrieve() raises, to exercise _crash_contained's worker-failure
    disclosure) has no other way to reach evidence_retriever_node's real
    dependency, since app/main.py's live /chat path never needs anything
    but the real singleton.

    This IS what app/main.py's /chat endpoint calls today (switched over
    the same night this module was built) -- GraphTurnResult's own
    docstring still says otherwise; that claim went stale the moment the
    switch happened and nothing caught it (see this function's tool_calls
    handling below, and GraphTurnResult's own docstring, for what that
    silently let through)."""
    llm = build_llm_client(settings)
    observer = observer or TurnObserver(settings)
    chart_agent = chart_agent or ClinicalCopilotAgent(settings, observer=observer)

    correlation_id = str(uuid.uuid4())
    trace = observer.start_turn(correlation_id, user_message, patient_id, resident_username)

    compiled = build_graph(
        llm=llm,
        model=settings.anthropic_model,
        chart_agent=chart_agent,
        fhir=fhir,
        prior_tool_records=prior_tool_records,
        trace=trace,
        retriever=retriever,
    )

    initial_state: CoPilotState = {
        "correlation_id": correlation_id,
        "resident_username": resident_username,
        "patient_id": patient_id,
        "user_message": user_message,
        "messages": list(history),
        "workers_invoked": [],
        "routing_rationale": "",
        "chart_facts": None,
        "uploaded_document_ref": uploaded_document_ref,
        "extracted_facts": [],
        "retrieved_evidence": [],
        "citations": [],
        "node_metrics": {},
        "partial_failures": [],
        "notices": [],
        "draft_response": None,
        "needs_review_flags": [],
        "verification_outcome": None,
        "final_response": None,
    }

    try:
        final_state: CoPilotState = compiled.invoke(initial_state)
    except PROVIDER_ERRORS as exc:
        # The model provider failed (rejected key, rate limit, outage) in the routing step or any later LLM call.
        # Answer with a plain message and the correlation id instead of a 500; the conversation is left as it was
        # so the resident can simply retry. Only the error type and status are logged, never its message.
        return _assistant_unavailable_result(correlation_id, exc, history, prior_tool_records, trace)

    final_response = final_state["final_response"] or ""
    messages = list(final_state["messages"])
    messages.append({"role": "assistant", "content": final_response})

    chart_facts: ChartGatherResult | None = final_state.get("chart_facts")
    accumulated_tool_records = chart_facts.turn_records if chart_facts else list(prior_tool_records or [])
    tool_calls = chart_facts.tool_calls if chart_facts else []

    verification_outcome = final_state["verification_outcome"] or {
        "passed_source_attribution": True,
        "passed_domain_constraint": True,
        "flagged_claims": [],
        "enforced_warnings": [],
    }

    return GraphTurnResult(
        correlation_id=correlation_id,
        response_text=final_response,
        verification_passed=verification_outcome["passed_source_attribution"]
        and verification_outcome["passed_domain_constraint"],
        passed_domain_constraint=verification_outcome["passed_domain_constraint"],
        flagged_claims=verification_outcome["flagged_claims"],
        enforced_warnings=verification_outcome["enforced_warnings"],
        workers_invoked=final_state["workers_invoked"],
        routing_rationale=final_state["routing_rationale"],
        citations=final_state["citations"],
        needs_review_flags=final_state["needs_review_flags"],
        partial_failures=final_state["partial_failures"],
        notices=final_state.get("notices") or [],
        node_metrics=final_state["node_metrics"],
        updated_history=messages,
        accumulated_tool_records=accumulated_tool_records,
        tool_calls=tool_calls,
    )
