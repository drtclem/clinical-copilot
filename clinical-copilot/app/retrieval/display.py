"""Display-only text helpers for guideline citations: turning a raw retrieved
chunk (markdown, cut at a fixed character offset, so it can begin or end in the
middle of a word) into text safe and pleasant to show a resident. Nothing here
feeds retrieval, reranking or grounding -- those keep using the raw chunk text.
"""

from __future__ import annotations

import re

EXCERPT_MAX_CHARS = 180

_HEADING_MARK_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+.*$\n?", re.MULTILINE)
_HEADING_TEXT_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])", re.DOTALL)
_CODE_RE = re.compile(r"`+([^`]*)`+")
_WS_RE = re.compile(r"\s+")


def _trim_partial_words(text: str, starts_mid_word: bool, ends_mid_word: bool) -> str:
    text = text.strip()
    if starts_mid_word:
        text = re.sub(r"^\S+\s*", "", text, count=1)
    if ends_mid_word:
        text = re.sub(r"\s*\S+$", "", text, count=1)
    return text.strip()


_LIST_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_ABBREVIATIONS = {"e.g", "i.e", "vs", "approx", "dr", "mr", "mrs", "ms", "st", "no", "fig", "cf", "etc"}
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]*]*[ \t]+(?=[A-Z0-9*(\[])")
_MIN_KEPT_CHARS = 40


def _unwrap(text: str) -> str:
    """Corpus files are hard-wrapped at ~75 columns; join a paragraph's wrapped
    lines back into one line (bullet items stay one per line)."""
    out: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        current: list[str] = []
        for line in paragraph.splitlines():
            if _LIST_LINE_RE.match(line):
                if current:
                    out.append(" ".join(current))
                current = [line.strip()]
            elif line.strip():
                current.append(line.strip())
        if current:
            out.append(" ".join(current))
        out.append("")
    return "\n".join(out).strip("\n")


def _sentence_starts(text: str) -> list[int]:
    """Offsets at which a new sentence, paragraph or list item begins (never 0).
    "e.g. Albuterol" and "5.1-5.9" are not boundaries."""
    starts: set[int] = set()
    for m in _SENTENCE_END_RE.finditer(text):
        word = re.split(r"\s", text[: m.start() + 1])[-1].lower().lstrip("(['\"*_").rstrip(".!?")
        if text[m.start()] == "." and (word in _ABBREVIATIONS or word.isdigit()):  # "e.g.", list numbers "2."
            continue
        starts.add(m.end())
    for m in re.finditer(r"\n+", text):
        starts.add(m.end())
    return sorted(s for s in starts if 0 < s < len(text))


def _fit_ends(body: str, starts_mid_sentence: bool, ends_mid_sentence: bool) -> tuple[str, bool, bool]:
    """Trim a partial sentence off either end when a sentence boundary is near
    enough that most of the text survives. Returns (body, cut_at_start, cut_at_end);
    a side is reported as cut whenever the passage begins/ends inside the source
    sentence, whether or not a boundary was found to trim to."""
    starts = _sentence_starts(body)
    if starts_mid_sentence:
        for b in starts:
            if b <= len(body) // 2 and len(body) - b >= _MIN_KEPT_CHARS:
                body = body[b:].lstrip()
                break
        starts = _sentence_starts(body)
    if ends_mid_sentence:
        for b in reversed(starts):
            if b >= len(body) // 2 and b >= _MIN_KEPT_CHARS:
                body = body[:b].rstrip()
                break
    return body, starts_mid_sentence, ends_mid_sentence


def _prepare(text: str, starts_mid_word: bool, ends_mid_word: bool, starts_mid_sentence: bool, ends_mid_sentence: bool):
    trimmed = _trim_partial_words(text, starts_mid_word, ends_mid_word)
    return trimmed, starts_mid_word or starts_mid_sentence, ends_mid_word or ends_mid_sentence


def clean_passage(
    text: str,
    *,
    starts_mid_word: bool = False,
    ends_mid_word: bool = False,
    starts_mid_sentence: bool = False,
    ends_mid_sentence: bool = False,
) -> str:
    """The passage as shown in the modal: a word cut in half at either end
    removed, a partial sentence trimmed off where a sentence boundary is close,
    "## " heading lines turned into bold, wrapped lines joined. A leading/trailing
    "\u2026" marks an end where the passage continues in the guideline. Bullets and
    bold are kept: the UI renders them through its escape-first markdown subset."""
    trimmed, cut_start, cut_end = _prepare(text, starts_mid_word, ends_mid_word, starts_mid_sentence, ends_mid_sentence)
    body = _unwrap(_HEADING_TEXT_RE.sub(lambda m: "**" + m.group(1).strip("*_ ") + "**", trimmed)).strip()
    body, cut_start, cut_end = _fit_ends(body, cut_start, cut_end)
    return _with_ellipses(body, cut_start, cut_end)


def _with_ellipses(body: str, cut_start: bool, cut_end: bool) -> str:
    """"…" before/after the text where it continues in the source; after a full stop it
    is set off by a space (". …") so it reads as "more follows", not a typo."""
    tail = ""
    if cut_end:
        tail = (" \u2026" if body.rstrip()[-1:] in ".!?" else "\u2026")
    return ("\u2026" if cut_start else "") + body + tail


def strip_markdown(text: str) -> str:
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _RULE_RE.sub(" ", text)
    text = _HEADING_MARK_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub(lambda m: m.group(2) or m.group(3), text)
    text = text.replace("|", " ")
    return _WS_RE.sub(" ", text).strip()


def excerpt(
    text: str,
    *,
    starts_mid_word: bool = False,
    ends_mid_word: bool = False,
    starts_mid_sentence: bool = False,
    ends_mid_sentence: bool = False,
    limit: int = EXCERPT_MAX_CHARS,
) -> str:
    """Plain-text preview: markdown removed, no word cut at either end, trimmed to
    sentence boundaries where possible (else a word boundary), with "\u2026" wherever
    the preview stops short of the source."""
    trimmed, cut_start, cut_end = _prepare(text, starts_mid_word, ends_mid_word, starts_mid_sentence, ends_mid_sentence)
    # Heading lines are the card title / section label already; a preview is prose.
    plain = strip_markdown(_HEADING_LINE_RE.sub("", trimmed))
    plain, cut_start, cut_end = _fit_ends(plain, cut_start, cut_end)
    if len(plain) > limit:
        cut_end = True
        cut = plain[:limit]
        boundaries = [b for b in _sentence_starts(plain) if _MIN_KEPT_CHARS <= b <= limit]
        if boundaries:
            plain = plain[: boundaries[-1]].rstrip()
        else:
            if not plain[limit].isspace():
                cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
            plain = cut.rstrip(" ,;:-\u2013\u2014")
    return _with_ellipses(plain, cut_start, cut_end)
