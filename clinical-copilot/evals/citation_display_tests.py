"""Offline tests (no network, no FHIR, no LLM) for citation display: readable
chart-record labels, guideline document titles, markdown-free excerpts that
never start or end on a cut-off word, and overlap trimming between adjacent
chunks. The browser side (one card per document, modal, escape-first
rendering) is covered by evals/browser/run_browser_check.py.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Callable

import json
import logging

from app.graph.citations import (
    Citation,
    chart_citations,
    chart_record_label,
    collapse_duplicate_documents,
    guideline_citations,
    translate_citations,
)
from app.graph.supervisor import synthesize_node
from app.observability import TurnTrace
from app.retrieval import display
from app.retrieval.chunking import _ends_mid_sentence, _starts_mid_sentence, load_corpus
from evals.truncation_tests import _FakeLLM, _NoTools, _sources, _state
from app.retrieval.types import RetrievedEvidence
from app.schemas import (
    AllergyFact,
    ConditionFact,
    GetPatientSnapshotOutput,
    GetUploadedDocumentsOutput,
    MedicationFact,
    UploadedDocumentFactSummary,
    UploadedDocumentSummary,
)
from app.tools import ToolCallRecord

PID = "11111111-1111-4111-8111-111111111111"


def _snapshot_record() -> ToolCallRecord:
    output = GetPatientSnapshotOutput(
        patient_id=PID, name="T P", birth_date=None, gender=None, chart_is_empty=False, duplicate_warnings=[],
        conditions=[
            ConditionFact(text="COPD", category="problem-list-item", clinical_status="active", source_resource="Condition/c1"),
            ConditionFact(text="COPD", category="problem-list-item", clinical_status="active", source_resource="Condition/c1"),
        ],
        medications=[MedicationFact(text="Lisinopril 10 mg", dosage_text=None, status="active", source_resource="MedicationRequest/m1")],
        allergies=[AllergyFact(text="Sulfa drugs", reaction="rash", is_coded=True, source_resource="AllergyIntolerance/a1")],
    )
    return ToolCallRecord(tool_name="get_patient_snapshot", patient_id=PID, output=output)


def test_chart_records_get_readable_labels_and_are_not_duplicated() -> None:
    cites = chart_citations(SimpleNamespace(turn_records=[_snapshot_record()]), PID)
    labels = {c.source_id: c.field_label for c in cites}
    assert labels == {
        "Condition/c1": "Condition: COPD",
        "MedicationRequest/m1": "Medication: Lisinopril 10 mg",
        "AllergyIntolerance/a1": "Allergy: Sulfa drugs",
    }, labels
    assert all(c.quote_or_value == c.source_id for c in cites), "quote_or_value keeps the record reference"


def test_uploaded_document_facts_never_become_chart_record_citations() -> None:
    docs = GetUploadedDocumentsOutput(
        patient_id=PID,
        documents=[UploadedDocumentSummary(
            doc_id="a.png", doc_type="lab_pdf", ocr_avg_confidence=90.0, uploaded_on="2026-09-25", upload_rank=0,
            facts=[UploadedDocumentFactSummary(
                field_name="results[0].value", value="7.8", confidence="high",
                source_resource="document:a.png#results[0].value", page_number=1,
            )],
        )],
    )
    records = [_snapshot_record(), ToolCallRecord(tool_name="get_uploaded_documents", patient_id=PID, output=docs)]
    cites = chart_citations(SimpleNamespace(turn_records=records), PID)
    assert not [c for c in cites if c.source_type == "fhir_resource" and c.source_id.startswith("document:")], cites
    assert [c.source_type for c in cites].count("uploaded_document") == 1


def test_chart_record_label_edge_cases() -> None:
    assert chart_record_label("AllergyIntolerance/x", None) is None
    assert chart_record_label("AllergyIntolerance/x", "   ") is None
    assert chart_record_label("Weird/x", "thing") == "Weird: thing"
    long = chart_record_label("Condition/x", "word " * 40)
    assert long is not None and len(long) <= 81 and long.endswith("…") and not long.endswith(" …"), long


def test_strip_markdown_removes_syntax_but_keeps_the_words() -> None:
    raw = (
        "## Heading\n\n**Bold** and _italic_ with `code` and a [link](http://x.y).\n\n"
        "- first item\n* second item\n1. numbered\n> quoted\n\n| a | b |\n---\n"
    )
    plain = display.strip_markdown(raw)
    for marker in ("#", "**", "_italic_", "`", "](", "http", "\n", "|", "---"):
        assert marker not in plain, (marker, plain)
    for word in ("Heading", "Bold", "italic", "code", "link", "first item", "second item", "numbered", "quoted"):
        assert word in plain, (word, plain)


def test_excerpt_drops_a_cut_off_first_word_and_cuts_only_at_word_boundaries() -> None:
    text = "cy regardless of the exact number. " + "alpha beta gamma delta " * 30
    out = display.excerpt(text, starts_mid_word=True, limit=60)
    assert out.startswith("…regardless of"), out
    assert out.endswith("…")
    body = out[1:-1]
    assert body.split()[-1] in {"alpha", "beta", "gamma", "delta"}, out  # never a half word
    assert display.excerpt("short text", limit=60) == "short text"
    exact = "x" * 10 + " " + "y" * 10
    assert display.excerpt(exact, limit=len(exact)) == exact
    assert display.excerpt("one two three", limit=7) == "one two…"
    assert display.excerpt("Untouched whole sentence.") == "Untouched whole sentence.", "no cut flags -> no ellipsis"


def test_excerpt_skips_heading_lines_and_trailing_fragments() -> None:
    text = "# Title\n\nFirst sentence here. Second sentence ends with a frag"
    assert display.excerpt(text, ends_mid_word=True) == "First sentence here. Second sentence ends with a…"


def test_clean_passage_unwraps_lines_keeps_bullets_and_bolds_headings() -> None:
    text = "## Section\n\nwrapped line one\nwrapped line two\n\n- item one\n- item two\nstill item two"
    out = display.clean_passage(text)
    assert out.startswith("**Section**"), out
    assert "wrapped line one wrapped line two" in out, out
    assert "- item one\n- item two still item two" in out, out


def test_corpus_chunks_carry_title_and_mid_word_flags() -> None:
    chunks = load_corpus()
    assert chunks
    titles = {c.source_id: c.doc_title for c in chunks}
    assert titles["hyperkalemia_management"] == "Hyperkalemia: general management approach", titles
    assert all(t and not t.startswith("#") for t in titles.values()), titles
    flagged = [c for c in chunks if c.starts_mid_word]
    assert flagged, "fixed-size chunking cuts words for this corpus; the flag must fire"
    for c in flagged:
        cleaned = display.clean_passage(c.text, starts_mid_word=True)
        assert not cleaned.startswith(c.text.split()[0]) or len(c.text.split()) == 1
    assert not chunks[0].starts_mid_word


def test_guideline_citations_carry_title_excerpt_and_untouched_raw_text() -> None:
    hk = [c for c in load_corpus() if c.source_id == "hyperkalemia_management"]
    cites = guideline_citations([RetrievedEvidence(chunk=hk[1], rerank_score=0.9)])
    c = cites[0]
    assert c.doc_label == "Hyperkalemia: general management approach"
    assert c.quote_or_value == hk[1].text, "raw chunk text must be unchanged (verification/grounding reads it)"
    assert c.excerpt and not re.search(r"#|\*\*", c.excerpt) and c.excerpt.startswith("…A single elevated value")
    assert c.display_text and "##" not in c.display_text


def test_adjacent_chunks_do_not_repeat_their_overlap_in_display_text() -> None:
    hk = [c for c in load_corpus() if c.source_id == "hyperkalemia_management"]
    cites = guideline_citations([RetrievedEvidence(chunk=c, rerank_score=0.9) for c in (hk[3], hk[1], hk[2])])  # rank order
    by_id = {c.field_or_chunk_id: c for c in cites}
    first, second = by_id["hyperkalemia_management:1"], by_id["hyperkalemia_management:2"]
    sentences = lambda t: [s.strip(" …") for s in re.split(r"(?<=[.!?])\s+", t.replace("\n", " ")) if len(s.strip(" …")) > 30]  # noqa: E731
    both = sentences(first.display_text) + sentences(second.display_text)
    assert len(both) == len(set(both)), "no sentence may appear in both adjacent passages"
    assert by_id["hyperkalemia_management:2"].quote_or_value == hk[2].text
    # A chunk with no adjacent partner is left as-is.
    alone = guideline_citations([RetrievedEvidence(chunk=hk[3], rerank_score=0.9)])[0]
    assert alone.display_text == display.clean_passage(
        hk[3].text, starts_mid_word=hk[3].starts_mid_word, ends_mid_word=hk[3].ends_mid_word,
        starts_mid_sentence=hk[3].starts_mid_sentence, ends_mid_sentence=hk[3].ends_mid_sentence,
    )


# --- sentence-boundary trimming -------------------------------------------------

_LEAD = "the tail of an earlier sentence. "
_S1 = "Sentence one is complete and long enough to be kept whole. "
_S2 = "Sentence two is also complete and stays. "
_CUT = "A third sentence that runs off the end of the chun"


def test_leading_partial_sentence_is_trimmed_and_marked() -> None:
    out = display.clean_passage(_LEAD + _S1 + _S2, starts_mid_sentence=True)
    assert out == "…" + (_S1 + _S2).strip(), out
    assert display.excerpt(_LEAD + _S1 + _S2, starts_mid_sentence=True).startswith("…Sentence one")


def test_trailing_partial_sentence_is_trimmed_and_marked() -> None:
    out = display.clean_passage(_S1 + _S2 + _CUT, ends_mid_word=True, ends_mid_sentence=True)
    assert out == (_S1 + _S2).strip() + " …", out  # set off after a full stop
    assert "third" not in out


def test_without_a_near_sentence_boundary_it_falls_back_to_a_word_boundary_with_ellipses() -> None:
    text = "half a sentence with no full stop anywhere near its ends and it keeps going on and on"
    out = display.clean_passage(text, starts_mid_word=True, starts_mid_sentence=True, ends_mid_word=True, ends_mid_sentence=True)
    assert out.startswith("…") and out.endswith("…"), out
    assert out[1:-1] == "a sentence with no full stop anywhere near its ends and it keeps going on and", out


def test_abbreviations_and_list_numbers_are_not_sentence_boundaries() -> None:
    text = "an agent (e.g. Albuterol) is used. Next sentence begins here and is long enough to keep around. 2. Shift potassium now."
    starts = display._sentence_starts(text)
    assert text.index("Next sentence") in starts and text.index("Albuterol") not in starts and text.index("Shift") not in starts, starts
    assert display._sentence_starts("range 5.1-5.9 mEq/L. Moderate: 6.0") == [len("range 5.1-5.9 mEq/L. ")]


def test_a_passage_that_starts_and_ends_cleanly_gets_no_ellipsis() -> None:
    out = display.clean_passage(_S1 + _S2)
    assert not out.startswith("…") and not out.endswith("…"), out


def test_chunk_sentence_flags_follow_the_source_text() -> None:
    text = "One sentence ends here. Next starts.\n\nNew paragraph starts here without a full stop before it\n\nAnother."
    assert not _starts_mid_sentence(text, 0)
    assert not _starts_mid_sentence(text, text.index("Next"))  # after a full stop
    assert not _starts_mid_sentence(text, text.index("New paragraph"))  # after a blank line
    assert _starts_mid_sentence(text, text.index("starts here without"))  # inside a sentence
    assert not _ends_mid_sentence(text, len(text))
    assert not _ends_mid_sentence(text, text.index(" Next"))  # right after "here."
    assert _ends_mid_sentence(text, text.index("without"))
    assert not _ends_mid_sentence(text, text.index("\n\nAnother"))  # ends at a paragraph break


# --- duplicate uploads collapse into one card -----------------------------------


def _doc_citations(doc_id: str, value: str = "7.8", label: str = "Lab report") -> list[Citation]:
    fields = [("Test name (result 1)", "Potassium"), ("Value (result 1)", value), ("Unit (result 1)", "mmol/L")]
    return [
        Citation(source_type="uploaded_document", source_id=doc_id, page_or_section="1", field_or_chunk_id=f"f{i}",
                 quote_or_value=v, doc_label=label, uploaded_on="2026-09-25", field_label=lab)
        for i, (lab, v) in enumerate(fields)
    ]


def test_identical_documents_collapse_into_the_first_with_a_copy_count() -> None:
    guide = Citation(source_type="guideline", source_id="g", page_or_section="s", field_or_chunk_id="g#0", quote_or_value="text")
    cites = _doc_citations("a.png") + _doc_citations("b.png") + [guide] + _doc_citations("c.png")
    out = collapse_duplicate_documents(cites)
    docs = [c for c in out if c.source_type == "uploaded_document"]
    assert {c.source_id for c in docs} == {"a.png"}, "the first-seen (most recent) copy represents the group"
    assert len(docs) == 3 and all(c.duplicate_count == 3 for c in docs)
    assert guide in out


def test_documents_that_differ_in_any_value_stay_separate() -> None:
    out = collapse_duplicate_documents(_doc_citations("a.png") + _doc_citations("b.png", value="4.1"))
    assert {c.source_id for c in out} == {"a.png", "b.png"}
    assert all(c.duplicate_count is None for c in out)
    other_type = collapse_duplicate_documents(_doc_citations("a.png") + _doc_citations("b.png", label="Intake form"))
    assert {c.source_id for c in other_type} == {"a.png", "b.png"}


def test_translate_citations_reports_how_many_documents_it_collapsed() -> None:
    from app.ingestion.schemas import DocumentCitation, ExtractedField, ExtractionResult, LabPdfExtraction, LabResultFact

    def f(v: str) -> ExtractedField:
        return ExtractedField(value=v, confidence="high", citation=DocumentCitation(page_number=1, quote_or_value=v))

    fact = LabResultFact(test_name=f("Potassium"), value=f("7.8"), unit=f("mmol/L"), reference_range=f("3.5-5.1"),
                         collection_date=f("2026-09-24"), abnormal_flag=f("high"))
    base = ExtractionResult(patient_id="p", doc_id="a.png", doc_type="lab_pdf", ocr_avg_confidence=90.0, uploaded_on="2026-09-25",
                            extraction=LabPdfExtraction(results=[fact]))
    counts: dict[str, int] = {}
    out = translate_citations(
        chart_facts=None, retrieved_evidence=[], patient_id="p", counts=counts,
        extracted_facts=[base, base.model_copy(update={"doc_id": "b.png"}), base.model_copy(update={"doc_id": "c.png"})],
    )
    assert counts["duplicate_documents_collapsed"] == 2 and counts["built"] == 18, counts
    assert {c["source_id"] for c in out} == {"a.png"} and all(c["duplicate_count"] == 3 for c in out)


def _two_result_lab(doc_id: str):
    """Potassium and sodium: DIFFERENT fields that share values (unit "mmol/L", collection date)."""
    from app.ingestion.schemas import DocumentCitation, ExtractedField, ExtractionResult, LabPdfExtraction, LabResultFact

    def f(v: str) -> ExtractedField:
        return ExtractedField(value=v, confidence="high", citation=DocumentCitation(page_number=1, quote_or_value=v))

    def result(name: str, value: str, rng: str) -> LabResultFact:
        return LabResultFact(test_name=f(name), value=f(value), unit=f("mmol/L"), reference_range=f(rng),
                             collection_date=f("2026-09-24"), abnormal_flag=f("normal"))

    return ExtractionResult(
        patient_id="p", doc_id=doc_id, doc_type="lab_pdf", ocr_avg_confidence=90.0, uploaded_on="2026-09-25",
        extraction=LabPdfExtraction(results=[result("Potassium", "4.1", "3.5-5.1"), result("Sodium", "139", "135-145")]),
    )


def test_different_fields_that_share_a_value_are_all_kept() -> None:
    out = translate_citations(chart_facts=None, retrieved_evidence=[], patient_id="p", extracted_facts=[_two_result_lab("a.png")])
    by_label = {c["field_label"]: c["quote_or_value"] for c in out}
    assert len(out) == 12, "every one of the 12 fields is cited, shared values included"
    assert by_label["Unit (result 1)"] == by_label["Unit (result 2)"] == "mmol/L", by_label
    assert by_label["Collection date (result 1)"] == by_label["Collection date (result 2)"] == "2026-09-24"
    assert by_label["Abnormal flag (result 1)"] == by_label["Abnormal flag (result 2)"] == "normal"


def test_duplicate_documents_merge_only_identical_field_value_pairs() -> None:
    docs = [_two_result_lab(d) for d in ("a.png", "b.png", "c.png")]
    out = translate_citations(chart_facts=None, retrieved_evidence=[], patient_id="p", extracted_facts=docs)
    assert {c["source_id"] for c in out} == {"a.png"}
    assert len(out) == 12 and {c["duplicate_count"] for c in out} == {3}, "3 copies merge; the 12 distinct fields all remain"
    units = sorted(c["field_label"] for c in out if c["field_label"].startswith("Unit"))
    assert units == ["Unit (result 1)", "Unit (result 2)"], units


def test_the_same_field_reached_by_both_paths_still_collapses_to_one_citation() -> None:
    from app.schemas import GetUploadedDocumentsOutput, UploadedDocumentFactSummary, UploadedDocumentSummary

    doc = _two_result_lab("a.png")
    facts = [
        UploadedDocumentFactSummary(field_name=f"results[{i}].{name}", value=value, confidence="high",
                                    source_resource=f"document:a.png#results[{i}].{name}", page_number=1)
        for i, res in enumerate(doc.extraction.results)
        for name, value in (("test_name", res.test_name.value), ("value", res.value.value), ("unit", res.unit.value),
                            ("reference_range", res.reference_range.value), ("collection_date", res.collection_date.value),
                            ("abnormal_flag", res.abnormal_flag.value))
    ]
    readback = ToolCallRecord(tool_name="get_uploaded_documents", patient_id="p", output=GetUploadedDocumentsOutput(
        patient_id="p", documents=[UploadedDocumentSummary(doc_id="a.png", doc_type="lab_pdf", ocr_avg_confidence=90.0,
                                                           uploaded_on="2026-09-25", upload_rank=0, facts=facts)]))
    out = translate_citations(chart_facts=SimpleNamespace(turn_records=[readback]), retrieved_evidence=[], patient_id="p",
                              extracted_facts=[doc])
    assert len(out) == 12, [c["field_label"] for c in out]  # same doc, same fields via two paths -> once each


# --- per-turn citation_counts log -------------------------------------------------


def _synthesize_and_capture(llm: _FakeLLM) -> dict:
    lines: list[str] = []
    logger = logging.getLogger("clinical_copilot")

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _H()
    logger.addHandler(handler)
    prior = logger.level
    logger.setLevel(logging.INFO)
    try:
        synthesize_node(_state(**_sources()), llm=llm, model="m", fhir=_NoTools(),
                        trace=TurnTrace(client=None, span=None, correlation_id="citation-counts-test"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior)
    events = [json.loads(m) for m in lines if '"event": "citation_counts"' in m]
    assert len(events) == 1, events
    return events[0]


def test_citation_counts_are_logged_per_turn_with_no_phi() -> None:
    event = _synthesize_and_capture(_FakeLLM("Potassium is 6.8 mmol/L (high).", "end_turn", ["text"], out_tokens=20))
    assert event["fallback_answer_used"] is False
    before, after = event["before_filter"], event["after_filter"]
    assert before["uploaded_document"] == 6 and 0 < after["uploaded_document"] < before["uploaded_document"], event
    assert before["guideline"] == after["guideline"] == 1
    assert event["built"] >= before["total"] and event["documents_before_filter"] == 1

    def leaves(o):
        return [x for v in o.values() for x in (leaves(v) if isinstance(v, dict) else [v])]

    strings = [v for v in leaves(event) if isinstance(v, str)]
    # numbers, booleans and the correlation id only: never a citation's text, id or value
    assert set(strings) == {"citation_counts", "citation-counts-test"}, strings


def test_citation_counts_flag_the_fallback_answer_that_makes_every_field_count_as_stated() -> None:
    event = _synthesize_and_capture(_FakeLLM("", "max_tokens", ["thinking"]))
    assert event["fallback_answer_used"] is True
    assert event["after_filter"]["uploaded_document"] == event["before_filter"]["uploaded_document"] == 6, event


_TESTS: list[Callable[[], None]] = [
    test_chart_records_get_readable_labels_and_are_not_duplicated,
    test_uploaded_document_facts_never_become_chart_record_citations,
    test_chart_record_label_edge_cases,
    test_strip_markdown_removes_syntax_but_keeps_the_words,
    test_excerpt_drops_a_cut_off_first_word_and_cuts_only_at_word_boundaries,
    test_excerpt_skips_heading_lines_and_trailing_fragments,
    test_clean_passage_unwraps_lines_keeps_bullets_and_bolds_headings,
    test_corpus_chunks_carry_title_and_mid_word_flags,
    test_guideline_citations_carry_title_excerpt_and_untouched_raw_text,
    test_adjacent_chunks_do_not_repeat_their_overlap_in_display_text,
    test_leading_partial_sentence_is_trimmed_and_marked,
    test_trailing_partial_sentence_is_trimmed_and_marked,
    test_without_a_near_sentence_boundary_it_falls_back_to_a_word_boundary_with_ellipses,
    test_abbreviations_and_list_numbers_are_not_sentence_boundaries,
    test_a_passage_that_starts_and_ends_cleanly_gets_no_ellipsis,
    test_chunk_sentence_flags_follow_the_source_text,
    test_identical_documents_collapse_into_the_first_with_a_copy_count,
    test_documents_that_differ_in_any_value_stay_separate,
    test_translate_citations_reports_how_many_documents_it_collapsed,
    test_different_fields_that_share_a_value_are_all_kept,
    test_duplicate_documents_merge_only_identical_field_value_pairs,
    test_the_same_field_reached_by_both_paths_still_collapses_to_one_citation,
    test_citation_counts_are_logged_per_turn_with_no_phi,
    test_citation_counts_flag_the_fallback_answer_that_makes_every_field_count_as_stated,
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
