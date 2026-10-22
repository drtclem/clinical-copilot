"""Verification layer: ARCHITECTURE.md Section 3, simplified per the Early
Submission scope note.

Two independent checks run on every drafted response, after the model
drafts it and before it reaches the resident:

1. Source attribution (3.1) -- simplified: a curated clinical-vocabulary
   scan for medication/allergy/condition-shaped terms in the draft, each
   checked by substring match against a "grounded vocabulary" built from this
   turn's actual tool outputs. A term that doesn't match anything the tools
   actually returned is stripped and logged as a verification failure. This
   is deliberately NOT full per-claim source tagging (out of scope for this
   pass, see early-submission-build-prompt.md) -- it will miss a clinical
   claim phrased in words outside CLINICAL_VOCAB, and it can't tell a
   positive assertion ("patient is on X") from a negation ("no record of
   X"). Both are documented, honest gaps, not assumed away.

2. Domain constraint enforcement (3.2) -- a hard, code-level check: for
   every medication-shaped term in the draft, `check_allergy_conflict` is
   re-run server-side regardless of whether the model already called it.
   If a conflict exists and isn't clearly stated, the verification layer
   forces the warning into the response rather than trusting the model
   noticed its own tool result.

Also enforces three ARCHITECTURE.md Section 4 failure modes the same way,
since a wrong answer here is exactly the kind of silent-degradation risk
Section 4 treats as a verification failure, not an acceptable fallback:
duplicate patient records, an empty chart presented as "nothing to report,"
and (added 2026-09-20, closing a gap found investigating get_recent_
encounters' sensitivity-lookup 401 -- evals/COVERAGE.md's corrected entry)
a tool's own partial_failures going unmentioned -- previously the only one
of the three with no code-level backstop, relying entirely on the model
choosing to surface it from the raw tool JSON under SYSTEM_PROMPT's
generic "say so plainly" instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.fhir_client import FhirClient
from app.schemas import (
    CheckAllergyConflictOutput,
    CompareSignoutToChartOutput,
    DuplicatePatientWarning,
    GetPatientSnapshotOutput,
    GetRecentEncountersOutput,
    GetRecentObservationsOutput,
    GetUploadedDocumentsOutput,
    SummarizeShiftEventsOutput,
    UploadedDocumentFactSummary,
)
from app.clinical_reference import drug_names_in
from app.tools import ToolCallRecord, check_allergy_conflict  # noqa: F401 -- ToolCallRecord re-exported,
# see its docstring in app/tools.py for why it lives there now, not here

# Curated clinical-term vocabulary used ONLY to decide "this looks like a
# claim worth checking" -- not to prove correctness. Deliberately modest;
# expanding this is the cheapest lever for improving recall later.
_MEDICATION_TERMS = [
    "metformin", "lisinopril", "warfarin", "aspirin", "ibuprofen", "amoxicillin",
    "penicillin", "atorvastatin", "simvastatin", "metoprolol", "amlodipine",
    "losartan", "omeprazole", "albuterol", "insulin", "heparin", "vancomycin",
    "ciprofloxacin", "azithromycin", "prednisone", "furosemide", "hydrochlorothiazide",
    "clopidogrel", "gabapentin", "sertraline", "fluoxetine", "levothyroxine",
]
_ALLERGY_TERMS = [
    "penicillin", "sulfa", "latex", "peanut", "shellfish", "iodine", "aspirin",
    "nsaid", "codeine", "morphine", "eggs", "soy",
]
_CONDITION_TERMS = [
    "diabetes", "hypertension", "hypotension", "pneumonia", "sepsis", "asthma",
    "copd", "ckd", "cirrhosis", "afib", "atrial fibrillation", "stroke", "cancer",
]

_ALL_TERMS = sorted(set(_MEDICATION_TERMS + _ALLERGY_TERMS + _CONDITION_TERMS), key=len, reverse=True)

# Fixed 2026-09-18, fourth of four bugs from the same audit as
# _mentions_conflict_near_medication/_mentions_duplicate_warning/
# _mentions_empty_chart above: the original single-pattern regex assumed
# the capitalized word immediately BEFORE a dose is always the drug name
# -- found live, "Started 10 mg Lisinopril daily" captured "Started", not
# "Lisinopril", stripping the whole (correct) sentence. Structural fix,
# not a bigger keyword list: checks BOTH sides of the dose pattern in one
# match (an optional preceding group and an optional following group),
# and prefers a real drug name found immediately AFTER the dose+unit (the
# "verb dose unit DRUG" clinical phrasing) over the preceding word when
# one exists. The following group excludes all-caps route/frequency
# abbreviations (IV, PO, BID) via [A-Z][a-z]+ (mixed case only).
_DOSE_RE = re.compile(
    r"\b(?:([A-Z][a-zA-Z]+)\s+)?\d+(?:\.\d+)?\s*(?:mg|mcg|g|units?)\b(?:\s+(?:of\s+)?([A-Z][a-z]+))?"
)

# When no following candidate exists, the preceding word is excluded if it
# has a common English past-tense/participle suffix ("Continued",
# "Increased", "Administered") -- essentially never a suffix of a real
# drug name. A morphological (shape-based) exclusion, not an enumerated
# word list.
_DOSE_VERB_SUFFIX_RE = re.compile(r"(ed|ing)$", re.IGNORECASE)

# The suffix rule above doesn't catch irregular English participles
# ("Given") or non-drug nouns that happen to precede a number in clinical
# shorthand ("Day 3", as in "On Day 3 units of blood were transfused").
# Honestly disclosed, not papered over: no general morphological rule
# distinguishes these from a genuinely short real drug name (Advil,
# Xanax, Norco, Ambien are all 5-6 letters) without actual POS tagging,
# which is out of scope for a regex fix. This is a small,
# grammatically-motivated supplement (administration verbs and clinical
# sequence nouns) needed only because pure morphology can't cover
# irregular verbs -- not the same kind of fix as the fabrication-phrase
# keyword lists corrected elsewhere in this file; it exists to patch the
# one gap the structural rule above can't close on its own.
_DOSE_NON_DRUG_WORDS = {"given", "day", "week", "month", "year", "visit", "encounter", "dose", "admission"}


def _dose_candidates(text: str) -> list[str]:
    candidates = []
    for m in _DOSE_RE.finditer(text):
        preceding, following = m.group(1), m.group(2)
        if following:
            candidates.append(following)
        elif (
            preceding
            and not _DOSE_VERB_SUFFIX_RE.search(preceding)
            and preceding.lower() not in _DOSE_NON_DRUG_WORDS
        ):
            candidates.append(preceding)
    return candidates

# Structural (shape-based) candidate detection, not enumeration -- the same
# principle _DOSE_RE already applies to medication doses, extended to
# lab/vital values. Added 2026-09-18, investigating compare_signout_to_chart:
# this is a real, if partial, fix for the recurring "_ALL_TERMS has no
# vocabulary for tool X's facts" gap (get_recent_observations, then this
# tool) -- a fabricated lab/vital value now becomes a candidate needing
# grounding WITHOUT "potassium" or any lab name ever needing to be added to
# a fixed list, because the match is on the number+unit shape, not a name.
# Deliberately does NOT close the other half of that recurring gap (a bare
# NAME-shaped claim, e.g. a fabricated condition or encounter type, has no
# numeric shape to match on) -- no cheap structural fix exists for that
# half; see COVERAGE.md's note on why expanding _ALL_TERMS incrementally
# remains the accepted approach there, not a structural fix.
#
# The gap between the number and the unit was originally plain whitespace
# (`\s*`) only. Found live 2026-09-23: a model presenting uploaded-document
# lab facts as a markdown table (`| 4.4 | mEq/L |`) put a `|` cell
# separator between the value and unit -- `\s*` doesn't match `|`, so the
# whole value+unit pair silently produced ZERO candidates and skipped
# verification entirely, rather than being checked and passing. That
# looked like "grounding worked" (no flagged claims) but was actually the
# check never firing -- a false negative on the check itself, not evidence
# of a working check. Widened to `[\s|]*` (whitespace and/or pipe, matching
# the same one-cell-boundary shape a real markdown table produces) so a
# table-formatted value+unit pair is extracted as a candidate exactly like
# a prose one is, and still goes through real grounding instead of
# bypassing it.
# Shared with _NUMERIC_RANGE_RE below -- one unit vocabulary, not two
# independently-maintained copies.
_LAB_UNIT_PATTERN = r"(?:mEq/L|mg/dL|mmHg|mmol/L|mcg/mL|ng/mL|g/dL|mIU/L|U/L|bpm|°C|°F|/min)"

_LAB_VALUE_RE = re.compile(
    rf"\b\d+(\.\d+)?[\s|]*({_LAB_UNIT_PATTERN})\b",
    re.IGNORECASE,
)

# Matches a "N1-N2 UNIT" shaped range (e.g. a corpus severity threshold like
# "5.1-5.9 mEq/L") -- see _range_endpoint_values' own docstring for why
# each endpoint needs to be separately groundable.
_NUMERIC_RANGE_RE = re.compile(
    rf"\b(\d+(?:\.\d+)?)\s*[-‐-―]\s*(\d+(?:\.\d+)?)\s*({_LAB_UNIT_PATTERN})\b",
    re.IGNORECASE,
)


def _range_endpoint_values(text: str) -> list[str]:
    """A grounded text blob's own "N1-N2 UNIT" range (e.g. a guideline's
    "5.1-5.9 mEq/L" severity threshold) attaches the unit once, after the
    upper bound only -- so a model correctly restating just the lower
    bound with its own unit ("5.1 mEq/L") never literally appears as a
    substring of the source text, even though the number itself is
    genuinely present. Found live: exactly this pattern flagged a
    genuinely-grounded value as unverified (graph_follow_up_turn_keeps_
    uploaded_document_fact_grounded, turn 2 -- confirmed by checking the
    real retrieved chunk directly: it contains "5.1" but never "5.1
    mEq/L" as a substring). Returns each endpoint restated with the unit
    attached directly, in the exact "N UNIT" shape _LAB_VALUE_RE itself
    extracts as a candidate, so a correct restatement of either endpoint
    grounds via the ordinary substring check -- a value strictly INSIDE
    the range (e.g. "5.5 mEq/L", never itself stated by the source) is
    deliberately not synthesized here and still fails grounding."""
    return [
        f"{bound} {unit}".lower()
        for low, high, unit in _NUMERIC_RANGE_RE.findall(text)
        for bound in (low, high)
    ]

# Same separator class _LAB_VALUE_RE now tolerates between a value and its
# unit -- reused here to split a composed candidate (e.g. "4.4 | mEq/L" as
# extracted from a table cell) back into its atomic pieces without a bare
# "|" surviving as its own, ungroundable token.
_COMPOSITION_SPLIT_RE = re.compile(r"[\s|]+")

# Excludes a lab/vital value from candidacy when it's the upper bound of a
# stated "X-Y unit" range (e.g. "normal range (~3.5-5.0 mEq/L)") -- found
# live, the same day _LAB_VALUE_RE itself was added: a model correctly
# citing a normal reference range alongside the real patient value got its
# OWN reference-range mention stripped as an "unverified claim", since a
# range's upper bound is exactly as number+unit-shaped as a genuine
# patient-specific value and nothing distinguished them. A reference range
# is general medical knowledge, not a claim about this patient, and
# shouldn't be held to the same "must come from a tool" bar. Unlike a
# fabricated medication name, a lab value's harm surface leans toward
# false positives (stripping real, useful clinical context) rather than
# false negatives (missing a fabricated value) being the primary risk --
# this exclusion accepts a small amount of the latter to avoid the former.
_LAB_VALUE_RANGE_PREFIX_RE = re.compile(r"\d+(\.\d+)?\s*[-‐-―]\s*$")

# Common clinical abbreviations that won't literally substring-match the
# expanded form a tool returns (e.g. a model saying "COPD" against a
# Condition.text of "Chronic obstructive pulmonary disease"). Caught this via
# eval case pid2_normal_snapshot flagging a true, grounded fact as
# unverified -- a false positive is exactly as bad as a missed hallucination
# for this metric, so this is a correctness fix, not just documented as a gap.
_CLINICAL_ALIASES = {
    "copd": "chronic obstructive pulmonary disease",
    "htn": "hypertension",
    "afib": "atrial fibrillation",
    "atrial fibrillation": "afib",
    "ckd": "chronic kidney disease",
    "dm": "diabetes",
    "t2dm": "diabetes",
    "mi": "myocardial infarction",
    "chf": "heart failure",
}


_LAB_VALUE_FORMATTING_RE = re.compile(r"[\s°]")


def _normalize_lab_value(s: str) -> str:
    """Strips whitespace and the degree symbol so a lab/vital value
    compares equal regardless of cosmetic formatting differences between
    how a tool stored it (app/tools.py's _observation_value_text formats
    as "<value> <unit>", e.g. "38.9 C") and how the model naturally
    writes it in prose (e.g. "38.9°C", no space, real degree symbol).
    Found live: _LAB_VALUE_RE's new candidate detection caught a real,
    tool-sourced temperature that then failed grounding on this exact
    formatting mismatch, stripping a true fact from the response."""
    return _LAB_VALUE_FORMATTING_RE.sub("", s)


def _is_grounded_by_composition(term: str, grounded: set[str]) -> bool:
    """A multi-token candidate (e.g. "4.4 mEq/L") is grounded if EVERY one
    of its atomic pieces independently matches something in the grounded
    vocabulary, even though no single grounded entry contains the full
    combined string. Root-caused live 2026-09-23: get_uploaded_documents'
    grounding branch (_grounded_vocabulary below) adds a lab fact's value
    and unit as separate atomic strings ("4.4", "mEq/L") -- app/tools.py's
    _flatten_extraction_facts splits them that way on purpose, one
    UploadedDocumentFactSummary per field -- unlike _ground_observations'
    obs.text, which is already a single descriptive string containing
    both, so the chart-observation path never had this gap. None of the
    exact/substring/normalized checks above can bridge "candidate is the
    concatenation of two separately-grounded pieces" -- this is a
    structural fix for that composition gap, not a bigger keyword list or
    another single-value normalization, in the same spirit as
    _normalize_lab_value tolerating cosmetic formatting differences,
    applied to the case where the pieces themselves are grounded but were
    never grounded together.

    Requires at least 2 atomic pieces and an EXACT (normalized) match per
    piece, not substring containment -- a single-token term falls through
    to the checks above unchanged, and substring matching on short numeric
    tokens (e.g. "4" against a grounded "34") would reopen exactly the
    false-positive risk this file's other exclusions (e.g.
    _LAB_VALUE_RANGE_PREFIX_RE) are written to avoid.
    """
    parts = [p for p in _COMPOSITION_SPLIT_RE.split(term) if p]
    if len(parts) < 2:
        return False
    for part in parts:
        part_norm = _normalize_lab_value(part)
        if not any(part == g or part_norm == _normalize_lab_value(g) for g in grounded):
            return False
    return True


def _is_grounded(term: str, grounded: set[str]) -> bool:
    t = term.lower()
    if t in grounded or any(t in g for g in grounded):
        return True
    alias = _CLINICAL_ALIASES.get(t)
    if alias and (alias in grounded or any(alias in g or g in alias for g in grounded)):
        return True
    t_norm = _normalize_lab_value(t)
    if t_norm != t and any(t_norm == _normalize_lab_value(g) or t_norm in _normalize_lab_value(g) for g in grounded):
        return True
    return _is_grounded_by_composition(t, grounded)

# Fixed 2026-09-18, found live via audit: bare "no problems"/"no
# medications" collided with unrelated uses ("no problems accessing this
# data"), silently suppressing the empty-chart safety caveat below --
# _enforce_duplicate_and_empty_chart is production enforcement, not an
# eval check, so a false match here means the hard "never present empty
# as reassuring" guarantee silently doesn't fire. Fixed two ways: a few
# highly specific standalone phrases stay as bare substrings (low
# collision risk), and the generic "no X" shapes are replaced with a
# structural pattern requiring "no"/"none" to actually be followed by
# "recorded"/"documented"/"on file"/"noted" within a short window --
# catches real phrasings ("no conditions, medications, or allergies are
# recorded", "Conditions: none recorded") while excluding "no problems
# accessing this data", which mentions none of those words at all.
_EMPTY_CHART_CUES = ["empty chart", "chart is empty", "essentially empty", "genuinely empty",
                      "no active problems"]
_EMPTY_CHART_RECORDED_RE = re.compile(
    r"\b(?:no|none)\b.{0,60}?\b(?:recorded|documented|on file|noted)\b",
    re.IGNORECASE | re.DOTALL,
)


def _mentions_empty_chart(response: str) -> bool:
    lowered = response.lower()
    if any(cue in lowered for cue in _EMPTY_CHART_CUES):
        return True
    return bool(_EMPTY_CHART_RECORDED_RE.search(response))


# Fixed 2026-09-18, same audit, same risk shape and same severity: bare
# "two records"/"another record"/"multiple records" collided with
# unrelated mentions ("multiple records of prior vaccinations"), silently
# suppressing the duplicate-patient-record warning -- the exact "never
# silently drop" guarantee c4_7_explicit_suppress_request's own
# guards_against text describes. Narrowed to phrases that tie the
# duplicate/matching language to the patient/chart/record context
# specifically, rather than any generic "records" mention; the literal
# other_patient_id is also checked directly (see _mentions_duplicate_
# warning below) since a UUID has effectively zero collision risk.
_DUPLICATE_CUES = [
    "duplicate patient", "duplicate record", "duplicate chart", "duplicate-record",
    "possible duplicate", "duplicate warning", "another patient record",
    "two patient records", "matching patient record", "matches on name",
    "matched on name", "matching on name", "same name and birthdate",
    "same name + birthdate", "flagged a duplicate", "flagged another patient",
]


def _mentions_duplicate_warning(response: str, warnings: list[DuplicatePatientWarning]) -> bool:
    lowered = response.lower()
    if any(w.other_patient_id.lower() in lowered for w in warnings):
        return True
    return any(cue in lowered for cue in _DUPLICATE_CUES)


# Added 2026-09-20, closing a gap found investigating get_recent_encounters'
# sensitivity-lookup 401 (evals/COVERAGE.md's corrected entry): every tool
# output type carrying a partial_failures field (app/schemas.py) --
# GetPatientSnapshotOutput, GetRecentEncountersOutput,
# GetRecentObservationsOutput, CompareSignoutToChartOutput -- shares the
# exact same list[str] shape, so one enforcement point covers all four
# rather than per-type logic the way _grounded_vocabulary needs (each of
# those needs different text pulled into the grounded vocabulary; a
# partial-failure disclosure needs none of that, just the failure strings
# themselves).
_PARTIAL_FAILURE_TYPES = (
    GetPatientSnapshotOutput,
    GetRecentEncountersOutput,
    GetRecentObservationsOutput,
    CompareSignoutToChartOutput,
    # W2 Checklist item 8 (2026-09-22): added alongside the 7th tool
    # (get_uploaded_documents) after finding live that a document
    # genuinely on file whose read-back failed was otherwise
    # indistinguishable from "no documents at all" -- see
    # app/ingestion/storage.py's list_extracted_documents docstring.
    GetUploadedDocumentsOutput,
)

# Same false-positive-avoidance philosophy as _EMPTY_CHART_CUES/
# _DUPLICATE_CUES above: reasonably specific to a failed fetch being
# disclosed, not so generic ("failed", "couldn't") that an unrelated
# clinical hedge ("I couldn't confirm the exact time of onset") would
# falsely suppress the warning. Expected to need the same kind of live
# narrowing those two needed if a real collision turns up.
_PARTIAL_FAILURE_CUES = [
    "couldn't retrieve", "could not retrieve", "couldn't confirm", "could not confirm",
    "couldn't fully retrieve", "wasn't able to retrieve", "unable to retrieve",
    "failed to retrieve", "partial failure", "partially failed", "couldn't complete",
    "could not be retrieved", "could not be confirmed",
]


def _mentions_partial_failure(response: str, failures: list[str]) -> bool:
    lowered = response.lower()
    if any(cue in lowered for cue in _PARTIAL_FAILURE_CUES):
        return True
    # A failure string's own subsystem name (the part before ": ", e.g.
    # "sensitivity lookup" from "sensitivity lookup: http_error") is a
    # reasonably specific literal cue too -- same idea as checking
    # other_patient_id directly above, applied to this field's own shape.
    return any(f.split(":")[0].strip().lower() in lowered for f in failures)


@dataclass
class VerificationOutcome:
    passed_source_attribution: bool
    passed_domain_constraint: bool
    final_response: str
    flagged_claims: list[str] = field(default_factory=list)
    enforced_warnings: list[str] = field(default_factory=list)


def _ground_snapshot(vocab: set[str], snap: GetPatientSnapshotOutput) -> None:
    vocab.update(part.lower() for part in snap.name.split())
    for c in snap.conditions:
        vocab.add(c.text.lower())
    for m in snap.medications:
        vocab.add(m.text.lower())
        vocab.add(m.text.split()[0].lower())  # bare drug name, e.g. "metformin"
    for a in snap.allergies:
        vocab.add(a.text.lower())
        if a.reaction:
            vocab.add(a.reaction.lower())


def _ground_encounters(vocab: set[str], enc_output: GetRecentEncountersOutput) -> None:
    # Phase 6: an encounter's reason/type text can legitimately overlap with
    # _ALL_TERMS' condition vocabulary (e.g. a visit reason of "diabetes
    # follow-up") -- ground it the same way snapshot conditions are, so a
    # real, sourced fact from this tool isn't falsely flagged as unverified.
    for enc in enc_output.encounters:
        vocab.add(enc.text.lower())


def _ground_observations(vocab: set[str], obs_output: GetRecentObservationsOutput) -> None:
    # UC2: grounds an observation's measured-thing text (e.g. "potassium")
    # and its formatted value (e.g. "5.2 mEq/L") so a real, sourced fact
    # from this tool isn't falsely flagged. Also reachable now via
    # _LAB_VALUE_RE's structural candidate detection (added alongside
    # compare_signout_to_chart), which doesn't need the measured-thing name
    # in _ALL_TERMS at all -- but the bare name (e.g. "potassium") is still
    # only a candidate if it happens to already be in _ALL_TERMS, which it
    # generally isn't; that half of the gap is unchanged, see this module's
    # docstring.
    for obs in obs_output.observations:
        vocab.add(obs.text.lower())
        if obs.value:
            vocab.add(obs.value.lower())


def _grounded_vocabulary(records: list[ToolCallRecord]) -> set[str]:
    vocab: set[str] = set()
    for rec in records:
        if isinstance(rec.output, GetPatientSnapshotOutput):
            _ground_snapshot(vocab, rec.output)
        elif isinstance(rec.output, CheckAllergyConflictOutput):
            conf = rec.output
            vocab.add(conf.medication_name.lower())
            if conf.matched_allergy_text:
                vocab.add(conf.matched_allergy_text.lower())
        elif isinstance(rec.output, GetRecentEncountersOutput):
            _ground_encounters(vocab, rec.output)
        elif isinstance(rec.output, GetRecentObservationsOutput):
            _ground_observations(vocab, rec.output)
        elif isinstance(rec.output, SummarizeShiftEventsOutput):
            # UC4: mostly redundant with the branches above, since the
            # underlying GetPatientSnapshotOutput/GetRecentEncountersOutput/
            # GetRecentObservationsOutput records this tool read from are
            # still in turn_records themselves and already grounded their
            # own text. Grounded anyway, defense in depth, and because the
            # duplicate-record event text is newly synthesized here (not a
            # verbatim field on any underlying Fact) -- without this branch
            # that specific phrasing wouldn't be grounded by anything else.
            for event in rec.output.events:
                vocab.add(event.text.lower())
        elif isinstance(rec.output, CompareSignoutToChartOutput):
            # UC2: grounds the fresh data this tool fetched internally,
            # which -- unlike every other tool -- never becomes its own
            # separate ToolCallRecord (get_patient_snapshot/get_recent_
            # encounters/get_recent_observations run *inside*
            # compare_signout_to_chart's own function body, invisible to
            # run_turn's dispatch loop). Without this branch, anything the
            # model restates from the fresh fetch beyond the diff itself
            # (e.g. "current potassium is 5.8") would be ungroundable.
            # Reuses the exact same per-type grounding logic as the real
            # top-level tools, applied to the nested current_* fields.
            cmp = rec.output
            for discrepancy in cmp.discrepancies:
                vocab.add(discrepancy.text.lower())
            if cmp.current_snapshot:
                _ground_snapshot(vocab, cmp.current_snapshot)
            if cmp.current_encounters:
                _ground_encounters(vocab, cmp.current_encounters)
            if cmp.current_observations:
                _ground_observations(vocab, cmp.current_observations)
        elif isinstance(rec.output, GetUploadedDocumentsOutput):
            # W2 PRD Section 4.2: grounds get_uploaded_documents' own
            # facts so a response that correctly cites them isn't falsely
            # flagged as unverified -- separate from, and in addition to,
            # _enforce_uploaded_document_disclosure below (which handles
            # the "unreconciled" framing hard rule this data ALSO needs;
            # grounding and the disclosure requirement are independent
            # checks, not substitutes for each other).
            for doc in rec.output.documents:
                for fact in doc.facts:
                    vocab.add(fact.value.lower())
    return vocab


def _find_candidate_terms(text: str) -> list[str]:
    lowered = text.lower()
    found = [term for term in _ALL_TERMS if term in lowered]
    found += _dose_candidates(text)
    found += [
        m.group(0)
        for m in _LAB_VALUE_RE.finditer(text)
        if not _LAB_VALUE_RANGE_PREFIX_RE.search(text[: m.start()])
    ]
    # de-dupe, case-insensitive
    seen: set[str] = set()
    unique = []
    for term in found:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def medication_candidates_in(text: str) -> list[str]:
    """Public wrapper for other modules (app/reference_layer.py) that need
    "does this text mention a medication-shaped term" without reaching
    into this module's private _find_candidate_terms/_MEDICATION_TERMS --
    the same filter _check_domain_constraint already applies internally,
    exposed intentionally rather than imported across the underscore
    boundary."""
    return [t for t in _find_candidate_terms(text) if t.lower() in _MEDICATION_TERMS]


_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
_HEADING_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\*\*[^*]+\*\*:?|__[^_]+__:?)\s*$")
_SENTENCE_GAP_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*(\[\"'])")
_NO_SPLIT_AFTER = ("e.g.", "i.e.", "vs.", "approx.", "dr.", "mr.", "mrs.", "ms.", "cf.", "fig.", "no.")


def _split_sentences(text: str) -> list[str]:
    """Sentences within ONE line: split after . ! ? only when a new sentence plainly starts (capital, digit,
    bullet mark or quote) and never after an abbreviation such as "e.g." -- so "(e.g. albuterol)" stays whole."""
    parts: list[str] = []
    start = 0
    for m in _SENTENCE_GAP_RE.finditer(text):
        before = text[start:m.start()]
        words = before.split()
        if words and words[-1].lower() in _NO_SPLIT_AFTER:
            continue
        parts.append(before)
        start = m.end()
    parts.append(text[start:])
    return parts


def _is_lead_in(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not _LIST_ITEM_RE.match(line) and (stripped.endswith(":") or bool(_HEADING_LINE_RE.match(line)))


def _remove_unverified_sentences(draft: str, unverified: list[str]) -> str:
    """Remove ONLY the sentences/bullets that carry an unverified term, and leave everything else exactly as the
    model wrote it. Line structure is preserved -- lines are never glued together -- and a lead-in ("Screen for:",
    a bold heading) whose entire body was removed is dropped with it. Which text counts as a claim to remove is
    unchanged (any sentence containing an unverified term); only HOW it is removed changed, after the old
    whole-text split-and-`" ".join` deleted a bulleted block with its heading and flattened every newline."""
    terms = [t.lower() for t in unverified]

    def carries_claim(sentence: str) -> bool:
        lowered = sentence.lower()
        return any(t in lowered for t in terms)

    lines = draft.split("\n")
    edited: list[str | None] = []
    for line in lines:
        if not line.strip():
            edited.append(line)
            continue
        marker = _LIST_ITEM_RE.match(line)
        prefix = line[: marker.end()] if marker else ""
        sentences = _split_sentences(line[len(prefix):])
        kept = [s for s in sentences if not carries_claim(s)]
        if len(kept) == len(sentences):
            edited.append(line)  # untouched line: byte-for-byte as written
        elif not kept:
            edited.append(None)
        else:
            edited.append(prefix + " ".join(kept))

    # Drop a lead-in whose whole body was removed (bottom-up so nested lead-ins cascade).
    for i in range(len(lines) - 1, -1, -1):
        if edited[i] is None or not _is_lead_in(lines[i]):
            continue
        is_heading = bool(_HEADING_LINE_RE.match(lines[i]))
        body: list[int] = []
        j = i + 1
        while j < len(lines):
            if _is_lead_in(lines[j]) and (is_heading or not _LIST_ITEM_RE.match(lines[j])):
                break
            if not is_heading and lines[j].strip() and not _LIST_ITEM_RE.match(lines[j]):
                break  # a colon lead-in owns only the list directly under it
            body.append(j)
            j += 1
        content = [k for k in body if lines[k].strip()]
        if content and all(edited[k] is None for k in content):
            edited[i] = None

    out = [line for line in edited if line is not None]
    collapsed: list[str] = []
    for line in out:
        if not line.strip() and (not collapsed or not collapsed[-1].strip()):
            continue
        collapsed.append(line)
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    return "\n".join(collapsed).strip()


def _check_source_attribution(
    draft_response: str, grounded: set[str]
) -> tuple[str, list[str]]:
    candidates = _find_candidate_terms(draft_response)
    unverified = [term for term in candidates if not _is_grounded(term, grounded)]
    if not unverified:
        return draft_response, []

    final = _remove_unverified_sentences(draft_response, unverified)
    final += (
        f"\n\n[Verification note: I removed {len(unverified)} detail(s) I could not "
        f"confirm against this patient's chart: {', '.join(unverified)}.]"
    )
    return final.lstrip("\n"), unverified


# Fixed 2026-09-18, found live via audit, the most severe of the three:
# bare "conflict" collided with unrelated uses ("a scheduling conflict",
# "the two records conflict on her DOB") -- ARCHITECTURE.md 3.2 calls this
# mechanism "a wall, not a request", and a false match here silently skips
# appending the HARD STOP override, no signal to the resident at all.
# "conflict" alone is dropped; the remaining safe cues ("allerg",
# "contraindicat", "do not give", or the specific matched allergy text)
# are checked in a window around each mention of the medication itself,
# not the whole response -- so an unrelated "conflict" elsewhere (e.g.
# about duplicate-record data) can't satisfy this, and a multi-medication
# response can't have one drug's allergy mention satisfy a different
# drug's check.
_CONFLICT_SAFE_CUES = ["allerg", "contraindicat", "do not give"]


def _descriptive_allergy_cue(matched_allergy_text: str | None, medication: str, min_length: int = 3) -> str | None:
    """The part of a matched allergy entry that's evidence the draft already
    acknowledges *this* conflict -- a reaction description ("(hives)"), not
    the bare allergen/drug name. For a direct-match conflict (the proposed
    medication IS the documented allergen, e.g. penicillin allergy +
    penicillin proposed), matched_allergy_text often reduces to nothing
    more than the medication's own name -- using it as a safe cue then
    makes the check vacuous, since the medication's own mention in the
    draft trivially "contains" itself. Only text left over after removing
    the medication name counts; for a cross-reactive match (allergen and
    medication are different drugs, e.g. penicillin allergy + amoxicillin
    proposed) the allergen name doesn't overlap with the medication at
    all, so it passes through unchanged and keeps working as before."""
    if not matched_allergy_text:
        return None
    remainder = matched_allergy_text.lower().replace(medication.lower(), "").strip(" ()-:,.")
    return remainder if len(remainder) >= min_length else None


# ---- A drug named only to say "do not use it" (2026-09-26) --------------------------------------------------
#
# Found in the final golden-set run: an answer to "what is the cross-reactivity consideration for related
# classes given her penicillin allergy" listed the penicillin class ("Within penicillins (penicillin,
# amoxicillin, ampicillin, ...): high cross-reactivity is assumed across the whole class -- this is a hard-stop
# in this system") and never recommended any of them. The window check above found no allergy cue within 200
# characters of those names, so the enforcer appended six HARD STOPs and failed the turn. A mention that itself
# says "avoid" is not a recommendation.
#
# This is an ADDITIONAL way to accept, deliberately narrow and fail-closed: a drug counts as acknowledged only if
# EVERY sentence that names it carries an unambiguous avoidance marker and NONE of that sentence's words can turn
# it into a recommendation (contrast, "no need to", "safe", "consider", ...). Sentence scope, not a window: a
# marker in a neighbouring sentence proves nothing about this one. Anything unclear is not accepted, so the HARD
# STOP is still appended -- the cost of a false positive is one extra note; of a false negative, a patient.

# Avoidance stated outright. "contraindicated" / "hard-stop" only count un-negated (see _NEGATED_MARKER).
_STRONG_AVOIDANCE = re.compile(r"\bavoid|\bcontraindicated\b|\bhard[- ]stop\b|\bnot recommended\b")
# "do not give", "never use", "should not be prescribed", "avoid giving": a negator directly on an action verb.
_NEGATED_ACTION = re.compile(
    r"\b(?:do not|don['\u2019]t|should not|shouldn['\u2019]t|must not|mustn['\u2019]t|never|not)\s+(?:be\s+)?"
    r"(?:give[n]?|us(?:e|ed)|prescribe[d]?|start(?:ed)?|order(?:ed)?|administer(?:ed)?|choose|chosen|"
    r"switch(?:ed)?(?:\s+to)?|substitute[d]?)\b"
    r"|\bavoid(?:ing)?\s+(?:giving|using|prescribing|starting|ordering|administering)\b"
)
# The marker itself negated: "not contraindicated", "no hard-stop", "do not avoid", "no need to avoid".
_NEGATED_MARKER = re.compile(
    r"\b(?:not|no|isn['\u2019]t|aren['\u2019]t|non-?)\s+(?:\w+\s+)?(?:contraindicat\w*|hard[- ]stop)"
    r"|\b(?:not|never|no|\w+n['\u2019]t)\s+(?:\w+\s+){0,2}avoid"
)
# Words that make a sentence a contrast, a permission or a recommendation. If any is present the sentence is
# ambiguous at best, so it is not an avoidance sentence.
_NOT_AN_AVOIDANCE = re.compile(
    r"\b(?:but|however|instead|except|unless|rather|although|though|whereas|yet|no reason|no need|need|needn['\u2019]t|"
    r"have to|not necessary|not required|unnecessary|not needed|unneeded|no longer|hesitate|withhold|"
    r"safe|safely|fine|reasonable|acceptable|ok|okay|consider|alternative|alternatives|switch|start|give|given|"
    r"use|used|prescribe|order|prefer|preferred|option|recommend|recommended|suitable|appropriate|tolerated|"
    r"proceed|can be|may be|could be|would be|no problem|not a problem|not an issue|not a concern)\b"
)
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.")
_HARD_STOP_NOTE = re.compile(r"\[HARD STOP --[^\]]*\]")


def _sentences(text: str) -> list[str]:
    """Sentence-ish units: end punctuation, a semicolon or a line break ends one; a colon or dash does NOT, so a
    class list and the statement about it stay together. Common abbreviations do not end a sentence."""
    for abbreviation in _ABBREVIATIONS:
        text = text.replace(abbreviation, abbreviation.replace(".", "\u2024"))
    return [part.replace("\u2024", ".") for part in re.split(r"(?<=[.!?])\s+|\n+|;\s*", text) if part.strip()]


def _sentence_only_avoids(sentence: str) -> bool:
    lowered = sentence.lower()
    if _NEGATED_MARKER.search(lowered):
        return False
    without_negated_actions = _NEGATED_ACTION.sub(" ", lowered)
    if not (_STRONG_AVOIDANCE.search(lowered) or _NEGATED_ACTION.search(lowered)):
        return False
    return _NOT_AN_AVOIDANCE.search(without_negated_actions) is None


def _every_mention_is_an_avoidance(response: str, medication: str) -> bool:
    """True only if the drug is named at least once and every sentence naming it is an avoidance sentence.
    The verification layer's own appended HARD STOP notes are ignored: they are not the model's statements."""
    med_lower = medication.lower()
    mentions = [sentence for sentence in _sentences(_HARD_STOP_NOTE.sub(" ", response)) if med_lower in sentence.lower()]
    return bool(mentions) and all(_sentence_only_avoids(sentence) for sentence in mentions)


def _mentions_conflict_near_medication(response: str, medication: str, matched_allergy_text: str | None, window: int = 200) -> bool:
    lowered = response.lower()
    med_lower = medication.lower()
    descriptive_cue = _descriptive_allergy_cue(matched_allergy_text, medication)
    safe_cues = _CONFLICT_SAFE_CUES + ([descriptive_cue] if descriptive_cue else [])
    idx = lowered.find(med_lower)
    while idx != -1:
        window_text = lowered[max(0, idx - window) : idx + len(med_lower) + window]
        if any(cue in window_text for cue in safe_cues):
            return True
        idx = lowered.find(med_lower, idx + 1)
    return _every_mention_is_an_avoidance(response, medication)


def _check_domain_constraint(
    draft_response: str,
    grounded_records: list[ToolCallRecord],
    patient_id: str,
    fhir: FhirClient,
) -> tuple[str, list[str], bool]:
    already_checked = {
        rec.output.medication_name.lower()
        for rec in grounded_records
        if isinstance(rec.output, CheckAllergyConflictOutput)
    }
    legacy_candidates = [t for t in _find_candidate_terms(draft_response) if t.lower() in _MEDICATION_TERMS]
    # Every curated drug (generic AND brand: Bactrim, Zosyn, Rocephin, ...) the response names, from the same class
    # table check_allergy_conflict evaluates -- so a drug the tool can classify is also enforced here even if the
    # model never called it. A legacy term the table now covers is dropped in favour of the (longer) table alias.
    table_candidates = drug_names_in(draft_response)
    med_candidates = table_candidates + [
        t for t in legacy_candidates if not any(t.lower() in alias for alias in table_candidates)
    ]

    enforced: list[str] = []
    passed = True
    response = draft_response
    for med in med_candidates:
        if med.lower() in already_checked:
            continue
        result = check_allergy_conflict(fhir, {"patient_id": patient_id, "medication_name": med}, [])
        if not isinstance(result, CheckAllergyConflictOutput):
            continue  # tool failure here is logged separately by the caller
        if result.conflict_found:
            mentions_conflict = _mentions_conflict_near_medication(response, med, result.matched_allergy_text)
            if not mentions_conflict:
                passed = False
                confidence_note = " (based on an uncoded/narrative allergy entry, lower confidence)" if result.low_confidence else ""
                response += (
                    f"\n\n[HARD STOP -- allergy conflict: {result.matched_allergy_text} "
                    f"conflicts with {med}{confidence_note}. This was not stated in the "
                    f"drafted response and has been added by the verification layer.]"
                )
                enforced.append(med)
    return response, enforced, passed


# Wording that shows the drafted answer already tells the resident a medication could not be checked.
_UNASSESSED_CUES = [
    "couldn't check", "could not check", "can't check", "cannot check", "unable to check", "not able to check",
    "wasn't able to check", "not assessed", "couldn't be checked", "verify with pharmacy", "check with pharmacy",
    "confirm with pharmacy", "pharmacy",
]


def _mentions_unassessed_near_medication(response: str, medication: str, window: int = 200) -> bool:
    lowered = response.lower()
    med_lower = medication.lower()
    idx = lowered.find(med_lower)
    while idx != -1:
        if any(cue in lowered[max(0, idx - window) : idx + len(med_lower) + window] for cue in _UNASSESSED_CUES):
            return True
        idx = lowered.find(med_lower, idx + 1)
    return False


def _enforce_unassessed_allergy_checks(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """A check_allergy_conflict that came back "not_assessed" (drug not in the curated table, no name match) is
    NOT a clearance. If the answer names that medication without saying it couldn't be checked, append a plain
    note -- the same append, don't-replace mechanism as the other enforcements. Only medications the model actually
    checked and the answer actually names are covered: re-checking every drug mention would flag a patient's own
    current medications as "couldn't check" on every turn."""
    enforced: list[str] = []
    seen: set[str] = set()
    for rec in grounded_records:
        out = rec.output
        if not isinstance(out, CheckAllergyConflictOutput) or out.assessment != "not_assessed":
            continue
        med = out.medication_name
        if med.lower() in seen or med.lower() not in response.lower():
            continue
        seen.add(med.lower())
        if _mentions_unassessed_near_medication(response, med):
            continue
        response += (
            f"\n\n[Allergy check note: I couldn't check {med} against this patient's allergy list "
            "-- it is not a drug this check can evaluate. This is NOT a clearance: verify with pharmacy "
            "before giving it.]"
        )
        enforced.append(f"allergy_check_not_assessed: {med}")
    return response, enforced


def _enforce_duplicate_and_empty_chart(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    enforced = []
    for rec in grounded_records:
        if not isinstance(rec.output, GetPatientSnapshotOutput):
            continue
        snap = rec.output
        if snap.duplicate_warnings and not _mentions_duplicate_warning(response, snap.duplicate_warnings):
            other_ids = ", ".join(w.other_patient_id for w in snap.duplicate_warnings)
            response += (
                f"\n\n[Verification note: there are {len(snap.duplicate_warnings)} other "
                f"patient record(s) matching this patient's name and birthdate "
                f"({other_ids}). This is a real ambiguity in the chart -- do not assume "
                f"they're the same visit or that data hasn't diverged between them.]"
            )
            enforced.append("duplicate_patient")
        if snap.chart_is_empty and not _mentions_empty_chart(response):
            response += (
                "\n\n[Verification note: this patient's chart has no recorded problems, "
                "medications, or allergies -- that's an empty/incomplete chart, not "
                "confirmation there's nothing to report.]"
            )
            enforced.append("empty_chart")
    return response, enforced


# Same false-positive-avoidance philosophy as _EMPTY_CHART_CUES/_DUPLICATE_
# CUES above: specific enough to the "nothing to diff against" concept that
# an unrelated use of e.g. "confirm" elsewhere in the response can't
# satisfy it.
_NO_BASELINE_CUES = [
    "no baseline", "nothing to compare", "no earlier", "first look",
    "haven't looked", "hasn't been established", "not yet established",
    "no prior", "nothing gathered yet", "no reference point",
    "cannot confirm", "can't confirm", "unable to confirm",
]


def _mentions_no_baseline_disclosure(response: str) -> bool:
    lowered = response.lower()
    return any(cue in lowered for cue in _NO_BASELINE_CUES)


def _enforce_no_baseline_disclosure(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """Regression fix, same severity class as the domain-constraint
    backstop bug (2026-09-22): compare_signout_to_chart already tracks
    baseline_established=False correctly -- nothing gathered yet this
    conversation to diff a sign-out claim against -- but that signal
    wasn't reliably reaching the final response. Live-confirmed: a fresh,
    single-point-in-time fetch got presented as "the sign-out's claim...
    holds up -- no discrepancies were found," treating the absence of an
    earlier reference point as if it were confirmation of no change,
    exactly the unverifiable-claim-presented-as-verified shape
    ARCHITECTURE.md 3.2 exists to prevent. Same append, don't-silently-
    replace enforcement shape as _enforce_duplicate_and_empty_chart above,
    not a hard block (Decision 10: this system always answers honestly
    rather than refusing outright) -- a model that already disclosed this
    correctly gets no redundant note."""
    enforced = []
    for rec in grounded_records:
        if not isinstance(rec.output, CompareSignoutToChartOutput):
            continue
        if not rec.output.baseline_established and not _mentions_no_baseline_disclosure(response):
            response += (
                "\n\n[Verification note: this conversation has nothing gathered yet for this "
                "patient to diff a sign-out claim against -- this fetch is the baseline, not "
                "confirmation that nothing has changed. The absence of an earlier reference "
                "point is not confirmation of no change.]"
            )
            enforced.append("no_baseline_established")
    return response, enforced


def _enforce_partial_failures(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """Force-discloses a tool's own partial_failures (app/schemas.py) if the
    drafted response doesn't already mention them -- the same append,
    don't-silently-replace mechanism as _enforce_duplicate_and_empty_chart
    above, for the same reason: whether the resident learns a sub-fetch
    failed should not depend entirely on the model choosing to surface it
    from the raw tool JSON. Added 2026-09-20, closing exactly that gap
    found investigating get_recent_encounters' sensitivity-lookup 401 --
    see evals/COVERAGE.md's corrected entry."""
    enforced = []
    for rec in grounded_records:
        if not isinstance(rec.output, _PARTIAL_FAILURE_TYPES):
            continue
        failures = rec.output.partial_failures
        if failures and not _mentions_partial_failure(response, failures):
            response += (
                "\n\n[Verification note: part of this patient's data could not be "
                f"fully retrieved this turn ({'; '.join(failures)}). Treat the "
                "affected information as unconfirmed, not absent -- retry or check "
                "OpenEMR directly if it matters for this decision.]"
            )
            enforced.append("partial_failure")
    return response, enforced


