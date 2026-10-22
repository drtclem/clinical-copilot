#!/usr/bin/env python3
"""Behavioral Coverage runner: executes every case in evals/behavioral_coverage.py
against the live agent (real Anthropic + FHIR calls).

Unlike run_evals.py's Golden Set, this is NOT gated and not all-must-pass --
a failing case here is expected, useful information about where the
agent's behavior gets shaky, not a build blocker. See evals/COVERAGE.md.

Usage:
    python3 -m evals.run_behavioral_coverage
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.observability import TurnObserver
from evals.behavioral_coverage import BEHAVIORAL_CASES, run_behavioral_case


def run_behavioral_suite() -> list[dict]:
    """Runs every Behavioral Coverage case and returns result dicts, no
    printing -- the reusable core, shared by main() and evals/run_all.py."""
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))
    observer = TurnObserver(settings)
    agent = ClinicalCopilotAgent(settings, fhir, observer)

    results = []
    for case in BEHAVIORAL_CASES:
        t0 = time.monotonic()
        try:
            turn_results = run_behavioral_case(agent, case)
            passed, reason = case.check(turn_results)
            error = None
            final = turn_results[-1]
        except Exception as exc:  # noqa: BLE001 -- record a failure, don't crash the suite
            passed = False
            reason = f"case raised an exception: {exc!r}"
            turn_results = []
            final = None
            error = repr(exc)
        elapsed = time.monotonic() - t0

        results.append(
            {
                "name": case.name,
                "category": case.category,
                "category_name": case.category_name,
                "guards_against": case.guards_against,
                "patient_id": case.patient_id,
                "messages": case.messages,
                "passed": passed,
                "reason": reason,
                "response_text": final.response_text if final else None,
                "flagged_claims": final.flagged_claims if final else None,
                "turn_count": len(turn_results),
                "elapsed_s": round(elapsed, 2),
                "error": error,
            }
        )
    return results


def main() -> int:
    results = run_behavioral_suite()

    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    pct = round(100 * passed_count / total) if total else 0

    by_category: dict[int, list[dict]] = defaultdict(list)
    for r in results:
        by_category[r["category"]].append(r)

    print(f"\n{'=' * 70}")
    print("Behavioral Coverage -- NOT gated, a failure here is information, not a blocker")
    print(f"{'=' * 70}")
    print(f"Overall: {passed_count}/{total} passed ({pct}%)")
    for cat in sorted(by_category):
        rows = by_category[cat]
        cat_passed = sum(1 for r in rows if r["passed"])
        print(f"  Category {cat} ({rows[0]['category_name']}): {cat_passed}/{len(rows)} passed")
    print(f"{'=' * 70}\n")

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['name']} (category {r['category']}: {r['category_name']}) -- {r['elapsed_s']}s")
        print(f"       guards against: {r['guards_against']}")
        print(f"       reason: {r['reason']}")
        if not r["passed"]:
            print(f"       patient_id: {r['patient_id']!r}")
            print(f"       messages sent: {r['messages']!r}")
            print(f"       final response: {r['response_text']!r}")
            print(f"       flagged_claims: {r['flagged_claims']!r}")
            if r["error"]:
                print(f"       exception: {r['error']}")
        print()

    out_path = Path(__file__).parent / "last_behavioral_run_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"Full results written to {out_path}")

    # Not gated: always returns 0 (informational suite, never blocks a build).
    return 0


if __name__ == "__main__":
    sys.exit(main())
