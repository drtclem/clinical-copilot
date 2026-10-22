#!/usr/bin/env python3
"""W2 Checklist item 8's graph-level cases: "extraction accuracy, citations
across all three source domains, RAG grounding, refusal/missing-data
behavior." A separate, additive suite from evals/cases.py -- that suite
(and evals/run_evals.py's Golden Set gate) exercises
ClinicalCopilotAgent.run_turn() (Week 1's single-worker path) and is
completely untouched by this file; every case here drives
app/graph/build.py's run_turn() instead, the only path that has
routing/multi-worker synthesis/citations to test at all.

Honest scope note: this was a real, first, additive set -- not the full
~30 cases the PRD's Checklist item 8 asked for. Extraction-accuracy cases
were NOT included here for the reason originally documented at this exact
spot: intake_extractor's read-back path was blocked by a CSRF-key gap on
the document-read route (README.md's then-"Known gap"). That gap was
diagnosed and fixed 2026-09-23 (commit cc99f2a) and verified end-to-end on
staging -- the reason no longer holds. Extraction-accuracy coverage, plus
the rest of the ~30-case gap this file's own docstring named, was added
2026-09-24 directly into evals/cases.py's Golden Set instead of here (see
that file's "Week 2 cases" section) -- graph-path cases scored against the
same five PRD-named boolean-rubric metrics (gate.py) real golden-set
membership requires, which this file's bespoke per-case checks were never
wired into (still true, and still why this suite stays separate -- see
that section's own comment for the full reasoning). What IS covered here,
unchanged: citations across the two domains that currently work end-to-end
(fhir_resource, guideline), RAG grounding, routing correctness, and
honest-failure/missing-data behavior for both the RAG and document
domains.

Like evals/unit_tests.py, no pytest dependency -- same lightweight runner
convention, real Anthropic + OpenEMR calls (costs real spend per run, not
run on every commit).

2026-09-23 addition: every GraphEvalCase above calls
app/graph/build.py's run_turn() directly -- exercises the graph itself,
never app/main.py's /chat route handler, which is a separate piece of
code (the HTTP response construction) with its own way to break. Found
live in production the hard way: that construction code crashed every
real /chat call with an AttributeError this file's entire suite -- graph
cases and the unrelated Golden Set alike -- had no way to catch, because
none of them call through it. check_chat_endpoint_builds_a_real_response_
without_crashing() closes that gap with a real in-process HTTP-level
check (starlette's TestClient against the real FastAPI app, a real
injected session, real backend calls) -- see its own docstring and
app/graph/build.py's GraphTurnResult for the full incident.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.graph import CoPilotState  # noqa: F401 -- re-exported for callers inspecting state shape
from app.graph.build import GraphTurnResult
from app.graph.build import run_turn as graph_run_turn
from app.ingestion.storage import list_extracted_documents
from evals import fixtures as f
# Reuses evals/cases.py's own real log-capture pattern rather than a second,
# independently-written copy of it -- see
# check_hyperkalemia_routing_logged_and_cites_guideline's docstring.
from evals.cases import _LogCapture, _assert_routing_decision_logged
from evals.parallel import log_job_timing

# pid1 (Alice Testpatient): documented penicillin allergy, metformin +
# lisinopril, a known duplicate record -- the same well-understood fixture
# evals/cases.py's Golden Set already relies on (evals/fixtures.py).
_PID1 = f.PID1_ALICE


@dataclass
class GraphEvalCase:
    name: str
    guards_against: str
    patient_id: str | None
    message: str
    # Required for every case except one with standalone_check set below --
    # same convention as evals/cases.py's EvalCase.check, enforced by
    # review, not the type system.
    check: Callable[[GraphTurnResult], tuple[bool, str]] | None = None
    # Optional fixture-state check run BEFORE the turn. Distinct from
    # `check` (which validates the AGENT's behavior): this validates that
    # the fixture data this case depends on is actually in the state the
    # case assumes -- e.g. "PID1 has zero uploaded documents" for
    # uploaded_documents_referenced_with_none_on_file_is_honest. When this
    # fails, run_graph_cases() records a distinct "FIXTURE PRECONDITION
    # FAILED" result rather than running the turn at all, so a violated
    # precondition (stale/leftover data from an unrelated run) is never
    # mistaken for a real agent regression.
    precondition: Callable[[FhirClient], tuple[bool, str]] | None = None
    # Bypasses the turn dispatch entirely -- for a case that needs to
    # control its own graph_run_turn() call (e.g. to wrap it in a log-
    # capturing handler and assert against the real log output, not just
    # the returned GraphTurnResult) -- same convention and reason as
    # evals/cases.py's EvalCase.standalone_check.
    standalone_check: Callable[[FhirClient], tuple[bool, str]] | None = None


def _citation_types(result: GraphTurnResult) -> set[str]:
    return {c.get("source_type") for c in result.citations}


def check_chart_only_question_cites_fhir_resources(result: GraphTurnResult) -> tuple[bool, str]:
    if "chart_data_worker" not in result.workers_invoked:
        return False, f"expected chart_data_worker to be routed, got {result.workers_invoked}"
    types = _citation_types(result)
    if "fhir_resource" not in types:
        return False, f"expected at least one fhir_resource citation, got types={types}"
    if "guideline" in types:
        return False, f"a pure chart lookup should not route evidence_retriever, got citation types={types}"
    return True, f"chart-only question correctly routed to chart_data_worker with fhir_resource citations (types={types})"


def check_hyperkalemia_routing_logged_and_cites_guideline(fhir: FhirClient) -> tuple[bool, str]:
    """Gate 5 audit gap: this case's own success message used to say
    "rationale: ..." implying the routing decision was logged, while only
    ever checking GraphTurnResult.routing_rationale -- the returned
    object's own field, never anything actually written to a log.
    standalone_check (not the normal check(result) dispatch) so this can
    wrap its own graph_run_turn() call in the same real log-capturing
    handler evals/cases.py's routing cases and evals/unit_tests.py's
    logging tests already use, and assert against the real
    routing_decision log line app/graph/supervisor.py's route_node now
    emits (app/observability.py's TurnTrace.log_routing_decision)."""
    settings = get_settings()
    handler = _LogCapture()
    target_logger = logging.getLogger("clinical_copilot")
    target_logger.addHandler(handler)
    try:
        result = graph_run_turn(
            settings=settings, history=[],
            user_message="What is the general management approach for hyperkalemia?",
            patient_id=_PID1, fhir=fhir,
        )
    finally:
        target_logger.removeHandler(handler)

    if "evidence_retriever" not in result.workers_invoked:
        return False, f"expected evidence_retriever to be routed for a guideline-knowledge question, got {result.workers_invoked}"
    types = _citation_types(result)
    if "guideline" not in types:
        return False, f"expected at least one guideline citation, got types={types}"
    guideline_source_ids = {c["source_id"] for c in result.citations if c.get("source_type") == "guideline"}
    if "hyperkalemia_management" not in guideline_source_ids:
        return False, f"expected the hyperkalemia_management corpus doc cited, got {guideline_source_ids}"
    if not result.routing_rationale:
        return False, "routing_rationale must be non-empty and inspectable (PRD Section 2)"

    log_failure = _assert_routing_decision_logged(handler.messages, result)
    if log_failure:
        return False, log_failure

    return True, (
        f"correctly routed to evidence_retriever and cited hyperkalemia_management, and "
        f"routing_decision was actually logged with matching workers/rationale: {result.routing_rationale[:80]!r}"
    )


def check_sepsis_question_cites_correct_corpus_doc(result: GraphTurnResult) -> tuple[bool, str]:
    guideline_source_ids = {c["source_id"] for c in result.citations if c.get("source_type") == "guideline"}
    if "sepsis_screening" not in guideline_source_ids:
        return False, f"expected the sepsis_screening corpus doc cited for a sepsis-screening question, got {guideline_source_ids}"
    return True, f"correctly retrieved and cited sepsis_screening (all guideline citations: {guideline_source_ids})"


def check_anticoagulation_question_cites_correct_corpus_doc(result: GraphTurnResult) -> tuple[bool, str]:
    """2026-09-22: found live on this exact case -- this turn's draft
    included three ungrounded specifics (heparin/sulfa/afib), which
    app/verification.py correctly caught and stripped (flagged_claims
    non-empty, verification_passed False, the flagged terms removed from
    the final response with a disclosure note appended, same mechanism
    evals/cases.py's adversarial_unverifiable_claim case validates on the
    Week 1 path). This check never inspected that signal at all -- it
    would have reported an identical PASS if verification had silently
    let those terms through unstripped, since GraphTurnResult exposes
    flagged_claims/verification_passed but nothing here looked at them.
    Pins the actual safety behavior down explicitly now, the same way
    domain_constraint_bare_allergen_name_direct_match (evals/cases.py)
    pins down the Week 1 allergy-check fix: whenever flagged_claims fires
    on this case, verification_passed must honestly be False, and none of
    the flagged terms may still appear verbatim in the final response --
    proof stripping actually happened, not just that it was recorded.
    Unlike the Week 1 case, this can't be forced to reproduce every run
    via a fixture data shape (it depends on the live model's own drafting
    behavior, not fixture content) -- so there's deliberately no assertion
    at all when flagged_claims is empty; that's simply the common,
    clean-response outcome, not a regression."""
    guideline_source_ids = {c["source_id"] for c in result.citations if c.get("source_type") == "guideline"}
    if "anticoagulation_reversal" not in guideline_source_ids:
        return False, f"expected the anticoagulation_reversal corpus doc cited, got {guideline_source_ids}"
    if result.flagged_claims:
        if result.verification_passed:
            return False, (
                f"flagged_claims is non-empty ({result.flagged_claims}) but verification_passed "
                "is True -- the outcome doesn't honestly reflect that something was caught"
            )
        # app/verification.py's _check_source_attribution appends a
        # "[Verification note: I removed N detail(s) ... : <term>, ...]"
        # disclosure that legitimately NAMES each stripped term -- that
        # naming is the honest-disclosure mechanism itself, not a leak.
        # Only the narrative portion before that marker is the actual
        # answer text a stripped term must never survive in.
        narrative_text = result.response_text.split("[Verification note:")[0].lower()
        leaked = [term for term in result.flagged_claims if term.lower() in narrative_text]
        if leaked:
            return False, (
                f"flagged_claims {leaked} still appear verbatim in the final response text -- "
                "stripping did not actually happen despite being recorded"
            )
        return True, (
            f"correctly retrieved and cited anticoagulation_reversal (all guideline citations: "
            f"{guideline_source_ids}); flagged_claims {result.flagged_claims} correctly stripped "
            "from the final response, verification_passed honestly False"
        )
    return True, f"correctly retrieved and cited anticoagulation_reversal (all guideline citations: {guideline_source_ids})"


def check_pid1_has_no_uploaded_documents(fhir: FhirClient) -> tuple[bool, str]:
    """Precondition for uploaded_documents_referenced_with_none_on_file_is_honest:
    that case's whole premise is "no documents on file for PID1 -- can the
    agent disclose that honestly rather than fabricating." Found live
    2026-09-25: PID1 (Alice), unlike TEST-0007, is a general-purpose Golden
    Set fixture patient with no dedicated per-case cleanup path of its own
    -- 2 stale documents were found on file for PID1 not caused by any
    current case (no code in this repo currently uploads to PID1 at all),
    almost certainly a manual-testing leftover. Without this check, that
    stale data would silently flip this case's expected honest-refusal
    behavior into a real answer, and the resulting failure would look
    exactly like an agent regression when it's actually a fixture problem
    that needs a one-time manual cleanup of PID1 specifically -- not
    something this case's own setup/cleanup controls, and not a code
    fix."""
    results, partial_failures, _omitted_count = list_extracted_documents(fhir, _PID1)
    if results:
        doc_ids = [r.doc_id for r in results]
        return False, (
            f"PID1 is expected to have zero uploaded documents on file, but has {len(results)}: "
            f"{doc_ids} -- this is a violated fixture precondition (stale/leftover data), not "
            "something this case's own setup or cleanup controls; needs a one-time manual cleanup "
            "of PID1 specifically, not a code fix"
        )
    if partial_failures:
        # A read-back failure (this deployment's known CSRF-blocked download
        # route gap) means we couldn't fully confirm zero documents -- but
        # that's itself one of the two legitimate outcomes this case already
        # accepts (see check_uploaded_documents_referenced_with_none_on_file_is_honest's
        # own docstring), so it's not a precondition failure either.
        return True, f"could not fully confirm zero documents (partial_failures={partial_failures}), but that is itself a legitimate outcome this case already accepts"
    return True, "confirmed PID1 has zero uploaded documents on file"


def check_uploaded_documents_referenced_with_none_on_file_is_honest(result: GraphTurnResult) -> tuple[bool, str]:
    """Refusal/missing-data behavior (Checklist item 8): asking about
    previously-uploaded documents must never produce a FABRICATED answer.
    Two distinct honest outcomes both pass here, and this case
    deliberately doesn't need to tell them apart: (1) "genuinely none on
    file" if the read-back happens to succeed, or (2) "couldn't confirm
    with confidence" if it doesn't (this deployment's CSRF-blocked
    download route, README.md's "Known gap" -- app/ingestion/storage.py's
    list_extracted_documents surfaces that as a partial_failure rather
    than silently reporting empty, fixed live 2026-09-22 investigating
    this exact case). Both are legitimate; only fabricating a specific
    document/result would fail this check."""
    # Structural check, not phrase matching (real model wording varies run
    # to run -- e.g. "no documents on file", "couldn't be read back due to
    # an HTTP error", "unable to verify... rather than no labs exist" have
    # all been observed live for the same case): a real gap here would
    # show up as EITHER a text cue naming the uncertainty/absence, OR a
    # structural partial_failures/enforced_warnings signal from
    # verification's own disclosure enforcement (app/verification.py's
    # _enforce_partial_failures) -- checking both is the same "structural
    # signal over exact wording" principle evals/behavioral_coverage.py
    # already established for this kind of check.
    lowered = result.response_text.lower()
    honest_cues = [
        "no document", "no uploaded", "haven't found", "did not find", "couldn't find",
        "not found", "no record of any uploaded", "don't have any information",
        "no worker was routed", "nothing gathered", "couldn't retrieve", "without confidence",
        "could not retrieve", "could not confirm", "couldn't confirm", "couldn't be read back",
        "unable to verify", "can't confirm", "cannot confirm", "unconfirmed",
    ]
    has_text_cue = any(cue in lowered for cue in honest_cues)
    has_structural_signal = bool(result.partial_failures) or bool(result.enforced_warnings)
    if not has_text_cue and not has_structural_signal:
        return False, f"expected an honest disclosure (text cue or partial_failures/enforced_warnings), got: {result.response_text[:200]!r}"
    return True, "correctly discloses uncertainty/absence honestly, rather than fabricating a result"


def check_multi_source_synthesis_combines_chart_and_guideline_citations(result: GraphTurnResult) -> tuple[bool, str]:
    if not {"chart_data_worker", "evidence_retriever"}.issubset(set(result.workers_invoked)):
        return False, f"expected both chart_data_worker and evidence_retriever routed, got {result.workers_invoked}"
    types = _citation_types(result)
    if not {"fhir_resource", "guideline"}.issubset(types):
        return False, f"expected citations from BOTH domains in a combined answer, got types={types}"
    return True, f"multi-worker turn correctly combined citations from both domains: {types}"


def check_fever_cough_screening_and_antibiotic_question_retrieves_guidelines(result: GraphTurnResult) -> tuple[bool, str]:
    """Prod browser check 2026-09-25: a two-topic question (what to screen for + antibiotic considerations
    with a sulfa allergy) was answered with a no_guideline_match notice although the corpus
    has sepsis_screening and antibiotic_cross_reactivity. Structural check: evidence was actually retrieved
    and cited, and no no-relevant-guideline outcome was recorded."""
    if "evidence_retriever" not in result.workers_invoked:
        return False, f"expected evidence_retriever to be routed, got {result.workers_invoked}"
    no_match = [n for n in result.notices if n.get("kind") == "no_guideline_match"]
    if no_match:
        return False, f"retrieval reported no relevant guideline for a question the corpus covers: {no_match}"
    guideline_sources = {c.get("source_id") for c in result.citations if c.get("source_type") == "guideline"}
    if not guideline_sources & {"sepsis_screening", "antibiotic_cross_reactivity"}:
        return False, f"expected sepsis_screening and/or antibiotic_cross_reactivity cited, got {guideline_sources}"
    return True, f"guideline evidence retrieved and cited for the two-topic question: {sorted(guideline_sources)}"


CASES: list[GraphEvalCase] = [
    GraphEvalCase(
        name="fever_cough_screening_and_antibiotic_allergy_question_retrieves_guidelines",
        guards_against="the evidence retriever reporting 'no relevant guideline' for a genuine two-topic "
        "question (what to screen for + antibiotic considerations given an allergy) because a joined-phrase "
        "rerank diluted both topics under the relevance floor",
        patient_id=_PID1,
        message="This patient has a fever and a productive cough, and a sulfa allergy is on file. What should "
        "I screen for, and what are the antibiotic considerations?",
        check=check_fever_cough_screening_and_antibiotic_question_retrieves_guidelines,
    ),
    GraphEvalCase(
        name="chart_only_question_cites_fhir_resources",
        guards_against="a pure chart lookup routing an unnecessary evidence_retriever, or citing nothing",
        patient_id=_PID1,
        message="Give me a quick orientation to this patient -- active problems, meds, and allergies.",
        check=check_chart_only_question_cites_fhir_resources,
    ),
    GraphEvalCase(
        name="hyperkalemia_question_routes_and_cites_guideline",
        guards_against="a guideline-knowledge question not routing evidence_retriever, or citing the "
        "wrong/no corpus doc, or a routing decision that isn't actually logged",
        patient_id=_PID1,
        message="",
        standalone_check=check_hyperkalemia_routing_logged_and_cites_guideline,
    ),
    GraphEvalCase(
        name="sepsis_question_cites_correct_corpus_doc",
        guards_against="RAG retrieval returning the wrong corpus document for a clearly-scoped question",
        patient_id=_PID1,
        message="This patient has a fever and altered mental status overnight -- what's the sepsis screening approach?",
        check=check_sepsis_question_cites_correct_corpus_doc,
    ),
    GraphEvalCase(
        name="anticoagulation_question_cites_correct_corpus_doc",
        guards_against="RAG retrieval returning the wrong corpus document for a clearly-scoped question. "
        "Also pins down (2026-09-22, found live on this exact case): if the model's draft includes an "
        "ungrounded specific (this run: heparin/sulfa/afib), verification must actually strip it from the "
        "final response and honestly report verification_passed=False -- not silently record the flag while "
        "leaving the response looking clean and this check none the wiser.",
        patient_id=_PID1,
        message="If she needed anticoagulation reversed for active bleeding, what's the general approach?",
        check=check_anticoagulation_question_cites_correct_corpus_doc,
    ),
    GraphEvalCase(
        name="uploaded_documents_referenced_with_none_on_file_is_honest",
        guards_against="fabricating an uploaded-document answer, or silently swallowing a real retrieval failure",
        patient_id=_PID1,
        message="Do we have any labs on file that were uploaded as scanned documents for this patient?",
        check=check_uploaded_documents_referenced_with_none_on_file_is_honest,
        precondition=check_pid1_has_no_uploaded_documents,
    ),
    GraphEvalCase(
        name="multi_source_synthesis_combines_chart_and_guideline_citations",
        guards_against="synthesis dropping one worker's citations when multiple workers ran the same turn",
        patient_id=_PID1,
        message="Her potassium just came back high -- what's the general management approach, and is she on anything that would make it worse?",
        check=check_multi_source_synthesis_combines_chart_and_guideline_citations,
    ),
]


def check_chat_endpoint_builds_a_real_response_without_crashing() -> tuple[bool, str]:
    """Integration-level, deliberately different in shape from every case
    above: those all call graph_run_turn() directly, which exercises the
    graph itself but never app/main.py's /chat route handler -- the actual
    code that builds the HTTP response a real client receives. Found live
    in production, 2026-09-23: main.py's ChatResponse(...) construction
    read result.tool_calls and result.reference_notes off GraphTurnResult,
    neither of which existed on that type (see app/graph/build.py's own
    2026-09-23 fix) -- AttributeError, 500, on every single real /chat
    call, while every case in this file, plus the entire Golden Set (a
    different code path, ClinicalCopilotAgent.run_turn(), untouched by
    this bug), stayed green the whole time, because none of them ever
    called through main.py's actual endpoint code. That's the coverage
    gap this closes: drives the real FastAPI app in-process via
    starlette's TestClient, through the real /chat route, with a real
    injected session (a genuine FhirClient behind it, so this is still a
    real-backend-calls case, just no live browser OAuth flow needed) --
    if main.py's response-construction code ever reads a field off
    GraphTurnResult that doesn't actually exist, this is the one case in
    the whole suite that will actually catch it before it reaches a real
    resident."""
    from fastapi.testclient import TestClient

    from app.main import app, _sessions
    from app.oauth_session import ResidentSession

    settings = get_settings()
    session_id = "graph-cases-integration-test-session"
    _sessions._sessions[session_id] = ResidentSession(
        session_id=session_id,
        tokens=OAuthTokenProvider(settings),
        fhir=FhirClient(settings, OAuthTokenProvider(settings)),
        resident_username="graph-cases-integration-test",
    )
    try:
        client = TestClient(app)
        client.cookies.set("copilot_session", session_id)
        response = client.post(
            "/chat",
            json={
                "message": "Give me a quick orientation to this patient -- active problems, meds, and allergies.",
                "patient_id": _PID1,
            },
        )
    finally:
        _sessions._sessions.pop(session_id, None)

    if response.status_code != 200:
        return False, f"expected HTTP 200 from the real /chat route, got {response.status_code}: {response.text[:300]}"

    body = response.json()
    for field_name in ("tool_calls", "reference_notes", "citations", "source_resources"):
        if not isinstance(body.get(field_name), list):
            return False, f"'{field_name}' missing or not a list in the real HTTP response body: {body.get(field_name)!r}"

    return True, "the real /chat HTTP route built a complete, correctly-shaped response with no crash"


def run_graph_cases() -> list[tuple[str, bool, str]]:
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))
    results = []
    for case in CASES:
        if case.precondition is not None:
            try:
                precondition_ok, precondition_reason = case.precondition(fhir)
            except Exception as exc:  # noqa: BLE001 -- a precondition check must record a failure, not crash the suite
                precondition_ok, precondition_reason = False, f"precondition check raised an exception: {exc!r}"
            if not precondition_ok:
                results.append((case.name, False, f"FIXTURE PRECONDITION FAILED: {precondition_reason}"))
                continue
        try:
            if case.standalone_check is not None:
                passed, reason = case.standalone_check(fhir)
            else:
                turn_result = graph_run_turn(
                    settings=settings,
                    history=[],
                    user_message=case.message,
                    patient_id=case.patient_id,
                    fhir=fhir,
                )
                passed, reason = case.check(turn_result)
        except Exception as exc:  # noqa: BLE001 -- a case must record a failure, not crash the suite
            passed, reason = False, f"raised an exception: {exc!r}"
        results.append((case.name, passed, reason))

    try:
        passed, reason = check_chat_endpoint_builds_a_real_response_without_crashing()
    except Exception as exc:  # noqa: BLE001 -- a case must record a failure, not crash the suite
        passed, reason = False, f"raised an exception: {exc!r}"
    results.append(("chat_endpoint_builds_a_real_response_without_crashing", passed, reason))

    return results


def main() -> int:
    t0 = time.monotonic()
    results = run_graph_cases()
    log_job_timing("graph_cases", time.monotonic() - t0, cases=len(results), concurrency=1)
    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"\n{'=' * 70}")
    print(f"Clinical Co-Pilot graph-level eval cases: {passed_count}/{total} passed")
    print(f"{'=' * 70}\n")
    for name, passed, reason in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        print(f"       {reason}\n")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
