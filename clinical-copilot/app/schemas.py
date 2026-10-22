"""Pydantic schemas for each tool, per ARCHITECTURE.md 7.3: these are the
source of truth for each tool's input/output shape. A tool implementation that
can't populate one of these validly must raise, not return malformed data --
see tools.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- get_patient_snapshot ----------------------------------------------------


class GetPatientSnapshotInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ConditionFact(BaseModel):
    text: str
    category: Literal["problem-list-item", "health-concern", "unknown"]
    clinical_status: str | None
    source_resource: str  # e.g. "Condition/<fhir-id>", for source-attribution checks


class MedicationFact(BaseModel):
    text: str
    dosage_text: str | None
    status: str | None
    source_resource: str


class AllergyFact(BaseModel):
    text: str
    reaction: str | None
    is_coded: bool = Field(
        ..., description="False if this only survived in narrative text.div (Finding 11)."
    )
    source_resource: str


class DuplicatePatientWarning(BaseModel):
    other_patient_id: str
    matched_on: str  # e.g. "name+birthdate"


class GetPatientSnapshotOutput(BaseModel):
    patient_id: str
    name: str
    birth_date: str | None
    gender: str | None
    conditions: list[ConditionFact]
    medications: list[MedicationFact]
    allergies: list[AllergyFact]
    chart_is_empty: bool
    duplicate_warnings: list[DuplicatePatientWarning]
    partial_failures: list[str] = Field(
        default_factory=list,
        description="Which sub-resource fetches failed this call, e.g. 'AllergyIntolerance: timeout'.",
    )


# --- check_allergy_conflict --------------------------------------------------


class CheckAllergyConflictInput(BaseModel):
    patient_id: str
    medication_name: str = Field(..., description="Medication name to check, as free text.")


class CheckAllergyConflictOutput(BaseModel):
    patient_id: str
    medication_name: str
    conflict_found: bool
    # "conflict": a recorded allergy matched by name or by curated class. "no_conflict": nothing matched and the
    # drug is one the curated table can evaluate (or the patient has no recorded allergies). "not_assessed": the
    # drug is NOT in the curated table and its name matches no recorded allergy, so cross-reactivity could not be
    # evaluated at all -- this is NOT a clearance and must never be reported as safe.
    assessment: Literal["conflict", "no_conflict", "not_assessed"] = "no_conflict"
    not_assessed_reason: str | None = None
    matched_allergy_text: str | None
    cross_reactive_class: str | None = Field(
        default=None,
        description=(
            "Set to the shared drug-class name (e.g. 'penicillins') when the conflict was "
            "found via the curated cross-reactivity table (app/clinical_reference.py) rather "
            "than a direct/substring match against the allergy text itself -- e.g. amoxicillin "
            "flagged against a documented penicillin allergy. None for a direct name match, "
            "and always None when conflict_found is False. That table is a scoped stand-in for "
            "a production drug-interaction database, not general clinical decision support -- "
            "see its module docstring."
        ),
    )
    checked_allergy_count: int
    low_confidence: bool = Field(
        ..., description="True if the match relied on uncoded/narrative-only allergy data."
    )
    source_resources: list[str]


# --- get_recent_encounters (Phase 6, CLAUDE_CODE_BUILD_INSTRUCTIONS.md) -----


class GetRecentEncountersInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class EncounterFact(BaseModel):
    text: str  # encounter type/reason display
    status: str | None
    period_start: str | None
    source_resource: str  # e.g. "Encounter/<fhir-id>"


class GetRecentEncountersOutput(BaseModel):
    patient_id: str
    encounters: list[EncounterFact]
    sensitivity_filtered_count: int = Field(
        ...,
        description=(
            "How many encounters the compensating sensitivity filter (app/sensitivity.py, "
            "ARCHITECTURE.md 3.3) excluded before reaching `encounters` above -- never silently "
            "zero, so a resident/log reader can tell filtering is active even when nothing this "
            "call happened to exclude anything."
        ),
    )
    partial_failures: list[str] = Field(default_factory=list)


# --- get_recent_observations (UC2, USERS.md) --------------------------------
#
# Deliberately NOT run through app/sensitivity.py's compensating filter, per
# explicit build instruction: that filter is specifically for Encounter-type
# data (ARCHITECTURE.md 3.3's own scope is the FHIR Encounter resource, which
# has no sensitivity field at all). Observation carries a different risk
# profile -- it's not the resource type that compensating control exists for
# -- so this tool is not gated behind it. If a future finding shows
# Observation data needs its own compensating control, that's a new,
# separate gap to evaluate on its own terms, not an oversight here.


class GetRecentObservationsInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ObservationFact(BaseModel):
    text: str  # what was measured, e.g. "Blood Pressure", "Potassium"
    value: str | None = Field(
        None, description="Formatted value + unit if present, e.g. '5.2 mEq/L' or '120/80 mmHg'."
    )
    status: str | None
    effective_datetime: str | None
    source_resource: str  # e.g. "Observation/<fhir-id>"


class GetRecentObservationsOutput(BaseModel):
    patient_id: str
    observations: list[ObservationFact]
    partial_failures: list[str] = Field(default_factory=list)


# --- summarize_shift_events (UC4, USERS.md) ---------------------------------
#
# Different in kind from every tool above: no FHIR call, no `fhir` parameter
# (see app/tools.py's docstring for the reasoning, and app/agent.py's
# ClinicalCopilotAgent._call_tool for the dispatch special-case this
# requires). Synthesizes purely from facts already gathered this
# conversation -- never fetches anything new, and therefore never needs its
# own compensating sensitivity filter: whatever app/sensitivity.py already
# excluded from get_recent_encounters' output was never added to the
# ToolCallRecord this tool reads in the first place.


class SummarizeShiftEventsInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ShiftEventFact(BaseModel):
    text: str
    category: Literal["condition", "medication", "allergy", "encounter", "observation", "duplicate_warning"]
    source_resource: str


class SummarizeShiftEventsOutput(BaseModel):
    patient_id: str
    events: list[ShiftEventFact]
    data_gathered: bool = Field(
        ...,
        description=(
            "False if nothing has been fetched for this patient yet this conversation -- "
            "distinct from True with events=[], which means data WAS gathered and there is "
            "genuinely nothing notable to report. Two different honest-failure conditions, "
            "not one collapsed empty case."
        ),
    )


# --- compare_signout_to_chart (UC2, USERS.md) -------------------------------
#
# Deliberately does NOT take the sign-out's own text as input, and does not
# attempt to parse or semantically compare it -- see app/tools.py's docstring
# for the full reasoning. It structurally diffs a fresh, unconditional chart
# fetch against whatever this conversation already knew (turn_records) for
# this patient; the model itself connects that diff to the sign-out claim
# already sitting in its own context (the resident's message), the same
# division of labor every other tool uses. "baseline_established=False" means
# there was nothing this conversation to diff against yet -- this fetch
# becomes the new baseline, not a confirmation that nothing has changed.


class CompareSignoutToChartInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ChartDiscrepancy(BaseModel):
    category: Literal["condition", "medication", "allergy", "encounter", "observation"]
    text: str
    source_resource: str


class CompareSignoutToChartOutput(BaseModel):
    patient_id: str
    baseline_established: bool = Field(
        ...,
        description=(
            "False if this conversation had nothing gathered yet for this patient to diff "
            "against -- this fetch is now the baseline, not confirmation nothing has changed."
        ),
    )
    discrepancies: list[ChartDiscrepancy]
    current_snapshot: GetPatientSnapshotOutput | None = None
    current_encounters: GetRecentEncountersOutput | None = None
    current_observations: GetRecentObservationsOutput | None = None
    partial_failures: list[str] = Field(default_factory=list)


# --- Medical reference layer (app/reference_layer.py) -----------------------
#
# NOT a tool the model calls -- a deterministic post-processing step run
# after verify_response(), reading only the verified final response text
# (never the model's own context, never a superseded draft). Not a
# correctness check: it never blocks, flags, or contradicts the response,
# purely additive informational footnotes, so it adds no coverage to
# verify_response() itself -- documented explicitly, the same honest
# distinction already made for summarize_shift_events' SYSTEM_PROMPT rule.
# A separate structured field (ChatTurnResult.reference_notes), never
# concatenated into response_text, so it's unambiguously distinguishable
# from something the model itself asserted -- a data-level guarantee, not
# a formatting convention.


class ReferenceNote(BaseModel):
    subject_type: Literal["medication", "condition"]
    subject_name: str
    source: Literal["DailyMed", "MedlinePlus"]
    note_text: str | None = Field(
        None, description="None means no reference note is available for this item -- never fabricated or paraphrased from memory."
    )
    source_url: str | None = Field(None, description="Link to the real DailyMed label or MedlinePlus page this note was drawn from.")


# --- get_uploaded_documents (W2 PRD Section 4.2 -- chart-data-worker's 7th
# tool) --------------------------------------------------------------------
#
# Reads previously-stored document extractions (app/ingestion/storage.py's
# list_extracted_documents) -- never re-runs OCR on a later turn. Every
# fact this tool surfaces comes from the "AI-Extracted Pending Review"
# category: unreconciled, patient/document-reported data, never promoted
# into authoritative chart resources (PRD Section 4.2's explicit rule).
# app/verification.py's hard-rule enforcement (_enforce_uploaded_document_
# disclosure) requires any response drawing on this tool's output to
# explicitly flag it as unreconciled/pending review -- not left to the
# system prompt alone.


class GetUploadedDocumentsInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class UploadedDocumentFactSummary(BaseModel):
    field_name: str
    value: str
    confidence: Literal["high", "low", "unreadable"]
    source_resource: str  # e.g. "document:<doc_id>#test_name" -- doc_id is the ORIGINAL
    # source scan's id (app/ingestion/storage.py's category-35 document), the only id
    # GET /documents/preview can ever resolve -- not extracted_doc_id (category 36).
    page_number: int | None = None  # the field's own DocumentCitation.page_number, for
    # a readable citation label and so a later-turn preview request can pass the right page.


class UploadedDocumentSummary(BaseModel):
    doc_id: str
    doc_type: Literal["lab_pdf", "intake_form"]
    facts: list[UploadedDocumentFactSummary]
    ocr_avg_confidence: float
    uploaded_on: str | None = None  # UTC upload date (ExtractionResult.uploaded_on); None for older documents
    upload_rank: int = Field(
        ...,
        description=(
            "Position among this patient's stored documents by upload order: 0 is the MOST "
            "recently uploaded, 1 the next most recent, and so on. Lets 'most recently uploaded' "
            "be answered directly, without guessing from a clinical field like collection_date -- "
            "which is a different fact (when the sample was drawn) that two documents can share "
            "even when uploaded at different times, or differ on even when uploaded together."
        ),
    )


class GetUploadedDocumentsOutput(BaseModel):
    patient_id: str
    documents: list[UploadedDocumentSummary]
    partial_failures: list[str] = Field(default_factory=list)
    # How many uploaded documents exist beyond app/ingestion/storage.py's
    # MAX_UPLOADED_DOCUMENTS_RETURNED cap and were therefore left out of
    # `documents` above. 0 means the patient has no more documents than
    # the cap. app/verification.py enforces that the response discloses
    # this whenever it's nonzero, rather than silently presenting a
    # partial list as if it were complete.
    omitted_count: int = Field(default=0, ge=0)


# --- shared tool-failure envelope (ARCHITECTURE.md Section 2, 4) ------------


class ToolFailure(BaseModel):
    tool: str
    reason: str = Field(..., description="Safe to show a resident directly, no raw exception text.")
    detail_code: Literal[
        "timeout", "http_error", "not_found", "malformed_response", "invalid_input", "patient_mismatch"
    ]
