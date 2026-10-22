"""Offline tests (no network, no FHIR, no LLM) for two problems found in a prod
browser check (patient with fever + productive cough + a sulfa allergy):

1. check_allergy_conflict("Bactrim") reported no conflict against a recorded
   "Sulfa drugs" allergy -- the curated class table held only penicillins.
2. The evidence retriever reported "no relevant guideline" for a two-topic
   question even though the corpus has both guidelines -- one rerank against
   the joined phrases pushed both topics under the relevance floor.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from app.agent import ChatTurnResult
from app.graph.notices import no_guideline_match_notice
from app.clinical_reference import DRUG_CLASSES, cross_reactive_class, drug_classes_for, normalize_for_match
from app.retrieval import retriever as retriever_module
from app.retrieval.chunking import GuidelineChunk
from app.retrieval.query import _QUERY_SYSTEM_PROMPT
from app.retrieval.retriever import GuidelineRetriever
from app.tools import ToolCallRecord, check_allergy_conflict
from evals.cases import CASE_SULFA_BRAND_NAME_ALLERGY, _check_sulfa_brand_name_allergy
from evals.graph_cases import check_fever_cough_screening_and_antibiotic_question_retrieves_guidelines

SULFA = "sulfonamide antibiotics"


# --- check_allergy_conflict ----------------------------------------------------


class _FakeFhir:
    """Just enough FhirClient for check_allergy_conflict: one allergy list."""

    def __init__(self, *allergies: str, narrative_only: bool = False) -> None:
        entries = []
        for i, text in enumerate(allergies):
            if narrative_only:
                resource = {"resourceType": "AllergyIntolerance", "id": f"a{i}",
                            "text": {"status": "additional", "div": f"<div>{text}</div>"}}
            else:
                resource = {"resourceType": "AllergyIntolerance", "id": f"a{i}", "code": {"text": text}}
            entries.append({"resource": resource})
        self._bundle = {"resourceType": "Bundle", "type": "searchset", "entry": entries}

    def search_fresh(self, resource_type: str, params: dict) -> dict:
        assert resource_type == "AllergyIntolerance"
        return self._bundle


def _check(allergy: str, medication: str, *, narrative_only: bool = False):
    out = check_allergy_conflict(_FakeFhir(allergy, narrative_only=narrative_only),
                                 {"patient_id": "p1", "medication_name": medication}, [])
    assert not hasattr(out, "detail_code"), out  # not a ToolFailure
    return out


def _flags(allergy: str, medication: str) -> bool:
    return _check(allergy, medication).conflict_found


def test_sulfa_allergy_flags_bactrim_and_the_other_sulfonamide_antibiotics_by_brand_and_generic() -> None:
    meds = [
        "Bactrim", "Bactrim DS", "bactrim", "Septra", "Septra DS", "sulfamethoxazole-trimethoprim",
        "sulfamethoxazole/trimethoprim", "trimethoprim-sulfamethoxazole", "TMP-SMX", "SMX/TMP", "co-trimoxazole",
        "Sulfatrim", "sulfamethoxazole", "sulfadiazine", "silver sulfadiazine", "Silvadene", "sulfasalazine",
        "Azulfidine", "sulfisoxazole", "sulfacetamide",
    ]
    for allergy in ("Sulfa drugs", "sulfa", "Sulfonamide antibiotics", "Sulfa (rash)", "SULFA ALLERGY", "sulfamethoxazole"):
        for med in meds:
            out = _check(allergy, med)
            assert out.conflict_found, f"{allergy!r} must flag {med!r}"
            assert out.cross_reactive_class in (SULFA, None), (allergy, med, out.cross_reactive_class)
    exact = _check("Sulfa drugs", "Bactrim")
    assert exact.cross_reactive_class == SULFA and exact.matched_allergy_text == "Sulfa drugs"
    assert exact.low_confidence is False and exact.source_resources == ["AllergyIntolerance/a0"]


def test_an_uncoded_narrative_sulfa_allergy_flags_bactrim_with_low_confidence() -> None:
    out = _check("Sulfa allergy -- hives", "Bactrim", narrative_only=True)
    assert out.conflict_found and out.low_confidence and out.cross_reactive_class == SULFA, out


def test_sulfa_allergy_does_not_flag_sulfates_or_unrelated_drugs() -> None:
    safe = [
        "morphine sulfate", "magnesium sulfate", "ferrous sulfate", "protamine sulfate", "atropine sulfate",
        "sodium polystyrene sulfonate", "amoxicillin", "ceftriaxone", "azithromycin", "doxycycline", "trimethoprim",
        "nitrofurantoin", "furosemide", "hydrochlorothiazide", "metformin",
    ]
    for med in safe:
        assert not _flags("Sulfa drugs", med), f"a sulfa allergy must not flag {med!r}"
        assert not _flags("sulfa", med), f"a bare 'sulfa' allergy must not flag {med!r}"


def test_a_sulfite_or_sulfate_allergy_is_not_a_sulfa_allergy() -> None:
    for text in ("sulfites", "Sulfite sensitivity", "sulfate"):
        assert not _flags(text, "Bactrim"), text
    assert _flags("sulfites", "sulfites"), "the allergy text is still compared to itself, never silently skipped"


def test_penicillin_allergy_flags_brand_names_but_not_cephalosporins_by_design() -> None:
    for med in ("Augmentin", "Zosyn", "Unasyn", "Amoxil", "pip-tazo", "ampicillin-sulbactam", "Bicillin", "Pen VK",
                "penicillin V potassium", "amoxicillin/clavulanate", "dicloxacillin", "nafcillin"):
        assert _flags("Penicillin allergy (hives)", med), med
    # Cross-reactivity depends on shared side chains, not the beta-lactam ring (see
    # app/retrieval/corpus/antibiotic_cross_reactivity.md): not auto-flagged. Change deliberately, not silently.
    for med in ("ceftriaxone", "cephalexin", "Keflex", "meropenem", "Bactrim"):
        assert not _flags("Penicillin allergy (hives)", med), med


def test_cephalosporin_allergy_flags_other_cephalosporins_by_brand_and_generic() -> None:
    for allergy in ("cephalosporins", "cefazolin", "Keflex"):
        for med in ("ceftriaxone", "Rocephin", "cefepime", "Maxipime", "cephalexin", "Ancef", "cefdinir", "Omnicef", "Teflaro"):
            assert _flags(allergy, med), (allergy, med)
        for med in ("amoxicillin", "Augmentin", "azithromycin", "Bactrim"):
            assert not _flags(allergy, med), (allergy, med)


def test_other_antibiotic_classes_and_single_drug_brands_match_in_both_directions() -> None:
    pairs = [
        ("azithromycin", "Zithromax"), ("azithromycin", "Z-Pak"), ("clarithromycin", "Biaxin"), ("macrolides", "erythromycin"),
        ("ciprofloxacin", "Levaquin"), ("fluoroquinolones", "Avelox"), ("quinolone", "Cipro"),
        ("doxycycline", "minocycline"), ("tetracycline", "Vibramycin"),
        ("meropenem", "Primaxin"), ("carbapenems", "Invanz"),
        ("gentamicin", "tobramycin"), ("aminoglycosides", "amikacin"),
        ("metronidazole", "Flagyl"), ("clindamycin", "Cleocin"), ("vancomycin", "Vancocin"),
        ("nitrofurantoin", "Macrobid"), ("linezolid", "Zyvox"),
    ]
    for a, b in pairs:
        assert _flags(a, b) and _flags(b, a), (a, b)
    for a, b in (("azithromycin", "ciprofloxacin"), ("doxycycline", "Zithromax"), ("Flagyl", "Cleocin"), ("Macrobid", "Bactrim")):
        assert not _flags(a, b), (a, b)


def test_direct_name_matching_still_works_for_drugs_outside_the_table() -> None:
    assert _flags("codeine", "codeine phosphate") and _flags("Latex", "latex")
    assert _flags("penicillin", "penicillin") and _check("penicillin", "penicillin").cross_reactive_class is None
    assert not _flags("peanut", "Bactrim")


def test_drug_class_table_is_well_formed() -> None:
    seen: dict[str, str] = {}
    for drug_class, aliases in DRUG_CLASSES.items():
        assert aliases, drug_class
        for alias in aliases:
            assert alias == alias.lower() and alias == alias.strip(), alias
            assert alias not in seen, f"{alias!r} is in both {seen.get(alias)!r} and {drug_class!r}"
            seen[alias] = drug_class
            assert drug_class in drug_classes_for(alias), f"{alias!r} must resolve to its own class {drug_class!r}"
    assert cross_reactive_class("", "Bactrim") is None and cross_reactive_class("sulfa", "") is None
    assert normalize_for_match("Morphine SULFATE  (IV)") == "morphine (iv)"
    assert drug_classes_for("penicillin and sulfa (hives)") == ["penicillins", SULFA]


# --- evidence retriever: rerank per phrase ---------------------------------------


def _chunk(source: str, index: int) -> GuidelineChunk:
    return GuidelineChunk(source_id=source, chunk_id=f"{source}:{index}", text=f"{source} passage {index}", page_or_section="s")


class _Index:
    def __init__(self, by_query: dict[str, list[GuidelineChunk]]) -> None:
        self._by_query = by_query

    def search(self, query: str, top_k: int) -> list:
        return [(c, 1.0) for c in self._by_query.get(query, [])]


class _DilutingReranker:
    """Models what was measured with the real cross-encoder: a phrase scores well against its own topic's
    chunks, and the SAME phrase scores far worse when it is joined with another topic's phrase."""

    def __init__(self, scores: dict[tuple[str, str], float]) -> None:
        self._scores = scores
        self.calls: list[str] = []

    def rerank(self, query: str, candidates: list, top_k: int) -> list:
        self.calls.append(query)
        penalty = -6.0 if " ; " in query else 0.0
        scored = [(c, self._scores.get((query, c.source_id), -9.0) + penalty) for c in candidates]
        return sorted(scored, key=lambda p: p[1], reverse=True)[:top_k]


