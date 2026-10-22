"""LLM provider abstraction -- the seam between app/agent.py's tool-calling
loop and whichever model actually drives it.

Anthropic Claude (AnthropicLLMClient) is the only validated implementation.
Every safety guarantee this project documents (ARCHITECTURE.md Section 3's
verification layer, THREAT_MODEL.md's prompt-injection resistance, the
Golden Set/Behavioral Coverage eval suites) was built and is only proven
true against Claude. OpenAICompatibleLLMClient below is an unvalidated
extension point, not a real implementation -- see its own docstring.
Before ANY non-Anthropic model is used in a clinical-adjacent context (real
or synthetic patient data, any USERS.md use case), evals/unit_tests.py AND
evals/cases.py must be re-run against that specific model and must match
the pass rates documented for Claude (currently 32/32 and 18/18, 100%/100%
gate) -- not optional fine print, see README.md's Model provider section
and ARCHITECTURE.md's Known tradeoffs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.config import Settings

# Duplicated from app/main.py's own _READINESS_TIMEOUT_S (2.5s), not
# imported from there -- main.py's constant is shared across three
# unrelated dependency checks (OpenEMR, Anthropic, Langfuse); this one is
# scoped to just the LLM client's own readiness check, so a future change
# to one of the other two checks' budget doesn't silently change this one.
_READINESS_TIMEOUT_S = 2.5


# Errors the model provider itself raises: a rejected key (401), rate limit (429), overload/5xx, a dropped
# connection, a timeout. They are outages of a dependency, not bugs in this app, and must reach the resident as a
# plain "temporarily unavailable" message, never as a 500 stack trace.
PROVIDER_ERRORS: tuple[type[BaseException], ...] = (anthropic.APIError,)


def provider_error_summary(exc: BaseException) -> dict[str, Any]:
    """Loggable facts about a provider error: its type and HTTP status. Never its message or body."""
    return {"error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}


@dataclass
class LLMToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMTurnResponse:
    text: str
    tool_calls: list[LLMToolCall]
    input_tokens: int
    output_tokens: int
    # The raw, provider-native assistant content (for Anthropic: the SDK's
    # own response.content list of ContentBlock objects) -- NOT pre-
    # serialized to a jsonable shape here. Serves two purposes, both
    # requiring the true raw content rather than just the extracted
    # text/tool_calls above: (1) app/agent.py's Langfuse tracing still
    # calls its own _blocks_to_jsonable() on this field, exactly as it
    # did on response.content before this abstraction existed; (2)
    # LLMClient.append_assistant_turn() below re-appends this exact value
    # into the conversation history, so a later round's request carries
    # the identical assistant-turn shape the provider itself returned
    # (verbatim behavior -- extracting only text/tool_calls and
    # reconstructing a synthetic assistant turn from them would silently
    # drop any other content-block type Anthropic ever returns, e.g.
    # extended-thinking blocks, even though nothing in this codebase
    # currently enables that feature).
    raw_for_trace: Any
    # "end_turn"/"max_tokens"/"tool_use"/etc. (the SDK's own StopReason
    # literal) -- added after finding several llm_call events landing on
    # output_tokens exactly equal to max_tokens (1024), a statistically
    # implausible coincidence for a natural stop. Never checked before
    # this: a genuinely truncated draft (cut off mid-sentence) could reach
    # the resident with no signal anything was cut short. Defaulted so
    # every pre-existing fake/test LLMTurnResponse construction (none of
    # which cares about truncation) doesn't need updating.
    stop_reason: str | None = "end_turn"
    # The provider's content-block types in order (e.g. ["thinking", "text"]),
    # logged on every llm_call. Added after a turn spent its whole 4096-token
    # output allowance and returned NO text: with `thinking` omitted,
    # claude-sonnet-5 runs adaptive thinking, whose tokens count against
    # max_tokens, and nothing logged which block types a response held.
    content_block_types: list[str] = field(default_factory=list)
    # Total characters of visible thinking text in the response. The API's
    # usage object has no separate thinking-token count (thinking tokens are
    # folded into output_tokens), so this is the closest per-call signal of
    # how much of output_tokens went to reasoning rather than the answer. 0
    # for a response with no thinking blocks, and for `display: omitted`.
    thinking_chars: int = 0


# Passed as create_message(thinking=...) by every call site whose output is a
# forced tool call or a one-line verdict (routing, structuring, query
# extraction, the eval judge). With `thinking` omitted, claude-sonnet-5 runs
# adaptive thinking, and thinking tokens count against max_tokens -- so a
# small max_tokens can be spent on reasoning before the answer is written.
# Forced tool_choice is also incompatible with thinking, so these sites gain
# nothing from it.
THINKING_DISABLED: dict[str, str] = {"type": "disabled"}


class TruncatedOutputError(RuntimeError):
    """A structured (tool-call) response hit max_tokens. Its JSON is cut off
    mid-object, so any fields after the cut are missing while what parsed can
    still be schema-valid -- callers must treat it as a failure, never as a
    shorter-but-complete result."""


def raise_if_truncated(response: LLMTurnResponse, what: str) -> None:
    if response.stop_reason == "max_tokens":
        raise TruncatedOutputError(
            f"{what} hit max_tokens (output_tokens={response.output_tokens}, "
            f"content_block_types={response.content_block_types})"
        )


def llm_call_meta(response: LLMTurnResponse) -> dict[str, Any]:
    """The per-call diagnostics every llm_call event carries, as kwargs for
    TurnTrace.finish_llm_call -- one place, so no call site can forget one."""
    return {
        "stop_reason": response.stop_reason,
        "content_block_types": response.content_block_types,
        "thinking_chars": response.thinking_chars,
    }


class LLMClient(Protocol):
    """The interface app/agent.py's run_turn() drives every LLM round
    through. `messages` is always the same provider-native list app/agent.py
    itself owns and passes straight through -- an LLMClient implementation
    reads/appends to it in whatever shape its own provider needs; run_turn()
    never inspects its contents directly."""

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[Any],
        max_tokens: int,
        tool_choice: dict | None = None,
        thinking: dict | None = None,
    ) -> LLMTurnResponse: ...

    def append_assistant_turn(self, messages: list[Any], response: LLMTurnResponse) -> None: ...

    def append_tool_results(self, messages: list[Any], results: list[tuple[str, str]]) -> None:
        """results: (tool_use_id, content_text) pairs, one per tool call
        this round, in the same order app/agent.py dispatched them --
        content_text is already the final, already-<retrieved_patient_data>-
        wrapped string (app/agent.py's _wrap_retrieved_data), not something
        this method needs to further transform."""
        ...

    def check_ready(self) -> bool: ...


class AnthropicLLMClient:
    """Native Anthropic implementation -- today's app/agent.py tool-calling
    logic and app/main.py's _check_anthropic() readiness check, moved here
    verbatim behind the LLMClient interface, not forced through a generic
    adapter. The only implementation this project's safety behavior has
    ever been validated against (see this module's own docstring)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        # ANTHROPIC_MAX_RETRIES: the SDK's own retry budget for 429/5xx/
        # connection errors (exponential backoff, honors retry-after).
        # Unset keeps the SDK default, so production behavior is unchanged;
        # the eval runner raises it so rate limits under concurrency are
        # absorbed instead of failing cases.
        client_kwargs: dict[str, Any] = {}
        max_retries = os.environ.get("ANTHROPIC_MAX_RETRIES", "").strip()
        if max_retries:
            client_kwargs["max_retries"] = int(max_retries)
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, **client_kwargs)

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[Any],
        max_tokens: int,
        tool_choice: dict | None = None,
        thinking: dict | None = None,
    ) -> LLMTurnResponse:
        kwargs: dict[str, Any] = {}
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        # None leaves the provider default (adaptive thinking) untouched.
        if thinking is not None:
            kwargs["thinking"] = thinking
        response = self._client.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            **kwargs,
        )
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")
        return LLMTurnResponse(
            text=text,
            tool_calls=[LLMToolCall(id=b.id, name=b.name, input=b.input) for b in tool_use_blocks],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw_for_trace=response.content,
            stop_reason=response.stop_reason,
            content_block_types=[b.type for b in response.content],
            thinking_chars=sum(len(getattr(b, "thinking", "") or "") for b in response.content if b.type == "thinking"),
        )

    def append_assistant_turn(self, messages: list[Any], response: LLMTurnResponse) -> None:
        messages.append({"role": "assistant", "content": response.raw_for_trace})

    def append_tool_results(self, messages: list[Any], results: list[tuple[str, str]]) -> None:
        tool_results_content = [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": content_text}
            for tool_use_id, content_text in results
        ]
        messages.append({"role": "user", "content": tool_results_content})

    def check_ready(self) -> bool:
        """A real API call that validates the key and reachability without
        burning a completion -- models.list() is a metadata endpoint, not a
        message generation."""
        try:
            client = anthropic.Anthropic(api_key=self._settings.anthropic_api_key, timeout=_READINESS_TIMEOUT_S)
            client.models.list(limit=1)
            return True
        except Exception:  # noqa: BLE001 -- any failure here means "not ready", full stop
            return False


