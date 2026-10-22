"""Offline tests for the server side of the citation cards: readable field
names and the upload date on document citations (app/graph/citations.py),
plus the upload-date stamp on ExtractionResult. No network, no LLM.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from app.graph.citations import chart_citations, document_citations, humanize_field_path
from app.ingestion.schemas import (
    DocumentCitation, ExtractedField, ExtractionResult, IntakeFormExtraction, DemographicsFact, LabPdfExtraction,
    LabResultFact,
)
from app.schemas import GetUploadedDocumentsOutput, UploadedDocumentFactSummary, UploadedDocumentSummary

PID = "11111111-1111-4111-8111-111111111111"


def _f(value: str) -> ExtractedField:
    return ExtractedField(value=value, confidence="high", citation=DocumentCitation(page_number=2, quote_or_value=value))


def _lab_result(uploaded_on: str | None) -> ExtractionResult:
    fact = LabResultFact(
        test_name=_f("Potassium"), value=_f("7.8"), unit=_f("mmol/L"), reference_range=_f("3.5-5.1"),
        collection_date=_f("2026-09-24"), abnormal_flag=_f("high"),
    )
    return ExtractionResult(
        patient_id=PID, doc_id="doc1.png", doc_type="lab_pdf", ocr_avg_confidence=90.0, uploaded_on=uploaded_on,
        extraction=LabPdfExtraction(results=[fact, fact]),
    )


def test_humanize_field_path_gives_readable_names() -> None:
    expected = {
        "results[0].value": "Value (result 1)",
        "results[1].abnormal_flag": "Abnormal flag (result 2)",
        "demographics.date_of_birth": "Date of birth",
        "chief_concern": "Chief concern",
        "current_medications[0]": "Medication 1",
        "allergies[2]": "Allergy 3",
        "family_history[0]": "Family history 1",
        "some_new_field": "some new field",
    }
    for path, label in expected.items():
        assert humanize_field_path(path) == label, (path, humanize_field_path(path))


def test_same_turn_document_citations_carry_field_label_and_upload_date() -> None:
    cites = document_citations([_lab_result("2026-09-25")])
    assert len(cites) == 12
    assert {c.uploaded_on for c in cites} == {"2026-09-25"}
    assert [c.field_label for c in cites[:2]] == ["Test name (result 1)", "Value (result 1)"]
    assert cites[6].field_label == "Test name (result 2)"
    assert all(c.source_id == "doc1.png" and c.page_or_section == "2" for c in cites)


def test_document_without_an_upload_date_yields_none_not_a_guess() -> None:
    assert {c.uploaded_on for c in document_citations([_lab_result(None)])} == {None}


def test_intake_form_citations_get_labels_for_lists_and_demographics() -> None:
    result = ExtractionResult(
        patient_id=PID, doc_id="intake.png", doc_type="intake_form", ocr_avg_confidence=90.0, uploaded_on="2026-09-25",
        extraction=IntakeFormExtraction(
            demographics=DemographicsFact(name=_f("A B"), date_of_birth=None, sex=None),
            chief_concern=_f("cough"), current_medications=[_f("metformin"), _f("lisinopril")],
            allergies=[_f("penicillin")], family_history=[],
        ),
    )
    labels = [c.field_label for c in document_citations([result])]
    assert labels == ["Name", "Chief concern", "Medication 1", "Medication 2", "Allergy 1"], labels


def test_later_turn_chart_citations_carry_field_label_and_upload_date() -> None:
    docs = GetUploadedDocumentsOutput(
        patient_id=PID,
        documents=[UploadedDocumentSummary(
            doc_id="doc1.png", doc_type="lab_pdf", ocr_avg_confidence=90.0, upload_rank=0, uploaded_on="2026-09-25",
            facts=[UploadedDocumentFactSummary(
                field_name="results[0].value", value="7.8", confidence="high",
                source_resource="document:doc1.png#results[0].value", page_number=1,
            )],
        )],
    )
    record = SimpleNamespace(patient_id=PID, output=docs, tool_name="get_uploaded_documents")
    cites = chart_citations(SimpleNamespace(turn_records=[record]), PID)
    assert len(cites) == 1
    assert cites[0].field_label == "Value (result 1)" and cites[0].uploaded_on == "2026-09-25"
    assert cites[0].doc_label == "Lab report"


_TESTS: list[Callable[[], None]] = [
    test_humanize_field_path_gives_readable_names,
    test_same_turn_document_citations_carry_field_label_and_upload_date,
    test_document_without_an_upload_date_yields_none_not_a_guess,
    test_intake_form_citations_get_labels_for_lists_and_demographics,
    test_later_turn_chart_citations_carry_field_label_and_upload_date,
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
