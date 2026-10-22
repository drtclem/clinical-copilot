"""The three peer workers PRD Section 2 names: chart_data_worker,
intake_extractor, evidence_retriever. Each is a LangGraph node function --
`(CoPilotState) -> dict` -- bound to its real dependencies (LLM client,
FhirClient, TurnTrace) via a closure built in app/graph/build.py, the same
per-request-construction pattern app/main.py already uses for
ClinicalCopilotAgent (never a module-level singleton holding request-scoped
state like a resident's FhirClient).

chart_data_worker and evidence_retriever are real; intake_extractor is
still a stub (Checklist item 1's "get it running end-to-end (even with
stub workers)") -- app/ingestion/ (Checklist item 4) doesn't exist yet.
See its own docstring for its current stub behavior.
"""

from __future__ import annotations

import logging

from app.agent import ClinicalCopilotAgent
from app.fhir_client import FhirClient, FhirRequestError
from app.graph.metrics import NodeTimer
from app.graph.state import CHART_DATA_WORKER, EVIDENCE_RETRIEVER, INTAKE_EXTRACTOR, CoPilotState
from app.ingestion.storage import list_extracted_documents
from app.graph.notices import no_guideline_match_notice
from app.llm_client import LLMClient, TruncatedOutputError
from app.observability import TurnTrace
from app.retrieval import GuidelineRetriever, build_safe_query
from app.verification import ToolCallRecord

logger = logging.getLogger("clinical_copilot")


def chart_data_worker_node(
    state: CoPilotState,
    *,
    chart_agent: ClinicalCopilotAgent,
    fhir: FhirClient,
    prior_tool_records: list[ToolCallRecord] | None,
    trace: TurnTrace,
) -> dict:
    """Wraps ClinicalCopilotAgent.gather_chart_facts() (app/agent.py) --
    Week 1's real tool-calling loop against OpenEMR/FHIR, extended with the
    7th tool (get_uploaded_documents, PRD Section 4.2) once Checklist item 4
    lands. Deliberately does NOT call run_turn()/verify_response() here --
    this worker only GATHERS facts; the graph's synthesize_node
    (app/graph/supervisor.py) drafts and verifies the one final answer
    across every worker that ran this turn (PRD Section 4.4).

    prior_tool_records: this conversation's earlier-turn ToolCallRecords
    (the same continuity gather_chart_facts's own docstring describes) --
    owned by the graph's caller (app/main.py's conversation store, or the
    eval harness), not by CoPilotState itself: state is scoped to one turn
    (PRD Section 3's "Turn identity & threading" comment), while
    conversation-level history/tool-record continuity is the same
    responsibility main.py's existing _ConversationState already carries
    for Week 1's agent.py path."""
    worker_span = trace.start_worker_span(CHART_DATA_WORKER)
    with NodeTimer() as timer:
        gathered = chart_agent.gather_chart_facts(
            history=state["messages"],
            user_message=state["user_message"],
            patient_id=state["patient_id"],
            prior_tool_records=prior_tool_records,
            fhir=fhir,
            trace=trace,
            trace_parent=worker_span,
        )
        metrics = timer.metrics(gathered.total_input_tokens, gathered.total_output_tokens)
    trace.finish_worker_span(worker_span)

    partial_failures = [
        f"chart_data_worker: {call['tool']} failed" for call in gathered.tool_calls if call["failed"]
    ]
    # A tool call can SUCCEED but still carry its own nested
    # partial_failures (GetPatientSnapshotOutput/GetUploadedDocumentsOutput/
    # etc., app/schemas.py) -- a tool that failed OUTRIGHT (above) and a
    # tool that succeeded but partially failed INTERNALLY are different
    # signals, and PRD Section 4.6 wants this node's own partial_failures
    # to surface both, not just the first. Found live investigating
    # evals/graph_cases.py's uploaded-documents case: verify_response()
    # already disclosed this correctly in the response TEXT via
    # _enforce_partial_failures, but CoPilotState.partial_failures (the
    # structured, dashboardable signal PRD Section 4.6 asks for) stayed
    # empty regardless -- only this turn's NEWLY fetched records (the
    # slice past prior_tool_records) are walked, so an inherited prior-turn
    # partial failure isn't re-reported every subsequent turn.
    new_records = gathered.turn_records[len(prior_tool_records or []) :]
    for record in new_records:
        nested = getattr(record.output, "partial_failures", None)
        if nested:
            partial_failures += [f"chart_data_worker: {record.tool_name}: {f}" for f in nested]

    return {
        "chart_facts": gathered,
        "messages": gathered.updated_messages,
        "partial_failures": partial_failures,
        "node_metrics": {CHART_DATA_WORKER: metrics},
    }


