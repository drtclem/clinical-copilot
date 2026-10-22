"""Offline tests for GET /documents/preview's authorization and denial
handling (app/main.py document_preview, app/ingestion/storage.py
get_source_document_bytes). Drives the real FastAPI app in-process through
starlette's TestClient with a duck-typed fake FHIR client -- no network, no
OpenEMR, no LLM.

Runnable with pytest (zero-arg test_* functions) and registered in
evals/unit_tests.py's TESTS via TESTS below.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Callable

from fastapi.testclient import TestClient
from PIL import Image

from app.fhir_client import FhirRequestError
from app.main import _sessions, app
from app.oauth_session import ResidentSession

PATIENT_A = "11111111-1111-4111-8111-111111111111"
PATIENT_B = "22222222-2222-4222-8222-222222222222"
PID = {PATIENT_A: 1, PATIENT_B: 2}  # FHIR uuid -> OpenEMR's internal numeric pid
DOC = "abc123def456.png"
RESIDENT = "dr.offline.test"
PHI_MARKERS = ["Testpatient", "03/14/1972", "1972-03-14", "Alice"]
SOURCE = "Categories/Source Document"
EXTRACTED = "Categories/AI-Extracted Pending Review"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def _row(doc_id: int, *, pid: int | None, name: str = DOC, deleted: bool = False, category: str = SOURCE) -> dict:
    """One row of OpenEMR's `documents` table (+ its category link)."""
    return {"id": doc_id, "pid": pid, "name": name, "deleted": deleted, "category": category}


_FORBIDDEN = FhirRequestError("forbidden", detail_code="http_error", http_status=403)


class _FakeFhir:
    """A resident-scoped FhirClient for a NON-ADMIN user (a Clinician).

    Models OpenEMR, not this app: the Standard API document listing applies
    the real DocumentService::getAllAtPath filter (category AND foreign_id =
    pid AND deleted = 0), the binary route serves whatever id it is handed,
    and the FHIR DocumentReference search / Binary routes answer 403 because
    OpenEMR gates them behind admin/super and admin/users. So a code path that
    reaches for either FHIR document route fails these tests, and a path that
    trusts a request-supplied id (rather than one read from the listing)
    serves the wrong patient's bytes."""

    def __init__(self, rows: list[dict], *, patient_read_error: FhirRequestError | None = None,
                 standard_patient_error: FhirRequestError | None = None,
                 binary_error: FhirRequestError | None = None) -> None:
        self._rows = rows
        self._patient_read_error = patient_read_error
        self._standard_patient_error = standard_patient_error
        self._binary_error = binary_error
        self.binary_calls: list[str] = []

    # FHIR Patient read -- user/Patient.read, which Clinicians have.
    def read(self, resource_type: str, resource_id: str) -> dict:
        if self._patient_read_error is not None:
            raise self._patient_read_error
        return {"resourceType": resource_type, "id": resource_id}

    # FHIR document routes -- admin only in OpenEMR.
    def search(self, resource_type: str, params: dict) -> dict:
        raise _FORBIDDEN

    def get_fhir_binary(self, resource_type: str, resource_id: str) -> tuple[bytes, str]:
        raise _FORBIDDEN

    # Standard API -- patients/docs, which Clinicians have.
    def get_standard_api(self, path: str) -> dict | list:
        if self._standard_patient_error is not None:
            raise self._standard_patient_error
        if path.startswith("/patient/") and "/document" not in path:
            uuid_ = path.rsplit("/", 1)[-1]
            if uuid_ not in PID:
                raise FhirRequestError("not found", detail_code="not_found")
            return {"pid": PID[uuid_]}
        pid_part, _, query = path.removeprefix("/patient/").partition("/document?path=")
        pid = int(pid_part)
        listed = [
            {"id": r["id"], "filename": r["name"], "mimetype": "image/png", "hash": "h", "docdate": "2026-09-25"}
            for r in self._rows
            if r["category"] == query and r["pid"] == pid and not r["deleted"]
        ]
        if not listed:
            raise FhirRequestError("not found", detail_code="not_found")  # OpenEMR's empty category answers 404
        return listed

    def get_standard_api_binary(self, path: str) -> bytes:
        self.binary_calls.append(path)
        if self._binary_error is not None:
            raise self._binary_error
        return _png()


