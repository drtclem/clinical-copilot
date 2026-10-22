#!/usr/bin/env python3
"""Fast, deterministic, LLM-free tests: no Anthropic API calls, so free and
instant enough to run on every commit (see .github/workflows/tests.yml).

Different from evals/cases.py, which exercises the agent's actual LLM
behavior against real Anthropic calls and costs real spend per run -- those
stay manually-triggered/pre-deploy, not run on every commit. These tests
instead check the parts of the system that don't need a model call at all:
the two tools directly (real FHIR calls, no LLM), and verification.py's
stripping logic against a hand-constructed fake draft (no FHIR chart
lookups involved in the draft itself, though the domain-constraint check
inside verify_response still makes a real, deterministic FHIR call --
that's the same LLM-free/network-allowed distinction as (a) and (b)).

Follows the same lightweight runner convention as run_evals.py (no pytest
dependency added) rather than introducing a second test-runner pattern.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
import uuid
from typing import Callable

import httpx
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import CachingFhirClient, FhirClient, FhirRequestError, FhirResourceCache, bundle_entries
from app.llm_client import AnthropicLLMClient, LLMToolCall, LLMTurnResponse, build_llm_client
from app.observability import TurnObserver, TurnTrace
from app.reference_layer import (
    _DAILYMED_BASE,
    _MEDLINEPLUS_BASE,
    _build_dailymed_query,
    _build_medlineplus_query,
    _fetch_dailymed_note,
    _fetch_medlineplus_note,
    _medications_recommended,
)
from app.schemas import (
    CheckAllergyConflictOutput,
    CompareSignoutToChartOutput,
    ConditionFact,
    DuplicatePatientWarning,
    GetPatientSnapshotOutput,
    GetRecentEncountersOutput,
    GetRecentObservationsOutput,
    ObservationFact,
    SummarizeShiftEventsOutput,
    ToolFailure,
)
from app.sensitivity import filter_encounters_by_sensitivity, has_high_sensitivity_clearance
from app.tools import (
    check_allergy_conflict,
    collect_source_resources,
    compare_signout_to_chart,
    get_patient_snapshot,
    get_uploaded_documents,
    summarize_shift_events,
)
from app.verification import ToolCallRecord, verify_response
from app.verification import _check_domain_constraint, _every_mention_is_an_avoidance, _format_confidence_note, _without_already_noted  # noqa: F401
from app.verification import _mentions_conflict_near_medication  # noqa: F401 -- see
# test_bare_allergy_text_matching_medication_name_not_treated_as_self_acknowledged's
# docstring for why this one test reaches past the public verify_response()
# entry point the rest of this file uses.
from app.verification import _is_grounded  # noqa: F401 -- same reasoning, see
# test_uploaded_document_composed_lab_value_is_grounded's docstring: this one
# needs the exact offline repro that proved the composition bug live, not
# just the full verify_response() pipeline around it.
from evals import fixtures as f
from evals.golden_facts import GOLDEN_FACTS
from evals.parallel import log_job_timing
from evals.parallel_tests import TESTS as _PARALLEL_RUNNER_TESTS
from evals.citation_cards_tests import TESTS as _CITATION_CARDS_TESTS
from evals.extraction_tests import TESTS as _EXTRACTION_TESTS
from evals.preview_authz_tests import TESTS as _PREVIEW_AUTHZ_TESTS
from evals.call_site_tests import TESTS as _CALL_SITE_TESTS
from evals.disclosure_filter_tests import TESTS as _DISCLOSURE_FILTER_TESTS
from evals.citation_display_tests import TESTS as _CITATION_DISPLAY_TESTS
from evals.allergy_retrieval_tests import TESTS as _ALLERGY_RETRIEVAL_TESTS
from evals.security_hardening_tests import TESTS as _SECURITY_HARDENING_TESTS
from evals.verification_notices_tests import TESTS as _VERIFICATION_NOTICES_TESTS
from evals.truncation_tests import TESTS as _TRUNCATION_TESTS
from evals.schema_contract_tests import TESTS as _SCHEMA_CONTRACT_TESTS

# W2 Checklist item 8: LLM-free tests for the new pure-logic modules
# Checklist items 2-5 added (chunking, plausibility, bbox resolution, RRF
# fusion, citation translation) -- none of this had any test coverage
# before this pass.
from app.agent import ChartGatherResult
from app.graph.citations import (
    Citation,
    _document_fact_stated_in_response,
    chart_citations,
    document_citations,
    filter_citations_to_stated_facts,
    guideline_citations,
    translate_citations,
)
from app.graph.build import GraphTurnResult
from app.graph.state import CHART_DATA_WORKER
from app.agent import ChatTurnResult
from app.graph.supervisor import (  # noqa: F401 -- see
    _extraction_field_label_value_confidence,
    _extraction_field_values,
    route_targets,
    synthesize_node,
)
# test_synthesize_node_grounds_same_turn_extracted_facts' docstring for why
# this test reaches past synthesize_node's public boundary.
from evals.cases import _check_ambiguous_query, _render_document_image  # noqa: F401 -- see
# test_check_ambiguous_query_handles_real_phrasing_variants' docstring for
# why this test reaches past the public EvalCase.check boundary.
from evals.cases import _check_no_citation_misattribution  # noqa: F401 -- see
# test_check_no_citation_misattribution_rejects_shared_field_only_citation's
# docstring for why this test reaches past the public EvalCase.check boundary.
from evals.document_cleanup import flush_cleanup, register_for_cleanup
from evals.prune_upload_fixture import (
    AI_EXTRACTED_CATEGORY_ID,
    AI_EXTRACTED_CATEGORY_NAME,
    PRODUCTION_DB_HOST,
    SOURCE_DOCUMENT_CATEGORY_ID,
    SOURCE_DOCUMENT_CATEGORY_NAME,
    DbConfig,
    PruneError,
    _validate_host,
    find_prunable_documents,
    prune,
    resolve_fixture_pid,
)
from app.ingestion.bbox import resolve_bbox
from app.ingestion.ocr import OcrPage, OcrResult, OcrWord, rasterize_page
from app.ingestion.plausibility import apply_plausibility_check, is_implausible
from app.ingestion.schemas import (
    DemographicsFact,
    DocumentCitation,
    ExtractedField,
    ExtractionResult,
    IntakeFormExtraction,
    LabPdfExtraction,
    LabResultFact,
)
from app.ingestion.storage import (
    MAX_UPLOADED_DOCUMENTS_RETURNED,
    DuplicateDocumentError,
    compute_document_ids,
    dedupe_key,
    get_source_document_bytes,
    list_extracted_documents,
    store_extraction,
)
from app.retrieval.chunking import GuidelineChunk, load_corpus
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.query import build_safe_query
from app.retrieval.retriever import GuidelineRetriever
from app.retrieval.types import RetrievedEvidence
from app.schemas import GetUploadedDocumentsOutput, UploadedDocumentFactSummary, UploadedDocumentSummary
from app.verification import _human_readable_field_labels, mentions_uploaded_document_disclosure


def _contains_all(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return all(term.lower() in lowered for term in terms)


# --- (a) get_patient_snapshot, tested directly against real FHIR data ------


def test_get_patient_snapshot_pid1(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: get_patient_snapshot must never silently drop a real
    condition/medication/allergy from the chart -- verification's source
    attribution is only as trustworthy as this completeness guarantee."""
    result = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(result, GetPatientSnapshotOutput):
        return False, f"expected a snapshot, got a tool failure: {result}"
    facts = GOLDEN_FACTS["pid1_alice"]
    all_text = " ".join(c.text for c in result.conditions) + " " + \
        " ".join(m.text for m in result.medications) + " " + \
        " ".join(a.text for a in result.allergies)
    expected = facts["conditions"] + facts["medications"] + facts["allergies"]
    if not _contains_all(all_text, expected):
        return False, f"snapshot is missing one of {expected}, got: {all_text!r}"
    return True, "snapshot contains all golden facts for pid1"


