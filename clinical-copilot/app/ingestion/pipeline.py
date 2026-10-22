"""attach_and_extract: the one-shot orchestration PRD Section 4.2 names as
the ingestion tool ("PDF's own wording -- naming is flexible"). Runs ONCE,
at upload time -- app/graph/workers.py's intake_extractor_node and
app/agent.py's 7th tool (get_uploaded_documents) both read back what this
function already stored, never re-running OCR on a later turn (PRD Section
4.2).

Pipeline: OCR (app/ingestion/ocr.py) -> LLM structuring
(app/ingestion/structuring.py) -> numeric-plausibility downgrade
(app/ingestion/plausibility.py) -> store both documents
(app/ingestion/storage.py). In-memory only throughout -- `file_bytes`
never touches this process' own disk (PRD Section 4.2's hard rule).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.fhir_client import FhirClient, FhirRequestError
from app.ingestion.ocr import EmptyFileError, PageLimitError, UnsupportedFileTypeError, run_ocr
from app.ingestion.plausibility import apply_plausibility_check
from app.ingestion.schemas import (
    DocType,
    DocumentIngestionFailure,
    ExtractionResult,
    IntakeFormExtraction,
    LabPdfExtraction,
)
from app.ingestion.storage import DuplicateDocumentError, compute_document_ids, store_extraction
from app.ingestion.structuring import structure_document
from app.llm_client import PROVIDER_ERRORS, LLMClient, TruncatedOutputError
from app.observability import TurnTrace, log_event

# An extraction with no fields from a page with this many OCR'd words is not "an empty document": something
# went wrong between the model and the schema. Below it (a near-blank page) zero fields is a believable answer.
_MIN_OCR_WORDS_FOR_EMPTY_TO_BE_SUSPECT = 25
_STRUCTURING_ATTEMPTS = 2  # the model is not pinned to a temperature, so a second attempt can differ from the first


def _field_count(extraction: LabPdfExtraction | IntakeFormExtraction) -> int:
    """How many extracted values there are: one per lab result row, one per intake field."""
    if isinstance(extraction, LabPdfExtraction):
        return len(extraction.results)
    demographics = extraction.demographics
    single = (demographics.name, demographics.date_of_birth, demographics.sex, extraction.chief_concern)
    return (
        sum(1 for field in single if field is not None)
        + len(extraction.current_medications)
        + len(extraction.allergies)
        + len(extraction.family_history)
    )


def attach_and_extract(
    patient_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    doc_type: DocType,
    *,
    fhir: FhirClient,
    llm: LLMClient,
    model: str,
    trace: TurnTrace,
) -> tuple[ExtractionResult | None, DocumentIngestionFailure | None, int, int]:
    """Returns (result, failure, input_tokens, output_tokens) -- exactly
    one of result/failure is non-None, matching app/schemas.py's
    ToolFailure convention (ARCHITECTURE.md Section 2/4: a failure is
    always a structured value the caller must surface, never a silent
    swallow)."""
    try:
        ocr_result = run_ocr(file_bytes, content_type)
    except UnsupportedFileTypeError as exc:
        return None, DocumentIngestionFailure(reason=str(exc), detail_code="invalid_file_type"), 0, 0
    except EmptyFileError as exc:
        return None, DocumentIngestionFailure(reason=str(exc), detail_code="empty_file"), 0, 0
    except PageLimitError as exc:
        return None, DocumentIngestionFailure(reason=str(exc), detail_code="too_many_pages"), 0, 0
    except Exception:  # noqa: BLE001 -- any OCR engine failure degrades to the same honest disclosure
        return (
            None,
            DocumentIngestionFailure(reason="OCR failed on this document.", detail_code="ocr_failed"),
            0,
            0,
        )

    ocr_word_count = sum(len(page.words) for page in ocr_result.pages)
    input_tokens = output_tokens = 0
    for attempt in range(1, _STRUCTURING_ATTEMPTS + 1):
        try:
            extraction, attempt_input_tokens, attempt_output_tokens = structure_document(
                ocr_result, doc_type, llm=llm, model=model, trace=trace
            )
        except TruncatedOutputError:
            return (
                None,
                DocumentIngestionFailure(
                    reason="This document was too long to extract completely, so nothing was saved -- "
                    "the extraction was cut off and could be missing fields.",
                    detail_code="structuring_truncated",
                ),
                0,
                0,
            )
        except PROVIDER_ERRORS:
            return (
                None,
                DocumentIngestionFailure(
                    reason="The assistant is temporarily unavailable, so this document was not processed. "
                    "Please try again in a few minutes.",
                    detail_code="structuring_failed",
                ),
                0,
                0,
            )
        except Exception:  # noqa: BLE001 -- a structuring-model failure degrades to the same honest disclosure
            return (
                None,
                DocumentIngestionFailure(
                    reason="Structuring the document's contents failed.", detail_code="structuring_failed"
                ),
                0,
                0,
            )
        input_tokens += attempt_input_tokens
        output_tokens += attempt_output_tokens
        field_count = _field_count(extraction)
        # Counts only, never content: this is what makes the next "no fields extracted" diagnosable from the log.
        log_event(
            "structuring_result",
            doc_type=doc_type,
            attempt=attempt,
            field_count=field_count,
            ocr_word_count=ocr_word_count,
            ocr_avg_confidence=round(ocr_result.avg_confidence, 1),
        )
        if field_count > 0 or ocr_word_count < _MIN_OCR_WORDS_FOR_EMPTY_TO_BE_SUSPECT:
            break
    else:
        # Every attempt came back empty for a page with real text on it. Storing that would put an empty
        # "AI-extracted" document on the chart and, because uploads are de-duplicated by content, block the
        # resident from simply uploading the file again. Fail before anything is saved.
        return (
            None,
            DocumentIngestionFailure(
                reason="No fields could be read from this document, so nothing was saved.",
                detail_code="structuring_failed",
            ),
            input_tokens,
            output_tokens,
        )

    # Numeric-value safety check (PRD Section 4.2, required, not optional):
    # every lab_pdf numeric field's confidence is re-derived against its
    # own reference_range, regardless of what OCR/structuring reported.
    if isinstance(extraction, LabPdfExtraction):
        for result in extraction.results:
            result.value.confidence = apply_plausibility_check(
                result.value.value, result.reference_range.value, result.value.confidence
            )

    source_doc_id, extracted_doc_id = compute_document_ids(patient_id, doc_type, filename, file_bytes)
    result = ExtractionResult(
        patient_id=patient_id,
        doc_id=source_doc_id,
        extracted_doc_id=extracted_doc_id,
        doc_type=doc_type,
        extraction=extraction,
        ocr_avg_confidence=round(ocr_result.avg_confidence, 1),
        uploaded_on=datetime.now(timezone.utc).date().isoformat(),
    )

    try:
        store_extraction(fhir, patient_id, file_bytes, content_type, source_doc_id, extracted_doc_id, result)
    except DuplicateDocumentError as exc:
        return None, DocumentIngestionFailure(reason=str(exc), detail_code="duplicate"), input_tokens, output_tokens
    except FhirRequestError:
        return (
            None,
            DocumentIngestionFailure(
                reason="Could not save this document to the patient's chart.", detail_code="storage_failed"
            ),
            input_tokens,
            output_tokens,
        )

    return result, None, input_tokens, output_tokens