class _LiveTokens:
    """SessionStore.get() probes session.tokens.get_token() to drop dead sessions."""

    def get_token(self) -> str:
        return "offline-token"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _call(fhir: _FakeFhir | None, patient_id: str, doc_id: str = DOC, *, logged_in: bool = True):
    """Returns (response, denial_log_events)."""
    session_id = f"preview-authz-{uuid.uuid4()}"
    capture = _Capture()
    logger = logging.getLogger("clinical_copilot")
    logger.addHandler(capture)
    if logged_in:
        _sessions._sessions[session_id] = ResidentSession(
            session_id=session_id, tokens=_LiveTokens(), fhir=fhir, resident_username=RESIDENT  # type: ignore[arg-type]
        )
    try:
        client = TestClient(app)
        if logged_in:
            client.cookies.set("copilot_session", session_id)
        response = client.get("/documents/preview", params={"patient_id": patient_id, "doc_id": doc_id, "page": 1})
    finally:
        logger.removeHandler(capture)
        _sessions._sessions.pop(session_id, None)
    events = [json.loads(line) for line in capture.lines if '"document_preview_denied"' in line]
    return response, events


def test_non_admin_resident_gets_the_page_image() -> None:
    # The FHIR DocumentReference/Binary routes 403 for this user (see _FakeFhir); the preview must not need them.
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A])])
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"\x89PNG")
    assert fhir.binary_calls == ["/patient/1/document/10"]
    assert events == []


def test_fhir_document_routes_are_never_used() -> None:
    # Guard against a regression to the admin-gated FHIR routes: fail loudly if either is even called.
    class _Tripwire(_FakeFhir):
        def search(self, resource_type: str, params: dict) -> dict:
            raise AssertionError("the preview called the admin-gated FHIR DocumentReference search")

        def get_fhir_binary(self, resource_type: str, resource_id: str) -> tuple[bytes, str]:
            raise AssertionError("the preview called the admin-gated FHIR Binary route")

    response, _ = _call(_Tripwire([_row(10, pid=PID[PATIENT_A])]), PATIENT_A)
    assert response.status_code == 200, response.text


def test_deleted_document_is_404_and_bytes_are_never_fetched() -> None:
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A], deleted=True)])
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 404
    assert fhir.binary_calls == []
    assert [e["reason"] for e in events] == ["not_found"]


def test_another_patients_document_is_404() -> None:
    # The document exists, but belongs to patient B. Asking for it under patient A must never serve it --
    # not by listing, and not by the request-supplied name reaching the binary route.
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_B])])
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 404 and fhir.binary_calls == []
    assert [e["reason"] for e in events] == ["not_found"]
    # ...and it is served to its actual owner.
    owner, _ = _call(_FakeFhir([_row(10, pid=PID[PATIENT_B])]), PATIENT_B)
    assert owner.status_code == 200


def test_document_with_no_owning_patient_is_refused_not_trusted() -> None:
    fhir = _FakeFhir([_row(10, pid=None)])
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 404 and fhir.binary_calls == []
    assert [e["reason"] for e in events] == ["not_found"]


def test_only_the_source_document_category_is_served() -> None:
    # The AI-extracted JSON shares the patient and the name; it must not be reachable as a "preview".
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A], category=EXTRACTED)])
    response, _ = _call(fhir, PATIENT_A)
    assert response.status_code == 404 and fhir.binary_calls == []


def test_a_duplicate_name_resolves_to_the_newest_live_row() -> None:
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A]), _row(12, pid=PID[PATIENT_A]), _row(11, pid=PID[PATIENT_A], deleted=True)])
    response, _ = _call(fhir, PATIENT_A)
    assert response.status_code == 200 and fhir.binary_calls == ["/patient/1/document/12"]


def test_the_requested_document_is_served_not_just_any_listed_one() -> None:
    # Two live Source Documents for the same patient: the one asked for by name is the one fetched, and a name
    # that matches nothing is a 404 rather than "the first document on file".
    rows = [_row(10, pid=PID[PATIENT_A], name="other-scan.png"), _row(11, pid=PID[PATIENT_A])]
    fhir = _FakeFhir(rows)
    response, _ = _call(fhir, PATIENT_A)
    assert response.status_code == 200 and fhir.binary_calls == ["/patient/1/document/11"]
    unmatched = _FakeFhir(rows)
    response, events = _call(unmatched, PATIENT_A, doc_id="no-such-scan.png")
    assert response.status_code == 404 and unmatched.binary_calls == []
    assert [e["reason"] for e in events] == ["not_found"]


def test_patient_the_resident_cannot_read_is_404_for_denied_and_missing_alike() -> None:
    rows = [_row(10, pid=PID[PATIENT_A])]
    forbidden = _FakeFhir(rows, patient_read_error=FhirRequestError("no", detail_code="http_error", http_status=403))
    missing = _FakeFhir(rows, patient_read_error=FhirRequestError("no", detail_code="not_found"))
    r1, e1 = _call(forbidden, PATIENT_A)
    r2, e2 = _call(missing, PATIENT_A)
    assert r1.status_code == r2.status_code == 404
    assert forbidden.binary_calls == missing.binary_calls == []
    assert [e["reason"] for e in e1] == ["not_authorized"]
    assert [e["reason"] for e in e2] == ["not_found"]


