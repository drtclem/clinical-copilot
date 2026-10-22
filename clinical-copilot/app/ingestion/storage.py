"""The Standard API write path (PRD Section 4.2): OpenEMR's FHIR API has
no create route for DocumentReference/Binary/Observation/QuestionnaireResponse
in this version (confirmed three independent ways per the PRD: route
table, service-layer no-op, live 404). All writes go through
POST /api/patient/:pid/document.

Storage model: both the original scanned document AND a second document
containing the structured JSON of extracted facts are uploaded via the
same endpoint, under two categories already created in OpenEMR: Source
Document (category id 35), AI-Extracted Pending Review (category id 36).
"""

from __future__ import annotations

import hashlib
import json

from app.fhir_client import FhirClient, FhirRequestError
from app.ingestion.schemas import DocType, ExtractionResult

# Category paths, as OpenEMR's DocumentService::isValidPath/getLastIdOfPath
# expect them (PRD Section 4.2: category selection uses the `path=`
# parameter; a real OpenEMR bug -- DocumentService::getLastIdOfPath()
# missing a space-strip on the bound parameter -- was found and already
# patched in this fork, confirmed live in src/Services/DocumentService.php).
SOURCE_DOCUMENT_PATH = "Categories/Source Document"
AI_EXTRACTED_PENDING_REVIEW_PATH = "Categories/AI-Extracted Pending Review"

# Root-caused live, 2026-09-25: a chart_data_worker turn on a patient with
# dozens of uploaded documents sent an 84,331-input-token call ($1.04 vs.
# the usual ~$0.60), because list_extracted_documents() fetched and
# returned every document's full extracted-JSON binary with no bound, and
# get_uploaded_documents (app/tools.py) serialized all of it back into the
# conversation. 10 is chosen as "enough to answer realistically-scoped
# questions about a patient's recent uploads" without re-opening the same
# unbounded-context failure mode -- a real patient rarely has more than a
# handful of recent uploads relevant to a single question, and older ones
# are still on file and can be asked about specifically by name if needed.
# Sorted by `documents.id` (insertion order), not the OCR'd
# `collection_date` field: collection_date is model-extracted from
# whatever text is on the page and is not a reliable/sortable value (a
# scanned lab report might have no date, a malformed date, or a date that
# doesn't sort against other documents' dates at all).
MAX_UPLOADED_DOCUMENTS_RETURNED = 10


class DuplicateDocumentError(Exception):
    """Raised when dedupe_key() already matches a previously-uploaded
    document for this patient/doc_type -- app/ingestion/pipeline.py
    catches this and returns a DocumentIngestionFailure(detail_code=
    "duplicate") rather than creating a second copy."""


def dedupe_key(patient_id: str, doc_type: DocType, file_bytes: bytes) -> str:
    """sha256(patient_id + doc_type + file_bytes), PRD Section 4.2's
    exact dedupe scheme -- checked before creating a new document upload."""
    hasher = hashlib.sha256()
    hasher.update(patient_id.encode("utf-8"))
    hasher.update(b"|")
    hasher.update(doc_type.encode("utf-8"))
    hasher.update(b"|")
    hasher.update(file_bytes)
    return hasher.hexdigest()


def resolve_internal_pid(patient_id: str, fhir: FhirClient) -> int:
    """OpenEMR's Standard API document routes key on the classic internal
    numeric pid (documents.foreign_id), NOT the FHIR UUID every other tool
    in this codebase uses as patient_id -- confirmed live (a UUID passed
    directly as :pid on /api/patient/:pid/document 404s; the FHIR Patient
    resource itself exposes no numeric-pid identifier). Resolved via
    GET /patient/:puuid (Standard API single-patient read) -- confirmed
    live the response is a bare dict with `pid` present directly on every
    patient record (src/Services/PatientService.php's standard column
    set), no encounter involved at all.

    2026-09-22: switched from the original approach (reading `pid` off
    this patient's first encounter via GET /patient/:puuid/encounter,
    user/encounter.rs) -- found investigating a staging CI failure:
    evals/seed_fixtures.py only gives ONE of its six fixture patients
    (TEST-0004) a real encounter, never PID1/TEST-0001, so this function
    raised not_found for PID1 before document upload/read-back could even
    start on staging -- a real platform-availability bug, not a fixture
    quirk specific to the test that happened to surface it. The
    encounter route was never a genuine requirement here; it was picked
    because that scope was already authorized elsewhere in this codebase
    at the time, not because it's the only way to resolve pid -- confirmed
    by reading src/Services/PatientService.php directly (pid is a normal,
    always-present field on every patient record) and by this codebase's
    own evals/seed_fixtures.py already reading `pid` straight off
    POST /api/patient's response with zero encounter dependency. Raises
    FhirRequestError (detail_code="not_found") if the patient itself
    doesn't resolve, rather than guessing -- a hard rule, not a fallback."""
    patient = fhir.get_standard_api(f"/patient/{patient_id}")
    if not isinstance(patient, dict) or "pid" not in patient:
        raise FhirRequestError(
            f"Could not resolve internal pid for patient {patient_id}.", detail_code="malformed_response"
        )
    return int(patient["pid"])