class OpenAICompatibleLLMClient:
    """UNVALIDATED EXTENSION POINT -- not implemented, not tested against
    any real self-hosted model (Ollama, vLLM, LM Studio, or otherwise).

    This class exists so build_llm_client()'s LLM_PROVIDER=openai_compatible
    seam is real and testable (a factory call actually returns an instance
    of this class, wired through app/config.py's Settings), not so it can be
    used. Every method below raises NotImplementedError with the same
    message, deliberately -- not a partial/best-effort implementation --
    so it is not possible to accidentally run this against real patient
    data before both (1) a real implementation exists and (2) that
    implementation has been proven against the eval suite below.

    Before a real implementation of this class is used in any
    clinical-adjacent context -- real or synthetic patient data, any
    USERS.md use case -- evals/unit_tests.py AND evals/cases.py must be
    re-run against the target model and must match the pass rates this
    project documents for Claude (currently 32/32 and 18/18, 100%/100%
    gate). A model that passes fewer of these is not validated for this
    use, full stop. See ARCHITECTURE.md's Known tradeoffs and README.md's
    Model provider section -- this docstring intentionally cross-references
    rather than duplicates that fuller explanation."""

    _NOT_IMPLEMENTED_MESSAGE = (
        "OpenAICompatibleLLMClient is an unvalidated extension point, not a real "
        "implementation. Before use in any clinical-adjacent context, evals/unit_tests.py "
        "and evals/cases.py must be re-run against the target model and must match the "
        "pass rates documented for Claude."
    )

    def __init__(self, settings: Settings):
        self._settings = settings

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[Any],
        max_tokens: int,
        tool_choice: dict | None = None,
        thinking: dict | None = None,
    ) -> LLMTurnResponse:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def append_assistant_turn(self, messages: list[Any], response: LLMTurnResponse) -> None:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def append_tool_results(self, messages: list[Any], results: list[tuple[str, str]]) -> None:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def check_ready(self) -> bool:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)


