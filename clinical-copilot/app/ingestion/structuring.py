"""LLM structuring on OCR'd text (PRD Section 4.2: "OCR-first..., then
LLM/VLM structuring on the OCR'd text. Not direct-VLM-on-image."). Calls
app/llm_client.py's LLMClient directly, same as every other new node/module
this phase (PRD Section 4.1) -- never a provider SDK, never a framework
model wrapper.

Per-field citations (PRD Section 4.2: "per-field, not per-document") are
resolved in two steps: the LLM is asked only for the field's value and
which OCR'd page it came from (page_number) -- it never sees or invents
bounding boxes -- then app/ingestion/bbox.py resolves the actual normalized
bbox by matching the extracted value's text back against that page's own
OCR word spans. A citation whose bbox can't be resolved this way still
gets a citation (page_number + the literal quote), just with bbox=None,
never a fabricated box.
"""

from __future__ import annotations

import json
from typing import Any

from app.ingestion.bbox import resolve_bbox
from app.ingestion.ocr import OcrResult
from app.ingestion.schemas import (
    DemographicsFact,
    DocumentCitation,
    DocType,
    ExtractedField,
    IntakeFormExtraction,
    LabPdfExtraction,
    LabResultFact,
)
from app.llm_client import THINKING_DISABLED, LLMClient, llm_call_meta, raise_if_truncated
from app.observability import TurnTrace, log_event

_STRUCTURING_SYSTEM_PROMPT = """\
You extract structured facts from OCR'd text of a scanned clinical \
document. You are given the raw OCR output, page by page -- it may contain \
OCR errors (misread characters, garbled spacing). Extract ONLY what is \
literally present in the text; never infer, compute, or guess a value that \
isn't there. For each field you extract, report: the value as a short \
literal string, the page number it came from, the exact literal snippet \
of OCR'd text you drew it from (quote), and your own confidence that the \
OCR'd text was read correctly and unambiguously as this value -- "high" \
if you're confident, "low" if the OCR text is ambiguous/partially garbled \
but you can make a reasonable reading, "unreadable" if the relevant text \
is too garbled to extract with any confidence. If a required field is \
genuinely not present anywhere in the document, omit it entirely -- never \
fabricate a placeholder value.
"""

# Shared by every extracted field, in both tool schemas below: a literal
# value, which OCR'd page it came from, the literal quote it was drawn
# from, and the model's own read-confidence -- see this module's docstring
# for why bbox is resolved separately (app/ingestion/bbox.py) rather than
# asked of the model directly.
_EXTRACTED_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "page_number": {"type": "integer"},
        "quote": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "low", "unreadable"]},
    },
    "required": ["value", "page_number", "quote", "confidence"],
}

_LAB_TOOL = {
    "name": "extract_lab_results",
    "description": "Structured lab results extracted from the OCR'd text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "test_name": _EXTRACTED_FIELD_SCHEMA,
                        "value": _EXTRACTED_FIELD_SCHEMA,
                        "unit": _EXTRACTED_FIELD_SCHEMA,
                        "reference_range": _EXTRACTED_FIELD_SCHEMA,
                        "collection_date": _EXTRACTED_FIELD_SCHEMA,
                        "abnormal_flag": _EXTRACTED_FIELD_SCHEMA,
                    },
                    "required": ["test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag"],
                },
            }
        },
        "required": ["results"],
    },
}

_INTAKE_FIELD_SCHEMA = _EXTRACTED_FIELD_SCHEMA

_INTAKE_TOOL = {
    "name": "extract_intake_form",
    "description": "Structured intake-form facts extracted from the OCR'd text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "demographics": {
                "type": "object",
                "properties": {
                    "name": _INTAKE_FIELD_SCHEMA,
                    "date_of_birth": _INTAKE_FIELD_SCHEMA,
                    "sex": _INTAKE_FIELD_SCHEMA,
                },
            },
            "chief_concern": _INTAKE_FIELD_SCHEMA,
            "current_medications": {"type": "array", "items": _INTAKE_FIELD_SCHEMA},
            "allergies": {"type": "array", "items": _INTAKE_FIELD_SCHEMA},
            "family_history": {"type": "array", "items": _INTAKE_FIELD_SCHEMA},
        },
        "required": ["demographics"],
    },
}


