"""Offline tests (no network, no FHIR, no LLM) for the security-hardening pass:

- log redaction: identifiers hashed, free text / clinical vocabulary kept out of journald, access-log query
  strings stripped, feedback.jsonl hashed;
- the rewritten no_phi_in_logs grader catches injected PHI (and passes properly redacted logs);
- /docs, /redoc and /openapi.json are off unless ENABLE_API_DOCS is set;
- the 5-page PDF cap and its user-facing message;
- provider errors (rejected key, rate limit, outage) become a friendly message with a correlation id, not a 500;
- CI: eval_graph_cases blocks merges.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import anthropic
import httpx
import yaml

from app import log_redaction
from app.config import get_settings
from app.graph import build as graph_build
from app.ingestion import ocr, pipeline
from app.llm_client import LLMTurnResponse
from app.log_redaction import AccessLogQueryStripFilter, hash_identifier, redact_fields
from app.observability import TurnObserver, TurnTrace, log_event
from evals import gate
from evals.cases import _assert_routing_decision_logged

PATIENT_UUID = "98c4b82b-b07e-11f1-8334-022958ad0af8"
CORRELATION_ID = "39bf87e5-466a-4c44-8b8a-de30eb25fc47"
RESIDENT = "dr.realname"

_LOGGER = logging.getLogger("clinical_copilot")


@contextlib.contextmanager
def _captured_lines():
    lines: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _H()
    _LOGGER.addHandler(handler)
    prior = _LOGGER.level
    _LOGGER.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        _LOGGER.removeHandler(handler)
        _LOGGER.setLevel(prior)


@contextlib.contextmanager
def _pinned_salt(salt: str = "test-salt"):
    log_redaction.reset_salt_for_tests(salt)
    try:
        yield
    finally:
        log_redaction.reset_salt_for_tests(None)


# --- log redaction ----------------------------------------------------------------


def test_identifier_hash_is_stable_salted_non_identifying_and_correlatable() -> None:
    with _pinned_salt("salt-a"):
        a1, a2, other = hash_identifier(PATIENT_UUID), hash_identifier(PATIENT_UUID), hash_identifier("another-patient")
    with _pinned_salt("salt-b"):
        b = hash_identifier(PATIENT_UUID)
    assert a1 == a2 and a1 != other, "same patient -> same token; different patients differ (correlatable)"
    assert a1 != b, "the token depends on the salt, so it cannot be reversed or matched without it"
    assert a1.startswith("h_") and len(a1) == 14 and PATIENT_UUID not in a1
    assert gate._UUID_PATTERN.search(a1) is None, "a token must never look like a UUID"
    assert hash_identifier(None) is None


def _emit_every_phi_bearing_event() -> list[str]:
    """Drives the real TurnObserver/TurnTrace with PHI in every place the app used to log it."""
    observer = TurnObserver(get_settings())
    with _captured_lines() as lines:
        trace = observer.start_turn(CORRELATION_ID, "Bob has a sulfa allergy and sepsis", PATIENT_UUID, RESIDENT)
        handle = trace.start_tool_call(
            tool_name="check_allergy_conflict", input_payload={"patient_id": PATIENT_UUID, "medication_name": "Bactrim DS"}
        )
        trace.finish_tool_call(handle, output_payload={"conflict_found": True}, failed=False, retry_count=1)
        trace.log_routing_decision(
            workers=["chart_data_worker"], rationale="Bob Testpatient asks about Bactrim given his sulfa allergy and sepsis"
        )
        trace.log_verification(
            passed_source_attribution=False, passed_domain_constraint=True,
            flagged_claims=["azithromycin", "pneumonia"],
            enforced_warnings=["bactrim", "allergy_check_not_assessed: codeine", "empty_chart", "worker_partial_failure"],
        )
        trace.log_worker_crash(worker="chart_data_worker", error=f"KeyError('Alice Testpatient {PATIENT_UUID}')")
        log_event("document_preview_denied", correlation_id=CORRELATION_ID, resident_username=RESIDENT, reason="forbidden")
        log_event("some_future_event", correlation_id=CORRELATION_ID, note=f"lookup for {PATIENT_UUID} failed")
    return lines


def test_no_raw_identifier_free_text_or_clinical_term_reaches_the_log() -> None:
    with _pinned_salt():
        lines = _emit_every_phi_bearing_event()
        joined = "\n".join(lines)
        for raw in (PATIENT_UUID, RESIDENT, "Bactrim", "sulfa", "sepsis", "azithromycin", "pneumonia", "codeine",
                    "Testpatient", "Bob", "KeyError('"):
            assert raw not in joined, f"raw {raw!r} reached the log:\n{joined}"
        assert CORRELATION_ID in joined, "correlation_id is the one UUID that stays"
        assert gate.find_phi_in_logs(lines) == [], gate.find_phi_in_logs(lines)


def test_identifiers_are_hashed_consistently_so_a_patient_can_still_be_followed() -> None:
    with _pinned_salt():
        events = [json.loads(line) for line in _emit_every_phi_bearing_event()]
        by_event = {e["event"]: e for e in events}
        expected_patient, expected_resident = hash_identifier(PATIENT_UUID), hash_identifier(RESIDENT)
        assert by_event["turn_started"]["patient_id"] == expected_patient
        assert by_event["turn_started"]["resident_username"] == expected_resident
        assert by_event["document_preview_denied"]["resident_username"] == expected_resident
        assert "h_" in by_event["some_future_event"]["note"] and PATIENT_UUID not in by_event["some_future_event"]["note"]


def test_tool_call_keeps_the_tool_arg_names_latency_and_outcome_but_no_input_values() -> None:
    with _pinned_salt():
        event = next(json.loads(l) for l in _emit_every_phi_bearing_event() if '"event": "tool_call"' in l)
    assert event["tool"] == "check_allergy_conflict" and event["arg_names"] == ["medication_name", "patient_id"]
    assert "input" not in event and event["failed"] is False and event["retry_count"] == 1 and "latency_s" in event


def test_routing_flagged_terms_warnings_and_crashes_are_reduced_to_counts_kinds_and_types() -> None:
    with _pinned_salt():
        events = {json.loads(l)["event"]: json.loads(l) for l in _emit_every_phi_bearing_event()}
    routing = events["routing_decision"]
    assert routing["workers"] == ["chart_data_worker"] and "rationale" not in routing and routing["rationale_length"] > 20
    verification = events["verification"]
    assert verification["flagged_claim_count"] == 2 and "flagged_claims" not in verification
    assert verification["enforced_warnings"] == [
        "allergy_conflict_enforced", "allergy_check_not_assessed", "empty_chart", "worker_partial_failure"
    ], verification["enforced_warnings"]
    assert events["worker_crashed"]["error_type"] == "KeyError" and "error" not in events["worker_crashed"]


def test_redact_fields_leaves_metrics_and_flags_untouched() -> None:
    fields = {"correlation_id": CORRELATION_ID, "latency_s": 0.5, "input_tokens": 10, "stop_reason": "end_turn",
              "content_block_types": ["text"], "fallback_answer_used": False, "before_filter": {"total": 3}}
    assert redact_fields("llm_call", fields) == fields


def test_access_log_lines_lose_their_query_string_and_any_path_uuid() -> None:
    def rendered(target: str) -> str:
        record = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
                                   ("10.0.0.1:5000", "GET", target, "1.1", 200), None)
        with _pinned_salt():
            assert AccessLogQueryStripFilter().filter(record)
        return record.getMessage()

    line = rendered(f"/ui?patient={PATIENT_UUID}")
    assert line == '10.0.0.1:5000 - "GET /ui HTTP/1.1" 200', line
    assert PATIENT_UUID not in rendered(f"/patient?patient_id={PATIENT_UUID}&x=1")
    assert PATIENT_UUID not in rendered(f"/documents/{PATIENT_UUID}/thing")
    assert rendered("/health") == '10.0.0.1:5000 - "GET /health HTTP/1.1" 200'


def test_the_app_installs_the_access_log_filter() -> None:
    import app.main  # noqa: F401 -- importing installs the filter

    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogQueryStripFilter) for f in filters) == 1
    log_redaction.install_access_log_filter()
    assert sum(isinstance(f, AccessLogQueryStripFilter) for f in logging.getLogger("uvicorn.access").filters) == 1


def test_the_live_routing_eval_helper_expects_the_redacted_routing_log_line() -> None:
    """Regression: two golden-set cases and one graph case read the routing_decision line back out of the log and
    used to demand the rationale text in it; with the text (correctly) gone they failed twice in CI."""
    rationale = "Direct chart lookup for Alice Testpatient's active problems, medications and allergies."
    result = SimpleNamespace(workers_invoked=["chart_data_worker"], routing_rationale=rationale)
    with _pinned_salt(), _captured_lines() as lines:
        trace = TurnObserver(get_settings()).start_turn(CORRELATION_ID, "q", PATIENT_UUID, RESIDENT)
        trace.log_routing_decision(workers=["chart_data_worker"], rationale=rationale)
    assert _assert_routing_decision_logged(lines, result) is None, _assert_routing_decision_logged(lines, result)
    old_style = [json.dumps({"event": "routing_decision", "workers": ["chart_data_worker"], "rationale": rationale})]
    assert "reached the log" in _assert_routing_decision_logged(old_style, result), "a leaked rationale must fail the check"
    wrong_length = [json.dumps({"event": "routing_decision", "workers": ["chart_data_worker"], "rationale_length": 3})]
    assert "rationale_length" in _assert_routing_decision_logged(wrong_length, result)
    wrong_workers = [json.dumps({"event": "routing_decision", "workers": ["evidence_retriever"], "rationale_length": len(rationale)})]
    assert "don't match" in _assert_routing_decision_logged(wrong_workers, result)


# --- the rewritten no_phi_in_logs grader --------------------------------------------

_CLEAN = [
    json.dumps({"event": "turn_started", "correlation_id": CORRELATION_ID, "patient_id": "h_1a2b3c4d5e6f",
                "resident_username": "h_aabbccddeeff", "message_length": 42}),
    json.dumps({"event": "tool_call", "correlation_id": CORRELATION_ID, "tool": "check_allergy_conflict",
                "arg_names": ["medication_name", "patient_id"], "latency_s": 0.4, "failed": False, "retry_count": 0}),
    json.dumps({"event": "verification", "correlation_id": CORRELATION_ID, "verification_pass": True,
                "flagged_claim_count": 0, "enforced_warnings": ["empty_chart"]}),
]


def _fails(line: str, expect: str) -> None:
    findings = gate.find_phi_in_logs([*_CLEAN, line])
    assert any(f.startswith(expect) for f in findings), (line, findings)
    assert gate.grade_no_phi_in_logs([*_CLEAN, line]) is False


def test_the_grader_passes_properly_redacted_logs_and_ignores_the_correlation_id_uuid() -> None:
    assert gate.grade_no_phi_in_logs(_CLEAN) is True
    assert gate.grade_no_phi_in_logs([]) is None


def test_the_grader_fails_on_a_raw_uuid_anywhere_but_correlation_id() -> None:
    _fails(json.dumps({"event": "turn_started", "correlation_id": CORRELATION_ID, "patient_id": PATIENT_UUID}), "raw_uuid")
    _fails(f"lookup failed for {PATIENT_UUID}", "raw_uuid")
    _fails(json.dumps({"event": "x", "correlation_id": CORRELATION_ID, "other": CORRELATION_ID}), "raw_uuid")


def test_the_grader_fails_on_fixture_names_dobs_ssns_streets_and_phones() -> None:
    for text in ("Alice Testpatient", "Bob Testpatient", "Carol Emptychart", "Dan Otherprovider", "Testpatient", "1972-03-14",
                 "03/14/1972", "3/14/1972", "March 14, 1972", "1958-11-02", "123-45-6789", "100 Main St", "555-0101"):
        line = json.dumps({"event": "leak", "correlation_id": CORRELATION_ID, "text": f"patient {text} arrived"})
        assert gate.find_phi_in_logs([line]), f"{text!r} was not caught"
        assert gate.grade_no_phi_in_logs([*_CLEAN, line]) is False, text
    _fails(json.dumps({"event": "leak", "text": "ssn 987-65-4321"}), "ssn_shaped_number")  # any SSN-shaped number


def test_the_grader_fails_on_resident_usernames_including_a_configured_one() -> None:
    for name in ("test_clinician", "graph-cases-integration-test"):
        _fails(json.dumps({"event": "leak", "user": name}), "resident_username")
    saved, gate._phi_terms_cache = os.environ.get("OPENEMR_OAUTH_USERNAME"), None
    os.environ["OPENEMR_OAUTH_USERNAME"] = "real.resident"
    try:
        _fails(json.dumps({"event": "leak", "user": "real.resident"}), "resident_username")
    finally:
        if saved is None:
            os.environ.pop("OPENEMR_OAUTH_USERNAME", None)
        else:
            os.environ["OPENEMR_OAUTH_USERNAME"] = saved
        gate._phi_terms_cache = None


def test_the_grader_fails_on_drug_allergy_and_condition_vocabulary_from_the_reference_tables() -> None:
    for term in ("Bactrim", "Zosyn", "sulfamethoxazole-trimethoprim", "ceftriaxone", "cephalosporins", "Z-Pak",    # class table
                 "warfarin", "metformin", "lisinopril",                                                          # verification meds
                 "sulfa", "penicillin", "latex", "codeine",                                                      # allergy terms
                 "sepsis", "pneumonia", "COPD", "atrial fibrillation"):                                          # conditions
        _fails(json.dumps({"event": "leak", "correlation_id": CORRELATION_ID, "tool_input": {"medication_name": term}}),
               "clinical_vocabulary")


def test_the_grader_catches_the_exact_pre_redaction_log_lines_the_app_used_to_write() -> None:
    old = [
        json.dumps({"event": "tool_call", "correlation_id": CORRELATION_ID, "tool": "check_allergy_conflict",
                    "input": {"patient_id": PATIENT_UUID, "medication_name": "Bactrim"}, "latency_s": 0.1}),
        json.dumps({"event": "routing_decision", "correlation_id": CORRELATION_ID, "workers": ["chart_data_worker"],
                    "rationale": "asks about sepsis"}),
        json.dumps({"event": "verification", "correlation_id": CORRELATION_ID, "flagged_claims": ["azithromycin"]}),
    ]
    for line in old:
        assert gate.grade_no_phi_in_logs([*_CLEAN, line]) is False, line
    assert gate.grade_no_phi_in_logs([json.dumps({"event": "turn_started", "correlation_id": CORRELATION_ID, "patient_id": PATIENT_UUID})]) is False


def test_a_grader_failure_never_repeats_what_it_found() -> None:
    findings = gate.find_phi_in_logs([json.dumps({"event": "leak", "a": PATIENT_UUID, "b": "Bactrim", "c": "Alice Testpatient"})])
    text = " ".join(findings)
    assert findings and PATIENT_UUID not in text and "Bactrim" not in text and "Alice" not in text, findings


def test_the_grader_word_boundaries_avoid_false_alarms_on_ordinary_log_words() -> None:
    ok = json.dumps({"event": "llm_call", "correlation_id": CORRELATION_ID, "stop_reason": "end_turn", "tool": "get_patient_snapshot",
                     "arg_names": ["patient_id"], "doc_type": "lab_pdf", "worker": "evidence_retriever", "note": "asserted sulfates"})
    assert gate.grade_no_phi_in_logs([ok]) is True, gate.find_phi_in_logs([ok])


# --- API docs off in production -----------------------------------------------------


def test_api_docs_and_schema_are_not_served_by_default() -> None:
    from starlette.testclient import TestClient

    from app import main

    assert main.app.docs_url is None and main.app.redoc_url is None and main.app.openapi_url is None
    client = TestClient(main.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.get("/health").status_code == 200, "the app itself still serves"


def test_api_docs_can_be_enabled_for_local_development_only_by_env() -> None:
    code = "import app.main as m; print(m.app.docs_url, m.app.redoc_url, m.app.openapi_url)"
    env = {**os.environ, "ENABLE_API_DOCS": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180)
    assert out.stdout.strip().endswith("/docs /redoc /openapi.json"), (out.stdout[-200:], out.stderr[-300:])


# --- PDF page cap ---------------------------------------------------------------------


def _fake_pdf_renderer(total_pages: int):
    calls: list[dict] = []

    def convert_from_bytes(data, **kwargs):
        calls.append(kwargs)
        rendered = min(total_pages, kwargs.get("last_page", total_pages))
        return [ocr.Image.new("RGB", (10, 10), "white") for _ in range(rendered)]

    return convert_from_bytes, calls


@contextlib.contextmanager
def _patched_pdf2image(total_pages: int):
    import pdf2image

    fake, calls = _fake_pdf_renderer(total_pages)
    original = pdf2image.convert_from_bytes
    pdf2image.convert_from_bytes = fake
    original_ocr = ocr._ocr_page
    ocr._ocr_page = lambda image, page_number: ocr.OcrPage(page_number=page_number, words=[])
    try:
        yield calls
    finally:
        pdf2image.convert_from_bytes = original
        ocr._ocr_page = original_ocr


def test_a_pdf_within_the_page_cap_is_processed_and_renders_at_most_one_page_beyond_it() -> None:
    assert ocr.MAX_DOCUMENT_PAGES == 5
    for pages in (1, 5):
        with _patched_pdf2image(pages) as calls:
            result = ocr.run_ocr(b"%PDF-fake", "application/pdf")
        assert len(result.pages) == pages and calls == [{"last_page": 6}], (pages, calls)


def test_a_pdf_over_the_cap_is_refused_with_a_clear_message_without_rendering_it_all() -> None:
    with _patched_pdf2image(400) as calls:
        try:
            ocr.run_ocr(b"%PDF-fake", "application/pdf")
        except ocr.PageLimitError as exc:
            message = str(exc)
        else:
            raise AssertionError("a 400-page PDF must be refused")
    assert calls == [{"last_page": 6}], "at most 6 pages are ever rasterized, however long the PDF is"
    assert "more than 5 pages" in message and "up to 5" in message, message


def test_a_stored_document_preview_is_not_subject_to_the_ingestion_cap() -> None:
    with _patched_pdf2image(8) as calls:
        images = ocr._page_images(b"%PDF-fake", "application/pdf")
    assert len(images) == 8 and calls == [{}], "previews of documents stored before the cap still render every page"


def test_the_pipeline_returns_a_too_many_pages_failure_and_stores_nothing() -> None:
    with _patched_pdf2image(9):
        result, failure, tokens_in, tokens_out = pipeline.attach_and_extract(
            "p1", b"%PDF-fake", "long.pdf", "application/pdf", "lab_pdf",
            fhir=SimpleNamespace(), llm=SimpleNamespace(), model="m",
            trace=TurnTrace(client=None, span=None, correlation_id="cap-test"),
        )
    assert result is None and (tokens_in, tokens_out) == (0, 0)
    assert failure.detail_code == "too_many_pages" and "more than 5 pages" in failure.reason, failure


def test_the_upload_endpoint_answers_400_with_the_page_limit_message() -> None:
    from starlette.testclient import TestClient

    from app import main
    from app.oauth_session import ResidentSession

    session_id = "cap-session"
    main._sessions._sessions[session_id] = ResidentSession(
        session_id=session_id, tokens=SimpleNamespace(get_token=lambda: "t"), fhir=SimpleNamespace(), resident_username="cap.resident"
    )
    try:
        with _patched_pdf2image(12):
            client = TestClient(main.app, cookies={"copilot_session": session_id})
            response = client.post("/documents", data={"patient_id": PATIENT_UUID, "doc_type": "lab_pdf"},
                                   files={"file": ("long.pdf", b"%PDF-fake", "application/pdf")})
    finally:
        main._sessions._sessions.pop(session_id, None)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["detail_code"] == "too_many_pages" and "more than 5 pages" in body["error"], body


def test_a_real_pdf_over_the_cap_is_refused_when_poppler_is_installed() -> None:
    if shutil.which("pdftoppm") is None:
        return  # the mocked tests above cover the logic; the CI runner has poppler and exercises this for real
    from PIL import Image

    def pdf(pages: int) -> bytes:
        buffer = io.BytesIO()
        first = Image.new("RGB", (200, 200), "white")
        first.save(buffer, format="PDF", save_all=True, append_images=[Image.new("RGB", (200, 200), "white") for _ in range(pages - 1)])
        return buffer.getvalue()

    original = ocr._ocr_page
    ocr._ocr_page = lambda image, page_number: ocr.OcrPage(page_number=page_number, words=[])
    try:
        assert len(ocr.run_ocr(pdf(5), "application/pdf").pages) == 5
        try:
            ocr.run_ocr(pdf(7), "application/pdf")
        except ocr.PageLimitError:
            pass
        else:
            raise AssertionError("a real 7-page PDF must be refused")
    finally:
        ocr._ocr_page = original


# --- provider errors ------------------------------------------------------------------


def _provider_error(kind: str) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "connection":
        return anthropic.APIConnectionError(request=request)
    status = {"auth": 401, "rate": 429, "server": 500}[kind]
    response = httpx.Response(status, request=request)
    cls = {"auth": anthropic.AuthenticationError, "rate": anthropic.RateLimitError, "server": anthropic.InternalServerError}[kind]
    return cls("secret-detail-that-must-not-be-logged", response=response, body=None)


class _FailingLLM:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create_message(self, **kwargs) -> LLMTurnResponse:
        raise self._exc

    def append_assistant_turn(self, messages, response) -> None: ...
    def append_tool_results(self, messages, results) -> None: ...
    def check_ready(self) -> bool:
        return False


@contextlib.contextmanager
def _llm_that_fails(exc: Exception):
    original = graph_build.build_llm_client
    graph_build.build_llm_client = lambda settings: _FailingLLM(exc)
    try:
        yield
    finally:
        graph_build.build_llm_client = original


def _run_turn_with_failing_provider(exc: Exception, history: list[dict] | None = None):
    with _llm_that_fails(exc), _captured_lines() as lines, _pinned_salt():
        result = graph_build.run_turn(
            settings=get_settings(), history=history or [], user_message="What changed?", patient_id=PATIENT_UUID,
            fhir=SimpleNamespace(), resident_username=RESIDENT, retriever=SimpleNamespace(retrieve=lambda q: []),
        )
    return result, lines


def test_a_provider_error_in_the_routing_step_becomes_a_friendly_message_with_a_correlation_id() -> None:
    for kind in ("auth", "rate", "server", "connection"):
        prior = [{"role": "user", "content": "earlier"}]
        result, _ = _run_turn_with_failing_provider(_provider_error(kind), history=prior)
        assert "temporarily unavailable" in result.response_text, (kind, result.response_text)
        assert result.correlation_id in result.response_text, "the correlation id must be quoted to the resident"
        assert result.updated_history == prior, "the failed turn must not be added to the conversation"
        notice = result.notices[0]
        assert notice["kind"] == "assistant_unavailable" and notice["severity"] == "warning", notice
        assert result.workers_invoked == [] and result.flagged_claims == [] and result.citations == []


def test_a_provider_error_logs_only_its_type_and_status_never_its_message() -> None:
    result, lines = _run_turn_with_failing_provider(_provider_error("auth"))
    events = [json.loads(l) for l in lines if l.startswith("{")]
    failure = next(e for e in events if e["event"] == "llm_provider_error")
    assert failure == {"event": "llm_provider_error", "correlation_id": result.correlation_id,
                       "error_type": "AuthenticationError", "status_code": 401}, failure
    joined = "\n".join(lines)
    assert "secret-detail-that-must-not-be-logged" not in joined and PATIENT_UUID not in joined
    assert gate.find_phi_in_logs(lines) == [], gate.find_phi_in_logs(lines)


def test_only_provider_errors_are_swallowed_other_bugs_still_surface() -> None:
    try:
        _run_turn_with_failing_provider(ValueError("a genuine bug"))
    except ValueError:
        return
    raise AssertionError("a non-provider exception must not be turned into a friendly message")


def test_chat_returns_200_with_the_friendly_message_not_a_500_when_the_provider_is_down() -> None:
    from starlette.testclient import TestClient

    from app import main
    from app.oauth_session import ResidentSession

    session_id = "provider-down-session"
    main._sessions._sessions[session_id] = ResidentSession(
        session_id=session_id, tokens=SimpleNamespace(get_token=lambda: "t"),
        fhir=SimpleNamespace(search=lambda *a, **k: {"entry": []}, read=lambda *a, **k: {}), resident_username=RESIDENT,
    )
    try:
        with _llm_that_fails(_provider_error("auth")):
            client = TestClient(main.app, cookies={"copilot_session": session_id}, raise_server_exceptions=False)
            response = client.post("/chat", json={"message": "What changed?", "patient_id": PATIENT_UUID})
    finally:
        main._sessions._sessions.pop(session_id, None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "temporarily unavailable" in body["response"] and body["correlation_id"] in body["response"]
    assert body["notices"][0]["kind"] == "assistant_unavailable"


def test_a_provider_outage_while_structuring_a_document_is_reported_as_unavailable() -> None:
    original = ocr.run_ocr
    pipeline.run_ocr = lambda b, ct: ocr.OcrResult(pages=[ocr.OcrPage(page_number=1, words=[])])
    try:
        result, failure, _, _ = pipeline.attach_and_extract(
            "p1", b"x", "f.png", "image/png", "lab_pdf", fhir=SimpleNamespace(), llm=_FailingLLM(_provider_error("rate")), model="m",
            trace=TurnTrace(client=None, span=None, correlation_id="c"),
        )
    finally:
        pipeline.run_ocr = original
    assert result is None and failure.detail_code == "structuring_failed" and "temporarily unavailable" in failure.reason, failure


# --- CI -------------------------------------------------------------------------------


def test_eval_graph_cases_and_the_golden_set_block_merges() -> None:
    ci = yaml.safe_load((Path(__file__).resolve().parents[2] / ".gitlab-ci.yml").read_text())
    for job in ("eval_graph_cases", "eval_golden_set", "eval_unit_tests"):
        assert "allow_failure" not in ci[job] or ci[job]["allow_failure"] is False, f"{job} must block, not allow failure"
    assert ci["eval_graph_cases"]["retry"] == 1
    assert ci["prune_upload_fixture"]["allow_failure"] is True, "the prune job is housekeeping and stays non-blocking"


_TESTS: list[Callable[[], None]] = [
    test_identifier_hash_is_stable_salted_non_identifying_and_correlatable,
    test_no_raw_identifier_free_text_or_clinical_term_reaches_the_log,
    test_identifiers_are_hashed_consistently_so_a_patient_can_still_be_followed,
    test_tool_call_keeps_the_tool_arg_names_latency_and_outcome_but_no_input_values,
    test_routing_flagged_terms_warnings_and_crashes_are_reduced_to_counts_kinds_and_types,
    test_redact_fields_leaves_metrics_and_flags_untouched,
    test_access_log_lines_lose_their_query_string_and_any_path_uuid,
    test_the_app_installs_the_access_log_filter,
    test_the_live_routing_eval_helper_expects_the_redacted_routing_log_line,
    test_the_grader_passes_properly_redacted_logs_and_ignores_the_correlation_id_uuid,
    test_the_grader_fails_on_a_raw_uuid_anywhere_but_correlation_id,
    test_the_grader_fails_on_fixture_names_dobs_ssns_streets_and_phones,
    test_the_grader_fails_on_resident_usernames_including_a_configured_one,
    test_the_grader_fails_on_drug_allergy_and_condition_vocabulary_from_the_reference_tables,
    test_the_grader_catches_the_exact_pre_redaction_log_lines_the_app_used_to_write,
    test_a_grader_failure_never_repeats_what_it_found,
    test_the_grader_word_boundaries_avoid_false_alarms_on_ordinary_log_words,
    test_api_docs_and_schema_are_not_served_by_default,
    test_api_docs_can_be_enabled_for_local_development_only_by_env,
    test_a_pdf_within_the_page_cap_is_processed_and_renders_at_most_one_page_beyond_it,
    test_a_pdf_over_the_cap_is_refused_with_a_clear_message_without_rendering_it_all,
    test_a_stored_document_preview_is_not_subject_to_the_ingestion_cap,
    test_the_pipeline_returns_a_too_many_pages_failure_and_stores_nothing,
    test_the_upload_endpoint_answers_400_with_the_page_limit_message,
    test_a_real_pdf_over_the_cap_is_refused_when_poppler_is_installed,
    test_a_provider_error_in_the_routing_step_becomes_a_friendly_message_with_a_correlation_id,
    test_a_provider_error_logs_only_its_type_and_status_never_its_message,
    test_only_provider_errors_are_swallowed_other_bugs_still_surface,
    test_chat_returns_200_with_the_friendly_message_not_a_500_when_the_provider_is_down,
    test_a_provider_outage_while_structuring_a_document_is_reported_as_unavailable,
    test_eval_graph_cases_and_the_golden_set_block_merges,
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
