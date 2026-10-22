"""Offline tests (no network, no FHIR, no LLM) for:

- verification's independent allergy re-check now following the curated class table (Bactrim, cephalosporins),
  with no sulfate false alarms;
- check_allergy_conflict's distinct "not_assessed" result for a drug it cannot evaluate, and its surfacing
  ("I couldn't check X against the allergy list -- verify with pharmacy"), never as safe;
- removal that no longer mangles the answer (only the offending bullet/sentence goes, newlines kept, orphaned
  lead-ins dropped);
- "no relevant guideline" as an info-level notice, distinct from a real failure.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from app.agent import SYSTEM_PROMPT, TOOLS
from app.graph.build import GraphTurnResult
from app.graph.notices import mentions_no_guideline, no_guideline_match_notice
from app.graph.supervisor import _SYNTHESIS_SYSTEM_PROMPT, synthesize_node
from app.graph.workers import evidence_retriever_node
from app.main import ChatResponse
from app.observability import TurnTrace
from app.schemas import CheckAllergyConflictOutput
from app.tools import ToolCallRecord, check_allergy_conflict
from app.verification import _check_source_attribution, verify_response
from evals.allergy_retrieval_tests import _FakeFhir
from evals.call_site_tests import _RecordingLLM, _tool_response
from evals.truncation_tests import _FakeLLM, _NoTools, _state

# --- verification's allergy re-check follows the class table ----------------------


def _verify(draft: str, *allergies: str, records: list | None = None):
    return verify_response(draft, records or [], "p1", _FakeFhir(*allergies))


def test_verification_catches_bactrim_even_when_the_model_never_called_the_allergy_check() -> None:
    out = _verify("Bactrim DS twice daily would cover this urinary infection.", "Sulfa drugs")
    assert not out.passed_domain_constraint, out
    assert "[HARD STOP" in out.final_response and "Sulfa drugs" in out.final_response, out.final_response
    assert out.final_response.count("[HARD STOP") == 1, "brand + strength ('Bactrim DS') is one finding, not two"
    assert any("bactrim" in w.lower() for w in out.enforced_warnings), out.enforced_warnings


def test_verification_catches_a_cephalosporin_by_brand_and_generic() -> None:
    for draft in ("Start ceftriaxone 1 g IV now.", "Rocephin is the usual empiric choice here.", "Give cefepime for coverage."):
        out = _verify(draft, "Cephalosporins")
        assert not out.passed_domain_constraint and "[HARD STOP" in out.final_response, (draft, out.final_response)
    ok = _verify("Start ceftriaxone 1 g IV now.", "Sulfa drugs")
    assert ok.passed_domain_constraint and "[HARD STOP" not in ok.final_response, "a sulfa allergy does not flag a cephalosporin"


def test_verification_has_no_sulfate_or_class_name_false_alarms() -> None:
    for draft in (
        "Give morphine sulfate 2 mg IV for pain.", "Replace magnesium sulfate per protocol.", "Ferrous sulfate daily.",
        "Sodium polystyrene sulfonate was discussed.", "She has a documented sulfa allergy on file.",
        "Avoid the sulfonamide class.", "Give amoxicillin 500 mg.",
    ):
        out = _verify(draft, "Sulfa drugs")
        assert out.passed_domain_constraint and "[HARD STOP" not in out.final_response, (draft, out.final_response)


def test_verification_does_not_add_a_hard_stop_when_the_answer_already_states_the_conflict() -> None:
    out = _verify("Avoid Bactrim -- she has a documented sulfa allergy.", "Sulfa drugs")
    assert out.passed_domain_constraint and "[HARD STOP" not in out.final_response, out.final_response


def test_the_legacy_drug_list_is_still_enforced_and_not_double_counted() -> None:
    out = _verify("Amoxicillin 500 mg three times daily.", "Penicillin (hives)")
    assert not out.passed_domain_constraint and out.final_response.count("[HARD STOP") == 1, out.final_response
    aspirin = _verify("Give aspirin 81 mg.", "aspirin")
    assert not aspirin.passed_domain_constraint, "non-antibiotic legacy drugs keep their direct-match enforcement"


# --- not_assessed ------------------------------------------------------------------


def _tool(allergy: str | None, medication: str) -> CheckAllergyConflictOutput:
    fhir = _FakeFhir(allergy) if allergy else _FakeFhir()
    out = check_allergy_conflict(fhir, {"patient_id": "p1", "medication_name": medication}, [])
    assert isinstance(out, CheckAllergyConflictOutput), out
    return out


def test_an_unrecognized_drug_that_does_not_name_match_is_not_assessed_never_no_conflict() -> None:
    for drug in ("codeine", "lisinopril", "warfarin", "some brand nobody curated"):
        out = _tool("Sulfa drugs", drug)
        assert out.assessment == "not_assessed" and out.conflict_found is False, (drug, out)
        assert out.not_assessed_reason and drug in out.not_assessed_reason and "could not be evaluated" in out.not_assessed_reason
        assert out.checked_allergy_count == 1


def test_assessment_values_for_conflict_no_conflict_and_no_recorded_allergies() -> None:
    conflict = _tool("Sulfa drugs", "Bactrim")
    assert conflict.assessment == "conflict" and conflict.conflict_found and conflict.not_assessed_reason is None
    assert _tool("codeine", "codeine").assessment == "conflict", "an unrecognized drug that name-matches is still a conflict"
    recognized_safe = _tool("Sulfa drugs", "amoxicillin")
    assert recognized_safe.assessment == "no_conflict" and not recognized_safe.conflict_found, "the table CAN evaluate amoxicillin"
    none_recorded = _tool(None, "codeine")
    assert none_recorded.assessment == "no_conflict" and none_recorded.checked_allergy_count == 0, "nothing to conflict with"


def test_agent_and_synthesis_prompts_and_tool_description_forbid_calling_it_safe() -> None:
    for prompt in (SYSTEM_PROMPT, _SYNTHESIS_SYSTEM_PROMPT):
        assert "couldn't check" in prompt and "verify with pharmacy" in prompt and "never" in prompt.lower(), prompt[:60]
    assert "not_assessed" in SYSTEM_PROMPT
    tool = next(t for t in TOOLS if t["name"] == "check_allergy_conflict")
    assert "not_assessed" in tool["description"] and "NOT a clearance" in tool["description"]


def _unassessed_record(med: str = "codeine") -> ToolCallRecord:
    return ToolCallRecord(tool_name="check_allergy_conflict", patient_id="p1", output=_tool("Sulfa drugs", med))


def test_verification_appends_a_couldnt_check_note_when_the_answer_names_an_unassessed_drug() -> None:
    out = _verify("Codeine 30 mg every 6 hours is reasonable for the pain.", "Sulfa drugs", records=[_unassessed_record()])
    assert "[Allergy check note: I couldn't check codeine against this patient's allergy list" in out.final_response, out.final_response
    assert "NOT a clearance" in out.final_response and "verify with pharmacy" in out.final_response
    assert out.enforced_warnings == ["allergy_check_not_assessed: codeine"], out.enforced_warnings
    assert out.passed_domain_constraint and out.passed_source_attribution, "no wrong claim was made, so it is not a failed check"


def test_verification_does_not_repeat_a_disclosure_the_answer_already_made() -> None:
    said = "I couldn't check codeine against the allergy list -- verify with pharmacy before giving it."
    out = _verify(said, "Sulfa drugs", records=[_unassessed_record()])
    assert out.final_response == said and out.enforced_warnings == [], out.final_response
    unmentioned = _verify("Give acetaminophen for pain.", "Sulfa drugs", records=[_unassessed_record()])
    assert "[Allergy check note" not in unmentioned.final_response, "a checked drug the answer never names needs no note"
    assessed = _verify("Codeine is fine.", "Sulfa drugs", records=[ToolCallRecord("check_allergy_conflict", "p1", _tool("Sulfa drugs", "Bactrim"))])
    assert "[Allergy check note" not in assessed.final_response


# --- removal formatting --------------------------------------------------------------

_DRAFT = """**Screening considerations**
- Screen for sepsis (qSOFA, lactate, SIRS criteria).
- Evaluate for pneumonia with a chest X-ray and oxygen saturation.
- Check for other causes of fever.

