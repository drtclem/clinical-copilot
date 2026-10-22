"""Document ingestion schemas (PRD Section 4.2, Core Requirement 2 -- field
lists are verbatim from the PDF, not paraphrased).

Every extracted value is an ExtractedField, never a bare str/float: a
citation and a confidence tier travel with the value itself, not
bolted on separately, so a caller can never accidentally use a value while
dropping its provenance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocType = Literal["lab_pdf", "intake_form"]

# Tiered enum, not a raw numeric score (PRD Section 4.2's explicit
# requirement) -- `unreadable` is reachable two ways: low OCR/VLM-reported
# confidence, OR app/ingestion/plausibility.py's deterministic
# numeric-plausibility check overriding a HIGH-confidence OCR read that is
# still implausible against its own reference range (the "1.1 misread as
# 141" finding -- OCR confidence alone does not catch digit transposition).
ConfidenceTier = Literal["high", "low", "unreadable"]

# Every ingestion schema forbids unknown keys (PRD Core Requirement 2: strict
# schemas). Pydantic's default silently drops an extra key, which would let a
# malformed or hallucinated field vanish without a trace; with "forbid" it
# raises instead. Deliberately NOT strict=True: that would reject the JSON list
# that stands in for the bbox tuple when a stored extraction is read back with
# model_validate().
_STRICT = ConfigDict(extra="forbid")


class DocumentCitation(BaseModel):
    """Per-field, not per-document (PRD Section 4.2) -- every individual
    extracted value carries its own citation. `bbox` is normalized (0-1
    range) coordinates on the page image at `page_number`, self-hosted OCR
    only -- the same image app/graph's click-to-source UI (Checklist item
    7) highlights. None when a bounding box genuinely could not be
    resolved (e.g. a value inferred/computed rather than lifted verbatim
    from one OCR'd word span) -- never a fabricated/guessed box."""

    model_config = _STRICT

    page_number: int = Field(..., ge=1)
    bbox: tuple[float, float, float, float] | None = Field(
        None, description="(x0, y0, x1, y1), each in [0, 1] -- normalized to the page image's own width/height."
    )
    quote_or_value: str = Field(..., description="The literal OCR'd text this field was extracted from.")

    @field_validator("bbox")
    @classmethod
    def _bbox_is_a_normalized_box(
        cls, bbox: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        if not all(0.0 <= v <= 1.0 for v in bbox):
            raise ValueError("bbox values must each be in [0, 1] (normalized to the page image)")
        if x0 > x1 or y0 > y1:
            raise ValueError("bbox must satisfy x0 <= x1 and y0 <= y1")
        return bbox


class ExtractedField(BaseModel):
    model_config = _STRICT

    value: str
    confidence: ConfidenceTier
    citation: DocumentCitation


# --- lab_pdf ------------------------------------------------------------
#
# Required fields verbatim from PRD Section 4.2 / the PDF's Core
# Requirement 2: "test name, value, unit, reference range, collection
# date, abnormal flag, source citation (at minimum)."


class LabResultFact(BaseModel):
    model_config = _STRICT

    test_name: ExtractedField
    value: ExtractedField
    unit: ExtractedField
    reference_range: ExtractedField
    collection_date: ExtractedField
    abnormal_flag: ExtractedField  # "H" / "L" / "normal" / "critical" / etc., as printed on the source


class LabPdfExtraction(BaseModel):
    model_config = _STRICT

    doc_type: Literal["lab_pdf"] = "lab_pdf"
    results: list[LabResultFact]


# --- intake_form ----------------------------------------------------------
#
# Required fields verbatim from PRD Section 4.2: "demographics fields,
# chief concern, current medications, allergies, family history, source
# citation (at minimum)."


class DemographicsFact(BaseModel):
    model_config = _STRICT

    name: ExtractedField | None = None
    date_of_birth: ExtractedField | None = None
    sex: ExtractedField | None = None
    # Extend only with fields the corpus of real intake forms actually
    # contains -- an empty/None field here means "not present on this
    # form," never a fabricated placeholder.


class IntakeFormExtraction(BaseModel):
    model_config = _STRICT

    doc_type: Literal["intake_form"] = "intake_form"
    demographics: DemographicsFact
    chief_concern: ExtractedField | None = None
    current_medications: list[ExtractedField] = Field(default_factory=list)
    allergies: list[ExtractedField] = Field(default_factory=list)
    family_history: list[ExtractedField] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """The structured shape stored as the "AI-Extracted Pending Review"
    document's own JSON content (app/ingestion/storage.py) -- also what
    app/ingestion/pipeline.py returns from attach_and_extract() and what
    app/graph/workers.py's intake_extractor_node surfaces as
    CoPilotState.extracted_facts."""

    model_config = _STRICT

    patient_id: str
    doc_id: str  # OpenEMR document id of the ORIGINAL scanned source (category 35)
    extracted_doc_id: str | None = None  # OpenEMR document id of THIS structured-JSON document (category 36) -- None until stored
    doc_type: DocType
    extraction: LabPdfExtraction | IntakeFormExtraction
    ocr_avg_confidence: float = Field(..., description="Raw OCR engine confidence, 0-100 -- diagnostic only, never itself a per-field confidence tier.")
    # UTC date (YYYY-MM-DD) this document was uploaded, stamped by
    # attach_and_extract and stored with the JSON, so a later turn can label
    # a citation card with it. None for documents stored before this field
    # existed -- the UI simply omits the date then.
    uploaded_on: str | None = None


class DocumentIngestionFailure(BaseModel):
    """Structured failure envelope, matching app/schemas.py's ToolFailure
    convention (ARCHITECTURE.md Section 2/4's hard rule: a failure is
    always a structured value the caller must surface, never a silent
    swallow)."""

    reason: str = Field(..., description="Safe to show a resident directly, no raw exception text.")
    detail_code: Literal[
        "invalid_file_type", "empty_file", "too_many_pages", "ocr_failed", "structuring_failed", "structuring_truncated",
        "storage_failed", "duplicate"
    ]