def test_get_patient_snapshot_pid2(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: same completeness guarantee as pid1 above, verified
    against a second, independent real patient so the property isn't
    proven only for one chart's particular shape."""
    result = get_patient_snapshot(fhir, {"patient_id": f.PID2_BOB}, [])
    if not isinstance(result, GetPatientSnapshotOutput):
        return False, f"expected a snapshot, got a tool failure: {result}"
    facts = GOLDEN_FACTS["pid2_bob"]
    all_text = " ".join(c.text for c in result.conditions) + " " + \
        " ".join(m.text for m in result.medications) + " " + \
        " ".join(a.text for a in result.allergies)
    expected = facts["conditions"] + facts["medications"] + facts["allergies"]
    if not _contains_all(all_text, expected):
        return False, f"snapshot is missing one of {expected}, got: {all_text!r}"
    return True, "snapshot contains all golden facts for pid2"


# --- (b) check_allergy_conflict, tested directly ---------------------------


def test_check_allergy_conflict_positive(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a real, documented allergy conflict must always be
    detected -- a false negative here is the single most dangerous failure
    mode this tool has."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "penicillin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "pid1 has a documented penicillin allergy; expected conflict_found=True"
    return True, "known conflict (pid1 + penicillin) correctly flagged"


def test_check_allergy_conflict_negative(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: the complementary guarantee to the positive case above --
    a medication with no real documented conflict must never be flagged as
    one (no false positives eroding trust in the real flags)."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if result.conflict_found:
        return False, "pid1's only allergy is penicillin; expected conflict_found=False for metformin"
    return True, "known non-conflict (pid1 + metformin) correctly not flagged"


def test_check_allergy_conflict_cross_reactive(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a documented allergy must also flag every drug in the same
    curated cross-reactive class, not just direct/substring name matches.
    Phase 5 (app/clinical_reference.py): pid1's documented penicillin
    allergy must also flag amoxicillin -- a different drug in the same
    curated 'penicillins' class -- even though "amoxicillin" never appears
    in the allergy text itself, so this can only pass via the cross-
    reactivity table, not the pre-existing direct/substring match."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "amoxicillin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "pid1's penicillin allergy should cross-flag amoxicillin (same drug class); got conflict_found=False"
    if result.cross_reactive_class != "penicillins":
        return False, f"expected cross_reactive_class='penicillins', got {result.cross_reactive_class!r}"
    return True, "amoxicillin correctly flagged via the curated penicillin cross-reactivity table"


def test_check_allergy_conflict_cross_reactive_scoped_to_curated_classes(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: the complementary guarantee to the cross-reactive case
    above -- the table is a scoped stand-in, not a general drug-class
    knowledge base, so a medication in no curated class, and not a
    direct/substring match, must still come back as no conflict rather
    than the table silently over-firing."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if result.conflict_found:
        return False, "metformin is in no curated drug class and isn't a substring match; expected conflict_found=False"
    if result.cross_reactive_class is not None:
        return False, f"expected cross_reactive_class=None for a non-conflict, got {result.cross_reactive_class!r}"
    return True, "medication outside every curated class correctly reported no conflict"


# --- (b2) FHIR caching (2026-09-19 caching design) --------------------------


class _CountingFhirClient(FhirClient):
    """Wraps a real FhirClient and counts real search()/search_fresh() calls
    per resource type -- lets a test assert "the cache actually engaged"
    against a deterministic signal (a real HTTP call count) rather than
    wall-clock time, which is too noisy in CI/local dev to gate a test on.
    Does not call FhirClient.__init__ -- every method delegates, nothing
    here needs self._settings/_tokens/_client."""

    def __init__(self, delegate: FhirClient):
        self._delegate = delegate
        self.search_calls: dict[str, int] = {}

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        self.search_calls[resource_type] = self.search_calls.get(resource_type, 0) + 1
        return self._delegate.search(resource_type, params)

    def search_fresh(self, resource_type: str, params: dict[str, str]) -> dict:
        self.search_calls[resource_type] = self.search_calls.get(resource_type, 0) + 1
        return self._delegate.search_fresh(resource_type, params)

    def read(self, resource_type: str, resource_id: str) -> dict:
        return self._delegate.read(resource_type, resource_id)

    def get_standard_api(self, path: str) -> dict | list:
        return self._delegate.get_standard_api(path)


def test_caching_reduces_redundant_fhir_calls(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: the conversation-scoped cache must always eliminate a
    real, redundant FHIR call for the same resource within one
    conversation. Case A (2026-09-19 caching design): proves this is
    actually true -- measured, not assumed. Calls get_patient_snapshot for
    the same real patient twice,
    sharing one CachingFhirClient/FhirResourceCache the same way one
    conversation's turns share app/main.py's per-conversation cache.

    Gates pass/fail on the deterministic signal (a real HTTP call count per
    resource type), not wall-clock time -- elapsed time is logged as
    supporting evidence only (PERFORMANCE_BASELINE.md's own "state it
    honestly" convention), since real timing is too noisy locally/in CI to
    gate a test on, even though it's what actually motivated this design."""
    counting = _CountingFhirClient(fhir)
    cached = CachingFhirClient(counting, FhirResourceCache())

    t0 = time.monotonic()
    first = get_patient_snapshot(cached, {"patient_id": f.PID1_ALICE}, [])
    t1 = time.monotonic()
    second = get_patient_snapshot(cached, {"patient_id": f.PID1_ALICE}, [])
    t2 = time.monotonic()

    if not isinstance(first, GetPatientSnapshotOutput):
        return False, f"expected a snapshot on the first call, got a tool failure: {first}"
    if not isinstance(second, GetPatientSnapshotOutput):
        return False, f"expected a snapshot on the second (cached) call, got a tool failure: {second}"

    for resource_type in ("Condition", "MedicationRequest", "AllergyIntolerance"):
        calls = counting.search_calls.get(resource_type, 0)
        if calls != 1:
            return False, (
                f"expected exactly 1 real {resource_type} search across both calls (the second "
                f"should be served from cache), got {calls}"
            )

    first_allergies = [a.text for a in first.allergies]
    second_allergies = [a.text for a in second.allergies]
    if first_allergies != second_allergies:
        return False, "cached snapshot's allergy data diverged from the first real fetch -- cache corrupted the result"

    return True, (
        "second call made 0 additional real Condition/MedicationRequest/AllergyIntolerance fetches "
        f"(cache hit); real elapsed: first={t1 - t0:.3f}s second={t2 - t1:.3f}s"
    )


_SLOW_FIXTURE_DELAY_S = 0.3


class _SlowFhirClient(FhirClient):
    """Every call sleeps a fixed, known duration before returning canned
    data -- lets a test prove genuine concurrent execution deterministically
    (sequential must take at least 5x the delay; concurrent should take
    roughly 1x), with no dependency on real network timing variance. Does
    not call FhirClient.__init__ -- nothing here needs self._settings/
    _tokens/_client."""

    def __init__(self, delay_s: float = _SLOW_FIXTURE_DELAY_S):
        self._delay_s = delay_s

    def read(self, resource_type: str, resource_id: str) -> dict:
        time.sleep(self._delay_s)
        return {
            "resourceType": "Patient",
            "id": resource_id,
            "name": [{"family": "Slowtest", "given": ["Case"]}],
            "birthDate": "1990-01-01",
        }

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        time.sleep(self._delay_s)
        return {"resourceType": "Bundle", "type": "searchset", "entry": []}


def test_parallelizing_snapshot_fetches_reduces_wall_clock(fhir: FhirClient) -> tuple[bool, str]:
    """Regression: guards against a future refactor silently turning
    get_patient_snapshot's concurrency back into sequential execution while
    still looking correct. Case (2026-09-19 parallelization design): proves
    the 5 FHIR calls (4 independent + 1 -- the duplicate search -- that
    depends on the Patient read) actually execute concurrently, not just
    that the code compiles with a ThreadPoolExecutor in it.

    Gated on a deterministic signal: a fixed, known per-call delay (not real
    network timing, which is too noisy to assert a tight bound on). 5 calls
    sequential would need >= 5 * 0.3s = 1.5s; concurrent execution with the
    4+1 dependency shape needs roughly 2 * 0.3s = 0.6s in the worst case
    (the Patient read, then the dependent duplicate search, with the three
    independent searches overlapping either half) -- asserting well under
    the sequential floor (1.0s) proves real concurrency with a wide,
    non-flaky margin either way.

    Real-network timing (against the actual local OpenEMR instance) is
    logged separately as supporting evidence, not gated on -- same
    convention as test_caching_reduces_redundant_fhir_calls."""
    slow = _SlowFhirClient()
    t0 = time.monotonic()
    result = get_patient_snapshot(slow, {"patient_id": "case-parallel-patient"}, [])
    elapsed = time.monotonic() - t0

    if not isinstance(result, GetPatientSnapshotOutput):
        return False, f"expected a snapshot, got a tool failure: {result}"
    if elapsed >= 1.0:
        return False, (
            f"expected concurrent execution to finish well under the 1.5s sequential floor "
            f"(5 calls x {_SLOW_FIXTURE_DELAY_S}s), took {elapsed:.3f}s -- looks sequential, not concurrent"
        )

    # Real-network supporting evidence, not gated on (informational only,
    # PERFORMANCE_BASELINE.md's own "state it honestly" convention).
    t0 = time.monotonic()
    real_result = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    real_elapsed = time.monotonic() - t0
    if not isinstance(real_result, GetPatientSnapshotOutput):
        return False, f"real-network snapshot fetch failed unexpectedly: {real_result}"

    return True, (
        f"deterministic fixture: 5 calls x {_SLOW_FIXTURE_DELAY_S}s delay finished in {elapsed:.3f}s "
        f"(sequential floor would be {5 * _SLOW_FIXTURE_DELAY_S:.1f}s); "
        f"real-network snapshot fetch (supporting evidence only): {real_elapsed:.3f}s"
    )


# --- (b3) FHIR call timeout/retry (2026-09-19 timeout/retry design) --------
#
# Unlike _SlowFhirClient/_ChangingAllergyFhirClient above, which override
# FhirClient.search()/read() directly, these two cases need to drive
# FhirClient._get_with_retry's actual retry/timeout logic (app/fhir_client.py)
# itself, not bypass it -- so instead they inject a fake object in place of
# the real httpx.Client (FhirClient.__init__'s existing `client` parameter,
# already used for real-httpx.Client injection, never exercised with a fake
# one before this).

_TIMEOUT_FIXTURE_DELAY_S = 0.1


class _FlakyTransport:
    """Stands in for httpx.Client. Each queued behavior is either an
    httpx.Response to return or an exception instance to raise; .get() pops
    one per call (a small, real sleep before raising a timeout, so an
    elapsed-time assertion means something) and records the `timeout` kwarg
    it was called with, so a test can confirm the retry actually used a
    shorter timeout than the original attempt (Step 3's "don't double the
    wait" rule), not a second fresh one. Single-call tools only (check_
    allergy_conflict, not get_patient_snapshot) -- .get() isn't safe for
    concurrent callers to share, and doesn't need to be: Step 4's three
    hard-bypass call sites are exactly the single-call tools this fixture
    targets."""

    def __init__(self, behaviors: list[httpx.Response | Exception]):
        self._behaviors = list(behaviors)
        self.call_timeouts: list[float | None] = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.call_timeouts.append(timeout)
        if not self._behaviors:
            raise AssertionError("fixture exhausted -- more GETs happened than expected (retried more than once?)")
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            time.sleep(_TIMEOUT_FIXTURE_DELAY_S)
            raise behavior
        return behavior


def test_fhir_timeout_retries_once_then_fails_honestly(fhir: FhirClient) -> tuple[bool, str]:
    """Boundary case (2026-09-19 timeout/retry design): a FHIR call that
    times out on both its first attempt AND its single retry must give up
    and surface an honest ToolFailure -- never hang, never fabricate data,
    never retry a second time. Exercised against check_allergy_conflict, one
    of Step 4's three hard-bypass call sites, not get_patient_snapshot: this
    proves the hard-bypass path specifically inherits the same timeout/retry
    behavior as every cached path, and a single sequential call keeps the
    fixture's behavior queue deterministic (no concurrent .get() callers).

    Gated on a deterministic signal (elapsed wall-clock time under a
    generous ceiling), same convention as
    test_parallelizing_snapshot_fetches_reduces_wall_clock above -- a real
    unbounded hang would take far longer than this ceiling, so this proves
    "didn't hang" without asserting exact timing."""
    settings = get_settings()
    transport = _FlakyTransport(
        [httpx.TimeoutException("simulated timeout"), httpx.TimeoutException("simulated timeout")]
    )
    flaky_fhir = FhirClient(settings, OAuthTokenProvider(settings), client=transport)

    t0 = time.monotonic()
    result = check_allergy_conflict(
        flaky_fhir, {"patient_id": "case-timeout-patient", "medication_name": "penicillin"}, []
    )
    elapsed = time.monotonic() - t0

    if elapsed >= 3.0:
        return False, (
            f"expected a bounded failure well under 3s (2 x {_TIMEOUT_FIXTURE_DELAY_S}s fixture "
            f"delay), took {elapsed:.3f}s -- looks like it hung or retried more than once"
        )
    if not isinstance(result, ToolFailure):
        return False, f"expected an honest ToolFailure after both attempts time out, got: {result!r}"
    if result.detail_code != "timeout":
        return False, f"expected detail_code='timeout', got {result.detail_code!r}"
    if len(transport.call_timeouts) != 2:
        return False, (
            f"expected exactly 2 GET attempts (1 original + 1 retry, no more), got "
            f"{len(transport.call_timeouts)}"
        )
    if transport.call_timeouts[0] is not None:
        return False, (
            f"the first attempt should use the client's configured default timeout (no explicit "
            f"override), got timeout={transport.call_timeouts[0]!r}"
        )
    expected_retry_timeout = settings.request_timeout_seconds / 2
    if transport.call_timeouts[1] != expected_retry_timeout:
        return False, (
            f"expected the retry to use half the configured timeout ({expected_retry_timeout}s), "
            f"got {transport.call_timeouts[1]!r}"
        )
    return True, (
        f"both attempts timed out; gave up honestly after exactly 1 retry (retry timeout="
        f"{transport.call_timeouts[1]}s, half of {settings.request_timeout_seconds}s), in "
        f"{elapsed:.3f}s -- no hang, no fabricated data"
    )


def test_fhir_retry_then_succeed_returns_real_data(fhir: FhirClient) -> tuple[bool, str]:
    """Boundary case (2026-09-19 timeout/retry design): the other edge of
    the retry state space from the give-up boundary test above -- a call
    that fails once (simulated timeout) but succeeds on its single retry
    must return the real, correct data from that retry -- proving the retry
    logic actually works and recovers, not just that the give-up path
    (tested above) is correct. Same hard-bypass call site
    (check_allergy_conflict) as the boundary case above, for the same
    Step-4-coverage reason."""
    settings = get_settings()
    real_penicillin_allergy_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "AllergyIntolerance",
                    "id": "fixture-retry-penicillin-allergy",
                    "code": {"coding": [{"display": "penicillin"}], "text": "penicillin"},
                }
            }
        ],
    }
    transport = _FlakyTransport(
        [httpx.TimeoutException("simulated timeout"), httpx.Response(200, json=real_penicillin_allergy_bundle)]
    )
    flaky_fhir = FhirClient(settings, OAuthTokenProvider(settings), client=transport)

    result = check_allergy_conflict(
        flaky_fhir, {"patient_id": "case-retry-success-patient", "medication_name": "penicillin"}, []
    )

    if len(transport.call_timeouts) != 2:
        return False, f"expected exactly 2 GET attempts (1 failed + 1 successful retry), got {len(transport.call_timeouts)}"
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a real conflict result from the successful retry, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "the retry's real penicillin allergy data should have flagged a conflict, got conflict_found=False"
    if result.matched_allergy_text != "penicillin":
        return False, f"expected the retry's real allergy text 'penicillin', got {result.matched_allergy_text!r}"
    return True, "a call that timed out once but succeeded on its single retry returned real, correct data"


# --- (b4) app/observability.py: resident identity on the trace (2026-09-19
# audit-logging design, ARCHITECTURE.md 7.11) -----------------------------


def test_turn_observer_logs_resident_identity_alongside_patient(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the gap found while writing ARCHITECTURE.md
    §7.11: traces recorded which patient was touched (patient_id) but not
    which resident did it -- session.resident_username (app/main.py) was
    never threaded into TurnObserver.start_turn(). Asserts against the real
    local structured log line _log() actually emits (captured via a
    temporary logging.Handler, no mocking of app code itself) rather than
    the Langfuse span's own server-side state, which would need a live
    round-trip and async ingestion delay to verify -- disproportionate
    infrastructure for this fix; that side was verified live, separately,
    by querying the real local Langfuse ClickHouse instance directly (see
    this commit's own message), not by this automated test.

    Confirms both fields land in the SAME log line together, not just that
    each exists somewhere -- the whole point of the fix is answering "which
    resident touched which patient" from one record, not two separately
    plausible ones."""
    settings = get_settings()
    observer = TurnObserver(settings)

    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CapturingHandler()
    target_logger = logging.getLogger("clinical_copilot")
    target_logger.addHandler(handler)
    try:
        observer.start_turn(
            "case-observability-correlation-id",
            "case message",
            "case-observability-patient-id",
            "case-observability-resident",
        )
    finally:
        target_logger.removeHandler(handler)

    turn_started_lines = [json.loads(line) for line in captured if '"event": "turn_started"' in line]
    if len(turn_started_lines) != 1:
        return False, f"expected exactly 1 turn_started log line, got {len(turn_started_lines)}: {captured!r}"
    fields = turn_started_lines[0]

    # 2026-09-25: identifiers are logged as salted hashes (app/log_redaction.py) -- still both present in the SAME
    # line, and still correlatable (the same value always maps to the same token), but never raw.
    from app.log_redaction import hash_identifier

    if fields.get("patient_id") != hash_identifier("case-observability-patient-id"):
        return False, f"expected the HASHED patient_id in the log line, got {fields.get('patient_id')!r}"
    if fields.get("resident_username") != hash_identifier("case-observability-resident"):
        return False, (
            f"expected the HASHED resident_username in the SAME log line as patient_id, got "
            f"{fields.get('resident_username')!r} -- this is the exact gap ARCHITECTURE.md 7.11 named"
        )
    if "case-observability-patient-id" in captured[0] or "case-observability-resident" in "".join(captured):
        return False, "a raw identifier reached the log line"
    if fields.get("correlation_id") != "case-observability-correlation-id":
        return False, f"expected correlation_id in the log line too, got {fields.get('correlation_id')!r}"

    return True, "hashed patient_id, hashed resident_username, and correlation_id land together in the same turn_started log line"


def test_log_routing_decision_logs_workers_and_rationale(fhir: FhirClient) -> tuple[bool, str]:
    """Gate 5 audit gap: app/graph/supervisor.py's route_node generated a
    model rationale for its worker choice, but never logged it as its own
    event -- it only ever reached the persisted trace buried inside the
    routing LLM call's raw tool-call payload, never a first-class,
    directly queryable field. Confirms TurnTrace.log_routing_decision
    emits a real "routing_decision" log line naming exactly which workers
    were chosen and why, via the same real local structured log _log()
    actually emits (captured via a temporary logging.Handler, no mocking
    of app code), the same pattern
    test_turn_observer_logs_resident_identity_alongside_patient already
    uses."""
    trace = TurnTrace(client=None, span=None, correlation_id="case-routing-correlation-id")

    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CapturingHandler()
    target_logger = logging.getLogger("clinical_copilot")
    target_logger.addHandler(handler)
    try:
        trace.log_routing_decision(
            workers=["chart_data_worker", "evidence_retriever"],
            rationale="the question asks about both the chart and general guideline criteria",
        )
    finally:
        target_logger.removeHandler(handler)

    routing_lines = [json.loads(line) for line in captured if '"event": "routing_decision"' in line]
    if len(routing_lines) != 1:
        return False, f"expected exactly 1 routing_decision log line, got {len(routing_lines)}: {captured!r}"
    fields = routing_lines[0]

    if fields.get("workers") != ["chart_data_worker", "evidence_retriever"]:
        return False, f"expected the exact workers list in the log line, got {fields.get('workers')!r}"
    # 2026-09-25: the model's free-text rationale is not written to journald (it can restate patient details);
    # its length is, and the rationale still reaches the response and the trace.
    if "rationale" in fields or "guideline criteria" in "".join(captured):
        return False, f"the free-text rationale must not be logged, got {fields!r}"
    if fields.get("rationale_length") != len("the question asks about both the chart and general guideline criteria"):
        return False, f"expected rationale_length in the log line, got {fields!r}"
    if fields.get("correlation_id") != "case-routing-correlation-id":
        return False, f"expected correlation_id in the routing_decision log line too, got {fields.get('correlation_id')!r}"

    return True, "routing_decision is logged as its own first-class event, naming the exact workers chosen (rationale length only, never its text)"


# --- (b5) app/llm_client.py: the provider factory (2026-09-19 LLM provider
# abstraction design) -----------------------------------------------------


def test_build_llm_client_defaults_to_anthropic(fhir: FhirClient) -> tuple[bool, str]:
    """Regression: the behavior-preservation check for the provider-
    abstraction refactor itself. build_llm_client() must resolve to
    AnthropicLLMClient -- the only validated provider (app/llm_client.py's
    own module docstring) -- when LLM_PROVIDER is unset, matching
    Settings.llm_provider's own "anthropic" default (app/config.py). Proves
    the real, live get_settings() this whole app/eval suite already runs under still
    resolves to the same concrete class ClinicalCopilotAgent used before
    the refactor, not just that SOME settings object could."""
    settings = get_settings()
    if settings.llm_provider != "anthropic":
        return False, (
            f"test precondition failed: expected the real local .env's LLM_PROVIDER to be "
            f"unset/'anthropic' for this check to mean anything, got {settings.llm_provider!r}"
        )
    client = build_llm_client(settings)
    if not isinstance(client, AnthropicLLMClient):
        return False, f"expected AnthropicLLMClient for the default/unset provider, got {type(client).__name__}"
    return True, "build_llm_client(get_settings()) resolves to AnthropicLLMClient when LLM_PROVIDER is unset"


class _ChangingAllergyFhirClient(FhirClient):
    """Returns an AllergyIntolerance Bundle with no penicillin allergy on
    its first real call, then a materially different Bundle WITH a new
    penicillin allergy on every call after -- everything else falls back to
    the real, live FHIR call. Lets Case B prove a hard-bypass call site
    sees the change immediately, in contrast to an ordinary cached call
    site, which should not."""

    def __init__(self, settings, tokens):
        super().__init__(settings, tokens)
        self._allergy_calls = 0

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        if resource_type != "AllergyIntolerance":
            return super().search(resource_type, params)
        self._allergy_calls += 1
        allergies = [] if self._allergy_calls == 1 else [
            {
                "resourceType": "AllergyIntolerance",
                "id": "fixture-new-penicillin-allergy",
                "code": {"coding": [{"display": "penicillin"}], "text": "penicillin"},
            }
        ]
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": a} for a in allergies],
        }


def test_never_cache_list_bypasses_stale_allergy_data(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: check_allergy_conflict must never rely on stale cached
    allergy data -- its hard cache-bypass must always be engaged. Case B
    (2026-09-19 caching design): proves check_allergy_conflict's hard
    cache-bypass (search_fresh(), app/tools.py) is actually wired in, not
    just present as unused code. Populates the conversation cache with
    a no-conflict allergy list via an ordinary cached search (mirroring
    get_patient_snapshot's own call), confirms an ordinary second call
    still sees that stale list (so the contrast below is meaningful, not a
    no-op), then proves check_allergy_conflict's own fetch reflects the
    newly-added penicillin allergy instead -- if the hard bypass weren't
    wired in, this would wrongly return conflict_found=False from the
    stale cached list, exactly the failure mode the caching design's
    hard-exception list exists to prevent."""
    settings = get_settings()
    changing = _ChangingAllergyFhirClient(settings, OAuthTokenProvider(settings))
    cached = CachingFhirClient(changing, FhirResourceCache())

    initial = cached.search("AllergyIntolerance", {"patient": f.PID1_ALICE})
    if len(bundle_entries(initial)) != 0:
        return False, "fixture setup error: expected the first real fetch to return no allergies"

    still_cached = cached.search("AllergyIntolerance", {"patient": f.PID1_ALICE})
    if len(bundle_entries(still_cached)) != 0:
        return False, (
            "expected an ordinary (non-bypass) second call to still be served the stale cached "
            "empty list -- if this fails, the contrast below proves nothing"
        )

    result = check_allergy_conflict(cached, {"patient_id": f.PID1_ALICE, "medication_name": "penicillin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, (
            "check_allergy_conflict did not see the newly-added penicillin allergy -- its hard "
            "cache-bypass is not actually wired in, it served stale cached data instead"
        )
    return True, "check_allergy_conflict correctly bypassed the cache and saw the newly-added allergy"


# --- (c) verification.py's stripping logic, no LLM involved ----------------


def test_verification_strips_ungrounded_claim(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a claim not grounded in real tool output must always be
    stripped before it reaches the resident -- claims must always cite a
    source (ARCHITECTURE.md §3.1)."""
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    fake_draft = (
        "Alice Testpatient has type 2 diabetes and hypertension, and is on Metformin "
        "and Lisinopril. She is also on Warfarin 5 mg daily, which was adjusted last week."
    )
    outcome = verify_response(fake_draft, [record], f.PID1_ALICE, fhir)

    if "warfarin" not in [c.lower() for c in outcome.flagged_claims]:
        return False, f"expected 'warfarin' to be flagged, got flagged_claims={outcome.flagged_claims}"
    if "warfarin" in outcome.final_response.lower().split("[verification note")[0].lower():
        return False, "the fabricated Warfarin claim survived into the final response body"
    if outcome.passed_source_attribution:
        return False, "passed_source_attribution should be False when a claim was stripped"
    return True, "fabricated Warfarin claim (not in real tool output) correctly flagged and stripped"


def test_verification_passes_grounded_claim(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: the complementary guarantee to the stripping case above --
    a claim that IS grounded in real tool output must always survive
    untouched. Verification must not over-strip as a side effect of
    catching fabrications."""
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    grounded_draft = (
        "Alice Testpatient has type 2 diabetes and hypertension. She is on Metformin "
        "500 mg and Lisinopril 10 mg."
    )
    outcome = verify_response(grounded_draft, [record], f.PID1_ALICE, fhir)

    if outcome.flagged_claims:
        return False, f"expected no flagged claims for an all-grounded draft, got {outcome.flagged_claims}"
    if not outcome.passed_source_attribution:
        return False, "passed_source_attribution should be True when nothing was stripped"
    if grounded_draft not in outcome.final_response:
        return False, "the grounded draft text should pass through unmodified (verification notes may be appended)"
    return True, "an all-grounded draft passed through untouched"


def test_lab_value_grounding_tolerates_formatting(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a bug caught and fixed live during compare_
    signout_to_chart's build (2026-09-18) -- at the time it was only
    verified with a one-off interactive check, not a permanent test; this
    is that missing test. _LAB_VALUE_RE (app/verification.py) extracts a
    lab/vital value from the model's own drafted prose, but the model
    doesn't always format it identically to how
    ObservationFact.value/_observation_value_text() (app/tools.py) stored
    it -- e.g. the model writing "38.9°C" (the real degree symbol, no
    space) against a stored "38.9 C" (a space, no degree symbol). Without
    _is_grounded()'s normalization fallback, this exact mismatch strips a
    true, tool-sourced value from the response.

    Confirms both directions that matter: the equivalent-but-differently-
    formatted value IS treated as grounded (the bug that was actually
    found), and -- the control that proves this isn't just loosened
    grounding in general -- a genuinely different value is NOT swept in as
    a false match by the same normalization.
    """
    observations = GetRecentObservationsOutput(
        patient_id=f.PID1_ALICE,
        observations=[
            ObservationFact(
                text="Temperature",
                value="38.9 C",
                status="final",
                effective_datetime="2026-09-18T01:00:00Z",
                source_resource="Observation/fixture-temp-1",
            )
        ],
    )
    record = ToolCallRecord(tool_name="get_recent_observations", patient_id=f.PID1_ALICE, output=observations)

    matching_draft = "Her most recent temperature was 38.9°C, which is febrile."
    outcome = verify_response(matching_draft, [record], f.PID1_ALICE, fhir)
    if any("38.9" in c for c in outcome.flagged_claims):
        return False, (
            "the real temperature (38.9 C, as stored) was incorrectly stripped when the model "
            f"wrote it as 38.9°C -- got flagged_claims={outcome.flagged_claims}"
        )
    if "38.9" not in outcome.final_response:
        return False, "the real, matching temperature value did not survive into the final response"

    wrong_draft = "Her most recent temperature was 39.5°C, which is febrile."
    wrong_outcome = verify_response(wrong_draft, [record], f.PID1_ALICE, fhir)
    if not any("39.5" in c for c in wrong_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely different temperature (39.5°C, not in the tool "
            f"output) should still be flagged as ungrounded, got flagged_claims={wrong_outcome.flagged_claims}"
        )

    return True, (
        "a real lab value survives despite cosmetic formatting differences, and a genuinely "
        "different value is still caught"
    )


def test_uploaded_document_composed_lab_value_is_grounded(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the atomic-field-splitting gap found live
    2026-09-23 (production /chat stripping a real uploaded-document lab
    value, "4.4 mEq/L", that an identical staging turn let through).
    app/tools.py's _flatten_extraction_facts splits a lab fact's value
    ("4.4") and unit ("mEq/L") into separate atomic
    UploadedDocumentFactSummary entries -- unlike _ground_observations'
    obs.text (the chart path test_lab_value_grounding_tolerates_formatting
    above covers), which is already one descriptive string containing
    both. Before _is_grounded()'s composition check, a combined-format
    candidate like "4.4 mEq/L" -- exactly how a model naturally writes a
    lab value in prose -- could never match either atomic grounded entry
    on its own. This is the exact offline repro proven live before the fix
    existed: _is_grounded('4.4 mEq/L', {atomic pieces}) returned False.

    Also covers the table-cell-separated shape (_LAB_VALUE_RE's own fix,
    see its docstring in app/verification.py): staging's earlier "pass" on
    this same bug was a false negative, not real coverage -- the model's
    markdown-table formatting made the regex extract zero candidates, so
    grounding was never even checked. "4.4 | mEq/L" (how the widened regex
    now extracts a table cell pair) must ground exactly like the prose
    form. Both come with a genuinely-wrong-value control, proving the
    composition check doesn't get sloppy and ground anything with a
    matching unit regardless of the number.
    """
    grounded = {"potassium", "4.4", "mEq/L".lower(), "3.5-5.0", "normal"}

    if not _is_grounded("4.4 mEq/L", grounded):
        return False, (
            "composed candidate '4.4 mEq/L' should be grounded when both atomic pieces "
            "(the value and the unit) are independently in the grounded vocabulary"
        )
    if not _is_grounded("4.4 | mEq/L", grounded):
        return False, (
            "the table-cell-separated form '4.4 | mEq/L' (as _LAB_VALUE_RE now extracts a "
            "markdown table cell pair) should ground the same way as the prose form"
        )
    if _is_grounded("5.9 mEq/L", grounded):
        return False, (
            "control failed: a genuinely different value (5.9, not in the grounded "
            "vocabulary) should not be swept in as grounded just because its unit matches"
        )
    if _is_grounded("5.9 | mEq/L", grounded):
        return False, "control failed: the same wrong-value check must also hold in table-cell-separated form"

    return True, (
        "a combined value+unit candidate is grounded via composition of its independently-"
        "grounded atomic pieces in both prose and table-cell form, and a genuinely wrong "
        "value is still caught in both"
    )


def test_uploaded_document_lab_value_survives_prose_and_table_formatting(fhir: FhirClient) -> tuple[bool, str]:
    """Sibling to test_lab_value_grounding_tolerates_formatting above, for
    the uploaded-document path instead of the chart-observation path --
    these are structurally different (see
    test_uploaded_document_composed_lab_value_is_grounded's docstring), so
    chart-path coverage alone left this path completely untested despite
    get_uploaded_documents having shipped in Week 2. Exercises the real
    verify_response() entry point end to end, not just _is_grounded()
    directly, so a regression anywhere in the pipeline (not only the
    composition check itself) would be caught here too."""
    facts = [
        UploadedDocumentFactSummary(field_name="test_name", value="Potassium", confidence="high", source_resource="document:x#test_name"),
        UploadedDocumentFactSummary(field_name="value", value="4.4", confidence="high", source_resource="document:x#value"),
        UploadedDocumentFactSummary(field_name="unit", value="mEq/L", confidence="high", source_resource="document:x#unit"),
    ]
    output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[UploadedDocumentSummary(doc_id="x", doc_type="lab_pdf", facts=facts, ocr_avg_confidence=99.0, upload_rank=0)],
    )
    records = [ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)]

    prose_draft = "From an uploaded scanned document (pending review), her potassium was 4.4 mEq/L."
    prose_outcome = verify_response(prose_draft, records, "p1", fhir)
    if any("4.4" in c for c in prose_outcome.flagged_claims):
        return False, (
            "the real, uploaded-document potassium value was stripped in prose format, got "
            f"flagged_claims={prose_outcome.flagged_claims}"
        )
    if "4.4" not in prose_outcome.final_response:
        return False, "the real potassium value did not survive into the final prose response"

    table_draft = (
        "From an uploaded scanned document (pending review):\n\n"
        "| Test | Value | Unit |\n|---|---|---|\n| Potassium | 4.4 | mEq/L |"
    )
    table_outcome = verify_response(table_draft, records, "p1", fhir)
    if any("4.4" in c for c in table_outcome.flagged_claims):
        return False, (
            "the real, uploaded-document potassium value was stripped in table format, got "
            f"flagged_claims={table_outcome.flagged_claims}"
        )
    if "4.4" not in table_outcome.final_response:
        return False, "the real potassium value did not survive into the final table response"

    wrong_table_draft = (
        "From an uploaded scanned document (pending review):\n\n"
        "| Test | Value | Unit |\n|---|---|---|\n| Potassium | 5.9 | mEq/L |"
    )
    wrong_outcome = verify_response(wrong_table_draft, records, "p1", fhir)
    if not any("5.9" in c for c in wrong_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely wrong value in table format (5.9, not in the tool "
            f"output) should still be flagged as ungrounded, got flagged_claims={wrong_outcome.flagged_claims}"
        )

    return True, (
        "a real uploaded-document lab value survives in both prose and markdown-table "
        "formatting, and a genuinely wrong value in table format is still caught"
    )


def test_lab_value_range_mention_not_flagged(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a second live bug, found on the very next
    full-suite run after test_lab_value_grounding_tolerates_formatting's
    fix landed (2026-09-18): _LAB_VALUE_RE's candidate detection also
    caught the upper bound of a stated reference range ("...above the
    typical normal range (~3.5-5.0 mEq/L)") and stripped it as an
    "unverified claim" -- even though a reference range is general medical
    knowledge, not a claim about this patient, and shouldn't need a tool
    call to back it. Confirms the range exclusion
    (_LAB_VALUE_RANGE_PREFIX_RE) works end-to-end through verify_response():
    a reference-range mention survives untouched, and -- the control that
    proves this isn't a blanket weakening -- a genuinely fabricated value
    elsewhere in the same response is still caught.
    """
    observations = GetRecentObservationsOutput(
        patient_id=f.PID1_ALICE,
        observations=[
            ObservationFact(
                text="Potassium",
                value="5.8 mEq/L",
                status="final",
                effective_datetime="2026-09-18T02:00:00Z",
                source_resource="Observation/fixture-potassium-1",
            )
        ],
    )
    record = ToolCallRecord(tool_name="get_recent_observations", patient_id=f.PID1_ALICE, output=observations)

    draft = (
        "Her most recent potassium was 5.8 mEq/L, which is elevated -- above the typical "
        "normal range (~3.5-5.0 mEq/L). This should be correlated clinically."
    )
    outcome = verify_response(draft, [record], f.PID1_ALICE, fhir)
    if outcome.flagged_claims:
        return False, (
            "the reference-range mention (5.0 mEq/L, the range's upper bound) was incorrectly "
            f"flagged as an unverified patient-specific claim, got flagged_claims={outcome.flagged_claims}"
        )
    if draft not in outcome.final_response:
        return False, "the reference-range context was stripped from the response instead of surviving untouched"

    fabricated_draft = (
        "Her most recent potassium was 5.8 mEq/L, and her sodium was 200 mEq/L, which is "
        "critically abnormal."
    )
    fabricated_outcome = verify_response(fabricated_draft, [record], f.PID1_ALICE, fhir)
    if not any("200" in c for c in fabricated_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely fabricated value (200 mEq/L sodium, not in the tool "
            f"output, not part of a range) should still be flagged, got "
            f"flagged_claims={fabricated_outcome.flagged_claims}"
        )

    return True, "a reference-range mention survives untouched, and a genuinely fabricated value elsewhere is still caught"


def test_allergy_hard_stop_not_silenced_by_unrelated_conflict_word(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the most severe of three bugs found in a
    systematic audit (2026-09-18) of every regex/substring-match mechanism
    in verification.py, prompted after the two _LAB_VALUE_RE bugs: the
    domain-constraint HARD STOP's "already mentioned" check used a bare
    "conflict" substring cue, which collides with unrelated uses ("a
    scheduling conflict", "the two records conflict on her DOB") --
    confirmed live via audit before this fix. Silently skipping the HARD
    STOP append is exactly what ARCHITECTURE.md 3.2 calls this mechanism a
    "wall" specifically to prevent; a false match here means no signal at
    all reaches the resident. Fixed by dropping bare "conflict" and
    checking the remaining safe cues in a window around the specific
    medication mention, not the whole response
    (_mentions_conflict_near_medication).

    Uses pid1's real, documented penicillin allergy (same fixture as
    test_domain_constraint_backstop_survives_missing_patient_id) so the
    conflict is genuine, not a stub.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    unrelated_conflict_draft = (
        "There is a scheduling conflict for tomorrow's follow-up that the resident should know "
        "about. Penicillin should be fine to give for this infection."
    )
    outcome = verify_response(unrelated_conflict_draft, [record], f.PID1_ALICE, fhir)
    if outcome.passed_domain_constraint:
        return False, (
            "an unrelated mention of 'conflict' (a scheduling conflict) incorrectly satisfied "
            "the allergy-conflict check and silenced the HARD STOP for a real penicillin conflict"
        )
    if "HARD STOP" not in outcome.final_response:
        return False, "expected a [HARD STOP] warning injected for the real, documented penicillin conflict"

    already_stated_draft = "She has a documented penicillin allergy, so penicillin should not be given."
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID1_ALICE, fhir)
    if "HARD STOP" in already_stated_outcome.final_response:
        return False, "a response that already correctly stated the conflict should not get a redundant HARD STOP appended"

    return True, "an unrelated 'conflict' mention no longer silences the HARD STOP, and a genuine self-correction is still respected"


def test_bare_allergy_text_matching_medication_name_not_treated_as_self_acknowledged(
    fhir: FhirClient,
) -> tuple[bool, str]:
    """Regression test for a real bug found investigating a staging CI
    failure (2026-09-22): _mentions_conflict_near_medication used
    matched_allergy_text as a "the draft already acknowledges this" safe
    cue verbatim. For a direct-match conflict (the proposed medication IS
    the documented allergen) with no descriptive/reaction text on the
    allergy record, matched_allergy_text reduces to just the medication's
    own name -- so the medication's own mention in the draft trivially
    satisfied its own safe cue, silently disabling the HARD STOP for
    exactly the simplest, most common conflict shape.

    This one reaches past verify_response() and calls
    _mentions_conflict_near_medication directly (unlike the rest of this
    file) precisely because the bug is only reproducible with a bare-named
    allergy entry, and no live fixture patient anywhere was guaranteed to
    have that exact shape -- pid1's local-dev allergy text happens to read
    "Penicillin (hives)", which coincidentally never triggers this bug,
    while a staging patient seeded with a bare "Penicillin" entry always
    does. A test built on real patient data would silently stop covering
    this the moment that patient's allergy text happened to include any
    descriptive detail, exactly how this went uncaught in the first place.
    Testing the matcher directly with a literal bare-name input pins the
    exact failure shape regardless of what any environment's fixture data
    happens to contain.
    """
    bare_match = _mentions_conflict_near_medication(
        "Penicillin should be fine to give for this infection.", "penicillin", "Penicillin"
    )
    if bare_match:
        return False, (
            "a bare allergen name ('Penicillin') exactly matching the proposed medication "
            "('penicillin') was incorrectly treated as the draft already acknowledging the "
            "conflict -- the medication's own mention trivially 'contains' its own name"
        )

    descriptive_match = _mentions_conflict_near_medication(
        "Given her history of hives with penicillin, we should avoid it for this infection.",
        "penicillin",
        "Penicillin (hives)",
    )
    if not descriptive_match:
        return False, (
            "a draft that genuinely restates the allergy's descriptive detail ('hives') near "
            "the medication should still be recognized as a real self-acknowledgment"
        )

    cross_reactive_match = _mentions_conflict_near_medication(
        "Given her penicillin allergy history, amoxicillin needs review.", "amoxicillin", "Penicillin"
    )
    if not cross_reactive_match:
        return False, (
            "a cross-reactive conflict (different allergen and medication names) should be "
            "unaffected by this fix -- the allergen name never overlaps the medication name "
            "to begin with, so mentioning it near the medication is still real evidence"
        )

    return True, (
        "a bare allergen name matching the medication is no longer self-acknowledging; a genuine "
        "descriptive mention and a cross-reactive mention both still correctly count"
    )


def test_compare_signout_baseline_requires_genuine_prior_turn(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a real bug found investigating a flaky
    signout_check_no_baseline CI failure (2026-09-22): compare_signout_to_
    chart's baseline_established was computed from ALL of turn_records,
    which includes records from EARLIER ROUNDS OF THE SAME TURN (e.g. the
    model calling get_patient_snapshot in round 0, then
    compare_signout_to_chart in round 1) -- not just genuinely earlier
    turns. That's not a real baseline, it's diffing "now" against "a few
    seconds ago", trivially showing zero discrepancies and producing
    exactly the false confidence this tool exists to prevent. Live-
    confirmed: the identical first message of a fresh conversation
    produced baseline_established=True on some real runs and False on
    others, purely from the model's own, non-deterministic choice of
    tool-calling order -- never from anything about the conversation's
    real history.

    Fix: ToolCallRecord.from_prior_turn (app/tools.py), re-stamped True
    only for records inherited from run_turn's prior_tool_records
    (app/agent.py), never for one created this turn. This test calls
    compare_signout_to_chart directly (no LLM involved -- FHIR calls are
    fine per this file's own "LLM-free" scope, just not Anthropic ones)
    with hand-built turn_records, pinning the exact distinction a live
    model call can't reliably exercise on demand."""
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"

    no_history = compare_signout_to_chart(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(no_history, CompareSignoutToChartOutput):
        return False, f"setup failed: compare_signout_to_chart returned {no_history}"
    if no_history.baseline_established:
        return False, "empty turn_records (genuinely no history at all) should never establish a baseline"

    same_turn_record = ToolCallRecord(
        tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot, from_prior_turn=False
    )
    same_turn_result = compare_signout_to_chart(fhir, {"patient_id": f.PID1_ALICE}, [same_turn_record])
    if not isinstance(same_turn_result, CompareSignoutToChartOutput):
        return False, f"setup failed: compare_signout_to_chart returned {same_turn_result}"
    if same_turn_result.baseline_established:
        return False, (
            "a record from earlier THIS SAME TURN incorrectly established a baseline -- this is "
            "the exact live-confirmed bug: diffing against data fetched seconds ago in the same "
            "turn is not a genuine baseline"
        )

    prior_turn_record = ToolCallRecord(
        tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot, from_prior_turn=True
    )
    prior_turn_result = compare_signout_to_chart(fhir, {"patient_id": f.PID1_ALICE}, [prior_turn_record])
    if not isinstance(prior_turn_result, CompareSignoutToChartOutput):
        return False, f"setup failed: compare_signout_to_chart returned {prior_turn_result}"
    if not prior_turn_result.baseline_established:
        return False, "a record genuinely inherited from an earlier turn should establish a baseline"

    return True, (
        "baseline_established correctly requires a genuine prior-turn record -- a same-turn "
        "record (the live-confirmed bug) and no history at all both correctly do not establish one"
    )


def test_duplicate_warning_not_silenced_by_unrelated_records_mention(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the second of three bugs from the same audit:
    the duplicate-patient-record caveat's "already mentioned" check used
    bare substrings like "two records"/"multiple records", which collide
    with unrelated mentions ("multiple records of prior vaccinations") --
    confirmed live via audit. This is the exact "never silently drop"
    guarantee c4_7_explicit_suppress_request's own guards_against text
    describes; a false match here silently skips the caveat entirely.
    Fixed by requiring the duplicate/matching language to be tied to the
    patient/chart/record context specifically, or by checking the literal
    other_patient_id directly (_mentions_duplicate_warning).
    """
    snapshot = GetPatientSnapshotOutput(
        patient_id=f.PID1_ALICE, name="Alice Testpatient", birth_date="1972-03-14", gender="female",
        conditions=[], medications=[], allergies=[], chart_is_empty=False,
        duplicate_warnings=[DuplicatePatientWarning(other_patient_id="fixture-other-id-1", matched_on="name+birthdate")],
        partial_failures=[],
    )
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    unrelated_records_draft = "Her chart shows multiple records of prior vaccinations. She has type 2 diabetes."
    outcome = verify_response(unrelated_records_draft, [record], f.PID1_ALICE, fhir)
    if "duplicate_patient" not in outcome.enforced_warnings:
        return False, (
            "an unrelated mention of 'multiple records' (vaccination history) incorrectly "
            "satisfied the duplicate-warning check and silenced the caveat for a real duplicate "
            f"patient record, got enforced_warnings={outcome.enforced_warnings}"
        )

    already_stated_draft = (
        "Possible duplicate record: this patient matches on name + birthdate with another "
        "patient record."
    )
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID1_ALICE, fhir)
    if "duplicate_patient" in already_stated_outcome.enforced_warnings:
        return False, "a response that already correctly stated the duplicate should not get a redundant caveat appended"

    return True, "an unrelated 'records' mention no longer silences the duplicate caveat, and a genuine self-correction is still respected"


def test_empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the third of three bugs from the same audit:
    the empty-chart caveat's "already mentioned" check used bare
    substrings like "no problems"/"no medications", which collide with
    unrelated uses ("no problems accessing this data") -- confirmed live
    via audit. A false match here silently skips the caveat that exists
    specifically so an empty chart never gets presented as reassuring
    (ARCHITECTURE.md Section 4). Fixed by keeping a few highly specific
    standalone phrases and replacing the generic ones with a structural
    pattern requiring "no"/"none" to actually be followed by "recorded"/
    "documented"/"on file"/"noted" (_mentions_empty_chart).
    """
    snapshot = GetPatientSnapshotOutput(
        patient_id=f.PID3_CAROL, name="Carol Emptychart", birth_date="1990-07-21", gender="female",
        conditions=[], medications=[], allergies=[], chart_is_empty=True,
        duplicate_warnings=[], partial_failures=[],
    )
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID3_CAROL, output=snapshot)

    unrelated_draft = "I had no problems accessing this data for the patient."
    outcome = verify_response(unrelated_draft, [record], f.PID3_CAROL, fhir)
    if "empty_chart" not in outcome.enforced_warnings:
        return False, (
            "an unrelated mention of 'no problems' (data access) incorrectly satisfied the "
            f"empty-chart check and silenced the caveat, got enforced_warnings={outcome.enforced_warnings}"
        )

    already_stated_draft = "This chart shows no conditions, medications, or allergies are recorded."
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID3_CAROL, fhir)
    if "empty_chart" in already_stated_outcome.enforced_warnings:
        return False, "a response that already correctly described the empty chart should not get a redundant caveat appended"

    return True, "an unrelated 'no problems' mention no longer silences the empty-chart caveat, and a genuine self-correction is still respected"


def test_partial_failure_force_disclosed_when_not_mentioned(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the gap found investigating get_recent_encounters'
    sensitivity-lookup 401 (evals/COVERAGE.md's corrected entry, 2026-09-20):
    partial_failures (app/schemas.py) had no enforcement backstop at all,
    unlike duplicate_warnings/chart_is_empty just above it in this same
    file -- whether a resident learned a sub-fetch failed depended entirely
    on the model choosing to mention it from the raw tool JSON. Confirms
    _enforce_partial_failures force-injects an explicit disclosure, naming
    the specific failure (not a generic "something went wrong"), when the
    drafted response doesn't already mention it.
    """
    encounters_output = GetRecentEncountersOutput(
        patient_id=f.PID1_ALICE, encounters=[], sensitivity_filtered_count=0,
        partial_failures=["sensitivity lookup: http_error"],
    )
    record = ToolCallRecord(tool_name="get_recent_encounters", patient_id=f.PID1_ALICE, output=encounters_output)

    undisclosed_draft = "This patient has no recent encounters on file."
    outcome = verify_response(undisclosed_draft, [record], f.PID1_ALICE, fhir)
    if "partial_failure" not in outcome.enforced_warnings:
        return False, (
            "expected a force-injected partial-failure disclosure when the draft never "
            f"mentioned it, got enforced_warnings={outcome.enforced_warnings}"
        )
    if "sensitivity lookup: http_error" not in outcome.final_response:
        return False, (
            "expected the specific failure ('sensitivity lookup: http_error') named in the "
            f"injected note, not a generic message -- got: {outcome.final_response!r}"
        )
    return True, "an undisclosed partial_failures entry was force-injected, naming the specific failure"


def test_partial_failure_not_double_injected_when_already_mentioned(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a response that already honestly disclosed a partial
    failure must never get a redundant warning appended -- genuine
    self-correction must always be respected. Companion to the
    force-disclosure test above: when the model already honestly surfaced
    the failure in its own words, _enforce_partial_failures must not append
    a redundant warning -- same guarantee already proven for
    duplicate_warnings/chart_is_empty just above."""
    encounters_output = GetRecentEncountersOutput(
        patient_id=f.PID1_ALICE, encounters=[], sensitivity_filtered_count=0,
        partial_failures=["sensitivity lookup: http_error"],
    )
    record = ToolCallRecord(tool_name="get_recent_encounters", patient_id=f.PID1_ALICE, output=encounters_output)

    already_disclosed_draft = (
        "I couldn't retrieve the sensitivity status for this patient's recent encounters, "
        "so I'm not showing any -- please check OpenEMR directly if you need the full history."
    )
    outcome = verify_response(already_disclosed_draft, [record], f.PID1_ALICE, fhir)
    if "partial_failure" in outcome.enforced_warnings:
        return False, (
            "a response that already honestly disclosed the partial failure should not get a "
            f"redundant verification note appended, got enforced_warnings={outcome.enforced_warnings}"
        )
    return True, "a response that already disclosed the partial failure was not given a redundant warning"


def test_dose_candidate_finds_real_drug_not_preceding_verb(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the fourth bug from the same audit as the three
    "mentions" fixes above, and the same class as the two _LAB_VALUE_RE
    bugs: _DOSE_RE assumed the capitalized word immediately BEFORE a dose
    is always the drug name -- found live, "Started 10 mg Lisinopril
    daily" captured "Started", not "Lisinopril", stripping the whole
    (correct) sentence. Fixed structurally: checks both sides of the dose
    pattern and prefers a real drug name found immediately after the
    dose+unit when one exists, falling back to the preceding word only
    when it doesn't look verb-shaped (see _dose_candidates' own docstring
    for the honestly-disclosed limit of pure morphology here -- "Given"
    and "Day" needed a small supplementary exclusion, not the same kind
    of fix as the fabrication-phrase keyword lists elsewhere in this
    file).

    Uses the exact three collision sentences found during the audit, plus
    a control confirming detection isn't blanket-weakened.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    # A real drug (Lisinopril, pid1's actual medication) present, with a
    # verb -- not the drug name -- immediately preceding the dose.
    real_drug_draft = "Started 10 mg Lisinopril daily for her hypertension."
    outcome = verify_response(real_drug_draft, [record], f.PID1_ALICE, fhir)
    if any("started" in c.lower() for c in outcome.flagged_claims):
        return False, (
            "'Started' was incorrectly treated as the drug-name candidate instead of "
            f"'Lisinopril', got flagged_claims={outcome.flagged_claims}"
        )
    if real_drug_draft not in outcome.final_response:
        return False, "the real, grounded Lisinopril sentence was stripped instead of surviving"

    # No real drug present at all in either sentence -- the false-positive
    # word (Given / Day) must not be flagged, since there's nothing here
    # to fabricate a claim about.
    for draft in ["Given 4 g IV push.", "On Day 3 units of blood were transfused."]:
        no_drug_outcome = verify_response(draft, [record], f.PID1_ALICE, fhir)
        if no_drug_outcome.flagged_claims:
            return False, (
                f"expected no flagged claims for {draft!r} (no real drug name present), got "
                f"flagged_claims={no_drug_outcome.flagged_claims}"
            )

    # Control: a genuinely fabricated drug name (not in the tool output)
    # in the same "verb dose unit DRUG" shape must still be caught -- the
    # fix prefers the following word, it doesn't stop checking it.
    fabricated_draft = "Started 20 mg Fakenstatin daily for her cholesterol."
    fabricated_outcome = verify_response(fabricated_draft, [record], f.PID1_ALICE, fhir)
    if not any("fakenstatin" in c.lower() for c in fabricated_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely fabricated drug name (Fakenstatin, not in the tool "
            f"output) should still be flagged, got flagged_claims={fabricated_outcome.flagged_claims}"
        )

    return True, (
        "the real drug name (not the preceding verb) is correctly extracted and grounds, the "
        "no-drug-present sentences don't get a false-positive word flagged, and a genuinely "
        "fabricated drug name in the same shape is still caught"
    )


# --- (c) app/sensitivity.py -- pure functions, no FHIR/network needed at all,
# but kept in this LLM-free suite (not evals/cases.py) since they need no
# model call either. Phase 1's empirical finding this guards: a real
# authorization_code `user/`-scoped token's FHIR Encounter search does NOT
# filter high-sensitivity encounters (confirmed live against pid4's known
# sensitivity='high' test encounter -- ARCHITECTURE.md 3.3 / architecture-
# audit.md 6.6) -- this filter is the only thing that does, so it must
# actually exclude what the platform doesn't. ------------------------------


def test_sensitivity_filter_excludes_high_for_clin(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a role without a High-sensitivity grant (clin, per
    audit-notes.md's gacl matrix) must never see a high-sensitivity
    encounter -- the platform itself doesn't filter this, so this
    compensating filter is the only thing that does."""
    encounters = [
        {"id": "1", "sensitivity": "normal"},
        {"id": "2", "sensitivity": "high"},
        {"id": "3", "sensitivity": ""},
    ]
    filtered = filter_encounters_by_sensitivity(encounters, "clin")
    ids = [e["id"] for e in filtered]
    if "2" in ids:
        return False, f"clin (no High grant per audit-notes.md) must not see a high-sensitivity encounter, got ids={ids}"
    if ids != ["1", "3"]:
        return False, f"expected non-high encounters to pass through unchanged, got ids={ids}"
    return True, "clin correctly excluded the high-sensitivity encounter, kept the others"


def test_sensitivity_filter_allows_high_for_doc(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: role-based sensitivity clearance must always match
    audit-notes.md's documented gacl matrix -- doc keeps High-sensitivity
    access, clin does not, admin does."""
    encounters = [{"id": "1", "sensitivity": "high"}]
    filtered = filter_encounters_by_sensitivity(encounters, "doc")
    if len(filtered) != 1:
        return False, f"doc holds a High-sensitivity grant per audit-notes.md's gacl matrix, expected it kept, got {filtered}"
    if has_high_sensitivity_clearance("clin"):
        return False, "clin must not report High-sensitivity clearance (audit-notes.md: no grant)"
    if not has_high_sensitivity_clearance("admin"):
        return False, "admin must report High-sensitivity clearance (audit-notes.md: full write grant)"
    return True, "doc's High-sensitivity grant is respected; clin/admin clearance flags match the gacl matrix"


# --- (d) Two invariants found via THREAT_MODEL.md-prompted re-verification
# (2026-09-17) against the post-Phase-1 code, confirmed live, then fixed.
# Both are deterministic reproductions with no LLM in the loop -- exactly
# what this suite is for, unlike evals/cases.py's LLM-behavior cases.


def test_domain_constraint_backstop_survives_missing_patient_id(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a real bug: verify_response() skipped the
    hard-coded allergy-conflict backstop entirely whenever a /chat caller
    omitted `patient_id`, because `_check_domain_constraint` only ran
    `if current_patient_id:`. Live-confirmed against pid1's real, documented
    penicillin allergy before the fix, with a drafted response mentioning
    penicillin but never stating the conflict:

        | patient_id declared | passed_domain_constraint | HARD STOP | verification_passed |
        |---|---|---|---|
        | supplied             | False                     | yes       | False                |
        | omitted              | True                      | no        | True                 |

    Omitting patient_id from the request silently disabled a hard,
    code-level safety check -- exactly the thing ARCHITECTURE.md 3.2 says a
    prompt instruction alone can't be trusted for. Fix: verify_response()
    now falls back to the single patient this turn's own tool calls
    grounded data for when the caller omits patient_id
    (`app/verification.py::_effective_domain_check_patient_id`), rather than
    skipping the check outright.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)
    fake_draft = "Penicillin should be fine to give for this infection."

    outcome = verify_response(fake_draft, [record], None, fhir)  # patient_id OMITTED, as /chat allows
    if outcome.passed_domain_constraint:
        return False, (
            "domain-constraint backstop was skipped even though patient_id was omitted -- "
            "regression of the 2026-09-17 fix"
        )
    if "HARD STOP" not in outcome.final_response:
        return False, "expected a [HARD STOP] warning injected for the real, documented penicillin conflict"
    if "penicillin" not in outcome.enforced_warnings:
        return False, f"expected 'penicillin' in enforced_warnings, got {outcome.enforced_warnings}"
    return True, "domain-constraint backstop correctly fired even though patient_id was omitted from the request"


def test_cross_patient_tool_call_blocked(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a real bug: `_call_tool()` dispatched straight to
    the real FHIR read for whatever `patient_id` a tool call carried, with
    no check against the conversation's declared active patient anywhere in
    the call path. Live-confirmed before the fix: a `get_patient_snapshot`
    call for pid4 executed and returned real data (Dan Otherprovider, his
    real conditions/medications) while the turn's declared active patient
    was pid1 -- nothing intercepted the mismatch before the network call.
    The only patient_id filtering that existed (`records_for_active_patient`
    in `run_turn`) ran *after* the fetch, and only affected grounding, not
    whether the read happened.

    Fix: `_call_tool()` now takes the turn's `active_patient_id` and rejects
    a mismatched tool-call `patient_id` with a `ToolFailure` (detail_code
    `patient_mismatch`) before dispatch (`app/agent.py`).
    """
    agent = ClinicalCopilotAgent(get_settings())

    blocked = agent._call_tool("get_patient_snapshot", {"patient_id": f.PID4_DAN}, fhir, f.PID1_ALICE, [])
    if not isinstance(blocked, ToolFailure):
        return False, f"expected the mismatched patient_id to be blocked before dispatch, got: {blocked!r}"
    if blocked.detail_code != "patient_mismatch":
        return False, f"expected detail_code='patient_mismatch', got {blocked.detail_code!r}"

    # Control: a matching patient_id must still work normally -- this isn't
    # supposed to block all tool calls, only mismatched ones.
    ok = agent._call_tool("get_patient_snapshot", {"patient_id": f.PID1_ALICE}, fhir, f.PID1_ALICE, [])
    if not isinstance(ok, GetPatientSnapshotOutput):
        return False, f"a matching patient_id should still succeed normally, got: {ok!r}"

    return True, "cross-patient tool call was blocked before any FHIR read; matching patient_id still works"


def test_shift_summary_reports_nothing_gathered_yet(fhir: FhirClient) -> tuple[bool, str]:
    """UC4 boundary condition, LLM-free by design rather than an eval case:
    summarize_shift_events' `data_gathered=False` path is a Python-level
    contract (what does the function return when turn_records has no
    records for this patient), not really a question of model behavior --
    a compliant model, following SYSTEM_PROMPT's "gather first" nudge,
    should rarely if ever hit this path in a real conversation, which
    makes it an unreliable thing to force via a natural LLM eval message.
    Testing the tool's own contract directly is both more rigorous and
    more deterministic for this specific case than trying to coax an LLM
    into skipping its own gathering step. The companion boundary condition
    -- data was gathered, and it's genuinely empty (data_gathered=True,
    events=[]) -- is `shift_summary_empty_honest_report` in evals/cases.py,
    exercised against pid3's real empty chart; this test is deliberately
    the other honest-failure condition, not a duplicate of it.
    """
    result = summarize_shift_events(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(result, SummarizeShiftEventsOutput):
        return False, f"expected a SummarizeShiftEventsOutput, got: {result!r}"
    if result.data_gathered is not False:
        return False, f"expected data_gathered=False when turn_records has nothing for this patient, got {result.data_gathered!r}"
    if result.events:
        return False, f"expected an empty events list when nothing was gathered, got {result.events!r}"

    # Control: a real record for a DIFFERENT patient must not leak in as
    # "gathered" for this patient -- data_gathered is per-patient, not
    # "anything exists in turn_records at all".
    other_patient_record = ToolCallRecord(
        tool_name="get_patient_snapshot",
        patient_id=f.PID2_BOB,
        output=GetPatientSnapshotOutput(
            patient_id=f.PID2_BOB, name="Bob Testpatient", birth_date=None, gender=None,
            conditions=[], medications=[], allergies=[], chart_is_empty=True,
            duplicate_warnings=[], partial_failures=[],
        ),
    )
    scoped_result = summarize_shift_events(fhir, {"patient_id": f.PID1_ALICE}, [other_patient_record])
    if scoped_result.data_gathered is not False:
        return False, "a different patient's gathered data incorrectly counted as 'gathered' for this patient"

    return True, "correctly reports data_gathered=False, scoped per-patient, when nothing has been gathered yet"

    return True, "cross-patient tool call was blocked before any FHIR read; matching patient_id still works"


# --- Medical reference layer (app/reference_layer.py) -----------------------
#
# medical_reference_layer_prompt.md's approved design, §6, plus the user's
# explicit addition of the past-tense negative case. Real, live network
# calls to DailyMed/MedlinePlus (not mocked) -- same "test against the real
# thing" convention already used for get_patient_snapshot/check_allergy_
# conflict above against real FHIR data.
#
# Unlike the FHIR calls above (this project's own dev stack, expected to
# always be up during a test run), DailyMed/MedlinePlus are third-party
# public services this project doesn't control -- an outage or a slow
# network there is a real possibility, and it must not look like a
# regression in our own code. _external_api_reachable() draws that line
# explicitly: a probe that can't even connect (DNS/TCP/TLS/timeout) means
# the API itself is unavailable, so the affected test is reported as a
# labeled SKIP (matching evals/run_evals.py's existing skip_reason
# convention), not a FAIL. A probe that DOES connect -- even a 4xx/5xx --
# means the service is up and whatever comes back is real, so the test
# proceeds and a genuine mismatch there (extraction logic broke, or the
# label/page's shape changed) is a real, meaningful failure, not
# something to paper over. This mirrors the feature's own philosophy at
# the test layer: distinguish "we couldn't check" from "we checked and it
# was wrong," the same distinction app/reference_layer.py itself makes
# between an honest no-reference-available and something worth surfacing.


def _external_api_reachable(url: str, params: dict[str, str]) -> bool:
    """Best-effort connectivity probe for a real third-party API. Only a
    connection-level failure (DNS, TCP, TLS, timeout) counts as
    unreachable -- any actual HTTP response, including a 4xx/5xx, means
    the service is up, so `raise_for_status()` is deliberately NOT called
    here."""
    try:
        httpx.get(url, params=params, timeout=5.0)
        return True
    except httpx.HTTPError:
        return False


def test_collect_source_resources_scoped_deduped_and_nested(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: collect_source_resources (feeds /chat's `source_resources`,
    which /ui shows as "what this reply was grounded on") -- LLM- and
    FHIR-free. Must (1) find source_resource strings nested inside another tool's
    output (compare_signout_to_chart embeds a whole snapshot), (2) list
    each once, in first-seen order, (3) never include another patient's
    records, and (4) return nothing when there is no active patient."""

    def snapshot(pid: str, condition_ref: str) -> GetPatientSnapshotOutput:
        return GetPatientSnapshotOutput(
            patient_id=pid,
            name="Test Patient",
            birth_date=None,
            gender=None,
            conditions=[
                ConditionFact(
                    text="Hypertension",
                    category="problem-list-item",
                    clinical_status="active",
                    source_resource=condition_ref,
                )
            ],
            medications=[],
            allergies=[],
            chart_is_empty=False,
            duplicate_warnings=[],
        )

    records = [
        ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p1", output=snapshot("p1", "Condition/a")),
        ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p2", output=snapshot("p2", "Condition/OTHER")),
        ToolCallRecord(
            tool_name="compare_signout_to_chart",
            patient_id="p1",
            output=CompareSignoutToChartOutput(
                patient_id="p1",
                baseline_established=True,
                discrepancies=[],
                # Same ref as the first record (dedupe) plus a new one, nested one level down.
                current_snapshot=snapshot("p1", "Condition/a"),
                current_encounters=GetRecentEncountersOutput(
                    patient_id="p1",
                    encounters=[
                        {"text": "Visit", "status": None, "period_start": None, "source_resource": "Encounter/b"}
                    ],
                    sensitivity_filtered_count=0,
                ),
            ),
        ),
    ]

    got = collect_source_resources(records, "p1")
    if got != ["Condition/a", "Encounter/b"]:
        return False, f"expected ['Condition/a', 'Encounter/b'] for p1, got {got!r}"
    if collect_source_resources(records, None) != []:
        return False, "expected no source resources when there is no active patient"
    return True, "nested refs found, deduped in first-seen order, other patients excluded, None patient -> []"


# --- /login?next= open-redirect regression (security finding) --------------
#
# /login?next=<x> is stored server-side against the OAuth `state` and
# redirected to verbatim after a successful OpenEMR login (app/main.py
# /callback). Unvalidated, an attacker-crafted link could bounce a resident
# to an off-site page right after a REAL login. `next` must only ever be
# /ui (optionally with a query string) -- narrower than "any same-origin
# path" on purpose, since nothing else needs it; anything else falls back
# to /ui.

_OPEN_REDIRECT_PAYLOADS = [
    "https://evil.example.com",
    "https://evil.example.com/fake-login",
    "http://evil.example.com",
    "//evil.example.com",  # protocol-relative: inherits the app's own scheme
    "//evil.example.com/fake-login",
    "///evil.example.com",
    "https:evil.example.com",  # no slashes: WHATWG parsers still treat it as absolute
    "/\\evil.example.com",  # browsers normalize backslash to slash
    "\\\\evil.example.com",
    "/\t/evil.example.com",  # browsers strip tab/CR/LF from URLs
    "\t//evil.example.com",
    " //evil.example.com",
    "javascript:alert(document.domain)",
    "data:text/html,<script>alert(1)</script>",
    "/ui\r\nX-Injected: 1",
]

# Same-origin and harmless-looking, but not a page anything sends a resident
# back to -- must fall back to /ui too (the allow-list is /ui only).
_DISALLOWED_SAME_ORIGIN_NEXT_TARGETS = [
    "/some-other-route",
    "/login?next=/some-other-route",
    "/chat",
    "/me",
    "/callback?code=x&state=y",
    "/login?next=/ui",
    "/ui/",
    "/ui/extra",
    "/uix",
    "/UI",
    "/ui#fragment",
    "/ui/../callback",
    "/ui%2F..%2Fcallback",
]

_MUST_FALL_BACK_TO_UI = _OPEN_REDIRECT_PAYLOADS + _DISALLOWED_SAME_ORIGIN_NEXT_TARGETS

_LEGIT_NEXT_TARGETS = [
    "/ui",
    "/ui?patient=98c4b82b-b07e-11f1-8334-022958ad0af8",
    "/ui?patient=98c4b82b-b07e-11f1-8334-022958ad0af8&x=%2F%2Fnot-a-host",
]


def test_login_next_sanitizer_rejects_offsite_targets(fhir: FhirClient) -> tuple[bool, str]:
    """Boundary: exhaustive hostile/malformed-input sweep against
    app/oauth_session.py's sanitize_next_url -- every off-site /
    scheme-bearing / protocol-relative / control-character payload, and
    every same-origin path other than /ui, collapses to the
    safe default; /ui with a query string (including a `//` that is merely
    inside a query VALUE) passes through byte-for-byte."""
    from app.oauth_session import sanitize_next_url

    still_honored = [p for p in _MUST_FALL_BACK_TO_UI if sanitize_next_url(p) != "/ui"]
    if still_honored:
        return False, f"values not collapsed to /ui: {still_honored!r}"
    mangled = [t for t in _LEGIT_NEXT_TARGETS if sanitize_next_url(t) != t]
    if mangled:
        return False, f"legitimate same-origin targets were altered: {mangled!r}"
    if sanitize_next_url("") != "/ui" or sanitize_next_url("/" + "a" * 5000) != "/ui":
        return False, "empty or oversized next must fall back to /ui"
    return True, (
        f"{len(_OPEN_REDIRECT_PAYLOADS)} hostile payloads and {len(_DISALLOWED_SAME_ORIGIN_NEXT_TARGETS)} "
        f"non-/ui same-origin paths collapsed to /ui, {len(_LEGIT_NEXT_TARGETS)} legit /ui targets preserved"
    )


def test_login_flow_never_redirects_offsite(fhir: FhirClient) -> tuple[bool, str]:
    """Regression: guards against the open-redirect vulnerability class
    being reintroduced at either the sanitizer or the handler layer.
    End-to-end through the REAL /login and /callback handlers
    (app/main.py), with only OpenEMR's token endpoint faked
    (httpx.MockTransport) -- so it fails if anyone reintroduces an
    unvalidated redirect at either end,
    not just if the helper regresses. Asserts the Location header the
    browser would actually follow after a successful login.

    Handlers are called directly rather than via TestClient so the request-
    timing middleware does not append test traffic to request_timing.log
    (real latency data lives there)."""
    from urllib.parse import parse_qs, urlsplit

    from app import main

    def fake_token_endpoint(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600})

    real_client = main._sessions._client
    main._sessions._client = httpx.Client(transport=httpx.MockTransport(fake_token_endpoint))
    try:

        def landing_after_login(next_value: str | None) -> str:
            login_response = main.login() if next_value is None else main.login(next=next_value)
            state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
            return main.callback(code="fake-code", state=state).headers["location"]

        landed_at = {p: landing_after_login(p) for p in _MUST_FALL_BACK_TO_UI}
        bad = {p: loc for p, loc in landed_at.items() if loc != "/ui"}
        if bad:
            return False, f"login honored a next value it must reject, Location was: {bad!r}"
        for target in _LEGIT_NEXT_TARGETS:
            landed = landing_after_login(target)
            if landed != target:
                return False, f"legit next {target!r} redirected to {landed!r} instead"
        if landing_after_login(None) != "/ui":
            return False, "omitted next must default to /ui"
    finally:
        main._sessions._client = real_client
    return True, "every hostile or non-/ui next value lands on /ui after a real login; legit /ui targets and the default are unchanged"


# --- (b6) POST /feedback (resident feedback mechanism, 2026-09-20 design) --
#
# Same "call the real handler directly" convention as
# test_login_flow_never_redirects_offsite above, for the same reason: a
# TestClient round-trip would route through request_timing_middleware and
# append test traffic to the real request_timing.log. Auth is faked by
# registering a ResidentSession directly in main._sessions._sessions
# (session.tokens only needs to satisfy get_token(), never a real OpenEMR
# token exchange) rather than a full login round-trip, which this feature
# has nothing to do with.


class _FakeTokenProvider:
    """Stands in for OAuthTokenProvider in a fake ResidentSession -- only
    needs to satisfy SessionStore.get()'s liveness check
    (session.tokens.get_token()) without a real network call."""

    def get_token(self) -> str:
        return "fake-token"


def _install_fake_session(resident_username: str) -> str:
    """Registers a fake authenticated session directly in
    main._sessions._sessions, same monkeypatch-by-hand convention as
    test_login_flow_never_redirects_offsite's main._sessions._client swap.
    fhir=None: POST /feedback never touches session.fhir. Caller is
    responsible for popping the returned session_id back out when done."""
    import uuid as uuid_module

    from app import main
    from app.oauth_session import ResidentSession

    session_id = str(uuid_module.uuid4())
    main._sessions._sessions[session_id] = ResidentSession(
        session_id=session_id,
        tokens=_FakeTokenProvider(),
        fhir=None,
        resident_username=resident_username,
    )
    return session_id


def test_feedback_requires_auth(fhir: FhirClient) -> tuple[bool, str]:
    """Boundary: no valid session must be rejected, not silently accepted --
    same 401 contract as POST /chat."""
    from fastapi import HTTPException

    from app import main
    from app.main import FeedbackRequest

    payload = FeedbackRequest(correlation_id="case-feedback-noauth", rating="up")
    try:
        main.submit_feedback(payload, copilot_session=None)
        return False, "expected an HTTPException(401) with no session, got a normal return"
    except HTTPException as exc:
        if exc.status_code != 401:
            return False, f"expected 401, got {exc.status_code}"
    return True, "POST /feedback with no session correctly rejected with 401"


def test_feedback_valid_submission_writes_log_and_attempts_langfuse(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a valid submission must produce a durable JSONL entry AND
    attempt (not necessarily succeed at -- Langfuse may not be configured
    locally) a Langfuse score attachment. Dual storage must not silently
    degrade to single storage. Points main._FEEDBACK_LOG_PATH at a
    throwaway temp file for the duration of the test, restored in
    finally -- same pattern as the _sessions._client swap above."""
    import tempfile
    from pathlib import Path

    from app import main
    from app.main import FeedbackRequest

    session_id = _install_fake_session("case-feedback-resident")
    real_log_path = main._FEEDBACK_LOG_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            main._FEEDBACK_LOG_PATH = Path(tmp_dir) / "feedback.jsonl"
            payload = FeedbackRequest(
                correlation_id="case-feedback-corr-1",
                rating="down",
                comment="Missed the recent A1c value.",
            )
            response = main.submit_feedback(payload, copilot_session=session_id)

            if not main._FEEDBACK_LOG_PATH.exists():
                return False, "expected feedback.jsonl to be created"
            lines = main._FEEDBACK_LOG_PATH.read_text().strip().splitlines()
            if len(lines) != 1:
                return False, f"expected exactly 1 log line, got {len(lines)}"
            entry = json.loads(lines[0])
            if entry.get("correlation_id") != "case-feedback-corr-1":
                return False, f"expected correlation_id in the log entry, got {entry!r}"
            if entry.get("rating") != "down":
                return False, f"expected rating='down' in the log entry, got {entry!r}"
            if entry.get("comment") != "Missed the recent A1c value.":
                return False, f"expected the comment in the log entry, got {entry!r}"
            from app.log_redaction import hash_identifier

            # 2026-09-25: identifiers are stored as salted hashes, never raw (app/log_redaction.py).
            if entry.get("resident_username") != hash_identifier("case-feedback-resident"):
                return False, f"expected the HASHED session resident_username in the log entry, got {entry!r}"
            if "case-feedback-resident" in lines[0]:
                return False, "the raw resident_username must not appear anywhere in the feedback.jsonl line"
            if "timestamp" not in entry:
                return False, f"expected a timestamp in the log entry, got {entry!r}"
            if response.status != "recorded":
                return False, f"expected status='recorded', got {response.status!r}"
            if not isinstance(response.langfuse_attached, bool):
                return False, (
                    f"expected langfuse_attached to be a bool (attempted either way), got "
                    f"{response.langfuse_attached!r}"
                )
    finally:
        main._FEEDBACK_LOG_PATH = real_log_path
        main._sessions._sessions.pop(session_id, None)

    return True, "a valid submission wrote exactly one correct JSONL entry and attempted a Langfuse score attachment"


def test_feedback_comment_is_optional(fhir: FhirClient) -> tuple[bool, str]:
    """Regression: omitting comment must not break the happy path."""
    import tempfile
    from pathlib import Path

    from app import main
    from app.main import FeedbackRequest

    session_id = _install_fake_session("case-feedback-resident-2")
    real_log_path = main._FEEDBACK_LOG_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            main._FEEDBACK_LOG_PATH = Path(tmp_dir) / "feedback.jsonl"
            payload = FeedbackRequest(correlation_id="case-feedback-corr-3", rating="up")
            response = main.submit_feedback(payload, copilot_session=session_id)
            if response.status != "recorded":
                return False, f"expected status='recorded' with comment omitted, got {response.status!r}"
    finally:
        main._FEEDBACK_LOG_PATH = real_log_path
        main._sessions._sessions.pop(session_id, None)

    return True, "a submission with no comment still succeeds"


def test_feedback_does_not_trust_client_supplied_identity(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: resident_username must come from the session, never a
    client-supplied value. FeedbackRequest has no resident_username field at
    all, so a client attempting to smuggle one into the request body is
    silently ignored by the schema (extra fields are dropped, not an error),
    and the log entry always reflects the real session identity."""
    import tempfile
    from pathlib import Path

    from app import main
    from app.main import FeedbackRequest

    session_id = _install_fake_session("case-feedback-real-resident")
    real_log_path = main._FEEDBACK_LOG_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            main._FEEDBACK_LOG_PATH = Path(tmp_dir) / "feedback.jsonl"
            payload = FeedbackRequest.model_validate({
                "correlation_id": "case-feedback-corr-4",
                "rating": "up",
                "resident_username": "dr_someone_else",  # not a real field on the schema
            })
            main.submit_feedback(payload, copilot_session=session_id)
            entry = json.loads(main._FEEDBACK_LOG_PATH.read_text().strip())
            from app.log_redaction import hash_identifier

            # stored as a salted hash (app/log_redaction.py): the REAL session identity, never the client's
            if entry.get("resident_username") != hash_identifier("case-feedback-real-resident"):
                return False, (
                    f"expected the real session's resident_username, got "
                    f"{entry.get('resident_username')!r} -- a client-supplied identity leaked through"
                )
    finally:
        main._FEEDBACK_LOG_PATH = real_log_path
        main._sessions._sessions.pop(session_id, None)

    return True, "a client-supplied resident_username in the payload was ignored; the real session identity was recorded"


def test_reference_layer_query_isolation(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: patient-data isolation, enforced structurally -- the
    outbound query dicts must contain only the drug/condition name string
    plus fixed, non-patient-identifying constants -- never a patient ID, chart data,
    or the resident's raw message. Tested against the pure, network-free
    query-builder functions rather than intercepting live HTTP, per the
    approved design (app/reference_layer.py's own docstring on why these
    are split out)."""
    dailymed_query = _build_dailymed_query("Lisinopril")
    if dailymed_query.get("drug_name") != "Lisinopril":
        return False, f"expected drug_name='Lisinopril', got {dailymed_query!r}"
    if set(dailymed_query) != {"drug_name", "name_type", "pagesize"}:
        return False, f"expected only {{drug_name, name_type, pagesize}} keys, got {dailymed_query!r}"

    medlineplus_query = _build_medlineplus_query("Type 2 diabetes mellitus")
    if medlineplus_query.get("term") != "Type 2 diabetes mellitus":
        return False, f"expected term='Type 2 diabetes mellitus', got {medlineplus_query!r}"
    if set(medlineplus_query) != {"db", "term", "retmax"}:
        return False, f"expected only {{db, term, retmax}} keys, got {medlineplus_query!r}"

    return True, "both outbound query dicts contain only the bare name plus fixed constants, nothing patient-identifying"


def test_reference_layer_dailymed_real_match(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: when a real, extractable reference note exists, the tool
    must return it -- the positive half of "never fabricate, never omit
    what's really there." Real match against the live DailyMed API:
    Lisinopril's modern-format label has an extractable Warnings and
    Precautions excerpt."""
    if not _external_api_reachable(f"{_DAILYMED_BASE}/spls.json", _build_dailymed_query("Lisinopril")):
        return True, "SKIPPED: DailyMed unreachable (network/infra issue, not a regression) -- rerun once connectivity is restored"
    note = _fetch_dailymed_note("Lisinopril")
    if note.note_text is None:
        return False, "expected a real Warnings and Precautions excerpt for Lisinopril, got note_text=None"
    if note.source_url is None:
        return False, "expected a source_url alongside a real excerpt"
    return True, f"real DailyMed excerpt returned for Lisinopril: {note.note_text[:60]!r}..."


def test_reference_layer_medlineplus_real_match(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: same "return the real note when one exists" guarantee as
    the DailyMed case above, for MedlinePlus. Real match against the live
    MedlinePlus Health Topics Search API, using pid1's actual
    chart-sourced condition text."""
    if not _external_api_reachable(_MEDLINEPLUS_BASE, _build_medlineplus_query("Type 2 diabetes mellitus")):
        return True, "SKIPPED: MedlinePlus unreachable (network/infra issue, not a regression) -- rerun once connectivity is restored"
    # 2026-10-01: MedlinePlus started returning an EMPTY FullSummary for the
    # Type 2 diabetes topic (checked live; hypertension/COPD/asthma still carry
    # text). The tool then correctly says "no reference available", which is the
    # honest behavior, not a regression. The invariant under test is "return the
    # real note when one exists", so when the upstream record for the primary
    # condition is empty, check it on a condition whose record does have text,
    # still against the live API. Not a skip: some real summary must come back.
    for condition, keyword in (("Type 2 diabetes mellitus", "diabetes"), ("hypertension", "blood pressure")):
        raw = httpx.get(_MEDLINEPLUS_BASE, params=_build_medlineplus_query(condition), timeout=10.0).text
        if '<content name="FullSummary"/>' in raw:
            continue  # upstream record has no summary text; the honest None is correct here
        note = _fetch_medlineplus_note(condition)
        if note.note_text is None:
            return False, f"expected a real MedlinePlus summary for {condition}, got note_text=None"
        if keyword not in note.note_text.lower():
            return False, f"expected the summary to mention {keyword!r}, got: {note.note_text!r}"
        return True, f"real MedlinePlus summary returned for {condition}: {note.note_text[:60]!r}..."
    return False, "no checked MedlinePlus topic returned summary text upstream -- re-pick test conditions"


def test_reference_layer_dailymed_honest_no_reference(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: the negative half of "never fabricate, never omit what's
    really there" -- when no real excerpt is extractable, the tool must
    honestly say so, never fabricate or paraphrase from memory. Penicillin's
    real DailyMed label uses the older LOINC 34071-1 'WARNINGS SECTION'
    format with no extractable excerpt -- confirmed live before writing the
    extraction logic. This must come back as note_text=None, never a
    fabricated or paraphrased-from-memory substitute.

    Guarded by the same reachability probe as the real-match test above:
    without it, a DailyMed outage would make this test pass for the wrong
    reason (note_text=None because nothing responded, not because of the
    older-label format this test actually exists to check) -- a false
    pass that masks an outage is worse than a false fail that just needs
    a rerun."""
    if not _external_api_reachable(f"{_DAILYMED_BASE}/spls.json", _build_dailymed_query("Penicillin")):
        return True, "SKIPPED: DailyMed unreachable (network/infra issue, not a regression) -- rerun once connectivity is restored"
    note = _fetch_dailymed_note("Penicillin")
    if note.note_text is not None:
        return False, f"expected note_text=None for Penicillin's older-format label, got {note.note_text!r}"
    if note.source_url is not None:
        return False, f"expected source_url=None alongside note_text=None, got {note.source_url!r}"
    return True, "Penicillin's older-format label correctly produces an honest 'no reference available', not a fabrication"


def test_reference_layer_medlineplus_honest_no_reference(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: same honest-absence guarantee as the DailyMed case above,
    for MedlinePlus -- a condition with no real entry must come back as
    honestly empty, never fabricated. Same reachability guard as the
    DailyMed honest-no-reference test above, and for the same reason: an
    outage must not silently pass as
    "correctly found nothing"."""
    if not _external_api_reachable(_MEDLINEPLUS_BASE, _build_medlineplus_query("zzqxnotarealcondition12345")):
        return True, "SKIPPED: MedlinePlus unreachable (network/infra issue, not a regression) -- rerun once connectivity is restored"
    note = _fetch_medlineplus_note("zzqxnotarealcondition12345")
    if note.note_text is not None:
        return False, f"expected note_text=None for a nonsense condition, got {note.note_text!r}"
    return True, "a nonsense condition string correctly produces an honest 'no reference available'"


def test_reference_layer_mere_mention_does_not_trigger(fhir: FhirClient) -> tuple[bool, str]:
    """Invariant: a drug merely mentioned/read off an existing med list --
    with no action verb nearby -- must never fire the medication trigger.
    Not every medication name in a response should generate a
    DailyMed lookup, only an actual recommendation to start/adjust/stop."""
    fired = _medications_recommended("Her current medications are Metformin and Lisinopril.")
    if fired:
        return False, f"expected no medication trigger for a plain med-list mention, got {fired!r}"
    return True, "a plain mention of existing medications correctly does not trigger the DailyMed path"


def test_reference_layer_past_tense_history_does_not_trigger(fhir: FhirClient) -> tuple[bool, str]:
    """Boundary: an adversarial edge-case input deliberately crafted to
    look like a current recommendation while actually narrating history.
    User-specified adversarial negative case: a response describing a
    past medication action ('she was started on lisinopril last year,
    then discontinued due to a cough') must NOT trigger the medication
    path, since it narrates history rather than recommending a current
    action -- exactly the false-positive risk flagged in
    _medications_recommended's own docstring. Confirmed here structurally
    (word-boundary + morphological restriction to base/gerund forms,
    excluding simple past tense) rather than trusting the heuristic
    without a direct check against this exact sentence.

    Control: a genuine current recommendation in a similar shape
    ('Consider starting Lisinopril...') must still fire -- the fix
    narrows what matches, it doesn't silence the trigger altogether.
    """
    history_text = "She was started on lisinopril last year, then discontinued due to a cough."
    fired = _medications_recommended(history_text)
    if fired:
        return False, (
            "expected the medication trigger to NOT fire on past-tense historical narration, "
            f"got {fired!r} for: {history_text!r}"
        )

    control_text = "Consider starting Lisinopril 10mg daily for her hypertension."
    control_fired = _medications_recommended(control_text)
    if not control_fired:
        return False, f"control failed: a genuine current recommendation should still fire, got {control_fired!r}"

    return True, (
        "past-tense historical narration ('was started ... discontinued') correctly does NOT "
        "trigger the medication path, while a genuine current recommendation in a similar "
        "shape still does"
    )


# --- (h) W2 Checklist items 2-5: chunking, plausibility, bbox, RRF, citations ---


def test_corpus_chunking_produces_unique_ids_and_sections(fhir: FhirClient) -> tuple[bool, str]:
    chunks = load_corpus()
    if not chunks:
        return False, "load_corpus() returned no chunks -- corpus files missing or unreadable"
    chunk_ids = [c.chunk_id for c in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        return False, f"chunk_ids are not unique: {chunk_ids}"
    source_ids = {c.source_id for c in chunks}
    expected = {"hyperkalemia_management", "sepsis_screening", "antibiotic_cross_reactivity", "anticoagulation_reversal"}
    if source_ids != expected:
        return False, f"expected corpus source_ids {expected}, got {source_ids}"
    if any(not c.page_or_section for c in chunks):
        return False, "every chunk must have a non-empty page_or_section label"
    return True, f"{len(chunks)} chunks across {len(source_ids)} corpus files, all with unique ids and section labels"


def test_plausibility_check_catches_digit_transposition(fhir: FhirClient) -> tuple[bool, str]:
    """The PRD's own confirmed scenario: creatinine '1.1' misread as
    '141' (reference range 0.6-1.2 mg/dL) must be forced to unreadable
    regardless of the OCR/VLM-reported confidence."""
    if not is_implausible("141", "0.6-1.2 mg/dL"):
        return False, "141 vs reference range 0.6-1.2 should be flagged implausible"
    if apply_plausibility_check("141", "0.6-1.2 mg/dL", "high") != "unreadable":
        return False, "a high-confidence but implausible value must still be downgraded to unreadable"
    return True, "141 vs 0.6-1.2 mg/dL (the PRD's creatinine-transposition scenario) correctly forced to unreadable"


def test_plausibility_check_preserves_genuine_abnormal_values(fhir: FhirClient) -> tuple[bool, str]:
    """A real, clinically severe abnormal value (well outside the
    reference range, but not by an order of magnitude) must NOT be
    downgraded -- the whole point is distinguishing a transposition error
    from a genuine critical result."""
    cases = [("1.4", "0.6-1.2 mg/dL"), ("5.8", "3.5-5.0 mEq/L"), ("1.1", "0.6-1.2 mg/dL")]
    for value, ref in cases:
        if is_implausible(value, ref):
            return False, f"{value} vs {ref} is a genuine value and must not be flagged implausible"
    if apply_plausibility_check("5.8", "3.5-5.0 mEq/L", "high") != "high":
        return False, "a genuine high value's original confidence tier must be preserved, not downgraded"
    return True, "genuine abnormal/high values (1.4, 5.8, 1.1) correctly left un-flagged"


def test_bbox_resolution_matches_and_falls_back(fhir: FhirClient) -> tuple[bool, str]:
    page = OcrPage(
        page_number=1,
        words=[
            OcrWord(text="Creatinine", confidence=95.0, bbox=(0.1, 0.5, 0.3, 0.55)),
            OcrWord(text="1.1", confidence=90.0, bbox=(0.32, 0.5, 0.4, 0.55)),
            OcrWord(text="mg/dL", confidence=88.0, bbox=(0.42, 0.5, 0.5, 0.55)),
        ],
    )
    ocr_result = OcrResult(pages=[page])

    exact = resolve_bbox(ocr_result, 1, "1.1 mg/dL")
    if exact is None:
        return False, "expected a contiguous-run match for '1.1 mg/dL'"
    if not (exact[0] < exact[2] and exact[1] < exact[3]):
        return False, f"resolved bbox is degenerate: {exact}"

    single_word_fallback = resolve_bbox(ocr_result, 1, "Creatinine level")
    if single_word_fallback != page.words[0].bbox:
        return False, f"expected fallback to the first-token match's own bbox, got {single_word_fallback}"

    no_match = resolve_bbox(ocr_result, 1, "Potassium")
    if no_match is not None:
        return False, f"expected None for a quote with no match anywhere on the page, got {no_match}"

    missing_page = resolve_bbox(ocr_result, 2, "1.1")
    if missing_page is not None:
        return False, f"expected None for a page that doesn't exist, got {missing_page}"

    return True, "exact contiguous-run match, single-token fallback, and no-match/missing-page cases all resolve correctly"


def test_get_source_document_bytes_via_standard_api(fhir: FhirClient) -> tuple[bool, str]:
    """Regression pin for the click-to-source preview read path.
    get_source_document_bytes() (used by app/main.py's /documents/preview
    endpoint, the citation bounding-box overlay) reads the original upload
    through the Standard API under the resident's own token: resolve the
    numeric pid, list the patient's Source Document category, then
    GET /api/patient/:pid/document/:did. (It used the FHIR
    DocumentReference + Binary routes from 2026-09-22 until 2026-09-25;
    OpenEMR gates those behind admin/super and admin/users, so a
    non-admin Clinician got a 403 -- see AUDIT.md.) Exercises the real
    chain end to end against real calls.

    Uploads a small, deterministic test image via the real ingestion
    storage layer (store_extraction) rather than mocking the read path in
    isolation -- the bug this exists to catch (bundle_entries() returning
    already-unwrapped resource dicts, not {"resource": ...} wrappers,
    caught and fixed during this fix's own verification) was in the
    DocumentReference-Bundle-parsing logic itself, which a mocked
    FhirClient would trivially paper over. Content is fixed/deterministic
    (a plain white 20x20 PNG) so repeat runs dedupe cleanly via
    store_extraction's own content-hash idempotency check rather than
    accumulating a new document every run -- DuplicateDocumentError on a
    second run is the expected, correct outcome, not a failure."""
    patient_id = f.PID1_ALICE
    image = Image.new("RGB", (20, 20), color=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    original_content = buffer.getvalue()
    original_content_type = "image/png"

    source_doc_id, extracted_doc_id = compute_document_ids(
        patient_id, "lab_pdf", "unit_test_fixture.png", original_content
    )
    citation = DocumentCitation(page_number=1, quote_or_value="unit test fixture")
    extraction = ExtractionResult(
        patient_id=patient_id,
        doc_id=source_doc_id,
        doc_type="lab_pdf",
        extraction=LabPdfExtraction(
            results=[
                LabResultFact(
                    test_name=ExtractedField(value="Potassium", confidence="high", citation=citation),
                    value=ExtractedField(value="4.0", confidence="high", citation=citation),
                    unit=ExtractedField(value="mEq/L", confidence="high", citation=citation),
                    reference_range=ExtractedField(value="3.5-5.0", confidence="high", citation=citation),
                    collection_date=ExtractedField(value="2026-09-22", confidence="high", citation=citation),
                    abnormal_flag=ExtractedField(value="normal", confidence="high", citation=citation),
                )
            ]
        ),
        ocr_avg_confidence=99.0,
    )

    try:
        store_extraction(
            fhir, patient_id, original_content, original_content_type, source_doc_id, extracted_doc_id, extraction
        )
    except DuplicateDocumentError:
        pass  # already uploaded by a prior run -- fine, proceed straight to read-back

    content, mimetype = get_source_document_bytes(fhir, patient_id, source_doc_id)
    if content != original_content:
        return False, "read-back content does not match the originally uploaded bytes"
    if mimetype != original_content_type:
        return False, f"expected mimetype {original_content_type!r}, got {mimetype!r}"

    rasterized = rasterize_page(content, mimetype, 1)
    if not rasterized or rasterized[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "rasterize_page did not return a valid PNG from the read-back content"

    return True, (
        "pid resolve -> Source Document listing -> document fetch -> rasterize chain works end to end via the real Standard API"
    )


class _FakeStandardApiClient:
    """Minimal duck-typed stand-in for FhirClient, implementing only what
    get_source_document_bytes actually calls (get_standard_api for the pid
    and the category listing, get_standard_api_binary). A mock is right here
    because there is no way to produce a real soft-deleted document to test
    against -- no REST route deletes one (evals/prune_upload_fixture.py's own
    docstring). What's under test is a pure decision: an id that is absent
    from the server's listing is never fetched."""

    def __init__(self, listing: list[dict]) -> None:
        self._listing = listing
        self.fetched: list[str] = []

    def get_standard_api(self, path: str) -> dict | list:
        if "/document" in path:
            if not self._listing:
                raise FhirRequestError("not found", detail_code="not_found")  # OpenEMR's empty-category answer
            return self._listing
        return {"pid": 7}

    def get_standard_api_binary(self, path: str) -> bytes:
        self.fetched.append(path)
        return b"fake-bytes"


def test_get_source_document_bytes_refuses_deleted_document(fhir: FhirClient) -> tuple[bool, str]:
    """AUDIT.md §6.9's platform gap, in its Standard API form: OpenEMR's
    DocumentService::getAllAtPath lists `WHERE ... doc.deleted = 0`, so a
    soft-deleted (or other patient's) document is absent from the listing.
    get_source_document_bytes must then refuse it exactly like a genuinely
    missing one -- a real FhirRequestError(detail_code="not_found"), which
    /documents/preview maps to a 404 -- and must never fetch bytes for an id
    the listing did not return."""
    empty = _FakeStandardApiClient([])
    try:
        get_source_document_bytes(empty, "p1", "deleted_doc.png")
        return False, "expected FhirRequestError for a document absent from the patient's Source Document listing"
    except FhirRequestError as exc:
        if exc.detail_code != "not_found":
            return False, f"expected detail_code='not_found' (so /documents/preview 404s), got {exc.detail_code!r}"
    if empty.fetched:
        return False, f"bytes were fetched for a document the listing did not return: {empty.fetched}"

    live = _FakeStandardApiClient(
        [{"id": 41, "filename": "live_doc.png", "mimetype": "image/png"}, {"id": 40, "filename": "other.png", "mimetype": "image/png"}]
    )
    content, mimetype = get_source_document_bytes(live, "p1", "live_doc.png")
    if content != b"fake-bytes" or mimetype != "image/png" or live.fetched != ["/patient/7/document/41"]:
        return False, f"expected the listed document 41 to resolve normally, got {content!r}, {mimetype!r}, {live.fetched}"

    return True, (
        "a document absent from the listing is refused with a real not_found error and no fetch; "
        "a listed document resolves by its own id with its listed mimetype"
    )



def test_get_uploaded_documents_output_omitted_count_defaults_and_validates(fhir: FhirClient) -> tuple[bool, str]:
    """Part (a) of the context-blowup fix: GetUploadedDocumentsOutput's new
    omitted_count field must default to 0 (existing callers/tests that
    never set it are unaffected), round-trip a positive value correctly,
    and reject a negative value -- a negative omission count can never be
    a real result of app/ingestion/storage.py's `max(0, ...)` computation,
    so pydantic should catch a caller bug here rather than silently
    accepting nonsense."""
    default_output = GetUploadedDocumentsOutput(patient_id="p1", documents=[])
    if default_output.omitted_count != 0:
        return False, f"expected omitted_count to default to 0, got {default_output.omitted_count!r}"

    positive_output = GetUploadedDocumentsOutput(patient_id="p1", documents=[], omitted_count=4)
    if positive_output.omitted_count != 4:
        return False, f"expected omitted_count to round-trip 4, got {positive_output.omitted_count!r}"

    try:
        GetUploadedDocumentsOutput(patient_id="p1", documents=[], omitted_count=-1)
        return False, "expected a negative omitted_count to raise ValidationError"
    except ValidationError:
        pass

    return True, "omitted_count defaults to 0, round-trips a positive value, and rejects a negative one"


class _FakeFhirDocumentListClient:
    """Minimal duck-typed stand-in for FhirClient, implementing only what
    list_extracted_documents actually calls: get_standard_api (patient-pid
    resolution, then the document listing) and get_standard_api_binary
    (each stored extraction's JSON bytes). Used to test the
    MAX_UPLOADED_DOCUMENTS_RETURNED cap without needing a real patient with
    that many documents on staging -- same "genuinely needs a mock, no real
    fixture can produce this shape on demand" rationale as
    _FakeStandardApiClient above."""

    def __init__(self, pid: int, documents_by_id: dict[int, bytes]) -> None:
        self._pid = pid
        self._documents_by_id = documents_by_id

    def get_standard_api(self, path: str):
        if path == f"/patient/fake-patient-uuid":
            return {"pid": self._pid}
        if path.startswith(f"/patient/{self._pid}/document"):
            return [{"id": did, "filename": f"doc_{did}.json"} for did in self._documents_by_id]
        raise AssertionError(f"unexpected get_standard_api path: {path!r}")

    def get_standard_api_binary(self, path: str) -> bytes:
        for did, content in self._documents_by_id.items():
            if path == f"/patient/{self._pid}/document/{did}":
                return content
        raise AssertionError(f"unexpected get_standard_api_binary path: {path!r}")


def test_list_extracted_documents_caps_to_most_recent_and_reports_omitted_count(fhir: FhirClient) -> tuple[bool, str]:
    """Part (a) of the context-blowup fix -- root-caused live 2026-09-25 to
    an 84,331-input-token chart_data_worker turn, because
    list_extracted_documents returned every uploaded document's full
    extracted JSON for a patient with no bound at all. Builds
    MAX_UPLOADED_DOCUMENTS_RETURNED + 4 fake documents with ids 1..N and
    confirms: exactly the cap's worth come back, omitted_count is exactly
    4, and the returned ids are the HIGHEST (most recently uploaded), not
    the lowest -- a naive `docs[:CAP]` without the reverse-sort would
    silently keep the OLDEST documents and drop the newest, backwards from
    what a resident actually wants to see."""
    pid = 999
    total = MAX_UPLOADED_DOCUMENTS_RETURNED + 4
    documents_by_id: dict[int, bytes] = {}
    for did in range(1, total + 1):
        result = ExtractionResult(
            patient_id="fake-patient-uuid",
            doc_id=f"doc_{did}.png",
            extracted_doc_id=f"doc_{did}.json",
            doc_type="intake_form",
            extraction=IntakeFormExtraction(demographics=DemographicsFact()),
            ocr_avg_confidence=95.0,
        )
        documents_by_id[did] = result.model_dump_json().encode("utf-8")

    fake_fhir = _FakeFhirDocumentListClient(pid, documents_by_id)
    results, partial_failures, omitted_count = list_extracted_documents(fake_fhir, "fake-patient-uuid")

    if partial_failures:
        return False, f"expected no partial failures, got {partial_failures}"
    if len(results) != MAX_UPLOADED_DOCUMENTS_RETURNED:
        return False, f"expected exactly {MAX_UPLOADED_DOCUMENTS_RETURNED} results (the cap), got {len(results)}"
    if omitted_count != 4:
        return False, f"expected omitted_count == 4 (total {total} - cap {MAX_UPLOADED_DOCUMENTS_RETURNED}), got {omitted_count}"

    returned_ids = {int(r.extracted_doc_id.removeprefix("doc_").removesuffix(".json")) for r in results}
    expected_ids = set(range(total - MAX_UPLOADED_DOCUMENTS_RETURNED + 1, total + 1))
    if returned_ids != expected_ids:
        return False, f"expected the {MAX_UPLOADED_DOCUMENTS_RETURNED} HIGHEST ids {expected_ids}, got {returned_ids}"

    return True, (
        f"list_extracted_documents correctly capped {total} documents to the {MAX_UPLOADED_DOCUMENTS_RETURNED} "
        f"most recent (highest id) and reported omitted_count={omitted_count}"
    )


def test_rrf_fusion_combines_dense_and_sparse_rankings(fhir: FhirClient) -> tuple[bool, str]:
    chunk_a = GuidelineChunk(source_id="doc", chunk_id="doc:0", text="a", page_or_section="s")
    chunk_b = GuidelineChunk(source_id="doc", chunk_id="doc:1", text="b", page_or_section="s")
    chunk_c = GuidelineChunk(source_id="doc", chunk_id="doc:2", text="c", page_or_section="s")

    # chunk_a: #1 dense, absent from sparse. chunk_b: #1 sparse, #2 dense.
    # chunk_c: only in dense, ranked last. RRF should place chunk_b first
    # (agreement across both lists beats a single #1 rank).
    dense = [(chunk_a, 0.9), (chunk_b, 0.8), (chunk_c, 0.1)]
    sparse = [(chunk_b, 5.0)]

    fused = reciprocal_rank_fusion(dense, sparse, top_k=3)
    if fused[0].chunk_id != "doc:1":
        return False, f"expected chunk_b (agreement across both lists) ranked first, got {[c.chunk_id for c in fused]}"
    if {c.chunk_id for c in fused} != {"doc:0", "doc:1", "doc:2"}:
        return False, f"expected all three chunks present, got {[c.chunk_id for c in fused]}"
    return True, f"RRF correctly ranks the dual-list agreement first: {[c.chunk_id for c in fused]}"


def test_build_safe_query_scrubs_every_phrase_and_drops_empty(fhir: FhirClient) -> tuple[bool, str]:
    """Confirms the PHI/identifier-scrub backstop (this module's own
    docstring: "a wall, not a request") applies to EVERY returned phrase,
    not just the first, and that an empty phrase (the model's own "no
    topic here" signal) is dropped rather than passed through to the
    retriever. (This call is deliberately NOT asked for temperature=0 --
    the installed SDK's Messages.create() has no such parameter; see
    build_safe_query's own docstring.)"""

    class _FakeLLM:
        def __init__(self) -> None:
            self.create_message_calls: list[dict] = []

        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            self.create_message_calls.append({"tool_choice": tool_choice})
            call = LLMToolCall(
                id="call1",
                name="extract_query",
                input={
                    "queries": [
                        "fever sepsis severity",
                        "antibiotic allergy cross-reactivity 12345678-1234-1234-1234-123456789012",
                        "",
                    ]
                },
            )
            return LLMTurnResponse(text="", tool_calls=[call], input_tokens=10, output_tokens=5, raw_for_trace=[])

    fake_llm = _FakeLLM()
    trace = TurnTrace(client=None, span=None, correlation_id="test-query-correlation-id")
    queries, input_tokens, output_tokens = build_safe_query(
        "She has a fever, and we're considering an antibiotic given her allergy.",
        llm=fake_llm, model="test-model", trace=trace,
    )

    if len(fake_llm.create_message_calls) != 1:
        return False, f"expected exactly 1 LLM call, got {len(fake_llm.create_message_calls)}"
    if queries != ["fever sepsis severity", "antibiotic allergy cross-reactivity"]:
        return False, (
            f"expected the UUID scrubbed from the second phrase and the empty third phrase dropped, "
            f"got {queries!r}"
        )

    return True, "build_safe_query scrubs every phrase and drops empty ones"


def test_build_safe_query_caps_at_three_phrases(fhir: FhirClient) -> tuple[bool, str]:
    """The model is asked for at most 3 phrases, but nothing about the tool
    schema prevents it from returning more -- this backstop must cap the
    result regardless of what the model actually returns."""

    class _FakeLLM:
        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            call = LLMToolCall(
                id="call1", name="extract_query",
                input={"queries": ["topic one", "topic two", "topic three", "topic four", "topic five"]},
            )
            return LLMTurnResponse(text="", tool_calls=[call], input_tokens=10, output_tokens=5, raw_for_trace=[])

    trace = TurnTrace(client=None, span=None, correlation_id="test-query-correlation-id")
    queries, _input_tokens, _output_tokens = build_safe_query(
        "a message spanning many topics", llm=_FakeLLM(), model="test-model", trace=trace,
    )
    if len(queries) != 3:
        return False, f"expected at most 3 query phrases regardless of how many the model returned, got {queries!r}"
    return True, "build_safe_query caps at 3 query phrases even if the model returns more"


def test_guideline_retriever_merges_candidates_from_multiple_queries_before_reranking(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: a genuinely two-topic question (fever/sepsis-severity
    AND antibiotic/allergy selection) depended entirely on one concept-
    extraction call picking a single phrase representing both topics at
    once -- if it emphasized only one, the other topic's guideline was
    never even a candidate, no matter how well reranking worked
    afterward. Confirms GuidelineRetriever.retrieve() runs a separate
    dense+sparse+fusion pass per query phrase and merges the fused
    candidates (deduped by chunk_id) BEFORE reranking. Reranking is now
    once PER PHRASE over that merged pool (each chunk keeps its best
    score), not once against the phrases joined with " ; " -- 2026-09-25:
    the joined rerank measurably diluted the cross-encoder and pushed a
    genuine two-topic question under the relevance floor (see
    evals/allergy_retrieval_tests.py and retriever.py's comment).
    Constructs a GuidelineRetriever without its real (expensive,
    self-hosted-model-loading) __init__, injecting fakes for _dense/
    _sparse/_reranker directly -- the same technique this class's own
    lack of a constructor injection seam requires for any test at this
    level."""
    chunk_a = GuidelineChunk(source_id="sepsis_screening", chunk_id="sepsis_screening:0", text="fever sepsis content", page_or_section="(preamble)")
    chunk_b = GuidelineChunk(source_id="antibiotic_cross_reactivity", chunk_id="antibiotic_cross_reactivity:0", text="antibiotic allergy content", page_or_section="(preamble)")

    class _FakeIndex:
        def __init__(self, results_by_query: dict) -> None:
            self._results_by_query = results_by_query

        def search(self, query: str, top_k: int) -> list:
            return self._results_by_query.get(query, [])

    class _FakeReranker:
        def __init__(self) -> None:
            self.rerank_calls: list[tuple] = []

        def rerank(self, query: str, candidates: list, top_k: int) -> list:
            self.rerank_calls.append((query, list(candidates)))
            return [(c, 1.0 - i * 0.1) for i, c in enumerate(candidates[:top_k])]

    retriever = object.__new__(GuidelineRetriever)
    retriever._dense = _FakeIndex({"fever sepsis severity": [(chunk_a, 0.9)], "antibiotic allergy": [(chunk_b, 0.9)]})
    retriever._sparse = _FakeIndex({"fever sepsis severity": [], "antibiotic allergy": []})
    fake_reranker = _FakeReranker()
    retriever._reranker = fake_reranker

    results = retriever.retrieve(["fever sepsis severity", "antibiotic allergy"])

    source_ids = {r.chunk.source_id for r in results}
    if source_ids != {"sepsis_screening", "antibiotic_cross_reactivity"}:
        return False, f"expected both topics' chunks retrieved (merged before reranking), got source_ids={source_ids}"

    if [q for q, _ in fake_reranker.rerank_calls] != ["fever sepsis severity", "antibiotic allergy"]:
        return False, f"expected one rerank call per phrase (never the joined query), got {[q for q, _ in fake_reranker.rerank_calls]!r}"
    for reranked_query, reranked_candidates in fake_reranker.rerank_calls:
        if {c.chunk_id for c in reranked_candidates} != {"sepsis_screening:0", "antibiotic_cross_reactivity:0"}:
            return False, f"expected the full merged pool in every rerank call, got {[c.chunk_id for c in reranked_candidates]} for {reranked_query!r}"

    return True, "candidates from multiple query phrases are merged into one pool, then reranked once per phrase"


def test_guideline_retriever_returns_empty_for_no_queries(fhir: FhirClient) -> tuple[bool, str]:
    retriever = object.__new__(GuidelineRetriever)
    retriever._dense = None  # never touched -- retrieve() must short-circuit before using it
    retriever._sparse = None
    retriever._reranker = None
    results = retriever.retrieve([])
    if results != []:
        return False, f"expected an empty query list to return no evidence without touching the index/reranker, got {results!r}"
    return True, "an empty query list returns no evidence without ever touching the dense/sparse index or reranker"


def _sample_extraction_result() -> ExtractionResult:
    citation = DocumentCitation(page_number=1, bbox=(0.1, 0.1, 0.2, 0.2), quote_or_value="1.1 mg/dL")
    field = lambda value, confidence="high": ExtractedField(value=value, confidence=confidence, citation=citation)  # noqa: E731
    lab = LabResultFact(
        test_name=field("Creatinine"),
        value=field("1.1"),
        unit=field("mg/dL"),
        reference_range=field("0.6-1.2"),
        collection_date=field("2026-09-20"),
        abnormal_flag=field("normal"),
    )
    return ExtractionResult(
        patient_id="test-patient",
        doc_id="abc_source_lab.png",
        extracted_doc_id="abc_extracted.json",
        doc_type="lab_pdf",
        extraction=LabPdfExtraction(results=[lab]),
        ocr_avg_confidence=91.0,
    )


def test_document_citations_translate_per_field(fhir: FhirClient) -> tuple[bool, str]:
    citations = document_citations([_sample_extraction_result()])
    if len(citations) != 6:
        return False, f"expected 6 citations (one per LabResultFact field), got {len(citations)}"
    if not all(c.source_type == "uploaded_document" for c in citations):
        return False, "every citation from an ExtractionResult must be source_type='uploaded_document'"
    if not all(c.source_id == "abc_source_lab.png" for c in citations):
        return False, (
            "source_id must be the ORIGINAL source scan's document id (doc_id), not extracted_doc_id -- "
            "2026-09-24 click-to-source fix: GET /documents/preview can only ever resolve a document by "
            "its original scan filename"
        )
    values = {c.quote_or_value for c in citations}
    if "1.1" not in values or "Creatinine" not in values:
        return False, f"expected field values among citations, got {values}"
    return True, "one Citation per ExtractedField, correctly source_type='uploaded_document' pointing at the original scan doc"


def test_guideline_citations_translate_chunk_shape(fhir: FhirClient) -> tuple[bool, str]:
    chunk = GuidelineChunk(source_id="sepsis_screening", chunk_id="sepsis_screening:2", text="qSOFA criteria...", page_or_section="Common screening tools")
    citations = guideline_citations([RetrievedEvidence(chunk=chunk, rerank_score=0.87)])
    if len(citations) != 1:
        return False, f"expected exactly 1 citation, got {len(citations)}"
    c = citations[0]
    if c.source_type != "guideline" or c.source_id != "sepsis_screening" or c.field_or_chunk_id != "sepsis_screening:2":
        return False, f"citation fields don't match the source chunk: {c}"
    return True, "one Citation per retrieved chunk, correctly source_type='guideline' with the chunk's own id/section"


def test_chart_citations_dedupes_and_scopes_to_active_patient(fhir: FhirClient) -> tuple[bool, str]:
    snapshot = GetPatientSnapshotOutput(
        patient_id="p1",
        name="Test Patient",
        birth_date=None,
        gender=None,
        conditions=[ConditionFact(text="Diabetes", category="problem-list-item", clinical_status=None, source_resource="Condition/1")],
        medications=[],
        allergies=[],
        chart_is_empty=False,
        duplicate_warnings=[],
    )
    other_patient_snapshot = GetPatientSnapshotOutput(
        patient_id="p2",
        name="Someone Else",
        birth_date=None,
        gender=None,
        conditions=[ConditionFact(text="Asthma", category="problem-list-item", clinical_status=None, source_resource="Condition/2")],
        medications=[],
        allergies=[],
        chart_is_empty=False,
        duplicate_warnings=[],
    )
    records = [
        ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p1", output=snapshot),
        ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p1", output=snapshot),  # same fact fetched twice
        ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p2", output=other_patient_snapshot),
    ]
    chart_facts = ChartGatherResult(
        draft_text="draft", turn_records=records, tool_calls=[], updated_messages=[],
        total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
    )
    citations = chart_citations(chart_facts, patient_id="p1")
    source_ids = [c.source_id for c in citations]
    if source_ids != ["Condition/1"]:
        return False, f"expected exactly one deduped citation scoped to patient p1, got {source_ids}"
    return True, "chart_citations correctly dedupes a repeated fetch and excludes a different patient's records"


def test_chart_citations_populates_all_five_fields_for_fhir_resource(fhir: FhirClient) -> tuple[bool, str]:
    """Gate 3 audit gap: fhir_resource citations used to leave page_or_section
    and field_or_chunk_id permanently None, unlike the other two domains.
    Every real source_resource string this codebase produces is
    "{ResourceType}/{id}" (confirmed by reading every source_resource=...
    call site in app/tools.py) -- field_or_chunk_id is that resource type,
    and page_or_section is the tool call that surfaced it, both real
    structural information, not fabricated placeholders."""
    snapshot = GetPatientSnapshotOutput(
        patient_id="p1", name="Test Patient", birth_date=None, gender=None,
        conditions=[ConditionFact(text="Diabetes", category="problem-list-item", clinical_status=None, source_resource="Condition/1")],
        medications=[], allergies=[], chart_is_empty=False, duplicate_warnings=[],
    )
    records = [ToolCallRecord(tool_name="get_patient_snapshot", patient_id="p1", output=snapshot)]
    chart_facts = ChartGatherResult(
        draft_text="draft", turn_records=records, tool_calls=[], updated_messages=[],
        total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
    )
    citations = chart_citations(chart_facts, patient_id="p1")
    if len(citations) != 1:
        return False, f"test fixture problem: expected 1 citation, got {len(citations)}"
    c = citations[0]
    if c.field_or_chunk_id != "Condition":
        return False, f"expected field_or_chunk_id='Condition' (the resource type), got {c.field_or_chunk_id!r}"
    if c.page_or_section != "get_patient_snapshot":
        return False, f"expected page_or_section='get_patient_snapshot' (the surfacing tool call), got {c.page_or_section!r}"
    return True, "fhir_resource citations now populate field_or_chunk_id (resource type) and page_or_section (surfacing tool call)"


def test_citation_schema_validation_rejects_invalid_shapes(fhir: FhirClient) -> tuple[bool, str]:
    """Gate 3 audit gap: every existing schema-rejection test targets the
    ingestion-layer citation shapes (DocumentCitation/ExtractedField/
    LabResultFact), never app/graph/citations.py's own Citation model --
    the actual 5-field shape (source_type, source_id, page_or_section,
    field_or_chunk_id, quote_or_value) that reaches the resident-facing
    citations list. Confirms Citation itself schema-validates: a missing
    required field and an invalid source_type are both rejected, and a
    genuinely valid construction (all 5 core fields populated) succeeds
    and round-trips through model_dump()."""
    for missing_kwargs in (
        {"source_type": "fhir_resource", "page_or_section": "t", "field_or_chunk_id": "Condition", "quote_or_value": "Condition/1"},  # missing source_id
        {"source_type": "fhir_resource", "source_id": "Condition/1", "page_or_section": "t", "field_or_chunk_id": "Condition"},  # missing quote_or_value
        {"source_id": "Condition/1", "page_or_section": "t", "field_or_chunk_id": "Condition", "quote_or_value": "Condition/1"},  # missing source_type
        {"source_type": "fhir_resource", "source_id": "Condition/1", "field_or_chunk_id": "Condition", "quote_or_value": "Condition/1"},  # missing page_or_section
        {"source_type": "fhir_resource", "source_id": "Condition/1", "page_or_section": "t", "quote_or_value": "Condition/1"},  # missing field_or_chunk_id
    ):
        try:
            Citation(**missing_kwargs)
            return False, f"Citation should reject a missing required field, accepted: {missing_kwargs}"
        except ValidationError:
            pass

    try:
        Citation(source_type="not_a_real_source_type", source_id="x", page_or_section="p", field_or_chunk_id="f", quote_or_value="y")
        return False, "Citation should reject a source_type outside {fhir_resource, uploaded_document, guideline}"
    except ValidationError:
        pass

    valid = Citation(
        source_type="uploaded_document", source_id="doc1", page_or_section="1",
        field_or_chunk_id="results[0].value", quote_or_value="7.8", doc_label="Lab report",
    )
    dumped = valid.model_dump()
    for field_name in ("source_type", "source_id", "page_or_section", "field_or_chunk_id", "quote_or_value"):
        if field_name not in dumped:
            return False, f"Citation.model_dump() is missing the {field_name!r} field"

    return True, (
        "Citation rejects a missing required field and an invalid source_type; a genuinely valid "
        "citation constructs and round-trips with all 5 core fields present"
    )


def test_filter_citations_to_stated_facts_drops_uncited_document_fields(fhir: FhirClient) -> tuple[bool, str]:
    """Citation-precision fix (KEY_METRICS.md's documented gap): a response
    stating only one extracted value should keep only that value's
    uploaded_document citation, not one per field of the whole document --
    while fhir_resource and guideline citations, whose own quote_or_value
    is never expected to appear verbatim in resident-facing prose the way
    an extracted field's value is, pass through untouched regardless."""
    # _sample_extraction_result()'s one LabResultFact: test_name=
    # "Creatinine", value="1.1", unit="mg/dL", reference_range="0.6-1.2",
    # collection_date="2026-09-20", abnormal_flag="normal" -- 6 fields, 6
    # citations from document_citations().
    document_cites = document_citations([_sample_extraction_result()])
    if len(document_cites) != 6:
        return False, f"test fixture problem: expected 6 document citations, got {len(document_cites)}"
    fhir_cite = Citation(
        source_type="fhir_resource", source_id="Condition/1", page_or_section="get_patient_snapshot",
        field_or_chunk_id="Condition", quote_or_value="Condition/1",
    )
    guideline_cite = Citation(
        source_type="guideline",
        source_id="hyperkalemia_management",
        page_or_section="Treatment",
        field_or_chunk_id="hyperkalemia_management#0",
        quote_or_value="Consider insulin and glucose for severe hyperkalemia.",
    )
    all_citations = [c.model_dump() for c in document_cites] + [fhir_cite.model_dump(), guideline_cite.model_dump()]

    # Deliberately states ONLY the "1.1" value -- never "Creatinine",
    # "mg/dL", "0.6-1.2", "2026-09-20", or "normal" -- so exactly one of
    # the 6 document citations should survive.
    response = "The result was 1.1, unremarkable otherwise."
    filtered = filter_citations_to_stated_facts(all_citations, response)

    surviving_document_values = {c["quote_or_value"] for c in filtered if c["source_type"] == "uploaded_document"}
    if surviving_document_values != {"1.1"}:
        return False, f"expected only the cited value '1.1' to survive, got {surviving_document_values}"

    surviving_source_types = {c["source_type"] for c in filtered}
    if "fhir_resource" not in surviving_source_types or "guideline" not in surviving_source_types:
        return False, (
            f"fhir_resource/guideline citations must never be filtered by this check, "
            f"got surviving source_types={surviving_source_types}"
        )

    return True, (
        "only the one extracted value the response actually states keeps its citation "
        "(5 of 6 document citations dropped); fhir_resource/guideline citations pass through unfiltered"
    )


def test_filter_citations_to_stated_facts_applies_to_later_turn_get_uploaded_documents_citations(
    fhir: FhirClient,
) -> tuple[bool, str]:
    """Interaction test, fix/combined-final only (2026-09-25): fix/click-
    to-source's chart_citations() rewrite makes a GetUploadedDocumentsOutput
    fact's citation properly source_type="uploaded_document" (it used to
    fall into the generic fhir_resource bucket via _walk_source_resources,
    which filter_citations_to_stated_facts() never touches at all) --
    meaning the citation-precision filter now applies to the later-turn
    (get_uploaded_documents, a document read back on a turn AFTER the one
    it was uploaded on) path for the first time. Both changes are
    individually correct and independently tested on their own branches
    (test_filter_citations_to_stated_facts_drops_uncited_document_fields
    above covers the same-turn document_citations() path), but the two
    were never exercised TOGETHER until this branch actually merges them.
    This proves the composition: a response stating only one field of a
    later-turn-read-back document keeps only that field's citation, the
    same precision guarantee the same-turn path already had."""
    facts = [
        UploadedDocumentFactSummary(
            field_name="results[0].test_name", value="Potassium", confidence="high",
            source_resource="document:doc1#results[0].test_name", page_number=1,
        ),
        UploadedDocumentFactSummary(
            field_name="results[0].value", value="5.9", confidence="high",
            source_resource="document:doc1#results[0].value", page_number=1,
        ),
        UploadedDocumentFactSummary(
            field_name="results[0].unit", value="mmol/L", confidence="high",
            source_resource="document:doc1#results[0].unit", page_number=1,
        ),
    ]
    output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[UploadedDocumentSummary(doc_id="doc1", doc_type="lab_pdf", facts=facts, ocr_avg_confidence=95.0, upload_rank=0)],
    )
    record = ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)
    chart_facts = ChartGatherResult(
        draft_text="", turn_records=[record], tool_calls=[], updated_messages=[],
        total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
    )

    raw_citations = chart_citations(chart_facts, "p1")
    if len(raw_citations) != 3:
        return False, f"test fixture problem: expected 3 raw citations from chart_citations(), got {len(raw_citations)}"
    if not all(c.source_type == "uploaded_document" for c in raw_citations):
        return False, (
            f"expected all 3 to already be source_type='uploaded_document' (fix/click-to-source's "
            f"chart_citations() rewrite), got {[c.source_type for c in raw_citations]} -- if this "
            f"fails, the interaction under test here no longer exists"
        )

    # States ONLY the value -- never "Potassium" or "mmol/L".
    response = "The result was 5.9, drawn this morning."
    filtered = filter_citations_to_stated_facts([c.model_dump() for c in raw_citations], response)
    surviving_values = {c["quote_or_value"] for c in filtered}
    if surviving_values != {"5.9"}:
        return False, f"expected only the stated value '5.9' to survive filtering, got {surviving_values}"

    return True, (
        "filter_citations_to_stated_facts correctly narrows chart_citations()'s later-turn "
        "get_uploaded_documents citations (now properly typed uploaded_document by fix/click-to-"
        "source) down to the one field the response actually states"
    )


def _lab_extraction_result(*, doc_id: str, value: str, test_name: str = "Potassium", unit: str = "mEq/L",
                            reference_range: str = "3.5-5.0", collection_date: str = "2026-09-24",
                            abnormal_flag: str = "normal") -> ExtractionResult:
    citation = DocumentCitation(page_number=1, quote_or_value=f"{test_name} {value} {unit}")
    field = lambda v: ExtractedField(value=v, confidence="high", citation=citation)  # noqa: E731
    lab = LabResultFact(
        test_name=field(test_name), value=field(value), unit=field(unit),
        reference_range=field(reference_range), collection_date=field(collection_date),
        abnormal_flag=field(abnormal_flag),
    )
    return ExtractionResult(
        patient_id="test-patient", doc_id=doc_id, extracted_doc_id=f"{doc_id}_extracted.json",
        doc_type="lab_pdf", extraction=LabPdfExtraction(results=[lab]), ocr_avg_confidence=99.0,
    )


def test_filter_citations_to_stated_facts_distinguishes_documents_differing_only_in_value(
    fhir: FhirClient,
) -> tuple[bool, str]:
    """The real regression (uploaded_document_multiple_documents_each_cited_
    distinctly, found live): two lab documents sharing every boilerplate
    field (test_name/unit/reference_range/collection_date/abnormal_flag) and
    differing ONLY in value. A response stating just the referenced
    document's value must not also drag in the other document's citations
    merely because their shared fields' text happens to appear too."""
    doc_a = _lab_extraction_result(doc_id="doc_a_source.png", value="4.4")
    doc_b = _lab_extraction_result(doc_id="doc_b_source.png", value="7.8")
    citations = [c.model_dump() for c in document_citations([doc_a, doc_b])]
    if len(citations) != 12:
        return False, f"test fixture problem: expected 12 citations (6 per document), got {len(citations)}"

    response = "Potassium was 7.8 mEq/L, reference range 3.5-5.0, collected 2026-09-24, flag normal."
    filtered = filter_citations_to_stated_facts(citations, response)

    surviving_source_ids = {c["source_id"] for c in filtered}
    if surviving_source_ids != {"doc_b_source.png"}:
        return False, (
            f"expected only the referenced document (doc_b_source.png) cited, got "
            f"source_ids={surviving_source_ids} -- the unreferenced document's shared-field "
            "citations must not ride along"
        )
    surviving_values = {c["quote_or_value"] for c in filtered}
    if surviving_values != {"Potassium", "7.8", "mEq/L", "3.5-5.0", "2026-09-24", "normal"}:
        return False, f"expected all 6 of the referenced document's stated fields, got {surviving_values}"

    return True, (
        "the referenced document's shared-field citations survive; the other document's "
        "identical-text shared fields are correctly dropped along with its own"
    )


def test_filter_citations_to_stated_facts_keeps_both_identical_documents(fhir: FhirClient) -> tuple[bool, str]:
    """Tie-break case: two documents with genuinely IDENTICAL values (a real
    duplicate upload) have no distinctive value to tell them apart by text
    at all. Rather than arbitrarily dropping one (or both -- an information-
    loss failure), both must survive together when their shared value is
    stated."""
    doc_x = _lab_extraction_result(doc_id="doc_x_source.png", value="4.4")
    doc_y = _lab_extraction_result(doc_id="doc_y_source.png", value="4.4")
    citations = [c.model_dump() for c in document_citations([doc_x, doc_y])]

    response = "Potassium was 4.4 mEq/L, reference range 3.5-5.0, collected 2026-09-24, flag normal."
    filtered = filter_citations_to_stated_facts(citations, response)

    surviving_source_ids = {c["source_id"] for c in filtered}
    if surviving_source_ids != {"doc_x_source.png", "doc_y_source.png"}:
        return False, (
            f"expected BOTH identical documents to survive together (neither can be "
            f"individually distinguished by text), got source_ids={surviving_source_ids}"
        )

    return True, "two genuinely identical documents survive together rather than one being arbitrarily dropped"


def test_filter_citations_to_stated_facts_handles_two_different_document_types(fhir: FhirClient) -> tuple[bool, str]:
    """Control case: a lab document and an intake-form document share no
    field values at all, so each is trivially "distinctive" from the
    other -- confirms the per-document distinctiveness rule doesn't change
    behavior for the ordinary (non-overlapping) multi-document case."""
    lab_doc = _lab_extraction_result(doc_id="lab_source.png", value="4.4")
    citation = DocumentCitation(page_number=1, quote_or_value="Jordan Rivera")
    intake_doc = ExtractionResult(
        patient_id="test-patient", doc_id="intake_source.png", extracted_doc_id="intake_extracted.json",
        doc_type="intake_form",
        extraction=IntakeFormExtraction(
            demographics=DemographicsFact(name=ExtractedField(value="Jordan Rivera", confidence="high", citation=citation)),
            chief_concern=ExtractedField(value="Chest pain", confidence="high", citation=citation),
        ),
        ocr_avg_confidence=97.0,
    )
    citations = [c.model_dump() for c in document_citations([lab_doc, intake_doc])]

    response = "Jordan Rivera's potassium was 4.4 mEq/L; chief concern was chest pain."
    filtered = filter_citations_to_stated_facts(citations, response)

    surviving_source_ids = {c["source_id"] for c in filtered}
    if surviving_source_ids != {"lab_source.png", "intake_source.png"}:
        return False, (
            f"expected both documents' stated fields cited (they share no values at all), got "
            f"source_ids={surviving_source_ids}"
        )
    surviving_values = {c["quote_or_value"] for c in filtered}
    if not {"4.4", "Jordan Rivera", "Chest pain"} <= surviving_values:
        return False, f"expected each document's stated distinctive value present, got {surviving_values}"

    return True, "two documents of different types, with no shared field values, are each cited normally"


def _graph_result_with_citations(citations: list[dict]) -> GraphTurnResult:
    return GraphTurnResult(
        correlation_id="test", response_text="", verification_passed=True, passed_domain_constraint=True,
        flagged_claims=[], enforced_warnings=[], workers_invoked=[], routing_rationale="",
        citations=citations,
    )


def test_check_no_citation_misattribution_rejects_shared_field_only_citation(fhir: FhirClient) -> tuple[bool, str]:
    """evals/cases.py's _check_no_citation_misattribution is a pure function
    (not a closure over eval-local state), specifically so the exact
    pre-fix bug pattern (found live via
    uploaded_document_multiple_documents_each_cited_distinctly, before
    app/graph/citations.py's distinctiveness fix, commit bb0a446) can be
    reproduced here directly against fabricated citations, without a real
    FHIR/LLM call: the other document cited via shared boilerplate fields
    only, never its own distinctive value -- must still fail under the
    new misattribution-based rule, not just the old "never cited at all"
    rule it replaces."""
    citations = [
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "Potassium"},
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "7.8"},
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "mEq/L"},
        # doc_a cited via a shared boilerplate field only ("Potassium") --
        # never its own distinctive value ("4.4") -- exactly the pre-fix
        # collapse bug.
        {"source_type": "uploaded_document", "source_id": "doc_a", "quote_or_value": "Potassium"},
    ]
    result = _graph_result_with_citations(citations)
    passed, reason = _check_no_citation_misattribution(
        result, referenced_doc="doc_b", other_doc="doc_a", referenced_value="7.8", other_value="4.4",
    )
    if passed:
        return False, f"expected the shared-field-only collapse pattern to still fail, got passed=True ({reason})"
    if "doc_a" not in reason or "4.4" not in reason:
        return False, f"expected the failure reason to name the offending document and its missing distinctive value, got: {reason!r}"
    return True, "the original collapse bug (other document cited via shared fields only) still fails under the new rule"


def test_check_no_citation_misattribution_rejects_wrong_document_attribution(fhir: FhirClient) -> tuple[bool, str]:
    """A citation carrying doc_a's own distinctive value ("4.4") but
    attributed to doc_b's source_id is a genuine misattribution -- the
    specific new failure mode this rule's rewrite exists to catch, not
    just a naming of the pre-existing collapse bug above."""
    citations = [
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "7.8"},
        # "4.4" belongs to doc_a, but is attributed to doc_b here.
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "4.4"},
    ]
    result = _graph_result_with_citations(citations)
    passed, reason = _check_no_citation_misattribution(
        result, referenced_doc="doc_b", other_doc="doc_a", referenced_value="7.8", other_value="4.4",
    )
    if passed:
        return False, f"expected a misattributed value to fail, got passed=True ({reason})"
    if "misattributed" not in reason.lower():
        return False, f"expected the failure reason to identify this as a misattribution, got: {reason!r}"
    return True, "a value attributed to the wrong document's source_id is correctly rejected as a misattribution"


def test_check_no_citation_misattribution_accepts_legitimate_dual_citation(fhir: FhirClient) -> tuple[bool, str]:
    """The behavior change this rewrite is FOR: a response that genuinely,
    correctly discusses both documents (each value attributed to its own
    document) must now pass -- the old rule ("the other document is never
    cited") would have failed this legitimate case."""
    citations = [
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "Potassium"},
        {"source_type": "uploaded_document", "source_id": "doc_b", "quote_or_value": "7.8"},
        {"source_type": "uploaded_document", "source_id": "doc_a", "quote_or_value": "4.4"},
    ]
    result = _graph_result_with_citations(citations)
    passed, reason = _check_no_citation_misattribution(
        result, referenced_doc="doc_b", other_doc="doc_a", referenced_value="7.8", other_value="4.4",
    )
    if not passed:
        return False, f"expected a legitimate dual-document citation (each value correctly attributed) to pass, got: {reason}"
    return True, "a response correctly discussing both documents, with every value attributed to its own document, passes"


def test_translate_citations_deduplicates_same_source_and_value(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: the same stored document's same field can reach
    translate_citations() TWICE in one turn -- once via chart_citations()'s
    GetUploadedDocumentsOutput branch (get_uploaded_documents, a later-turn
    read-back that always returns every stored document) and again via
    document_citations() (a same-turn intake_extractor_node extraction of
    the exact same document, e.g. via uploaded_document_ref) -- each with
    its own field_or_chunk_id for the identical underlying fact. Two
    citation chips for the same (document, value) is redundant, not two
    distinct citable things."""
    dup_doc_id = "dup_doc_source.png"
    fact = UploadedDocumentFactSummary(
        field_name="results[0].value", value="7.8", confidence="high",
        source_resource=f"document:{dup_doc_id}#results[0].value",
    )
    output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[UploadedDocumentSummary(doc_id=dup_doc_id, doc_type="lab_pdf", facts=[fact], ocr_avg_confidence=99.0, upload_rank=0)],
    )
    record = ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)
    chart_facts = ChartGatherResult(
        draft_text="", turn_records=[record], tool_calls=[], updated_messages=[],
        total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
    )
    # Same document, same value ("7.8"), via the OTHER path -- document_
    # citations()'s own field_or_chunk_id (truncated DocumentCitation quote)
    # necessarily differs from chart_citations()'s ("results[0].value"),
    # even though it's the identical underlying fact.
    extracted = _lab_extraction_result(doc_id=dup_doc_id, value="7.8")

    citations = translate_citations(chart_facts=chart_facts, extracted_facts=[extracted], retrieved_evidence=[], patient_id="p1")

    value_citations = [c for c in citations if c["source_id"] == dup_doc_id and c["quote_or_value"] == "7.8"]
    if len(value_citations) != 1:
        return False, (
            f"expected exactly 1 citation for (source_id={dup_doc_id!r}, value='7.8') after "
            f"deduplication, got {len(value_citations)}: {value_citations}"
        )
    # The other 5 lab fields (test_name/unit/reference_range/collection_date/
    # abnormal_flag) only ever come from document_citations() here -- never
    # duplicated -- confirming this isn't accidentally over-deduplicating.
    all_doc_citations = [c for c in citations if c["source_id"] == dup_doc_id]
    if len(all_doc_citations) != 6:
        return False, f"expected 6 total citations for {dup_doc_id!r} (1 deduped value + 5 non-duplicated fields), got {len(all_doc_citations)}"

    return True, "the same (source, value) reached via two different paths collapses to one citation; distinct fields are untouched"


def test_document_fact_stated_in_response_tolerates_unit_formatting(fhir: FhirClient) -> tuple[bool, str]:
    """A citation must never end up uncited over a formatting mismatch
    between the stored value and how the response phrases it -- both
    directions: the response adding a unit the stored value doesn't have,
    and the stored value carrying more than the response restates."""
    # Response states the value WITH a unit the stored citation doesn't carry.
    if not _document_fact_stated_in_response("5.9", "Her potassium came back at 5.9 mmol/L, up from baseline."):
        return False, "'5.9' should be considered stated when the response says '5.9 mmol/L'"

    # Reverse: the stored value itself carries a unit the response drops.
    if not _document_fact_stated_in_response("5.9 mmol/L", "Potassium was 5.9 today, watch for hyperkalemia."):
        return False, "'5.9 mmol/L' should be considered stated when the response only restates '5.9'"

    # A genuinely different number must still be rejected in both shapes.
    if _document_fact_stated_in_response("5.9", "Her potassium came back at 4.2 mmol/L."):
        return False, "'5.9' must NOT match a response stating a different value, '4.2'"
    if _document_fact_stated_in_response("5.9 mmol/L", "Her potassium came back at 4.2 mmol/L."):
        return False, "'5.9 mmol/L' must NOT match a response stating a different value, '4.2'"

    return True, "a value/unit formatting mismatch never causes a stated fact to end up uncited, in either direction"


def test_document_fact_stated_in_response_tolerates_date_formatting(fhir: FhirClient) -> tuple[bool, str]:
    """A collection_date-shaped citation stored as ISO (2026-09-20) must
    still be recognized when the response writes the same calendar date in
    a different, natural-language format -- compared as real dates, not
    string-for-string."""
    if not _document_fact_stated_in_response("2026-09-20", "The lab was drawn on September 20, 2026."):
        return False, "'2026-09-20' should be considered stated when the response says 'September 20, 2026'"
    if not _document_fact_stated_in_response("2026-09-20", "Collected 09/20/2026."):
        return False, "'2026-09-20' should be considered stated when the response says '09/20/2026'"
    if _document_fact_stated_in_response("2026-09-20", "The lab was drawn on September 21, 2026."):
        return False, "'2026-09-20' must NOT match a response stating a different date, September 21"

    return True, "a collection date is recognized across common formats when it's the same real calendar date"


def test_document_fact_stated_in_response_short_value_needs_word_boundary(fhir: FhirClient) -> tuple[bool, str]:
    """A short value like an abnormal_flag "H" or a lab value "4.2" must
    not falsely match just because its characters happen to appear inside
    an unrelated, longer word or number in the response -- the exact
    over-keep risk plain substring-anywhere matching has."""
    if _document_fact_stated_in_response("H", "The patient was seen at Hospital General today."):
        return False, "'H' must NOT match merely because it's a substring of 'Hospital'"
    if not _document_fact_stated_in_response("H", "The abnormal flag was H, indicating a high result."):
        return False, "'H' SHOULD match when it genuinely appears as its own token in the response"

    if _document_fact_stated_in_response("4.2", "Her creatinine was 34.2, an unusually high reading."):
        return False, "'4.2' must NOT match merely because it's a substring of the unrelated value '34.2'"
    if not _document_fact_stated_in_response("4.2", "Her creatinine was 4.2, within the expected range."):
        return False, "'4.2' SHOULD match when it genuinely appears as its own token in the response"

    return True, "a short value only counts as stated at a real word boundary, never as a substring of something else"


def test_dedupe_key_deterministic_and_content_sensitive(fhir: FhirClient) -> tuple[bool, str]:
    key1 = dedupe_key("patient-1", "lab_pdf", b"same bytes")
    key2 = dedupe_key("patient-1", "lab_pdf", b"same bytes")
    key3 = dedupe_key("patient-1", "lab_pdf", b"different bytes")
    key4 = dedupe_key("patient-2", "lab_pdf", b"same bytes")
    if key1 != key2:
        return False, "identical inputs must produce identical dedupe keys"
    if key1 == key3 or key1 == key4:
        return False, "different content or patient_id must produce a different dedupe key"
    source_id, extracted_id = compute_document_ids("patient-1", "lab_pdf", "scan.png", b"same bytes")
    if key1 not in source_id or key1 not in extracted_id:
        return False, f"compute_document_ids should derive filenames from the same dedupe key, got {source_id}, {extracted_id}"
    return True, "dedupe_key is deterministic and sensitive to both content and patient_id; compute_document_ids derives from it"


def test_uploaded_document_disclosure_enforced_when_missing(fhir: FhirClient) -> tuple[bool, str]:
    """PRD Section 4.2's hard rule: a response citing a get_uploaded_documents
    fact must be flagged as unreconciled/pending-review -- checkable in
    verification, not left to the system prompt alone."""
    fact = UploadedDocumentFactSummary(field_name="value", value="1.1", confidence="high", source_resource="document:x#value")
    output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[UploadedDocumentSummary(doc_id="x", doc_type="lab_pdf", facts=[fact], ocr_avg_confidence=90.0, upload_rank=0)],
    )
    records = [ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)]

    undisclosed = "Her creatinine was 1.1, which is within normal limits."
    outcome = verify_response(undisclosed, records, "p1", fhir)
    if "uploaded_document_disclosure" not in outcome.enforced_warnings:
        return False, f"expected the disclosure to be enforced, got warnings={outcome.enforced_warnings}"
    if not mentions_uploaded_document_disclosure(outcome.final_response):
        return False, "final_response should now mention the unreconciled/pending-review disclosure"

    already_disclosed = "From an uploaded lab report (pending review), her creatinine was 1.1."
    outcome2 = verify_response(already_disclosed, records, "p1", fhir)
    if "uploaded_document_disclosure" in outcome2.enforced_warnings:
        return False, "a response that already discloses the uploaded-document caveat should not get a redundant note"

    return True, "citing an uploaded-document fact without disclosure gets the note enforced; an already-disclosed response does not"


def test_low_confidence_value_warning_enforced_for_cited_uncertain_facts(fhir: FhirClient) -> tuple[bool, str]:
    """W2_ARCHITECTURE.md 4.5's planned fix, later-turn (tool-call) half: a
    response citing a low/unreadable-confidence extracted value keeps that
    value in the answer -- never silently filtered out of grounding -- but
    gets an explicit per-value uncertainty note distinct from the blanket
    pending-review disclosure, naming the real field (e.g. "Potassium
    value"), not a positional/raw-attribute label a resident can't
    interpret. A citation of only a high-confidence value gets no such
    note.

    field_name values below match app/tools.py's _flatten_extraction_facts'
    real "results[N].subfield" shape exactly (not a simplified stand-in),
    since _human_readable_field_labels' composition depends on it -- a
    results[0].test_name sibling fact is included so "Potassium" is
    available to compose with "value"/"unit"."""
    facts = [
        UploadedDocumentFactSummary(
            field_name="results[0].test_name", value="Potassium", confidence="high",
            source_resource="document:x#results[0].test_name",
        ),
        UploadedDocumentFactSummary(
            field_name="results[0].value", value="5.9", confidence="low",
            source_resource="document:x#results[0].value",
        ),
        UploadedDocumentFactSummary(
            field_name="results[0].unit", value="mmol/L", confidence="high",
            source_resource="document:x#results[0].unit",
        ),
    ]
    output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[UploadedDocumentSummary(doc_id="x", doc_type="lab_pdf", facts=facts, ocr_avg_confidence=60.0, upload_rank=0)],
    )
    records = [ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)]

    draft = "From an uploaded lab report (pending review), potassium was 5.9 mmol/L."
    outcome = verify_response(draft, records, "p1", fhir)
    if "low_confidence_value_warning" not in outcome.enforced_warnings:
        return False, f"expected the low-confidence warning to be enforced, got warnings={outcome.enforced_warnings}"
    if "5.9" not in outcome.final_response:
        return False, "the low-confidence value must stay in the answer, not be silently dropped"
    if "Confidence note" not in outcome.final_response:
        return False, "expected an explicit per-value confidence note in the final response"
    confidence_note = outcome.final_response.split("Confidence note", 1)[1]
    if "Potassium value" not in confidence_note:
        return False, f"expected the real field label 'Potassium value' in the confidence note, got: {confidence_note!r}"
    if "Potassium unit" in confidence_note:
        return False, "the high-confidence 'unit' fact should not appear in the confidence note"

    high_confidence_facts = [
        UploadedDocumentFactSummary(
            field_name="results[0].test_name", value="Potassium", confidence="high",
            source_resource="document:x#results[0].test_name",
        ),
        UploadedDocumentFactSummary(
            field_name="results[0].unit", value="mmol/L", confidence="high",
            source_resource="document:x#results[0].unit",
        ),
    ]
    high_confidence_output = GetUploadedDocumentsOutput(
        patient_id="p1",
        documents=[
            UploadedDocumentSummary(doc_id="x", doc_type="lab_pdf", facts=high_confidence_facts, ocr_avg_confidence=95.0, upload_rank=0)
        ],
    )
    high_confidence_records = [
        ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=high_confidence_output)
    ]
    high_confidence_draft = "From an uploaded lab report (pending review), the unit was mmol/L."
    high_confidence_outcome = verify_response(high_confidence_draft, high_confidence_records, "p1", fhir)
    if "low_confidence_value_warning" in high_confidence_outcome.enforced_warnings:
        return False, "a citation of only a high-confidence value should not get a confidence-note warning"

    return True, (
        "a cited low-confidence value keeps its place in the answer and gets an explicit per-value "
        "note naming the real field ('Potassium value'); a high-confidence-only citation does not"
    )


_PENICILLIN_CLASS_BULLET = (
    "- **Within penicillins** (penicillin, amoxicillin, ampicillin, piperacillin, nafcillin, oxacillin, dicloxacillin, "
    "etc.): high cross-reactivity is assumed across the whole class \u2014 this is a hard-stop in this system."
)


def _mention_cases(cases: list[tuple[str, str, str]], *, expect_acknowledged: bool) -> list[str]:
    """(label, response, medication) -> the labels whose result is not `expect_acknowledged`, through the real
    _mentions_conflict_near_medication with a documented 'Penicillin (hives)' allergy."""
    return [
        label for label, response, medication in cases
        if _mentions_conflict_near_medication(response, medication, "Penicillin (hives)") is not expect_acknowledged
    ]


def test_allergy_enforcer_accepts_the_penicillin_class_list_marked_hard_stop(fhir: FhirClient) -> tuple[bool, str]:
    """Found in the final golden-set run (2026-09-26): the model listed the penicillin class and said it was a
    hard-stop; it recommended none of them, yet six HARD STOPs were appended and the turn failed. Through the real
    _check_domain_constraint with a documented penicillin allergy: no HARD STOP, passed."""
    drugs = ["amoxicillin", "ampicillin", "piperacillin", "nafcillin", "oxacillin", "dicloxacillin"]
    wrong = _mention_cases([(d, _PENICILLIN_CLASS_BULLET, d) for d in drugs], expect_acknowledged=True)
    if wrong:
        return False, f"the class-list bullet must be accepted for every member, not for: {wrong}"

    class _PenicillinAllergyFhir:
        def search_fresh(self, resource_type: str, params: dict) -> dict:
            return {"entry": [{"resource": {"resourceType": "AllergyIntolerance", "id": "a1", "text": {"div": "<div>Penicillin (hives)</div>"}}}]}

    response, enforced, passed = _check_domain_constraint(_PENICILLIN_CLASS_BULLET, [], "p1", _PenicillinAllergyFhir())  # type: ignore[arg-type]
    if not passed or enforced or "HARD STOP" in response:
        return False, f"expected no enforcement for the class-list bullet, got passed={passed}, enforced={enforced}"
    recommended = "Amoxicillin is a reasonable alternative for her."
    response, enforced, passed = _check_domain_constraint(recommended, [], "p1", _PenicillinAllergyFhir())  # type: ignore[arg-type]
    if passed or "amoxicillin" not in enforced or "HARD STOP" not in response:
        return False, f"a recommendation of amoxicillin must still be enforced, got passed={passed}, enforced={enforced}"
    return True, "the class list marked hard-stop is accepted for all six drugs; a recommendation is still enforced"


def test_allergy_enforcer_accepts_drugs_named_only_to_avoid_them(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("avoid", "Avoid amoxicillin here.", "amoxicillin"),
        ("do not give", "Please do not give ampicillin tonight.", "ampicillin"),
        ("never use", "Never use nafcillin in this patient.", "nafcillin"),
        ("avoid giving", "Avoid giving piperacillin.", "piperacillin"),
        ("e.g. does not split the sentence", "Avoid penicillins (e.g. amoxicillin).", "amoxicillin"),
    ], expect_acknowledged=True)
    if wrong:
        return False, f"these avoidance mentions should be accepted: {wrong}"
    return True, "avoid / do not give / never use / avoid giving are accepted"


def test_allergy_enforcer_still_enforces_a_recommendation_with_no_cues(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("reasonable alternative", "Amoxicillin is a reasonable alternative.", "amoxicillin"),
        ("plain suggestion", "She could take amoxicillin tonight.", "amoxicillin"),
        ("do not hesitate", "Do not hesitate to give amoxicillin.", "amoxicillin"),
    ], expect_acknowledged=False)
    if wrong:
        return False, f"these must still be enforced (not accepted): {wrong}"
    return True, "a recommendation without any allergy or avoidance wording is still enforced"


def test_allergy_enforcer_fails_closed_on_mixed_sentences(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("avoid Y but X fine", "Avoid ceftriaxone, but amoxicillin is fine.", "amoxicillin"),
        ("avoid Y; give X instead", "Avoid ceftriaxone; give amoxicillin instead.", "amoxicillin"),
        ("avoid Y however X", "Avoid ceftriaxone. However amoxicillin can be used.", "amoxicillin"),
        ("a marker in the NEXT sentence", "Amoxicillin is an option. Avoid ceftriaxone.", "amoxicillin"),
        # no recommending word anywhere: only the sentence scope keeps the neighbour's 'avoid' from excusing it
        ("a marker in the next sentence, no other cue", "Amoxicillin was on her list. Avoid ceftriaxone.", "amoxicillin"),
    ], expect_acknowledged=False)
    if wrong:
        return False, f"contrastive / cross-sentence mixes must be enforced: {wrong}"
    return True, "contrast words and a neighbouring sentence's marker do not launder a recommendation"


def test_allergy_enforcer_fails_closed_on_double_negatives(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("no reason to avoid", "There is no reason to avoid amoxicillin.", "amoxicillin"),
        ("do not need to avoid", "You do not need to avoid amoxicillin.", "amoxicillin"),
        ("avoiding is unnecessary", "Avoiding amoxicillin is unnecessary.", "amoxicillin"),
        ("never a problem", "Amoxicillin has never been a problem.", "amoxicillin"),
        ("should not avoid", "You should not avoid amoxicillin.", "amoxicillin"),
    ], expect_acknowledged=False)
    if wrong:
        return False, f"double negatives must be enforced: {wrong}"
    # "not contraindicated": the pre-existing window check already lets this through (it treats the substring
    # 'contraindicat' as an acknowledgement -- an AUDIT.md known gap); the NEW path must not add to it.
    if _every_mention_is_an_avoidance("Amoxicillin is not contraindicated.", "amoxicillin"):
        return False, "the avoidance path must not accept 'not contraindicated'"
    return True, "no reason / no need / unnecessary / never-a-problem / not contraindicated are not accepted as avoidance"


def test_allergy_enforcer_needs_every_mention_to_avoid(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("avoid once, recommend once", "Avoid amoxicillin overnight. Amoxicillin is reasonable tomorrow.", "amoxicillin"),
    ], expect_acknowledged=False)
    if wrong:
        return False, f"one avoiding mention must not excuse a recommending one: {wrong}"
    return True, "every sentence naming the drug has to be an avoidance"


def test_allergy_enforcer_does_not_treat_cross_reactivity_wording_as_avoidance(fhir: FhirClient) -> tuple[bool, str]:
    wrong = _mention_cases([
        ("tolerated despite low cross-reactivity", "Cefazolin is well tolerated despite low cross-reactivity.", "cefazolin"),
        ("low cross-reactivity, reasonable", "Cross-reactivity is low, so cefazolin is reasonable.", "cefazolin"),
        # no recommending word: only refusing to treat the topic word as a marker keeps this enforced
        ("low cross-reactivity, no other cue", "Cefazolin has low cross-reactivity with penicillin.", "cefazolin"),
    ], expect_acknowledged=False)
    if wrong:
        return False, f"'cross-reactivity' alone is not an avoidance marker (that direction fails open): {wrong}"
    return True, "cross-reactivity wording alone never counts as avoidance"


def test_confidence_note_helper_drops_only_clauses_already_stated(fhir: FhirClient) -> tuple[bool, str]:
    """_without_already_noted: what the supervisor's same-turn note asks before appending, so a value the
    tool-record path already noted is not noted again -- but a value not yet noted still is."""
    potassium = ("Potassium value", "5.9", "low")
    sodium = ("Sodium value", "131", "unreadable")
    already = "Potassium was 5.9." + _format_confidence_note([potassium])
    if _without_already_noted(already, [potassium, sodium]) != [sodium]:
        return False, f"expected only the not-yet-noted clause to remain, got {_without_already_noted(already, [potassium, sodium])!r}"
    if _without_already_noted(already, [potassium]) != []:
        return False, "a clause already stated must not be added again"
    if _without_already_noted("A plain answer with no note.", [potassium, sodium]) != [potassium, sodium]:
        return False, "with no earlier note, every clause must be kept"
    # the same value at a different confidence tier is a different statement
    if _without_already_noted(already, [("Potassium value", "5.9", "unreadable")]) != [("Potassium value", "5.9", "unreadable")]:
        return False, "a different confidence tier for the same value is not 'already stated'"
    return True, "only clauses the response does not already state are kept"


def _low_confidence_lab(doc_id: str, rows: list[tuple[str, str]]) -> ExtractionResult:
    def field(value: str) -> ExtractedField:
        return ExtractedField(value=value, confidence="low", citation=DocumentCitation(page_number=1, quote_or_value=value))

    def solid(value: str) -> ExtractedField:
        return ExtractedField(value=value, confidence="high", citation=DocumentCitation(page_number=1, quote_or_value=value))

    return ExtractionResult(
        patient_id="p1", doc_id=doc_id, doc_type="lab_pdf", ocr_avg_confidence=80.0,
        extraction=LabPdfExtraction(results=[
            LabResultFact(test_name=solid(name), value=field(value), unit=solid("mmol/L"), reference_range=solid("3.5-5.1"),
                          collection_date=solid("2026-09-24"), abnormal_flag=solid("high"))
            for name, value in rows]),
    )


def _lab_tool_record(doc_id: str, rows: list[tuple[str, str]]) -> ToolCallRecord:
    facts: list[UploadedDocumentFactSummary] = []
    for i, (name, value) in enumerate(rows):
        for leaf, val, conf in (("test_name", name, "high"), ("value", value, "low"), ("unit", "mmol/L", "high")):
            facts.append(UploadedDocumentFactSummary(field_name=f"results[{i}].{leaf}", value=val, confidence=conf,
                                                     source_resource=f"document:{doc_id}#results[{i}].{leaf}"))
    output = GetUploadedDocumentsOutput(
        patient_id="p1", documents=[UploadedDocumentSummary(doc_id=doc_id, doc_type="lab_pdf", facts=facts, ocr_avg_confidence=80.0, upload_rank=0)])
    return ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p1", output=output)


def test_synthesize_node_does_not_repeat_the_confidence_note_when_both_paths_cover_one_document(fhir: FhirClient) -> tuple[bool, str]:
    """Found live 2026-09-26: a question sent with uploaded_document_ref ran BOTH the chart-data worker
    (get_uploaded_documents -> verify_response's tool-record confidence note) and the intake extractor
    (this turn's extracted_facts -> the supervisor's own confidence note), so the same low-confidence values
    were noted twice, with overlapping clauses. Real synthesize_node, scripted draft."""

    class _DraftLLM(_NoOpLLMMixin):
        def __init__(self, text: str) -> None:
            self._text = text

        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            return LLMTurnResponse(text=self._text, tool_calls=[], input_tokens=50, output_tokens=50, raw_for_trace=[], stop_reason="end_turn")

    def chart(records: list[ToolCallRecord]) -> ChartGatherResult:
        return ChartGatherResult(draft_text="", turn_records=records, tool_calls=[], updated_messages=[],
                                 total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0)

    def run(rows_in_tool: list[tuple[str, str]], rows_extracted: list[tuple[str, str]], draft: str) -> str:
        state = _base_synthesis_state(
            user_message="What does the uploaded lab show?", workers_invoked=["chart_data_worker", "intake_extractor"],
            chart_facts=chart([_lab_tool_record("x", rows_in_tool)]), extracted_facts=[_low_confidence_lab("x", rows_extracted)])
        trace = TurnTrace(client=None, span=None, correlation_id="test-synthesis-correlation-id")
        return synthesize_node(state, llm=_DraftLLM(draft), model="test-model", fhir=fhir, trace=trace)["final_response"]

    # Same document through both paths: one note, not two.
    both = run([("Potassium", "5.9")], [("Potassium", "5.9")], "From an uploaded lab (pending review), potassium was 5.9 mmol/L.")
    if both.count("[Confidence note:") != 1 or both.count("(5.9) was read with low confidence") != 1:
        return False, f"expected exactly one confidence note for the shared value, got: {both!r}"

    # The extraction path also has a value the tool path does not: it still gets noted, without repeating the first.
    partly = run([("Potassium", "5.9")], [("Potassium", "5.9"), ("Sodium", "131")],
                 "From an uploaded lab (pending review), potassium was 5.9 mmol/L and sodium 131 mmol/L.")
    if partly.count("(5.9) was read with low confidence") != 1:
        return False, f"the shared value must be noted once, got: {partly!r}"
    if partly.count("(131) was read with low confidence") != 1:
        return False, f"the value only the extraction path has must still be noted, got: {partly!r}"
    return True, "a value both paths cover is noted once; a value only one path has is still noted"


def test_human_readable_field_labels_covers_intake_form_field_shapes(fhir: FhirClient) -> tuple[bool, str]:
    """_human_readable_field_labels must produce real, resident-readable
    labels for every intake-form field_name shape app/tools.py's
    _flatten_extraction_facts actually emits (demographics.*, chief_concern,
    and the three list fields), not just the lab-result composition path
    the confidence-warning tests above already cover."""
    facts = [
        UploadedDocumentFactSummary(
            field_name="demographics.name", value="Jordan Rivera", confidence="high",
            source_resource="document:x#demographics.name",
        ),
        UploadedDocumentFactSummary(
            field_name="chief_concern", value="Shortness of breath", confidence="low",
            source_resource="document:x#chief_concern",
        ),
        UploadedDocumentFactSummary(
            field_name="allergies[0]", value="Penicillin (hives)", confidence="high",
            source_resource="document:x#allergies[0]",
        ),
    ]
    labels = _human_readable_field_labels(facts)
    expected = ["Patient name", "Chief concern", "Allergy"]
    if labels != expected:
        return False, f"expected {expected}, got {labels}"
    return True, "demographics, chief_concern, and list-field field_name shapes all resolve to real, readable labels"


def test_route_targets_defaults_to_chart_worker_when_empty(fhir: FhirClient) -> tuple[bool, str]:
    """Routing safety net (app/graph/supervisor.py): an empty/failed
    routing decision must fall back to chart_data_worker, never to
    routing nowhere."""
    result = route_targets({"workers_invoked": []})
    if result != [CHART_DATA_WORKER]:
        return False, f"expected fallback to [{CHART_DATA_WORKER}], got {result}"
    explicit = route_targets({"workers_invoked": ["evidence_retriever"]})
    if explicit != ["evidence_retriever"]:
        return False, f"a real routing decision should pass through unchanged, got {explicit}"
    return True, "empty routing decision falls back to chart_data_worker; a real decision passes through unchanged"


def test_uploaded_document_citations_preview_resolves_on_both_paths(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the click-to-source bug found live 2026-09-24:
    a citation's source_id must actually resolve via GET /documents/preview
    (app/main.py's document_preview() -> get_source_document_bytes(), which
    can only ever find a document by its ORIGINAL scan filename -- doc_id,
    category 35 -- never extracted_doc_id, category 36). Both
    document_citations() (same-turn intake_extractor path) and
    chart_citations() (later-turn get_uploaded_documents path) now use
    doc_id as source_id; this test proves both actually resolve, not just
    that they construct without error.

    LLM-free: stores a document directly via store_extraction (the same
    zero-API-cost pattern evals/cases.py's _store_known_lab_document
    established), with a REAL rendered image (not arbitrary bytes) so the
    real FHIR round-trip and rasterize_page have a real image to work
    with -- calls document_preview()'s own underlying functions directly
    (get_source_document_bytes, rasterize_page, resolve_bbox) rather than
    the HTTP route, since those functions are exactly what that route
    calls and reaching them directly needs no session/cookie plumbing.

    OCR itself is STUBBED (a hand-built OcrResult, not run_ocr()), found
    live 2026-09-24: this test failed 1 run in 4 in eval_unit_tests (a
    merge-blocking job) with real Tesseract OCR against the rendered
    image -- ordinary OCR-engine variance, not a bug in the code under
    test. eval_unit_tests must not flake, and this test's actual purpose
    (a citation's source_id resolves via get_source_document_bytes/
    rasterize_page, and resolve_bbox's own matching logic finds the right
    words once it HAS OCR output) doesn't need a real Tesseract call to
    prove -- resolve_bbox's matching algorithm is exercised identically
    against a fixed OcrResult built from the exact same field values the
    image renders. What stays real and un-stubbed: the FHIR store +
    fetch-by-doc_id round trip (the actual regression this test guards)
    and rasterize_page's real image decoding. Live OCR accuracy itself is
    covered elsewhere (evals/cases.py's real extraction-accuracy cases,
    which run in the separately-gated, real-Anthropic-spend eval_golden_set,
    not this LLM-free suite)."""
    patient_id = f.PID1_ALICE
    nonce = uuid.uuid4().hex[:8]
    # evals/cases.py's _render_document_image, not a hand-rolled PIL draw
    # -- still a real, realistic image for the FHIR round-trip/rasterize_
    # page half of this test, even though OCR itself is stubbed below.
    content = _render_document_image([
        "LAB REPORT", f"Test: Potassium (ref {nonce})", f"Value: {nonce}", "Unit: mEqPerL",
        "Reference Range: 3.5to5.0", "Collection Date: 20260924", "Flag: Normal",
    ])

    source_doc_id, extracted_doc_id = compute_document_ids(patient_id, "lab_pdf", f"citepreview_{nonce}.png", content)
    citation = DocumentCitation(page_number=1, quote_or_value=f"Potassium (ref {nonce})")
    field = lambda value: ExtractedField(value=value, confidence="high", citation=citation)  # noqa: E731
    extraction = ExtractionResult(
        patient_id=patient_id, doc_id=source_doc_id, extracted_doc_id=extracted_doc_id, doc_type="lab_pdf",
        extraction=LabPdfExtraction(results=[
            LabResultFact(
                test_name=field("Potassium"), value=field(nonce), unit=field("mEqPerL"),
                reference_range=field("3.5to5.0"), collection_date=field("20260924"), abnormal_flag=field("Normal"),
            )
        ]),
        ocr_avg_confidence=99.0,
    )

    # Registered immediately, before the upload attempt, and flushed in
    # the `finally` below regardless of how this test ends -- found live
    # 2026-09-27: PID1 (a shared Golden Set fixture with no per-case
    # cleanup path of its own) was accumulating one new orphaned Source
    # Document + AI-Extracted pair per run, which also permanently
    # breaks graph_cases.py's check_pid1_has_no_uploaded_documents
    # precondition (it counts exactly the AI-Extracted category this
    # test writes to). document_cleanup's TEST-0001 allowlist entry
    # exists specifically for this test. If store_extraction below never
    # actually uploads anything (some exception other than
    # DuplicateDocumentError), delete_documents_for_pubpid simply finds 0
    # matching rows and no-ops -- registering unconditionally is safe.
    register_for_cleanup("TEST-0001", source_doc_id, extracted_doc_id)
    try:
        try:
            store_extraction(fhir, patient_id, content, "image/png", source_doc_id, extracted_doc_id, extraction)
        except DuplicateDocumentError:
            pass  # already uploaded earlier in THIS run -- fine, proceed straight to read-back

        # Deterministic stand-in for a real run_ocr() call (see the docstring
        # above) -- one word per field value actually rendered on the page,
        # in the same y-ordering as the image, with plausible, arbitrary
        # bboxes (their exact coordinates don't matter to this test; only
        # resolve_bbox's own token-matching logic, exercised identically
        # against real or stubbed OCR output, is under test here).
        stub_words = [
            OcrWord(text="Potassium", confidence=96.0, bbox=(0.1, 0.10, 0.4, 0.15)),
            OcrWord(text=nonce, confidence=96.0, bbox=(0.1, 0.20, 0.4, 0.25)),
            OcrWord(text="mEqPerL", confidence=96.0, bbox=(0.1, 0.30, 0.4, 0.35)),
            OcrWord(text="3.5to5.0", confidence=96.0, bbox=(0.1, 0.40, 0.4, 0.45)),
            OcrWord(text="20260924", confidence=96.0, bbox=(0.1, 0.50, 0.4, 0.55)),
            OcrWord(text="Normal", confidence=96.0, bbox=(0.1, 0.60, 0.4, 0.65)),
        ]
        stub_ocr_result = OcrResult(pages=[OcrPage(page_number=1, words=stub_words)])

        def preview_resolves(source_id: str, page_or_section: str | None, quote: str) -> tuple[bool, str]:
            try:
                raw_content, mimetype = get_source_document_bytes(fhir, patient_id, source_id)
            except FhirRequestError as exc:
                return False, f"get_source_document_bytes failed for source_id={source_id!r}: {exc.detail_code}"
            page = int(page_or_section) if page_or_section else 1
            png_bytes = rasterize_page(raw_content, mimetype, page)
            if not png_bytes:
                return False, f"rasterize_page returned no bytes for source_id={source_id!r}"
            bbox = resolve_bbox(stub_ocr_result, page, quote)
            if bbox is None:
                return False, f"no X-Citation-Bbox resolved for source_id={source_id!r}, quote={quote!r}"
            return True, "ok"

        same_turn = document_citations([extraction])
        if not same_turn:
            return False, "document_citations() produced no citations -- test fixture problem"
        for c in same_turn:
            ok, reason = preview_resolves(c.source_id, c.page_or_section, c.quote_or_value)
            if not ok:
                return False, f"same-turn citation failed: {reason}"

        output = get_uploaded_documents(fhir, {"patient_id": patient_id}, [])
        if isinstance(output, ToolFailure) or not output.documents:
            return False, "get_uploaded_documents returned no documents -- test fixture problem"
        record = ToolCallRecord(tool_name="get_uploaded_documents", patient_id=patient_id, output=output)
        chart_facts = ChartGatherResult(
            draft_text="", turn_records=[record], tool_calls=[], updated_messages=[],
            total_input_tokens=0, total_output_tokens=0, total_llm_latency_s=0.0,
        )
        # Scoped to THIS test's own document -- get_uploaded_documents() returns
        # every document on file for the patient, including leftovers from
        # other tests/prior runs on this shared dev instance, not just the one
        # just stored here.
        later_turn = [
            c for c in chart_citations(chart_facts, patient_id)
            if c.source_type == "uploaded_document" and c.source_id == source_doc_id
        ]
        if not later_turn:
            return False, "chart_citations() produced no uploaded_document citations for this test's own document"
        for c in later_turn:
            ok, reason = preview_resolves(c.source_id, c.page_or_section, c.quote_or_value)
            if not ok:
                return False, f"later-turn citation failed: {reason}"

        return True, (
            f"{len(same_turn)} same-turn and {len(later_turn)} later-turn uploaded_document citations "
            "all resolve via GET /documents/preview's underlying functions (real FHIR fetch + rasterize, "
            "stubbed OCR) with an X-Citation-Bbox"
        )
    finally:
        flush_cleanup()


def test_synthesize_node_grounds_same_turn_extracted_facts(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the intake_extractor grounding gap found live
    2026-09-24: app/graph/supervisor.py's synthesize_node() only ever
    built extra_grounded_text from retrieved_evidence's guideline chunks
    -- state["extracted_facts"] (THIS turn's intake_extractor output, a
    direct state write, never a ToolCallRecord) never reached verify_
    response()'s grounding vocabulary at all. A response correctly citing
    a same-turn-extracted fact, on a turn where chart_data_worker gathered
    nothing (turn_records=[]), had zero grounding vocabulary and was
    structurally vulnerable to being wrongly stripped as unverified.

    Fixed via the new _extraction_field_values() helper, which flattens an
    ExtractionResult's field values the same way _grounded_vocabulary's
    own GetUploadedDocumentsOutput branch already does for the read-back-
    on-a-later-turn get_uploaded_documents TOOL path (app/verification.py)
    -- now wired into synthesize_node's verify_response() call alongside
    retrieved_evidence's guideline text.

    Reproduces exactly what synthesize_node's verify_response() call does
    (driving the real LLM-calling synthesize_node end to end would cost
    real API spend) rather than exercising the full graph: builds extra_
    grounded_text from _extraction_field_values() applied to a sample
    extraction and checks a draft combining the fact's value and unit into
    one candidate ("1.1 mg/dL", the same atomic-composition gap 2b8f4b8
    fixed for the tool path) is correctly grounded. The WITHOUT condition
    (empty extra_grounded_text -- the pre-fix state) is asserted first, as
    the actual regression proof: it must get flagged, or this test isn't
    exercising anything real."""
    extraction = _sample_extraction_result()
    draft = "Creatinine is 1.1 mg/dL, within the reference range."

    without_fix = verify_response(draft, [], None, fhir, extra_grounded_text=[])
    if not without_fix.flagged_claims:
        return False, (
            "test fixture problem: expected the composed value+unit claim to be flagged when "
            "extracted_facts is NOT grounded (turn_records=[], extra_grounded_text=[]) -- this "
            "test doesn't actually exercise the intake_extractor grounding gap"
        )

    extra_grounded_text = _extraction_field_values(extraction)
    with_fix = verify_response(draft, [], None, fhir, extra_grounded_text=extra_grounded_text)
    if with_fix.flagged_claims:
        return False, (
            "expected the same-turn extracted fact to be grounded via _extraction_field_values, "
            f"but it was still flagged: {with_fix.flagged_claims!r}"
        )
    if "1.1 mg/dL" not in with_fix.final_response:
        return False, "the grounded claim was unexpectedly stripped from the final response"

    return True, (
        "a draft citing a same-turn intake_extractor fact is flagged with no grounding, and "
        "correctly grounded once _extraction_field_values(extracted_facts) feeds extra_grounded_text"
    )


def _base_synthesis_state(**overrides) -> dict:
    """Minimal CoPilotState for calling synthesize_node directly, bypassing
    the full graph (routing, tool-calling loops) -- a mocked Anthropic
    client, no live calls, same reasoning as every other test in this
    file that reaches past a public function boundary to test a specific
    branch in isolation."""
    state = {
        "correlation_id": "test-synthesis-correlation-id",
        "resident_username": None,
        "patient_id": "p1",
        "user_message": "test message",
        "messages": [],
        "workers_invoked": [],
        "routing_rationale": "test",
        "chart_facts": None,
        "uploaded_document_ref": None,
        "extracted_facts": [],
        "retrieved_evidence": [],
        "citations": [],
        "node_metrics": {},
        "partial_failures": [],
        "draft_response": None,
        "needs_review_flags": [],
        "verification_outcome": None,
        "final_response": None,
    }
    state.update(overrides)
    return state


class _NoOpLLMMixin:
    def append_assistant_turn(self, messages, response) -> None:
        pass

    def append_tool_results(self, messages, results) -> None:
        pass

    def check_ready(self) -> bool:
        return True


def test_gather_chart_facts_discloses_truncated_draft(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: several llm_call events (including chart_data_worker's
    own final drafting round) landed on output_tokens exactly equal to
    max_tokens (1024) -- confirmed as real truncation once stop_reason
    started being captured. Mocked Anthropic client (no live call),
    injected by overwriting ClinicalCopilotAgent's own _llm attribute
    after construction (its constructor always builds a real
    AnthropicLLMClient internally, with no injection seam) -- a fake
    final-round response with stop_reason="max_tokens" and no tool_calls
    must produce a draft_text disclosing the cutoff."""

    class _TruncatingFakeLLM(_NoOpLLMMixin):
        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            return LLMTurnResponse(
                text="Active problems include diabetes and hypertension, currently on lisinopril and metf",
                tool_calls=[], input_tokens=50, output_tokens=max_tokens, raw_for_trace=[],
                stop_reason="max_tokens",
            )

    settings = get_settings()
    agent = ClinicalCopilotAgent(settings, observer=TurnObserver(settings))
    agent._llm = _TruncatingFakeLLM()

    trace = TurnTrace(client=None, span=None, correlation_id="test-chart-correlation-id")
    result = agent.gather_chart_facts(
        history=[], user_message="Give me a quick orientation to this patient.", patient_id="p1",
        prior_tool_records=None, fhir=fhir, trace=trace,
    )
    if "cut off" not in result.draft_text:
        return False, f"expected the truncation disclosure in draft_text, got: {result.draft_text!r}"
    return True, "gather_chart_facts discloses a truncated (stop_reason='max_tokens') draft"


def test_synthesize_node_discloses_truncated_synthesis_response(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: several llm_call events (including final synthesis
    rounds) landed on output_tokens exactly equal to max_tokens (1024) --
    confirmed as real truncation once stop_reason started being captured.
    Mocked Anthropic client (no live call): a fake synthesis response with
    stop_reason="max_tokens" must produce a draft_response disclosing the
    cutoff, not silently presenting a truncated answer as complete."""
    chunk = GuidelineChunk(
        source_id="hyperkalemia_management", chunk_id="hyperkalemia_management:0",
        text="Mild hyperkalemia is roughly 5.1-5.9 mEq/L.", page_or_section="Recognition and severity",
    )
    evidence = [RetrievedEvidence(chunk=chunk, rerank_score=1.0)]

    class _TruncatingFakeLLM(_NoOpLLMMixin):
        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            return LLMTurnResponse(
                text="Based on the retrieved guideline, the general management approach is to",
                tool_calls=[], input_tokens=50, output_tokens=max_tokens, raw_for_trace=[],
                stop_reason="max_tokens",
            )

    state = _base_synthesis_state(
        user_message="What is the general management approach for hyperkalemia?",
        workers_invoked=["evidence_retriever"], retrieved_evidence=evidence,
    )
    trace = TurnTrace(client=None, span=None, correlation_id="test-synthesis-correlation-id")
    result = synthesize_node(state, llm=_TruncatingFakeLLM(), model="test-model", fhir=fhir, trace=trace)

    if "cut off" not in result["draft_response"]:
        return False, f"expected the truncation disclosure in draft_response, got: {result['draft_response']!r}"
    return True, "synthesize_node discloses a truncated (stop_reason='max_tokens') synthesis response"


def test_synthesize_node_enforces_worker_partial_failure_on_retriever_only_crash(
    fhir: FhirClient,
) -> tuple[bool, str]:
    """Found live: when evidence_retriever crashes and it's the ONLY
    worker routed (chart_facts=None, extracted_facts=[],
    retrieved_evidence=[]), synthesize_node's own "nothing gathered"
    branch builds draft_response by concatenating partial_failures text
    directly -- never calling the LLM, and (before this fix) never
    recording "worker_partial_failure" in enforced_warnings either, since
    the shared disclosure check only appended the flag when it ALSO had
    to append new text (which it didn't here -- the failure was already
    embedded). Result: an honestly-worded response with
    enforced_warnings=[] and eval_outcome=True, giving no structured
    signal that a worker had failed at all. Mocked Anthropic client (no
    live call) that raises if invoked -- proves this branch never calls
    the LLM, so a bug that accidentally routed it through the LLM would
    fail loudly, not silently pass."""

    class _UnusedLLM(_NoOpLLMMixin):
        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            raise AssertionError("the 'nothing gathered' branch must never call the LLM")

    state = _base_synthesis_state(
        workers_invoked=["evidence_retriever"],
        partial_failures=["evidence_retriever: an unexpected error occurred -- answering without this worker's contribution"],
    )
    trace = TurnTrace(client=None, span=None, correlation_id="test-synthesis-correlation-id")
    result = synthesize_node(state, llm=_UnusedLLM(), model="test-model", fhir=fhir, trace=trace)

    enforced_warnings = result["verification_outcome"]["enforced_warnings"]
    if "worker_partial_failure" not in enforced_warnings:
        return False, (
            f"expected 'worker_partial_failure' in enforced_warnings for a retriever-only crash, "
            f"got {enforced_warnings!r}"
        )
    if "evidence_retriever" not in result["final_response"]:
        return False, f"expected the crash to still be disclosed in the response text, got: {result['final_response']!r}"
    return True, "a retriever-only crash correctly sets worker_partial_failure in enforced_warnings"


def test_synthesize_node_logs_synthesis_node_metrics_and_cost(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: turn_summary's logged node_metrics always omitted
    supervisor_synthesize (a snapshot-timing bug -- state["node_metrics"]
    reflects only PRIOR nodes' contributions at the point this function
    reads it, before its own metrics merge into state). Confirms the
    LOGGED event now includes synthesis's own node_metrics entry and that
    cost_usd reflects it -- not just the real, returned
    GraphTurnResult.node_metrics (read after the graph completes, in
    app/graph/build.py, which already included it correctly even before
    this fix). Mocked Anthropic client (no live call); captures the real
    local structured log line via a temporary logging.Handler, the same
    pattern test_turn_observer_logs_resident_identity_alongside_patient
    already uses."""
    chunk = GuidelineChunk(source_id="x", chunk_id="x:0", text="general guidance", page_or_section="(preamble)")
    evidence = [RetrievedEvidence(chunk=chunk, rerank_score=1.0)]

    class _FakeLLM(_NoOpLLMMixin):
        def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
            return LLMTurnResponse(text="A general answer.", tool_calls=[], input_tokens=100, output_tokens=50, raw_for_trace=[])

    # A pre-existing node's own contribution, so "does cost_usd include
    # synthesis" is checkable as "is the total more than JUST this," not
    # merely "is it non-zero" (which synthesis's own cost alone could
    # already satisfy, proving nothing about whether prior nodes are also
    # still correctly included).
    pre_existing_cost = 0.001
    state = _base_synthesis_state(
        workers_invoked=["evidence_retriever"], retrieved_evidence=evidence,
        node_metrics={"supervisor_route": {"latency_ms": 1.0, "input_tokens": 1, "output_tokens": 1, "cost_usd": pre_existing_cost}},
    )
    trace = TurnTrace(client=None, span=None, correlation_id="test-metrics-correlation-id")

    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CapturingHandler()
    target_logger = logging.getLogger("clinical_copilot")
    target_logger.addHandler(handler)
    try:
        synthesize_node(state, llm=_FakeLLM(), model="test-model", fhir=fhir, trace=trace)
    finally:
        target_logger.removeHandler(handler)

    turn_summary_lines = [json.loads(line) for line in captured if '"event": "turn_summary"' in line]
    if len(turn_summary_lines) != 1:
        return False, f"expected exactly 1 turn_summary log line, got {len(turn_summary_lines)}: {captured!r}"
    logged = turn_summary_lines[0]

    logged_node_metrics = logged.get("node_metrics") or {}
    if "supervisor_synthesize" not in logged_node_metrics:
        return False, f"expected 'supervisor_synthesize' in the logged node_metrics, got {logged_node_metrics!r}"
    if logged.get("cost_usd") is None or logged["cost_usd"] <= pre_existing_cost:
        return False, (
            f"expected logged cost_usd to include synthesis's own cost on top of the pre-existing "
            f"{pre_existing_cost}, got {logged.get('cost_usd')!r}"
        )
    return True, "turn_summary's logged node_metrics/cost_usd correctly include synthesis's own contribution"


def test_check_allergy_conflict_rejects_malformed_empty_input(fhir: FhirClient) -> tuple[bool, str]:
    """Found live: a real turn sent check_allergy_conflict input={}
    (missing patient_id/medication_name entirely) -- confirms the
    existing, already-correct behavior (Pydantic validation catches this
    before any FHIR call, returning an honest ToolFailure rather than
    crashing or hanging) stays covered against regression. No retry is
    correct here: unlike a network blip, retrying identical malformed
    input can never succeed -- only the model calling again with actually-
    correct arguments can."""
    result = check_allergy_conflict(fhir, {}, [])
    if not isinstance(result, ToolFailure):
        return False, f"expected a ToolFailure for empty input, got {result!r}"
    if result.detail_code != "invalid_input":
        return False, f"expected detail_code='invalid_input', got {result.detail_code!r}"
    return True, "check_allergy_conflict cleanly rejects malformed (empty) input as a ToolFailure, not a crash"


def test_range_endpoints_ground_when_restated_with_unit(fhir: FhirClient) -> tuple[bool, str]:
    """Found live via graph_follow_up_turn_keeps_uploaded_document_fact_
    grounded, turn 2: a retrieved guideline chunk stated "roughly 5.1-5.9
    mEq/L" (a severity threshold range); the model correctly restated the
    lower bound as "5.1 mEq/L", which was flagged as unverified -- a pure
    literal-match false positive, not a hallucination. The source's own
    "N1-N2 UNIT" phrasing attaches the unit once, after the upper bound
    only, so "5.1 mEq/L" (unit attached directly to the lower bound, the
    exact shape a model naturally writes when citing just one endpoint)
    never appears as a literal substring of "5.1-5.9 mEq/L" -- even though
    the number itself is genuinely present in what was retrieved.
    Confirmed against the real corpus chunk before writing this fix (it
    contains "5.1" but never "5.1 mEq/L")."""
    chunk_text = "Mild hyperkalemia is roughly 5.1-5.9 mEq/L, usually with no ECG changes."
    lower_bound_draft = "Her potassium is mildly elevated, in the mild range starting at 5.1 mEq/L."

    without_fix = verify_response(lower_bound_draft, [], None, fhir, extra_grounded_text=[])
    if not without_fix.flagged_claims:
        return False, (
            "test fixture problem: expected '5.1 mEq/L' to be flagged with no grounded text at "
            "all -- this test isn't exercising the range-endpoint fix"
        )

    lower_outcome = verify_response(lower_bound_draft, [], None, fhir, extra_grounded_text=[chunk_text])
    if lower_outcome.flagged_claims:
        return False, f"expected the range's lower bound (5.1 mEq/L) to ground, got flagged_claims={lower_outcome.flagged_claims!r}"

    upper_bound_draft = "Mild hyperkalemia runs up to 5.9 mEq/L before it's considered moderate."
    upper_outcome = verify_response(upper_bound_draft, [], None, fhir, extra_grounded_text=[chunk_text])
    if upper_outcome.flagged_claims:
        return False, f"expected the range's upper bound (5.9 mEq/L) to ground, got flagged_claims={upper_outcome.flagged_claims!r}"

    return True, "both endpoints of a retrieved 'N1-N2 UNIT' range ground correctly when restated with their unit"


def test_value_inside_range_but_not_an_endpoint_still_fails_grounding(fhir: FhirClient) -> tuple[bool, str]:
    """The range-endpoint fix must stay narrow: a value strictly INSIDE a
    retrieved range (never itself stated by the source) must still fail
    grounding -- only the two real endpoints the source actually states
    are synthesized as groundable, not every number the range spans."""
    chunk_text = "Mild hyperkalemia is roughly 5.1-5.9 mEq/L, usually with no ECG changes."
    draft = "Her potassium is 5.5 mEq/L, right in the middle of the mild range."
    outcome = verify_response(draft, [], None, fhir, extra_grounded_text=[chunk_text])
    if not outcome.flagged_claims:
        return False, (
            "expected '5.5 mEq/L' (inside the range but never itself stated by the source) to "
            "still be flagged as unverified -- the range-endpoint fix should not ground every "
            "value a range spans, only its real endpoints"
        )
    return True, "a value strictly inside a retrieved range, never itself stated, still fails grounding"


def test_unrelated_number_still_fails_grounding_near_a_retrieved_range(fhir: FhirClient) -> tuple[bool, str]:
    """Control: a number with no relationship at all to the retrieved
    range must still fail grounding -- confirms the range-endpoint fix
    doesn't accidentally over-ground unrelated values just because SOME
    numeric range exists in the grounded text."""
    chunk_text = "Mild hyperkalemia is roughly 5.1-5.9 mEq/L, usually with no ECG changes."
    draft = "Her potassium is 9.9 mEq/L, dangerously high."
    outcome = verify_response(draft, [], None, fhir, extra_grounded_text=[chunk_text])
    if not outcome.flagged_claims:
        return False, "expected an unrelated value (9.9 mEq/L) to still be flagged as unverified"
    return True, "a value unrelated to the retrieved range still fails grounding"


def test_extraction_field_label_value_confidence_pairs_labels_correctly(fhir: FhirClient) -> tuple[bool, str]:
    """Same-turn half of W2_ARCHITECTURE.md 4.5's planned per-value
    confidence flagging: _extraction_field_label_value_confidence must
    pair each field's own label with its own value and confidence, in the
    same field order _extraction_field_values/_extraction_field_confidences
    already use. A labeling bug here would silently mislabel which field
    synthesize_node's confidence note names as uncertain -- exercised as a
    pure function (not through synthesize_node, which would require a real
    LLM call for its multi-source-synthesis branch), same "reaches past
    the public boundary" convention test_synthesize_node_grounds_same_turn_
    extracted_facts above already uses for the grounding half of this same
    gap."""
    citation = DocumentCitation(page_number=1, bbox=(0.1, 0.1, 0.2, 0.2), quote_or_value="141")
    field = lambda value, confidence="high": ExtractedField(value=value, confidence=confidence, citation=citation)  # noqa: E731
    lab = LabResultFact(
        test_name=field("Creatinine"),
        value=field("141", "unreadable"),
        unit=field("mg/dL"),
        reference_range=field("0.6-1.2"),
        collection_date=field("2026-09-20"),
        abnormal_flag=field("normal"),
    )
    result = ExtractionResult(
        patient_id="test-patient",
        doc_id="abc_source_lab.png",
        extracted_doc_id="abc_extracted.json",
        doc_type="lab_pdf",
        extraction=LabPdfExtraction(results=[lab]),
        ocr_avg_confidence=40.0,
    )
    triples = _extraction_field_label_value_confidence(result)
    if len(triples) != 6:
        return False, f"expected 6 (label, value, confidence) triples for 6 lab fields, got {len(triples)}"

    value_triple = next((t for t in triples if t[0] == "Creatinine value"), None)
    if value_triple is None or value_triple[1] != "141" or value_triple[2] != "unreadable":
        return False, f"expected ('Creatinine value', '141', 'unreadable'), got {value_triple}"

    test_name_triple = next((t for t in triples if t[0] == "Creatinine test name"), None)
    if test_name_triple is None or test_name_triple[1] != "Creatinine" or test_name_triple[2] != "high":
        return False, f"expected ('Creatinine test name', 'Creatinine', 'high'), got {test_name_triple}"

    unit_triple = next((t for t in triples if t[0] == "Creatinine unit"), None)
    if unit_triple is None or unit_triple[1] != "mg/dL":
        return False, f"expected a 'Creatinine unit' label pairing with value 'mg/dL', got {unit_triple}"

    return True, (
        "each field's label is composed with its own lab result's real test name (e.g. 'Creatinine "
        "value'), not a positional/raw-attribute label, and pairs with its own value and confidence"
    )


def _chat_turn_result(response_text: str) -> ChatTurnResult:
    return ChatTurnResult(
        correlation_id="unit-test",
        response_text=response_text,
        verification_passed=True,
        passed_domain_constraint=True,
        flagged_claims=[],
        enforced_warnings=[],
    )


def test_check_ambiguous_query_handles_real_phrasing_variants(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for evals/ERROR_ANALYSIS.md Entry 2: evals/cases.py's
    _check_ambiguous_query used to be a 17-phrase exact-substring
    enumeration, broadened three separate times against three different
    live-run paraphrasings and still structurally guaranteed to break on
    the next one (see that function's own docstring for the full history).
    Replaced 2026-09-24 with two independent structural signals -- a
    general request/question marker, AND no single one of the patient's
    two known active medications asserted alone -- instead of enumerating
    correct wordings.

    Reaches past the EvalCase.check boundary to call _check_ambiguous_query
    directly (deterministic, no API call) against a fixed set of real and
    representative phrasings, rather than driving a live turn -- proving
    the check itself handles wording variance, which a live run can only
    ever sample one instance of per pipeline run.

    Must-pass variants: the three actual historical phrasings that broke
    the old list (evals/ERROR_ANALYSIS.md Entry 2 / evals/cases.py's own
    docstring history), plus "which medication" and "which specific
    medication" (the two phrasings the original 17-item list was written
    to catch in the first place -- must still pass under the new check).
    Must-fail controls: a response that silently answers about exactly one
    of the two medications (the actual failure mode this case exists to
    catch), and a response with no request signal at all."""
    must_pass = [
        "Which medication are you asking about?",
        "Which specific medication would you like me to check?",
        "Please tell me the specific medication name you want to give.",
        "I need the name of the specific medication you're considering.",
        "please tell me the name of the medication you're asking about.",
    ]
    failures = []
    for text in must_pass:
        passed, reason = _check_ambiguous_query(_chat_turn_result(text))
        if not passed:
            failures.append(f"expected PASS for {text!r}, got FAIL: {reason}")

    must_fail = [
        "Metformin is okay to give given her documented allergies.",
        "Metformin is fine to give. Anything else?",
        "The chart shows no documented allergy conflicts.",
    ]
    for text in must_fail:
        passed, reason = _check_ambiguous_query(_chat_turn_result(text))
        if passed:
            failures.append(f"expected FAIL for {text!r}, got PASS: {reason}")

    if failures:
        return False, "; ".join(failures)
    return True, (
        f"_check_ambiguous_query correctly passes {len(must_pass)} real/representative clarification "
        f"phrasings and correctly fails {len(must_fail)} silent-single-medication-answer controls"
    )


def test_grade_factually_consistent_respects_expects_verification_failure_flag(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the mechanism evals/gate.py's grade_factually_
    consistent and evals/run_evals.py's check_gate() now share:
    EvalCase.expects_verification_failure (evals/cases.py), not a
    hardcoded name set. Found live 2026-09-24: the old hardcoded-set
    version of this exclusion had already been missed once
    (domain_constraint_allergy_hard_block, 2026-09-22) and was missed
    again the same night (graph_adversarial_unverifiable_claim_stripped,
    added to only one of two independently-maintained lists) -- the same
    bug class recurring is exactly why this is now a single boolean field
    read directly off each case, not a list a person has to remember to
    update in two places. This test only proves the MECHANISM itself is
    correct (the flag actually changes the graded outcome); it can't by
    itself catch a real case that forgot to set the flag -- that needs a
    real turn's verification_passed result, which evals/run_evals.py's
    own live cross-check (added alongside this test, see check_gate())
    covers instead."""
    from evals import gate

    # A case whose turn genuinely failed verification, but is flagged as
    # expected (a deliberately-baited claim correctly caught and
    # stripped) -- must be excluded (None), not scored as a failure.
    excluded = gate.grade_factually_consistent(False, expects_failure=True)
    if excluded is not None:
        return False, f"expected None (excluded) for a flagged, by-design verification failure, got {excluded!r}"

    # The identical verification_passed=False, WITHOUT the flag -- must be
    # scored as a real failure (False), not silently excluded.
    real_failure = gate.grade_factually_consistent(False, expects_failure=False)
    if real_failure is not False:
        return False, f"expected False (a real, counted failure) for an unflagged verification failure, got {real_failure!r}"

    # A genuine pass, unflagged -- must be scored as a real pass (True).
    real_pass = gate.grade_factually_consistent(True, expects_failure=False)
    if real_pass is not True:
        return False, f"expected True for a genuine, unflagged verification pass, got {real_pass!r}"

    # No turn_result at all (standalone_check cases) -- must stay None
    # regardless of the flag, the existing "not applicable" behavior.
    no_turn = gate.grade_factually_consistent(None, expects_failure=False)
    if no_turn is not None:
        return False, f"expected None when verification_passed itself is None, got {no_turn!r}"

    return True, "grade_factually_consistent correctly reads expects_failure for all four cases (flagged-fail, real-fail, real-pass, no-turn)"


# --- Schema validation tests (2026-09-24), PRD Submission Requirements'
# "Schemas: Pydantic/Zod schemas for lab_pdf and intake_form, including
# source citation fields and validation tests" -- every prior use of
# LabPdfExtraction/IntakeFormExtraction (evals/cases.py, this file) only
# ever constructed VALID instances; nothing asserted that an invalid one is
# actually rejected. These do.
#
# Honest scope note on abnormal_flag specifically: LabResultFact.
# abnormal_flag (app/ingestion/schemas.py) is deliberately typed as a plain
# ExtractedField (free-text value), not a closed enum -- its own comment
# says "as printed on the source": real lab reports print this as
# free-form text ("H", "HIGH", "Panic", "Abnormal - High", etc.), and
# evals/cases.py's real-pipeline extraction-accuracy cases depend on that
# being genuinely free text, not a fixed vocabulary. Constraining it to a
# closed set would misrepresent the field's actual, intentional design and
# break real passing cases -- so there's no "invalid abnormal_flag
# rejected" test here. confidence IS a real, closed Literal
# (ConfidenceTier = "high"|"low"|"unreadable") and is tested below.


def _valid_document_citation() -> DocumentCitation:
    return DocumentCitation(page_number=1, quote_or_value="Potassium 4.4 mEq/L")


def _valid_extracted_field(value: str = "4.4") -> ExtractedField:
    return ExtractedField(value=value, confidence="high", citation=_valid_document_citation())


def _valid_lab_result_fact() -> LabResultFact:
    return LabResultFact(
        test_name=_valid_extracted_field("Potassium"),
        value=_valid_extracted_field("4.4"),
        unit=_valid_extracted_field("mEq/L"),
        reference_range=_valid_extracted_field("3.5-5.0"),
        collection_date=_valid_extracted_field("2026-09-24"),
        abnormal_flag=_valid_extracted_field("normal"),
    )


def test_lab_pdf_extraction_valid_case_accepted(fhir: FhirClient) -> tuple[bool, str]:
    """Control for every rejection test below: a genuinely valid
    LabPdfExtraction must construct without raising."""
    try:
        ext = LabPdfExtraction(results=[_valid_lab_result_fact()])
    except ValidationError as exc:
        return False, f"a genuinely valid LabPdfExtraction was rejected: {exc}"
    if len(ext.results) != 1:
        return False, f"expected exactly 1 result, got {len(ext.results)}"
    return True, "a valid LabPdfExtraction is accepted"


def test_lab_result_fact_missing_required_field_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """Every one of LabResultFact's 6 fields (PRD Core Requirement 2's
    verbatim minimum: test name, value, unit, reference range, collection
    date, abnormal flag) is required -- omitting any one must raise, not
    silently default to None/empty."""
    field = _valid_extracted_field()
    complete_kwargs = {
        "test_name": field, "value": field, "unit": field,
        "reference_range": field, "collection_date": field, "abnormal_flag": field,
    }
    missing_any_rejected = []
    for omit in complete_kwargs:
        kwargs = {k: v for k, v in complete_kwargs.items() if k != omit}
        try:
            LabResultFact(**kwargs)
            missing_any_rejected.append(omit)  # did NOT raise -- a real gap
        except ValidationError:
            pass  # correctly rejected
    if missing_any_rejected:
        return False, f"LabResultFact accepted construction with these required fields missing: {missing_any_rejected}"
    return True, "LabResultFact rejects construction with any of its 6 required fields missing"


def test_extracted_field_wrong_type_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """ExtractedField.value must be a str -- a non-string (e.g. a raw
    float or dict, which a careless integration might pass straight from
    a parsed number) must be rejected, not silently coerced or accepted."""
    citation = _valid_document_citation()
    try:
        ExtractedField(value=4.4, confidence="high", citation=citation)  # type: ignore[arg-type]
        return False, "ExtractedField accepted a non-string value (float 4.4) without rejecting it"
    except ValidationError:
        pass
    try:
        ExtractedField(value={"amount": 4.4}, confidence="high", citation=citation)  # type: ignore[arg-type]
        return False, "ExtractedField accepted a non-string value (dict) without rejecting it"
    except ValidationError:
        pass
    return True, "ExtractedField correctly rejects non-string values"


def test_extracted_field_missing_citation_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """PRD Requirement 5/Section 4.2: every extracted value must carry a
    citation -- ExtractedField.citation is required, not Optional. Omitting
    it must raise."""
    try:
        ExtractedField(value="4.4", confidence="high")  # type: ignore[call-arg]
        return False, "ExtractedField accepted construction with no citation at all"
    except ValidationError:
        pass
    return True, "ExtractedField rejects construction with a missing citation"


def test_extracted_field_invalid_confidence_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """ConfidenceTier (app/ingestion/schemas.py) is a closed Literal --
    "high" | "low" | "unreadable" -- not a free string. Anything else
    (a typo, a numeric score, a different tier name) must be rejected."""
    citation = _valid_document_citation()
    for bad_value in ("medium", "HIGH", "0.9", "certain", ""):
        try:
            ExtractedField(value="4.4", confidence=bad_value, citation=citation)  # type: ignore[arg-type]
            return False, f"ExtractedField accepted an invalid confidence tier: {bad_value!r}"
        except ValidationError:
            pass
    return True, "ExtractedField rejects every tested invalid confidence value"


def test_document_citation_invalid_page_number_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """DocumentCitation.page_number is constrained ge=1 (app/ingestion/
    schemas.py) -- page 0 or negative must be rejected, not silently
    accepted as a nonsensical page reference."""
    for bad_page in (0, -1):
        try:
            DocumentCitation(page_number=bad_page, quote_or_value="some text")
            return False, f"DocumentCitation accepted an invalid page_number: {bad_page}"
        except ValidationError:
            pass
    return True, "DocumentCitation rejects page_number values below 1"


def test_intake_form_extraction_valid_case_accepted(fhir: FhirClient) -> tuple[bool, str]:
    """Control for the intake_form rejection tests below: a genuinely
    valid IntakeFormExtraction must construct without raising."""
    citation = _valid_document_citation()
    try:
        ext = IntakeFormExtraction(
            demographics=DemographicsFact(
                name=ExtractedField(value="Jordan Rivera", confidence="high", citation=citation),
                date_of_birth=ExtractedField(value="1988-04-12", confidence="high", citation=citation),
                sex=ExtractedField(value="Female", confidence="high", citation=citation),
            ),
            chief_concern=ExtractedField(value="Annual checkup", confidence="high", citation=citation),
        )
    except ValidationError as exc:
        return False, f"a genuinely valid IntakeFormExtraction was rejected: {exc}"
    if ext.doc_type != "intake_form":
        return False, f"expected doc_type='intake_form', got {ext.doc_type!r}"
    return True, "a valid IntakeFormExtraction is accepted"


def test_intake_form_extraction_missing_demographics_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """IntakeFormExtraction.demographics is required (not Optional) --
    PRD Core Requirement 2's minimum intake fields start with
    "demographics fields". Omitting it entirely must raise."""
    try:
        IntakeFormExtraction()  # type: ignore[call-arg]
        return False, "IntakeFormExtraction accepted construction with no demographics at all"
    except ValidationError:
        pass
    return True, "IntakeFormExtraction rejects construction with demographics missing"


def test_intake_form_extraction_wrong_type_in_medication_list_rejected(fhir: FhirClient) -> tuple[bool, str]:
    """current_medications/allergies/family_history are list[ExtractedField]
    -- a bare string in that list (e.g. a careless "just pass the drug
    name" shortcut) must be rejected, not silently accepted as if it were
    a real ExtractedField."""
    citation = _valid_document_citation()
    try:
        IntakeFormExtraction(
            demographics=DemographicsFact(name=ExtractedField(value="Jordan Rivera", confidence="high", citation=citation)),
            current_medications=["Albuterol inhaler"],  # type: ignore[list-item]  -- bare str, not ExtractedField
        )
        return False, "IntakeFormExtraction accepted a bare string in current_medications instead of an ExtractedField"
    except ValidationError:
        pass
    return True, "IntakeFormExtraction rejects a bare string where an ExtractedField is required in current_medications"


# --- evals/prune_upload_fixture.py: pure-logic safety checks, no real DB ---
# (fix/tomorrow-prep, 2026-09-24) -- resolve_fixture_pid/find_prunable_
# documents/prune all take an injectable run_mysql callable specifically so
# these tests can exercise the script's abort/scoping logic without a real
# mysql binary or server -- consistent with "no commands that call ...
# OpenEMR" for tonight's work, and with this suite's own LLM-free/network-
# free convention for pure-logic modules.


def _fake_db_config() -> DbConfig:
    return DbConfig(host="fake", port=0, user="fake", password="fake", database="fake")


def test_prune_fixture_validate_host_guardrails(fhir: FhirClient) -> tuple[bool, str]:
    """_validate_host must refuse the known production host unconditionally
    -- even if --staging-host is (mis)configured to match it -- and must
    refuse any other non-loopback host unless an explicit, matching
    --staging-host is declared. Loopback (local dev) needs no declaration
    at all. Review feedback: "refuse if the DB host is production ... or
    anything but staging" -- this is that guardrail."""
    # Production is refused outright, with no staging_host at all...
    try:
        _validate_host(PRODUCTION_DB_HOST, None)
        return False, "expected PruneError for the production host with no staging_host declared"
    except PruneError:
        pass
    # ...and refused even if staging_host is misconfigured to match it --
    # the production check must not be overridable by any flag.
    try:
        _validate_host(PRODUCTION_DB_HOST, PRODUCTION_DB_HOST)
        return False, "expected PruneError for the production host even with a matching staging_host"
    except PruneError:
        pass

    # Loopback hosts need no --staging-host declaration at all.
    for loopback in ("127.0.0.1", "localhost", "::1"):
        _validate_host(loopback, None)  # must not raise

    # A non-loopback, non-production host with no staging_host declared is refused.
    try:
        _validate_host("10.0.0.5", None)
        return False, "expected PruneError for a non-loopback host with no --staging-host declared"
    except PruneError:
        pass

    # A non-loopback host with a MISMATCHED staging_host is refused.
    try:
        _validate_host("10.0.0.5", "10.0.0.9")
        return False, "expected PruneError when --staging-host doesn't match --db-host"
    except PruneError:
        pass

    # A non-loopback host with a MATCHING, explicitly declared staging_host is allowed.
    _validate_host("10.0.0.5", "10.0.0.5")  # must not raise

    return True, (
        "production is refused unconditionally; any other non-loopback host requires an "
        "explicit, matching --staging-host; loopback needs no declaration"
    )


def test_prune_fixture_resolves_pid_or_aborts_on_ambiguity(fhir: FhirClient) -> tuple[bool, str]:
    """resolve_fixture_pid must never guess -- exactly one patient_data row
    for pubpid='TEST-0007' resolves to a real pid; zero or more than one
    aborts with PruneError rather than picking arbitrarily. This script is
    never allowed to touch a pid it isn't certain is the dedicated
    upload-eval fixture."""
    cfg = _fake_db_config()

    def one_row(_cfg: DbConfig, _sql: str) -> str:
        return "42\n"

    pid = resolve_fixture_pid(cfg, run_mysql=one_row)
    if pid != 42:
        return False, f"expected pid=42 from a single-row result, got {pid}"

    def zero_rows(_cfg: DbConfig, _sql: str) -> str:
        return ""

    try:
        resolve_fixture_pid(cfg, run_mysql=zero_rows)
        return False, "expected PruneError when no patient_data row matches TEST-0007"
    except PruneError:
        pass

    def two_rows(_cfg: DbConfig, _sql: str) -> str:
        return "42\n43\n"

    try:
        resolve_fixture_pid(cfg, run_mysql=two_rows)
        return False, "expected PruneError when more than one patient_data row matches TEST-0007"
    except PruneError:
        pass

    return True, "a single matching row resolves to its pid; zero or multiple rows abort with PruneError rather than guessing"


def test_prune_fixture_aborts_on_category_id_mismatch(fhir: FhirClient) -> tuple[bool, str]:
    """find_prunable_documents must abort, not silently prune, if
    categories.id 35/36 don't map to the exact names app/ingestion/
    storage.py hardcodes -- a renumbered category on some stack is exactly
    the kind of surprise this script must never guess past."""
    cfg = _fake_db_config()

    def wrong_category_names(_cfg: DbConfig, sql: str) -> str:
        if "FROM categories" in sql:
            return (
                f"{SOURCE_DOCUMENT_CATEGORY_ID}\tSome Other Category\n"
                f"{AI_EXTRACTED_CATEGORY_ID}\t{AI_EXTRACTED_CATEGORY_NAME}\n"
            )
        return ""

    try:
        find_prunable_documents(cfg, 42, run_mysql=wrong_category_names)
        return False, "expected PruneError when category 35's real name doesn't match 'Source Document'"
    except PruneError:
        pass

    return True, "a category id/name mismatch aborts with PruneError instead of pruning against the wrong category"


def test_prune_fixture_scopes_update_to_exact_selected_ids(fhir: FhirClient) -> tuple[bool, str]:
    """find_prunable_documents parses (id, category, name) rows correctly,
    and prune()'s UPDATE is scoped to exactly the ids it was given -- never
    a broader WHERE clause re-evaluated at delete time, so nothing can
    match more rows between the SELECT an operator sees and the UPDATE
    that actually runs."""
    cfg = _fake_db_config()
    calls: list[str] = []

    def fake_run(_cfg: DbConfig, sql: str) -> str:
        calls.append(sql)
        if "FROM categories" in sql:
            return (
                f"{SOURCE_DOCUMENT_CATEGORY_ID}\t{SOURCE_DOCUMENT_CATEGORY_NAME}\n"
                f"{AI_EXTRACTED_CATEGORY_ID}\t{AI_EXTRACTED_CATEGORY_NAME}\n"
            )
        if "FROM documents" in sql:
            return (
                f"101\t{SOURCE_DOCUMENT_CATEGORY_NAME}\tsome_source.pdf\n"
                f"102\t{AI_EXTRACTED_CATEGORY_NAME}\tsome_extracted.json\n"
            )
        return ""

    rows = find_prunable_documents(cfg, 42, run_mysql=fake_run)
    if [r[0] for r in rows] != [101, 102]:
        return False, f"expected document ids [101, 102], got {[r[0] for r in rows]}"

    prune(cfg, [r[0] for r in rows], run_mysql=fake_run)
    update_calls = [c for c in calls if c.strip().upper().startswith("UPDATE")]
    if len(update_calls) != 1:
        return False, f"expected exactly one UPDATE statement, got {len(update_calls)}"
    if "WHERE id IN (101,102)" not in update_calls[0]:
        return False, f"expected the UPDATE to be scoped to exactly ids 101,102, got: {update_calls[0]!r}"

    return True, "documents are parsed correctly and the UPDATE is scoped to exactly the SELECTed ids, no broader match"


TESTS: list[tuple[str, Callable[[FhirClient], tuple[bool, str]]]] = [
    ("get_patient_snapshot_pid1", test_get_patient_snapshot_pid1),
    ("get_patient_snapshot_pid2", test_get_patient_snapshot_pid2),
    ("check_allergy_conflict_positive", test_check_allergy_conflict_positive),
    ("check_allergy_conflict_negative", test_check_allergy_conflict_negative),
    ("check_allergy_conflict_cross_reactive", test_check_allergy_conflict_cross_reactive),
    ("check_allergy_conflict_cross_reactive_scoped_to_curated_classes", test_check_allergy_conflict_cross_reactive_scoped_to_curated_classes),
    ("caching_reduces_redundant_fhir_calls", test_caching_reduces_redundant_fhir_calls),
    ("parallelizing_snapshot_fetches_reduces_wall_clock", test_parallelizing_snapshot_fetches_reduces_wall_clock),
    ("fhir_timeout_retries_once_then_fails_honestly", test_fhir_timeout_retries_once_then_fails_honestly),
    ("fhir_retry_then_succeed_returns_real_data", test_fhir_retry_then_succeed_returns_real_data),
    ("turn_observer_logs_resident_identity_alongside_patient", test_turn_observer_logs_resident_identity_alongside_patient),
    ("log_routing_decision_logs_workers_and_rationale", test_log_routing_decision_logs_workers_and_rationale),
    ("build_llm_client_defaults_to_anthropic", test_build_llm_client_defaults_to_anthropic),
    ("never_cache_list_bypasses_stale_allergy_data", test_never_cache_list_bypasses_stale_allergy_data),
    ("verification_strips_ungrounded_claim", test_verification_strips_ungrounded_claim),
    ("verification_passes_grounded_claim", test_verification_passes_grounded_claim),
    ("lab_value_grounding_tolerates_formatting", test_lab_value_grounding_tolerates_formatting),
    ("uploaded_document_composed_lab_value_is_grounded", test_uploaded_document_composed_lab_value_is_grounded),
    ("uploaded_document_lab_value_survives_prose_and_table_formatting", test_uploaded_document_lab_value_survives_prose_and_table_formatting),
    ("lab_value_range_mention_not_flagged", test_lab_value_range_mention_not_flagged),
    ("allergy_hard_stop_not_silenced_by_unrelated_conflict_word", test_allergy_hard_stop_not_silenced_by_unrelated_conflict_word),
    ("bare_allergy_text_matching_medication_name_not_treated_as_self_acknowledged", test_bare_allergy_text_matching_medication_name_not_treated_as_self_acknowledged),
    ("compare_signout_baseline_requires_genuine_prior_turn", test_compare_signout_baseline_requires_genuine_prior_turn),
    ("duplicate_warning_not_silenced_by_unrelated_records_mention", test_duplicate_warning_not_silenced_by_unrelated_records_mention),
    ("empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention", test_empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention),
    ("partial_failure_force_disclosed_when_not_mentioned", test_partial_failure_force_disclosed_when_not_mentioned),
    ("partial_failure_not_double_injected_when_already_mentioned", test_partial_failure_not_double_injected_when_already_mentioned),
    ("dose_candidate_finds_real_drug_not_preceding_verb", test_dose_candidate_finds_real_drug_not_preceding_verb),
    ("sensitivity_filter_excludes_high_for_clin", test_sensitivity_filter_excludes_high_for_clin),
    ("sensitivity_filter_allows_high_for_doc", test_sensitivity_filter_allows_high_for_doc),
    ("domain_constraint_backstop_survives_missing_patient_id", test_domain_constraint_backstop_survives_missing_patient_id),
    ("cross_patient_tool_call_blocked", test_cross_patient_tool_call_blocked),
    ("shift_summary_reports_nothing_gathered_yet", test_shift_summary_reports_nothing_gathered_yet),
    ("collect_source_resources_scoped_deduped_and_nested", test_collect_source_resources_scoped_deduped_and_nested),
    ("login_next_sanitizer_rejects_offsite_targets", test_login_next_sanitizer_rejects_offsite_targets),
    ("login_flow_never_redirects_offsite", test_login_flow_never_redirects_offsite),
    ("feedback_requires_auth", test_feedback_requires_auth),
    ("feedback_valid_submission_writes_log_and_attempts_langfuse", test_feedback_valid_submission_writes_log_and_attempts_langfuse),
    ("feedback_comment_is_optional", test_feedback_comment_is_optional),
    ("feedback_does_not_trust_client_supplied_identity", test_feedback_does_not_trust_client_supplied_identity),
    ("reference_layer_query_isolation", test_reference_layer_query_isolation),
    ("reference_layer_dailymed_real_match", test_reference_layer_dailymed_real_match),
    ("reference_layer_medlineplus_real_match", test_reference_layer_medlineplus_real_match),
    ("reference_layer_dailymed_honest_no_reference", test_reference_layer_dailymed_honest_no_reference),
    ("reference_layer_medlineplus_honest_no_reference", test_reference_layer_medlineplus_honest_no_reference),
    ("reference_layer_mere_mention_does_not_trigger", test_reference_layer_mere_mention_does_not_trigger),
    ("reference_layer_past_tense_history_does_not_trigger", test_reference_layer_past_tense_history_does_not_trigger),
    ("corpus_chunking_produces_unique_ids_and_sections", test_corpus_chunking_produces_unique_ids_and_sections),
    ("plausibility_check_catches_digit_transposition", test_plausibility_check_catches_digit_transposition),
    ("plausibility_check_preserves_genuine_abnormal_values", test_plausibility_check_preserves_genuine_abnormal_values),
    ("bbox_resolution_matches_and_falls_back", test_bbox_resolution_matches_and_falls_back),
    ("get_source_document_bytes_via_standard_api", test_get_source_document_bytes_via_standard_api),
    ("get_source_document_bytes_refuses_deleted_document", test_get_source_document_bytes_refuses_deleted_document),
    ("allergy_enforcer_accepts_the_penicillin_class_list_marked_hard_stop", test_allergy_enforcer_accepts_the_penicillin_class_list_marked_hard_stop),
    ("allergy_enforcer_accepts_drugs_named_only_to_avoid_them", test_allergy_enforcer_accepts_drugs_named_only_to_avoid_them),
    ("allergy_enforcer_still_enforces_a_recommendation_with_no_cues", test_allergy_enforcer_still_enforces_a_recommendation_with_no_cues),
    ("allergy_enforcer_fails_closed_on_mixed_sentences", test_allergy_enforcer_fails_closed_on_mixed_sentences),
    ("allergy_enforcer_fails_closed_on_double_negatives", test_allergy_enforcer_fails_closed_on_double_negatives),
    ("allergy_enforcer_needs_every_mention_to_avoid", test_allergy_enforcer_needs_every_mention_to_avoid),
    ("allergy_enforcer_does_not_treat_cross_reactivity_wording_as_avoidance", test_allergy_enforcer_does_not_treat_cross_reactivity_wording_as_avoidance),
    ("confidence_note_helper_drops_only_clauses_already_stated", test_confidence_note_helper_drops_only_clauses_already_stated),
    ("synthesize_node_does_not_repeat_the_confidence_note_when_both_paths_cover_one_document", test_synthesize_node_does_not_repeat_the_confidence_note_when_both_paths_cover_one_document),
    (
        "get_uploaded_documents_output_omitted_count_defaults_and_validates",
        test_get_uploaded_documents_output_omitted_count_defaults_and_validates,
    ),
    (
        "list_extracted_documents_caps_to_most_recent_and_reports_omitted_count",
        test_list_extracted_documents_caps_to_most_recent_and_reports_omitted_count,
    ),
    ("rrf_fusion_combines_dense_and_sparse_rankings", test_rrf_fusion_combines_dense_and_sparse_rankings),
    (
        "build_safe_query_scrubs_every_phrase_and_drops_empty",
        test_build_safe_query_scrubs_every_phrase_and_drops_empty,
    ),
    ("build_safe_query_caps_at_three_phrases", test_build_safe_query_caps_at_three_phrases),
    (
        "guideline_retriever_merges_candidates_from_multiple_queries_before_reranking",
        test_guideline_retriever_merges_candidates_from_multiple_queries_before_reranking,
    ),
    ("guideline_retriever_returns_empty_for_no_queries", test_guideline_retriever_returns_empty_for_no_queries),
    ("document_citations_translate_per_field", test_document_citations_translate_per_field),
    ("guideline_citations_translate_chunk_shape", test_guideline_citations_translate_chunk_shape),
    ("chart_citations_dedupes_and_scopes_to_active_patient", test_chart_citations_dedupes_and_scopes_to_active_patient),
    (
        "chart_citations_populates_all_five_fields_for_fhir_resource",
        test_chart_citations_populates_all_five_fields_for_fhir_resource,
    ),
    (
        "citation_schema_validation_rejects_invalid_shapes",
        test_citation_schema_validation_rejects_invalid_shapes,
    ),
    (
        "filter_citations_to_stated_facts_drops_uncited_document_fields",
        test_filter_citations_to_stated_facts_drops_uncited_document_fields,
    ),
    (
        "filter_citations_to_stated_facts_applies_to_later_turn_get_uploaded_documents_citations",
        test_filter_citations_to_stated_facts_applies_to_later_turn_get_uploaded_documents_citations,
    ),
    (
        "filter_citations_to_stated_facts_distinguishes_documents_differing_only_in_value",
        test_filter_citations_to_stated_facts_distinguishes_documents_differing_only_in_value,
    ),
    (
        "filter_citations_to_stated_facts_keeps_both_identical_documents",
        test_filter_citations_to_stated_facts_keeps_both_identical_documents,
    ),
    (
        "filter_citations_to_stated_facts_handles_two_different_document_types",
        test_filter_citations_to_stated_facts_handles_two_different_document_types,
    ),
    (
        "check_no_citation_misattribution_rejects_shared_field_only_citation",
        test_check_no_citation_misattribution_rejects_shared_field_only_citation,
    ),
    (
        "check_no_citation_misattribution_rejects_wrong_document_attribution",
        test_check_no_citation_misattribution_rejects_wrong_document_attribution,
    ),
    (
        "check_no_citation_misattribution_accepts_legitimate_dual_citation",
        test_check_no_citation_misattribution_accepts_legitimate_dual_citation,
    ),
    (
        "translate_citations_deduplicates_same_source_and_value",
        test_translate_citations_deduplicates_same_source_and_value,
    ),
    (
        "document_fact_stated_in_response_tolerates_unit_formatting",
        test_document_fact_stated_in_response_tolerates_unit_formatting,
    ),
    (
        "document_fact_stated_in_response_tolerates_date_formatting",
        test_document_fact_stated_in_response_tolerates_date_formatting,
    ),
    (
        "document_fact_stated_in_response_short_value_needs_word_boundary",
        test_document_fact_stated_in_response_short_value_needs_word_boundary,
    ),
    ("dedupe_key_deterministic_and_content_sensitive", test_dedupe_key_deterministic_and_content_sensitive),
    ("uploaded_document_disclosure_enforced_when_missing", test_uploaded_document_disclosure_enforced_when_missing),
    (
        "low_confidence_value_warning_enforced_for_cited_uncertain_facts",
        test_low_confidence_value_warning_enforced_for_cited_uncertain_facts,
    ),
    (
        "human_readable_field_labels_covers_intake_form_field_shapes",
        test_human_readable_field_labels_covers_intake_form_field_shapes,
    ),
    ("route_targets_defaults_to_chart_worker_when_empty", test_route_targets_defaults_to_chart_worker_when_empty),
    (
        "uploaded_document_citations_preview_resolves_on_both_paths",
        test_uploaded_document_citations_preview_resolves_on_both_paths,
    ),
    ("synthesize_node_grounds_same_turn_extracted_facts", test_synthesize_node_grounds_same_turn_extracted_facts),
    (
        "gather_chart_facts_discloses_truncated_draft",
        test_gather_chart_facts_discloses_truncated_draft,
    ),
    (
        "synthesize_node_discloses_truncated_synthesis_response",
        test_synthesize_node_discloses_truncated_synthesis_response,
    ),
    (
        "synthesize_node_enforces_worker_partial_failure_on_retriever_only_crash",
        test_synthesize_node_enforces_worker_partial_failure_on_retriever_only_crash,
    ),
    (
        "synthesize_node_logs_synthesis_node_metrics_and_cost",
        test_synthesize_node_logs_synthesis_node_metrics_and_cost,
    ),
    (
        "check_allergy_conflict_rejects_malformed_empty_input",
        test_check_allergy_conflict_rejects_malformed_empty_input,
    ),
    ("range_endpoints_ground_when_restated_with_unit", test_range_endpoints_ground_when_restated_with_unit),
    (
        "value_inside_range_but_not_an_endpoint_still_fails_grounding",
        test_value_inside_range_but_not_an_endpoint_still_fails_grounding,
    ),
    (
        "unrelated_number_still_fails_grounding_near_a_retrieved_range",
        test_unrelated_number_still_fails_grounding_near_a_retrieved_range,
    ),
    (
        "extraction_field_label_value_confidence_pairs_labels_correctly",
        test_extraction_field_label_value_confidence_pairs_labels_correctly,
    ),
    ("check_ambiguous_query_handles_real_phrasing_variants", test_check_ambiguous_query_handles_real_phrasing_variants),
    (
        "grade_factually_consistent_respects_expects_verification_failure_flag",
        test_grade_factually_consistent_respects_expects_verification_failure_flag,
    ),
    ("lab_pdf_extraction_valid_case_accepted", test_lab_pdf_extraction_valid_case_accepted),
    ("lab_result_fact_missing_required_field_rejected", test_lab_result_fact_missing_required_field_rejected),
    ("extracted_field_wrong_type_rejected", test_extracted_field_wrong_type_rejected),
    ("extracted_field_missing_citation_rejected", test_extracted_field_missing_citation_rejected),
    ("extracted_field_invalid_confidence_rejected", test_extracted_field_invalid_confidence_rejected),
    ("document_citation_invalid_page_number_rejected", test_document_citation_invalid_page_number_rejected),
    ("intake_form_extraction_valid_case_accepted", test_intake_form_extraction_valid_case_accepted),
    ("intake_form_extraction_missing_demographics_rejected", test_intake_form_extraction_missing_demographics_rejected),
    ("intake_form_extraction_wrong_type_in_medication_list_rejected", test_intake_form_extraction_wrong_type_in_medication_list_rejected),
    ("prune_fixture_validate_host_guardrails", test_prune_fixture_validate_host_guardrails),
    ("prune_fixture_resolves_pid_or_aborts_on_ambiguity", test_prune_fixture_resolves_pid_or_aborts_on_ambiguity),
    ("prune_fixture_aborts_on_category_id_mismatch", test_prune_fixture_aborts_on_category_id_mismatch),
    ("prune_fixture_scopes_update_to_exact_selected_ids", test_prune_fixture_scopes_update_to_exact_selected_ids),
]
TESTS.extend(_PARALLEL_RUNNER_TESTS)
TESTS.extend(_EXTRACTION_TESTS)
TESTS.extend(_PREVIEW_AUTHZ_TESTS)
TESTS.extend(_CITATION_CARDS_TESTS)
TESTS.extend(_TRUNCATION_TESTS)
TESTS.extend(_SCHEMA_CONTRACT_TESTS)
TESTS.extend(_ALLERGY_RETRIEVAL_TESTS)
TESTS.extend(_SECURITY_HARDENING_TESTS)
TESTS.extend(_VERIFICATION_NOTICES_TESTS)
TESTS.extend(_CITATION_DISPLAY_TESTS)
TESTS.extend(_CALL_SITE_TESTS)
TESTS.extend(_DISCLOSURE_FILTER_TESTS)


def run_all_tests() -> list[tuple[str, bool, str]]:
    """Runs every test and returns (name, passed, reason) tuples, no
    printing -- the reusable core, shared by main() and evals/run_all.py."""
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))

    results = []
    for name, fn in TESTS:
        try:
            passed, reason = fn(fhir)
        except Exception as exc:  # noqa: BLE001 -- a test must record a failure, not crash the suite
            passed, reason = False, f"raised an exception: {exc!r}"
        results.append((name, passed, reason))
    return results


def main() -> int:
    t0 = time.monotonic()
    results = run_all_tests()
    log_job_timing("unit_tests", time.monotonic() - t0, cases=len(results), concurrency=1)

    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    skipped_count = sum(1 for _, p, r in results if p and r.startswith("SKIPPED:"))
    print(f"\n{'=' * 70}")
    summary = f"Clinical Co-Pilot unit tests (LLM-free): {passed_count}/{total} passed"
    if skipped_count:
        summary += f" ({skipped_count} skipped -- external API unreachable, not a regression)"
    print(summary)
    print(f"{'=' * 70}\n")
    for name, passed, reason in results:
        # A "SKIPPED:" reason (see _external_api_reachable's callers) means an
        # external dependency (DailyMed/MedlinePlus) was unreachable -- passed=True
        # so it doesn't block the suite, same convention as evals/run_evals.py's
        # skip_reason, but shown as SKIP here, not PASS, so it stays visible
        # rather than reading as a real, checked pass.
        status = "SKIP" if passed and reason.startswith("SKIPPED:") else ("PASS" if passed else "FAIL")
        print(f"[{status}] {name}")
        print(f"       {reason}\n")

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
