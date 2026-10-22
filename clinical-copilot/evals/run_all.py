#!/usr/bin/env python3
"""Runs all three eval suites and prints one clean, unified summary --
useful for demonstrating the whole eval story live (video, interview)
without piecing together three separate command outputs by hand.

Each suite's own detailed per-case output is suppressed here (run that
suite's own module directly for full detail); this prints only the final
counts, pulled live from each suite's real run -- nothing hardcoded except
the PRD-category-to-case-name mapping and the historical bug list, both of
which are explicitly static/traceable by design (see inline comments).

Usage:
    python3 -m evals.run_all
"""

from __future__ import annotations

import datetime
import logging
import sys
import time
from pathlib import Path

from evals.cases import ALL_CASES
from evals.run_behavioral_coverage import run_behavioral_suite
from evals.run_evals import check_gate, flaked_cases, print_flake_summary, run_golden_set
from evals.unit_tests import run_all_tests

HISTORY_LOG_PATH = Path(__file__).parent / "run_history.log"

# app/observability.py's structured per-event JSON logging (every llm_call /
# tool_call / verification / turn event) is genuinely useful when running a
# suite's own module directly, but floods this command's "one clean summary"
# output otherwise -- suppress it here specifically, not in observability.py
# itself, so the other two runners keep their normal verbose behavior.
logging.getLogger("clinical_copilot").setLevel(logging.CRITICAL)

# Maps each PRD Evaluation requirement category to the specific Golden Set
# case name(s) that satisfy it (evals/COVERAGE.md's "PRD Evaluation
# requirement coverage" section is the source of this mapping -- kept in
# sync by hand, same as that section already is). Checked off live below
# based on whether those named cases actually passed THIS run, not asserted
# statically. None means no case exists for it at all.
PRD_CATEGORIES: dict[str, list[str] | None] = {
    "Missing data": ["pid3_empty_chart", "malformed_patient_id"],
    "Ambiguous queries": ["ambiguous_query_unspecified_medication"],
    "Regression / failure modes": [
        "pid1_normal_snapshot",
        "pid2_normal_snapshot",
        "pid6_duplicate_conflicting_dose",
        "adversarial_unverifiable_claim",
        "domain_constraint_allergy_hard_block",
    ],
    "Unauthorized access attempts": None,  # blocked on auth model -- README Known Gaps
}

# Historical record, not live-computed (can't derive "what got fixed today"
# from a test run) -- each entry names the real case/entry that's the
# traceable evidence, per ERROR_ANALYSIS.md.
BUGS_FOUND_AND_FIXED = [
    (
        "COPD abbreviation grounding gap",
        "regression-guarded by c1_1_copd_not_stripped -- see ERROR_ANALYSIS.md Entry 4",
    ),
    (
        "Multi-turn grounding gap",
        "fixed in app/agent.py/app/main.py -- see ERROR_ANALYSIS.md Entry 5",
    ),
]


def main() -> int:
    t0 = time.monotonic()

    print("Running unit tests (LLM-free, code-level)...")
    unit_results = run_all_tests()
    unit_total = len(unit_results)
    unit_passed = sum(1 for _, p, _ in unit_results if p)

    print(f"Running Golden Set ({len(ALL_CASES)} cases, real Anthropic calls)...")
    golden_results = run_golden_set()
    golden_total = len(golden_results)
    golden_passed = sum(1 for r in golden_results if r["passed"])
    gate_passed = check_gate(golden_results, verbose=False)

    print("Running Behavioral Coverage (49 cases, real Anthropic calls -- this takes a few minutes)...")
    behavioral_results = run_behavioral_suite()
    behavioral_total = len(behavioral_results)
    behavioral_passed = sum(1 for r in behavioral_results if r["passed"])

    elapsed_min = (time.monotonic() - t0) / 60

    golden_by_name = {r["name"]: r["passed"] for r in golden_results}

    unit_pct = round(100 * unit_passed / unit_total) if unit_total else 0
    golden_pct = round(100 * golden_passed / golden_total) if golden_total else 0
    behavioral_pct = round(100 * behavioral_passed / behavioral_total) if behavioral_total else 0

    print()
    print("=" * 62)
    print("CLINICAL CO-PILOT -- FULL EVALUATION SUMMARY")
    print("=" * 62)
    print()
    print(f"Unit Tests (LLM-free, code-level):        {unit_passed}/{unit_total}   ({unit_pct}%)")
    print(f"Golden Set (must-pass correctness):       {golden_passed}/{golden_total}   ({golden_pct}%)   Gate: {'PASS' if gate_passed else 'BLOCKED'}")
    print(f"Behavioral Coverage (5 categories):      {behavioral_passed}/{behavioral_total}  ({behavioral_pct}%)")
    print_flake_summary(golden_results)
    print(f"  (golden-set flakes this run: {len(flaked_cases(golden_results))})")
    print()
    print("PRD-required eval categories:")
    for category_name, case_names in PRD_CATEGORIES.items():
        if case_names is None:
            print(f"  [ ] {category_name}  (blocked on auth model -- see README known gaps)")
            continue
        satisfied = all(golden_by_name.get(name) for name in case_names)
        mark = "x" if satisfied else " "
        print(f"  [{mark}] {category_name}  ({', '.join(case_names)})")
    print()
    print("Real bugs found via this suite and fixed today:")
    for i, (title, evidence) in enumerate(BUGS_FOUND_AND_FIXED, start=1):
        print(f"  {i}. {title} ({evidence})")
    print("=" * 62)
    print(f"\nTotal run time: {elapsed_min:.1f} min")

    # Append-only permanent record, distinct from last_run_results.json /
    # last_behavioral_run_results.json (both regenerated-every-run, gitignored).
    # Pulled from the exact same variables already used for the printed
    # summary above, not re-derived.
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    history_line = (
        f"{timestamp}  unit={unit_passed}/{unit_total}  "
        f"golden={golden_passed}/{golden_total}(gate:{'PASS' if gate_passed else 'BLOCKED'})  "
        f"behavioral={behavioral_passed}/{behavioral_total}({behavioral_pct}%)\n"
    )
    with open(HISTORY_LOG_PATH, "a") as f:
        f.write(history_line)

    all_gated_passed = (unit_passed == unit_total) and (golden_passed == golden_total) and gate_passed
    return 0 if all_gated_passed else 1


if __name__ == "__main__":
    sys.exit(main())
