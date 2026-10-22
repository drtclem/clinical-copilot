"""Thin HTTP client for OpenEMR's FHIR API.

Data access is exclusively through this client -- never the database directly
-- per ARCHITECTURE.md 1.3 / 4.4. Every method here either returns a parsed
FHIR Bundle/dict or raises FhirRequestError; it never swallows a failure
silently (ARCHITECTURE.md Section 4's hard rule).

Every real GET this client (or CachingFhirClient, which delegates to one)
issues is bounded by a single per-attempt timeout and at most one retry --
both centralized on FhirClient.__init__'s shared httpx.Client and
_get_with_retry respectively (2026-09-19 timeout/retry design), not
per-call-site, the same centralization already used for the timeout that
predates this design. A timed-out or failed call surfaces as the existing
FhirRequestError -> ToolFailure/partial_failures path (app/tools.py) --
never an unhandled exception, and never a silent hang.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.auth import OAuthTokenProvider
from app.config import Settings


class FhirRequestError(Exception):
    def __init__(self, message: str, *, detail_code: str, http_status: int | None = None):
        super().__init__(message)
        self.detail_code = detail_code
        # Set only for a non-200 HTTP response, so a caller can tell an
        # authorization refusal (401/403) from any other http_error.
        self.http_status = http_status


class FhirClient:
    def __init__(self, settings: Settings, token_provider: OAuthTokenProvider, client: httpx.Client | None = None):
        self._settings = settings
        self._tokens = token_provider
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds, verify=settings.verify_tls
        )
        # Best-effort dashboard signal (app/agent.py reads this via
        # get_and_reset_retry_count around each tool call), not
        # safety-critical data -- no lock. get_patient_snapshot's own
        # ThreadPoolExecutor (app/tools.py) can call _get_with_retry
        # concurrently on this same instance, so a simultaneous retry on
        # two threads could rarely lose one increment; that only means the
        # retry-count metric undercounts by one in an already-rare
        # condition, never a wrong safety/authorization outcome.
        self._retry_count = 0

    def _get_with_retry(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with at most one retry, on a timeout or a 5xx-class response
        only -- not on a 4xx (not transient, retrying won't change a bad
        request) and not on a non-timeout httpx.HTTPError like a connection
        refusal (deliberately scoped to just the two conditions named in
        ARCHITECTURE.md Section 4's failure-handling note; broadening this
        to other transient network errors is a real future option, not
        assumed equivalent without its own justification).

        The single retry uses half the configured per-attempt timeout, not a
        fresh full one: PERFORMANCE_BASELINE.md found OpenEMR's own DB/CPU,
        not this service, is the real bottleneck under load, so a second
        full-length attempt against a backend that just failed compounds the
        exact problem being worked around, and would let one slow call cost
        the resident up to 2x the configured timeout for no added value. A
        call that already failed once is more likely to either fail fast or
        succeed quickly on retry than to need another full wait.

        This is the single point every FhirClient method funnels a real GET
        through -- centralized here, not per call site, for the same reason
        the timeout itself is centralized on __init__'s shared httpx.Client:
        one place to audit, every current and future caller (including every
        CachingFhirClient delegate call) inherits it automatically."""
        try:
            response = self._client.get(url, params=params, headers=self._headers())
            if response.status_code < 500:
                return response
        except httpx.TimeoutException:
            pass  # fall through to the single retry, below
        self._retry_count = getattr(self, "_retry_count", 0) + 1
        retry_timeout = self._settings.request_timeout_seconds / 2
        return self._client.get(url, params=params, headers=self._headers(), timeout=retry_timeout)

    def get_and_reset_retry_count(self) -> int:
        """Number of real GETs that needed the single retry (timeout or
        5xx) since the last call to this method -- lets a caller
        (app/agent.py, around each tool call) attribute retries to the
        specific Langfuse tool-call span that triggered them, without
        instrumenting every individual FHIR GET as its own span. Reads via
        getattr(default 0) rather than assuming __init__ ran: some eval
        fixtures subclass FhirClient without calling super().__init__()
        (they override every method that would touch _get_with_retry
        anyway), and this must never raise for those."""
        count = getattr(self, "_retry_count", 0)
        self._retry_count = 0
        return count

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        """GET <fhir_base>/<resource_type>?params, returns the parsed Bundle."""
        url = f"{self._settings.openemr_fhir_base_url}/{resource_type}"
        try:
            response = self._get_with_retry(url, params=params)
        except httpx.TimeoutException as exc:
            raise FhirRequestError(
                f"Timed out calling {resource_type} search", detail_code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(
                f"Network error calling {resource_type} search", detail_code="http_error"
            ) from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{resource_type} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{resource_type} search returned HTTP {response.status_code}",
                detail_code="http_error",
                http_status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirRequestError(
                f"{resource_type} search returned non-JSON body", detail_code="malformed_response"
            ) from exc
        if body.get("resourceType") != "Bundle":
            raise FhirRequestError(
                f"{resource_type} search did not return a FHIR Bundle", detail_code="malformed_response"
            )
        return body

    def search_fresh(self, resource_type: str, params: dict[str, str]) -> dict:
        """Identical to search() on a plain FhirClient -- there's nothing to
        bypass here. Exists so a call site can demand a guaranteed-fresh
        fetch by name (self-documenting at the call site -- see
        app/tools.py's check_allergy_conflict and compare_signout_to_chart)
        without every FhirClient needing a bypass_cache parameter on
        search() itself, which would force every existing eval fixture
        overriding search(self, resource_type, params) to change its
        signature too. CachingFhirClient overrides this to actually skip
        its cache; every other FhirClient (this one, and any eval fixture
        subclassing it) never caches in the first place, so this is just
        search()."""
        return self.search(resource_type, params)

    def read(self, resource_type: str, resource_id: str) -> dict:
        """GET <fhir_base>/<resource_type>/<id>, returns the parsed resource."""
        url = f"{self._settings.openemr_fhir_base_url}/{resource_type}/{resource_id}"
        try:
            response = self._get_with_retry(url)
        except httpx.TimeoutException as exc:
            raise FhirRequestError(
                f"Timed out reading {resource_type}/{resource_id}", detail_code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(
                f"Network error reading {resource_type}/{resource_id}", detail_code="http_error"
            ) from exc

        if response.status_code == 404:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} not found", detail_code="not_found"
            )
        if response.status_code != 200:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} returned HTTP {response.status_code}",
                detail_code="http_error",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} returned non-JSON body",
                detail_code="malformed_response",
            ) from exc

    def get_standard_api(self, path: str) -> dict | list:
        """GET <openemr_base_url>/apis/default/api<path> -- OpenEMR's own
        standard REST API (`api:oemr` scope), used only where the FHIR API
        omits a field this app needs (Encounter.sensitivity -- see
        app/sensitivity.py's module docstring). Still "OpenEMR's REST/FHIR
        API" per ARCHITECTURE.md 1.3, not a direct-database read.
        """
        url = f"{self._settings.openemr_base_url}/apis/default/api{path}"
        try:
            response = self._get_with_retry(url)
        except httpx.TimeoutException as exc:
            raise FhirRequestError(f"Timed out calling {path}", detail_code="timeout") from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(f"Network error calling {path}", detail_code="http_error") from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{path} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{path} returned HTTP {response.status_code}",
                detail_code="http_error",
                http_status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirRequestError(f"{path} returned non-JSON body", detail_code="malformed_response") from exc
        return body.get("data", body) if isinstance(body, dict) else body

    def get_standard_api_binary(self, path: str) -> bytes:
        """GET <openemr_base_url>/apis/default/api<path>, returning the raw
        response body -- for the one Standard API route that returns a
        file, not JSON (GET /api/patient/:pid/document/:did, confirmed live:
        DocumentRestController::downloadFile returns a BinaryFileResponse).
        get_standard_api() above always calls response.json() and would
        raise malformed_response against this route's real output."""
        url = f"{self._settings.openemr_base_url}/apis/default/api{path}"
        try:
            response = self._get_with_retry(url)
        except httpx.TimeoutException as exc:
            raise FhirRequestError(f"Timed out calling {path}", detail_code="timeout") from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(f"Network error calling {path}", detail_code="http_error") from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{path} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{path} returned HTTP {response.status_code}",
                detail_code="http_error",
                http_status=response.status_code,
            )
        return response.content

    def get_fhir_binary(self, resource_type: str, resource_id: str) -> tuple[bytes, str]:
        """GET <fhir_base>/<resource_type>/<id>, for the one FHIR route that
        returns a raw file, not JSON (GET /fhir/Binary/:id -- confirmed
        live, 2026-09-22 investigating the CSRF-gap fix: FhirDocumentRest
        Controller::downloadDocument() -> BaseDocumentDownloader returns a
        raw Content-Type: <mimetype> file response, not a FHIR-JSON-
        wrapped Binary resource with base64 `data`, even with an
        Accept: application/fhir+json header). read() above always calls
        response.json() and would raise malformed_response against this
        route's real output, the same reason get_standard_api_binary()
        exists alongside get_standard_api(). Mimetype comes from the
        response's own Content-Type header, not assumed."""
        url = f"{self._settings.openemr_fhir_base_url}/{resource_type}/{resource_id}"
        try:
            response = self._get_with_retry(url)
        except httpx.TimeoutException as exc:
            raise FhirRequestError(
                f"Timed out reading {resource_type}/{resource_id}", detail_code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(
                f"Network error reading {resource_type}/{resource_id}", detail_code="http_error"
            ) from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{resource_type}/{resource_id} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} returned HTTP {response.status_code}",
                detail_code="http_error",
                http_status=response.status_code,
            )
        mimetype = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        return response.content, mimetype

    def post_standard_api_document(
        self, path: str, *, params: dict[str, str], filename: str, content: bytes, content_type: str
    ) -> dict | list:
        """POST <openemr_base_url>/apis/default/api<path>, multipart/form-data
        with a `document` file field -- the one write path this project
        uses against OpenEMR (app/ingestion/storage.py, PRD Section 4.2:
        the FHIR API has no create route for DocumentReference/Binary at
        all; confirmed live -- src/RestControllers/DocumentRestController.php
        reads $_FILES['document']).

        No retry: `_get_with_retry`'s single-retry policy exists for
        read-only, idempotent GETs (this class' own docstring) -- retrying
        a failed document CREATE risks a duplicate write. A caller that
        wants resilience here should rely on app/ingestion/'s own dedupe
        key (sha256 of patient_id + doc_type + file bytes), not a blind
        client-level retry.
        """
        url = f"{self._settings.openemr_base_url}/apis/default/api{path}"
        try:
            response = self._client.post(
                url,
                params=params,
                files={"document": (filename, content, content_type)},
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise FhirRequestError(f"Timed out calling {path}", detail_code="timeout") from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(f"Network error calling {path}", detail_code="http_error") from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{path} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(f"{path} returned HTTP {response.status_code}", detail_code="http_error")
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirRequestError(f"{path} returned non-JSON body", detail_code="malformed_response") from exc
        return body.get("data", body) if isinstance(body, dict) else body

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.get_token()}",
            "Accept": "application/fhir+json",
        }


