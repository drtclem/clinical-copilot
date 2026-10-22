"""HTTP interface: POST /chat.

Deliberately minimal per early-submission-build-prompt.md's scope note: "a
basic HTTP endpoint that takes a message and patient context, returns a
response." A real UI/OpenEMR module (ARCHITECTURE.md 1.1) is future work,
noted in README.md, not built here. GET /ui serves a standalone chat page
(app/static/chat.html) as a grader convenience only.

Conversation state is an in-memory dict keyed by conversation_id -- fine for
a single-process demo tonight; a real deployment needs a shared store
(ARCHITECTURE.md doesn't specify one yet, this is a new gap this build
surfaced, see README).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Cookie, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.disclosure_filter import scrub_internal_identifiers
from app.agent import ClinicalCopilotAgent
from app.config import get_settings
from app.fhir_client import CachingFhirClient, FhirRequestError, FhirResourceCache
from app.graph import run_turn as graph_run_turn
from app.ingestion import DocType, attach_and_extract
from app.ingestion.bbox import resolve_bbox
from app.ingestion.ocr import rasterize_page, run_ocr
from app.ingestion.storage import DocumentAccessDenied, get_source_document_bytes
from app.log_redaction import hash_identifier, install_access_log_filter
from app.observability import TurnObserver, log_event
from app.oauth_session import SESSION_COOKIE_NAME, LoginError, SessionStore
from app.schemas import ReferenceNote
from app.tools import _patient_display, collect_source_resources
from app.verification import ToolCallRecord

_READINESS_TIMEOUT_S = 2.5
# The FHIR capability statement (/metadata) is real and unauthenticated, but
# not as cheap to compute as a plain resource fetch -- measured latency on
# the droplet is ~2.3-3.5s (vs ~0.6-0.9s locally, consistent with its more
# modest resources, a pattern seen throughout this project's droplet work),
# right at or over the general 2.5s budget. A dedicated, more generous
# timeout for this one check, not a blanket increase for the others.
_OPENEMR_READINESS_TIMEOUT_S = 5.0

# The interactive API docs (/docs, /redoc) and the schema (/openapi.json) publish every route and model to
# anyone who can reach the port. Off unless ENABLE_API_DOCS is set (local development only).
_API_DOCS_ENABLED = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(
    title="Clinical Co-Pilot (Early Submission)",
    docs_url="/docs" if _API_DOCS_ENABLED else None,
    redoc_url="/redoc" if _API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _API_DOCS_ENABLED else None,
)

# Uvicorn's access log prints the request target; the query string carries patient ids. Strip it.
install_access_log_filter()

_REQUEST_TIMING_LOG_PATH = Path(__file__).parent.parent / "request_timing.log"

# Append-only, human-inspectable, independent of Langfuse (ERROR_ANALYSIS.md
# Entry 6: Langfuse's own telemetry can itself go quiet under contention) --
# same fixed-path convention as _REQUEST_TIMING_LOG_PATH above, not an
# env-configurable location.
_FEEDBACK_LOG_PATH = Path(__file__).parent.parent / "feedback.jsonl"


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    """Independent request-timing log -- plain file I/O, zero dependency on
    Langfuse being configured or reachable (ERROR_ANALYSIS.md Entry 6).

    PERFORMANCE_BASELINE.md's droplet load test found Langfuse's own
    telemetry can itself go quiet under real contention: only 5 of 20 real
    requests produced an AGENT span there, with the span duration it did
    record far below what the client actually experienced. This starts
    timing at the ASGI/middleware layer -- before routing reaches the
    /chat handler, and therefore before any Langfuse span is opened --
    so real request latency is captured even when Langfuse's own
    instrumentation degrades under load.
    """
    start = time.monotonic()
    response = await call_next(request)
    duration_s = time.monotonic() - start
    line = (
        f"{datetime.now(timezone.utc).isoformat()},{request.method},"
        f"{request.url.path},{duration_s:.3f},{response.status_code}\n"
    )
    with _REQUEST_TIMING_LOG_PATH.open("a") as f:
        f.write(line)
    return response


_settings = get_settings()
_observer = TurnObserver(_settings)
_agent = ClinicalCopilotAgent(_settings, observer=_observer)
# No default_fhir: Phase 1 (CLAUDE_CODE_BUILD_INSTRUCTIONS.md) removed the
# single shared password-grant credential from the live path -- every
# /chat call below resolves and passes its own resident-scoped FhirClient
# explicitly. An accidental omission raises in run_turn() rather than
# silently falling back to a shared identity.
_sessions = SessionStore(_settings)


@dataclass
class _ConversationState:
    history: list[dict] = field(default_factory=list)
    # Every real tool result fetched anywhere in this conversation so far --
    # must travel with the conversation the same way history does, or a
    # follow-up turn loses grounding for facts fetched in an earlier turn
    # (ERROR_ANALYSIS.md Entry 5).
    tool_records: list[ToolCallRecord] = field(default_factory=list)
    # Per-conversation FHIR cache (2026-09-19 caching design) -- scoped to
    # this conversation_id the same way tool_records is, never shared
    # across conversations: a fresh conversation_id always gets a fresh
    # FhirResourceCache, so a second resident (or this resident starting a
    # new conversation later) always gets a cache miss, never another
    # session's cached data. See app/fhir_client.py's FhirResourceCache/
    # CachingFhirClient docstrings for the caching policy itself.
    fhir_cache: FhirResourceCache = field(default_factory=FhirResourceCache)


_conversations: dict[str, _ConversationState] = {}


class ChatRequest(BaseModel):
    message: str
    patient_id: str | None = None
    conversation_id: str | None = None
    # {doc_id, doc_type} of a document POST /documents already OCR'd/
    # structured/stored THIS turn (PRD Section 4.2) -- set only on the
    # turn immediately following a successful upload; the graph's
    # intake_extractor worker (app/graph/workers.py) reads it back rather
    # than re-running OCR. None (the common case) means no document was
    # just attached.
    uploaded_document_ref: dict | None = None


class PatientHeader(BaseModel):
    patient_id: str
    name: str
    birth_date: str | None
    gender: str | None


class ChatResponse(BaseModel):
    conversation_id: str
    correlation_id: str
    response: str
    verification_passed: bool
    flagged_claims: list[str]
    enforced_warnings: list[str]
    tool_calls: list[dict]
    # Every distinct source_resource (e.g. "Condition/<id>") this conversation
    # has fetched for the active patient -- the set verification grounds
    # against (app/tools.py's collect_source_resources). Lets a client show
    # what a reply was based on without parsing tool_calls.
    source_resources: list[str]
    # Informational DailyMed/MedlinePlus footnotes (app/reference_layer.py) --
    # a separate field, never folded into `response`, so a client renders it
    # distinctly from the model's own text. Empty list when nothing triggers.
    reference_notes: list[ReferenceNote]
    # W2 PRD Section 3/4.4: the canonical Citation shape
    # (app/graph/citations.py), built once at the graph's synthesis step
    # across every worker's output. Additive field -- source_resources
    # above is untouched, same FHIR-only shape it always was.
    citations: list[dict] = Field(default_factory=list)
    # Structured notices about expected outcomes (app/graph/notices.py), e.g. a guideline search that
    # found nothing relevant. Info-level notices are not warnings and do not affect verification.
    notices: list[dict] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    correlation_id: str = Field(..., min_length=1)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=2000)
    # Chart context only, not identity -- same trust level as ChatRequest.
    # patient_id above, which the client already supplies directly to
    # /chat. There is no correlation_id -> patient_id mapping stored
    # anywhere to resolve this server-side instead (correlation_id is a
    # per-turn id, unrelated to the conversation_id keying _conversations).
    patient_id: str | None = None


class FeedbackResponse(BaseModel):
    status: Literal["recorded"]
    langfuse_attached: bool


@app.get("/health")
def health() -> dict:
    """Returns 200 if the process itself is running -- no dependency
    checks. Should never fail unless the process is genuinely down."""
    return {"status": "ok"}


def _check_openemr() -> bool:
    """Reuses the FhirClient's own configured base URL/TLS setting; hits
    the FHIR capability statement, which per FHIR_README.md needs no auth
    -- a real, lightweight, unauthenticated connectivity check, not a new
    heavy call invented for this."""
    try:
        resp = httpx.get(
            f"{_settings.openemr_fhir_base_url}/metadata",
            timeout=_OPENEMR_READINESS_TIMEOUT_S,
            verify=_settings.verify_tls,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _check_langfuse() -> bool:
    """If Langfuse isn't configured at all, that's a deliberate,
    supported configuration (app/observability.py degrades gracefully to
    local-only logging) -- not a failed dependency, so it doesn't count
    against readiness. If it IS configured, actually check its real
    public health endpoint."""
    if not _settings.observability_enabled:
        return True
    try:
        resp = httpx.get(f"{_settings.langfuse_host}/api/public/health", timeout=_READINESS_TIMEOUT_S)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


@app.get("/ready")
def ready() -> JSONResponse:
    """Actively checks each real dependency, per-dependency breakdown so a
    failure is diagnosable from the response itself, not just a single
    true/false (ARCHITECTURE.md 7.6)."""
    checks = {
        "openemr": _check_openemr(),
        # _agent._llm is the same LLMClient build_llm_client(settings)
        # resolved for the agent itself (app/llm_client.py), reused here
        # rather than constructing a second client. The "anthropic" key
        # name is unchanged (out of scope for this change -- renaming the
        # /ready response shape is a real API-contract change, not just an
        # implementation swap); today it is still accurate, since
        # AnthropicLLMClient is the only real (non-stub) LLMClient.
        "anthropic": _agent._llm.check_ready(),
        "langfuse": _check_langfuse(),
    }
    all_ready = all(checks.values())
    return JSONResponse(status_code=200 if all_ready else 503, content={"ready": all_ready, "checks": checks})


# pid1 (Alice Testpatient) -- a normal chart with a known duplicate record,
# see evals/fixtures.py / bruno/chat/01. Same UUID on local and droplet
# (both seeded from the same fixture data). Only ever offered as an explicit
# "use the demo patient" link on /ui's no-patient screen -- never silently
# substituted when ?patient= is missing, since which chart is open is a
# patient-safety question, not a convenience default.
_DEFAULT_PATIENT_ID = "98c4b82b-b07e-11f1-8334-022958ad0af8"

_UI_PATH = Path(__file__).parent / "static" / "chat.html"


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    """Serves app/static/chat.html -- a grader convenience, not a
    replacement for the real OpenEMR-embedded module (ARCHITECTURE.md
    1.1), which remains future work. No build step, no new dependency.
    The patient comes from the page's own `?patient=<uuid>` query
    parameter; whether the chat or a "Log in with OpenEMR" prompt renders
    is decided client-side by the page's own call to GET /me -- see that
    route below. Read per request (not cached at import) so an edit to the
    file shows up without a server restart."""
    return _UI_PATH.read_text(encoding="utf-8").replace("__DEFAULT_PATIENT_ID__", _DEFAULT_PATIENT_ID)


@app.get("/login")
def login(next: str = "/ui") -> RedirectResponse:
    """Starts the authorization_code flow: redirects the browser to
    OpenEMR's own login/authorize page. Phase 1 (CLAUDE_CODE_BUILD_
    INSTRUCTIONS.md) -- replaces the shared password-grant credential
    with a token bound to whichever resident actually logs in here,
    per ARCHITECTURE.md 1.3."""
    return RedirectResponse(_sessions.build_authorize_url(next_url=next))


@app.get("/callback")
def callback(code: str, state: str) -> RedirectResponse:
    """OpenEMR redirects here after the resident logs in. Exchanges the
    code for tokens, creates a session, sets the session cookie, and
    sends the browser on to wherever /login's `next` pointed (default
    /ui)."""
    try:
        session_id, next_url = _sessions.complete_login(code=code, state=state)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = RedirectResponse(next_url)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        # The droplet serves plain HTTP on 8420 today (documented,
        # accepted-risk grading exposure) -- a blanket Secure flag would
        # silently break login there, since browsers drop Secure cookies
        # over a non-TLS connection. Revisit once a TLS-fronted deployment
        # exists (clinical-copilot/README.md Known Gaps).
        secure=_settings.copilot_base_url.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/me")
def me(request: Request) -> JSONResponse:
    """Used by /ui's own JS to decide whether to show the chat form or a
    login prompt -- also handy for a quick curl-based liveness check of
    whether a session cookie is still valid."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = _sessions.get(session_id)
    if session is None:
        return JSONResponse(status_code=401, content={"authenticated": False})
    return JSONResponse(content={"authenticated": True, "resident": session.resident_username})