def _retriever(dense: dict, reranker: _DilutingReranker) -> GuidelineRetriever:
    r = object.__new__(GuidelineRetriever)
    r._dense = _Index(dense)
    r._sparse = _Index({})
    r._reranker = reranker
    return r


_SEPSIS, _ABX = _chunk("sepsis_screening", 0), _chunk("antibiotic_cross_reactivity", 0)
_Q1, _Q2 = "sepsis screening fever", "antibiotic selection drug allergy"


def test_a_two_topic_question_keeps_both_topics_instead_of_reporting_no_relevant_guideline() -> None:
    reranker = _DilutingReranker({(_Q1, "sepsis_screening"): 0.2, (_Q2, "antibiotic_cross_reactivity"): 2.4})
    results = _retriever({_Q1: [_SEPSIS], _Q2: [_ABX]}, reranker).retrieve([_Q1, _Q2])
    assert {r.chunk.source_id for r in results} == {"sepsis_screening", "antibiotic_cross_reactivity"}, results
    joined = " ; ".join([_Q1, _Q2])
    assert joined not in reranker.calls, "the phrases must not be reranked as one diluted joined query"
    assert sorted(reranker.calls) == sorted([_Q1, _Q2]), reranker.calls  # once per phrase


def test_each_chunk_is_scored_by_its_best_phrase_and_the_floor_is_still_per_chunk() -> None:
    other = _chunk("hyperkalemia_management", 0)
    reranker = _DilutingReranker({(_Q1, "sepsis_screening"): 0.2, (_Q2, "sepsis_screening"): -8.0,
                                  (_Q2, "antibiotic_cross_reactivity"): 2.4, (_Q1, "hyperkalemia_management"): -7.0,
                                  (_Q2, "hyperkalemia_management"): -7.5})
    results = _retriever({_Q1: [_SEPSIS, other], _Q2: [_ABX]}, reranker).retrieve([_Q1, _Q2])
    by_source = {r.chunk.source_id: r.rerank_score for r in results}
    assert by_source == {"antibiotic_cross_reactivity": 2.4, "sepsis_screening": 0.2}, by_source
    assert [r.rerank_score for r in results] == sorted(by_source.values(), reverse=True)