def _list_documents(fhir: FhirClient, pid: int, category_path: str) -> list[dict]:
    """GET /patient/:pid/document?path=<category_path>, treating OpenEMR's
    own "empty category" response as an empty list rather than an error.
    Confirmed live: DocumentService::getAllAtPath returns `false` for a
    valid category with zero documents in it (not `[]`), and
    RestControllerHelper::responseHandler serializes a falsy serviceResult
    as HTTP 404 -- indistinguishable, at the HTTP layer, from "this route
    doesn't exist." Every real caller of this route in this codebase
    (dedupe pre-check, list_extracted_documents) wants "no documents yet"
    treated as a normal, common, non-error outcome, not propagated as a
    FhirRequestError -- see FhirRequestError's not_found detail_code
    remaining reserved for a genuinely missing PATIENT/route elsewhere in
    this client."""
    try:
        result = fhir.get_standard_api(f"/patient/{pid}/document?path={category_path}")
    except FhirRequestError as exc:
        if exc.detail_code == "not_found":
            return []
        raise
    return result if isinstance(result, list) else []


def _upload(fhir: FhirClient, pid: int, category_path: str, filename: str, content: bytes, content_type: str) -> None:
    fhir.post_standard_api_document(
        f"/patient/{pid}/document",
        params={"path": category_path},
        filename=filename,
        content=content,
        content_type=content_type,
    )


def compute_document_ids(patient_id: str, doc_type: DocType, original_filename: str, file_bytes: bytes) -> tuple[str, str]:
    """The (source_doc_id, extracted_doc_id) filenames store_extraction()
    will upload under -- deterministic from dedupe_key(), computed with NO
    network call. Exposed separately (not just returned from
    store_extraction) so app/ingestion/pipeline.py can build the final,
    fully-populated ExtractionResult (its own doc_id/extracted_doc_id
    fields) BEFORE serializing it as the AI-Extracted-Pending-Review
    document's own JSON content -- otherwise the stored JSON would only
    know its own document ids after the fact, a step too late to include
    them in what gets stored."""
    key = dedupe_key(patient_id, doc_type, file_bytes)
    return f"{key}_source_{original_filename}", f"{key}_extracted.json"


def store_extraction(
    fhir: FhirClient,
    patient_id: str,
    original_content: bytes,
    original_content_type: str,
    source_doc_id: str,
    extracted_doc_id: str,
    extraction: ExtractionResult,
) -> None:
    """Uploads the original scan (Source Document) and the structured
    extraction JSON (AI-Extracted Pending Review), both via the Standard
    API, under the ids compute_document_ids() already decided.

    Raises DuplicateDocumentError before any upload if this exact content
    already exists for this patient (PRD Section 4.2's idempotency
    requirement) -- checked via the `hash` field
    DocumentService::getAllAtPath already returns, no separate side-table.
    """
    pid = resolve_internal_pid(patient_id, fhir)

    existing_source_docs = _list_documents(fhir, pid, SOURCE_DOCUMENT_PATH)
    # OpenEMR hashes uploaded content with sha3-512, not sha1/sha256
    # (confirmed live: library/classes/Document.class.php's createDocument,
    # `$this->hash = hash('sha3-512', $data)`) -- matched against that
    # exact algorithm, not assumed.
    content_hash = hashlib.sha3_512(original_content).hexdigest()
    for doc in existing_source_docs:
        if doc.get("hash") == content_hash:
            raise DuplicateDocumentError(
                f"A document with this exact content already exists for patient {patient_id} ({extraction.doc_type})."
            )

    _upload(fhir, pid, SOURCE_DOCUMENT_PATH, source_doc_id, original_content, original_content_type)

    extraction_json = extraction.model_dump_json(indent=2).encode("utf-8")
    _upload(fhir, pid, AI_EXTRACTED_PENDING_REVIEW_PATH, extracted_doc_id, extraction_json, "application/json")