def test_a_standard_api_403_is_a_denial_not_a_502() -> None:
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A])], standard_patient_error=_FORBIDDEN)
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 404 and fhir.binary_calls == []
    assert [e["reason"] for e in events] == ["not_authorized"]


def test_all_denials_return_an_identical_404_body() -> None:
    a = PID[PATIENT_A]
    cases = [
        _call(_FakeFhir([]), PATIENT_A)[0],  # not found
        _call(_FakeFhir([_row(10, pid=a, deleted=True)]), PATIENT_A)[0],  # deleted
        _call(_FakeFhir([_row(10, pid=PID[PATIENT_B])]), PATIENT_A)[0],  # other patient's
        _call(_FakeFhir([_row(10, pid=None)]), PATIENT_A)[0],  # no owner
        _call(_FakeFhir([], patient_read_error=FhirRequestError("x", detail_code="http_error", http_status=401)), PATIENT_A)[0],
        _call(_FakeFhir([]), "not-a-uuid")[0],  # malformed id
        _call(_FakeFhir([_row(10, pid=a)], binary_error=FhirRequestError("gone", detail_code="not_found")), PATIENT_A)[0],
    ]
    assert {r.status_code for r in cases} == {404}
    assert len({r.text for r in cases}) == 1, [r.text for r in cases]


def test_logged_out_request_is_401_and_logged_without_a_user() -> None:
    response, events = _call(None, PATIENT_A, logged_in=False)
    assert response.status_code == 401
    assert len(events) == 1 and events[0]["reason"] == "unauthenticated" and events[0]["resident_username"] is None


def test_upstream_failure_stays_a_502_not_a_denial() -> None:
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A])], binary_error=FhirRequestError("t", detail_code="timeout"))
    response, events = _call(fhir, PATIENT_A)
    assert response.status_code == 502 and events == []


def test_denial_log_has_correlation_id_user_reason_and_no_phi() -> None:
    hostile_doc_id = "Testpatient_Alice_03-14-1972_labs.png"
    fhir = _FakeFhir([_row(10, pid=PID[PATIENT_A], name=hostile_doc_id, deleted=True)])
    session_id = f"preview-authz-{uuid.uuid4()}"
    capture = _Capture()
    logger = logging.getLogger("clinical_copilot")
    logger.addHandler(capture)
    _sessions._sessions[session_id] = ResidentSession(
        session_id=session_id, tokens=_LiveTokens(), fhir=fhir, resident_username=RESIDENT  # type: ignore[arg-type]
    )
    try:
        client = TestClient(app)
        client.cookies.set("copilot_session", session_id)
        client.get("/documents/preview", params={"patient_id": PATIENT_A, "doc_id": hostile_doc_id})
    finally:
        logger.removeHandler(capture)
        _sessions._sessions.pop(session_id, None)
    lines = [ln for ln in capture.lines if "document_preview_denied" in ln]
    assert len(lines) == 1, capture.lines
    event = json.loads(lines[0])
    from app.log_redaction import hash_identifier

    # 2026-09-25: the resident is logged as a salted hash (still the same user every time), never by name.
    assert event["resident_username"] == hash_identifier(RESIDENT) and event["reason"] == "not_found"
    assert RESIDENT not in "\n".join(capture.lines), "the raw resident username reached the denial log"
    assert uuid.UUID(event["correlation_id"])
    blob = "\n".join(capture.lines)
    for marker in PHI_MARKERS + [hostile_doc_id, PATIENT_A]:
        assert marker not in blob, f"{marker!r} leaked into the denial log: {blob}"


_TESTS: list[Callable[[], None]] = [
    test_non_admin_resident_gets_the_page_image,
    test_fhir_document_routes_are_never_used,
    test_deleted_document_is_404_and_bytes_are_never_fetched,
    test_another_patients_document_is_404,
    test_document_with_no_owning_patient_is_refused_not_trusted,
    test_only_the_source_document_category_is_served,
    test_a_duplicate_name_resolves_to_the_newest_live_row,
    test_the_requested_document_is_served_not_just_any_listed_one,
    test_patient_the_resident_cannot_read_is_404_for_denied_and_missing_alike,
    test_a_standard_api_403_is_a_denial_not_a_502,
    test_all_denials_return_an_identical_404_body,
    test_logged_out_request_is_401_and_logged_without_a_user,
    test_upstream_failure_stays_a_502_not_a_denial,
    test_denial_log_has_correlation_id_user_reason_and_no_phi,
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