def test_out_of_scope_questions_still_return_nothing() -> None:
    reranker = _DilutingReranker({})  # every (phrase, chunk) pair scores -9, far under the floor
    assert _retriever({"pancreatitis": [_SEPSIS], "asthma": [_ABX]}, reranker).retrieve(["pancreatitis", "asthma"]) == []
    assert retriever_module._MIN_RELEVANCE_SCORE == -3.4, "the calibrated floor is unchanged"


def test_a_single_phrase_is_reranked_once_exactly_as_before() -> None:
    reranker = _DilutingReranker({(_Q1, "sepsis_screening"): 5.0})
    results = _retriever({_Q1: [_SEPSIS]}, reranker).retrieve([_Q1])
    assert reranker.calls == [_Q1] and [r.chunk.chunk_id for r in results] == ["sepsis_screening:0"]


def test_query_extraction_prompt_steers_toward_concepts_and_still_forbids_identifiers() -> None:
    prompt = _QUERY_SYSTEM_PROMPT
    assert "CONCEPT" in prompt and "sepsis screening" in prompt and "sulfa" in prompt
    assert "Never include: the patient's name" in prompt and "MRN" in prompt


# --- the live eval cases' own check functions -----------------------------------


def _turn(record_output, text: str) -> ChatTurnResult:
    records = [] if record_output is None else [ToolCallRecord(tool_name="check_allergy_conflict", patient_id="p1", output=record_output)]
    return ChatTurnResult(correlation_id="c", response_text=text, verification_passed=True, passed_domain_constraint=True,
                          flagged_claims=[], enforced_warnings=[], accumulated_tool_records=records)


