"""Per-node cost/latency helper, PRD Section 4.6: "node_metrics includes
latency, tokens, cost PER NODE... LangGraph's automatic tracing does not
capture this for free... Token/cost data must be pushed into the span
manually inside each node."

Pricing is confirmed current in COST_ANALYSIS.md ("Claude Sonnet 5: $2/MTok
input, $10/MTok output") -- duplicated here as the one place a node computes
cost_usd from raw token counts, rather than each node reimplementing the
arithmetic. Not settings-driven: a per-model price table would be more
correct for a genuinely multi-model deployment, but ARCHITECTURE.md's Known
tradeoffs already documents Anthropic/Claude as the only validated
provider (app/llm_client.py's module docstring) -- this can grow into a
table keyed by settings.anthropic_model if/when that changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.graph.state import NodeMetrics

# Claude Sonnet 5 published API pricing, USD per million tokens.
# Re-confirmed live against https://claude.com/pricing on 2026-09-24
# (fetched directly, not recalled from training data) -- unchanged from
# the value already in place here. If settings.anthropic_model ever
# changes, these two constants (and this date) must be re-checked and
# updated together, not assumed still correct.
_PRICING_CHECKED_DATE = "2026-09-24"
_INPUT_USD_PER_MTOK = 2.0
_OUTPUT_USD_PER_MTOK = 10.0


def compute_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * _INPUT_USD_PER_MTOK + (output_tokens / 1_000_000) * _OUTPUT_USD_PER_MTOK


@dataclass
class NodeTimer:
    """`with NodeTimer() as t: ...` then `t.metrics(input_tokens, output_tokens)`
    -- a node accumulates tokens across however many LLM calls it makes
    internally (e.g. chart_data_worker's multi-round tool-calling loop) and
    reports one NodeMetrics for the whole node, matching node_metrics'
    per-node (not per-LLM-call) granularity."""

    _wall_t0: float = 0.0

    def __enter__(self) -> "NodeTimer":
        self._wall_t0 = time.monotonic()
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def metrics(self, input_tokens: int, output_tokens: int) -> NodeMetrics:
        latency_ms = (time.monotonic() - self._wall_t0) * 1000
        return NodeMetrics(
            latency_ms=round(latency_ms, 1),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(compute_cost_usd(input_tokens, output_tokens), 6),
        )
