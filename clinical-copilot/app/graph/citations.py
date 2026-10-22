"""The citation-translation layer (PRD Section 4.4, Checklist item 5):
translates each worker's native output shape into ONE canonical Citation
object, at the supervisor's synthesis step, BEFORE verify_response() runs
-- so verification only ever operates on one canonical shape internally,
even though storage stays split (chart facts live in FHIR resources,
document facts live in OpenEMR documents, guideline facts live in
app/retrieval/corpus/*.md).

Week 1's six existing schemas keep their plain `source_resource: str`
fields, unchanged (app/schemas.py) -- this module ADDS the canonical shape
for the two new domains (documents, guidelines) and for exposing chart
citations in the new shape too, it does not replace the old one anywhere
verify_response() already reads it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent import ChartGatherResult
from app.ingestion.schemas import ExtractedField, ExtractionResult, IntakeFormExtraction, LabPdfExtraction
from app.retrieval import display
from app.retrieval.types import RetrievedEvidence
from app.schemas import GetUploadedDocumentsOutput
from app.verification import _normalize_lab_value

SourceType = Literal["fhir_resource", "uploaded_document", "guideline"]


# What page_or_section says for an uploaded-document field whose page number is not
# known. A stated "unknown", never a guessed page number.
PAGE_UNKNOWN = "page unknown"


class Citation(BaseModel):
    """PRD Core Requirement 5's minimum shape: all five fields are required and
    non-empty, and unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_id: str = Field(min_length=1)
    page_or_section: str = Field(min_length=1)
    field_or_chunk_id: str = Field(min_length=1)
    quote_or_value: str = Field(min_length=1)
    doc_label: str | None = None  # human-readable doc type ("Lab report",
    # "Intake form"), uploaded_document citations only -- lets the UI build
    # a readable chip label ("Lab report (pending review), p.1: ...")
    # instead of the generic "Uploaded document" text every chip used to
    # share regardless of what was actually cited.
    uploaded_on: str | None = None  # UTC upload date of the source document (YYYY-MM-DD), for the
    # UI's citation card title; None when the document predates the field.
    field_label: str | None = None  # readable name of the cited field ("Value (result 1)",
    # "Medication 2") for uploaded_document citations; for fhir_resource citations the readable
    # chart-record label ("Allergy: Sulfa drugs", "Condition: COPD").
    duplicate_count: int | None = None  # uploaded_document citations only: how many stored documents
    # carry this exact extracted content (same document type, same fields, same values) and were
    # collapsed into this one card; None when the document is not duplicated.
    excerpt: str | None = None  # guideline citations only: plain-text preview of the passage
    # (markdown removed, no cut-off word at either end, trimmed at a word boundary).
    display_text: str | None = None  # guideline citations only: the passage with cut-off words
    # and heading markers removed; still markdown-lite, for the UI's escape-first renderer.
    # quote_or_value keeps the raw retrieved chunk unchanged.


_DOC_TYPE_LABELS = {"lab_pdf": "Lab report", "intake_form": "Intake form"}

_FIELD_LABELS = {
    "test_name": "Test name", "value": "Value", "unit": "Unit", "reference_range": "Reference range",
    "collection_date": "Collection date", "abnormal_flag": "Abnormal flag",
    "name": "Name", "date_of_birth": "Date of birth", "sex": "Sex", "chief_concern": "Chief concern",
}
_LIST_FIELD_LABELS = {"current_medications": "Medication", "allergies": "Allergy", "family_history": "Family history"}


def humanize_field_path(field_path: str) -> str:
    """Readable name for an ExtractionResult field path such as
    "results[0].value", "demographics.name", "chief_concern" or
    "allergies[1]". Unknown paths fall back to the leaf, underscores
    spaced -- never raises."""
    lab = re.match(r"^results\[(\d+)\]\.(\w+)$", field_path)
    if lab:
        leaf = _FIELD_LABELS.get(lab.group(2), lab.group(2).replace("_", " "))
        return f"{leaf} (result {int(lab.group(1)) + 1})"
    listed = re.match(r"^(\w+)\[(\d+)\]$", field_path)
    if listed and listed.group(1) in _LIST_FIELD_LABELS:
        return f"{_LIST_FIELD_LABELS[listed.group(1)]} {int(listed.group(2)) + 1}"
    leaf = re.split(r"[.\[]", field_path)[-1].rstrip("]")
    return _FIELD_LABELS.get(leaf, leaf.replace("_", " "))