def test_the_sulfa_eval_case_check_passes_on_real_tool_output_and_fails_when_the_gap_returns() -> None:
    real = _check("Sulfa drugs", "Bactrim")  # the real tool, run against a recorded sulfa allergy
    ok, reason = _check_sulfa_brand_name_allergy(_turn(real, "Bactrim is contraindicated: documented sulfa allergy."))
    assert ok, reason
    ok, reason = _check_sulfa_brand_name_allergy(_turn(real, "Bactrim is fine to start."))
    assert not ok and "not surfaced" in reason, reason
    no_conflict = real.model_copy(update={"conflict_found": False, "matched_allergy_text": None, "cross_reactive_class": None})
    ok, reason = _check_sulfa_brand_name_allergy(_turn(no_conflict, "No allergy conflict found for Bactrim."))
    assert not ok and "gap is back" in reason, reason
    ok, reason = _check_sulfa_brand_name_allergy(_turn(None, "Consider Bactrim."))
    assert not ok and "never run" in reason, reason
    other_drug = _check("Sulfa drugs", "amoxicillin")
    assert not _check_sulfa_brand_name_allergy(_turn(other_drug, "allergy checked"))[0], "a non-sulfa check doesn't count"
    assert CASE_SULFA_BRAND_NAME_ALLERGY.expects_verification_failure and "Bactrim" in CASE_SULFA_BRAND_NAME_ALLERGY.message


def test_the_retrieval_graph_case_check_fails_on_the_no_relevant_guideline_outcome() -> None:
    check = check_fever_cough_screening_and_antibiotic_question_retrieves_guidelines
    cite = {"source_type": "guideline", "source_id": "sepsis_screening"}
    good = SimpleNamespace(workers_invoked=["chart_data_worker", "evidence_retriever"], notices=[], citations=[cite])
    assert check(good)[0]
    no_match = SimpleNamespace(
        workers_invoked=["evidence_retriever"], citations=[], notices=[no_guideline_match_notice()])
    ok, reason = check(no_match)
    assert not ok and "no relevant guideline" in reason, reason
    assert not check(SimpleNamespace(workers_invoked=["chart_data_worker"], notices=[], citations=[cite]))[0]
    assert not check(SimpleNamespace(workers_invoked=["evidence_retriever"], notices=[], citations=[]))[0]
    other_doc = {"source_type": "guideline", "source_id": "hyperkalemia_management"}
    assert not check(SimpleNamespace(workers_invoked=["evidence_retriever"], notices=[], citations=[other_doc]))[0]


