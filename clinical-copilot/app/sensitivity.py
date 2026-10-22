"""Compensating sensitivity control for Encounter-type resources.

ARCHITECTURE.md 3.3: because whether a `user/`-scoped FHIR token enforces
OpenEMR's own encounter-sensitivity gate was unverified (architecture-audit.md
6.6), any tool touching Encounter data applies its own filter, deliberately
redundant with whatever the platform does.

That empirical test (Phase 1) found the FHIR Encounter resource does not
carry a sensitivity marker at all -- not filtered, not tagged, simply absent
(`grep -i sensitivity src/Services/FHIR/FhirEncounterService.php` returns
nothing; confirms architecture-audit.md Finding 12's "no check exists in the
FHIR read path" applies to the *representation*, not just enforcement). The
`sensitivity` column is only surfaced by OpenEMR's own standard REST API
(`GET /apis/default/api/patient/{puuid}/encounter`, `EncounterService.php`),
which is still "OpenEMR's REST/FHIR API" per ARCHITECTURE.md 1.3, not a
direct-database read. `FhirClient.get_standard_api()` is how a caller reaches
it. This module's filter is written against that shape, not a FHIR Bundle.

Role -> sensitivity clearance, from audit-notes.md's confirmed gacl matrix
(read from the DB, 2026-09-14): only `doc`, `breakglass`, and `admin` hold a
High-sensitivity grant. The overnight cross-covering resident persona this
project is built around is NOT the patient's own attending -- ARCHITECTURE.md
3.3 maps that persona to the `clin`/physician-covering roles, which hold no
High grant, so this control excludes High-sensitivity encounters for it.
"""

from __future__ import annotations

from typing import Literal

Role = Literal["doc", "clin", "front", "back", "breakglass", "admin"]
SensitivityLevel = Literal["normal", "high", ""]

# audit-notes.md's gacl matrix, "Sensitivity: Normal" / "Sensitivity: High"
# rows only -- the two capabilities this filter cares about. Any grant other
# than blank/"—" counts as clearance for that level.
_HIGH_SENSITIVITY_CLEARANCE: frozenset[Role] = frozenset({"doc", "breakglass", "admin"})


def has_high_sensitivity_clearance(role: Role) -> bool:
    return role in _HIGH_SENSITIVITY_CLEARANCE


def filter_encounters_by_sensitivity(encounters: list[dict], role: Role) -> list[dict]:
    """encounters: dicts from OpenEMR's standard REST API encounter list
    (each carrying a `sensitivity` key: 'high' / 'normal' / '' or missing).
    Excludes -- not just hides -- any encounter this role isn't cleared for,
    so a filtered-out encounter never reaches the model's context at all
    (ARCHITECTURE.md 3.3's "not just hidden from the final response").
    """
    if has_high_sensitivity_clearance(role):
        return list(encounters)
    return [e for e in encounters if (e.get("sensitivity") or "").lower() != "high"]