def _walk_source_resources(node: object) -> list[str]:
    """Every `source_resource` string found anywhere in a tool output's
    nested Pydantic structure -- the same walk app/tools.py's
    collect_source_resources already does for the old shape, reused here
    (not reimplemented) so the two never drift into disagreeing about what
    counts as a source_resource."""
    found: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, BaseModel):
            walk(n.model_dump())
        elif isinstance(n, dict):
            for key, value in n.items():
                if key == "source_resource" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return found


_FHIR_TYPE_LABELS = {
    "Condition": "Condition", "MedicationRequest": "Medication", "AllergyIntolerance": "Allergy",
    "Encounter": "Encounter", "Observation": "Observation", "Patient": "Patient",
}
_CHART_LABEL_MAX_CHARS = 80


def chart_record_label(ref: str, text: str | None) -> str | None:
    """"Allergy: Sulfa drugs" for "AllergyIntolerance/<id>" + its fact text; None
    when the fact carries no text (nothing readable to show beyond the type)."""
    text = " ".join((text or "").split())
    if not text:
        return None
    kind = ref.split("/", 1)[0]
    label = f"{_FHIR_TYPE_LABELS.get(kind, kind)}: {text}"
    if len(label) > _CHART_LABEL_MAX_CHARS:
        label = label[:_CHART_LABEL_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") + "\u2026"
    return label


def _walk_labelled_source_resources(node: object) -> list[tuple[str, str | None]]:
    """Like _walk_source_resources, but also returns the sibling `text` of the
    fact each source_resource belongs to (ConditionFact.text, AllergyFact.text,
    ...), so the UI can say what the record is, not just its type."""
    found: list[tuple[str, str | None]] = []

    def walk(n: object) -> None:
        if isinstance(n, BaseModel):
            walk(n.model_dump())
        elif isinstance(n, dict):
            for key, value in n.items():
                if key == "source_resource" and isinstance(value, str):
                    sibling = n.get("text")
                    found.append((value, sibling if isinstance(sibling, str) else None))
                else:
                    walk(value)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return found


def chart_citations(chart_facts: ChartGatherResult | None, patient_id: str | None) -> list[Citation]:
    """Every distinct source_resource this turn's chart_data_worker
    grounded a fact against, for the active patient -- `quote_or_value` is
    the resource reference itself (e.g. "Condition/<fhir-id>"): the actual
    clinical text lives in the FHIR resource, not duplicated here.

    field_or_chunk_id/page_or_section: a fhir_resource citation has no
    document page or extracted-field-path in the sense the other two
    domains do, but it isn't blank either -- every source_resource this
    codebase ever produces is genuinely "{ResourceType}/{id}" (confirmed
    by reading every source_resource=... call site in app/tools.py: always
    an f-string of exactly that shape for this branch; the "document:..."
    shape is a different, GetUploadedDocumentsOutput-only string this
    branch never sees). field_or_chunk_id is that resource type
    (real, structural information -- which kind of clinical fact this is),
    and page_or_section is the tool call that surfaced it (e.g.
    "get_patient_snapshot") -- the closest real analogue to "which part of
    the chart-gathering process this came from" a live API lookup has,
    not a fabricated placeholder.

    GetUploadedDocumentsOutput records (the get_uploaded_documents tool --
    a LATER turn reading back a document attached earlier) are handled
    separately from the generic source_resource walk, not folded into the
    blanket "fhir_resource" bucket every other tool output falls into.
    Found live 2026-09-24: stamping these fhir_resource meant the frontend
    (chat.html's buildCitationsGroup, which only makes uploaded_document/
    guideline chips clickable) never offered click-to-source for a
    document cited on any turn after the one it was uploaded on -- the
    generic string walk also had no way to recover a clean, resolvable
    doc_id from UploadedDocumentFactSummary.source_resource's compound
    "document:<id>#field" shape. Reading doc.doc_id/doc.doc_type/
    fact.field_name/fact.page_number directly off the typed output (like
    document_citations() already does for the same-turn path) gives a
    real, resolvable source_id and a readable label -- same as the
    same-turn path, not a lesser version of it."""
    if chart_facts is None:
        return []
    citations: list[Citation] = []
    seen_doc_fields: dict[tuple[str, str], None] = {}
    # ref -> the tool call that first surfaced it (first-wins, same dedup
    # convention seen_doc_fields already uses above).
    seen_generic: dict[str, tuple[str, str | None]] = {}
    for record in chart_facts.turn_records:
        if patient_id and record.patient_id != patient_id:
            continue
        if isinstance(record.output, GetUploadedDocumentsOutput):
            for doc in record.output.documents:
                doc_label = _DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type)
                for fact in doc.facts:
                    key = (doc.doc_id, fact.field_name)
                    if key in seen_doc_fields or not fact.value:
                        # A blank extracted value has nothing to quote, and the citation
                        # contract requires a non-empty quote_or_value.
                        continue
                    seen_doc_fields[key] = None
                    citations.append(
                        Citation(
                            source_type="uploaded_document",
                            source_id=doc.doc_id,
                            page_or_section=str(fact.page_number) if fact.page_number is not None else PAGE_UNKNOWN,
                            field_or_chunk_id=fact.field_name,
                            quote_or_value=fact.value,
                            doc_label=doc_label,
                            uploaded_on=doc.uploaded_on,
                            field_label=humanize_field_path(fact.field_name),
                        )
                    )
            continue
        for source_resource, fact_text in _walk_labelled_source_resources(record.output):
            if source_resource not in seen_generic or (seen_generic[source_resource][1] is None and fact_text):
                seen_generic[source_resource] = (record.tool_name, fact_text)
    citations += [
        Citation(
            source_type="fhir_resource",
            source_id=ref,
            quote_or_value=ref,
            page_or_section=tool_name,
            field_or_chunk_id=ref.split("/", 1)[0] if "/" in ref else ref,
            field_label=chart_record_label(ref, fact_text),
        )
        for ref, (tool_name, fact_text) in seen_generic.items()
    ]
    return citations


