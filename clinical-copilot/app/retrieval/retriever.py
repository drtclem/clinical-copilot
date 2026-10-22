"""Orchestrates PRD Section 4.3's hybrid RAG + rerank pipeline: chunk once,
dense + sparse retrieval, RRF fusion, cross-encoder rerank.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from app.retrieval.chunking import load_corpus
from app.retrieval.embeddings import DenseIndex
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.rerank import Reranker
from app.retrieval.sparse import SparseIndex
from app.retrieval.types import RetrievedEvidence

# Retrieve more candidates than we finally return, so the reranker (cheap
# on a handful of candidates -- PRD Section 4.3's own accepted tradeoff
# reasoning) has a real pool to work with rather than reranking an
# already-narrow top-k.
_FUSION_CANDIDATES = 10
_FINAL_TOP_K = 4

# Relevance floor on the reranker's own score (cross-encoder/ms-marco-
# MiniLM-L-6-v2's raw logit, unbounded, not a 0-1 probability -- see
# app/retrieval/rerank.py). Without this, retrieve() always returned its
# top-4 nearest chunks regardless of true topical relevance -- RRF fusion
# and reranking both rank a fixed candidate pool, neither has a concept of
# "none of these are actually relevant." Found live 2026-09-24: a
# pancreatitis question retrieved hyperkalemia_management and
# sepsis_screening chunks into `retrieved_evidence` (and from there into
# the response's own `citations` list) even though the corpus has nothing
# on pancreatitis -- the synthesis model happened to notice and declined
# to use them that run, but the CITATIONS themselves still listed two
# irrelevant guidelines as sources, which nothing guaranteed a different
# run/phrasing would also catch. A citation is metadata a caller may
# reasonably trust without re-reading the narrative; it must never claim
# irrelevant material backs an answer, independent of what the narrative
# says.
#
# Threshold empirically calibrated (2026-09-24), NOT tuned against any eval
# case: 8 realistic in-scope questions (2 per corpus doc) scored
# min=-2.727 max=4.909 mean=1.466; 8 realistic out-of-scope questions
# (pancreatitis-style topics genuinely absent from the corpus) scored
# min=-10.528 max=-4.117 mean=-7.101 -- a clean 1.389-point gap between the
# lowest in-scope score and the highest out-of-scope score. -3.4 sits
# roughly at the midpoint (~0.7-point margin on each side). Full
# calibration set, score distributions, and this rationale are also in
# W2_ARCHITECTURE.md Sec. 5.
_MIN_RELEVANCE_SCORE = -3.4


class GuidelineRetriever:
    def __init__(self):
        chunks = load_corpus()
        self._dense = DenseIndex(chunks)
        self._sparse = SparseIndex(chunks)
        self._reranker = Reranker()

    def retrieve(self, queries: list[str]) -> list[RetrievedEvidence]:
        """queries: up to a few distinct clinical-concept phrases for the
        SAME turn (app/retrieval/query.py's build_safe_query can return
        more than one when a question spans multiple topics). Each gets
        its own dense+sparse+fusion pass -- a single embedding/BM25 query
        can't represent two unrelated concepts at once -- and the fused
        candidate pools are merged (deduped by chunk_id) BEFORE reranking,
        so a chunk either topic's own search surfaced is a real candidate
        for the one rerank call. Found live: a genuinely two-topic
        question (fever/sepsis-severity AND antibiotic/allergy selection)
        previously depended entirely on one concept-extraction call
        picking a single phrase that happened to represent both topics at
        once -- if it emphasized only one, the other topic's guideline was
        never even in the candidate pool to begin with, no matter how
        well reranking or the relevance floor worked afterward. Reranked
        once PER PHRASE over the merged pool, each chunk keeping its best
        score (see the comment where that happens for the measurements
        that replaced the earlier single rerank of the joined phrases)."""
        queries = [q for q in queries if q]
        if not queries:
            return []
        merged_candidates: dict[str, object] = {}
        for query in queries:
            dense_ranked = self._dense.search(query, top_k=_FUSION_CANDIDATES)
            sparse_ranked = self._sparse.search(query, top_k=_FUSION_CANDIDATES)
            fused = reciprocal_rank_fusion(dense_ranked, sparse_ranked, top_k=_FUSION_CANDIDATES)
            for chunk in fused:
                merged_candidates.setdefault(chunk.chunk_id, chunk)
        candidates = list(merged_candidates.values())
        # Rerank the merged pool against EACH phrase on its own and score every chunk by its best
        # phrase. This used to be ONE rerank against the phrases joined with " ; ", which measurably
        # dilutes the cross-encoder: measured 2026-09-25 with the real models, "sepsis screening fever
        # productive cough" scores +0.21 alone but -2.08 once joined with "antibiotic selection sulfa
        # allergy", and "sulfonamide allergy antibiotic cross-reactivity" scores +2.4 alone but -0.2
        # joined -- enough for a genuine two-topic question to fall under the relevance floor and be
        # reported as "no relevant guideline". A single phrase scores identically either way, and the
        # out-of-scope calibration questions all stay far below the floor (best -7.2 vs the -3.4 floor).
        best: dict[str, tuple[object, float]] = {}
        for query in queries:
            for chunk, score in self._reranker.rerank(query, candidates, top_k=len(candidates)):
                if chunk.chunk_id not in best or score > best[chunk.chunk_id][1]:
                    best[chunk.chunk_id] = (chunk, score)
        ranked = sorted(best.values(), key=lambda pair: pair[1], reverse=True)[:_FINAL_TOP_K]
        # Per-chunk, not all-or-nothing on the top score: a genuinely
        # relevant question can retrieve one strongly relevant chunk
        # alongside a weaker, borderline one -- only chunks that
        # individually clear the floor survive.
        return [
            RetrievedEvidence(chunk=chunk, rerank_score=score)
            for chunk, score in ranked
            if score >= _MIN_RELEVANCE_SCORE
        ]


# lru_cache alone lets concurrent first callers each load both models.
_retriever_lock = threading.Lock()


def get_retriever() -> GuidelineRetriever:
    """Process-wide singleton. Loading two self-hosted models and
    embedding the corpus is expensive enough (measured ~565MB combined
    resident memory, PRD Section 4.3) that doing it once per process --
    not once per turn -- is the only sane default at this scale. Safe to
    share across requests/patients: GuidelineRetriever holds no
    request-scoped state at all (no patient data, no FhirClient) -- only
    the fixed, non-patient-specific guideline corpus and its two model
    instances, exactly the kind of shared, stateless resource a
    process-wide cache is for."""
    with _retriever_lock:
        return _build_retriever()


@lru_cache(maxsize=1)
def _build_retriever() -> GuidelineRetriever:
    return GuidelineRetriever()
