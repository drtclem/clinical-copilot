"""Reciprocal Rank Fusion -- combines app/retrieval/embeddings.py's dense
ranking and app/retrieval/sparse.py's sparse ranking into one fused
ranking (PRD Section 4.3: "Fusion: Reciprocal Rank Fusion (RRF) to combine
sparse + dense results").
"""

from __future__ import annotations

from app.retrieval.chunking import GuidelineChunk

# The standard RRF constant from Cormack, Clarke & Buettcher (2009) --
# dampens the influence of any single high rank so one list's #1 result
# doesn't dominate regardless of the other list's opinion. Not tuned for
# this specific corpus; a real tuning pass is future work if/when the
# corpus grows materially past its current curated size.
_RRF_K = 60


def reciprocal_rank_fusion(
    dense_ranked: list[tuple[GuidelineChunk, float]],
    sparse_ranked: list[tuple[GuidelineChunk, float]],
    top_k: int,
) -> list[GuidelineChunk]:
    scores: dict[str, float] = {}
    chunks_by_id: dict[str, GuidelineChunk] = {}
    for ranked_list in (dense_ranked, sparse_ranked):
        for rank, (chunk, _score) in enumerate(ranked_list):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            chunks_by_id[chunk.chunk_id] = chunk
    ordered_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:top_k]
    return [chunks_by_id[cid] for cid in ordered_ids]