def document_citations(extracted_facts: list[ExtractionResult]) -> list[Citation]:
    """Per-field (PRD Section 4.4/4.2), not per-document -- one Citation
    per ExtractedField, using its own DocumentCitation (page_number/quote)
    directly. `source_id` is the ORIGINAL source scan's id (result.doc_id,
    category 35) -- deliberately NOT extracted_doc_id (category 36):
    found live 2026-09-24, GET /documents/preview only ever resolves a
    document by the original scan's filename, so a citation pointing at
    extracted_doc_id 404s the moment it's actually clicked. Both this and
    chart_citations()'s document branch now consistently use doc_id."""
    citations: list[Citation] = []
    for result in extracted_facts:
        doc_ref = result.doc_id
        doc_label = _DOC_TYPE_LABELS.get(result.doc_type, result.doc_type)
        fields: list[tuple[ExtractedField, str]] = []  # (field, its ExtractionResult field path)
        if isinstance(result.extraction, LabPdfExtraction):
            for i, lab in enumerate(result.extraction.results):
                for name in ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag"):
                    fields.append((getattr(lab, name), f"results[{i}].{name}"))
        elif isinstance(result.extraction, IntakeFormExtraction):
            demo = result.extraction.demographics
            for name in ("name", "date_of_birth", "sex"):
                if getattr(demo, name) is not None:
                    fields.append((getattr(demo, name), f"demographics.{name}"))
            if result.extraction.chief_concern is not None:
                fields.append((result.extraction.chief_concern, "chief_concern"))
            for list_name in ("current_medications", "allergies", "family_history"):
                for i, item in enumerate(getattr(result.extraction, list_name)):
                    fields.append((item, f"{list_name}[{i}]"))
        for field, field_path in fields:
            if not field.value:
                continue  # nothing to quote; see chart_citations()
            citations.append(
                Citation(
                    source_type="uploaded_document",
                    source_id=doc_ref,
                    page_or_section=str(field.citation.page_number),
                    field_or_chunk_id=field_path,
                    quote_or_value=field.value,
                    doc_label=doc_label,
                    uploaded_on=result.uploaded_on,
                    field_label=humanize_field_path(field_path),
                )
            )
    return citations


