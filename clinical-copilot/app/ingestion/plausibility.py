"""The numeric-value safety check PRD Section 4.2 requires as non-optional:
"OCR confidence alone does NOT reliably catch digit-transposition errors
(confirmed: a creatinine value '1.1' was misread as '141' at 74%
confidence -- clinically the difference between normal and severe renal
impairment). Every numeric lab field gets a second, deterministic
plausibility check against its own required reference-range field -- if
the extracted value falls implausibly far outside that range, force it to
`unreadable` tier regardless of OCR/VLM-reported confidence."

Deliberately a blunt, order-of-magnitude gate, not a clinical range
validator: this catches "141 where the range tops out at 1.2" (two orders
of magnitude), not "1.4 where the range tops out at 1.2" (a genuine,
legitimate high value) -- the latter is exactly the kind of true abnormal
result this system must not suppress or miscategorize.
"""

from __future__ import annotations

import re

from app.ingestion.schemas import ConfidenceTier

_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[-–—to]+\s*(-?\d+(?:\.\d+)?)")
_UPPER_BOUND_RE = re.compile(r"[<≤]\s*(-?\d+(?:\.\d+)?)")
_LOWER_BOUND_RE = re.compile(r"[>≥]\s*(-?\d+(?:\.\d+)?)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# How far outside the reference range (as a multiple of the bound itself)
# still counts as "plausible" -- a genuinely severe abnormal result can
# reasonably run several multiples past its reference range (e.g. a
# critical potassium, a markedly elevated WBC); a DIGIT-TRANSPOSITION error
# routinely produces a value one to two orders of magnitude off (the PRD's
# own 1.1 -> 141 example is ~118x the upper bound). 10x is chosen to sit
# clearly above real severe-abnormal territory and clearly below a
# transposition error, not tuned against a labeled dataset -- a real
# deployment would calibrate this against actual flagged cases over time.
_IMPLAUSIBILITY_MULTIPLE = 10.0


def _parse_bounds(reference_range: str) -> tuple[float | None, float | None]:
    """Best-effort (lower, upper) from common reference-range phrasings:
    "0.6-1.2", "0.6 - 1.2 mg/dL", "<200", ">40". Either side is None when
    unparseable/unbounded -- the check below only applies on a side it
    could actually parse, never fabricating a bound."""
    range_match = _RANGE_RE.search(reference_range)
    if range_match:
        lo, hi = float(range_match.group(1)), float(range_match.group(2))
        return (lo, hi) if lo <= hi else (hi, lo)
    upper_match = _UPPER_BOUND_RE.search(reference_range)
    if upper_match:
        return None, float(upper_match.group(1))
    lower_match = _LOWER_BOUND_RE.search(reference_range)
    if lower_match:
        return float(lower_match.group(1)), None
    return None, None


def _parse_value(value: str) -> float | None:
    match = _NUMBER_RE.search(value)
    return float(match.group(0)) if match else None


def is_implausible(value: str, reference_range: str) -> bool:
    """True if `value` falls more than _IMPLAUSIBILITY_MULTIPLE times past
    either bound of `reference_range`. False (never implausible) when
    either side can't be parsed -- this check only ever ADDS a downgrade
    to `unreadable`, never silently passes a field through by assuming
    plausibility it can't actually verify (a genuinely unparseable
    reference_range is its own confidence problem, handled by the OCR/LLM
    confidence tier directly, not by this function)."""
    parsed_value = _parse_value(value)
    if parsed_value is None:
        return False
    lower, upper = _parse_bounds(reference_range)
    if upper is not None and parsed_value > 0 and upper > 0 and parsed_value > upper * _IMPLAUSIBILITY_MULTIPLE:
        return True
    if lower is not None and parsed_value < 0 and lower < 0 and parsed_value < lower * _IMPLAUSIBILITY_MULTIPLE:
        return True
    if lower is not None and lower > 0 and parsed_value < lower / _IMPLAUSIBILITY_MULTIPLE:
        return True
    return False


def apply_plausibility_check(value: str, reference_range: str, reported_confidence: ConfidenceTier) -> ConfidenceTier:
    """The actual enforcement point: downgrades to `unreadable` regardless
    of what OCR/VLM reported, per PRD Section 4.2's "regardless of
    OCR/VLM-reported confidence." Never upgrades -- a plausible value keeps
    whatever confidence tier the OCR/structuring step already assigned it."""
    if is_implausible(value, reference_range):
        return "unreadable"
    return reported_confidence