def intake_extractor_node(state: CoPilotState, *, fhir: FhirClient) -> dict:
    """Real as of Checklist item 4. Surfaces the already-stored extraction
    for the document this turn's `uploaded_document_ref` names -- set by
    whichever caller already ran app/ingestion/pipeline.py's
    attach_and_extract (app/main.py's POST /documents, PRD Section 4.2)
    BEFORE this turn started. Does NOT re-run OCR itself: reads back via
    app/ingestion/storage.py's list_extracted_documents, the same
    never-re-OCR-on-a-later-turn rule the 7th chart-data-worker tool
    (get_uploaded_documents) follows -- the two exist for different
    triggers (this node: a document attached THIS turn; that tool: the
    model deciding on its own to check for previously-uploaded documents),
    not different mechanisms."""
    doc_ref = state.get("uploaded_document_ref")
    with NodeTimer() as timer:
        if not doc_ref:
            extracted_facts: list = []
            partial_failures: list[str] = []
        else:
            try:
                # omitted_count is intentionally unused here: this node looks
                # up one specific document (target_id below) attached THIS
                # turn, which is always the most-recently-uploaded one and so
                # always sorts within list_extracted_documents' cap -- unlike
                # get_uploaded_documents (app/tools.py), which surfaces the
                # whole list and must disclose omissions.
                results, list_failures, _omitted_count = list_extracted_documents(fhir, state["patient_id"])
            except FhirRequestError:
                extracted_facts = []
                partial_failures = ["intake_extractor: could not retrieve the uploaded document's extracted facts"]
            else:
                target_id = doc_ref.get("doc_id")
                matching = [r for r in results if target_id in (r.doc_id, r.extracted_doc_id)]
                extracted_facts = matching
                partial_failures = list(list_failures)
                if not matching:
                    partial_failures.append("intake_extractor: referenced document not found")
        metrics = timer.metrics(0, 0)

    return {
        "extracted_facts": extracted_facts,
        "partial_failures": partial_failures,
        "node_metrics": {INTAKE_EXTRACTOR: metrics},
    }


def evidence_retriever_node(
    state: CoPilotState,
    *,
    llm: LLMClient,
    model: str,
    retriever: GuidelineRetriever,
    trace: TurnTrace,
) -> dict:
    """Real as of Checklist item 3: app/retrieval/query.py's PHI-safe query
    construction, then app/retrieval/retriever.py's hybrid RAG + rerank
    pipeline against the curated corpus (Checklist item 2).

    Failure handling (PRD Section 4.10): empty results are reported
    honestly, not as a failure -- a genuinely irrelevant/unmatched query is
    an expected outcome, not a system fault. A down reranker/embedding
    service gets one retry, then the turn proceeds with an explicit
    "answering without guideline evidence" disclosure via partial_failures
    (app/verification.py's existing _enforce_partial_failures convention
    already surfaces this to the resident) -- never a hard block
    (Section 4.10's "no hard-block/refusal path" philosophy)."""
    input_tokens = output_tokens = 0
    partial_failures: list[str] = []
    notices: list[dict] = []
    worker_span = trace.start_worker_span(EVIDENCE_RETRIEVER)
    with NodeTimer() as timer:
        try:
            queries, q_input_tokens, q_output_tokens = build_safe_query(
                state["user_message"], llm=llm, model=model, trace=trace, trace_parent=worker_span
            )
            input_tokens += q_input_tokens
            output_tokens += q_output_tokens
        except TruncatedOutputError:
            # Never retrieve on a cut-off (possibly partial) query list, and never
            # let the empty result read as "searched, found nothing relevant".
            queries = []
            partial_failures.append(
                "evidence_retriever: the search query was cut off before it finished -- "
                "answering without guideline evidence"
            )

        evidence = []
        if queries:
            last_error: Exception | None = None
            for attempt in range(2):  # one retry, per PRD Section 4.10
                try:
                    evidence = retriever.retrieve(queries)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 -- a down local model process is the
                    # one condition this retry exists for; any other cause still
                    # degrades to the same honest disclosure below, never an
                    # unhandled exception reaching the resident.
                    last_error = exc
                    logger.warning('{"event": "evidence_retriever_attempt_failed", "attempt": %d}', attempt)
            if last_error is not None:
                partial_failures.append(
                    "evidence_retriever: retrieval service unavailable after retry -- "
                    "answering without guideline evidence"
                )
            elif not evidence:
                # 2026-09-24: distinct from the service-failure case above
                # -- retrieval ran successfully but nothing in the corpus
                # cleared app/retrieval/retriever.py's relevance floor. This
                # node only runs when the supervisor decided guideline
                # evidence was actually needed, so a genuinely empty result
                # here means "searched, found nothing relevant" -- a real,
                # expected outcome (PRD Section 4.10), not a fault, but one
                # the resident should see disclosed structurally, not left
                # entirely to the synthesis model's own judgment to notice
                # and decline gracefully. 2026-09-25: as an info-level
                # NOTICE (app/graph/notices.py), not a partial failure --
                # nothing failed, so it must not raise a warning, add a
                # "[Verification note]", or cost the answer its verified check.
                notices.append(no_guideline_match_notice())
        metrics = timer.metrics(input_tokens, output_tokens)
    trace.finish_worker_span(worker_span)

    return {
        "retrieved_evidence": evidence,
        "partial_failures": partial_failures,
        "notices": notices,
        "node_metrics": {EVIDENCE_RETRIEVER: metrics},
    }
