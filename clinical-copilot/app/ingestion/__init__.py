"""Document ingestion (PRD Section 4.2, Checklist item 4): OCR-first
extraction, LLM/VLM structuring, numeric-plausibility safety check, and the
Standard API write path -- app/graph/workers.py's intake_extractor_node and
app/agent.py's 7th tool (get_uploaded_documents) are the intended callers
of this package's public surface."""

from __future__ import annotations

from app.ingestion.pipeline import attach_and_extract
from app.ingestion.schemas import (
    DocType,
    DocumentIngestionFailure,
    ExtractionResult,
    LabPdfExtraction,
    IntakeFormExtraction,
)
from app.ingestion.storage import list_extracted_documents

__all__ = [
    "attach_and_extract",
    "list_extracted_documents",
    "DocType",
    "DocumentIngestionFailure",
    "ExtractionResult",
    "LabPdfExtraction",
    "IntakeFormExtraction",
]