def guideline_citations(retrieved_evidence: list[RetrievedEvidence]) -> list[Citation]:
    """One Citation per retrieved guideline chunk (PRD Section 4.4). The UI groups
    them into one card per guideline document; `doc_label` is the document's own
    title and `excerpt`/`display_text` are display-only cleanups of the chunk."""
    citations: list[Citation] = []
    for ev in retrieved_evidence:
        chunk = ev.chunk
        cuts = {
            "starts_mid_word": bool(getattr(chunk, "starts_mid_word", False)),
            "ends_mid_word": bool(getattr(chunk, "ends_mid_word", False)),
            "starts_mid_sentence": bool(getattr(chunk, "starts_mid_sentence", False)),
            "ends_mid_sentence": bool(getattr(chunk, "ends_mid_sentence", False)),
        }
        citations.append(
            Citation(
                source_type="guideline",
                source_id=chunk.source_id,
                page_or_section=chunk.page_or_section,
                field_or_chunk_id=chunk.chunk_id,
                quote_or_value=chunk.text,
                doc_label=getattr(chunk, "doc_title", "") or None,
                excerpt=display.excerpt(chunk.text, **cuts),
                display_text=display.clean_passage(chunk.text, **cuts),
            )
        )
    _trim_chunk_overlaps(citations)
    return citations


_CHUNK_INDEX_RE = re.compile(r":(\d+)$")
_OVERLAP_PROBE_CHARS = 40
_OVERLAP_MAX_CHARS = 400


def _trim_chunk_overlaps(citations: list[Citation]) -> None:
    """Corpus chunks overlap by ~150 characters, so two adjacent chunks of one
    guideline repeat the same sentences when listed together. Remove that repeat
    from the later chunk's display_text (never from quote_or_value, which stays
    the raw retrieved chunk). Only touches chunks whose ids are consecutive
    (":3" then ":4") and only when the repeat is found, so it can only ever
    shorten a passage by text the reader has just seen."""
    by_source: dict[str, dict[int, Citation]] = {}
    for c in citations:
        m = _CHUNK_INDEX_RE.search(c.field_or_chunk_id or "")
        if m:
            by_source.setdefault(c.source_id, {})[int(m.group(1))] = c
    for chunks in by_source.values():
        for index, current in chunks.items():
            previous = chunks.get(index - 1)
            if not previous or not previous.display_text or not current.display_text:
                continue
            probe = current.display_text[:_OVERLAP_PROBE_CHARS]
            at = previous.display_text.find(probe)
            if at < 0 or len(previous.display_text) - at > _OVERLAP_MAX_CHARS:
                continue
            remainder = current.display_text[len(previous.display_text) - at :].lstrip()
            if remainder:
                current.display_text = remainder


_NUMBER_LEADING_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\b")
_STARTS_WITH_NUMBER_RE = re.compile(r"^[+-]?\d")

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y"]

# No capturing groups (month names use a non-capturing (?:...) group) so
# .findall() returns whole matched strings, not group tuples.
_DATE_CANDIDATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
    r"|\b\d{1,2}-\d{1,2}-\d{4}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)


def _parse_date_any(text: str) -> date | None:
    cleaned = text.replace(",", "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _word_boundary_search(needle: str, haystack: str) -> bool:
    """Case-insensitive, but real word-boundary containment, not naive
    substring-anywhere -- the false-positive gap plain `in` has for a
    short value: "H" would match inside "Hospital", "4.2" would match
    inside "34.2". `\\w` excludes "." so a decimal value's boundary check
    is still exactly at the digits, not swallowed by the punctuation."""
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle.lower()) + r"(?!\w)", haystack.lower()) is not None


