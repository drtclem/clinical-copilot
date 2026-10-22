"""Reranker half of PRD Section 4.3's hybrid RAG + rerank:
cross-encoder/ms-marco-MiniLM-L-6-v2, self-hosted -- **deliberately not
BGE-family**. Measured directly: bge-reranker-base alone loads to 1.49GB,
over half of staging's entire confirmed headroom. The MiniLM pair (this
reranker + app/retrieval/embeddings.py's embedder) measures a realistic
combined ~565MB, both models sharing one process, as GuidelineRetriever
(app/retrieval/retriever.py) actually runs them.

**Known, accepted tradeoff:** BGE-family models generally outperform
MiniLM on retrieval benchmarks -- this trades some quality for resource
fit, partially mitigated by the corpus being small and curated (a reranker
has an easier job on a handful of candidates than thousands).
"""

from __future__ import annotations

from sentence_transformers import CrossEncoder

from app.retrieval.chunking import GuidelineChunk

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class Reranker:
    def __init__(self, model: CrossEncoder | None = None):
        self._model = model or CrossEncoder(_MODEL_NAME)

    def rerank(self, query: str, candidates: list[GuidelineChunk], top_k: int) -> list[tuple[GuidelineChunk, float]]:
        if not candidates or not query:
            return []
        pairs = [(query, c.text) for c in candidates]
        scores = self._model.predict(pairs)
        ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
        return [(chunk, float(score)) for chunk, score in ranked[:top_k]]