def _ocr_text_by_page(ocr_result: OcrResult) -> str:
    return "\n\n".join(f"--- Page {p.page_number} ---\n{p.text}" for p in ocr_result.pages)


def _to_extracted_field(raw: dict[str, Any], ocr_result: OcrResult) -> ExtractedField:
    page_number = int(raw["page_number"])
    quote = raw["quote"]
    bbox = resolve_bbox(ocr_result, page_number, quote)
    return ExtractedField(
        value=raw["value"],
        confidence=raw["confidence"],
        citation=DocumentCitation(page_number=page_number, bbox=bbox, quote_or_value=quote),
    )


_LAB_RESULT_KEYS = ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag")


def _known_lab_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """The schemas forbid unknown keys (`extra="forbid"`), so an extra key the model
    adds to a result row (e.g. "notes") is dropped HERE, at the boundary, and counted
    in the log, instead of crashing the whole extraction. Only the count is logged,
    not the key names or values."""
    known = {k: v for k, v in raw.items() if k in _LAB_RESULT_KEYS}
    dropped = len(raw) - len(known)
    if dropped:
        log_event("extraction_extra_keys_dropped", dropped_key_count=dropped)
    return known


# Thinking is disabled for this call (THINKING_DISABLED), so the whole budget
# is the tool call's JSON. 2048 was tight for a many-row lab PDF or a full
# intake form; hitting this limit anyway raises TruncatedOutputError.
_STRUCTURING_MAX_TOKENS = 4096


def structure_document(
    ocr_result: OcrResult, doc_type: DocType, *, llm: LLMClient, model: str, trace: TurnTrace
) -> tuple[LabPdfExtraction | IntakeFormExtraction, int, int]:
    """Returns (extraction, input_tokens, output_tokens)."""
    tool = _LAB_TOOL if doc_type == "lab_pdf" else _INTAKE_TOOL
    tool_name = tool["name"]
    messages = [{"role": "user", "content": _ocr_text_by_page(ocr_result)}]

    llm_handle = trace.start_llm_call(
        round_index=0, model=model, input_payload={"system": _STRUCTURING_SYSTEM_PROMPT, "doc_type": doc_type}
    )
    response = llm.create_message(
        system=_STRUCTURING_SYSTEM_PROMPT,
        tools=[tool],
        messages=messages,
        max_tokens=_STRUCTURING_MAX_TOKENS,
        tool_choice={"type": "tool", "name": tool_name},
        thinking=THINKING_DISABLED,
    )
    trace.finish_llm_call(
        llm_handle,
        output_payload={"tool_calls": [tc.input for tc in response.tool_calls]},
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        **llm_call_meta(response),
    )
    # A cut-off tool call can still parse into a schema-valid extraction that
    # is silently missing every field after the cut (allergies, family
    # history, later lab rows). Never return that as if it were complete.
    raise_if_truncated(response, "document structuring")

    call = next((c for c in response.tool_calls if c.name == tool_name), None)
    if call is None:
        raise ValueError(f"Structuring model did not call {tool_name}.")

    if doc_type == "lab_pdf":
        results = [
            LabResultFact(**{k: _to_extracted_field(v, ocr_result) for k, v in _known_lab_keys(raw).items()})
            for raw in call.input.get("results", [])
        ]
        return LabPdfExtraction(results=results), response.input_tokens, response.output_tokens

    raw = call.input
    demographics_raw = raw.get("demographics", {})
    demographics = DemographicsFact(
        **{
            key: _to_extracted_field(demographics_raw[key], ocr_result)
            for key in ("name", "date_of_birth", "sex")
            if key in demographics_raw
        }
    )
    extraction = IntakeFormExtraction(
        demographics=demographics,
        chief_concern=_to_extracted_field(raw["chief_concern"], ocr_result) if raw.get("chief_concern") else None,
        current_medications=[_to_extracted_field(f, ocr_result) for f in raw.get("current_medications", [])],
        allergies=[_to_extracted_field(f, ocr_result) for f in raw.get("allergies", [])],
        family_history=[_to_extracted_field(f, ocr_result) for f in raw.get("family_history", [])],
    )
    return extraction, response.input_tokens, response.output_tokens