def bundle_entries(bundle: dict) -> list[dict]:
    """Extract the list of resource dicts from a FHIR Bundle, [] if none."""
    return [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]


# --- Per-conversation FHIR caching (2026-09-19 investigation) --------------
#
# Blind-trust window before even the cheap since-check below runs, per
# resource type -- reflects real-world staleness risk, not one blanket
# duration. AllergyIntolerance/Condition change rarely; MedicationRequest/
# Encounter can change same-day; Observation (labs/vitals) can change within
# minutes and is the most dangerous to serve stale, so it gets zero blind
# trust -- every reuse pays for the since-check below before being served.
# Any resource type not listed here (e.g. "Patient", used only for
# get_patient_snapshot's own-record read and duplicate-detection search) is
# never cached at all -- out of scope for this design, not an oversight.
_TRUST_WINDOW_SECONDS: dict[str, float] = {
    "AllergyIntolerance": 900.0,
    "Condition": 900.0,
    "MedicationRequest": 300.0,
    "Encounter": 300.0,
    "Observation": 0.0,
}

# Past this age, force a full refetch even if a since-check would say
# "unchanged" -- bounds worst-case staleness regardless of how quiet a
# chart has been, rather than letting a chain of cheap since-checks extend
# one original fetch's life indefinitely. One conversation is bounded by a
# single resident's current interaction, so this is generous on purpose,
# not a knob meant to be tuned in day-to-day use.
_HARD_CEILING_SECONDS = 3600.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _CacheEntry:
    bundle: dict
    fetched_at_monotonic: float
    fetched_at_utc: str