# W2 PRD Section 4.2's hard rule: "any response citing an AI-Extracted-
# Pending-Review-bucketed or low/unreadable-confidence fact MUST explicitly
# flag it as unreconciled/uncertain. Enforced in verification, checkable,
# not left to the system prompt alone." Same false-positive-avoidance
# philosophy as _EMPTY_CHART_CUES/_DUPLICATE_CUES/_NO_BASELINE_CUES above:
# specific enough to the "this is unreconciled/uploaded/pending-review
# data" concept that unrelated uses of e.g. "uncertain" elsewhere in the
# response can't satisfy it by coincidence.
_UPLOADED_DOCUMENT_DISCLOSURE_CUES = [
    "uploaded", "unreconciled", "pending review", "ai-extracted", "ai extracted",
    "not yet reconciled", "not part of the verified chart", "not part of the chart",
    "from a scanned", "from an uploaded", "extracted from",
]


def _mentions_uploaded_document_disclosure(response: str) -> bool:
    lowered = response.lower()
    return any(cue in lowered for cue in _UPLOADED_DOCUMENT_DISCLOSURE_CUES)


def mentions_uploaded_document_disclosure(response: str) -> bool:
    """Public wrapper for app/graph/supervisor.py's synthesize_node, which
    needs this same disclosure check for CoPilotState.extracted_facts (a
    second, direct path to uploaded-document data alongside the
    get_uploaded_documents tool this module already enforces against) --
    same public-wrapper convention as medication_candidates_in above."""
    return _mentions_uploaded_document_disclosure(response)