def list_extracted_documents(
    fhir: FhirClient, patient_id: str
) -> tuple[list[ExtractionResult], list[str], int]:
    """Reads back previously-stored ExtractionResults for this patient --
    app/graph's 7th chart-data-worker tool (get_uploaded_documents,
    app/tools.py) and app/graph/workers.py's intake_extractor_node both use
    this, never re-running OCR on a later turn (PRD Section 4.2).

    Returns (results, partial_failures, omitted_count) -- NOT just results.
    Found live, 2026-09-22: a document genuinely on file whose binary
    read-back failed (the CSRF-key gap this deployment currently has,
    README.md's "Known gap") was being silently `continue`d past, so a
    real, existing document and a genuinely empty category were
    indistinguishable at the HTTP layer -- exactly the silent-degradation
    ARCHITECTURE.md Section 2/4's hard rule exists to prevent
    (get_uploaded_documents surfaced "no documents on file" when a
    document actually existed but couldn't be read). A malformed/corrupt
    stored JSON (ValueError/JSONDecodeError) is still a genuine
    per-document data problem, distinct from this deployment's transient
    route failure, but gets the exact same disclosure treatment -- the
    caller doesn't get to silently drop either.

    Capped at MAX_UPLOADED_DOCUMENTS_RETURNED (root-caused live,
    2026-09-25 -- see that constant's docstring): sorted by `id`
    descending (most recently uploaded first) and sliced to the cap
    BEFORE the binary-fetch loop, so an omitted document never costs a
    network round trip or a byte of context. omitted_count tells the
    caller how many documents beyond the cap exist, so get_uploaded_documents
    can disclose the omission rather than silently presenting a partial
    list as if it were complete."""
    pid = resolve_internal_pid(patient_id, fhir)
    docs = _list_documents(fhir, pid, AI_EXTRACTED_PENDING_REVIEW_PATH)
    docs = sorted(docs, key=lambda d: d.get("id", 0), reverse=True)
    omitted_count = max(0, len(docs) - MAX_UPLOADED_DOCUMENTS_RETURNED)
    docs = docs[:MAX_UPLOADED_DOCUMENTS_RETURNED]
    results: list[ExtractionResult] = []
    partial_failures: list[str] = []
    for doc in docs:
        did = doc.get("id")
        if did is None:
            continue
        filename = doc.get("filename", str(did))
        try:
            file_bytes = fhir.get_standard_api_binary(f"/patient/{pid}/document/{did}")
            results.append(ExtractionResult.model_validate(json.loads(file_bytes)))
        except FhirRequestError as exc:
            partial_failures.append(f"document {filename}: could not be read back ({exc.detail_code})")
        except (ValueError, json.JSONDecodeError):
            partial_failures.append(f"document {filename}: stored extraction is malformed/unreadable")
    return results, partial_failures, omitted_count


class DocumentAccessDenied(FhirRequestError):
    """A source document the caller may not be shown. Always
    detail_code="not_found" so app/main.py's /documents/preview answers
    every reason with the same 404; `reason` exists only for the server-side
    denial log, and never carries patient or document content."""

    def __init__(self, reason: str) -> None:
        super().__init__("Source document unavailable.", detail_code="not_found")
        self.reason = reason


def get_source_document_bytes(fhir: FhirClient, patient_id: str, doc_id: str) -> tuple[bytes, str]:
    """Fetches the original scanned document's raw bytes by the filename-
    based id ExtractionResult.doc_id carries (compute_document_ids() uses
    the filename itself as the stored document's name, since OpenEMR's
    insertAtPath returns only a bool, not an id -- see that function's own
    docstring). Returns (content, mimetype). Used by app/main.py's preview
    endpoint (Checklist item 7's click-to-source UI) to re-rasterize the
    same page image app/ingestion/ocr.py's pipeline already produced at
    upload time.

    2026-09-25: moved back onto the Standard API, under the resident's
    OWN token, with no privilege borrowing. The FHIR route this used from
    2026-09-22 (GET /fhir/DocumentReference?patient=..., then GET
    /fhir/Binary/:id) is gated in OpenEMR by
    RestConfig::request_authorization_check($request, "admin", "super")
    and "admin", "users" respectively
    (apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php), i.e. it only
    worked for a Superuser -- a Clinicians/Physicians resident got a 403
    and every preview said "not available" (found live 2026-09-25 with the
    test_clinician account; recorded in AUDIT.md). The Standard API document
    routes are gated at "patients"/"docs" instead, which Clinicians hold by
    default, and get_uploaded_documents already reads through them with
    the same token.

    What the server enforces, so this function does not have to re-derive
    it (DocumentService::getAllAtPath, src/Services/DocumentService.php):
    the listing is `WHERE ctd.category_id = ? AND doc.foreign_id = ? AND
    doc.deleted = 0`. So a document that belongs to another patient, that
    is soft-deleted, or that has no owning patient is simply absent from
    this patient's Source Document listing -- the same "not found" as a
    document that never existed. This function only ever fetches an id it
    just read out of that listing, never one taken from the request.
    (This replaces the FHIR-era status/subject checks on the
    DocumentReference resource; the same three refusals now come from the
    listing's own SQL.)

    The internal pid is resolved from the FHIR patient UUID under the
    resident's token (resolve_internal_pid), so a patient the resident's
    ACL cannot read fails there first, and the route also gates on a FHIR
    Patient read before calling this."""
    pid = resolve_internal_pid(patient_id, fhir)
    docs = _list_documents(fhir, pid, SOURCE_DOCUMENT_PATH)
    match: dict | None = None
    for doc in sorted(docs, key=lambda d: d.get("id", 0), reverse=True):
        if doc.get("filename") == doc_id and doc.get("id") is not None:
            match = doc
            break
    if match is None:
        raise DocumentAccessDenied("not_found")

    content = fhir.get_standard_api_binary(f"/patient/{pid}/document/{match['id']}")
    return content, match.get("mimetype") or "application/octet-stream"
