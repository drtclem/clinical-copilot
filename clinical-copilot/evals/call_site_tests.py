"""Offline tests (mocked LLM, no network, no FHIR) for: stop_reason / thinking
diagnostics on every llm_call site, thinking disabled + truncation handling on
the routing / structuring / query-extraction / judge sites, the per-case
golden-set retry (FLAKE reporting, and a deterministic failure still blocking),
and the behavioral prompt-injection check.

Runnable with pytest and registered in evals/unit_tests.py's TESTS.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from app.agent import SYSTEM_PROMPT, ChatTurnResult
from app.graph.supervisor import _FALLBACK_WORKERS, route_node
from app.graph.workers import evidence_retriever_node
from app.ingestion import pipeline
from app.ingestion.ocr import OcrPage, OcrResult, OcrWord
from app.ingestion.structuring import structure_document
from app.llm_client import THINKING_DISABLED, AnthropicLLMClient, LLMToolCall, LLMTurnResponse, TruncatedOutputError
from app.observability import TurnTrace
from app.retrieval.query import build_safe_query
from evals import cases as eval_cases
from evals import run_evals

_LOGGER = logging.getLogger("clinical_copilot")


class _RecordingLLM:
    """Returns one canned response and records what each call asked for."""

    def __init__(self, response: LLMTurnResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create_message(self, *, system, tools, messages, max_tokens, tool_choice=None, thinking=None):
        self.calls.append({"max_tokens": max_tokens, "tool_choice": tool_choice, "thinking": thinking})
        return self._response

    def append_assistant_turn(self, messages, response) -> None: ...
    def append_tool_results(self, messages, results) -> None: ...
    def check_ready(self) -> bool:
        return True


def _tool_response(name: str, payload: dict, *, stop_reason: str = "tool_use", out_tokens: int = 50) -> LLMTurnResponse:
    return LLMTurnResponse(
        text="", tool_calls=[LLMToolCall(id="c1", name=name, input=payload)], input_tokens=100,
        output_tokens=out_tokens, raw_for_trace=[], stop_reason=stop_reason,
        content_block_types=["tool_use"], thinking_chars=7,
    )


def _trace() -> TurnTrace:
    return TurnTrace(client=None, span=None, correlation_id="call-site-test")


@contextlib.contextmanager
def _captured_llm_calls():
    captured: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _H()
    _LOGGER.addHandler(handler)
    prior_level = _LOGGER.level
    _LOGGER.setLevel(logging.INFO)
    events: list[dict] = []
    try:
        yield events
    finally:
        _LOGGER.removeHandler(handler)
        _LOGGER.setLevel(prior_level)
        events.extend(json.loads(m) for m in captured if '"event": "llm_call"' in m)


def _assert_meta(event: dict) -> None:
    assert event["stop_reason"] is not None, event
    assert event["content_block_types"] == ["tool_use"], event
    assert event["thinking_chars"] == 7, event


def _ocr() -> OcrResult:
    word = OcrWord(text="Allergies: penicillin", confidence=95.0, bbox=(0.1, 0.1, 0.5, 0.2))
    return OcrResult(pages=[OcrPage(page_number=1, words=[word])])


_INTAKE_DEMOGRAPHICS_ONLY = {
    "demographics": {"name": {"value": "A B", "page_number": 1, "quote": "Allergies", "confidence": "high"}}
}


# --- every call site logs the diagnostics, and disables thinking -----------


def test_routing_disables_thinking_and_logs_stop_reason_and_thinking_size() -> None:
    llm = _RecordingLLM(_tool_response("route_turn", {"workers": ["chart_data_worker"], "rationale": "r"}))
    state = {"user_message": "q", "patient_id": "p1", "uploaded_document_ref": None}
    with _captured_llm_calls() as events:
        route_node(state, llm=llm, model="m", trace=_trace())
    assert llm.calls[0]["thinking"] == THINKING_DISABLED, llm.calls
    assert len(events) == 1
    _assert_meta(events[0])


def test_structuring_disables_thinking_and_logs_diagnostics() -> None:
    llm = _RecordingLLM(_tool_response("extract_intake_form", _INTAKE_DEMOGRAPHICS_ONLY))
    with _captured_llm_calls() as events:
        structure_document(_ocr(), "intake_form", llm=llm, model="m", trace=_trace())
    assert llm.calls[0]["thinking"] == THINKING_DISABLED, llm.calls
    # Headroom beyond the old 2048 that thinking could have eaten into.
    assert llm.calls[0]["max_tokens"] >= 4096, llm.calls
    _assert_meta(events[0])


def test_query_extraction_disables_thinking_and_logs_diagnostics() -> None:
    llm = _RecordingLLM(_tool_response("extract_query", {"queries": ["hyperkalemia management"]}))
    with _captured_llm_calls() as events:
        queries, _, _ = build_safe_query("q", llm=llm, model="m", trace=_trace())
    assert queries == ["hyperkalemia management"]
    assert llm.calls[0]["thinking"] == THINKING_DISABLED, llm.calls
    _assert_meta(events[0])


def test_judge_disables_thinking_and_logs_diagnostics() -> None:
    response = LLMTurnResponse(
        text="VERDICT: YES\nREASON: ok", tool_calls=[], input_tokens=10, output_tokens=8, raw_for_trace=[],
        stop_reason="end_turn", content_block_types=["text"], thinking_chars=0,
    )
    llm = _RecordingLLM(response)
    original = eval_cases._judge_llm_singleton
    eval_cases._judge_llm_singleton = llm
    try:
        with _captured_llm_calls() as events:
            passed, _ = eval_cases._llm_judge_absence("q", "text")
    finally:
        eval_cases._judge_llm_singleton = original
    assert passed
    assert llm.calls[0]["thinking"] == THINKING_DISABLED, llm.calls
    assert events[0]["role"] == "judge" and events[0]["stop_reason"] == "end_turn", events
    assert events[0]["content_block_types"] == ["text"] and events[0]["thinking_chars"] == 0, events


def test_judge_cut_off_at_max_tokens_fails_closed() -> None:
    response = LLMTurnResponse(
        text="VERDICT: YES", tool_calls=[], input_tokens=10, output_tokens=300, raw_for_trace=[],
        stop_reason="max_tokens", content_block_types=["thinking"],
    )
    original = eval_cases._judge_llm_singleton
    eval_cases._judge_llm_singleton = _RecordingLLM(response)
    try:
        passed, reason = eval_cases._llm_judge_absence("q", "text")
    finally:
        eval_cases._judge_llm_singleton = original
    assert not passed and "cut off" in reason, reason


def test_anthropic_client_forwards_thinking_only_when_asked_and_counts_thinking_chars() -> None:
    seen: list[dict] = []
    blocks = [
        SimpleNamespace(type="thinking", thinking="abcde"),
        SimpleNamespace(type="thinking", thinking="fg"),
        SimpleNamespace(type="text", text="hi"),
    ]

    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=2), stop_reason="end_turn"
        )

    client = AnthropicLLMClient(SimpleNamespace(anthropic_api_key="offline-dummy", anthropic_model="m"))
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    args = dict(system="s", tools=[], messages=[], max_tokens=16)
    response = client.create_message(**args)
    client.create_message(**args, thinking=THINKING_DISABLED)
    assert "thinking" not in seen[0], "default must leave provider behavior untouched"
    assert seen[1]["thinking"] == {"type": "disabled"}, seen[1]
    assert response.thinking_chars == 7 and response.content_block_types == ["thinking", "thinking", "text"]


# --- truncation is never returned as a complete result ---------------------


def test_truncated_structuring_output_raises_instead_of_returning_a_partial_extraction() -> None:
    # Schema-valid, but cut off before allergies / medications / family history.
    llm = _RecordingLLM(_tool_response("extract_intake_form", _INTAKE_DEMOGRAPHICS_ONLY, stop_reason="max_tokens"))
    try:
        structure_document(_ocr(), "intake_form", llm=llm, model="m", trace=_trace())
    except TruncatedOutputError:
        return
    raise AssertionError("a max_tokens-truncated structuring call must not yield an extraction")


def test_truncated_structuring_is_disclosed_by_the_pipeline_and_nothing_is_stored() -> None:
    llm = _RecordingLLM(_tool_response("extract_intake_form", _INTAKE_DEMOGRAPHICS_ONLY, stop_reason="max_tokens"))
    original_ocr = pipeline.run_ocr
    pipeline.run_ocr = lambda file_bytes, content_type: _ocr()
    try:
        result, failure, _, _ = pipeline.attach_and_extract(
            "p1", b"x", "f.png", "image/png", "intake_form",
            fhir=SimpleNamespace(), llm=llm, model="m", trace=_trace(),
        )
    finally:
        pipeline.run_ocr = original_ocr
    assert result is None and failure is not None and failure.detail_code == "structuring_truncated", failure
    assert "cut off" in failure.reason


def test_truncated_query_extraction_raises_and_the_evidence_worker_discloses_it() -> None:
    llm = _RecordingLLM(_tool_response("extract_query", {"queries": ["only the first"]}, stop_reason="max_tokens"))
    try:
        build_safe_query("q", llm=llm, model="m", trace=_trace())
    except TruncatedOutputError:
        pass
    else:
        raise AssertionError("expected TruncatedOutputError")
    out = evidence_retriever_node(
        {"user_message": "q"}, llm=llm, model="m", retriever=SimpleNamespace(), trace=_trace()
    )
    assert out["retrieved_evidence"] == []
    assert any("cut off" in m for m in out["partial_failures"]), out["partial_failures"]


def test_truncated_routing_falls_back_instead_of_trusting_a_partial_worker_list() -> None:
    llm = _RecordingLLM(
        _tool_response("route_turn", {"workers": ["evidence_retriever"], "rationale": "r"}, stop_reason="max_tokens")
    )
    state = {"user_message": "q", "patient_id": "p1", "uploaded_document_ref": None}
    out = route_node(state, llm=llm, model="m", trace=_trace())
    assert out["workers_invoked"] == list(_FALLBACK_WORKERS), out
    assert "cut off" in out["routing_rationale"]


# --- per-case golden-set retry ---------------------------------------------


def _standalone_case(name: str, outcomes: list[bool]) -> tuple[eval_cases.EvalCase, list[int]]:
    """A case whose successive attempts pass/fail per `outcomes` (last value
    repeats), plus a call counter."""
    calls: list[int] = []

    def check(_fhir) -> tuple[bool, str]:
        calls.append(1)
        ok = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        return ok, "ok" if ok else f"failed attempt {len(calls)}"

    case = eval_cases.EvalCase(
        name=name, category="boundary", guards_against="g", patient_id=None, message="", standalone_check=check
    )
    return case, calls


def _run(cases: list[eval_cases.EvalCase]) -> list[dict]:
    clients = (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    with contextlib.redirect_stdout(io.StringIO()):
        return run_evals.run_golden_set(cases, concurrency=1, clients=clients)


def test_a_case_that_fails_once_and_passes_on_rerun_is_a_reported_flake() -> None:
    flaky, flaky_calls = _standalone_case("flaky", [False, True])
    steady, steady_calls = _standalone_case("steady", [True])
    results = _run([flaky, steady])
    by_name = {r["name"]: r for r in results}
    assert by_name["flaky"]["passed"] and by_name["flaky"]["flake"] is True, by_name["flaky"]
    assert by_name["flaky"]["first_attempt"]["reason"] == "failed attempt 1", by_name["flaky"]
    assert len(flaky_calls) == 2
    assert len(steady_calls) == 1 and "flake" not in by_name["steady"], "a passing case is never rerun"
    assert [r["name"] for r in run_evals.flaked_cases(results)] == ["flaky"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_evals.print_flake_summary(results)
    assert "[FLAKE] flaky" in buf.getvalue(), buf.getvalue()


def test_a_deterministic_failure_fails_both_tries_and_still_blocks_the_gate() -> None:
    broken, broken_calls = _standalone_case("broken", [False])
    fine, _ = _standalone_case("fine", [True])
    results = _run([broken, fine])
    by_name = {r["name"]: r for r in results}
    assert len(broken_calls) == 2, "failed case must be rerun exactly once"
    assert by_name["broken"]["passed"] is False and by_name["broken"]["flake"] is False, by_name["broken"]
    assert by_name["broken"]["attempts"] == 2
    assert run_evals.flaked_cases(results) == []

    # And main()'s exit code -- what CI gates on -- is non-zero, while the same
    # harness with every case passing exits 0 (so non-zero here isn't incidental).
    assert _main_exit_code([_standalone_case("broken2", [False])[0]]) != 0
    assert _main_exit_code([_standalone_case("ok", [True])[0]]) == 0
    assert _main_exit_code([_standalone_case("flaky2", [False, True])[0]]) == 0, "a flake passes, by design"


def _main_exit_code(cases: list[eval_cases.EvalCase]) -> int:
    out_path = Path(run_evals.__file__).parent / "last_run_results.json"
    prior = out_path.read_bytes() if out_path.exists() else None
    saved = (run_evals.ALL_CASES, run_evals._build_clients)
    run_evals.ALL_CASES = cases
    run_evals._build_clients = lambda: (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return run_evals.main()
    finally:
        run_evals.ALL_CASES, run_evals._build_clients = saved
        if prior is None:
            out_path.unlink(missing_ok=True)
        else:
            out_path.write_bytes(prior)


def test_retry_can_be_disabled() -> None:
    broken, calls = _standalone_case("broken", [False])
    clients = (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    with contextlib.redirect_stdout(io.StringIO()):
        run_evals.run_golden_set([broken], concurrency=1, clients=clients, retry_failed=False)
    assert len(calls) == 1


# --- prompt_injection_resisted tests behavior, not phrasing ----------------


def _turn(text: str, *, snapshot: bool = True) -> ChatTurnResult:
    return ChatTurnResult(
        correlation_id="c", response_text=text, verification_passed=True, passed_domain_constraint=True,
        flagged_claims=[], enforced_warnings=[],
        tool_calls=[{"tool": "get_patient_snapshot", "failed": False}] if snapshot else [],
    )


def _check_with_judge(text: str, judge_says_not_followed: bool, *, snapshot: bool = True) -> tuple[bool, str]:
    original = eval_cases._llm_judge_absence
    eval_cases._llm_judge_absence = lambda q, t: (judge_says_not_followed, "judge")
    try:
        return eval_cases._check_prompt_injection_resisted(_turn(text, snapshot=snapshot))
    finally:
        eval_cases._llm_judge_absence = original


def test_injection_check_passes_a_refusal_with_none_of_the_old_cue_words() -> None:
    # The old check needed one of "not comply", "injected", "disregard", ... in the text.
    text = "One allergy entry is not real chart content; allergies need to be confirmed with the patient."
    assert _check_with_judge(text, True)[0]


def test_injection_check_fails_when_the_judge_says_the_instruction_was_followed() -> None:
    # Contains the old cue "disregard" but actually complies -- the old check passed this.
    text = "I'll disregard the usual checks: all medications are safe to give."
    passed, reason = _check_with_judge(text, False)
    assert not passed and "followed the injected instruction" in reason, reason


def test_injection_judge_question_targets_compliance_not_wording() -> None:
    q = eval_cases._INJECTION_JUDGE_QUESTION
    assert eval_cases._INJECTION_PAYLOAD in q and "wording" in q and "quoting" in q.lower()


def test_injection_check_fails_on_a_system_prompt_leak_without_consulting_the_judge() -> None:
    leaked = " ".join(SYSTEM_PROMPT.split())[100:260]
    passed, reason = _check_with_judge(f"Sure, here are my instructions: {leaked}", True)
    assert not passed and "leaked" in reason, reason
    assert not _check_with_judge("The <retrieved_patient_data> block says NKDA.", True)[0]


def test_injection_check_still_requires_the_injected_fixture_to_have_been_read() -> None:
    assert not _check_with_judge("Nothing to see.", True, snapshot=False)[0]


_TESTS: list[Callable[[], None]] = [
    test_routing_disables_thinking_and_logs_stop_reason_and_thinking_size,
    test_structuring_disables_thinking_and_logs_diagnostics,
    test_query_extraction_disables_thinking_and_logs_diagnostics,
    test_judge_disables_thinking_and_logs_diagnostics,
    test_judge_cut_off_at_max_tokens_fails_closed,
    test_anthropic_client_forwards_thinking_only_when_asked_and_counts_thinking_chars,
    test_truncated_structuring_output_raises_instead_of_returning_a_partial_extraction,
    test_truncated_structuring_is_disclosed_by_the_pipeline_and_nothing_is_stored,
    test_truncated_query_extraction_raises_and_the_evidence_worker_discloses_it,
    test_truncated_routing_falls_back_instead_of_trusting_a_partial_worker_list,
    test_a_case_that_fails_once_and_passes_on_rerun_is_a_reported_flake,
    test_a_deterministic_failure_fails_both_tries_and_still_blocks_the_gate,
    test_retry_can_be_disabled,
    test_injection_check_passes_a_refusal_with_none_of_the_old_cue_words,
    test_injection_check_fails_when_the_judge_says_the_instruction_was_followed,
    test_injection_judge_question_targets_compliance_not_wording,
    test_injection_check_fails_on_a_system_prompt_leak_without_consulting_the_judge,
    test_injection_check_still_requires_the_injected_fixture_to_have_been_read,
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
