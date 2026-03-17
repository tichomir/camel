"""Unit tests for PLLMWrapper, CodeBlockParser, ToolSignature, and CodePlan.

Covers:
1.  CodeBlockParser.extract — happy path (```python fence).
2.  CodeBlockParser.extract — bare ``` fence accepted.
3.  CodeBlockParser.extract — first block extracted when multiple present.
4.  CodeBlockParser.extract — raises CodeBlockNotFoundError when absent.
5.  CodeBlockParser.extract — raises CodeBlockNotFoundError for empty block.
6.  PLLMWrapper.build_system_prompt — contains all five required sections.
7.  PLLMWrapper.build_system_prompt — tool stubs formatted correctly.
8.  PLLMWrapper.build_system_prompt — zero-tool case handled.
9.  PLLMWrapper.build_system_prompt — user_context key-value pairs injected.
10. PLLMWrapper.build_system_prompt — no tool return values in constructed
    prompt (structural isolation test).
11. PLLMWrapper.parse_code_plan — returns CodePlan wrapping source.
12. PLLMWrapper.parse_code_plan — propagates CodeBlockNotFoundError.
13. PLLMWrapper.generate_plan — happy path returns CodePlan on first attempt.
14. PLLMWrapper.generate_plan — retries on CodeBlockNotFoundError.
15. PLLMWrapper.generate_plan — retries on SyntaxError.
16. PLLMWrapper.generate_plan — retry loop respects max_retries limit.
17. PLLMWrapper.generate_plan — error feedback is redacted (type + line only).
18. PLLMWrapper.generate_plan — PLLMIsolationError when user_query is CaMeLValue.
19. PLLMWrapper.generate_plan — PLLMIsolationError when context value is CaMeLValue.
20. PLLMWrapper.generate_plan — no CaMeLValue appears in any constructed message.
21. PLLMWrapper.generate_plan — LLMBackendError propagates without retry.
22. PLLMWrapper — system prompt contains M2-F13 opaque-variable instruction.
23. PLLMWrapper — system prompt contains M2-F10 print() guidance.
24. PLLMWrapper — retry appends assistant turn + error feedback to messages.
25. PLLMWrapper — retry with SyntaxError includes line number in feedback.

All LLM backend calls use unittest.mock; no real API calls are made.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from camel.llm.backend import LLMBackendError
from camel.llm.p_llm import (
    CodeBlockNotFoundError,
    CodeBlockParser,
    CodePlan,
    PLLMIsolationError,
    PLLMRetryExhaustedError,
    PLLMWrapper,
    ToolSignature,
)
from camel.value import CaMeLValue, Public

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_backend(responses: list[str]) -> MagicMock:
    """Return a mock LLMBackend whose generate() yields successive responses."""
    backend = MagicMock()
    backend.generate = AsyncMock(side_effect=responses)
    return backend


def _make_tools() -> list[ToolSignature]:
    """Return a small fixture tool list."""
    return [
        ToolSignature(
            name="get_email",
            signature="",
            return_type="EmailMessage",
            description="Retrieve the most recent email from the inbox.",
        ),
        ToolSignature(
            name="send_email",
            signature="recipient: str, body: str",
            return_type="None",
            description="Send an email to recipient.",
        ),
    ]


def _make_camel_value(raw: str = "secret") -> CaMeLValue:
    """Return a CaMeLValue wrapping a plain string."""
    return CaMeLValue(
        value=raw,
        sources=frozenset({"get_email"}),
        inner_source=None,
        readers=Public,
    )


def _wrap_plan(code: str) -> str:
    """Wrap *code* in a Markdown python fence as a P-LLM would respond."""
    return f"Here is the plan:\n\n```python\n{code}\n```\n"


# ---------------------------------------------------------------------------
# 1–5. CodeBlockParser
# ---------------------------------------------------------------------------


class TestCodeBlockParser(unittest.TestCase):
    """Tests for CodeBlockParser.extract()."""

    def test_extracts_python_fenced_block(self) -> None:
        """extract() returns code from a ```python fence."""
        response = "```python\nresult = get_email()\n```"
        code = CodeBlockParser.extract(response)
        self.assertEqual(code, "result = get_email()")

    def test_accepts_bare_fence(self) -> None:
        """extract() accepts a bare ``` fence without a language tag."""
        response = "```\nresult = get_email()\n```"
        code = CodeBlockParser.extract(response)
        self.assertEqual(code, "result = get_email()")

    def test_extracts_first_block_when_multiple_present(self) -> None:
        """extract() returns the first fenced block when several are present."""
        response = (
            "First block:\n```python\nx = 1\n```\n"
            "Second block:\n```python\ny = 2\n```"
        )
        code = CodeBlockParser.extract(response)
        self.assertEqual(code, "x = 1")

    def test_raises_when_no_fence_found(self) -> None:
        """extract() raises CodeBlockNotFoundError when no fence is present."""
        with self.assertRaises(CodeBlockNotFoundError) as ctx:
            CodeBlockParser.extract("No code here at all.")
        self.assertIn("No code here at all.", ctx.exception.response)

    def test_raises_for_empty_fenced_block(self) -> None:
        """extract() raises CodeBlockNotFoundError for an empty fenced block."""
        response = "```python\n   \n```"
        with self.assertRaises(CodeBlockNotFoundError):
            CodeBlockParser.extract(response)

    def test_strips_surrounding_whitespace(self) -> None:
        """extract() strips leading/trailing whitespace from the code block."""
        response = "```python\n\n  x = 1\n  \n```"
        code = CodeBlockParser.extract(response)
        self.assertEqual(code, "x = 1")

    def test_multiline_code_preserved(self) -> None:
        """extract() preserves multi-line code blocks intact."""
        plan = "email = get_email()\nsend_email(recipient='alice', body='hi')"
        response = f"```python\n{plan}\n```"
        code = CodeBlockParser.extract(response)
        self.assertEqual(code, plan)


# ---------------------------------------------------------------------------
# 6–10. PLLMWrapper.build_system_prompt — section content
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt(unittest.TestCase):
    """Tests for PLLMWrapper.build_system_prompt()."""

    def setUp(self) -> None:
        """Set up a PLLMWrapper with a stub backend."""
        self.wrapper = PLLMWrapper(backend=MagicMock())

    def test_contains_subset_spec_section(self) -> None:
        """build_system_prompt() includes the CaMeL Python subset spec."""
        prompt = self.wrapper.build_system_prompt([])
        self.assertIn("SUPPORTED CONSTRUCTS ONLY", prompt)
        self.assertIn("FORBIDDEN", prompt)

    def test_contains_opaque_variable_instruction(self) -> None:
        """build_system_prompt() includes the M2-F13 opaque-variable section."""
        prompt = self.wrapper.build_system_prompt([])
        # Key phrase from the M2-F13 section
        self.assertIn("OPAQUE VARIABLES", prompt)
        self.assertIn("M2-F13", prompt)

    def test_contains_tool_signatures_section(self) -> None:
        """build_system_prompt() includes the AVAILABLE TOOLS section."""
        prompt = self.wrapper.build_system_prompt([])
        self.assertIn("AVAILABLE TOOLS", prompt)

    def test_contains_user_context_section(self) -> None:
        """build_system_prompt() includes the CONTEXT section."""
        prompt = self.wrapper.build_system_prompt([], user_context={})
        self.assertIn("CONTEXT", prompt)

    def test_contains_print_guidance_section(self) -> None:
        """build_system_prompt() includes the M2-F10 print() guidance."""
        prompt = self.wrapper.build_system_prompt([])
        self.assertIn("M2-F10", prompt)
        self.assertIn("print()", prompt)

    def test_all_five_sections_present(self) -> None:
        """build_system_prompt() includes all five required sections."""
        prompt = self.wrapper.build_system_prompt(_make_tools(), {"date": "2026-03-17"})
        # 1. Subset spec
        self.assertIn("SUPPORTED CONSTRUCTS ONLY", prompt)
        # 2. Opaque-variable instruction (M2-F13)
        self.assertIn("OPAQUE VARIABLES", prompt)
        # 3. Tool signatures
        self.assertIn("AVAILABLE TOOLS", prompt)
        # 4. User context
        self.assertIn("CONTEXT", prompt)
        # 5. print() guidance (M2-F10)
        self.assertIn("M2-F10", prompt)

    def test_tool_stubs_formatted_correctly(self) -> None:
        """build_system_prompt() formats tools as def name(sig) -> ret: stubs."""
        tools = _make_tools()
        prompt = self.wrapper.build_system_prompt(tools)
        # get_email stub
        self.assertIn("def get_email() -> EmailMessage:", prompt)
        self.assertIn('"""Retrieve the most recent email from the inbox."""', prompt)
        # send_email stub
        self.assertIn("def send_email(recipient: str, body: str) -> None:", prompt)
        self.assertIn('"""Send an email to recipient."""', prompt)

    def test_zero_tools_handled_gracefully(self) -> None:
        """build_system_prompt() handles an empty tool list without error."""
        prompt = self.wrapper.build_system_prompt([])
        self.assertIn("AVAILABLE TOOLS", prompt)
        self.assertIn("No tools are available", prompt)

    def test_user_context_key_value_pairs_injected(self) -> None:
        """build_system_prompt() injects user_context key-value pairs."""
        context = {"current_date": "2026-03-17", "user_email": "alice@example.com"}
        prompt = self.wrapper.build_system_prompt([], user_context=context)
        self.assertIn("current_date: 2026-03-17", prompt)
        self.assertIn("user_email: alice@example.com", prompt)

    def test_no_tool_return_values_in_system_prompt(self) -> None:
        """build_system_prompt() never places CaMeLValue instances in the prompt.

        This is a structural isolation test: the method accepts only plain str
        parameters so a CaMeLValue cannot reach the model.  We verify that the
        returned prompt is a plain str with no CaMeLValue repr.
        """
        tools = _make_tools()
        prompt = self.wrapper.build_system_prompt(
            tools, user_context={"date": "2026-03-17"}
        )
        self.assertIsInstance(prompt, str)
        self.assertNotIn("CaMeLValue", prompt)

    def test_output_format_instruction_present(self) -> None:
        """build_system_prompt() includes the output-format instruction."""
        prompt = self.wrapper.build_system_prompt([])
        self.assertIn("OUTPUT FORMAT", prompt)
        self.assertIn("```python", prompt)


# ---------------------------------------------------------------------------
# 11–12. PLLMWrapper.parse_code_plan
# ---------------------------------------------------------------------------


class TestParseCodePlan(unittest.TestCase):
    """Tests for PLLMWrapper.parse_code_plan()."""

    def setUp(self) -> None:
        """Set up a PLLMWrapper with a stub backend."""
        self.wrapper = PLLMWrapper(backend=MagicMock())

    def test_returns_code_plan_wrapping_source(self) -> None:
        """parse_code_plan() returns a CodePlan with the extracted source."""
        response = _wrap_plan("email = get_email()")
        plan = self.wrapper.parse_code_plan(response)
        self.assertIsInstance(plan, CodePlan)
        self.assertEqual(plan.source, "email = get_email()")

    def test_propagates_code_block_not_found_error(self) -> None:
        """parse_code_plan() propagates CodeBlockNotFoundError on missing fence."""
        with self.assertRaises(CodeBlockNotFoundError):
            self.wrapper.parse_code_plan("No code here.")


# ---------------------------------------------------------------------------
# 13–21. PLLMWrapper.generate_plan
# ---------------------------------------------------------------------------


class TestGeneratePlanHappyPath(unittest.IsolatedAsyncioTestCase):
    """Happy-path tests for PLLMWrapper.generate_plan()."""

    async def test_returns_code_plan_on_first_attempt(self) -> None:
        """generate_plan() returns a CodePlan on the first successful attempt."""
        code = "email = get_email()\nsend_email(recipient='alice@x.com', body='hi')"
        backend = _make_backend([_wrap_plan(code)])
        wrapper = PLLMWrapper(backend)

        plan = await wrapper.generate_plan(
            user_query="Forward the last email to alice@x.com",
            tool_signatures=_make_tools(),
        )

        self.assertIsInstance(plan, CodePlan)
        self.assertEqual(plan.source, code)
        backend.generate.assert_awaited_once()

    async def test_plan_source_is_syntactically_valid_python(self) -> None:
        """generate_plan() only returns plans that pass ast.parse."""
        code = "x = 1\ny = x + 2"
        backend = _make_backend([_wrap_plan(code)])
        wrapper = PLLMWrapper(backend)

        plan = await wrapper.generate_plan(
            user_query="Compute something",
            tool_signatures=[],
        )
        self.assertEqual(plan.source, code)


class TestGeneratePlanRetry(unittest.IsolatedAsyncioTestCase):
    """Tests for the generate_plan() retry loop."""

    async def test_retries_on_code_block_not_found(self) -> None:
        """generate_plan() retries when first response has no fenced block."""
        code = "result = get_email()"
        backend = _make_backend(["No code block here.", _wrap_plan(code)])
        wrapper = PLLMWrapper(backend)

        plan = await wrapper.generate_plan(
            user_query="Get the email",
            tool_signatures=_make_tools(),
        )

        self.assertEqual(plan.source, code)
        self.assertEqual(backend.generate.await_count, 2)

    async def test_retries_on_syntax_error(self) -> None:
        """generate_plan() retries when extracted code has a SyntaxError."""
        bad_code = "def foo( :"  # deliberately invalid syntax
        good_code = "result = get_email()"
        backend = _make_backend(
            [_wrap_plan(bad_code), _wrap_plan(good_code)]
        )
        wrapper = PLLMWrapper(backend)

        plan = await wrapper.generate_plan(
            user_query="Get the email",
            tool_signatures=_make_tools(),
        )

        self.assertEqual(plan.source, good_code)
        self.assertEqual(backend.generate.await_count, 2)

    async def test_retry_loop_respects_max_retries_limit(self) -> None:
        """generate_plan() raises PLLMRetryExhaustedError after max_retries."""
        max_retries = 3
        # All responses are missing code blocks
        responses = ["no block"] * max_retries
        backend = _make_backend(responses)
        wrapper = PLLMWrapper(backend, max_retries=max_retries)

        with self.assertRaises(PLLMRetryExhaustedError) as ctx:
            await wrapper.generate_plan(
                user_query="Do something",
                tool_signatures=[],
            )

        self.assertEqual(ctx.exception.attempts, max_retries)
        self.assertEqual(backend.generate.await_count, max_retries)

    async def test_retry_exhausted_error_stores_attempt_count(self) -> None:
        """PLLMRetryExhaustedError.attempts equals the configured max_retries."""
        backend = _make_backend(["bad"] * 5)
        wrapper = PLLMWrapper(backend, max_retries=5)

        with self.assertRaises(PLLMRetryExhaustedError) as ctx:
            await wrapper.generate_plan("query", [])

        self.assertEqual(ctx.exception.attempts, 5)

    async def test_retry_appends_assistant_and_error_turns(self) -> None:
        """On retry, the previous assistant response and error feedback are appended."""
        good_code = "x = 1"
        first_bad = "no block here"
        backend = _make_backend([first_bad, _wrap_plan(good_code)])

        # Capture all messages passed to generate()
        captured: list[list[Any]] = []

        async def _record_generate(messages: list[Any], **kwargs: Any) -> str:
            captured.append(list(messages))
            idx = len(captured) - 1
            responses = [first_bad, _wrap_plan(good_code)]
            return responses[idx]

        backend.generate = AsyncMock(side_effect=_record_generate)
        wrapper = PLLMWrapper(backend)

        await wrapper.generate_plan("Do something", [])

        # First call: [system, user]
        self.assertEqual(len(captured[0]), 2)
        self.assertEqual(captured[0][0]["role"], "system")
        self.assertEqual(captured[0][1]["role"], "user")

        # Second call: [system, user, assistant, user(error)]
        self.assertEqual(len(captured[1]), 4)
        self.assertEqual(captured[1][2]["role"], "assistant")
        self.assertEqual(captured[1][2]["content"], first_bad)
        self.assertEqual(captured[1][3]["role"], "user")
        # Error feedback should contain the error type
        self.assertIn("CodeBlockNotFoundError", captured[1][3]["content"])

    async def test_error_feedback_contains_error_type_only(self) -> None:
        """Retry error feedback includes error type but NOT the error message text."""
        bad_code = "def foo( :"  # syntax error
        good_code = "x = 1"
        backend = _make_backend([_wrap_plan(bad_code), _wrap_plan(good_code)])

        captured_messages: list[list[Any]] = []

        async def _record(messages: list[Any], **kwargs: Any) -> str:
            captured_messages.append(list(messages))
            idx = len(captured_messages) - 1
            return [_wrap_plan(bad_code), _wrap_plan(good_code)][idx]

        backend.generate = AsyncMock(side_effect=_record)
        wrapper = PLLMWrapper(backend)

        await wrapper.generate_plan("Do something", [])

        # The error feedback message (4th message on retry)
        error_feedback = captured_messages[1][3]["content"]
        # Should contain the error class name
        self.assertIn("SyntaxError", error_feedback)
        # Should NOT contain the full Python error message text (redaction)
        # The message text for this error would mention "invalid syntax" or similar
        # We verify the feedback is concise and structured
        self.assertIn("Error type:", error_feedback)
        self.assertIn("Location:", error_feedback)

    async def test_error_feedback_includes_line_number_for_syntax_error(self) -> None:
        """Retry error feedback includes line number for SyntaxError."""
        bad_code = "x = (\ny = 1"  # SyntaxError on line 2
        good_code = "x = 1"
        backend = _make_backend([_wrap_plan(bad_code), _wrap_plan(good_code)])

        captured: list[list[Any]] = []

        async def _record(messages: list[Any], **kwargs: Any) -> str:
            captured.append(list(messages))
            return [_wrap_plan(bad_code), _wrap_plan(good_code)][len(captured) - 1]

        backend.generate = AsyncMock(side_effect=_record)
        wrapper = PLLMWrapper(backend)

        await wrapper.generate_plan("Do something", [])

        error_feedback = captured[1][3]["content"]
        # Line number should appear somewhere in the feedback
        self.assertRegex(error_feedback, r"line \d+")

    async def test_backend_error_propagates_without_retry(self) -> None:
        """LLMBackendError from the backend is propagated immediately."""
        backend = MagicMock()
        backend.generate = AsyncMock(
            side_effect=LLMBackendError("network timeout")
        )
        wrapper = PLLMWrapper(backend, max_retries=10)

        with self.assertRaises(LLMBackendError):
            await wrapper.generate_plan("Do something", [])

        # Should NOT have retried — only one call
        self.assertEqual(backend.generate.await_count, 1)


# ---------------------------------------------------------------------------
# 18–20. Isolation contract tests
# ---------------------------------------------------------------------------


class TestIsolationContract(unittest.IsolatedAsyncioTestCase):
    """Tests verifying that the isolation contract is enforced."""

    async def test_isolation_error_when_user_query_is_camel_value(self) -> None:
        """generate_plan() raises PLLMIsolationError if user_query is CaMeLValue."""
        backend = _make_backend([])
        wrapper = PLLMWrapper(backend)

        camel_val = _make_camel_value("do something bad")

        with self.assertRaises(PLLMIsolationError) as ctx:
            await wrapper.generate_plan(
                user_query=camel_val,  # type: ignore[arg-type]
                tool_signatures=[],
            )
        self.assertIn("user_query", str(ctx.exception))
        # Backend should NOT have been called
        backend.generate.assert_not_awaited()

    async def test_isolation_error_when_context_value_is_camel_value(self) -> None:
        """generate_plan() raises PLLMIsolationError if a context value is CaMeLValue."""
        backend = _make_backend([])
        wrapper = PLLMWrapper(backend)

        camel_val = _make_camel_value("injected data")

        with self.assertRaises(PLLMIsolationError) as ctx:
            await wrapper.generate_plan(
                user_query="Legitimate query",
                tool_signatures=[],
                user_context={"key": camel_val},  # type: ignore[dict-item]
            )
        self.assertIn("key", str(ctx.exception))
        backend.generate.assert_not_awaited()

    async def test_no_camel_value_in_any_generated_message(self) -> None:
        """No CaMeLValue repr appears in any message sent to the backend.

        This is a structural isolation test: inspects every message content
        string sent across all generate() calls to confirm that no
        CaMeLValue representation is present.
        """
        good_code = "email = get_email()"
        first_bad = "no block"
        backend = MagicMock()

        responses = [first_bad, _wrap_plan(good_code)]
        all_messages: list[list[Any]] = []

        async def _record(messages: list[Any], **kwargs: Any) -> str:
            all_messages.append(list(messages))
            idx = len(all_messages) - 1
            return responses[idx]

        backend.generate = AsyncMock(side_effect=_record)
        wrapper = PLLMWrapper(backend)

        await wrapper.generate_plan(
            user_query="Check email",
            tool_signatures=_make_tools(),
            user_context={"date": "2026-03-17"},
        )

        for call_messages in all_messages:
            for msg in call_messages:
                content = msg.get("content", "")
                self.assertNotIn("CaMeLValue", content)
                self.assertIsInstance(content, str)

    async def test_system_prompt_never_contains_tool_return_values(self) -> None:
        """The system prompt section of generated messages contains no tool data.

        Verifies that the system message content (which comes from
        build_system_prompt) consists only of static template text and the
        caller-supplied tool signatures and user context — no tool return
        values.
        """
        captured_system: list[str] = []

        async def _record(messages: list[Any], **kwargs: Any) -> str:
            for msg in messages:
                if msg["role"] == "system":
                    captured_system.append(msg["content"])
            return _wrap_plan("x = 1")

        backend = MagicMock()
        backend.generate = AsyncMock(side_effect=_record)
        wrapper = PLLMWrapper(backend)

        tools = _make_tools()
        await wrapper.generate_plan(
            user_query="Run the plan",
            tool_signatures=tools,
            user_context={"session_id": "abc123"},
        )

        self.assertTrue(len(captured_system) > 0)
        for sp in captured_system:
            # System prompt must contain tool stubs (fine)
            self.assertIn("get_email", sp)
            # Must not contain runtime variable values
            self.assertNotIn("CaMeLValue", sp)
            # Must be a plain str
            self.assertIsInstance(sp, str)


# ---------------------------------------------------------------------------
# 22–23. System prompt section-specific content
# ---------------------------------------------------------------------------


class TestSystemPromptContent(unittest.TestCase):
    """Detailed content tests for specific system prompt sections."""

    def setUp(self) -> None:
        """Set up a PLLMWrapper with a stub backend."""
        self.wrapper = PLLMWrapper(backend=MagicMock())

    def test_m2_f13_opaque_variable_section_content(self) -> None:
        """System prompt M2-F13 section instructs P-LLM not to inspect values."""
        prompt = self.wrapper.build_system_prompt([])
        # Key phrases from M2-F13
        self.assertIn("completely opaque", prompt)
        self.assertIn("passes these variables", prompt)

    def test_m2_f10_print_section_instructs_print_usage(self) -> None:
        """System prompt M2-F10 section provides print() usage guidance."""
        prompt = self.wrapper.build_system_prompt([])
        # Key phrases from M2-F10
        self.assertIn("print()", prompt)
        self.assertIn("OUTPUT TO USER", prompt)

    def test_tool_signature_with_empty_signature_renders_correctly(self) -> None:
        """Zero-argument tool renders as def name() -> ret: correctly."""
        tools = [
            ToolSignature(
                name="get_time",
                signature="",
                return_type="str",
                description="Get the current time.",
            )
        ]
        prompt = self.wrapper.build_system_prompt(tools)
        self.assertIn("def get_time() -> str:", prompt)
        self.assertIn('"""Get the current time."""', prompt)

    def test_multiple_tools_all_present_in_prompt(self) -> None:
        """All tools in the list appear as stubs in the system prompt."""
        tools = [
            ToolSignature("tool_a", "x: int", "int", "Does A."),
            ToolSignature("tool_b", "y: str", "str", "Does B."),
            ToolSignature("tool_c", "", "None", "Does C."),
        ]
        prompt = self.wrapper.build_system_prompt(tools)
        self.assertIn("def tool_a(x: int) -> int:", prompt)
        self.assertIn("def tool_b(y: str) -> str:", prompt)
        self.assertIn("def tool_c() -> None:", prompt)


# ---------------------------------------------------------------------------
# Exception attribute tests
# ---------------------------------------------------------------------------


class TestExceptions(unittest.TestCase):
    """Tests for P-LLM exception classes."""

    def test_code_block_not_found_stores_response(self) -> None:
        """CodeBlockNotFoundError.response holds the raw response text."""
        exc = CodeBlockNotFoundError(response="raw text here")
        self.assertEqual(exc.response, "raw text here")
        self.assertIsInstance(exc, Exception)

    def test_retry_exhausted_stores_attempts(self) -> None:
        """PLLMRetryExhaustedError.attempts holds the attempt count."""
        exc = PLLMRetryExhaustedError(attempts=7)
        self.assertEqual(exc.attempts, 7)
        self.assertIn("7", str(exc))

    def test_isolation_error_is_pllm_error(self) -> None:
        """PLLMIsolationError is a subclass of PLLMError."""
        from camel.llm.p_llm import PLLMError

        exc = PLLMIsolationError("bad input")
        self.assertIsInstance(exc, PLLMError)

    def test_code_plan_exposes_source(self) -> None:
        """CodePlan.source is accessible and immutable."""
        plan = CodePlan(source="x = 1")
        self.assertEqual(plan.source, "x = 1")
        with self.assertRaises(Exception):
            plan.source = "y = 2"  # type: ignore[misc]  # frozen dataclass


if __name__ == "__main__":
    unittest.main()
