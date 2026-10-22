"""RetrievedEvidence: evidence_retriever's native output shape
(CoPilotState.retrieved_evidence's element type) -- translated into the
canonical Citation object at the supervisor's synthesis step (PRD Section
4.4), not used directly as a citation itself."""

from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.chunking import GuidelineChunk


@dataclass(frozen=True)
class RetrievedEvidence:
    chunk: GuidelineChunk
    rerank_score: float
