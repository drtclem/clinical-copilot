"""OCR-first extraction (PRD Section 4.2): Tesseract, self-hosted, run
against each page image, THEN LLM/VLM structuring on the OCR'd text
(app/ingestion/structuring.py) -- never direct-VLM-on-image. Accepts
PDF/JPEG/PNG only; anything else is rejected explicitly, never guessed at.

In-memory only (PRD Section 4.2's hard rule): every function here takes
raw bytes and returns in-memory structures -- nothing is ever written to
this process' own disk. Uploaded bytes live only for the duration of one
attach_and_extract() call (app/ingestion/pipeline.py).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytesseract
from PIL import Image

_ACCEPTED_CONTENT_TYPES = {
    "application/pdf": "pdf",
    "image/jpeg": "image",
    "image/png": "image",
}


class UnsupportedFileTypeError(Exception):
    pass


class EmptyFileError(Exception):
    pass


# A scanned lab report or intake form is a page or two. Without a cap, a 20 MB PDF can be hundreds of pages, each
# OCR'd on a 2-vCPU host and then fed to the structuring model -- unbounded CPU time and token cost for one upload.
MAX_DOCUMENT_PAGES = 5


class PageLimitError(Exception):
    """A PDF has more than MAX_DOCUMENT_PAGES pages. The message is safe to show the resident."""


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float  # Tesseract's own 0-100 word-level confidence
    # Normalized (0-1) bounding box on this word's own page image (PRD
    # Section 4.2's "Bounding boxes: normalized (0-1 range) coordinates").
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class OcrPage:
    page_number: int  # 1-indexed
    words: list[OcrWord]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def avg_confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.confidence for w in self.words) / len(self.words)


@dataclass(frozen=True)
class OcrResult:
    pages: list[OcrPage]

    @property
    def avg_confidence(self) -> float:
        if not self.pages:
            return 0.0
        return sum(p.avg_confidence for p in self.pages) / len(self.pages)


def _content_type_kind(content_type: str) -> str:
    kind = _ACCEPTED_CONTENT_TYPES.get(content_type.lower())
    if kind is None:
        raise UnsupportedFileTypeError(
            f"Unsupported file type {content_type!r} -- only PDF, JPEG, and PNG are accepted."
        )
    return kind


def _page_images(file_bytes: bytes, content_type: str, *, max_pages: int | None = None) -> list[Image.Image]:
    """max_pages: for INGESTION. Renders at most max_pages + 1 pages -- enough to know the document is over the
    limit without rasterizing all of a huge one -- and raises PageLimitError if it is. None (previews of documents
    already stored) renders every page, as before."""
    kind = _content_type_kind(content_type)
    if kind == "pdf":
        from pdf2image import convert_from_bytes  # local import: optional/heavy dep, only needed for PDFs

        if max_pages is None:
            return convert_from_bytes(file_bytes)
        images = convert_from_bytes(file_bytes, last_page=max_pages + 1)
        if len(images) > max_pages:
            raise PageLimitError(
                f"This PDF has more than {max_pages} pages. Please upload just the pages that matter "
                f"(up to {max_pages}) -- for example, only the page with the lab results."
            )
        return images
    return [Image.open(io.BytesIO(file_bytes)).convert("RGB")]


def _ocr_page(image: Image.Image, page_number: int) -> OcrPage:
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    width, height = image.size
    words: list[OcrWord] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        conf = float(data["conf"][i])
        if conf < 0:  # Tesseract uses -1 for non-word regions (block/paragraph markers)
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        bbox = (x / width, y / height, (x + w) / width, (y + h) / height)
        words.append(OcrWord(text=text, confidence=conf, bbox=bbox))
    return OcrPage(page_number=page_number, words=words)


def run_ocr(file_bytes: bytes, content_type: str) -> OcrResult:
    if not file_bytes:
        raise EmptyFileError("Uploaded file was empty.")
    images = _page_images(file_bytes, content_type, max_pages=MAX_DOCUMENT_PAGES)
    pages = [_ocr_page(image, page_number=i + 1) for i, image in enumerate(images)]
    return OcrResult(pages=pages)


def rasterize_page(file_bytes: bytes, content_type: str, page_number: int) -> bytes:
    """Renders one page as a PNG -- the same page-image mechanism run_ocr()
    uses internally, exposed separately for app/main.py's citation preview
    endpoint (PRD Section 4.8: "Preview image = the same rasterized page
    image the OCR pipeline already produces. No separate client-side PDF
    renderer."). Computed on demand from the stored original, not a second
    persisted copy of every page -- this project stores exactly two
    documents per upload (PRD Section 4.2), not N+2."""
    images = _page_images(file_bytes, content_type)
    if not (1 <= page_number <= len(images)):
        raise ValueError(f"Page {page_number} out of range (document has {len(images)} page(s)).")
    buffer = io.BytesIO()
    images[page_number - 1].save(buffer, format="PNG")
    return buffer.getvalue()
