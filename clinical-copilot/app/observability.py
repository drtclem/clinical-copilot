"""Observability: ARCHITECTURE.md Section 5 + 7.4, KEY_METRICS.md.

Every invocation gets a correlation ID at the moment the resident's message
arrives; it's used as the seed for the Langfuse trace id, so the same ID
threads through structured logs AND the Langfuse UI -- either one alone is
enough to reconstruct what happened for a given request (Section 5's "a full
trace is reconstructable from logs alone" is met by the local structured
logs; Langfuse is the dashboard on top, not the only record).

If LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY aren't set, this module still logs
everything locally -- it just skips sending to Langfuse. That keeps the
service runnable before the Langfuse container is up.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.log_redaction import redact_fields

logger = logging.getLogger("clinical_copilot")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _log(event: str, **fields: Any) -> None:
    # Every structured event passes through redact_fields: identifiers hashed, free text / clinical vocabulary
    # dropped (app/log_redaction.py). Nothing is written to journald as-is.
    logger.info(json.dumps({"event": event, **redact_fields(event, fields)}, default=str))


def log_event(event: str, **fields: Any) -> None:
    """Public entry point to the same structured log every turn event uses,
    for callers outside a turn (e.g. app/main.py's preview denials)."""
    _log(event, **fields)


class TurnObserver:
    def __init__(self, settings: Settings):
        self._client = None
        if settings.observability_enabled:
            from langfuse import Langfuse  # local import: optional dependency at runtime

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )

    def start_turn(
        self,
        correlation_id: str,
        user_message: str,
        patient_id: str | None,
        resident_username: str | None = None,
    ) -> "TurnTrace":
        """resident_username: the authenticated resident this turn runs as
        (app/oauth_session.py's ResidentSession.resident_username, threaded
        through app/agent.py's run_turn -- None for the eval harness, which
        has no real logged-in session). Attached the same way correlation_id
        already is -- a plain key in both the local structured log and the
        Langfuse span's metadata -- not a one-off mechanism, and not
        Langfuse's separate native user_id/propagate_attributes API, which
        would be a different shape from every other field this module
        attaches (2026-09-19 audit-logging design, ARCHITECTURE.md 7.11:
        this closes the "traces show which patient but not which resident"
        gap found while writing that section).

        message/final_response deliberately NEVER appear here or in
        finish_turn() below -- retrofit 2026-09-22, closing a real gap:
        this is the actual clinical conversation text (the resident's
        question, the model's answer, which routinely restates the
        patient's name/DOB/allergy details as normal clinical phrasing),
        logged to both the local structured log and this trace's Langfuse
        input/output. HIPAA's audit-control need -- who touched which
        patient's record, when -- is already fully served by the
        structured fields below (patient_id, correlation_id,
        resident_username) without the raw text riding along; the project
        has consistently taken the stricter reading wherever this was
        ambiguous, and this was pre-existing Week 1 behavior, not
        something introduced by any later change. message_length is
        logged instead -- enough to debug e.g. "why did this call use so
        many input tokens" without carrying chart content."""
        _log(
            "turn_started",
            correlation_id=correlation_id,
            patient_id=patient_id,
            resident_username=resident_username,
            message_length=len(user_message),
        )
        span = None
        if self._client is not None:
            trace_id = self._client.create_trace_id(seed=correlation_id)
            span = self._client.start_observation(
                trace_context={"trace_id": trace_id},
                name="chat_turn",
                as_type="agent",
                input={"message_length": len(user_message), "patient_id": patient_id},
                metadata={"correlation_id": correlation_id, "resident_username": resident_username},
            )
        return TurnTrace(client=self._client, span=span, correlation_id=correlation_id)

    def attach_feedback_score(self, correlation_id: str, rating: str, comment: str | None) -> bool:
        """Attaches a resident_feedback score to the turn's existing Langfuse
        trace (app/main.py's POST /feedback), located by recomputing the
        same seeded trace id start_turn() used to create it -- correlation_id
        itself is never the trace id (see that method's create_trace_id
        call). Returns False without raising if Langfuse isn't configured or
        the attach fails for any reason: feedback.jsonl is the durable
        record regardless, and a resident's feedback submission must never
        fail because of this best-effort second copy."""
        if self._client is None:
            return False
        try:
            trace_id = self._client.create_trace_id(seed=correlation_id)
            self._client.create_score(
                trace_id=trace_id,
                name="resident_feedback",
                value=1.0 if rating == "up" else 0.0,
                data_type="BOOLEAN",
                comment=comment,
            )
            return True
        except Exception:
            _log("feedback_langfuse_attach_failed", correlation_id=correlation_id)
            return False


