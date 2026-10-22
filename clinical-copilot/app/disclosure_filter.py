"""Response-boundary filter: never send internal identifiers to the chat user.

Week 3 adversarial testing (VULN-103, VULN-105) showed the model naming its
internal tools, worker nodes, tool parameters, the allergy tool's return values
and error codes to the resident, on plain conversational requests as well as in
error text. That is a map for targeting the allergy safety gate. The system
prompt now forbids it, but a prompt rule is probabilistic, so this deterministic
pass at the /chat boundary is the guarantee: every internal identifier is
replaced with plain clinical language before the response leaves the service.

Scope, stated plainly:
  * It runs on the response TEXT only, after verification (so grounding and
    claim-stripping are unaffected) and only at the HTTP boundary (evals that
    call the graph directly see the unfiltered text).
  * It does not touch the structured `tool_calls` / `enforced_warnings` fields
    of ChatResponse. Those are a deliberate transparency surface for the
    authenticated clinician UI ("what I looked up"), documented as such.
  * Matching is on whole snake_case identifiers, so ordinary English ("no
    conflict", "a patient snapshot") is never altered.
"""

from __future__ import annotations

import re

# identifier -> plain-language replacement. Ordered longest-first at compile time.
INTERNAL_IDENTIFIERS: dict[str, str] = {
    # tools
    "get_patient_snapshot": "the patient snapshot",
    "check_allergy_conflict": "the allergy check",
    "get_recent_encounters": "the recent-encounters lookup",
    "get_recent_observations": "the recent-observations lookup",
    "summarize_shift_events": "the shift summary",
    "compare_signout_to_chart": "the sign-out check",
    "get_uploaded_documents": "the uploaded-documents lookup",
    # worker nodes
    "chart_data_worker": "chart lookup",
    "evidence_retriever": "guideline lookup",
    "intake_extractor": "document intake",
    # tool parameters
    "medication_name": "medication name",
    "patient_id": "patient ID",
    # allergy-check return values
    "no_conflict": "no recorded conflict",
    "not_assessed": "could not be checked",
    # error / status codes
    "worker_partial_failure": "a partial retrieval failure",
    "http_error": "a retrieval error",
    "malformed_response": "an unreadable response",
}

_IDS = sorted(INTERNAL_IDENTIFIERS, key=len, reverse=True)
# Optional surrounding backticks or straight/curly quotes are consumed with the
# identifier so `not_assessed` / "not_assessed" become plain text, not `could not be checked`.
_PATTERN = re.compile(
    r"""([`"'“‘]?)(?<![A-Za-z0-9_])(""" + "|".join(map(re.escape, _IDS)) + r""")(?![A-Za-z0-9_])\1""",
)


def scrub_internal_identifiers(text: str | None) -> str:
    """Replace every internal identifier in `text` with its plain-language form."""
    if not text:
        return text or ""
    return _PATTERN.sub(lambda m: INTERNAL_IDENTIFIERS[m.group(2)], text)


def find_internal_identifiers(text: str | None) -> list[str]:
    """Identifiers still present in `text` (empty after a scrub)."""
    return sorted({m.group(2) for m in _PATTERN.finditer(text or "")})
