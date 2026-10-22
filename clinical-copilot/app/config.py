"""Runtime configuration, loaded from environment variables.

This is the one place `.env` values get parsed. Nothing else in the app
reads `os.environ` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openemr_base_url: str
    openemr_fhir_base_url: str
    oauth_token_url: str
    oauth_authorize_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_username: str
    oauth_password: str
    oauth_scope: str

    # Where this Co-Pilot instance itself is reachable -- used to build the
    # `redirect_uri` for the authorization_code flow (app/oauth_session.py).
    # Must match a `redirect_uri` already registered on the OpenEMR OAuth
    # client (see clinical-copilot/README.md's setup section).
    copilot_base_url: str

    # A second, deliberately narrower-scoped credential used ONLY by the
    # oauth_scope_enforcement_denied eval case (evals/cases.py) to prove
    # OpenEMR's OAuth server actually rejects FHIR calls outside a token's
    # granted scope -- never used on the live /chat path. Optional so the
    # rest of the app/eval suite still runs without it configured; that one
    # eval case skips (not fails) if unset.
    oauth_scope_test_username: str | None
    oauth_scope_test_password: str | None

    anthropic_api_key: str
    anthropic_model: str

    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None

    # Per-attempt httpx timeout (connect/read/write/pool each bounded to this
    # many seconds) for every FHIR/OAuth call -- set once here, on the shared
    # httpx.Client construction point (app/fhir_client.py's FhirClient.__init__,
    # app/auth.py's OAuthTokenProvider.__init__), not per call site, so every
    # current and future caller inherits it automatically. A timed-out first
    # attempt gets exactly one retry at half this value -- see
    # FhirClient._get_with_retry's docstring for why. 12s, not the round
    # number this defaulted to before (15s, present since the very first
    # commit, never tied to real data): PERFORMANCE_BASELINE.md's per-FHIR-call
    # span data (avg 2.66s, p95 20.87s, max 33.41s, pre-parallelization) shows
    # typical calls finish in a few seconds, so 12s is generous headroom over
    # the common case while still bounding the tail well under what an
    # unbounded hang could cost a resident.
    request_timeout_seconds: float
    verify_tls: bool

    # LLM provider abstraction (app/llm_client.py) -- "anthropic" is this
    # field's own default and the only validated path (see that module's
    # docstring / ARCHITECTURE.md's Known tradeoffs). The openai_compatible_*
    # fields are placeholders only at this stage: not required, not
    # validated -- OpenAICompatibleLLMClient raises NotImplementedError on
    # every real method regardless of what's set here, so an unset or wrong
    # value here cannot yet cause a live failure against real patient data.
    llm_provider: str = "anthropic"
    openai_compatible_base_url: str | None = None
    openai_compatible_model: str | None = None
    openai_compatible_api_key: str | None = None

    @property
    def observability_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def _require(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(
            f"Missing required environment variable: {name}. See .env.example."
        )
    return value


@lru_cache
def get_settings() -> Settings:
    base_url = os.environ.get("OPENEMR_BASE_URL", "https://localhost:9300")
    return Settings(
        openemr_base_url=base_url,
        openemr_fhir_base_url=os.environ.get(
            "OPENEMR_FHIR_BASE_URL", f"{base_url}/apis/default/fhir"
        ),
        oauth_token_url=os.environ.get(
            "OPENEMR_OAUTH_TOKEN_URL", f"{base_url}/oauth2/default/token"
        ),
        oauth_authorize_url=os.environ.get(
            "OPENEMR_OAUTH_AUTHORIZE_URL", f"{base_url}/oauth2/default/authorize"
        ),
        oauth_client_id=_require("OPENEMR_OAUTH_CLIENT_ID"),
        oauth_client_secret=_require("OPENEMR_OAUTH_CLIENT_SECRET"),
        oauth_username=_require("OPENEMR_OAUTH_USERNAME"),
        oauth_password=_require("OPENEMR_OAUTH_PASSWORD"),
        oauth_scope=os.environ.get(
            "OPENEMR_OAUTH_SCOPE",
            "openid offline_access api:oemr api:fhir "
            "user/Patient.read user/Condition.read "
            "user/AllergyIntolerance.read user/MedicationRequest.read "
            "user/Encounter.read user/Observation.read "
            "user/encounter.rs "
            # Document ingestion (Decision 2, Investigation 1/2's revision):
            # OpenEMR's FHIR API has no create route for DocumentReference/
            # Binary at all (Investigation 1/2 -- Decision 2.1's originally
            # planned user/DocumentReference.write/user/Binary.write/
            # user/Observation.write scopes don't correspond to any real
            # route). Document upload instead goes through the Standard API
            # (POST /api/patient/:pid/document).
            #
            # W2 Checklist item 4 (2026-09-22): re-verified live against
            # this exact deployment, end to end (token claims decoded +
            # real request against each route, not log inference -- an
            # earlier pass here mis-attributed a stale/conflated log entry
            # and briefly had this wrong; this version is the one actually
            # confirmed by a clean, reproducible test). The scope a
            # Standard API GET needs depends on whether the route is a
            # collection or a single-instance route -- confirmed directly
            # from src/Common/Http/HttpRestRouteHandler.php's own
            # getGetRequestPermission(): an instance-identifier segment
            # (e.g. /document/{did}) -> 'r' (read); no instance identifier
            # (e.g. /document, a list) -> 's' (search). POST is always 'c'
            # (same file's checkSecurity() permission match). Concretely:
            #   - GET  /api/patient/:pid/document           -> user/document.s
            #   - POST /api/patient/:pid/document           -> user/document.c
            #   - GET  /api/patient/:pid/document/:did      -> user/document.r
            #   - GET  /fhir/DocumentReference?patient=...  -> user/DocumentReference.s
            #     (a patient-filtered search, not a by-id read -- confirmed
            #     live: .s returns 200, and citations here always filter by
            #     patient, never fetch a single DocumentReference by id)
            # Also confirmed: the registered OAuth client's own `scope`
            # column (oauth_clients table) does NOT gate what a password-
            # grant token is actually issued -- user/DocumentReference.s
            # was granted and worked live even though that column still
            # only lists user/DocumentReference.read. No client-record
            # change was needed for any of this.
            #
            # user/document.r is requested even though the route it gates
            # (GET .../document/:did, used by app/ingestion/storage.py's
            # list_extracted_documents to read a stored extraction back) is
            # currently blocked by a SEPARATE, unrelated bug -- that route
            # 500s with "CSRF key is empty" because DocumentRestController::
            # downloadFile() calls into the legacy, session-based C_Document
            # controller, which a stateless OAuth Bearer-token REST request
            # never populates a csrf_private_key for (see README.md's
            # "Known gap" note). Requesting the correct scope now means
            # nothing further is needed here once that route is patched.
            #
            # user/Binary.read (2026-09-22): powers app/ingestion/storage.py's
            # get_source_document_bytes() (app/main.py's /documents/preview,
            # Checklist item 7's click-to-source bbox overlay), rerouted off
            # the CSRF-blocked Standard API path onto GET /fhir/Binary/:id --
            # confirmed live this has NO CsrfUtils/C_Document dependency at
            # all, a genuinely working, modern route. Requires this exact
            # suffix: the server defines Binary only under FHIR's older V1
            # `.read` naming (ServerScopeListEntity::fhirResourceScopesV1()),
            # not the `.r`/`.s`/`.rs` scheme every other resource above uses --
            # `user/Binary.r` silently drops from the issued token (no error,
            # just absent -- confirmed via a decoded token's own scopes
            # claim). Also required a one-row registration on the OAuth
            # client itself (oauth_clients.scope): ScopeRepository::
            # finalizeScopes() only lets through scopes the client was
            # registered with, and this client had zero Binary entries in
            # any form before now -- same category of one-row admin fix as
            # the MIME-whitelist INSERT (README.md, "A second, unrelated fix
            # is also required" section).
            #
            # user/patient.r (2026-09-22): powers app/ingestion/storage.py's
            # resolve_internal_pid() -- GET /patient/:puuid (Standard API
            # single-patient read), single-instance so 'r' per
            # HttpRestRouteHandler::getGetRequestPermission(), server-
            # supported per ServerScopeListEntity.php's Standard API resource
            # map ('patient' => ['c','r','u','s']). Replaces the original
            # approach (reading pid off the patient's first encounter via
            # GET /patient/:puuid/encounter, user/encounter.rs) -- found
            # investigating a staging CI failure: evals/seed_fixtures.py
            # only gives ONE of its six fixture patients (TEST-0004) a real
            # encounter, never PID1, so resolve_internal_pid() raised
            # not_found for PID1 before document upload/read-back could
            # even start on staging. The encounter route was never a
            # genuine requirement -- picked because its scope happened to
            # already be authorized elsewhere in this codebase, not because
            # it's the only way to get pid. Needed the same one-row
            # oauth_clients registration as user/Binary.read above (this
            # client had zero user/patient.* entries before now).
            "user/document.c user/document.s user/document.r user/DocumentReference.s "
            "user/Binary.read user/patient.r",
        ),
        copilot_base_url=os.environ.get("COPILOT_BASE_URL", "http://localhost:8420"),
        oauth_scope_test_username=os.environ.get("OPENEMR_SCOPE_TEST_USERNAME"),
        oauth_scope_test_password=os.environ.get("OPENEMR_SCOPE_TEST_PASSWORD"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        langfuse_host=os.environ.get("LANGFUSE_HOST", "http://localhost:3300"),
        request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "12")),
        verify_tls=os.environ.get("OPENEMR_VERIFY_TLS", "true").lower() not in ("false", "0", "no"),
        llm_provider=os.environ.get("LLM_PROVIDER", "anthropic"),
        openai_compatible_base_url=os.environ.get("OPENAI_COMPATIBLE_BASE_URL"),
        openai_compatible_model=os.environ.get("OPENAI_COMPATIBLE_MODEL"),
        openai_compatible_api_key=os.environ.get("OPENAI_COMPATIBLE_API_KEY"),
    )