def _document_fact_stated_in_response(value: str, response_text: str) -> bool:
    """Whether an uploaded-document fact's own extracted value is actually
    stated in the final response text -- filter_citations_to_stated_facts'
    real keep/drop decision, replacing an earlier, cruder substring check
    that could both over-keep (a short value like "H", "Female", or "4.2"
    matching for unrelated reasons -- "Hospital", "34.2") and under-keep (a
    stated fact ending up uncited over a cosmetic formatting difference,
    e.g. "5.9" vs "5.9 mmol/L", or a differently-formatted date) -- the
    second failure mode is explicitly worse than the first: a stated fact
    must never end up uncited.

    Deliberately NOT a reuse of app/verification.py's
    _find_candidate_terms/_is_grounded pipeline, even though that is
    "the matching verification already does" in a narrower sense. That
    pipeline only ever extracts candidates shaped like a curated drug/
    condition-vocabulary term, a dose pattern, or a bare numeric lab
    value -- it never attempts to match a demographics name, a chief-
    concern sentence, or free-text allergy/family-history content, since
    none of those look like a "candidate" to it. Gating citation-keeping
    on that narrower pipeline would silently drop every intake-form
    citation for those field types on every turn -- a worse failure than
    this function's own, intentionally slightly more permissive
    free-text fallback below. What IS reused directly is
    app/verification.py's own _normalize_lab_value -- the same
    whitespace/degree-symbol cosmetic-formatting tolerance verification's
    grounding check already applies to lab/vital values -- not a second,
    independently-invented normalization rule."""
    value = value.strip()
    if not value:
        return False

    if _word_boundary_search(value, response_text):
        return True

    # Date-format tolerance checked BEFORE generic numeric handling below,
    # since an ISO date (e.g. "2026-09-20", this schema's collection_date
    # convention) also starts with a digit and would otherwise be
    # incorrectly routed into the numeric branch, which has no notion of
    # calendar dates at all. Compared as a real date, not string-for-
    # string -- "2026-09-20" and "September 20, 2026" are the same fact
    # stated two different ways, something no substring check (normalized
    # or not) can bridge.
    value_date = _parse_date_any(value)
    if value_date is not None:
        return any(_parse_date_any(candidate) == value_date for candidate in _DATE_CANDIDATE_RE.findall(response_text))

    if _STARTS_WITH_NUMBER_RE.match(value):
        # _normalize_lab_value strips ALL whitespace (by design, so
        # "38.9 C" == "38.9°C") -- safe here because this branch is gated
        # to values that start with a digit, never a multi-word free-text
        # value (that gating is load-bearing: unconditionally normalizing
        # e.g. "Jordan Rivera" would squish it to "JordanRivera" and break
        # its own match against the unsquished response text).
        value_norm = _normalize_lab_value(value)
        response_norm = _normalize_lab_value(response_text)
        if _word_boundary_search(value_norm, response_norm):
            return True

        # A leading-number-plus-unit shaped value (e.g. a hypothetical
        # "5.9 mmol/L" stored as one combined field) still counts as
        # stated if the response restates just the number and drops the
        # unit -- the exact "5.9" vs "5.9 mmol/L" formatting mismatch a
        # plain substring check gets backwards when the CITATION carries
        # more text than the response restates.
        number_match = _NUMBER_LEADING_RE.match(value)
        if number_match and _word_boundary_search(number_match.group(1), response_text):
            return True

        return False

    # A multi-word, non-numeric, non-date value (a name, an allergy
    # description, a chief-concern sentence) can still count as stated by
    # its own most distinctive word -- the first token at least 4
    # characters long -- covers a citation like "Penicillin (hives)"
    # surviving a response that only restates "penicillin allergy",
    # dropping the parenthetical reaction detail. Deliberately loose in
    # this one direction only, per this function's own stated priority: a
    # stated fact must never end up uncited, and a single distinctive-word
    # match carries much lower false-positive risk than a short numeric/
    # flag token would (which is why this fallback is never reached for
    # those -- they already returned above via the exact word-boundary
    # check on the whole (short) value).
    words = re.findall(r"[A-Za-z]{4,}", value)
    if words:
        return _word_boundary_search(words[0], response_text)

    return False