@app.get("/patient", response_model=PatientHeader)
def patient_header(
    patient_id: str,
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> PatientHeader:
    """Who a chart belongs to, for /ui's persistent patient header -- the
    identity has to be visible before the first message, not only after the
    model happens to mention it. One FHIR Patient read under the
    resident's own token, so OpenEMR's own scope/ACL decides what is
    visible; nothing is cached or retained here.

    patient_id is a query parameter, not a path segment, so the ID never
    lands in request_timing.log (which records only the URL path). It is
    parsed as a UUID and the canonical form returned -- the client uses
    that echoed ID for /chat, so the header and the chart the model is
    told about cannot differ by formatting."""
    session = _sessions.get(copilot_session)
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in.")
    try:
        canonical_id = str(uuid.UUID(patient_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid patient id.") from exc
    try:
        patient = session.fhir.read("Patient", canonical_id)
    except FhirRequestError as exc:
        if exc.detail_code == "not_found":
            raise HTTPException(status_code=404, detail="Patient not found.") from exc
        raise HTTPException(status_code=502, detail="Could not retrieve patient details.") from exc
    name, birth_date, gender = _patient_display(patient)
    return PatientHeader(patient_id=canonical_id, name=name, birth_date=birth_date, gender=gender)


_MAX_DOCUMENT_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB -- a generous cap for a scanned lab report/intake form,
# not something USERS.md's real documents should ever approach; exists purely to bound the request, not
# a tuned clinical-document-size figure.


class DocumentUploadErrorResponse(BaseModel):
    error: str
    detail_code: str


@app.post("/documents")
async def upload_document(
    patient_id: str = Form(...),
    doc_type: DocType = Form(...),
    file: UploadFile = File(...),
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> JSONResponse:
    """The one-shot ingestion entry point (PRD Section 4.2's attach_and_extract,
    "PDF's own wording -- naming is flexible"): OCR + LLM structuring +
    numeric-plausibility check + Standard API storage, run ONCE at upload
    time. The graph's intake_extractor worker (app/graph/workers.py) and
    the 7th chart-data-worker tool (get_uploaded_documents, app/tools.py)
    both read back what this call already stored -- neither re-runs OCR.

    Returns the full ExtractionResult on success so the uploading resident
    can review what was extracted (and its per-field confidence) before
    relying on it in conversation -- same "always answer honestly" posture
    as the rest of this system, not a black-box "uploaded successfully."
    """
    session = _sessions.get(copilot_session)
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first.",
        )

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_DOCUMENT_UPLOAD_BYTES:
        return JSONResponse(
            status_code=400,
            content=DocumentUploadErrorResponse(error="File too large.", detail_code="invalid_file_type").model_dump(),
        )

    correlation_id = str(uuid.uuid4())
    trace = _observer.start_turn(
        correlation_id, f"[document upload: {doc_type}]", patient_id, session.resident_username
    )
    result, failure, _input_tokens, _output_tokens = attach_and_extract(
        patient_id,
        file_bytes,
        file.filename or "upload",
        file.content_type or "application/octet-stream",
        doc_type,
        fhir=session.fhir,
        llm=_agent._llm,
        model=_settings.anthropic_model,
        trace=trace,
    )
    trace.finish_turn(final_response=f"[document upload result: {'ok' if result else failure.detail_code}]")

    if failure is not None:
        status = 409 if failure.detail_code == "duplicate" else 400
        return JSONResponse(status_code=status, content=DocumentUploadErrorResponse(
            error=failure.reason, detail_code=failure.detail_code
        ).model_dump())

    return JSONResponse(content=result.model_dump())


@app.get("/documents/preview")
def document_preview(
    patient_id: str,
    doc_id: str,
    page: int = 1,
    quote: str | None = None,
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Checklist item 7's click-to-source UI: the preview image behind a
    document citation's modal/lightbox. PRD Section 4.8's security-critical
    rule, verbatim: "preview images served through an endpoint that checks
    BOTH authentication (valid session, same as /chat) AND authorization
    (this specific resident has standing access to this specific patient)
    -- never a static file URL." Both checks here are real, not cosmetic:

    - Authentication: the same session-cookie gate every other endpoint
      uses (401 if absent/invalid) -- identical to /chat, /patient.
    - Authorization: everything runs under THIS resident's own FhirClient
      (session.fhir, the same per-resident OAuth-scoped client /chat and
      /patient use), so OpenEMR's own ACL decides what is visible
      (ARCHITECTURE.md 1.3's "access ceiling no higher than what the
      resident could reach directly"). On top of that: patient_id must be a
      UUID and a Patient read under the resident's token must succeed (the
      same gate /patient applies); the document must be in THAT patient's
      Source Document listing, which OpenEMR's own SQL restricts to
      foreign_id = this patient and deleted = 0 (see storage.py's
      get_source_document_bytes -- other patients' and soft-deleted
      documents are simply absent). Reads use the Standard API document
      routes (ACL patients/docs, held by Clinicians), NOT the FHIR
      DocumentReference/Binary routes, which OpenEMR gates behind
      admin/super and admin/users. Missing, deleted, other-patient's and
      not-visible all answer the same 404; the reason is only in the "document_preview_
      denied" log line (correlation_id, resident, reason -- no patient or
      document identifiers).

    Renders the requested page as a PNG on the fly (app/ingestion/ocr.py's
    rasterize_page -- the same rasterization the OCR pipeline already used
    at upload time, PRD Section 4.8: "No separate client-side PDF
    renderer") -- never returns the raw original file directly, and never
    a static/predictable file URL a resident could bookmark past their own
    session.

    quote: the citation's own quote_or_value (app/graph/citations.py) --
    when given, re-resolves that field's normalized bbox against this
    page's OCR word spans (app/ingestion/bbox.py, the same function
    structuring used at upload time) and returns it as the
    X-Citation-Bbox response header ("x0,y0,x1,y1", each in [0,1]) so the
    client can draw a highlight overlay without a second round trip.
    Absent (no page image + patient chart text to reason over) when a
    match can't be resolved -- never a fabricated box."""
    correlation_id = str(uuid.uuid4())
    session = _sessions.get(copilot_session)
    if session is None:
        log_event("document_preview_denied", correlation_id=correlation_id, resident_username=None, reason="unauthenticated")
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first.",
        )

    def deny(reason: str) -> HTTPException:
        # Same outward answer for every reason (missing, deleted, other
        # patient's, not visible to this resident); only the log differs.
        # Deliberately no patient_id/doc_id: both are caller-supplied
        # strings, and a reason code is all an investigator needs.
        log_event(
            "document_preview_denied",
            correlation_id=correlation_id,
            resident_username=session.resident_username,
            reason=reason,
        )
        return HTTPException(status_code=404, detail="Document not found.")

    try:
        canonical_patient_id = str(uuid.UUID(patient_id))
    except ValueError:
        raise deny("invalid_patient_id") from None

    try:
        # Same gate /patient uses: a Patient read under THIS resident's own
        # token. If OpenEMR won't show this resident the patient, no
        # document of theirs is shown either.
        session.fhir.read("Patient", canonical_patient_id)
        content, content_type = get_source_document_bytes(session.fhir, canonical_patient_id, doc_id)
        png_bytes = rasterize_page(content, content_type, page)
    except DocumentAccessDenied as exc:
        raise deny(exc.reason) from None
    except FhirRequestError as exc:
        if exc.http_status in (401, 403):
            raise deny("not_authorized") from None
        if exc.detail_code == "not_found":
            raise deny("not_found") from None
        raise HTTPException(status_code=502, detail="Could not retrieve this document.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    headers = {"Cache-Control": "private, no-store"}
    if quote:
        try:
            ocr_result = run_ocr(content, content_type)
            bbox = resolve_bbox(ocr_result, page, quote)
            if bbox is not None:
                headers["X-Citation-Bbox"] = ",".join(f"{v:.4f}" for v in bbox)
        except Exception:  # noqa: BLE001 -- the highlight is a nice-to-have; a failure here
            # must never break the preview image itself.
            pass

    return Response(content=png_bytes, media_type="image/png", headers=headers)


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> ChatResponse:
    session = _sessions.get(copilot_session)
    if session is None:
        # No more anonymous access and no shared fallback credential --
        # Phase 1's whole point. A curl caller now needs a real session
        # cookie from completing /login first; see README's updated
        # "Try it directly" section.
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first.",
        )

    conversation_id = request.conversation_id or str(uuid.uuid4())
    state = _conversations.get(conversation_id, _ConversationState())

    # session.fhir is one instance per logged-in resident session and can
    # outlive any single conversation (app/oauth_session.py) -- the cache
    # must NOT live on it directly, or a second conversation for the same
    # resident would inherit the first conversation's cached data. Wrapping
    # it fresh each call, bound to this conversation_id's own fhir_cache,
    # is what actually gives the per-conversation scope its safety
    # guarantee (2026-09-19 caching design).
    cached_fhir = CachingFhirClient(session.fhir, state.fhir_cache)
    result = graph_run_turn(
        settings=_settings,
        history=state.history,
        user_message=request.message,
        patient_id=request.patient_id,
        prior_tool_records=state.tool_records,
        uploaded_document_ref=request.uploaded_document_ref,
        fhir=cached_fhir,
        resident_username=session.resident_username,
        observer=_observer,
        chart_agent=_agent,
    )
    _conversations[conversation_id] = _ConversationState(
        history=result.updated_history,
        tool_records=result.accumulated_tool_records,
        fhir_cache=state.fhir_cache,
    )

    return ChatResponse(
        conversation_id=conversation_id,
        correlation_id=result.correlation_id,
        # VULN-103/105 (Week 3): internal identifiers never leave the service.
        response=scrub_internal_identifiers(result.response_text),
        verification_passed=result.verification_passed,
        flagged_claims=result.flagged_claims,
        enforced_warnings=result.enforced_warnings,
        tool_calls=result.tool_calls,
        source_resources=collect_source_resources(result.accumulated_tool_records, request.patient_id),
        # 2026-09-23: GraphTurnResult has no reference_notes field -- unlike
        # tool_calls (just above), this isn't a rename of existing data.
        # app/reference_layer.py's build_reference_notes() (DailyMed/
        # MedlinePlus footnotes) is called in Week 1's agent.py run_turn(),
        # never anywhere in app/graph/ -- the graph path never ported this
        # feature over when /chat switched to it. Empty list here is an
        # honest "nothing to show" (the schema's own documented meaning for
        # this field when nothing triggers), not a silent data loss of a
        # real value -- but it does mean every graph-path /chat response
        # is missing this feature entirely, every time, not just when
        # nothing would have triggered it. Flagged, not fixed here:
        # wiring build_reference_notes() into the graph (which node, with
        # which tool records) is a real design decision, not a rename.
        reference_notes=[],
        citations=result.citations,
        notices=result.notices,
    )


@app.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(
    payload: FeedbackRequest,
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> FeedbackResponse:
    """Resident feedback on one turn's response -- qualitative/supplementary
    signal only, per KEY_METRICS.md's "raw satisfaction is gameable, never a
    key metric" reasoning; this must never be folded into or reported
    alongside verification_pass_rate. Same auth gate as /chat: no anonymous
    feedback, and resident_username always comes from the session, never
    from the request body.

    Dual storage: (1) an append-only feedback.jsonl entry, which must
    succeed for the request to succeed and is the durable record
    independent of Langfuse; (2) a best-effort Langfuse score on the turn's
    existing trace (TurnObserver.attach_feedback_score) -- its failure is
    reported in the response but never fails the request itself.
    """
    session = _sessions.get(copilot_session)
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first.",
        )

    entry = {
        "correlation_id": payload.correlation_id,
        # Salted hashes (app/log_redaction.py), not raw identifiers: correlatable, not identifying. The free-text
        # `comment` below is the resident's own words and is kept -- that is the point of feedback.
        "resident_username": hash_identifier(session.resident_username),
        "patient_id": hash_identifier(payload.patient_id),
        "rating": payload.rating,
        "comment": payload.comment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _FEEDBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _FEEDBACK_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    langfuse_attached = _observer.attach_feedback_score(
        payload.correlation_id, payload.rating, payload.comment
    )

    return FeedbackResponse(status="recorded", langfuse_attached=langfuse_attached)
