"""Unit tests for LLMBackend Protocol, LLMBackendError, and get_backend factory.

Covers:
1.  ClaudeBackend.generate() — happy path (delegates to complete).
2.  ClaudeBackend.generate() — SDK error wrapped as LLMBackendError.
3.  ClaudeBackend.generate_structured() — tool_use block → validated schema.
4.  ClaudeBackend.generate_structured() — SDK error wrapped as LLMBackendError.
5.  ClaudeBackend.generate_structured() — system messages extracted.
6.  GeminiBackend.generate() — happy path (delegates to complete).
7.  GeminiBackend.generate() — SDK error wrapped as LLMBackendError.
8.  GeminiBackend.generate_structured() — GenerationConfig set correctly.
9.  GeminiBackend.generate_structured() — SDK error wrapped as LLMBackendError.
10. get_backend('claude') — returns ClaudeBackend satisfying LLMBackend.
11. get_backend('gemini') — returns GeminiBackend satisfying LLMBackend.
12. get_backend('unknown') — raises ValueError.
13. LLMBackend Protocol isinstance check with duck-typed mock.
14. P-LLM isolation — generate() does not accept CaMeLValue (type-level guard).

All SDK clients are replaced with unittest.mock objects; no real API calls
are made.
"""

from __future__ import annotations

import json
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import BaseModel

from camel.llm.backend import LLMBackend, LLMBackendError, get_backend

# ---------------------------------------------------------------------------
# Shared test schema (plain BaseModel — not QResponse)
# ---------------------------------------------------------------------------


class _SummarySchema(BaseModel):
    """Minimal BaseModel subclass used as the generate_structured schema."""

    title: str
    word_count: int


# ---------------------------------------------------------------------------
# Helpers — Anthropic mock factory
# ---------------------------------------------------------------------------


def _make_anthropic_mock() -> tuple[MagicMock, MagicMock]:
    """Return (mock_anthropic_module, mock_async_client)."""
    mock_anthropic = MagicMock()
    mock_client = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client
    return mock_anthropic, mock_client


def _make_text_block(text: str) -> MagicMock:
    """Return a mock Anthropic content block with type='text'."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(
    name: str = "extract_structured_data",
    input_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Return a mock Anthropic content block with type='tool_use'."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_data or {}
    return block


# ---------------------------------------------------------------------------
# Helpers — Gemini mock factory
# ---------------------------------------------------------------------------


def _make_genai_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (mock_google, mock_genai, mock_genai_types)."""
    mock_google = MagicMock()
    mock_genai = MagicMock()
    mock_genai_types = MagicMock()
    mock_google.generativeai = mock_genai
    mock_genai.types = mock_genai_types
    return mock_google, mock_genai, mock_genai_types


# ---------------------------------------------------------------------------
# 1–2. ClaudeBackend.generate() — happy path + error wrapping
# ---------------------------------------------------------------------------


