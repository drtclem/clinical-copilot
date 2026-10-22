"""Offline tests for attach_and_extract's handling of an empty extraction (app/ingestion/pipeline.py).

Found live 2026-09-26: an intake form whose OCR was clean (~92% average confidence, every field readable) came
back "No fields extracted": the structuring call succeeded (stop_reason tool_use, output tokens spent) but the
extraction held no usable fields, the pipeline stored that empty document as if it were the answer, and because
uploads are de-duplicated by content the resident could not simply upload the file again.

These drive the real attach_and_extract with a stubbed OCR result and a scripted model (no network, no OCR
engine, no OpenEMR): an empty extraction from a page with real text is retried once, still-empty is refused
BEFORE anything is stored, a near-blank page with no fields is still a believable answer, and every attempt logs
counts only.

Runnable with pytest (zero-arg test_* functions) and registered in evals/unit_tests.py's TESTS via TESTS below.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace as NS
from typing import Callable

import app.ingestion.pipeline as pipeline
from app.ingestion.ocr import OcrPage, OcrResult, OcrWord
from app.ingestion.schemas import IntakeFormExtraction, LabPdfExtraction

PHI_MARKERS = ["Testpatient", "03/14/1972", "Penicillin", "Metformin"]


def _ocr(word_count: int) -> OcrResult:
    words = [OcrWord(text=f"w{i}", confidence=91.0, bbox=(0.0, 0.0, 0.1, 0.1)) for i in range(word_count)]
    return OcrResult(pages=[OcrPage(page_number=1, words=words)])


def _field(value: str) -> dict:
    return {"value": value, "page_number": 1, "quote": value, "confidence": "high"}


FULL_INTAKE = {
    "demographics": {"name": _field("Alice Testpatient"), "date_of_birth": _field("03/14/1972"), "sex": _field("Female")},
    "chief_concern": _field("Follow up for diabetes"),
    "current_medications": [_field("Metformin 500 mg Twice daily")],
    "allergies": [_field("Penicillin - Hives")],
    "family_history": [_field("Father: Heart attack at 61")],
}
EMPTY_INTAKE = {"demographics": {}}


class _Trace:
    def start_llm_call(self, **kwargs) -> int:
        return 1

    def finish_llm_call(self, *args, **kwargs) -> None:
        pass


class _ScriptedLLM:
    """Returns the scripted tool inputs in order (the last one repeats), like a model whose output varies."""

    def __init__(self, tool_name: str, inputs: list[dict]) -> None:
        self._tool_name = tool_name
        self._inputs = inputs
        self.calls = 0

    def create_message(self, **kwargs):
        raw = self._inputs[min(self.calls, len(self._inputs) - 1)]
        self.calls += 1
        return NS(tool_calls=[NS(name=self._tool_name, input=raw)], input_tokens=900, output_tokens=700,
                  stop_reason="tool_use", content_block_types=["tool_use"], thinking_chars=0)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _run(doc_type: str, inputs: list[dict], *, ocr_words: int = 140):
    """Returns (result, failure, llm, stored_count, structuring_result_events, all_log_text)."""
    llm = _ScriptedLLM("extract_lab_results" if doc_type == "lab_pdf" else "extract_intake_form", inputs)
    stored: list[object] = []
    capture = _Capture()
    logger = logging.getLogger("clinical_copilot")
    logger.addHandler(capture)
    real_ocr, real_store = pipeline.run_ocr, pipeline.store_extraction
    pipeline.run_ocr = lambda file_bytes, content_type: _ocr(ocr_words)
    pipeline.store_extraction = lambda *args, **kwargs: stored.append(args)
    try:
        result, failure, _in, _out = pipeline.attach_and_extract(
            "11111111-1111-4111-8111-111111111111", b"scan", "scan.png", "image/png", doc_type,  # type: ignore[arg-type]
            fhir=None, llm=llm, model="test-model", trace=_Trace(),  # type: ignore[arg-type]
        )
    finally:
        pipeline.run_ocr, pipeline.store_extraction = real_ocr, real_store
        logger.removeHandler(capture)
    events = [json.loads(ln) for ln in capture.lines if '"structuring_result"' in ln]
    return result, failure, llm, len(stored), events, "\n".join(capture.lines)


def test_a_populated_extraction_takes_one_call_and_is_stored() -> None:
    result, failure, llm, stored, events, _ = _run("intake_form", [FULL_INTAKE])
    assert failure is None and result is not None
    assert llm.calls == 1 and stored == 1
    assert isinstance(result.extraction, IntakeFormExtraction) and pipeline._field_count(result.extraction) == 7
    assert [(e["attempt"], e["field_count"]) for e in events] == [(1, 7)]


def test_empty_from_a_page_with_text_is_retried_and_the_second_attempt_is_used() -> None:
    result, failure, llm, stored, events, _ = _run("intake_form", [EMPTY_INTAKE, FULL_INTAKE])
    assert failure is None and result is not None
    assert llm.calls == 2 and stored == 1
    assert pipeline._field_count(result.extraction) == 7
    assert [(e["attempt"], e["field_count"]) for e in events] == [(1, 0), (2, 7)]


def test_still_empty_after_the_retry_is_refused_and_nothing_is_stored() -> None:
    result, failure, llm, stored, events, _ = _run("intake_form", [EMPTY_INTAKE])
    assert result is None and failure is not None
    assert failure.detail_code == "structuring_failed"
    assert llm.calls == 2, "exactly one retry, not a loop"
    assert stored == 0, "an empty document must never reach the chart (it would also block re-uploading the file)"
    assert [(e["attempt"], e["field_count"]) for e in events] == [(1, 0), (2, 0)]


def test_a_lab_with_no_result_rows_from_a_page_with_text_is_refused_too() -> None:
    result, failure, llm, stored, _, _ = _run("lab_pdf", [{"results": []}])
    assert result is None and failure is not None and failure.detail_code == "structuring_failed"
    assert llm.calls == 2 and stored == 0


def test_a_near_blank_page_with_no_fields_is_still_a_believable_answer() -> None:
    # 5 OCR'd words: a genuinely (nearly) empty scan. Zero fields is stored, with no retry.
    result, failure, llm, stored, events, _ = _run("intake_form", [EMPTY_INTAKE], ocr_words=5)
    assert failure is None and result is not None
    assert llm.calls == 1 and stored == 1
    assert isinstance(result.extraction, IntakeFormExtraction) and pipeline._field_count(result.extraction) == 0
    assert [(e["attempt"], e["field_count"], e["ocr_word_count"]) for e in events] == [(1, 0, 5)]


def test_the_threshold_is_the_documented_word_count() -> None:
    at, _, llm_at, stored_at, _, _ = _run("intake_form", [EMPTY_INTAKE], ocr_words=pipeline._MIN_OCR_WORDS_FOR_EMPTY_TO_BE_SUSPECT)
    below, _, llm_below, stored_below, _, _ = _run("intake_form", [EMPTY_INTAKE], ocr_words=pipeline._MIN_OCR_WORDS_FOR_EMPTY_TO_BE_SUSPECT - 1)
    assert at is None and llm_at.calls == 2 and stored_at == 0
    assert below is not None and llm_below.calls == 1 and stored_below == 1


def test_a_provider_error_or_bad_tool_call_is_still_a_structuring_failure_not_an_empty_document() -> None:
    result, failure, llm, stored, _, _ = _run("intake_form", [{"demographics": {"name": {"value": "x"}}}])  # missing keys
    assert result is None and failure is not None and failure.detail_code == "structuring_failed"
    assert llm.calls == 1 and stored == 0


def test_structuring_result_logs_counts_only_never_document_content() -> None:
    _, _, _, _, events, text = _run("intake_form", [FULL_INTAKE])
    assert len(events) == 1
    assert set(events[0]) - {"event", "level", "timestamp", "ts", "time", "logger"} <= {
        "doc_type", "attempt", "field_count", "ocr_word_count", "ocr_avg_confidence"}, events[0]
    for marker in PHI_MARKERS:
        assert marker not in text, f"{marker!r} leaked into the structuring log: {text}"


_TESTS: list[Callable[[], None]] = [
    test_a_populated_extraction_takes_one_call_and_is_stored,
    test_empty_from_a_page_with_text_is_retried_and_the_second_attempt_is_used,
    test_still_empty_after_the_retry_is_refused_and_nothing_is_stored,
    test_a_lab_with_no_result_rows_from_a_page_with_text_is_refused_too,
    test_a_near_blank_page_with_no_fields_is_still_a_believable_answer,
    test_the_threshold_is_the_documented_word_count,
    test_a_provider_error_or_bad_tool_call_is_still_a_structuring_failure_not_an_empty_document,
    test_structuring_result_logs_counts_only_never_document_content,
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