def build_llm_client(settings: Settings) -> LLMClient:
    """Resolves settings.llm_provider to a concrete LLMClient. "anthropic"
    (or unset, matching Settings.llm_provider's own "anthropic" default in
    app/config.py) is the only validated path -- see this module's
    docstring. "openai_compatible" resolves to a real, swappable seam
    (OpenAICompatibleLLMClient) rather than failing here, so the
    abstraction itself is testable end-to-end -- but that class raises
    NotImplementedError on every real method, so nothing can actually run
    against it yet. Any other value is a configuration error, not silently
    defaulted to Anthropic."""
    provider = settings.llm_provider or "anthropic"
    if provider == "anthropic":
        return AnthropicLLMClient(settings)
    if provider == "openai_compatible":
        return OpenAICompatibleLLMClient(settings)
    raise NotImplementedError(
        f"Unknown LLM_PROVIDER={provider!r}. Supported values: 'anthropic' (default, the "
        "only validated path) or 'openai_compatible' (an unvalidated extension point -- "
        "see OpenAICompatibleLLMClient's docstring)."
    )


_TRUNCATION_NOTE = (
    "\n\n[Note: this response may have been cut off before finishing -- ask a follow-up "
    "if anything seems incomplete.]"
)


_EMPTY_TRUNCATION_NOTE = (
    "[Note: the model used its entire output allowance before writing any answer text, so no answer "
    "was produced -- ask again, ideally with a narrower question.]"
)


def disclose_if_truncated(text: str, stop_reason: str | None) -> str:
    """Appends an honest disclosure if the model's response was cut off by
    hitting max_tokens rather than finishing naturally. Found live:
    several llm_call events landed on output_tokens exactly equal to
    max_tokens (1024) -- a statistically implausible coincidence for a
    natural stop -- confirmed as real truncation once stop_reason started
    being captured. A resident must never be shown a cut-off clinical
    answer with no signal it's incomplete; called at every site that
    turns a raw LLM response into resident-facing prose (app/agent.py's
    chart_data_worker draft, app/graph/supervisor.py's synthesis draft)."""
    if stop_reason != "max_tokens":
        return text
    if not text.strip():
        # Nothing but disclosure would otherwise reach the resident (the
        # generic note alone reads like a complete, empty answer).
        return _EMPTY_TRUNCATION_NOTE
    return text + _TRUNCATION_NOTE