class TestClaudeBackendGenerate(unittest.IsolatedAsyncioTestCase):
    """Tests for ClaudeBackend.generate()."""

    async def test_generate_returns_text_from_api(self) -> None:
        """generate() returns the first text content block from the API."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        text_block = _make_text_block("Hello from Claude")
        mock_response = MagicMock()
        mock_response.content = [text_block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        messages = [{"role": "user", "content": "Say hello."}]
        result = await backend.generate(messages)

        self.assertEqual(result, "Hello from Claude")
        mock_client.messages.create.assert_awaited_once()

    async def test_generate_passes_kwargs_to_api(self) -> None:
        """generate() forwards extra kwargs to the underlying API call."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        mock_response = MagicMock()
        mock_response.content = [_make_text_block("ok")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        await backend.generate(
            [{"role": "user", "content": "test"}],
            temperature=0.5,
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(kwargs.get("temperature"), 0.5)

    async def test_generate_wraps_sdk_error_as_llm_backend_error(self) -> None:
        """generate() converts a native SDK exception into LLMBackendError."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        mock_client.messages.create = AsyncMock(
            side_effect=RuntimeError("network timeout")
        )

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        with self.assertRaises(LLMBackendError) as ctx:
            await backend.generate([{"role": "user", "content": "hi"}])

        self.assertIn("network timeout", str(ctx.exception))
        self.assertIsInstance(ctx.exception.cause, RuntimeError)

    async def test_generate_system_messages_extracted(self) -> None:
        """generate() sends system messages as the Anthropic 'system' param."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        mock_response = MagicMock()
        mock_response.content = [_make_text_block("plan")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        messages = [
            {"role": "system", "content": "You are a planner."},
            {"role": "user", "content": "Plan something."},
        ]
        await backend.generate(messages)

        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertIn("system", kwargs)
        self.assertIn("You are a planner.", kwargs["system"])
        roles = [m.get("role") for m in kwargs["messages"]]
        self.assertNotIn("system", roles)


# ---------------------------------------------------------------------------
# 3–5. ClaudeBackend.generate_structured() — happy path + error wrapping
# ---------------------------------------------------------------------------


class TestClaudeBackendGenerateStructured(unittest.IsolatedAsyncioTestCase):
    """Tests for ClaudeBackend.generate_structured()."""

    async def test_generate_structured_returns_validated_schema(self) -> None:
        """generate_structured() parses the tool_use block into the schema."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        block = _make_tool_use_block(
            input_data={"title": "AI Safety", "word_count": 42}
        )
        mock_response = MagicMock()
        mock_response.content = [block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        messages = [{"role": "user", "content": "Summarise."}]
        result = await backend.generate_structured(messages, _SummarySchema)

        self.assertIsInstance(result, _SummarySchema)
        self.assertEqual(result.title, "AI Safety")
        self.assertEqual(result.word_count, 42)

    async def test_generate_structured_passes_tool_choice(self) -> None:
        """generate_structured() forces tool_choice to the extraction tool."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        block = _make_tool_use_block(
            input_data={"title": "Test", "word_count": 1}
        )
        mock_response = MagicMock()
        mock_response.content = [block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        await backend.generate_structured(
            [{"role": "user", "content": "go"}],
            _SummarySchema,
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertIn("tools", kwargs)
        self.assertEqual(len(kwargs["tools"]), 1)
        self.assertIn("tool_choice", kwargs)
        self.assertEqual(kwargs["tool_choice"]["type"], "tool")

    async def test_generate_structured_extracts_system_messages(self) -> None:
        """generate_structured() moves system messages to the 'system' param."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        block = _make_tool_use_block(
            input_data={"title": "X", "word_count": 0}
        )
        mock_response = MagicMock()
        mock_response.content = [block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        messages = [
            {"role": "system", "content": "Extract data."},
            {"role": "user", "content": "Content here."},
        ]
        await backend.generate_structured(messages, _SummarySchema)

        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertIn("system", kwargs)
        self.assertIn("Extract data.", kwargs["system"])
        roles = [m.get("role") for m in kwargs["messages"]]
        self.assertNotIn("system", roles)

    async def test_generate_structured_wraps_sdk_error(self) -> None:
        """generate_structured() converts SDK exceptions into LLMBackendError."""
        from camel.llm.adapters.claude import ClaudeBackend

        mock_anthropic, mock_client = _make_anthropic_mock()
        mock_client.messages.create = AsyncMock(
            side_effect=ConnectionError("API unavailable")
        )

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = ClaudeBackend(api_key="test-key")

        with self.assertRaises(LLMBackendError) as ctx:
            await backend.generate_structured(
                [{"role": "user", "content": "test"}],
                _SummarySchema,
            )

        self.assertIn("API unavailable", str(ctx.exception))
        self.assertIsInstance(ctx.exception.cause, ConnectionError)


# ---------------------------------------------------------------------------
# 6–7. GeminiBackend.generate() — happy path + error wrapping
# ---------------------------------------------------------------------------


class TestGeminiBackendGenerate(unittest.IsolatedAsyncioTestCase):
    """Tests for GeminiBackend.generate()."""

    async def test_generate_returns_response_text(self) -> None:
        """generate() returns the text attribute of the Gemini response."""
        from camel.llm.adapters.gemini import GeminiBackend

        mock_google, mock_genai, _ = _make_genai_mocks()
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Hello from Gemini"
        mock_model.generate_content_async = AsyncMock(return_value=mock_response)
        mock_genai.GenerativeModel.return_value = mock_model

        modules = {
            "google": mock_google,
            "google.generativeai": mock_genai,
        }
        with patch.dict(sys.modules, modules):
            backend = GeminiBackend(api_key="test-key")

        result = await backend.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "Hello from Gemini")

    async def test_generate_wraps_sdk_error_as_llm_backend_error(self) -> None:
        """generate() converts a Google AI SDK exception into LLMBackendError."""
        from camel.llm.adapters.gemini import GeminiBackend

        mock_google, mock_genai, _ = _make_genai_mocks()
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(
            side_effect=OSError("quota exceeded")
        )
        mock_genai.GenerativeModel.return_value = mock_model

        modules = {
            "google": mock_google,
            "google.generativeai": mock_genai,
        }
        with patch.dict(sys.modules, modules):
            backend = GeminiBackend(api_key="test-key")

        with self.assertRaises(LLMBackendError) as ctx:
            await backend.generate([{"role": "user", "content": "test"}])

        self.assertIn("quota exceeded", str(ctx.exception))
        self.assertIsInstance(ctx.exception.cause, IOError)


# ---------------------------------------------------------------------------
# 8–9. GeminiBackend.generate_structured() — happy path + error wrapping
# ---------------------------------------------------------------------------


class TestGeminiBackendGenerateStructured(unittest.IsolatedAsyncioTestCase):
    """Tests for GeminiBackend.generate_structured()."""

    async def test_generate_structured_returns_validated_schema(self) -> None:
        """generate_structured() parses JSON response into the schema."""
        from camel.llm.adapters.gemini import GeminiBackend

        mock_google, mock_genai, mock_genai_types = _make_genai_mocks()
        mock_gen_config = MagicMock()
        mock_genai_types.GenerationConfig.return_value = mock_gen_config

        mock_model = MagicMock()
        payload = {"title": "Gemini Summary", "word_count": 99}
        mock_response = MagicMock()
        mock_response.text = json.dumps(payload)
        mock_model.generate_content_async = AsyncMock(return_value=mock_response)
        mock_genai.GenerativeModel.return_value = mock_model

        modules = {
            "google": mock_google,
            "google.generativeai": mock_genai,
            "google.generativeai.types": mock_genai_types,
        }
        with patch.dict(sys.modules, modules):
            backend = GeminiBackend(api_key="test-key")
            result = await backend.generate_structured(
                [{"role": "user", "content": "Summarise."}],
                _SummarySchema,
            )

        self.assertIsInstance(result, _SummarySchema)
        self.assertEqual(result.title, "Gemini Summary")
        self.assertEqual(result.word_count, 99)

    async def test_generate_structured_sets_response_mime_type(self) -> None:
        """generate_structured() configures response_mime_type='application/json'."""
        from camel.llm.adapters.gemini import GeminiBackend

        mock_google, mock_genai, mock_genai_types = _make_genai_mocks()
        mock_gen_config = MagicMock()
        mock_genai_types.GenerationConfig.return_value = mock_gen_config

        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps({"title": "T", "word_count": 0})
        mock_model.generate_content_async = AsyncMock(return_value=mock_response)
        mock_genai.GenerativeModel.return_value = mock_model

        modules = {
            "google": mock_google,
            "google.generativeai": mock_genai,
            "google.generativeai.types": mock_genai_types,
        }
        with patch.dict(sys.modules, modules):
            backend = GeminiBackend(api_key="test-key")
            await backend.generate_structured(
                [{"role": "user", "content": "go"}],
                _SummarySchema,
            )

        mock_genai_types.GenerationConfig.assert_called_once()
        gen_kwargs = mock_genai_types.GenerationConfig.call_args.kwargs
        self.assertEqual(gen_kwargs["response_mime_type"], "application/json")
        self.assertEqual(gen_kwargs["response_schema"], _SummarySchema.model_json_schema())

    async def test_generate_structured_wraps_sdk_error(self) -> None:
        """generate_structured() wraps Google AI errors as LLMBackendError."""
        from camel.llm.adapters.gemini import GeminiBackend

        mock_google, mock_genai, mock_genai_types = _make_genai_mocks()
        mock_genai_types.GenerationConfig.return_value = MagicMock()
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(
            side_effect=ValueError("invalid response schema")
        )
        mock_genai.GenerativeModel.return_value = mock_model

        modules = {
            "google": mock_google,
            "google.generativeai": mock_genai,
            "google.generativeai.types": mock_genai_types,
        }
        with patch.dict(sys.modules, modules):
            backend = GeminiBackend(api_key="test-key")
            with self.assertRaises(LLMBackendError) as ctx:
                await backend.generate_structured(
                    [{"role": "user", "content": "test"}],
                    _SummarySchema,
                )

        self.assertIn("invalid response schema", str(ctx.exception))
        self.assertIsInstance(ctx.exception.cause, ValueError)


# ---------------------------------------------------------------------------
# 10–12. get_backend factory
# ---------------------------------------------------------------------------


class TestGetBackend(unittest.TestCase):
    """Tests for the get_backend() factory function."""

    def test_get_backend_claude_returns_claude_backend(self) -> None:
        """get_backend('claude') returns a ClaudeBackend instance."""
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = MagicMock()

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = get_backend("claude", api_key="test-key")

        from camel.llm.adapters.claude import ClaudeBackend

        self.assertIsInstance(backend, ClaudeBackend)

    def test_get_backend_gemini_returns_gemini_backend(self) -> None:
        """get_backend('gemini') returns a GeminiBackend instance."""
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
        ):
            backend = get_backend("gemini", api_key="test-key")

        from camel.llm.adapters.gemini import GeminiBackend

        self.assertIsInstance(backend, GeminiBackend)

    def test_get_backend_unknown_raises_value_error(self) -> None:
        """get_backend raises ValueError for unrecognised provider strings."""
        with self.assertRaises(ValueError) as ctx:
            get_backend("openai", api_key="x")

        self.assertIn("openai", str(ctx.exception))

    def test_get_backend_claude_forwards_model_kwarg(self) -> None:
        """get_backend forwards kwargs to the ClaudeBackend constructor."""
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = MagicMock()

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            backend = get_backend(
                "claude", api_key="test-key", model="claude-haiku-4-5-20251001"
            )

        self.assertEqual(backend._model, "claude-haiku-4-5-20251001")

    def test_get_backend_gemini_forwards_model_kwarg(self) -> None:
        """get_backend forwards kwargs to the GeminiBackend constructor."""
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
        ):
            backend = get_backend("gemini", api_key="test-key", model="gemini-2.0-pro")

        self.assertEqual(backend._model_name, "gemini-2.0-pro")


# ---------------------------------------------------------------------------
# 13. LLMBackend Protocol isinstance check
# ---------------------------------------------------------------------------


class TestLLMBackendProtocol(unittest.TestCase):
    """Tests for the LLMBackend Protocol runtime_checkable behaviour."""

    def test_duck_typed_object_satisfies_protocol(self) -> None:
        """Any object exposing generate() and generate_structured() satisfies
        the LLMBackend Protocol for isinstance checks."""

        class _FakeBackend:
            """Minimal duck-typed backend for protocol check."""

            async def generate(
                self, messages: list[Any], **kwargs: Any
            ) -> str:
                """Stub generate."""
                return ""

            async def generate_structured(
                self, messages: list[Any], schema: type[Any]
            ) -> Any:
                """Stub generate_structured."""
                return schema()

        self.assertIsInstance(_FakeBackend(), LLMBackend)

    def test_object_missing_generate_does_not_satisfy_protocol(self) -> None:
        """An object missing generate() does not satisfy LLMBackend."""

        class _Incomplete:
            """Missing generate() method."""

            async def generate_structured(
                self, messages: list[Any], schema: type[Any]
            ) -> Any:
                """Only structured."""
                return schema()

        self.assertNotIsInstance(_Incomplete(), LLMBackend)


# ---------------------------------------------------------------------------
# 14. LLMBackendError attributes
# ---------------------------------------------------------------------------


class TestLLMBackendError(unittest.TestCase):
    """Tests for LLMBackendError construction and attributes."""

    def test_error_message_stored(self) -> None:
        """LLMBackendError stores the message string."""
        err = LLMBackendError("something went wrong")
        self.assertEqual(str(err), "something went wrong")

    def test_cause_defaults_to_none(self) -> None:
        """LLMBackendError.cause is None when not provided."""
        err = LLMBackendError("oops")
        self.assertIsNone(err.cause)

    def test_cause_stored_when_provided(self) -> None:
        """LLMBackendError.cause holds the original exception."""
        original = RuntimeError("root cause")
        err = LLMBackendError("wrapped", cause=original)
        self.assertIs(err.cause, original)

    def test_is_exception_subclass(self) -> None:
        """LLMBackendError is an Exception subclass for broad except blocks."""
        self.assertTrue(issubclass(LLMBackendError, Exception))


if __name__ == "__main__":
    unittest.main()
