"""Medical reference verification layer: informational footnotes drawing on
DailyMed (medications) and MedlinePlus (conditions), added alongside the
model's response, never blocking or judging it.

Design constraints, investigated and agreed before any code was written:

- Fully deterministic end to end. No model call anywhere in this module --
  not to decide relevance, not to compare, not to summarize. If a fixed
  section can't be extracted cleanly and deterministically, that's a "no
  reference note available" case (ReferenceNote.note_text=None), never a
  reason to add a model call or fabricate/paraphrase a substitute.
- Not a correctness check. This never blocks, flags, or contradicts the
  model's own response -- purely additive context, the same spirit as a
  footnote. It adds no coverage to verify_response() -- the same honest
  distinction already made for summarize_shift_events' SYSTEM_PROMPT rule.
- Strict patient-data isolation: every function here that talks to a real
  API takes only a bare drug/condition name string -- no FhirClient, no
  patient_id, no ToolCallRecord, nothing else from the chart. Isolation is
  enforced by the signature, not by convention (same principle already
  used for summarize_shift_events omitting `fhir` entirely).
- Two independent, structural triggers -- see _medications_recommended and
  _conditions_in_turn_records below for the reasoning behind each.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from app.schemas import ConditionFact, GetPatientSnapshotOutput, ReferenceNote
from app.tools import ToolCallRecord
from app.verification import medication_candidates_in

_DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
_MEDLINEPLUS_BASE = "https://wsearch.nlm.nih.gov/ws/query"
_SPL_NS = "{urn:hl7-org:v3}"
_WARNINGS_AND_PRECAUTIONS_CODE = "43685-7"

_REQUEST_TIMEOUT_SECONDS = 10.0

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    """SPL/MedlinePlus source XML preserves original indentation and line
    breaks inside element text -- found live, a real Lisinopril excerpt
    came back with embedded "\\n  \\n     " noise from the source
    document's own formatting. Collapses any run of whitespace to a single
    space, a purely mechanical cleanup with no relevance judgment."""
    return _WHITESPACE_RE.sub(" ", text).strip()


# --- Medication path (DailyMed) ----------------------------------------------


def _build_dailymed_query(drug_name: str) -> dict[str, str]:
    """Pure, network-free: the exact query this module sends to DailyMed.
    Kept separate from the HTTP call so patient-data isolation is directly,
    deterministically testable -- assert on this dict's contents, not on
    intercepting a live request. name_type=generic reduces (does not
    eliminate) the chance of matching a combination-product label instead
    of a plain single-ingredient one; pagesize=1 plus taking the first
    result is a fixed, deterministic pick, not a "best match" search."""
    return {"drug_name": drug_name, "name_type": "generic", "pagesize": "1"}


def _extract_dailymed_excerpt(spl_xml: str) -> tuple[str | None, str | None]:
    """Returns (excerpt_text, section_title) for the Warnings and
    Precautions section's short, FDA-authored excerpt/highlight -- or
    (None, None) if that specific section+excerpt shape isn't present.

    Confirmed live against 5 real drugs before writing this: 4 of 5
    (Metformin, Lisinopril, Warfarin, Amoxicillin, Albuterol) use the
    modern combined "WARNINGS AND PRECAUTIONS SECTION" (LOINC 43685-7),
    which carries a structured <excerpt><highlight> sub-element -- FDA's
    own short bullet-point summary, not something this module invents a
    slice of. Penicillin's real label uses the older, separate "WARNINGS
    SECTION" (LOINC 34071-1) instead, which has no excerpt at all -- just
    raw paragraphs, sometimes long. Deliberately does NOT fall back to
    extracting from that older section's free-form text: there is no
    fixed, non-arbitrary slice to take from an unstructured paragraph
    without the relevance judgment this mechanism explicitly avoids. That
    gap (older-format labels) is a real, disclosed coverage limitation,
    not something this function tries to paper over.
    """
    try:
        root = ET.fromstring(spl_xml)
    except ET.ParseError:
        return None, None
    for section in root.iter(f"{_SPL_NS}section"):
        code_el = section.find(f"{_SPL_NS}code")
        if code_el is None or code_el.get("code") != _WARNINGS_AND_PRECAUTIONS_CODE:
            continue
        title_el = section.find(f"{_SPL_NS}title")
        title = _normalize_whitespace("".join(title_el.itertext())) if title_el is not None else "Warnings and Precautions"
        highlight = section.find(f"{_SPL_NS}excerpt/{_SPL_NS}highlight")
        if highlight is None:
            return None, None
        items = [
            text for el in highlight.iter(f"{_SPL_NS}item") if (text := _normalize_whitespace("".join(el.itertext())))
        ]
        if items:
            return "; ".join(items), title
        paragraphs = [
            text
            for el in highlight.iter(f"{_SPL_NS}paragraph")
            if (text := _normalize_whitespace("".join(el.itertext())))
        ]
        if paragraphs:
            return " ".join(paragraphs), title
        return None, None
    return None, None


def _fetch_dailymed_note(drug_name: str, client: httpx.Client | None = None) -> ReferenceNote:
    """The only function in this path that touches the network. Takes
    (and needs) only `drug_name` -- see this module's own docstring on
    isolation."""
    owns_client = client is None
    client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        search = client.get(f"{_DAILYMED_BASE}/spls.json", params=_build_dailymed_query(drug_name))
        search.raise_for_status()
        results = search.json().get("data") or []
        if not results:
            return ReferenceNote(subject_type="medication", subject_name=drug_name, source="DailyMed")
        setid = results[0]["setid"]
        label = client.get(f"{_DAILYMED_BASE}/spls/{setid}.xml")
        label.raise_for_status()
        excerpt, _title = _extract_dailymed_excerpt(label.text)
        source_url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
        return ReferenceNote(
            subject_type="medication",
            subject_name=drug_name,
            source="DailyMed",
            note_text=excerpt,
            source_url=source_url if excerpt else None,
        )
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return ReferenceNote(subject_type="medication", subject_name=drug_name, source="DailyMed")
    finally:
        if owns_client:
            client.close()


# --- Illness path (MedlinePlus) ----------------------------------------------


def _build_medlineplus_query(condition_text: str) -> dict[str, str]:
    """Pure, network-free -- same isolation-testing rationale as
    _build_dailymed_query. retmax=1 plus taking the top-ranked result is a
    fixed, deterministic pick (MedlinePlus's own relevance ranking), not a
    judgment call made here."""
    return {"db": "healthTopics", "term": condition_text, "retmax": "1"}


def _extract_medlineplus_summary(response_xml: str) -> tuple[str | None, str | None, str | None]:
    """Returns (first_paragraph_text, page_title, page_url) for the
    top-ranked result's FullSummary, split at the first paragraph boundary
    -- confirmed live against Type 2 diabetes, hypertension, and COPD to
    all follow a consistent "What is X?" heading + one definitional
    paragraph structure. Returns (None, None, None) if no result, or if
    FullSummary is missing or has no extractable paragraph."""
    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError:
        return None, None, None
    document = root.find(".//document")
    if document is None:
        return None, None, None
    page_url = document.get("url")
    title = summary = None
    for content in document.findall("content"):
        name = content.get("name")
        if name == "title":
            title = "".join(content.itertext())
        elif name == "FullSummary":
            summary = "".join(content.itertext())
    if not summary:
        return None, None, page_url
    # FullSummary is HTML with embedded <p> tags -- take the text up to
    # the first paragraph break as the fixed, deterministic slice.
    parts = re.split(r"<p>", summary, maxsplit=1)
    first_chunk = re.sub(r"<[^>]+>", "", parts[0])
    if len(parts) > 1:
        first_para = re.sub(r"<[^>]+>", "", parts[1].split("</p>")[0])
        text = _normalize_whitespace(f"{first_chunk} {first_para}")
    else:
        text = _normalize_whitespace(first_chunk)
    return (text or None), (_normalize_whitespace(title) if title else None), page_url


def _fetch_medlineplus_note(condition_text: str, client: httpx.Client | None = None) -> ReferenceNote:
    """The only function in this path that touches the network. Takes
    (and needs) only `condition_text` -- see this module's own docstring
    on isolation.

    Uses the MedlinePlus Health Topics Search service (wsearch.nlm.nih.gov),
    NOT MedlinePlus Connect, despite Connect being the more commonly cited
    MedlinePlus API: Connect requires a coded diagnosis (ICD-10-CM,
    ICD-9-CM, or SNOMED CT) as its query key and does not accept a
    free-text condition name for diagnosis lookups. Checked live against
    this system's own real Condition FHIR data before choosing: every
    ConditionFact here has `code: {"text": "..."}`, with no `coding` array
    at all -- no ICD-10, no SNOMED, nothing to feed Connect. The Health
    Topics Search service accepts free text directly, which is what this
    system actually has.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        response = client.get(_MEDLINEPLUS_BASE, params=_build_medlineplus_query(condition_text))
        response.raise_for_status()
        text, _title, page_url = _extract_medlineplus_summary(response.text)
        return ReferenceNote(
            subject_type="condition",
            subject_name=condition_text,
            source="MedlinePlus",
            note_text=text,
            source_url=page_url if text else None,
        )
    except (httpx.HTTPError, ValueError):
        return ReferenceNote(subject_type="condition", subject_name=condition_text, source="MedlinePlus")
    finally:
        if owns_client:
            client.close()


# --- Triggers -----------------------------------------------------------


_MEDICATION_ACTION_VERB_RE = re.compile(
    r"\b(start|starting|begin|beginning|increase|increasing|decrease|decreasing|"
    r"add|adding|discontinue|discontinuing|stop|stopping|hold|holding|"
    r"switch|switching|adjust|adjusting|consider|considering)\b",
    re.IGNORECASE,
)


def _medications_recommended(response_text: str, window: int = 100) -> list[str]:
    """Structural, deterministic trigger for the medication path: fires
    only when a medication-shaped term (reusing verification.py's own
    candidate detection) co-occurs with a prospective action verb within a
    short window -- not on a drug merely being mentioned or read off an
    existing med list.

    Deliberately restricted to base/gerund verb forms ("start",
    "starting"), NOT simple past tense ("started", "discontinued").
    English clinical recommendation language almost always uses an
    imperative, modal, or gerund form ("Start Lisinopril", "consider
    starting", "should start"); simple past tense narrates something that
    already happened ("she was started on Lisinopril last year, then
    discontinued"). Word-boundary matching restricted to those forms means
    "start" cannot match inside "started" (no boundary between "start" and
    the following "ed") -- this was checked directly against exactly that
    historical-narration sentence before shipping, not assumed to work.
    """
    candidates = medication_candidates_in(response_text)
    lowered = response_text.lower()
    fired: list[str] = []
    for med in candidates:
        med_lower = med.lower()
        idx = lowered.find(med_lower)
        while idx != -1:
            window_text = response_text[max(0, idx - window) : idx + len(med) + window]
            if _MEDICATION_ACTION_VERB_RE.search(window_text):
                fired.append(med)
                break
            idx = lowered.find(med_lower, idx + 1)
    # de-dupe, case-insensitive, preserve first-seen casing
    seen: set[str] = set()
    unique = []
    for med in fired:
        key = med.lower()
        if key not in seen:
            seen.add(key)
            unique.append(med)
    return unique


def _conditions_in_turn_records(response_text: str, condition_facts: list[ConditionFact]) -> list[str]:
    """Structural, deterministic trigger for the illness path: fires only
    for a real, chart-sourced ConditionFact already present in this turn's
    turn_records AND actually mentioned in the verified final response --
    never a hypothesized or speculative diagnosis. This is a plain
    substring check, not a heuristic: the "already fetched this turn"
    constraint does essentially all the disambiguating work structurally
    -- a speculative diagnosis was never fetched, so it can never appear
    in condition_facts to begin with."""
    lowered = response_text.lower()
    return [c.text for c in condition_facts if c.text.lower() in lowered]


def build_reference_notes(
    response_text: str, records_for_active_patient: list[ToolCallRecord]
) -> list[ReferenceNote]:
    """Called once per turn from ClinicalCopilotAgent.run_turn, AFTER the
    verified final response is appended to the conversation's own message
    history -- never before. This function's output must never enter the
    model's own context on a later turn (it isn't part of `messages`),
    only ChatTurnResult.reference_notes, a separate field the resident-
    facing layer renders distinctly from the model's own text.
    """
    notes: list[ReferenceNote] = [_fetch_dailymed_note(med) for med in _medications_recommended(response_text)]

    condition_facts = [
        condition
        for record in records_for_active_patient
        if isinstance(record.output, GetPatientSnapshotOutput)
        for condition in record.output.conditions
    ]
    notes += [
        _fetch_medlineplus_note(condition)
        for condition in _conditions_in_turn_records(response_text, condition_facts)
    ]
    return notes
