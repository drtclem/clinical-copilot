#!/usr/bin/env python3
"""Decision 5.3: creates/overwrites evals/baseline_results.json from a real
Golden Set run. Deliberately a SEPARATE command from evals/run_evals.py --
never invoked automatically, never run as part of CI. A human runs this,
reviews the diff, and commits it -- that review is the entire point; a
baseline nothing looked at before landing is exactly what Decision 5.3
warns against ("updated ONLY by deliberate human action -- never
auto-overwritten by 'last run's numbers'").

Usage:
    python3 -m evals.create_baseline
    git diff evals/baseline_results.json   # read it before committing
"""

from __future__ import annotations

import sys

from evals import gate
from evals.run_evals import BASELINE_PATH, run_golden_set


def main() -> int:
    print("Running the full Golden Set to create/update the regression baseline...")
    print("This makes real Anthropic + FHIR calls, same as evals/run_evals.py.\n")

    results = run_golden_set()
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    if passed_count != total:
        print(
            f"REFUSING to write a baseline: {passed_count}/{total} passed. "
            "A baseline must come from a confirmed-clean run -- fix the failures first, "
            "or re-run once they're fixed."
        )
        return 1

    metric_rates = gate.compute_metric_rates([r["metrics_5_2"] for r in results])
    print(f"Golden Set: {passed_count}/{total} passed. Metric rates this run:")
    for metric in gate.METRICS:
        rate = metric_rates[metric]
        print(f"  {metric:22s} {'n/a' if rate is None else f'{rate:.0%}'}")

    gate.write_baseline(BASELINE_PATH, metric_rates)
    print(f"\nWrote {BASELINE_PATH}")
    print("Review the diff and commit deliberately -- this is not done for you.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