**Antibiotic considerations**
The chart records a sulfa allergy, so avoid Bactrim. Options such as azithromycin or doxycycline could be considered if a respiratory source is likely."""
_GROUNDED = {"sulfa drugs", "copd"}


def _strip(draft: str, grounded: set[str] = _GROUNDED) -> str:
    return _check_source_attribution(draft, grounded)[0].split("\n\n[Verification note")[0]


def test_removal_takes_only_the_offending_bullets_and_keeps_every_other_line_intact() -> None:
    assert _strip(_DRAFT) == (
        "**Screening considerations**\n- Check for other causes of fever.\n\n**Antibiotic considerations**\n"
        "The chart records a sulfa allergy, so avoid Bactrim."
    )
    final, flagged = _check_source_attribution(_DRAFT, _GROUNDED)
    assert sorted(flagged) == ["azithromycin", "pneumonia", "sepsis"]
    assert final.endswith("azithromycin, pneumonia, sepsis.]") and "\n\n[Verification note: I removed 3 detail(s)" in final


def test_removal_never_glues_lines_together() -> None:
    out = _strip(_DRAFT)
    for kept in ("- Check for other causes of fever.", "**Antibiotic considerations**", "**Screening considerations**"):
        assert kept in out.split("\n"), f"{kept!r} must remain a line of its own: {out!r}"
    assert "fever. **" not in out


def test_removal_drops_a_lead_in_whose_whole_body_was_removed() -> None:
    assert _strip("Key points:\n- Consider sepsis.\n- Rule out pneumonia.\n\nThe chart lists COPD.", {"copd"}) == "The chart lists COPD."
    assert _strip("## Differential\n\n- sepsis\n- pneumonia\n\n## Plan\n\nRecheck COPD.", {"copd"}) == "## Plan\n\nRecheck COPD."
    partial = _strip("Key points:\n- Consider sepsis.\n- Check the COPD plan.", {"copd"})
    assert partial == "Key points:\n- Check the COPD plan.", "a lead-in with a surviving item stays"


def test_removal_keeps_abbreviations_whole_and_preserves_blank_lines_and_untouched_text() -> None:
    kept = _strip("Use agents (e.g. albuterol) for shifting. The patient has COPD. She is on warfarin now.", {"copd", "albuterol"})
    assert kept == "Use agents (e.g. albuterol) for shifting. The patient has COPD."
    para = "First paragraph about COPD.\n\nSecond paragraph mentions sepsis.\n\nThird about COPD."
    assert _strip(para, {"copd"}) == "First paragraph about COPD.\n\nThird about COPD."
    fully_grounded = "Line one about COPD.\n- bullet\n\n\nLine four."
    assert _check_source_attribution(fully_grounded, {"copd"}) == (fully_grounded, []), "nothing flagged -> byte-for-byte unchanged"


def test_removal_removes_only_the_offending_sentence_inside_a_paragraph() -> None:
    assert _strip("She has COPD. She also has sepsis. Recheck the COPD plan tomorrow.", {"copd"}) == "She has COPD. Recheck the COPD plan tomorrow."


# --- notices ----------------------------------------------------------------------------


def test_the_no_matching_guideline_eval_checks_accept_the_notice_as_their_structural_signal() -> None:
    """Regression: these two golden-set checks used partial_failures as the structural sign of an honest
    no-match answer; when no-match became a notice the live case failed twice in CI."""
    from evals.cases import _check_rag_no_matching_guideline_honest, _check_rag_vague_broader_than_corpus_honest_no_match

    plain = "I searched but didn't find anything in the available sources that directly answers this."
    for check in (_check_rag_no_matching_guideline_honest, _check_rag_vague_broader_than_corpus_honest_no_match):
        with_notice = SimpleNamespace(citations=[], response_text=plain, partial_failures=[], notices=[no_guideline_match_notice()])
        assert check(with_notice)[0], check(with_notice)
        bare = SimpleNamespace(citations=[], response_text="Here is my general advice.", partial_failures=[], notices=[])
        assert not check(bare)[0], "no cue, no partial failure, no notice must still fail"
        failed = SimpleNamespace(citations=[], response_text="Here is my general advice.", partial_failures=["evidence_retriever: x"], notices=[])
        assert check(failed)[0], "a real failure is still a structural signal"


def _trace() -> TurnTrace:
    return TurnTrace(client=None, span=None, correlation_id="notice-test")


def _retrieval_node(retriever, *, stop_reason: str = "tool_use") -> dict:
    llm = _RecordingLLM(_tool_response("extract_query", {"queries": ["acute pancreatitis management"]}, stop_reason=stop_reason))
    return evidence_retriever_node({"user_message": "q"}, llm=llm, model="m", retriever=retriever, trace=_trace())


def test_a_search_with_no_relevant_result_is_an_info_notice_not_a_partial_failure() -> None:
    out = _retrieval_node(SimpleNamespace(retrieve=lambda queries: []))
    assert out["partial_failures"] == [], out
    assert out["notices"] == [no_guideline_match_notice()], out["notices"]
    n = out["notices"][0]
    assert n["kind"] == "no_guideline_match" and n["severity"] == "info" and n["label"] == "No matching guideline"
    assert "isn't based on one" in n["text"] and "[" not in n["text"] and "failure" not in n["text"].lower()


def test_real_retrieval_failures_stay_partial_failures_with_no_notice() -> None:
    def broken(queries):
        raise RuntimeError("reranker down")

    out = _retrieval_node(SimpleNamespace(retrieve=broken))
    assert out["notices"] == [] and any("unavailable after retry" in f for f in out["partial_failures"]), out
    truncated = _retrieval_node(SimpleNamespace(retrieve=lambda q: []), stop_reason="max_tokens")
    assert truncated["notices"] == [] and any("cut off" in f for f in truncated["partial_failures"]), truncated


def _synthesize(**state_overrides) -> dict:
    return synthesize_node(_state(**state_overrides), llm=_FakeLLM("", "end_turn", ["text"]), model="m", fhir=_NoTools(), trace=_trace())


def test_a_no_match_notice_adds_plain_wording_and_no_warning_bracket_or_enforced_code() -> None:
    chart = SimpleNamespace(draft_text="The chart lists two active problems.", turn_records=[])
    out = _synthesize(chart_facts=chart, workers_invoked=["chart_data_worker", "evidence_retriever"], notices=[no_guideline_match_notice()])
    text = out["final_response"]
    assert text.startswith("The chart lists two active problems.") and text.endswith(no_guideline_match_notice()["text"]), text
    assert "[Verification note" not in text
    assert out["verification_outcome"]["enforced_warnings"] == [], out["verification_outcome"]
    assert out["verification_outcome"]["passed_source_attribution"] and out["verification_outcome"]["passed_domain_constraint"]


def test_the_plain_sentence_is_not_repeated_when_the_answer_already_says_it() -> None:
    chart = SimpleNamespace(draft_text="There is no guideline in the reference set for this; the chart lists two active problems.", turn_records=[])
    out = _synthesize(chart_facts=chart, notices=[no_guideline_match_notice()])
    assert out["final_response"] == chart.draft_text, out["final_response"]
    assert mentions_no_guideline("I didn't find a guideline that covers this") and not mentions_no_guideline("The chart lists two active problems.")


def test_a_notice_only_turn_is_answered_with_the_notice_not_the_generic_rephrase_message() -> None:
    out = _synthesize(chart_facts=None, workers_invoked=["evidence_retriever"], notices=[no_guideline_match_notice()])
    text = out["final_response"]
    assert "didn't find anything in the available sources" in text and no_guideline_match_notice()["text"] in text
    assert "Please rephrase" not in text and text.count(no_guideline_match_notice()["text"]) == 1
    assert out["verification_outcome"]["enforced_warnings"] == []


def test_a_real_worker_failure_still_adds_the_warning_and_the_bracket() -> None:
    chart = SimpleNamespace(draft_text="The chart lists two active problems.", turn_records=[])
    failure = "evidence_retriever: retrieval service unavailable after retry -- answering without guideline evidence"
    out = _synthesize(chart_facts=chart, partial_failures=[failure])
    assert "worker_partial_failure" in out["verification_outcome"]["enforced_warnings"]
    assert f"[Verification note: {failure}.]" in out["final_response"]


def test_notices_travel_through_the_result_and_the_http_response_with_safe_defaults() -> None:
    assert ChatResponse.model_fields["notices"].default_factory() == []
    result = GraphTurnResult(correlation_id="c", response_text="t", verification_passed=True, passed_domain_constraint=True,
                             flagged_claims=[], enforced_warnings=[], workers_invoked=[], routing_rationale="r")
    assert result.notices == []
    n = no_guideline_match_notice()
    response = ChatResponse(conversation_id="x", correlation_id="c", response="t", verification_passed=True, flagged_claims=[],
                            enforced_warnings=[], tool_calls=[], source_resources=[], reference_notes=[], notices=[n])
    assert response.notices == [n]


_TESTS: list[Callable[[], None]] = [
    test_verification_catches_bactrim_even_when_the_model_never_called_the_allergy_check,
    test_verification_catches_a_cephalosporin_by_brand_and_generic,
    test_verification_has_no_sulfate_or_class_name_false_alarms,
    test_verification_does_not_add_a_hard_stop_when_the_answer_already_states_the_conflict,
    test_the_legacy_drug_list_is_still_enforced_and_not_double_counted,
    test_an_unrecognized_drug_that_does_not_name_match_is_not_assessed_never_no_conflict,
    test_assessment_values_for_conflict_no_conflict_and_no_recorded_allergies,
    test_agent_and_synthesis_prompts_and_tool_description_forbid_calling_it_safe,
    test_verification_appends_a_couldnt_check_note_when_the_answer_names_an_unassessed_drug,
    test_verification_does_not_repeat_a_disclosure_the_answer_already_made,
    test_removal_takes_only_the_offending_bullets_and_keeps_every_other_line_intact,
    test_removal_never_glues_lines_together,
    test_removal_drops_a_lead_in_whose_whole_body_was_removed,
    test_removal_keeps_abbreviations_whole_and_preserves_blank_lines_and_untouched_text,
    test_removal_removes_only_the_offending_sentence_inside_a_paragraph,
    test_the_no_matching_guideline_eval_checks_accept_the_notice_as_their_structural_signal,
    test_a_search_with_no_relevant_result_is_an_info_notice_not_a_partial_failure,
    test_real_retrieval_failures_stay_partial_failures_with_no_notice,
    test_a_no_match_notice_adds_plain_wording_and_no_warning_bracket_or_enforced_code,
    test_the_plain_sentence_is_not_repeated_when_the_answer_already_says_it,
    test_a_notice_only_turn_is_answered_with_the_notice_not_the_generic_rephrase_message,
    test_a_real_worker_failure_still_adds_the_warning_and_the_bracket,
    test_notices_travel_through_the_result_and_the_http_response_with_safe_defaults,
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
