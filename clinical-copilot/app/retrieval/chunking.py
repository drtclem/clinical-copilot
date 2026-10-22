"""Simple fixed-size chunking with modest overlap over the guideline corpus
(app/retrieval/corpus/*.md) -- PRD Section 4.3: "Chunking: simple
fixed-size, modest overlap. No semantic/section-aware chunking this
phase" (Section 6 lists semantic/section-aware chunking as an explicit
non-goal, a named stretch goal for later).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CORPUS_DIR = Path(__file__).parent / "corpus"
# Chosen to comfortably hold one coherent paragraph or two per chunk for
# this corpus' prose style, without being so large that RRF/rerank operate
# on whole-document-sized blocks -- not benchmarked against a larger corpus
# (this one is four short files, PRD Section 4.3), a real tuning pass is
# future work if the corpus grows materially past its current curated size.
_CHUNK_SIZE_CHARS = 800
_CHUNK_OVERLAP_CHARS = 150

_HEADING_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class GuidelineChunk:
    source_id: str  # corpus filename stem, e.g. "hyperkalemia_management" -- Citation.source_id
    chunk_id: str  # f"{source_id}:{index}" -- Citation.field_or_chunk_id
    text: str
    page_or_section: str  # nearest preceding "## " heading, best-effort, for Citation.page_or_section
    # Display-only metadata (never embedded, reranked, or used for grounding, so adding it
    # changes no retrieval behaviour): the file's "# " title, and whether fixed-size chunking
    # cut this chunk's first/last word in half (so the UI can drop the fragment).
    doc_title: str = ""
    starts_mid_word: bool = False
    ends_mid_word: bool = False
    # Whether the fixed-size cut fell inside a sentence (not at a sentence end, paragraph break
    # or heading), so the UI can trim to sentence boundaries and mark the cut with an ellipsis.
    starts_mid_sentence: bool = False
    ends_mid_sentence: bool = False


def _chunk_spans(length: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Character-offset (start, end) spans, fixed-size with overlap.
    Returns spans rather than substrings so the caller can also resolve
    the nearest heading for `page_or_section` from the same offsets,
    against the original (unstripped) text."""
    spans: list[tuple[int, int]] = []
    if length == 0:
        return spans
    start = 0
    while start < length:
        end = min(start + size, length)
        spans.append((start, end))
        if end == length:
            break
        start = end - overlap
    return spans


def _nearest_heading(text: str, offset: int) -> str:
    """Best-effort section label for a chunk -- the nearest '## ' heading
    at or before `offset`, purely for human-readable citations
    (page_or_section). NOT used to decide chunk boundaries: chunking stays
    fixed-size/heading-blind per this module's own docstring."""
    headings = _HEADING_RE.findall(text[:offset])
    return headings[-1].strip() if headings else "(preamble)"


def _starts_mid_sentence(text: str, start: int) -> bool:
    """True if a chunk beginning at `start` begins inside a sentence: not the top
    of the file, not right after a sentence end, and not after a paragraph break
    (blank line). Hard-wrapped single newlines do not count as boundaries."""
    if start <= 0:
        return False
    before = text[:start]
    stripped = before.rstrip()
    if not stripped:
        return False
    gap = before[len(stripped):]
    if "\n\n" in gap or stripped[-1] in ".!?:":
        return False
    return True


def _ends_mid_sentence(text: str, end: int) -> bool:
    """Mirror of _starts_mid_sentence for the chunk's end."""
    if end >= len(text):
        return False
    upto = text[:end].rstrip()
    if not upto or upto[-1] in ".!?:":
        return False
    if "\n\n" in text[len(upto):end + 1] or text[end:].startswith("\n\n"):
        return False
    last_line = upto.rsplit("\n", 1)[-1].lstrip()
    if last_line.startswith("#"):  # ends on a heading line
        return False
    return True


def load_corpus(corpus_dir: Path = _CORPUS_DIR) -> list[GuidelineChunk]:
    """Every `.md` file in `corpus_dir` except README.md (that file is
    documentation about the corpus, not corpus content itself)."""
    chunks: list[GuidelineChunk] = []
    for path in sorted(corpus_dir.glob("*.md")):
        if path.stem.upper() == "README":
            continue
        text = path.read_text(encoding="utf-8")
        title_match = _TITLE_RE.search(text)
        doc_title = title_match.group(1).strip() if title_match else path.stem.replace("_", " ").capitalize()
        for index, (start, end) in enumerate(_chunk_spans(len(text), _CHUNK_SIZE_CHARS, _CHUNK_OVERLAP_CHARS)):
            chunk_text = text[start:end].strip()
            if not chunk_text:
                continue
            chunks.append(
                GuidelineChunk(
                    source_id=path.stem,
                    chunk_id=f"{path.stem}:{index}",
                    text=chunk_text,
                    page_or_section=_nearest_heading(text, start),
                    doc_title=doc_title,
                    starts_mid_word=start > 0 and not text[start - 1].isspace() and not text[start].isspace(),
                    ends_mid_word=end < len(text) and not text[end - 1].isspace() and not text[end].isspace(),
                    starts_mid_sentence=_starts_mid_sentence(text, start + (len(text[start:end]) - len(text[start:end].lstrip()))),
                    ends_mid_sentence=_ends_mid_sentence(text, end),
                )
            )
    return chunks
