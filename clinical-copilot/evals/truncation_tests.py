"""Offline tests for: synthesis returning no text (the 4096-token empty-answer
turn), the lead-with-a-summary prompt rule, the field_or_chunk_id check that
accepts either path's real id, and the [timing:mock] label. Mocked LLM, no
network, no FHIR.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from types import SimpleNamespace
from typing import Callable

from app.graph.supervisor import _SYNTHESIS_SYSTEM_PROMPT, _fallback_answer, synthesize_node
from app.ingestion.schemas import DocumentCitation, ExtractedField, ExtractionResult, LabPdfExtraction, LabResultFact
from app.llm_client import _EMPTY_TRUNCATION_NOTE, _TRUNCATION_NOTE, LLMTurnResponse, disclose_if_truncated
from app.observability import TurnTrace
from app.retrieval.types import GuidelineChunk, RetrievedEvidence
from app.tools import _flatten_extraction_facts
from evals import parallel
from evals.cases import EvalCase, _check_field_or_chunk_id_resolves
from evals import run_evals

_LOGGER = logging.getLogger("clinical_copilot")


class _NoTools:
    def read(self, *a, **k):
        raise AssertionError("no FHIR call expected")

    def search(self, *a, **k):
        raise AssertionError("no FHIR call expected")


class _FakeLLM:
    def __init__(self, text: str, stop_reason: str, block_types: list[str], out_tokens: int = 4096) -> None:
        self._resp = LLMTurnResponse(
            text=text, tool_calls=[], input_tokens=4000, output_tokens=out_tokens, raw_for_trace=[],
            stop_reason=stop_reason, content_block_types=block_types,
        )

    def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
        return self._resp

    def append_assistant_turn(self, messages, response) -> None: ...
    def append_tool_results(self, messages, results) -> None: ...
    def check_ready(self) -> bool:
        return True


def _field(value: str) -> ExtractedField:
    return ExtractedField(value=value, confidence="high", citation=DocumentCitation(page_number=1, quote_or_value=value))


def _extraction() -> ExtractionResult:
    fact = LabResultFact(
        test_name=_field("Potassium"), value=_field("6.8"), unit=_field("mmol/L"),
        reference_range=_field("3.5-5.1"), collection_date=_field("2026-09-24"), abnormal_flag=_field("high"),
    )
    return ExtractionResult(
        patient_id="p1", doc_id="doc1.png", doc_type="lab_pdf", ocr_avg_confidence=90.0, uploaded_on="2026-09-25",
        extraction=LabPdfExtraction(results=[fact]),
    )


def _state(**overrides) -> dict:
    state = {
        "correlation_id": "trunc-test", "resident_username": None, "patient_id": "p1", "user_message": "q",
        "messages": [], "workers_invoked": ["chart_data_worker", "intake_extractor", "evidence_retriever"],
        "routing_rationale": "t", "chart_facts": None, "uploaded_document_ref": None, "extracted_facts": [],
        "retrieved_evidence": [], "citations": [], "node_metrics": {}, "partial_failures": [],
        "draft_response": None, "needs_review_flags": [], "verification_outcome": None, "final_response": None,
    }
    state.update(overrides)
    return state


def _run_synthesis(llm: _FakeLLM, state: dict) -> tuple[dict, list[dict]]:
    captured: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _H()
    _LOGGER.addHandler(handler)
    try:
        out = synthesize_node(
            state, llm=llm, model="test", fhir=_NoTools(),
            trace=TurnTrace(client=None, span=None, correlation_id="trunc-test"),
        )
    finally:
        _LOGGER.removeHandler(handler)
    return out, [json.loads(line) for line in captured if '"event": "llm_call"' in line]


def _sources() -> dict:
    chunk = GuidelineChunk(source_id="hyperkalemia_management", chunk_id="h:1", text="Give calcium.", page_or_section="Acute")
    return dict(
        chart_facts=SimpleNamespace(draft_text="Chart summary: no allergies recorded.", turn_records=[]),
        extracted_facts=[_extraction()],
        retrieved_evidence=[RetrievedEvidence(chunk=chunk, rerank_score=0.9)],
    )


def test_empty_synthesis_at_the_cap_still_returns_the_gathered_sources_and_a_disclosure() -> None:
    out, llm_calls = _run_synthesis(_FakeLLM("", "max_tokens", ["thinking"]), _state(**_sources()))
    text = out["final_response"]
    assert text.strip(), "answer must never be empty"
    assert "entire output allowance" in text, text
    assert "Chart summary: no allergies recorded." in text, text
    assert "Value (result 1): 6.8" in text and "pending review" in text, text
    assert "hyperkalemia management (Acute)" in text, text
    assert "Give calcium." not in text, "guideline passage text must not be restated"
    call = llm_calls[-1]
    assert call["stop_reason"] == "max_tokens" and call["content_block_types"] == ["thinking"], call


def test_empty_synthesis_with_a_normal_stop_is_labelled_differently() -> None:
    out, _ = _run_synthesis(_FakeLLM("  ", "end_turn", []), _state(**_sources()))
    assert "no answer text" in out["final_response"] and "entire output allowance" not in out["final_response"]


def test_fallback_with_nothing_gathered_is_still_a_disclosure_not_an_empty_string() -> None:
    assert _fallback_answer(None, [], [], "max_tokens") == _EMPTY_TRUNCATION_NOTE
    assert "no answer text" in _fallback_answer(None, [], [], "end_turn")
    blank_chart = SimpleNamespace(draft_text="   ", turn_records=[])
    assert _fallback_answer(blank_chart, [], [], "max_tokens") == _EMPTY_TRUNCATION_NOTE


def test_partial_text_at_the_cap_is_kept_with_the_truncation_note() -> None:
    out, _ = _run_synthesis(_FakeLLM("Potassium is high.", "max_tokens", ["text"]), _state(**_sources()))
    assert out["final_response"].startswith("Potassium is high.") and "cut off before finishing" in out["final_response"]
    assert "entire output allowance" not in out["final_response"]


def test_disclose_if_truncated_handles_empty_and_normal_text() -> None:
    assert disclose_if_truncated("", "max_tokens") == _EMPTY_TRUNCATION_NOTE
    assert disclose_if_truncated("abc", "max_tokens") == "abc" + _TRUNCATION_NOTE
    assert disclose_if_truncated("abc", "end_turn") == "abc"
    assert disclose_if_truncated("", "end_turn") == ""


def test_synthesis_prompt_leads_with_a_brief_summary() -> None:
    assert "Lead with a one- to two-sentence summary" in _SYNTHESIS_SYSTEM_PROMPT
    assert "well under 300 words" in _SYNTHESIS_SYSTEM_PROMPT
    assert "state only what the gathered material" in _SYNTHESIS_SYSTEM_PROMPT.lower()  # hard rules kept


def test_field_or_chunk_id_check_accepts_either_real_id_and_rejects_fakes() -> None:
    extraction = _extraction()
    paths = {f.field_name for f in _flatten_extraction_facts(extraction)}
    quote = "Potassium result 4.4 mEq/L on printed report (ref: abc)"
    doc = "doc1.png"

    def cite(fid):
        return [{"source_type": "uploaded_document", "source_id": doc, "field_or_chunk_id": fid}]

    assert _check_field_or_chunk_id_resolves(cite(quote[:40]), doc, quote, paths)[0]  # same-turn snippet
    assert _check_field_or_chunk_id_resolves(cite("results[0].value"), doc, quote, paths)[0]  # read-back path
    assert not _check_field_or_chunk_id_resolves(cite("placeholder"), doc, quote, paths)[0]
    assert not _check_field_or_chunk_id_resolves(cite("results[9].value"), doc, quote, paths)[0]  # not a real field
    assert not _check_field_or_chunk_id_resolves(cite(None), doc, quote, paths)[0]
    assert not _check_field_or_chunk_id_resolves(cite(quote[:40]), "other.png", quote, paths)[0]  # wrong document
    assert _check_field_or_chunk_id_resolves(cite("bad") + cite("results[0].unit"), doc, quote, paths)[0]  # any one real id


def test_mocked_golden_set_runs_are_labelled_timing_mock() -> None:
    class _Agent:
        def run_turn(self, *a, **k):
            raise AssertionError("not reached")

    case = EvalCase(name="skipped", category="boundary", guards_against="g", patient_id=None, message="", skip_reason="x")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_evals.run_golden_set([case], concurrency=1, clients=(SimpleNamespace(), SimpleNamespace(), _Agent()))
        parallel.log_job_timing("graph_cases", 1.5, cases=3)
    lines = buf.getvalue().splitlines()
    assert any(ln.startswith("[timing:mock] job=golden_set") for ln in lines), lines
    assert any(ln.startswith("[parallel:mock]") for ln in lines), lines
    assert any(ln.startswith("[timing] job=graph_cases wall_clock_s=1.5") for ln in lines), lines  # real ones unchanged
    assert not any(ln.startswith("[timing] job=golden_set") for ln in lines), lines


_TESTS: list[Callable[[], None]] = [
    test_empty_synthesis_at_the_cap_still_returns_the_gathered_sources_and_a_disclosure,
    test_empty_synthesis_with_a_normal_stop_is_labelled_differently,
    test_fallback_with_nothing_gathered_is_still_a_disclosure_not_an_empty_string,
    test_partial_text_at_the_cap_is_kept_with_the_truncation_note,
    test_disclose_if_truncated_handles_empty_and_normal_text,
    test_synthesis_prompt_leads_with_a_brief_summary,
    test_field_or_chunk_id_check_accepts_either_real_id_and_rejects_fakes,
    test_mocked_golden_set_runs_are_labelled_timing_mock,
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