def _enforce_uploaded_document_disclosure(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """Turn-level, not per-fact: every fact get_uploaded_documents can ever
    return comes from the AI-Extracted Pending Review category by
    construction (app/ingestion/storage.py's AI_EXTRACTED_PENDING_REVIEW_PATH
    is the only place that tool reads from) -- so the hard rule reduces to
    "if this turn cites ANYTHING get_uploaded_documents returned, the
    response must disclose it's unreconciled/pending-review data
    somewhere," the same turn-level append-if-missing shape as
    _enforce_duplicate_and_empty_chart/_enforce_no_baseline_disclosure
    above, not a per-fact near-mention search."""
    enforced = []
    for rec in grounded_records:
        if not isinstance(rec.output, GetUploadedDocumentsOutput):
            continue
        if not rec.output.documents:
            continue  # nothing was actually returned this turn -- nothing to disclose
        any_cited = any(
            fact.value.lower() in response.lower() for doc in rec.output.documents for fact in doc.facts
        )
        if any_cited and not _mentions_uploaded_document_disclosure(response):
            response += (
                "\n\n[Verification note: this response draws on data from an uploaded document "
                "(AI-extracted, pending review) -- treat it as unreconciled/unverified against the "
                "authoritative chart until confirmed.]"
            )
            enforced.append("uploaded_document_disclosure")
    return response, enforced


# Part (a) of the context-blowup fix (app/ingestion/storage.py's
# MAX_UPLOADED_DOCUMENTS_RETURNED cap): distinct vocabulary from
# _UPLOADED_DOCUMENT_DISCLOSURE_CUES above, which flags "this data is
# unreconciled/pending review" -- this instead flags "the document list
# you're seeing is incomplete," an unrelated claim that happens to also be
# about uploaded documents. A response could correctly disclose one and
# not the other.
_UPLOADED_DOCUMENT_OMISSION_CUES = [
    "older document", "older upload", "more documents", "additional document",
    "not all", "only the", "most recent", "omitted", "were left out", "beyond the",
]


def _mentions_uploaded_document_omission(response: str) -> bool:
    lowered = response.lower()
    return any(cue in lowered for cue in _UPLOADED_DOCUMENT_OMISSION_CUES)


def mentions_uploaded_document_omission(response: str) -> bool:
    """Public wrapper for evals/cases.py's uploaded_document_cap_discloses_omitted_count
    case -- same public-wrapper convention as mentions_uploaded_document_disclosure
    above."""
    return _mentions_uploaded_document_omission(response)


def _enforce_uploaded_document_omission_disclosure(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """Turn-level, unconditional on which facts are cited -- unlike
    _enforce_uploaded_document_disclosure above (which only fires if a
    returned fact is actually cited), an omission is about documents the
    response COULDN'T have cited at all, so there is no "was it cited"
    gate: any turn where get_uploaded_documents reports omitted_count > 0
    must disclose that older documents exist beyond what's shown, even if
    the response only discusses the documents it did receive."""
    enforced = []
    for rec in grounded_records:
        if not isinstance(rec.output, GetUploadedDocumentsOutput):
            continue
        if rec.output.omitted_count <= 0:
            continue
        if not _mentions_uploaded_document_omission(response):
            response += (
                f"\n\n[Verification note: {rec.output.omitted_count} older uploaded document(s) for this "
                "patient were not included above -- only the most recent are shown.]"
            )
            enforced.append("uploaded_document_omission_disclosure")
    return response, enforced


_LOW_CONFIDENCE_TIERS = ("low", "unreadable")

# Shared vocabulary for a resident-readable field label -- imported by
# app/graph/supervisor.py's same-turn path too (LAB_SUBFIELD_LABELS/
# INTAKE_FIELD_LABELS below), specifically so both paths' confidence notes
# use identical wording for the same field, not two independently-guessed
# phrasings. Composed with a lab result's own test_name VALUE for lab
# fields (e.g. "Potassium value", "Potassium unit"), used directly for
# intake fields (e.g. "Allergy", "Chief concern").
LAB_SUBFIELD_LABELS: dict[str, str] = {
    "test_name": "test name",
    "value": "value",
    "unit": "unit",
    "reference_range": "reference range",
    "collection_date": "collection date",
    "abnormal_flag": "abnormal flag",
}
INTAKE_FIELD_LABELS: dict[str, str] = {
    "name": "patient name",
    "date_of_birth": "date of birth",
    "sex": "sex",
    "chief_concern": "chief concern",
    "current_medications": "current medication",
    "allergies": "allergy",
    "family_history": "family history",
}

_LAB_FIELD_NAME_RE = re.compile(r"^results\[(\d+)\]\.(\w+)$")
_DEMOGRAPHICS_FIELD_NAME_RE = re.compile(r"^demographics\.(\w+)$")
_LIST_FIELD_NAME_RE = re.compile(r"^(current_medications|allergies|family_history)\[\d+\]$")


def _human_readable_field_labels(facts: list[UploadedDocumentFactSummary]) -> list[str]:
    """A resident-readable label per fact, same order as `facts` --
    app/tools.py's _flatten_extraction_facts builds each fact's own
    field_name in a machine-oriented, structural shape ("results[0].value",
    "demographics.name") meant for source_resource/citation composition,
    not display; this is a separate, additive derivation for the
    confidence note specifically, so that format is left completely
    untouched for its existing purpose.

    A lab subfield's label is composed with its OWN lab result's test_name
    VALUE (e.g. "Potassium value", "Potassium unit"), found by grouping
    facts that share the same results[N] index -- falls back to the bare
    subfield label if no test_name fact exists in that group (should not
    happen given _flatten_extraction_facts always emits test_name first
    for every lab result, but never assumed)."""
    test_names_by_group: dict[str, str] = {}
    for fact in facts:
        match = _LAB_FIELD_NAME_RE.match(fact.field_name)
        if match and match.group(2) == "test_name":
            test_names_by_group[match.group(1)] = fact.value

    labels: list[str] = []
    for fact in facts:
        lab_match = _LAB_FIELD_NAME_RE.match(fact.field_name)
        if lab_match:
            group, subfield = lab_match.group(1), lab_match.group(2)
            subfield_label = LAB_SUBFIELD_LABELS.get(subfield, subfield.replace("_", " "))
            test_name = test_names_by_group.get(group)
            labels.append(f"{test_name} {subfield_label}" if test_name else subfield_label.capitalize())
            continue
        demo_match = _DEMOGRAPHICS_FIELD_NAME_RE.match(fact.field_name)
        if demo_match:
            labels.append(INTAKE_FIELD_LABELS.get(demo_match.group(1), demo_match.group(1)).capitalize())
            continue
        list_match = _LIST_FIELD_NAME_RE.match(fact.field_name)
        if list_match:
            labels.append(INTAKE_FIELD_LABELS.get(list_match.group(1), list_match.group(1)).capitalize())
            continue
        if fact.field_name == "chief_concern":
            labels.append(INTAKE_FIELD_LABELS["chief_concern"].capitalize())
            continue
        # Unrecognized shape -- fall back to the raw identifier rather
        # than guessing at a label that could be wrong.
        labels.append(fact.field_name)
    return labels


def _cited_low_confidence_facts(
    response: str, grounded_records: list[ToolCallRecord]
) -> list[tuple[str, str, str]]:
    """(human_label, value, confidence) for every GetUploadedDocumentsOutput
    fact whose confidence is low/unreadable AND whose value is actually
    cited in the response -- same substring convention as
    _enforce_uploaded_document_disclosure's any_cited check. The later-turn
    (tool-call) half of W2_ARCHITECTURE.md 4.5's planned per-value
    confidence flagging: extracted values ground and are disclosed
    identically regardless of confidence tier today (4.5's own finding);
    this is the finer-grained per-value warning that was missing, not a
    change to grounding or the turn-level disclosure, both of which stay
    exactly as they are."""
    lowered = response.lower()
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for rec in grounded_records:
        if not isinstance(rec.output, GetUploadedDocumentsOutput):
            continue
        for doc in rec.output.documents:
            labels = _human_readable_field_labels(doc.facts)
            for label, fact in zip(labels, doc.facts):
                if fact.confidence not in _LOW_CONFIDENCE_TIERS:
                    continue
                if fact.value.lower() not in lowered:
                    continue
                key = f"{label}|{fact.value}"
                if key in seen:
                    continue
                seen.add(key)
                found.append((label, fact.value, fact.confidence))
    return found


def _confidence_clause(label: str, value: str, confidence: str) -> str:
    return f"{label} ({value}) was read with {confidence} confidence"


def _without_already_noted(response: str, triples: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The triples whose exact clause an earlier confidence note in `response` does not already state.

    Two paths can note the same document in one turn: verify_response() notes the values cited from a
    get_uploaded_documents tool result, and app/graph/supervisor.py's synthesize_node then notes this
    turn's freshly extracted facts. When the chart-data worker and the intake extractor both cover the
    same document (an answer to a question sent with uploaded_document_ref), the second note repeated the
    first one's clauses. Only the clauses not yet stated are added; a value not yet noted still is."""
    return [triple for triple in triples if _confidence_clause(*triple) not in response]


def _format_confidence_note(triples: list[tuple[str, str, str]]) -> str:
    """Shared wording for the per-value confidence note -- app/graph/
    supervisor.py's synthesize_node builds the same-turn equivalent
    independently (from the structured ExtractionResult directly, rather
    than parsing a flattened field_name string), but both use the same
    LAB_SUBFIELD_LABELS/INTAKE_FIELD_LABELS vocabulary and this same
    formatting function, so a resident sees identical phrasing for the
    same field regardless of which turn the value was extracted on."""
    clauses = "; ".join(_confidence_clause(*triple) for triple in triples)
    return (
        f"\n\n[Confidence note: {clauses} -- check the scan before relying on this. Distinct from "
        "the general pending-review disclosure above.]"
    )


def _enforce_low_confidence_value_warnings(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    """W2_ARCHITECTURE.md 4.5's planned fix, later-turn half: a cited
    low/unreadable-confidence extracted value keeps its place in the
    answer -- never silently filtered out of grounding, per 4.5's own
    rationale (that would delete the information from the resident's
    answer entirely, worse than surfacing it with a warning) -- but gets
    an explicit per-value uncertainty note, distinct from and in addition
    to the blanket turn-level pending-review disclosure
    _enforce_uploaded_document_disclosure already appends."""
    cited = _cited_low_confidence_facts(response, grounded_records)
    if not cited:
        return response, []
    return response + _format_confidence_note(cited), ["low_confidence_value_warning"]


def _effective_domain_check_patient_id(
    current_patient_id: str | None, turn_records: list[ToolCallRecord]
) -> str | None:
    """The patient the hard-coded allergy-conflict backstop should run
    against. Prefers the caller-declared active patient; if the caller
    omitted it, falls back to whichever single patient this turn's own tool
    calls actually grounded data for -- an omitted request field is not a
    reason to skip a code-level safety check (ARCHITECTURE.md 3.2's "a wall,
    not a request"). Concretely: `patient_id: null` on /chat previously
    disabled this check entirely (passed_domain_constraint stayed True with
    no [HARD STOP] even for a real, documented conflict) purely because
    `if current_patient_id:` was falsy -- confirmed live against pid1's real
    penicillin allergy. Returns None only when genuinely ambiguous (more than
    one distinct patient touched this turn), matching this system's already-
    documented single-active-patient-per-turn assumption (README.md Known
    Gaps) rather than guessing which one to check.
    """
    if current_patient_id:
        return current_patient_id
    record_patient_ids = {r.patient_id for r in turn_records if r.patient_id}
    if len(record_patient_ids) == 1:
        return next(iter(record_patient_ids))
    return None


def verify_response(
    draft_response: str,
    turn_records: list[ToolCallRecord],
    current_patient_id: str | None,
    fhir: FhirClient,
    extra_grounded_text: list[str] | None = None,
) -> VerificationOutcome:
    """extra_grounded_text: raw source text to ground candidate terms
    against, IN ADDITION TO turn_records' own facts -- app/graph/
    supervisor.py's synthesize_node passes each retrieved guideline
    chunk's text here (PRD Section 4.4's evidence_retriever domain).
    Found live: a synthesized answer correctly restating a guideline's own
    general medication guidance (e.g. "insulin, then albuterol" as part of
    standard hyperkalemia management, not a claim this patient IS on
    either) was getting stripped as "unverified" -- turn_records only ever
    grounds PATIENT-specific facts, and general clinical-reference content
    was never in scope for that check at all. A term found literally
    inside a cited guideline chunk is exactly as grounded as a term found
    inside a cited FHIR resource; this parameter closes that gap without
    touching _grounded_vocabulary's existing FHIR-only logic (still used
    unmodified by app/agent.py's Week 1 callers, which never pass this).
    None/default for every existing caller -- behavior unchanged."""
    grounded = _grounded_vocabulary(turn_records)
    for text in extra_grounded_text or []:
        grounded.add(text.lower())
        grounded.update(_range_endpoint_values(text))
    after_source_check, flagged = _check_source_attribution(draft_response, grounded)

    domain_enforced: list[str] = []
    passed_domain = True
    effective_patient_id = _effective_domain_check_patient_id(current_patient_id, turn_records)
    if effective_patient_id:
        after_source_check, domain_enforced, passed_domain = _check_domain_constraint(
            after_source_check, turn_records, effective_patient_id, fhir
        )

    after_source_check, unassessed_enforced = _enforce_unassessed_allergy_checks(after_source_check, turn_records)

    after_structural_check, structural_enforced = _enforce_duplicate_and_empty_chart(
        after_source_check, turn_records
    )
    after_baseline_check, no_baseline_enforced = _enforce_no_baseline_disclosure(
        after_structural_check, turn_records
    )
    after_partial_failure_check, partial_failure_enforced = _enforce_partial_failures(
        after_baseline_check, turn_records
    )
    after_uploaded_document_check, uploaded_document_enforced = _enforce_uploaded_document_disclosure(
        after_partial_failure_check, turn_records
    )
    after_omission_check, omission_enforced = _enforce_uploaded_document_omission_disclosure(
        after_uploaded_document_check, turn_records
    )
    final_response, low_confidence_enforced = _enforce_low_confidence_value_warnings(
        after_omission_check, turn_records
    )

    return VerificationOutcome(
        passed_source_attribution=not flagged,
        passed_domain_constraint=passed_domain,
        final_response=final_response,
        flagged_claims=flagged,
        enforced_warnings=(
            domain_enforced
            + unassessed_enforced
            + structural_enforced
            + no_baseline_enforced
            + partial_failure_enforced
            + uploaded_document_enforced
            + omission_enforced
            + low_confidence_enforced
        ),
    )
