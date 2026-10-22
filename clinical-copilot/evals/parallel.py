"""Concurrent, order-preserving case execution for evals/run_evals.py.

EVAL_CONCURRENCY caps how many cases run at once (default 4; 1 is the
original strictly-serial behavior, in ALL_CASES order on the calling
thread). Cases that write shared state are not run in the general pool --
they run one after another on a single "serial lane" worker, which itself
counts toward the cap and runs alongside the pool. Results always come back
in input order, so everything downstream (gate math, regression thresholds,
report ordering) sees exactly what a serial run would have produced.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence, TypeVar

from evals import fixtures as f

DEFAULT_CONCURRENCY = 4
CONCURRENCY_ENV_VAR = "EVAL_CONCURRENCY"

T = TypeVar("T")
R = TypeVar("R")


def resolve_concurrency() -> int:
    raw = os.environ.get(CONCURRENCY_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{CONCURRENCY_ENV_VAR}={raw!r} is not an integer") from None
    if value < 1:
        raise ValueError(f"{CONCURRENCY_ENV_VAR}={value} must be >= 1 (1 = serial)")
    return value


def log_job_timing(job: str, wall_clock_s: float, *, mock: bool = False, **fields: object) -> None:
    """One greppable line per CI job so before/after runs can be compared:
    `[timing] job=golden_set wall_clock_s=812.4 concurrency=4 ...`.
    mock=True (runs driven by fake clients, i.e. the offline tests) prints
    `[timing:mock]` instead, so a test's near-zero timing can never be
    mistaken for a real job's."""
    extras = "".join(f" {key}={value}" for key, value in fields.items())
    tag = "timing:mock" if mock else "timing"
    print(f"[{tag}] job={job} wall_clock_s={wall_clock_s:.1f}{extras}", flush=True)


def serial_reason(case) -> str | None:
    """Why this case can't share the pool, or None if it can. Every
    document-upload case targets the one dedicated upload fixture patient
    (evals/fixtures.py's PID7_UPLOAD_EVAL) and asserts on that patient's
    document set ("most recent upload", the omitted-count cap, duplicate
    rejection, per-case cleanup of exactly what it stored), so two of them
    interleaving would corrupt each other's result. Everything else only
    reads PID1-PID6 or runs against its own in-memory fake FHIR client."""
    explicit = getattr(case, "serial_only_reason", None)
    if explicit:
        return explicit
    if case.patient_id == f.PID7_UPLOAD_EVAL:
        return "writes/reads documents on the shared upload-eval fixture patient (PID7_UPLOAD_EVAL)"
    return None


def run_ordered(
    items: Sequence[T],
    run_one: Callable[[int, T], R],
    *,
    concurrency: int,
    is_serial: Callable[[T], bool],
) -> list[R]:
    n = len(items)
    if concurrency <= 1 or n <= 1:
        return [run_one(i, item) for i, item in enumerate(items)]

    results: list[R | None] = [None] * n
    lane = [i for i, item in enumerate(items) if is_serial(item)]
    pool = [i for i, item in enumerate(items) if not is_serial(item)]

    def run_index(i: int) -> None:
        results[i] = run_one(i, items[i])

    def run_lane() -> None:
        for i in lane:
            run_index(i)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="eval-case") as executor:
        futures = []
        if lane:
            futures.append(executor.submit(run_lane))
        futures.extend(executor.submit(run_index, i) for i in pool)
        for future in futures:
            future.result()

    return results  # type: ignore[return-value]
