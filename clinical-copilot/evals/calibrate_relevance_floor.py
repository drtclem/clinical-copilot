#!/usr/bin/env python3
"""Reproduces the empirical calibration behind app/retrieval/retriever.py's
_MIN_RELEVANCE_SCORE (found/fixed 2026-09-24, see W2_ARCHITECTURE.md Sec.
5.1) -- a real, committed artifact, not just a number pasted into a commit
message. Run this again if the corpus changes, the reranker model changes,
or the threshold is ever revisited.

A separate calibration set from the golden set's own eval cases, on
purpose -- evals/cases.py's rag_no_matching_guideline_honest_no_evidence_
and_no_fabricated_citation and rag_vague_broader_than_corpus_question_
honest_no_match were never used to choose this threshold, only to verify
its effect afterward.

Usage:
    python3 -m evals.calibrate_relevance_floor
"""

from __future__ import annotations

from app.retrieval.chunking import load_corpus
from app.retrieval.embeddings import DenseIndex
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.rerank import Reranker
from app.retrieval.sparse import SparseIndex

# 8 realistic in-scope questions, 2 per corpus doc -- phrased the way a
# resident would actually ask, not keyword lists.
IN_SCOPE = [
    ("hyperkalemia", "What's the recommended treatment sequence for severe hyperkalemia with ECG changes?"),
    ("hyperkalemia", "When should I be worried about a hyperkalemic patient's kidney function affecting management?"),
    ("sepsis", "What bedside score can help me decide if a febrile patient needs escalated sepsis workup?"),
    ("sepsis", "What's considered source control in the context of a septic patient?"),
    ("anticoagulation", "How should I approach reversing a DOAC in a patient with life-threatening bleeding?"),
    ("anticoagulation", "What's the general approach if a patient on warfarin needs urgent reversal for surgery?"),
    ("antibiotic", "If a patient has a documented penicillin allergy, which antibiotic classes carry meaningful cross-reactivity risk?"),
    ("antibiotic", "How reliable is a reported penicillin allergy history when deciding on cephalosporin use?"),
]

# 8 realistic out-of-scope questions -- real clinical topics genuinely
# absent from the 4-doc corpus (hyperkalemia_management, sepsis_screening,
# anticoagulation_reversal, antibiotic_cross_reactivity).
OUT_OF_SCOPE = [
    ("pancreatitis", "What's the general approach to managing acute pancreatitis?"),
    ("afib", "How should I evaluate a patient with new-onset atrial fibrillation with rapid ventricular rate?"),
    ("PE", "What's the standard workup for a suspected pulmonary embolism?"),
    ("DKA", "How do I approach a patient presenting with diabetic ketoacidosis?"),
    ("asthma", "What's the general management of an acute asthma exacerbation?"),
    ("stroke", "How should I think about a patient with suspected acute stroke within the treatment window?"),
    ("GI bleed", "What's the approach to a patient with a suspected upper GI bleed?"),
    ("pneumonia", "How do I manage a patient with acute community-acquired pneumonia?"),
]


def _top_rerank_score(dense: DenseIndex, sparse: SparseIndex, reranker: Reranker, query: str) -> float:
    dense_ranked = dense.search(query, top_k=10)
    sparse_ranked = sparse.search(query, top_k=10)
    fused = reciprocal_rank_fusion(dense_ranked, sparse_ranked, top_k=10)
    reranked = reranker.rerank(query, fused, top_k=4)
    return reranked[0][1] if reranked else float("-inf")


def main() -> int:
    chunks = load_corpus()
    dense = DenseIndex(chunks)
    sparse = SparseIndex(chunks)
    reranker = Reranker()

    print("=== IN-SCOPE (should score high) ===")
    in_scores = []
    for topic, q in IN_SCOPE:
        score = _top_rerank_score(dense, sparse, reranker, q)
        in_scores.append(score)
        print(f"{score:8.3f}  [{topic}]  {q}")

    print("\n=== OUT-OF-SCOPE (should score low) ===")
    out_scores = []
    for topic, q in OUT_OF_SCOPE:
        score = _top_rerank_score(dense, sparse, reranker, q)
        out_scores.append(score)
        print(f"{score:8.3f}  [{topic}]  {q}")

    print(f"\nin-scope:  min={min(in_scores):.3f} max={max(in_scores):.3f} mean={sum(in_scores) / len(in_scores):.3f}")
    print(f"out-scope: min={min(out_scores):.3f} max={max(out_scores):.3f} mean={sum(out_scores) / len(out_scores):.3f}")
    print(f"gap (in-scope min) - (out-of-scope max) = {min(in_scores) - max(out_scores):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
