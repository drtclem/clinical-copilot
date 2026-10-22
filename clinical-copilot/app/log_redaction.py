"""PHI-safe logging: what may reach journald (and feedback.jsonl), and what may not.

The PRD's rule is "logs must not contain raw PHI". Identifiers, free text and clinical
vocabulary are therefore never written to a log line as-is:

- `patient_id` and `resident_username` (and `other_patient_id`) are replaced by a salted HMAC
  ("h_<12 hex>"): still correlatable -- the same patient always maps to the same token under one salt, so
  one can follow a patient's turns and count distinct patients -- but not identifying without the salt.
- `tool_call.input` (patient id, a medication name, a search phrase, ...) is dropped; only the argument
  NAMES are kept.
- Free text the model wrote (`routing_decision.rationale`), clinical terms (`verification.flagged_claims`,
  drug names inside `enforced_warnings`) and exception text (`worker_crashed.error`) are reduced to counts,
  kinds or exception types.
- Any UUID-shaped string that remains in any other field is hashed too (safety net for future events).
  `correlation_id` is the one UUID that is meant to be logged: it is random per turn and identifies no one.

The salt comes from LOG_HASH_SALT. If it is unset, a random per-process salt is used, so tokens are still
consistent within one run but change on restart -- set LOG_HASH_SALT in production for tokens that stay stable.
Uvicorn's access log is handled by AccessLogQueryStripFilter (query strings carry patient ids).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from typing import Any

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HASHED_KEYS = frozenset({"patient_id", "other_patient_id", "resident_username"})
_PASS_THROUGH_KEYS = frozenset({"correlation_id", "event"})

# Codes verification/supervisor put in enforced_warnings that carry no clinical content. Anything else in that
# list is a drug name (verification.py appends the conflicting medication itself) and is not logged.
_KNOWN_WARNING_CODES = frozenset({
    "worker_partial_failure", "uploaded_document_disclosure", "low_confidence_value_warning", "duplicate_patient",
    "empty_chart", "no_baseline_established", "partial_failure", "uploaded_document_omission_disclosure",
})

_salt_bytes: bytes | None = None


def _salt() -> bytes:
    global _salt_bytes
    if _salt_bytes is None:
        configured = os.environ.get("LOG_HASH_SALT", "").strip()
        _salt_bytes = configured.encode("utf-8") if configured else secrets.token_bytes(32)
    return _salt_bytes


def reset_salt_for_tests(salt: str | None = None) -> None:
    """Test hook: forget the cached salt (None -> re-read env / new random salt) or pin one."""
    global _salt_bytes
    _salt_bytes = salt.encode("utf-8") if salt is not None else None


def hash_identifier(value: object) -> str | None:
    """Stable, non-reversible token for an identifier under the current salt; None stays None."""
    if value is None:
        return None
    digest = hmac.new(_salt(), str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"h_{digest[:12]}"


def _scrub_uuids(text: str) -> str:
    return _UUID_RE.sub(lambda m: hash_identifier(m.group(0)) or "", text)


def redact_warning(warning: str) -> str:
    """A warning code with no clinical content, or a neutral kind for anything that could name a drug."""
    if warning in _KNOWN_WARNING_CODES:
        return warning
    if warning.startswith("allergy_check_not_assessed"):
        return "allergy_check_not_assessed"
    return "allergy_conflict_enforced"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_uuids(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact_fields(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The fields of one log event with identifiers hashed and free text / clinical vocabulary removed."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _PASS_THROUGH_KEYS:
            out[key] = value
        elif key in _HASHED_KEYS:
            out[key] = hash_identifier(value)
        elif event == "tool_call" and key == "input":
            out["arg_names"] = sorted(value) if isinstance(value, dict) else []
        elif event == "routing_decision" and key == "rationale":
            out["rationale_length"] = len(value) if isinstance(value, str) else 0
        elif event == "verification" and key == "flagged_claims":
            out["flagged_claim_count"] = len(value) if isinstance(value, (list, tuple)) else 0
        elif key == "enforced_warnings" and isinstance(value, (list, tuple)):
            out[key] = [redact_warning(str(w)) for w in value]
        elif event == "worker_crashed" and key == "error":
            out["error_type"] = str(value).split("(", 1)[0].strip()[:80]
        else:
            out[key] = _redact_value(value)
    return out


class AccessLogQueryStripFilter(logging.Filter):
    """Uvicorn access log lines carry the request target, and the query string carries patient ids
    (`/ui?patient=<uuid>`, `/patient?patient_id=<uuid>`). Drops the query and hashes any UUID left in the path."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            target = args[2].split("?", 1)[0]
            record.args = (*args[:2], _scrub_uuids(target), *args[3:])
        return True


def install_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogQueryStripFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogQueryStripFilter())
