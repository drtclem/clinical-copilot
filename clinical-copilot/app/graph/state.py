"""Shared graph state, PRD Section 3 -- verbatim, this is the actual build
spec for the schema, not a paraphrase.

`CoPilotState` is threaded through every node (`app/graph/supervisor.py`,
`app/graph/workers.py`) by LangGraph's own state-passing mechanism; a node
function receives the current state and returns a partial dict of the keys
it updates. Annotated fields carry a reducer so multiple nodes writing to
the same key in one superstep (a routed fan-out to several workers) merge
rather than clobber -- see each reducer's own docstring below.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from typing_extensions import TypedDict


def _merge_node_metrics(a: dict[str, "NodeMetrics"], b: dict[str, "NodeMetrics"]) -> dict[str, "NodeMetrics"]:
    """Multiple nodes write to node_metrics in one turn: merge, don't let
    the second write clobber the first. Each node writes under its own key
    (its node name), so a plain dict union is correct here -- unlike
    workers_invoked/partial_failures/needs_review_flags (operator.add),
    two different nodes never share a node_metrics key, so there is nothing
    to concatenate, only to combine into one dict."""
    return {**a, **b}


class NodeMetrics(TypedDict):
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CoPilotState(TypedDict):
    # Turn identity & threading
    correlation_id: str
    resident_username: str | None
    patient_id: str
    user_message: str
    messages: list[Any]  # provider-native history

    # Routing -- must be inspectable
    workers_invoked: Annotated[list[str], operator.add]
    routing_rationale: str

    # Per-worker outputs
    chart_facts: Any | None
    uploaded_document_ref: dict | None  # {doc_id, doc_type} ONLY --
    # never raw bytes/full OCR text
    extracted_facts: list[Any]
    retrieved_evidence: list[Any]

    # Citations -- populated ONCE at synthesis, translating each worker's
    # native shape into this canonical one, BEFORE verification runs
    citations: list[dict]  # {source_type, source_id, page_or_section,
    #  field_or_chunk_id, quote_or_value}

    # Cost/latency, per node, rolled up for the turn
    node_metrics: Annotated[dict[str, NodeMetrics], _merge_node_metrics]

    # Failure handling -- extends Week 1's existing partial_failures
    # convention to all three new workers
    partial_failures: Annotated[list[str], operator.add]
    # Structured notices about expected outcomes (app/graph/notices.py) -- e.g. a guideline search that
    # found nothing relevant. Deliberately NOT partial_failures: not a fault, no warning, no verified-check loss.
    notices: Annotated[list[dict], operator.add]

    # Output & hard rules
    draft_response: str | None
    needs_review_flags: Annotated[list[str], operator.add]
    verification_outcome: dict | None
    final_response: str | None


# Worker node names, as they appear in workers_invoked/routing decisions/
# node_metrics keys -- a single place both app/graph/supervisor.py's
# routing tool schema and app/graph/build.py's conditional-edge dispatch
# import from, so the two can never drift into naming a worker differently.
CHART_DATA_WORKER = "chart_data_worker"
INTAKE_EXTRACTOR = "intake_extractor"
EVIDENCE_RETRIEVER = "evidence_retriever"

WorkerName = Literal["chart_data_worker", "intake_extractor", "evidence_retriever"]
ALL_WORKERS: tuple[WorkerName, ...] = (CHART_DATA_WORKER, INTAKE_EXTRACTOR, EVIDENCE_RETRIEVER)
