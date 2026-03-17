"""Unit tests for QLLMWrapper — isolation, schema validation, and error handling.

Tests verify:
- Successful extraction returning a validated Pydantic model
- NotEnoughInformationError raised when have_enough_information=False
- Pydantic ValidationError propagated on malformed LLM response
- Isolation: QLLMWrapper has no tool-related parameters or attributes
- Backend-agnosticism: Claude and Gemini adapter mocks both work
- Prompt content: untrusted data and schema present, no tool definitions
- Generic typing: two different schemas produce correctly typed return values
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from camel.llm.exceptions import NotEnoughInformationError
from camel.llm.protocols import Message
from camel.llm.qllm import QLLMWrapper, make_qllm_wrapper
from camel.llm.schemas import QResponse

# ---------------------------------------------------------------------------
# Test schemas
# ---------------------------------------------------------------------------


class EmailExtraction(QResponse):
    sender: str
    subject: str


class ProductInfo(QResponse):
    name: str
    price: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_claude_mock() -> AsyncMock:
    """Return an AsyncMock named after ClaudeBackend (no real import needed)."""
    mock = AsyncMock(name="ClaudeBackend")
    mock.__class__.__name__ = "ClaudeBackend"
    return mock


def _make_gemini_mock() -> AsyncMock:
    """Return an AsyncMock named after GeminiBackend (no real import needed)."""
    mock = AsyncMock(name="GeminiBackend")
    mock.__class__.__name__ = "GeminiBackend"
    return mock


# ---------------------------------------------------------------------------
# Test 1: Successful extraction — Claude adapter mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_extraction_claude_adapter() -> None:
    """QLLMWrapper.extract returns a validated EmailExtraction via a Claude mock."""
    expected = EmailExtraction(
        have_enough_information=True,
        sender="alice@example.com",
        subject="Project update",
    )
    backend = _make_claude_mock()
    backend.structured_complete.return_value = expected

    wrapper = QLLMWrapper(backend)
    result = await wrapper.extract(
        "From: alice@example.com\nSubject: Project update\nBody: Done.",
        EmailExtraction,
    )

    assert isinstance(result, EmailExtraction)
    assert result.sender == "alice@example.com"
    assert result.subject == "Project update"
    assert result.have_enough_information is True
    backend.structured_complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: Successful extraction — Gemini adapter mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_extraction_gemini_adapter() -> None:
    """QLLMWrapper.extract returns a validated ProductInfo via a Gemini mock."""
    expected = ProductInfo(
        have_enough_information=True,
        name="Widget Pro",
        price=29.99,
    )
    backend = _make_gemini_mock()
    backend.structured_complete.return_value = expected

    wrapper = make_qllm_wrapper(backend)
    result = await wrapper.extract("Widget Pro costs $29.99.", ProductInfo)

    assert isinstance(result, ProductInfo)
    assert result.name == "Widget Pro"
    assert result.price == 29.99
    backend.structured_complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 3: NotEnoughInformationError raised when have_enough_information=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_enough_information_error_raised() -> None:
    """NotEnoughInformationError is raised (not swallowed) when the model
    signals it lacked sufficient context."""
    partial = EmailExtraction(
        have_enough_information=False,
        sender="",
        subject="",
    )
    backend = _make_claude_mock()
    backend.structured_complete.return_value = partial

    wrapper = QLLMWrapper(backend)
    with pytest.raises(NotEnoughInformationError) as exc_info:
        await wrapper.extract("This content has no useful data.", EmailExtraction)

    err = exc_info.value
    assert err.schema_type is EmailExtraction
    assert err.partial_response is partial
    assert "EmailExtraction" in str(err)


# ---------------------------------------------------------------------------
# Test 4: Pydantic ValidationError propagated on malformed backend response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_error_propagated() -> None:
    """A ValidationError from the backend (malformed response) bubbles up
    through QLLMWrapper.extract unchanged."""
    # Manufacture a real ValidationError by feeding an invalid payload.
    try:
        EmailExtraction.model_validate({"have_enough_information": True})
        # Missing `sender` and `subject` → ValidationError
    except ValidationError as exc:
        canned_error = exc
    else:
        pytest.fail("Expected ValidationError was not raised during setup")

    backend = _make_claude_mock()
    backend.structured_complete.side_effect = canned_error

    wrapper = QLLMWrapper(backend)
    with pytest.raises(ValidationError):
        await wrapper.extract("some raw data", EmailExtraction)


# ---------------------------------------------------------------------------
# Test 5: Isolation — no tool-related parameters on constructor or extract()
# ---------------------------------------------------------------------------


def test_isolation_constructor_has_no_tool_parameters() -> None:
    """QLLMWrapper.__init__ must not accept tool_signatures, tools, or
    functions parameters — these paths simply do not exist in the API."""
    sig = inspect.signature(QLLMWrapper.__init__)
    banned = {"tool_signatures", "tools", "functions", "tool_definitions"}
    present = set(sig.parameters) & banned
    assert not present, (
        f"QLLMWrapper.__init__ must not expose tool parameters; found: {present}"
    )


def test_isolation_extract_has_no_tool_parameters() -> None:
    """QLLMWrapper.extract must not accept tool_signatures, tools, or
    functions parameters."""
    sig = inspect.signature(QLLMWrapper.extract)
    banned = {"tool_signatures", "tools", "functions", "tool_definitions"}
    present = set(sig.parameters) & banned
    assert not present, (
        f"QLLMWrapper.extract must not expose tool parameters; found: {present}"
    )


def test_isolation_instance_has_no_tool_signatures_attribute() -> None:
    """A constructed QLLMWrapper instance must not carry a tool_signatures
    attribute (neither class-level nor instance-level)."""
    backend = MagicMock()
    wrapper = QLLMWrapper(backend)
    assert not hasattr(wrapper, "tool_signatures"), (
        "QLLMWrapper instance must not have a tool_signatures attribute"
    )
    assert not hasattr(QLLMWrapper, "tool_signatures"), (
        "QLLMWrapper class must not have a tool_signatures attribute"
    )


# ---------------------------------------------------------------------------
# Test 6: Prompt contains untrusted data and schema; no tool keys in messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_contains_data_and_schema_no_tool_definitions() -> None:
    """Messages forwarded to the backend must contain the untrusted data and
    the schema name/description, but must not carry tool-related keys."""
    untrusted_data = "From: bob@example.com\nSubject: Budget review"
    captured: list[list[Message]] = []

    async def capturing_backend(
        messages: list[Message], schema: type[Any]
    ) -> EmailExtraction:
        captured.append(messages)
        return EmailExtraction(
            have_enough_information=True,
            sender="bob@example.com",
            subject="Budget review",
        )

    backend = MagicMock()
    backend.structured_complete = capturing_backend

    wrapper = QLLMWrapper(backend)
    await wrapper.extract(untrusted_data, EmailExtraction)

    assert captured, "Backend was never called"
    messages = captured[0]

    # Concatenate all message content for substring checks.
    all_content = "\n".join(m.get("content", "") for m in messages)

    # The untrusted data must appear in the prompt.
    assert untrusted_data in all_content, "Untrusted data missing from prompt"

    # The schema class name must appear (so the model knows what to populate).
    assert "EmailExtraction" in all_content, "Schema name missing from prompt"

    # No message dict should carry tool-related keys.
    tool_keys = {"tools", "tool_choice", "functions", "tool_definitions"}
    for msg in messages:
        present = tool_keys & set(msg.keys())
        assert not present, (
            f"Message dict must not contain tool keys; found {present} in {msg!r}"
        )


# ---------------------------------------------------------------------------
# Test 7: Generic typing — two different schemas produce correctly typed values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_typing_two_schemas_return_correct_types() -> None:
    """Calling extract() with two different QResponse subclasses returns
    correctly typed instances of each."""
    email_result = EmailExtraction(
        have_enough_information=True,
        sender="carol@example.com",
        subject="Hello",
    )
    product_result = ProductInfo(
        have_enough_information=True,
        name="Gadget X",
        price=49.99,
    )

    backend = _make_gemini_mock()
    backend.structured_complete.side_effect = [email_result, product_result]

    wrapper = QLLMWrapper(backend)

    r1 = await wrapper.extract("email content", EmailExtraction)
    r2 = await wrapper.extract("product content", ProductInfo)

    # Runtime type checks.
    assert isinstance(r1, EmailExtraction)
    assert not isinstance(r1, ProductInfo)
    assert isinstance(r2, ProductInfo)
    assert not isinstance(r2, EmailExtraction)

    # Field value checks.
    assert r1.sender == "carol@example.com"
    assert r1.subject == "Hello"
    assert r2.name == "Gadget X"
    assert r2.price == 49.99

    assert backend.structured_complete.await_count == 2
