"""LangGraph orchestration layer (W2_ARCHITECTURE.md, PRD Section 2).

One supervisor entered twice per turn (route, then synthesize+verify),
three peer workers (`app/graph/workers.py`). Node functions call
`app/llm_client.py`'s `LLMClient` Protocol directly -- never a provider SDK,
never a LangChain/LangGraph model wrapper class (PRD Section 2) -- so this
package adds a graph topology on top of Week 1's existing provider-agnostic
seam, not a second, competing one.
"""

from __future__ import annotations

from app.graph.build import build_graph, run_turn
from app.graph.state import CoPilotState, NodeMetrics

__all__ = ["CoPilotState", "NodeMetrics", "build_graph", "run_turn"]
