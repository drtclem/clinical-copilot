"""Dense retrieval half of PRD Section 4.3's hybrid RAG: self-hosted
sentence-transformers/all-MiniLM-L6-v2. Paired with app/retrieval/rerank.py's
cross-encoder -- see that module's docstring for the full BGE-vs-MiniLM
memory tradeoff both models were chosen under (~565MB combined, measured,
vs. bge-reranker-base alone at 1.49GB).
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from app.retrieval.chunking import GuidelineChunk

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class DenseIndex:
    """Embeds the corpus once, at construction, and keeps vectors
    in-memory -- the corpus is small and curated (PRD Section 4.3), so a
    real vector database is not warranted at this scale. Not safe to
    mutate after construction (no add/remove) -- the corpus is fixed
    content, not a live-updated index."""

    def __init__(self, chunks: list[GuidelineChunk], model: SentenceTransformer | None = None):
        self._chunks = chunks
        self._model = model or SentenceTransformer(_MODEL_NAME)
        self._embeddings = (
            np.asarray(self._model.encode([c.text for c in chunks], normalize_embeddings=True, show_progress_bar=False))
            if chunks
            else np.zeros((0, 0))
        )

    def search(self, query: str, top_k: int) -> list[tuple[GuidelineChunk, float]]:
        """Cosine similarity (both sides normalized, so a plain dot
        product is cosine similarity) -- ranked descending, top_k."""
        if not self._chunks or not query:
            return []
        query_vec = np.asarray(self._model.encode([query], normalize_embeddings=True, show_progress_bar=False))[0]
        scores = self._embeddings @ query_vec
        ranked_indices = np.argsort(-scores)[:top_k]
        return [(self._chunks[i], float(scores[i])) for i in ranked_indices]