@dataclass
class LlmCallHandle:
    round_index: int
    wall_t0: float
    langfuse_span: Any


@dataclass
class ToolCallHandle:
    tool_name: str
    input_payload: dict
    wall_t0: float
    langfuse_span: Any


@dataclass
class TurnTrace:
    client: Any
    span: Any
    correlation_id: str

    def start_worker_span(self, worker_name: str) -> Any:
        """Opens a named child span for one worker's WHOLE execution this
        turn (PRD Section 4.6: "Correlation ID becomes a parent trace
        (supervisor) with child spans (each worker)"). Returns the raw
        Langfuse span object (None if unconfigured) -- pass it as
        `parent` to start_llm_call/start_tool_call below so that worker's
        own LLM/tool calls nest under IT, not directly under the
        turn-level span, giving each worker its own subtree in the
        Langfuse UI instead of a flat list of calls. Call
        finish_worker_span() when the worker's node function returns."""
        if self.span is None:
            return None
        return self.span.start_observation(name=f"worker:{worker_name}", as_type="span")

    def finish_worker_span(self, worker_span: Any) -> None:
        if worker_span is not None:
            worker_span.end()

    def start_llm_call(
        self, *, round_index: int, model: str, input_payload: Any, parent: Any = None
    ) -> "LlmCallHandle":
        """Opens the Langfuse generation span *before* the API call, so its
        start_time is real. Call finish_llm_call() after the response comes
        back -- creating and ending the span back-to-back (the previous bug
        here) gives every call 0.00s latency regardless of how long it took.

        parent: the span this LLM call should nest under -- defaults to
        the turn-level span (Week 1 behavior, unchanged), but a caller
        driving one worker's own subtree (app/graph/workers.py) passes its
        own start_worker_span() result instead."""
        wall_t0 = time.monotonic()
        parent_span = parent if parent is not None else self.span
        langfuse_span = None
        if parent_span is not None:
            langfuse_span = parent_span.start_observation(
                name=f"llm_call_{round_index}",
                as_type="generation",
                model=model,
                input=input_payload,
            )
        return LlmCallHandle(round_index=round_index, wall_t0=wall_t0, langfuse_span=langfuse_span)

    def finish_llm_call(
        self,
        handle: "LlmCallHandle",
        *,
        output_payload: Any,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str | None = None,
        content_block_types: list[str] | None = None,
        thinking_chars: int | None = None,
    ) -> None:
        latency_s = time.monotonic() - handle.wall_t0
        extra: dict[str, Any] = {}
        if stop_reason is not None:
            extra["stop_reason"] = stop_reason
        if content_block_types is not None:
            extra["content_block_types"] = content_block_types
        if thinking_chars is not None:
            extra["thinking_chars"] = thinking_chars
        _log(
            "llm_call",
            correlation_id=self.correlation_id,
            round_index=handle.round_index,
            latency_s=round(latency_s, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            **extra,
        )
        if handle.langfuse_span is not None:
            handle.langfuse_span.update(
                output=output_payload,
                usage_details={"input": input_tokens, "output": output_tokens},
            )
            handle.langfuse_span.end()

    def start_tool_call(self, *, tool_name: str, input_payload: dict, parent: Any = None) -> "ToolCallHandle":
        """parent: same nesting convention as start_llm_call above."""
        wall_t0 = time.monotonic()
        parent_span = parent if parent is not None else self.span
        langfuse_span = None
        if parent_span is not None:
            langfuse_span = parent_span.start_observation(
                name=f"tool:{tool_name}",
                as_type="tool",
                input=input_payload,
            )
        return ToolCallHandle(tool_name=tool_name, input_payload=input_payload, wall_t0=wall_t0, langfuse_span=langfuse_span)

    def finish_tool_call(
        self, handle: "ToolCallHandle", *, output_payload: Any, failed: bool, retry_count: int = 0
    ) -> None:
        """retry_count: how many of this tool call's underlying FHIR GETs
        needed FhirClient's single retry (app/agent.py reads this via
        FhirClient.get_and_reset_retry_count() around the call) -- scored
        per tool call, the same granularity as the existing failed/level
        flag below, rather than instrumenting every individual GET as its
        own span."""
        latency_s = time.monotonic() - handle.wall_t0
        _log(
            "tool_call",
            correlation_id=self.correlation_id,
            tool=handle.tool_name,
            input=handle.input_payload,
            latency_s=round(latency_s, 3),
            failed=failed,
            retry_count=retry_count,
        )
        if handle.langfuse_span is not None:
            handle.langfuse_span.update(
                output=output_payload,
                level="ERROR" if failed else "DEFAULT",
            )
            # Promotes the level="ERROR" flag above into a real, aggregable
            # dashboard metric -- Langfuse can average tool_call_error
            # across every tool-call observation into an error rate, the
            # same mechanism verification_pass_rate already uses at the
            # trace level (log_verification below), just scoped per tool
            # call to match "error count" sitting alongside "request count"
            # in the dashboard requirement.
            handle.langfuse_span.score(
                name="tool_call_error",
                value=1.0 if failed else 0.0,
                data_type="BOOLEAN",
            )
            handle.langfuse_span.score(
                name="tool_call_retry_count",
                value=float(retry_count),
                data_type="NUMERIC",
            )
            handle.langfuse_span.end()

    def log_worker_crash(self, *, worker: str, error: str) -> None:
        """A worker node raised something none of its own structural error
        handling anticipated (evidence_retriever_node's retrieval-service
        retry, intake_extractor_node's FhirRequestError catch, etc. all
        still apply first -- this is the backstop for whatever's left).
        app/graph/build.py's per-worker crash-containment wrapper calls
        this instead of letting the exception propagate and abort the
        whole turn (PRD Section 4.10's "no hard-block" philosophy applied
        to a genuine bug/outage in one worker, not just the honest-empty-
        result paths each worker already handles on its own)."""
        _log(
            "worker_crashed",
            correlation_id=self.correlation_id,
            worker=worker,
            error=error,
        )

    def log_routing_decision(self, *, workers: list[str], rationale: str) -> None:
        """Gate 5 audit gap: app/graph/supervisor.py's route_node generates
        a model rationale for which workers it chose, but never logged
        it as its own event -- the rationale only ever reached the
        persisted trace buried inside the routing LLM call's raw
        tool-call payload (start_llm_call/finish_llm_call's
        output_payload), never as a first-class, directly queryable
        field the way turn_summary's own fields are. A person auditing
        past routing decisions from the log/trace alone (not the live API
        response) had to parse that raw payload rather than read a
        dedicated field. This closes that: one explicit event per turn,
        naming exactly which workers were chosen and why."""
        _log(
            "routing_decision",
            correlation_id=self.correlation_id,
            workers=workers,
            rationale=rationale,
        )

    def log_citation_counts(
        self,
        *,
        built: int,
        duplicate_documents_collapsed: int,
        before_filter: dict[str, int],
        after_filter: dict[str, int],
        documents_before_filter: int,
        documents_after_filter: int,
        fallback_answer_used: bool,
    ) -> None:
        """How many citations a turn produced and how many survived
        filter_citations_to_stated_facts, per source_type, plus whether the
        deterministic fallback answer (synthesis wrote no text) was what the
        resident saw -- the case that makes every extracted field count as
        "stated". Counts and a boolean only: no citation text, ids or values."""
        _log(
            "citation_counts",
            correlation_id=self.correlation_id,
            built=built,
            duplicate_documents_collapsed=duplicate_documents_collapsed,
            before_filter=before_filter,
            after_filter=after_filter,
            documents_before_filter=documents_before_filter,
            documents_after_filter=documents_after_filter,
            fallback_answer_used=fallback_answer_used,
        )

    def log_verification(
        self,
        *,
        passed_source_attribution: bool,
        passed_domain_constraint: bool,
        flagged_claims: list[str],
        enforced_warnings: list[str],
    ) -> None:
        overall_pass = passed_source_attribution and passed_domain_constraint
        _log(
            "verification",
            correlation_id=self.correlation_id,
            passed_source_attribution=passed_source_attribution,
            passed_domain_constraint=passed_domain_constraint,
            verification_pass=overall_pass,  # North Star metric, KEY_METRICS.md
            flagged_claims=flagged_claims,
            enforced_warnings=enforced_warnings,
        )
        if self.span is not None:
            self.span.score_trace(
                name="verification_pass_rate",
                value=1.0 if overall_pass else 0.0,
                data_type="BOOLEAN",
            )
            self.span.score_trace(
                name="source_attribution_pass",
                value=1.0 if passed_source_attribution else 0.0,
                data_type="BOOLEAN",
            )
            self.span.score_trace(
                name="domain_constraint_pass",
                value=1.0 if passed_domain_constraint else 0.0,
                data_type="BOOLEAN",
            )

    def log_turn_summary(
        self,
        *,
        retrieval_hits: int | None,
        extraction_confidence: list[str] | None,
        eval_outcome: bool | None,
        cost_usd: float | None = None,
        node_metrics: dict | None = None,
    ) -> None:
        """PRD Section 4.6, Core Requirement 7 (verbatim from the PDF):
        "retrieval hits (count of guideline chunks retrieved for the
        turn), extraction confidence (the tiered enum from [Section 4.2],
        surfaced per extracted field), and eval outcome" -- defined per
        Decision 6.4 as a live pass/fail quality signal on every real turn
        (verification passed, domain constraints held, schema valid,
        citation present), generalizing Week 1's existing
        verification_passed/domain_constraint_pass fields -- NOT a formal
        eval-suite result, since there is no eval suite run against a real
        conversation. Only app/graph's multi-worker path has all three
        (retrieval_hits/extraction_confidence are None -- not 0/[] -- on
        Week 1's single-worker run_turn(), which never touches
        evidence_retriever/intake_extractor at all: None distinguishes
        "this worker didn't run" from "it ran and found nothing," the same
        distinction SummarizeShiftEventsOutput.data_gathered already makes
        elsewhere in this codebase). Call once, near the end of a turn,
        before finish_turn() ends the span.

        cost_usd: added 2026-09-24, the 7th Core Requirement 7 field this
        method was missing -- per-node cost_usd already existed in
        node_metrics (app/graph/metrics.py's NodeTimer.metrics(), computed
        from real token counts at Claude Sonnet 5's published per-MTok
        price) but was never rolled up into one per-turn total anywhere a
        caller could read it. app/graph/supervisor.py's synthesize_node
        passes sum(m['cost_usd'] for m in state['node_metrics'].values()).
        Optional/None (not 0.0) on Week 1's single-worker path, which has
        no node_metrics dict to sum at all -- same "worker didn't run,
        not ran-and-found-nothing" distinction the other two fields
        already use.

        node_metrics: added 2026-09-24, closing PRD Core Requirement 7's
        "latency by step" -- per-node {latency_ms, input_tokens,
        output_tokens, cost_usd} (app/graph/metrics.py's NodeTimer)
        already existed in graph state, but was computed and then
        discarded once a turn finished; this is the first place it's
        actually persisted anywhere, local log or Langfuse. PHI-free by
        construction -- every value is a number keyed by a fixed node
        name (e.g. "chart_data_worker", "supervisor_synthesize"), never
        patient data, chart content, or free text of any kind, the same
        allowlisted-fields discipline every other field in this method
        already follows."""
        _log(
            "turn_summary",
            correlation_id=self.correlation_id,
            retrieval_hits=retrieval_hits,
            extraction_confidence=extraction_confidence,
            eval_outcome=eval_outcome,
            cost_usd=cost_usd,
            node_metrics=node_metrics,
        )
        if self.span is not None and eval_outcome is not None:
            self.span.score_trace(name="eval_outcome", value=1.0 if eval_outcome else 0.0, data_type="BOOLEAN")
        if self.span is not None and cost_usd is not None:
            self.span.score_trace(name="turn_cost_usd", value=cost_usd, data_type="NUMERIC")

    def finish_turn(self, *, final_response: str) -> None:
        """final_response never appears here -- see start_turn()'s
        docstring for why (2026-09-22 retrofit)."""
        _log("turn_finished", correlation_id=self.correlation_id, response_length=len(final_response))
        if self.span is not None:
            self.span.update(output={"response_length": len(final_response)})
            self.span.end()
            self.client.flush()