class FhirResourceCache:
    """Per-conversation FHIR resource cache. Owned by app/main.py's
    _ConversationState and threaded into a CachingFhirClient for that
    conversation's turns -- never shared across conversation_ids. A second
    resident, or the same resident starting a fresh conversation later,
    must always get a cache miss: cross-conversation staleness (a
    different real moment in time, with neither side able to tell the data
    crossed a session boundary) is a materially more dangerous failure
    than within-conversation staleness bounded by one resident's current,
    active interaction.

    Keyed by (resource_type, patient_id) -- one conversation can span more
    than one patient (README/USERS.md don't rule this out), and each
    patient's data must invalidate independently.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _CacheEntry] = {}

    def get(self, resource_type: str, patient_id: str) -> _CacheEntry | None:
        return self._entries.get((resource_type, patient_id))

    def put(self, resource_type: str, patient_id: str, bundle: dict) -> None:
        self._entries[(resource_type, patient_id)] = _CacheEntry(
            bundle=bundle, fetched_at_monotonic=time.monotonic(), fetched_at_utc=_utc_now_iso()
        )


class CachingFhirClient(FhirClient):
    """Wraps a resident-scoped FhirClient (app/oauth_session.py's one
    instance per logged-in session) with a conversation-scoped cache. Same
    public interface every tool already calls -- app/tools.py needed no
    signature changes for the common (cached) path.

    Three call sites must never see cached data -- see each one's own
    comment in app/tools.py for why:
      - check_allergy_conflict's AllergyIntolerance fetch (calls
        search_fresh() directly, hardcoded, no parameter).
      - compare_signout_to_chart's three delegated fetches (passes
        bypass_cache=True into get_patient_snapshot/get_recent_encounters/
        get_recent_observations, which call search_fresh() when set).
      - get_recent_encounters' sensitivity lookup via get_standard_api():
        enforced structurally below, not by convention at a call site --
        see that method's own docstring for why this one has no bypass
        toggle at all, it simply never caches.
    """

    def __init__(self, delegate: FhirClient, cache: FhirResourceCache):
        self._delegate = delegate
        self._cache = cache

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        patient_id = params.get("patient")
        window = _TRUST_WINDOW_SECONDS.get(resource_type)
        if window is None or patient_id is None:
            # Not one of the five resource types this design covers (e.g.
            # "Patient"), or no patient to key the cache on -- pass through
            # uncached, exactly like a plain FhirClient.
            return self._delegate.search(resource_type, params)

        entry = self._cache.get(resource_type, patient_id)
        if entry is not None:
            age = time.monotonic() - entry.fetched_at_monotonic
            if age < _HARD_CEILING_SECONDS:
                if age < window:
                    return entry.bundle
                if not self._changed_since(resource_type, params, entry.fetched_at_utc):
                    return entry.bundle
            # else: hard ceiling exceeded, or something changed since the
            # cached fetch -- fall through to a full refetch below.

        bundle = self._delegate.search(resource_type, params)
        self._cache.put(resource_type, patient_id, bundle)
        return bundle

    def search_fresh(self, resource_type: str, params: dict[str, str]) -> dict:
        """Always bypasses the cache -- does not even consult it, does not
        populate it. Deliberately does not update a matching cache entry
        with what it fetches either: a hard-bypass caller's whole point is
        guaranteeing freshness for its own decision, not refreshing the
        cache for someone else's later, possibly-cached read."""
        return self._delegate.search(resource_type, params)

    def _changed_since(self, resource_type: str, params: dict[str, str], since_utc: str) -> bool:
        """The cheap invalidation check (2026-09-19 investigation, live-
        verified against Condition/MedicationRequest/AllergyIntolerance/
        Encounter): _lastUpdated is a real, working search filter on all
        five resource types this design covers, backed by a real DB column
        even for the two types (Condition, Observation) whose individual
        resources don't echo meta.lastUpdated. An empty result means
        nothing changed since the cached fetch -- cheaper than a full
        refetch since there's nothing to parse, not just fewer bytes."""
        probe_params = dict(params)
        probe_params["_lastUpdated"] = f"gt{since_utc}"
        probe_bundle = self._delegate.search(resource_type, probe_params)
        return len(bundle_entries(probe_bundle)) > 0

    def read(self, resource_type: str, resource_id: str) -> dict:
        # Patient reads (get_patient_snapshot's own-record fetch) are out of
        # scope for this design -- a single cheap call, not the N+1 pattern
        # driving latency -- so this always passes through uncached.
        return self._delegate.read(resource_type, resource_id)

    def get_standard_api(self, path: str) -> dict | list:
        """Hard bypass, enforced here rather than by convention at the one
        call site that uses this (get_recent_encounters' Encounter
        sensitivity lookup). Unlike the five FHIR resource types above,
        this standard (non-FHIR) REST API endpoint has NO invalidation
        signal at
        all (confirmed by reading EncounterRestController::getAll(), which
        calls EncounterService::search([], true, $puuid) with a hardcoded
        empty filter -- the route never even forwards query parameters, so
        there's no _lastUpdated-equivalent to fall back on here). Caching
        this would mean either blind time-based expiry for a sensitivity
        classification -- exactly the pattern this whole design exists to
        avoid for anything safety/authorization-relevant -- or serving a
        previously-"normal" encounter as still-normal after it was
        reclassified "high", which would wrongly disclose it to a resident
        whose ACL role should no longer see it. That's the same category of
        harm (though a different kind: confidentiality, not physical) as
        the allergy-conflict check, and ARCHITECTURE.md 3.3 already
        mandates fail-closed for exactly this control. So: never cached,
        no bypass_cache parameter to accidentally flip -- this method
        simply never touches the cache."""
        return self._delegate.get_standard_api(path)

    def get_standard_api_binary(self, path: str) -> bytes:
        """Hard passthrough -- same reasoning as get_standard_api above."""
        return self._delegate.get_standard_api_binary(path)

    def get_fhir_binary(self, resource_type: str, resource_id: str) -> tuple[bytes, str]:
        """Hard passthrough -- same reasoning as get_standard_api above."""
        return self._delegate.get_fhir_binary(resource_type, resource_id)

    def post_standard_api_document(
        self, path: str, *, params: dict[str, str], filename: str, content: bytes, content_type: str
    ) -> dict | list:
        """Hard passthrough, same reasoning as get_standard_api above: a
        write has no cache semantics to begin with, and this wrapper's
        __init__ never sets up the httpx.Client/token machinery the base
        implementation needs -- only the real delegate can actually make
        this call."""
        return self._delegate.post_standard_api_document(
            path, params=params, filename=filename, content=content, content_type=content_type
        )

    def get_and_reset_retry_count(self) -> int:
        """Passes through to the real FhirClient doing the actual HTTP
        calls -- this wrapper never retries anything itself. Reads via
        getattr rather than a direct call: a delegate could in principle be
        an eval fixture that doesn't implement this (see FhirClient's own
        docstring on this method), and a best-effort dashboard signal must
        never be the reason a real chat turn fails."""
        getter = getattr(self._delegate, "get_and_reset_retry_count", None)
        return getter() if getter is not None else 0
