"""Offline tests for concurrent eval execution (evals/parallel.py,
evals/run_evals.py's run_golden_set, evals/log_scope.py, evals/
document_cleanup.py's per-thread registry, and the ANTHROPIC_MAX_RETRIES
rate-limit path). No Anthropic calls, no FHIR, no database: fake agent and
FHIR objects, and an httpx MockTransport standing in for api.anthropic.com.

Runnable directly with pytest (zero-arg test_* functions) and registered in
evals/unit_tests.py's TESTS via TESTS below.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Callable

import anthropic
import httpx2

from app.agent import ChatTurnResult
from evals import document_cleanup, fixtures as f
from evals import parallel, run_evals
from evals.cases import EvalCase
from evals.log_scope import ScopedLogCapture, current_case

_LOGGER = logging.getLogger("clinical_copilot")


def test_resolve_concurrency_default_and_validation() -> None:
    saved = os.environ.pop(parallel.CONCURRENCY_ENV_VAR, None)
    try:
        assert parallel.resolve_concurrency() == 4
        for raw, expected in (("1", 1), ("8", 8), (" 2 ", 2)):
            os.environ[parallel.CONCURRENCY_ENV_VAR] = raw
            assert parallel.resolve_concurrency() == expected
        for bad in ("0", "-3", "four", "1.5"):
            os.environ[parallel.CONCURRENCY_ENV_VAR] = bad
            try:
                parallel.resolve_concurrency()
            except ValueError:
                continue
            raise AssertionError(f"{bad!r} should have been rejected")
    finally:
        os.environ.pop(parallel.CONCURRENCY_ENV_VAR, None)
        if saved is not None:
            os.environ[parallel.CONCURRENCY_ENV_VAR] = saved


def test_serial_reason_groups_upload_cases_and_honors_explicit_marker() -> None:
    upload = EvalCase(name="u", category="invariant", guards_against="", patient_id=f.PID7_UPLOAD_EVAL, message="")
    chart = EvalCase(name="c", category="invariant", guards_against="", patient_id=f.PID1_ALICE, message="")
    marked = EvalCase(
        name="m", category="invariant", guards_against="", patient_id=f.PID2_BOB, message="",
        serial_only_reason="mutates something global",
    )
    assert parallel.serial_reason(upload)
    assert parallel.serial_reason(chart) is None
    assert parallel.serial_reason(marked) == "mutates something global"


def test_run_ordered_keeps_input_order_when_completion_order_is_reversed() -> None:
    items = list(range(8))

    def run_one(i: int, item: int) -> int:
        time.sleep((len(items) - i) * 0.01)  # later items finish first
        return item * 10

    out = parallel.run_ordered(items, run_one, concurrency=4, is_serial=lambda x: False)
    assert out == [i * 10 for i in items], out


def test_run_ordered_respects_cap_and_serial_lane_never_overlaps_itself() -> None:
    lock = threading.Lock()
    running = 0
    peak = 0
    serial_running = 0
    serial_peak = 0
    overlapped_lane_with_pool = False

    def run_one(i: int, item: tuple[str, int]) -> str:
        nonlocal running, peak, serial_running, serial_peak, overlapped_lane_with_pool
        kind = item[0]
        with lock:
            running += 1
            peak = max(peak, running)
            if kind == "serial":
                serial_running += 1
                serial_peak = max(serial_peak, serial_running)
            elif serial_running:
                overlapped_lane_with_pool = True
        time.sleep(0.03)
        with lock:
            running -= 1
            if kind == "serial":
                serial_running -= 1
        return f"{kind}{item[1]}"

    items = [("serial", 0), ("pool", 1), ("serial", 2), ("pool", 3), ("pool", 4), ("pool", 5), ("serial", 6), ("pool", 7)]
    out = parallel.run_ordered(items, run_one, concurrency=3, is_serial=lambda x: x[0] == "serial")
    assert out == [f"{k}{n}" for k, n in items], out
    assert peak <= 3, f"cap of 3 exceeded: {peak}"
    assert peak >= 2, "nothing ran concurrently"
    assert serial_peak == 1, f"serial-lane cases overlapped each other ({serial_peak})"
    assert overlapped_lane_with_pool, "the serial lane should run alongside the pool, not block it"


def test_run_ordered_concurrency_one_is_strictly_serial_on_the_calling_thread() -> None:
    seen: list[tuple[int, int]] = []
    caller = threading.get_ident()

    def run_one(i: int, item: int) -> int:
        seen.append((i, threading.get_ident()))
        return item

    parallel.run_ordered([5, 6, 7], run_one, concurrency=1, is_serial=lambda x: False)
    assert [i for i, _ in seen] == [0, 1, 2]
    assert {t for _, t in seen} == {caller}


def _fake_turn(text: str, *, verification_passed: bool = True, domain_ok: bool = True) -> ChatTurnResult:
    return ChatTurnResult(
        correlation_id="cid",
        response_text=text,
        verification_passed=verification_passed,
        passed_domain_constraint=domain_ok,
        flagged_claims=[],
        enforced_warnings=[],
    )


class _FakeAgent:
    def run_turn(self, history, message, patient_id, prior_tool_records=None, fhir=None):
        time.sleep(0.01 if message.startswith("slow") else 0)
        _LOGGER.info('{"event": "turn_started", "message_length": %d}', len(message))
        return _fake_turn(f"answer to {message}")


def _cases() -> list[EvalCase]:
    def check(result: ChatTurnResult) -> tuple[bool, str]:
        return ("answer" in result.response_text, "ok")

    cases = []
    for i in range(12):
        pid = f.PID7_UPLOAD_EVAL if i % 3 == 0 else f.PID1_ALICE
        cases.append(
            EvalCase(
                name=f"case_{i}", category="invariant", guards_against="g", patient_id=pid,
                message=("slow " if i % 2 else "") + f"q{i}", check=check,
            )
        )
    cases.append(
        EvalCase(
            name="skipped", category="boundary", guards_against="g", patient_id=None, message="",
            skip_reason="no credential",
        )
    )
    cases.append(
        EvalCase(
            name="fails", category="regression", guards_against="g", patient_id=f.PID2_BOB, message="q",
            check=lambda r: (False, "expected failure"),
        )
    )
    return cases


def _normalized(results: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "elapsed_s"} for r in results]


def test_parallel_results_match_serial_and_gate_math_is_unchanged() -> None:
    clients = (SimpleNamespace(), SimpleNamespace(), _FakeAgent())
    cases = _cases()
    serial = run_evals.run_golden_set(cases, concurrency=1, clients=clients)
    concurrent = run_evals.run_golden_set(cases, concurrency=4, clients=clients)

    assert [r["name"] for r in concurrent] == [c.name for c in cases]
    assert _normalized(concurrent) == _normalized(serial)

    from evals import gate

    assert run_evals.check_gate(serial, verbose=False) == run_evals.check_gate(concurrent, verbose=False)
    rates_serial = gate.compute_metric_rates([r["metrics_5_2"] for r in serial])
    rates_concurrent = gate.compute_metric_rates([r["metrics_5_2"] for r in concurrent])
    assert rates_serial == rates_concurrent
    assert run_evals.check_unflagged_verification_failures(serial) == run_evals.check_unflagged_verification_failures(
        concurrent
    )


def test_log_capture_is_scoped_to_its_own_case_under_concurrency() -> None:
    barrier = threading.Barrier(2)
    captured: dict[str, list[str]] = {}

    def run_one(i: int, name: str) -> None:
        token = current_case.set(f"{i}:{name}")
        try:
            capture = ScopedLogCapture()
            _LOGGER.addHandler(capture)
            try:
                barrier.wait(timeout=5)  # both captures are attached before either logs
                _LOGGER.info("%s-line", name)
                barrier.wait(timeout=5)  # both have logged before either reads
                captured[name] = list(capture.messages)
            finally:
                _LOGGER.removeHandler(capture)
        finally:
            current_case.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for fut in [pool.submit(run_one, 0, "alpha"), pool.submit(run_one, 1, "beta")]:
            fut.result()
    assert captured["alpha"] == ["alpha-line"], captured
    assert captured["beta"] == ["beta-line"], captured


def test_untagged_log_records_are_attributed_to_every_active_capture() -> None:
    def make(name: str) -> ScopedLogCapture:
        def build() -> ScopedLogCapture:
            current_case.set(name)
            return ScopedLogCapture()

        return contextvars.copy_context().run(build)

    cap_a, cap_b = make("a"), make("b")
    _LOGGER.addHandler(cap_a)
    _LOGGER.addHandler(cap_b)
    try:
        # Emitted from a thread that never saw a case tag -- must not be
        # silently dropped (a PHI leak there would otherwise vanish).
        t = threading.Thread(target=lambda: _LOGGER.info("from-untagged-thread"))
        t.start()
        t.join()
    finally:
        _LOGGER.removeHandler(cap_a)
        _LOGGER.removeHandler(cap_b)
    assert cap_a.messages == ["from-untagged-thread"]
    assert cap_b.messages == ["from-untagged-thread"]


def test_phi_leak_in_one_concurrent_case_is_not_blamed_on_another() -> None:
    class LeakyThenCleanAgent:
        def run_turn(self, history, message, patient_id, prior_tool_records=None, fhir=None):
            _LOGGER.info("patient Testpatient DOB 03/14/1972" if message == "leak" else "turn ok")
            time.sleep(0.02)
            return _fake_turn("answer")

    cases = [
        EvalCase(name=f"c{i}", category="invariant", guards_against="g", patient_id=f.PID1_ALICE,
                 message="leak" if i == 0 else "clean", check=lambda r: (True, "ok"))
        for i in range(6)
    ]
    out = run_evals.run_golden_set(
        cases, concurrency=4, clients=(SimpleNamespace(), SimpleNamespace(), LeakyThenCleanAgent())
    )
    flags = [r["metrics_5_2"]["no_phi_in_logs"] for r in out]
    assert flags == [False, True, True, True, True, True], flags


def test_cleanup_registry_is_per_thread() -> None:
    deleted: list[tuple[str, list[str]]] = []
    original = document_cleanup.delete_documents_for_pubpid
    document_cleanup.delete_documents_for_pubpid = lambda pubpid, names: (deleted.append((pubpid, list(names))), len(names))[1]
    try:
        registered = threading.Event()
        flushed_elsewhere = threading.Event()

        def uploader() -> None:
            document_cleanup.register_for_cleanup("TEST-0007", "mine.png")
            registered.set()
            flushed_elsewhere.wait(timeout=5)
            document_cleanup.flush_cleanup()

        t = threading.Thread(target=uploader)
        t.start()
        registered.wait(timeout=5)
        document_cleanup.flush_cleanup()  # another case finishing on this thread
        assert deleted == [], f"another thread's flush deleted a concurrent case's upload: {deleted}"
        flushed_elsewhere.set()
        t.join()
        assert deleted == [("TEST-0007", ["mine.png"])], deleted
    finally:
        document_cleanup.delete_documents_for_pubpid = original


def _anthropic_ok_body() -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _llm_client_over_mock_transport(rate_limited_calls: int):
    from app import llm_client

    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] <= rate_limited_calls:
            return httpx2.Response(
                429, headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            )
        return httpx2.Response(200, json=_anthropic_ok_body())

    real = anthropic.Anthropic

    def patched(**kwargs):
        return real(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kwargs)

    llm_client.anthropic.Anthropic = patched
    try:
        settings = SimpleNamespace(anthropic_api_key="offline-dummy", anthropic_model="m")
        client = llm_client.AnthropicLLMClient(settings)
    finally:
        llm_client.anthropic.Anthropic = real
    return client, calls


def _call(client) -> None:
    client.create_message(system="s", tools=[], messages=[{"role": "user", "content": "hi"}], max_tokens=16)


def test_rate_limit_429s_are_retried_with_configured_budget() -> None:
    saved = os.environ.get("ANTHROPIC_MAX_RETRIES")
    os.environ["ANTHROPIC_MAX_RETRIES"] = "5"
    try:
        client, calls = _llm_client_over_mock_transport(rate_limited_calls=3)
        _call(client)
        assert calls["n"] == 4, calls
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_MAX_RETRIES", None)
        else:
            os.environ["ANTHROPIC_MAX_RETRIES"] = saved


def test_rate_limit_still_raises_once_the_retry_budget_is_exhausted() -> None:
    saved = os.environ.get("ANTHROPIC_MAX_RETRIES")
    os.environ["ANTHROPIC_MAX_RETRIES"] = "2"
    try:
        client, calls = _llm_client_over_mock_transport(rate_limited_calls=99)
        try:
            _call(client)
        except anthropic.RateLimitError:
            pass
        else:
            raise AssertionError("expected RateLimitError after the budget was exhausted")
        assert calls["n"] == 3, calls  # 1 attempt + 2 retries
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_MAX_RETRIES", None)
        else:
            os.environ["ANTHROPIC_MAX_RETRIES"] = saved


_TESTS: list[Callable[[], None]] = [
    test_resolve_concurrency_default_and_validation,
    test_serial_reason_groups_upload_cases_and_honors_explicit_marker,
    test_run_ordered_keeps_input_order_when_completion_order_is_reversed,
    test_run_ordered_respects_cap_and_serial_lane_never_overlaps_itself,
    test_run_ordered_concurrency_one_is_strictly_serial_on_the_calling_thread,
    test_parallel_results_match_serial_and_gate_math_is_unchanged,
    test_log_capture_is_scoped_to_its_own_case_under_concurrency,
    test_untagged_log_records_are_attributed_to_every_active_capture,
    test_phi_leak_in_one_concurrent_case_is_not_blamed_on_another,
    test_cleanup_registry_is_per_thread,
    test_rate_limit_429s_are_retried_with_configured_budget,
    test_rate_limit_still_raises_once_the_retry_budget_is_exhausted,
]


def _adapt(fn: Callable[[], None]) -> Callable[[object], tuple[bool, str]]:
    def run(_fhir: object) -> tuple[bool, str]:
        try:
            fn()
        except AssertionError as exc:
            return False, f"assertion failed: {exc}"
        return True, "ok"

    return run


TESTS = [(fn.__name__.removeprefix("test_"), _adapt(fn)) for fn in _TESTS]