def filter_citations_to_stated_facts(citations: list[dict], response_text: str) -> list[dict]:
    """Drops an uploaded_document citation whose own extracted value never
    actually appears in the final response text -- the documented citation-
    precision gap (KEY_METRICS.md's "Known gap: citation precision isn't
    measured yet"): chart_citations()'s generic _walk_source_resources scan
    picks up every UploadedDocumentFactSummary.source_resource a get_
    uploaded_documents call returned this turn, and document_citations()
    emits one Citation per ExtractedField on a same-turn extraction -- both
    regardless of whether the synthesized response ever actually mentions
    that specific field. Found live: a short answer citing one lab value
    produced roughly 33 citation chips, one per field of every document on
    file, not just the one the answer stated.

    fhir_resource and guideline citations are deliberately left untouched.
    Their own quote_or_value is a bare resource reference (e.g.
    "Condition/<id>") or a full retrieved guideline chunk, neither of which
    is expected to appear verbatim in resident-facing prose the way an
    uploaded document's own extracted value is -- applying the same check
    to those would wrongly strip citations that were never supposed to
    pass it, not close a real gap.

    Multi-document collapse fix (found live via
    uploaded_document_multiple_documents_each_cited_distinctly): a plain
    per-citation text match, with no notion of which document a value
    belongs to, cannot tell "this document is genuinely being discussed"
    from "this document happens to share a boilerplate field value (unit,
    reference range, collection date, ...) with the document that's
    actually being discussed." Two lab documents of the same test type
    routinely share every field except the one that actually differs (the
    value), so a per-value-only check let the untouched document's shared
    fields ride along on the referenced document's citation.

    Fixed by deciding at the DOCUMENT level, not the citation level: a
    document counts as "referenced" this turn only if the response states
    at least one of its values that is NOT shared by any other document
    among these citations (its distinctive value(s)) -- e.g. the "7.8" in
    one lab report vs. another's "4.4", even though both share the same
    test_name/unit/reference_range/collection_date. Only a referenced
    document's own citations are then kept (still individually, via the
    same per-value stated check below -- being "referenced" doesn't mean
    every field the document has gets cited, only the ones the response
    actually states).

    A document with NO distinctive value at all -- every one of its values
    is also present on at least one other cited document, i.e. a genuine
    duplicate/near-duplicate upload -- can never be individually
    "referenced" by this rule, since there is no text that could
    distinguish it from the other one. Rather than silently drop such a
    document (an information-loss failure, arguably worse than the
    over-citation this fix closes), it falls back to the original
    per-value check: kept whenever its (shared) value is stated, same as
    every other document. Two genuinely identical documents are expected
    to both survive together, or neither.

    Keep/drop logic for an individual citation still lives in
    _document_fact_stated_in_response -- see that function's own docstring
    for why it's a real, format-tolerant match rather than a plain
    substring check.

    Known limitation (see W2_ARCHITECTURE.md's Known Issues): this is still
    a text-matching heuristic, not a real per-claim citation id the model
    itself emits -- it can be fooled by a coincidental cross-document value
    match beyond the boilerplate fields this fix targets. The longer-term
    fix is a model-emitted structured citation id per claim, not a sharper
    heuristic here."""
    doc_citations = [c for c in citations if c.get("source_type") == "uploaded_document"]

    value_sets: dict[str, set[str]] = {}
    for c in doc_citations:
        value_sets.setdefault(c.get("source_id"), set()).add(c.get("quote_or_value", "").strip().lower())

    def _is_distinctive(doc_id: str, value: str) -> bool:
        v = value.strip().lower()
        return not any(v in other_values for other_id, other_values in value_sets.items() if other_id != doc_id)

    # Per document_id: True if this document is "referenced" (has a stated
    # distinctive value) OR has no distinctive value at all (collapsible --
    # the identical-documents tie-break, handled per-citation below via the
    # ordinary stated check rather than being gated on document identity).
    doc_is_referenced_or_collapsible: dict[str, bool] = {}
    for doc_id, values in value_sets.items():
        doc_cites = [c for c in doc_citations if c.get("source_id") == doc_id]
        distinctive_cites = [c for c in doc_cites if _is_distinctive(doc_id, c.get("quote_or_value", ""))]
        if not distinctive_cites:
            doc_is_referenced_or_collapsible[doc_id] = True
            continue
        doc_is_referenced_or_collapsible[doc_id] = any(
            _document_fact_stated_in_response(c.get("quote_or_value", ""), response_text) for c in distinctive_cites
        )

    kept = []
    for c in citations:
        if c.get("source_type") != "uploaded_document":
            kept.append(c)
            continue
        if not doc_is_referenced_or_collapsible.get(c.get("source_id"), False):
            continue
        if _document_fact_stated_in_response(c.get("quote_or_value", ""), response_text):
            kept.append(c)
    return kept


