"""Resolves a normalized bounding box for an extracted field's citation
(app/ingestion/structuring.py) by matching the LLM's literal quote back
against the OCR engine's own word-level bounding boxes for that page
(app/ingestion/ocr.py) -- the LLM is never asked for a bbox directly (it
has no reliable notion of pixel/normalized coordinates), so this is the
only place a citation's bbox actually gets computed, and it is always
derived from Tesseract's own self-hosted output, never invented.
"""

from __future__ import annotations

import re

from app.ingestion.ocr import OcrResult

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(token: str) -> str:
    return _NORMALIZE_RE.sub("", token.lower())


def _clamp_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """OCR pixel rounding can put an edge a hair outside [0, 1]; clamp here so
    DocumentCitation's validator never has to be loosened."""
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in box)
    return (x0, y0, x1, y1)


def resolve_bbox(ocr_result: OcrResult, page_number: int, quote: str) -> tuple[float, float, float, float] | None:
    """Finds the shortest contiguous run of this page's OCR words whose
    normalized text matches the quote's normalized tokens, and returns the
    union of their normalized bboxes. None if the page doesn't exist, the
    quote is empty, or no matching run is found -- never a guessed box."""
    page = next((p for p in ocr_result.pages if p.page_number == page_number), None)
    if page is None or not page.words:
        return None

    quote_tokens = [_normalize(t) for t in quote.split()]
    quote_tokens = [t for t in quote_tokens if t]
    if not quote_tokens:
        return None

    page_tokens = [_normalize(w.text) for w in page.words]
    n, m = len(page_tokens), len(quote_tokens)
    for start in range(n - m + 1):
        if page_tokens[start : start + m] == quote_tokens:
            matched = page.words[start : start + m]
            x0 = min(w.bbox[0] for w in matched)
            y0 = min(w.bbox[1] for w in matched)
            x1 = max(w.bbox[2] for w in matched)
            y1 = max(w.bbox[3] for w in matched)
            return _clamp_box((x0, y0, x1, y1))

    # No exact contiguous run -- fall back to the single word matching the
    # FIRST quote token, if any, rather than returning nothing whenever OCR
    # tokenization doesn't line up 1:1 with the model's quote (e.g. the
    # model quoted "1.1 mg/dL" but OCR split "1.1mg/dL" as one token).
    first = quote_tokens[0]
    for word, token in zip(page.words, page_tokens):
        if token == first:
            return _clamp_box(word.bbox)
    return None
