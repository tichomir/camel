"""Isolation contract enforcement tests for PLLMWrapper.

This module is dedicated to formally verifying the P-LLM isolation contract:

1. No tool return value (CaMeLValue wrapping str, dict, list, Pydantic model)
   appears in any LLM message constructed by PLLMWrapper.
2. Retry prompt after a trusted-source error (CodeBlockNotFoundError) includes
   the error type name in the feedback.
3. Retry prompt after an untrusted-source error (SyntaxError) includes only
   exception type and code location — never the error message body, which may
   echo back attacker-controlled content.
4. parse_code_plan raises CodeBlockNotFoundError for: responses with no fenced
   block, responses with an empty fenced block, and responses with only
   non-Python fenced blocks.
5. generate_plan raises PLLMRetryExhaustedError after exactly 10 failed
   attempts.

All backend interactions use mocked LLMBackend; no real API calls are made.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from camel.llm.p_llm import (
    CodeBlockNotFoundError,
    CodePlan,
    PLLMIsolationError,
    PLLMRetryExhaustedError,
    PLLMWrapper,
    ToolSignature,
)
from camel.value import CaMeLValue, Public

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(responses: list[str]) -> MagicMock:
    """Return a mock LLMBackend whose generate() yields successive responses."""
    backend = MagicMock()
    backend.generate = AsyncMock(side_effect=responses)
    return backend


def _wrap_plan(code: str) -> str:
    """Wrap *code* in a Markdown python fence as a P-LLM would produce."""
    return f"```python\n{code}\n```"


def _make_camel_value(raw: Any) -> CaMeLValue:
    """Return a CaMeLValue wrapping *raw* as if it came from a tool call."""
    return CaMeLValue(
        value=raw,
        sources=frozenset({"tool_result"}),
        inner_source=None,
        readers=Public,
    )


# Pydantic model representing a realistic tool return value.
class _ToolOutput(BaseModel):
    """Simulated structured tool return value."""

    result: str
    score: float


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def wrapper() -> PLLMWrapper:
    """Return a PLLMWrapper backed by a stub MagicMock backend."""
    return PLLMWrapper(backend=MagicMock())


# ---------------------------------------------------------------------------
# 1. Parameterized: no tool return value in any constructed message
# ---------------------------------------------------------------------------


_RAW_VALUES: list[tuple[Any, str]] = [
    ("sensitive tool output string", "str"),
    ({"key": "value", "nested": {"x": 1}}, "dict"),
    (["item_a", "item_b", "item_c"], "list"),
    (_ToolOutput(result="success", score=0.97), "pydantic_model"),
]


@pytest.mark.parametrize("raw_value,type_label", _RAW_VALUES)
def test_camel_value_as_user_query_raises_isolation_error(
    raw_value: Any, type_label: str
) -> None:
    """PLLMIsolationError is raised and no message is constructed.

    Parameterized across CaMeLValue wrapping str, dict, list, and a Pydantic
    model — all realistic tool return value types.  The backend must never
    be called when the guard fires.
    """
    backend = _make_backend([])
    w = PLLMWrapper(backend)
    camel_val = _make_camel_value(raw_value)

    with pytest.raises(PLLMIsolationError) as exc_info:
        asyncio.run(
            w.generate_plan(
                user_query=camel_val,  # type: ignore[arg-type]
                tool_signatures=[],
            )
        )

    # Guard message must mention the offending parameter
    assert "user_query" in str(exc_info.value)
    # Backend must not have been invoked — no message was built
    backend.generate.assert_not_awaited()


@pytest.mark.parametrize("raw_value,type_label", _RAW_VALUES)
def test_camel_value_as_context_value_raises_isolation_error(
    raw_value: Any, type_label: str
) -> None:
    """PLLMIsolationError is raised for CaMeLValue in user_context.

    Parameterized across the same set of raw value types.  The context key
    name must appear in the exception message so callers can diagnose which
    key caused the violation.
    """
    backend = _make_backend([])
    w = PLLMWrapper(backend)
    camel_val = _make_camel_value(raw_value)

    with pytest.raises(PLLMIsolationError) as exc_info:
        asyncio.run(
            w.generate_plan(
                user_query="legitimate user request",
                tool_signatures=[],
                user_context={"injected_key": camel_val},  # type: ignore[dict-item]
            )
        )

    assert "injected_key" in str(exc_info.value)
    backend.generate.assert_not_awaited()


@pytest.mark.parametrize("raw_value,type_label", _RAW_VALUES)
def test_no_camel_value_repr_in_messages_on_valid_call(
    raw_value: Any, type_label: str
) -> None:
    """CaMeLValue repr never appears in any message sent to the backend.

    Simulates a successful two-attempt run (first response missing a code
    block, second succeeds) and inspects every message content string across
    both calls.  Parameterized to ensure the assertion holds regardless of
    what raw value type a hypothetical tool might return.
    """
    first_bad = "I forgot the code block."
    good = _wrap_plan("result = compute()")
    backend = MagicMock()
    all_messages: list[dict[str, Any]] = []

    async def _capture(messages: list[dict[str, Any]], **kwargs: Any) -> str:
        all_messages.extend(messages)
        idx = backend.generate.await_count - 1
        return [first_bad, good][idx]

    backend.generate = AsyncMock(side_effect=_capture)
    w = PLLMWrapper(backend)

    asyncio.run(
        w.generate_plan(
            user_query="do something useful",
            tool_signatures=[
                ToolSignature(
                    name="compute",
                    signature="",
                    return_type="Result",
                    description="Run computation.",
                )
            ],
            user_context={"date": "2026-03-17"},
        )
    )

    assert len(all_messages) > 0, "Expected at least one message to be captured"
    for msg in all_messages:
        content = msg.get("content", "")
        assert isinstance(content, str), (
            f"Message content must be a plain str, got {type(content)!r}"
        )
        assert "CaMeLValue" not in content, (
            f"CaMeLValue repr leaked into {msg['role']!r} message "
            f"(raw_value type: {type_label})"
        )


# ---------------------------------------------------------------------------
# 2. Trusted-source error retry: error type present in feedback
# ---------------------------------------------------------------------------


def test_trusted_error_retry_prompt_includes_error_type() -> None:
    """Retry feedback after CodeBlockNotFoundError contains the error type name.

    CodeBlockNotFoundError originates from the P-LLM's own output-format
    failure (a trusted source).  The retry prompt must convey at minimum
    the exception class name so the model can diagnose and correct its output.
    """
    first_bad = "Here is a plain text response without any code block."
    good = _wrap_plan("x = get_value()")
    backend = MagicMock()
    captured: list[list[dict[str, Any]]] = []

    async def _capture(messages: list[dict[str, Any]], **kwargs: Any) -> str:
        captured.append(list(messages))
        return [first_bad, good][len(captured) - 1]

    backend.generate = AsyncMock(side_effect=_capture)
    w = PLLMWrapper(backend)

    asyncio.run(w.generate_plan("fetch a value", []))

    assert len(captured) == 2, "Expected exactly two backend calls"
    retry_messages = captured[1]
    assert len(retry_messages) == 4, (
        "Retry call must have [system, user, assistant, user(error)] messages"
    )

    error_feedback = retry_messages[3]["content"]
    # Error type must be present — this is the core diagnostic information
    assert "CodeBlockNotFoundError" in error_feedback
    # Structured format markers must be present
    assert "Error type:" in error_feedback


# ---------------------------------------------------------------------------
# 3. Untrusted-source error retry: message body is redacted
# ---------------------------------------------------------------------------


def test_untrusted_error_retry_prompt_redacts_message_body() -> None:
    """Retry feedback after SyntaxError includes type+location but NOT the body.

    SyntaxError may echo back attacker-controlled content embedded as string
    literals in the generated code.  The _build_error_feedback function must
    redact the exception message text, retaining only the exception class name
    and the line number.
    """
    # Deliberately invalid syntax that Python will produce an error message for
    bad_code = "x = (\ny = 1"  # SyntaxError: '(' was never closed
    good = _wrap_plan("x = 1")
    backend = MagicMock()
    captured: list[list[dict[str, Any]]] = []

    async def _capture(messages: list[dict[str, Any]], **kwargs: Any) -> str:
        captured.append(list(messages))
        return [_wrap_plan(bad_code), good][len(captured) - 1]

    backend.generate = AsyncMock(side_effect=_capture)
    w = PLLMWrapper(backend)

    asyncio.run(w.generate_plan("do something", []))

    error_feedback = captured[1][3]["content"]

    # Exception type name MUST be present
    assert "SyntaxError" in error_feedback
    # Structured keys MUST be present
    assert "Error type:" in error_feedback
    assert "Location:" in error_feedback
    # Line number MUST be present (covers the lineno redaction rule)
    import re

    assert re.search(r"line \d+", error_feedback), (
        "Retry feedback for SyntaxError must include a line number"
    )
    # Python error message body MUST NOT be present — common phrases from CPython
    error_feedback_lower = error_feedback.lower()
    for forbidden_phrase in ("was never closed", "invalid syntax", "unexpected eof"):
        assert forbidden_phrase not in error_feedback_lower, (
            f"Error message body {forbidden_phrase!r} leaked into retry feedback"
        )


def test_untrusted_error_retry_prompt_contains_only_type_and_location() -> None:
    """Retry feedback is structurally minimal: only type and location lines.

    Verifies the full redaction contract: no exception attributes, no
    traceback text, and no Python repr beyond type name + line.
    """
    bad_code = "if True\n    pass"  # SyntaxError missing colon
    good = _wrap_plan("pass_val = noop()")
    backend = MagicMock()
    captured: list[list[dict[str, Any]]] = []

    async def _capture(messages: list[dict[str, Any]], **kwargs: Any) -> str:
        captured.append(list(messages))
        return [_wrap_plan(bad_code), good][len(captured) - 1]

    backend.generate = AsyncMock(side_effect=_capture)
    w = PLLMWrapper(backend, max_retries=5)

    asyncio.run(w.generate_plan("do something", []))

    error_feedback = captured[1][3]["content"]
    # Raw Python SyntaxError details must be absent
    assert "Traceback" not in error_feedback
    assert "File " not in error_feedback


# ---------------------------------------------------------------------------
# 4. parse_code_plan raises CodeBlockNotFoundError (ParseError contract)
# ---------------------------------------------------------------------------


def test_parse_code_plan_raises_for_no_fenced_block(wrapper: PLLMWrapper) -> None:
    """parse_code_plan raises CodeBlockNotFoundError when no fence is present."""
    with pytest.raises(CodeBlockNotFoundError):
        wrapper.parse_code_plan(
            "Here is my response. I will send the email. No code block."
        )


def test_parse_code_plan_raises_for_empty_fenced_block(wrapper: PLLMWrapper) -> None:
    """parse_code_plan raises CodeBlockNotFoundError for an empty fenced block."""
    with pytest.raises(CodeBlockNotFoundError):
        wrapper.parse_code_plan("```python\n   \n   \n```")


def test_parse_code_plan_raises_for_non_python_fenced_block(
    wrapper: PLLMWrapper,
) -> None:
    """parse_code_plan raises CodeBlockNotFoundError for a non-Python fenced block.

    The FENCE_PATTERN only matches ```python or bare ``` fences.  A block
    tagged with a different language (e.g. javascript, json, bash) must not
    be accepted, because only Python-tagged blocks are valid execution plans.
    """
    with pytest.raises(CodeBlockNotFoundError):
        wrapper.parse_code_plan(
            "Here is your plan:\n\n```javascript\nconsole.log('hello');\n```\n"
        )


def test_parse_code_plan_raises_for_json_fenced_block(wrapper: PLLMWrapper) -> None:
    """parse_code_plan raises CodeBlockNotFoundError for a ```json block."""
    with pytest.raises(CodeBlockNotFoundError):
        wrapper.parse_code_plan('```json\n{"action": "send_email"}\n```')


def test_parse_code_plan_raises_for_bash_fenced_block(wrapper: PLLMWrapper) -> None:
    """parse_code_plan raises CodeBlockNotFoundError for a ```bash block."""
    with pytest.raises(CodeBlockNotFoundError):
        wrapper.parse_code_plan("```bash\necho hello\n```")


def test_parse_code_plan_returns_code_plan_for_python_fence(
    wrapper: PLLMWrapper,
) -> None:
    """parse_code_plan returns a CodePlan for a valid ```python block (control)."""
    result = wrapper.parse_code_plan("```python\nresult = get_email()\n```")
    assert isinstance(result, CodePlan)
    assert result.source == "result = get_email()"


# ---------------------------------------------------------------------------
# 5. generate_plan raises PLLMRetryExhaustedError after exactly 10 attempts
# ---------------------------------------------------------------------------


def test_generate_plan_raises_after_exactly_10_attempts() -> None:
    """generate_plan raises PLLMRetryExhaustedError after exactly 10 attempts.

    The default max_retries is 10 (per PRD §6.1).  The wrapper must exhaust
    all retries before raising, and the exception must record the correct
    attempt count.
    """
    # 10 responses, all missing a code block
    backend = _make_backend(["no code block at all"] * 10)
    w = PLLMWrapper(backend)  # default max_retries=10

    with pytest.raises(PLLMRetryExhaustedError) as exc_info:
        asyncio.run(
            w.generate_plan(
                user_query="do something",
                tool_signatures=[],
            )
        )

    assert exc_info.value.attempts == 10
    assert backend.generate.await_count == 10


def test_generate_plan_raises_retry_exhausted_with_correct_count() -> None:
    """PLLMRetryExhaustedError.attempts matches the configured max_retries.

    Verifies the contract holds for a non-default max_retries value (3),
    confirming the wrapper does not attempt fewer or more calls than
    configured.
    """
    backend = _make_backend(["still no block"] * 3)
    w = PLLMWrapper(backend, max_retries=3)

    with pytest.raises(PLLMRetryExhaustedError) as exc_info:
        asyncio.run(w.generate_plan("query", []))

    assert exc_info.value.attempts == 3
    assert backend.generate.await_count == 3


def test_generate_plan_succeeds_on_10th_attempt_does_not_raise() -> None:
    """generate_plan succeeds on the final allowed attempt without raising.

    Boundary case: 9 bad responses followed by one valid response on attempt
    10 must return a CodePlan, not raise PLLMRetryExhaustedError.
    """
    good = _wrap_plan("result = compute()")
    responses = ["no block"] * 9 + [good]
    backend = _make_backend(responses)
    w = PLLMWrapper(backend)  # max_retries=10

    plan = asyncio.run(w.generate_plan("compute", []))

    assert isinstance(plan, CodePlan)
    assert plan.source == "result = compute()"
    assert backend.generate.await_count == 10


def test_generate_plan_raises_on_11th_attempt_would_exhaust() -> None:
    """generate_plan with max_retries=1 raises after a single failed attempt."""
    backend = _make_backend(["no block"])
    w = PLLMWrapper(backend, max_retries=1)

    with pytest.raises(PLLMRetryExhaustedError) as exc_info:
        asyncio.run(w.generate_plan("query", []))

    assert exc_info.value.attempts == 1
    assert backend.generate.await_count == 1