def collapse_duplicate_documents(citations: list[Citation]) -> list[Citation]:
    """One card per distinct document CONTENT. A patient can have several stored
    uploads of the same page (each gets its own document id), and each would
    otherwise contribute its own copy of every field. Documents whose extracted
    content is identical (same document type and the same set of field/value
    pairs) collapse into the first one seen -- the most recent, since
    get_uploaded_documents lists newest first -- and that document's citations
    carry `duplicate_count` = how many copies exist. Anything that differs in
    even one value stays separate; non-document citations are untouched."""
    fields_by_doc: dict[str, list[Citation]] = {}
    for c in citations:
        if c.source_type == "uploaded_document":
            fields_by_doc.setdefault(c.source_id, []).append(c)

    def fingerprint(cites: list[Citation]) -> tuple:
        return (cites[0].doc_label, frozenset((c.field_label, c.quote_or_value) for c in cites))

    representative: dict[tuple, str] = {}
    copies: dict[tuple, int] = {}
    doc_fingerprint: dict[str, tuple] = {}
    for doc_id, cites in fields_by_doc.items():
        fp = fingerprint(cites)
        doc_fingerprint[doc_id] = fp
        representative.setdefault(fp, doc_id)
        copies[fp] = copies.get(fp, 0) + 1

    kept: list[Citation] = []
    for c in citations:
        if c.source_type != "uploaded_document":
            kept.append(c)
            continue
        fp = doc_fingerprint[c.source_id]
        if representative[fp] != c.source_id:
            continue
        if copies[fp] > 1:
            c.duplicate_count = copies[fp]
        kept.append(c)
    return kept


def translate_citations(
    *,
    chart_facts: ChartGatherResult | None,
    extracted_facts: list[ExtractionResult],
    retrieved_evidence: list[RetrievedEvidence],
    patient_id: str | None,
    counts: dict[str, int] | None = None,
) -> list[dict]:
    """The one call site app/graph/supervisor.py's synthesize_node uses --
    combines all three domains into CoPilotState.citations' plain-dict
    shape (PRD Section 3: `citations: list[dict]`).

    Deduplicated by (source_type, source_id, field_label, quote_or_value): the same
    document's same field can legitimately reach this combined list twice
    -- once via chart_citations()'s GetUploadedDocumentsOutput branch
    (get_uploaded_documents, a later-turn read-back) and again via
    document_citations() (a same-turn intake_extractor_node extraction of
    the exact same stored document, e.g. via uploaded_document_ref) --
    each producing its own Citation with a different field_or_chunk_id for
    the identical underlying fact. Deliberately keyed on (source, field
    label, value), not field_or_chunk_id/page_or_section/doc_label: two
    citations that agree on all three are the same fact restated, regardless
    of which path's own labeling differs. The field label is in the key so
    that two different fields sharing a value (two results' units) are never
    merged -- only an identical field+value pair is.

    Documents with identical extracted content are collapsed into one
    (collapse_duplicate_documents). `counts`, if given, is filled with how many
    citations were built and how many documents that collapsed -- numbers only,
    for the per-turn citation_counts log event."""
    citations = (
        chart_citations(chart_facts, patient_id)
        + document_citations(extracted_facts)
        + guideline_citations(retrieved_evidence)
    )
    built = len(citations)
    docs_before = len({c.source_id for c in citations if c.source_type == "uploaded_document"})
    citations = collapse_duplicate_documents(citations)
    if counts is not None:
        counts["built"] = built
        counts["duplicate_documents_collapsed"] = docs_before - len(
            {c.source_id for c in citations if c.source_type == "uploaded_document"}
        )
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict] = []
    for c in citations:
        # field_label is part of the key so two DIFFERENT fields that happen to share a value
        # (result 1's and result 2's unit, both "mmol/L") are both kept; the same field reached
        # by both paths carries the same label, so it still collapses.
        key = (c.source_type, c.source_id, c.field_label or "", c.quote_or_value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c.model_dump())
    return deduped
