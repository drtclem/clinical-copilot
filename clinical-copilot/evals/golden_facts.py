"""Golden Sets: the known ground-truth facts for each fixture patient (pid
1-6, audit-notes.md), pulled out of eval-check logic where they previously
lived implicitly as inline `required = [...]` lists in cases.py. One obvious
source of truth for "what this patient's chart actually contains," rather
than ground truth duplicated across checks.

Confirmed against the live FHIR data on both the local dev stack and the
droplet (same UUIDs, see evals/fixtures.py) at the time this was written --
if the fixture data changes, update this alongside it.
"""

from __future__ import annotations

from typing import TypedDict


class GoldenFacts(TypedDict):
    conditions: list[str]
    medications: list[str]
    allergies: list[str]
    known_duplicate_of: str | None


GOLDEN_FACTS: dict[str, GoldenFacts] = {
    "pid1_alice": {
        "conditions": ["diabetes", "hypertension"],
        "medications": ["metformin", "lisinopril"],
        "allergies": ["penicillin"],
        "known_duplicate_of": "pid6_alice_dup",
    },
    "pid2_bob": {
        "conditions": ["chronic obstructive pulmonary disease"],
        "medications": ["tiotropium"],
        "allergies": ["sulfa"],
        "known_duplicate_of": None,
    },
}