# --- the real models, on this exact question shape --------------------------------

_IN_SCOPE_SHAPES = {
    "two concept phrases": ["sepsis screening fever productive cough", "antibiotic selection sulfa allergy"],
    "screening + allergy cross-reactivity": ["pneumonia sepsis screening", "sulfonamide allergy antibiotic cross-reactivity"],
    "symptom-style phrases": ["what to screen for in fever with productive cough", "antibiotic considerations with sulfa allergy"],
    "three phrases": ["fever with productive cough screening", "sepsis screening", "antibiotic allergy cross-reactivity sulfa"],
}
_OUT_OF_SCOPE = [["acute pancreatitis management", "asthma exacerbation treatment"], ["migraine prophylaxis", "gout flare treatment"]]


def test_real_models_retrieve_a_guideline_for_the_fever_cough_sulfa_allergy_question_shape() -> None:
    try:
        real = retriever_module.get_retriever()
    except Exception as exc:  # noqa: BLE001 -- models unavailable (no cache/network): say so, never pass silently
        raise AssertionError(f"SKIPPED-BUT-FAILING: real retrieval models could not load: {exc!r}") from exc
    for label, phrases in _IN_SCOPE_SHAPES.items():
        sources = {e.chunk.source_id for e in real.retrieve(phrases)}
        assert sources, f"{label}: nothing retrieved -- the 'no relevant guideline' bug"
        assert sources & {"sepsis_screening", "antibiotic_cross_reactivity"}, (label, sources)
    # Measured with the real models: the joined-query rerank kept ONLY antibiotic_cross_reactivity here (the
    # sepsis phrase's +2.2 was diluted under the floor); scoring each phrase on its own keeps BOTH documents.
    two_topics = {e.chunk.source_id for e in real.retrieve(_IN_SCOPE_SHAPES["screening + allergy cross-reactivity"])}
    assert two_topics == {"sepsis_screening", "antibiotic_cross_reactivity"}, two_topics
    three = {e.chunk.source_id for e in real.retrieve(_IN_SCOPE_SHAPES["three phrases"])}
    assert three == {"sepsis_screening", "antibiotic_cross_reactivity"}, three
    for phrases in _OUT_OF_SCOPE:
        assert real.retrieve(phrases) == [], f"out-of-scope question retrieved evidence: {phrases}"


_TESTS: list[Callable[[], None]] = [
    test_sulfa_allergy_flags_bactrim_and_the_other_sulfonamide_antibiotics_by_brand_and_generic,
    test_an_uncoded_narrative_sulfa_allergy_flags_bactrim_with_low_confidence,
    test_sulfa_allergy_does_not_flag_sulfates_or_unrelated_drugs,
    test_a_sulfite_or_sulfate_allergy_is_not_a_sulfa_allergy,
    test_penicillin_allergy_flags_brand_names_but_not_cephalosporins_by_design,
    test_cephalosporin_allergy_flags_other_cephalosporins_by_brand_and_generic,
    test_other_antibiotic_classes_and_single_drug_brands_match_in_both_directions,
    test_direct_name_matching_still_works_for_drugs_outside_the_table,
    test_drug_class_table_is_well_formed,
    test_a_two_topic_question_keeps_both_topics_instead_of_reporting_no_relevant_guideline,
    test_each_chunk_is_scored_by_its_best_phrase_and_the_floor_is_still_per_chunk,
    test_out_of_scope_questions_still_return_nothing,
    test_a_single_phrase_is_reranked_once_exactly_as_before,
    test_query_extraction_prompt_steers_toward_concepts_and_still_forbids_identifiers,
    test_the_sulfa_eval_case_check_passes_on_real_tool_output_and_fails_when_the_gap_returns,
    test_the_retrieval_graph_case_check_fails_on_the_no_relevant_guideline_outcome,
    test_real_models_retrieve_a_guideline_for_the_fever_cough_sulfa_allergy_question_shape,
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
