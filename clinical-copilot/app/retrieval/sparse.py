"""Sparse retrieval half of PRD Section 4.3's hybrid RAG: BM25 (rank_bm25)
over the same chunked corpus app/retrieval/embeddings.py indexes densely.
No separate search-engine process -- this corpus is a handful of curated
documents, not a real search index (see app/retrieval/corpus/README.md).
"""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from app.retrieval.chunking import GuidelineChunk

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class SparseIndex:
    def __init__(self, chunks: list[GuidelineChunk]):
        self._chunks = chunks
        self._bm25 = BM25Okapi([_tokenize(c.text) for c in chunks]) if chunks else None

    def search(self, query: str, top_k: int) -> list[tuple[GuidelineChunk, float]]:
        if not self._chunks or not query or self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked_indices = sorted(range(len(self._chunks)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [(self._chunks[i], float(scores[i])) for i in ranked_indices]
