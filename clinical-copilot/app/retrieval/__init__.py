"""Hybrid RAG + rerank over the curated guideline corpus (PRD Section 4.3,
Checklist item 3): app/graph/workers.py's evidence_retriever_node is the
only intended caller of this package's public surface
(build_safe_query, get_retriever)."""

from __future__ import annotations

from app.retrieval.query import build_safe_query
from app.retrieval.retriever import GuidelineRetriever, get_retriever
from app.retrieval.types import RetrievedEvidence

__all__ = ["build_safe_query", "get_retriever", "GuidelineRetriever", "RetrievedEvidence"]
