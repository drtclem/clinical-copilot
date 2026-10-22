"""Structured, severity-tagged notices about how a turn's evidence came out.

A worker that ran fine but found nothing (a guideline search with no relevant
match) is a normal outcome, not a fault. It used to travel through
`partial_failures` -- the channel for real failures -- so the resident saw a
`[Verification note: ...]` bracket, lost the verified check, and got the raw
code "worker_partial_failure" listed as a warning. Notices keep the two apart:

- severity "info": an expected outcome to state plainly (no_guideline_match).
  Never affects verification, enforced_warnings or the verified check.
- real failures (retrieval service unavailable, a crashed worker) keep going
  through `partial_failures` and remain warnings.
"""

from __future__ import annotations

NO_GUIDELINE_MATCH = "no_guideline_match"

_NO_GUIDELINE_TEXT = "I didn't find a guideline in the reference set that covers this, so this answer isn't based on one."


def no_guideline_match_notice() -> dict:
    return {
        "worker": "evidence_retriever",
        "kind": NO_GUIDELINE_MATCH,
        "severity": "info",
        "label": "No matching guideline",
        "text": _NO_GUIDELINE_TEXT,
    }


# Phrases showing the drafted answer already tells the resident no guideline backs it.
_ALREADY_SAID_CUES = (
    "no guideline", "no relevant guideline", "didn't find a guideline", "did not find a guideline",
    "couldn't find a guideline", "not based on a guideline", "isn't based on a guideline", "reference set",
    "without guideline", "guideline evidence", "no retrieved guideline", "no matching guideline",
)


def mentions_no_guideline(text: str) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in _ALREADY_SAID_CUES)
