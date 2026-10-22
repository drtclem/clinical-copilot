"""Offline tests (no network, no FHIR, no LLM call) for the strict-schema and
citation-contract fixes (PRD Core Requirements 2 and 5): every ingestion and
citation schema forbids unknown keys, the Citation's five contract fields are
required and non-empty, the fallback values for an unknown page / a ref without
a slash are stated rather than guessed, document citations carry the real field
path, and DocumentCitation.bbox is a valid normalized box.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

from typing import Callable

from pydantic import ValidationError

from app.agent import ChartGatherResult
from app.graph.citations import PAGE_UNKNOWN, Citation, chart_citations, document_citations
from app.ingestion.bbox import resolve_bbox
from app.ingestion.ocr import OcrPage, OcrResult, OcrWord
from app.ingestion.schemas import (
    DemographicsFact,
    DocumentCitation,
    ExtractedField,
    ExtractionResult,
    IntakeFormExtraction,
    LabPdfExtraction,
    LabResultFact,
)
from app.ingestion.structuring import structure_document
from app.schemas import (
    ConditionFact,
    GetPatientSnapshotOutput,
    GetUploadedDocumentsOutput,
    UploadedDocumentFactSummary,
    UploadedDocumentSummary,
)
from app.verification import ToolCallRecord
from evals.call_site_tests import _RecordingLLM, _tool_response, _trace


def _citation(**overrides) -> DocumentCitation:
    return DocumentCitation(**{"page_number": 1, "bbox": (0.1, 0.1, 0.2, 0.2), "quote_or_value": "1.1", **overrides})


def _field(value: str = "1.1") -> ExtractedField:
    return ExtractedField(value=value, confidence="high", citation=_citation())


def _lab() -> LabResultFact:
    return LabResultFact(
        test_name=_field("Creatinine"), value=_field("1.1"), unit=_field("mg/dL"),
        reference_range=_field("0.6-1.2"), collection_date=_field("2026-09-20"), abnormal_flag=_field("normal"),
    )


def _result(extraction=None, doc_type: str = "lab_pdf") -> ExtractionResult:
    return ExtractionResult(
        patient_id="p1", doc_id="src.png", extracted_doc_id="ext.json", doc_type=doc_type,
        extraction=extraction or LabPdfExtraction(results=[_lab()]), ocr_avg_confidence=90.0,
    )


def _rejects(build: Callable[[], object], what: str) -> None:
    try:
        build()
    except ValidationError:
        return
    raise AssertionError(f"{what} should raise ValidationError, but was accepted")


def _chart_facts(records: list[ToolCallRecord]) -> ChartGatherResult:
    return ChartGatherResult(
        draft_text="d", turn_records=records, tool_calls=[], updated_messages=[],
        total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
    )


# --- A1: unknown keys are rejected on every ingestion and citation schema ---


def test_every_ingestion_schema_rejects_an_unknown_key() -> None:
    lab_kwargs = {n: _field() for n in ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag")}
    _rejects(lambda: DocumentCitation(page_number=1, quote_or_value="x", extra_key=1), "DocumentCitation with an unknown key")
    _rejects(lambda: ExtractedField(value="1", confidence="high", citation=_citation(), notes="n"), "ExtractedField with an unknown key")
    _rejects(lambda: LabResultFact(**lab_kwargs, notes="n"), "LabResultFact with an unknown key")
    _rejects(lambda: LabPdfExtraction(results=[], notes="n"), "LabPdfExtraction with an unknown key")
    _rejects(lambda: DemographicsFact(name=_field(), age=40), "DemographicsFact with an unknown key")
    _rejects(
        lambda: IntakeFormExtraction(demographics=DemographicsFact(), notes="n"), "IntakeFormExtraction with an unknown key"
    )
    _rejects(
        lambda: ExtractionResult(
            patient_id="p", doc_id="d", doc_type="lab_pdf", extraction=LabPdfExtraction(results=[]),
            ocr_avg_confidence=1.0, notes="n",
        ),
        "ExtractionResult with an unknown key",
    )


def test_citation_rejects_an_unknown_key() -> None:
    _rejects(
        lambda: Citation(
            source_type="guideline", source_id="g", page_or_section="s", field_or_chunk_id="c", quote_or_value="q", extra=1
        ),
        "Citation with an unknown key",
    )


def test_a_stored_extraction_round_trips_through_json_under_forbid() -> None:
    """Storage writes model_dump_json() and reads model_validate(json): the bbox tuple comes back as a
    JSON list, so the schemas must be strict about keys but not about types (no strict=True)."""
    original = _result()
    restored = ExtractionResult.model_validate_json(original.model_dump_json(indent=2))
    assert restored == original, "a stored extraction must validate again unchanged"
    intake = _result(IntakeFormExtraction(demographics=DemographicsFact(name=_field("A B")), allergies=[_field("PCN")]), "intake_form")
    assert ExtractionResult.model_validate_json(intake.model_dump_json()) == intake


def test_an_extra_llm_key_is_dropped_at_the_boundary_and_extraction_still_succeeds() -> None:
    ocr = OcrResult(pages=[OcrPage(page_number=1, words=[OcrWord(text="1.1", confidence=95.0, bbox=(0.1, 0.1, 0.2, 0.2))])])
    field = {"value": "1.1", "page_number": 1, "quote": "1.1", "confidence": "high"}
    row = {n: dict(field) for n in ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag")}
    row["notes"] = {"value": "hedged", "page_number": 1, "quote": "1.1", "confidence": "high"}
    row["flag_reason"] = "free text the model added"
    llm = _RecordingLLM(_tool_response("extract_lab_results", {"results": [row]}))
    extraction, _, _ = structure_document(ocr, "lab_pdf", llm=llm, model="m", trace=_trace())
    assert isinstance(extraction, LabPdfExtraction) and len(extraction.results) == 1
    assert not hasattr(extraction.results[0], "notes")


# --- A2: the citation contract is required, and its fallbacks are honest ---


def test_citation_requires_every_contract_field_for_every_source_type() -> None:
    complete = {"source_id": "s", "page_or_section": "p", "field_or_chunk_id": "f", "quote_or_value": "q"}
    for source_type in ("fhir_resource", "uploaded_document", "guideline"):
        Citation(source_type=source_type, **complete)  # the complete shape is accepted
        for name in complete:
            missing = {k: v for k, v in complete.items() if k != name}
            _rejects(lambda: Citation(source_type=source_type, **missing), f"{source_type} Citation missing {name}")
            _rejects(lambda: Citation(source_type=source_type, **{**complete, name: ""}), f"{source_type} Citation with empty {name}")


def test_fhir_citation_without_a_slash_still_has_a_field_id() -> None:
    snapshot = GetPatientSnapshotOutput(
        patient_id="p1", name="T P", birth_date=None, gender=None,
        conditions=[ConditionFact(text="Diabetes", category="problem-list-item", clinical_status=None, source_resource="Condition1")],
        medications=[], allergies=[], chart_is_empty=False, duplicate_warnings=[],
    )
    cites = chart_citations(_chart_facts([ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p1", output=snapshot)]), "p1")
    assert len(cites) == 1
    assert cites[0].field_or_chunk_id == "Condition1", "with no '/', the full ref is the field id, not None"
    assert cites[0].page_or_section == "get_patient_snapshot"


def _uploaded_docs_output(page_number: int | None) -> GetUploadedDocumentsOutput:
    fact = UploadedDocumentFactSummary(
        field_name="results[0].value", value="7.8", confidence="high", source_resource="document:d#value", page_number=page_number
    )
    doc = UploadedDocumentSummary(doc_id="d.png", doc_type="lab_pdf", facts=[fact], ocr_avg_confidence=90.0, upload_rank=0)
    return GetUploadedDocumentsOutput(patient_id="p1", documents=[doc])


def test_uploaded_document_citation_with_no_page_says_page_unknown() -> None:
    record = ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=_uploaded_docs_output(None))
    (cite,) = chart_citations(_chart_facts([record]), "p1")
    assert cite.page_or_section == PAGE_UNKNOWN == "page unknown", cite.page_or_section
    assert cite.field_or_chunk_id == "results[0].value"
    record = ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=_uploaded_docs_output(3))
    (cite,) = chart_citations(_chart_facts([record]), "p1")
    assert cite.page_or_section == "3", "a known page is still reported as that page"


def test_a_blank_extracted_value_yields_no_citation_instead_of_an_invalid_one() -> None:
    lab = _lab()
    lab.value = _field("")
    cites = document_citations([_result(LabPdfExtraction(results=[lab]))])
    assert len(cites) == 5 and all(c.quote_or_value for c in cites), [c.field_or_chunk_id for c in cites]


# --- A3: document field ids are the real field paths -----------------------


def test_document_citations_use_field_paths_as_field_ids() -> None:
    cites = document_citations([_result()])
    assert [c.field_or_chunk_id for c in cites] == [
        f"results[0].{n}" for n in ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag")
    ], [c.field_or_chunk_id for c in cites]
    assert cites[1].field_label == "Value (result 1)", "the readable label is unchanged"

    intake = _result(
        IntakeFormExtraction(
            demographics=DemographicsFact(name=_field("A B")), chief_concern=_field("cough"),
            current_medications=[_field("metformin"), _field("lisinopril")], allergies=[_field("PCN")],
        ),
        "intake_form",
    )
    ids = [c.field_or_chunk_id for c in document_citations([intake])]
    assert ids == ["demographics.name", "chief_concern", "current_medications[0]", "current_medications[1]", "allergies[0]"], ids


# --- A4: bbox validation and clamping --------------------------------------


def test_bbox_must_be_a_normalized_ordered_box() -> None:
    _citation(bbox=(0.0, 0.0, 1.0, 1.0))
    _citation(bbox=None)
    _citation(bbox=(0.1, 0.2, 0.1, 0.2))  # a zero-area box is still ordered
    for bad in [(-0.01, 0.1, 0.2, 0.2), (0.1, 0.1, 1.01, 0.2), (0.3, 0.1, 0.2, 0.2), (0.1, 0.3, 0.2, 0.2)]:
        _rejects(lambda: _citation(bbox=bad), f"bbox {bad}")


def test_resolve_bbox_clamps_ocr_rounding_into_the_valid_range() -> None:
    word = OcrWord(text="1.1", confidence=95.0, bbox=(0.9, 0.95, 1.0004, 1.0002))
    ocr = OcrResult(pages=[OcrPage(page_number=1, words=[word])])
    box = resolve_bbox(ocr, 1, "1.1")
    assert box == (0.9, 0.95, 1.0, 1.0), box
    _citation(bbox=box)  # and the validator accepts what the producer emits


_TESTS: list[Callable[[], None]] = [
    test_every_ingestion_schema_rejects_an_unknown_key,
    test_citation_rejects_an_unknown_key,
    test_a_stored_extraction_round_trips_through_json_under_forbid,
    test_an_extra_llm_key_is_dropped_at_the_boundary_and_extraction_still_succeeds,
    test_citation_requires_every_contract_field_for_every_source_type,
    test_fhir_citation_without_a_slash_still_has_a_field_id,
    test_uploaded_document_citation_with_no_page_says_page_unknown,
    test_a_blank_extracted_value_yields_no_citation_instead_of_an_invalid_one,
    test_document_citations_use_field_paths_as_field_ids,
    test_bbox_must_be_a_normalized_ordered_box,
    test_resolve_bbox_clamps_ocr_rounding_into_the_valid_range,
]


def _adapt(fn: Callable[[], None]) -> Callable[[object], tuple[bool, str]]:
    def run(_fhir: object) -> tuple[bool, str]:
        try:
            fn()
        except AssertionError as exc:
            return False, f"assertion failed: {exc}"
        return True, "ok"

    return run


TESTS = [(fn.__name__.removeprefix("test_"), _adapt(fn)) for fn in _TESTS]
