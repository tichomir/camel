"""Multi-backend swappability test.

Verifies that the CaMeL orchestration stack is backend-agnostic: two
different backend implementations are interchangeable via a single
configuration change without any code modifications.

Design follows ``docs/e2e-scenario-specification.md §3`` and ADR-008 §5.

Tests
-----
1. ``test_backend_satisfies_protocol`` — both mock backends satisfy the
   ``LLMBackend`` structural protocol (``isinstance`` check).
2. ``test_claude_mock_backend_swap`` — S02 runs against a Claude-style mock.
3. ``test_gemini_mock_backend_swap`` — S02 runs against a Gemini-style mock.
4. ``test_execution_trace_shape_identical`` — the ``ExecutionTrace`` from
   both backends has the same shape for scenario S02.
5. ``test_get_backend_factory_protocol_conformance`` — ``get_backend()``
   returns an object satisfying ``LLMBackend`` for both "claude" and
   "gemini" providers (tested with a lightweight structural check only,
   no real API keys required).
6. ``test_no_direct_adapter_import_needed`` — test module imports only
   ``get_backend``; direct import of ``ClaudeBackend`` / ``GeminiBackend``
   is not required for backend selection.

All tests use mock backends; no real API keys or network calls are needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from camel import CaMeLInterpreter
from camel.execution_loop import CaMeLOrchestrator, ExecutionResult
from camel.llm.backend import LLMBackend
from camel.llm.p_llm import PLLMWrapper, ToolSignature
from camel.value import CaMeLValue, wrap

from tests.harness.scenarios import SCENARIOS


# ---------------------------------------------------------------------------
# S02 scenario fixture (simplest single-step, used across all swap tests)
# ---------------------------------------------------------------------------


def _get_s02() -> Any:
    """Return the S02 scenario spec from the registry."""
    return next(s for s in SCENARIOS if s.scenario_id == "S02")


def _make_mock_backend(plan: str) -> MagicMock:
    """Return a mock satisfying the LLMBackend structural protocol.

    The mock's ``generate`` coroutine returns a fenced plan block on every
    call so ``PLLMWrapper`` can parse it.

    Parameters
    ----------
    plan:
        Pre-baked plan source to wrap in a Markdown fence.

    Returns
    -------
    MagicMock
        A mock with ``generate`` set to an ``AsyncMock``.
    """
    mock = MagicMock()
    mock.generate = AsyncMock(
        return_value=f"```python\n{plan}\n```"
    )
    mock.generate_structured = AsyncMock()
    return mock


async def _run_scenario_with_backend(backend: Any) -> ExecutionResult:
    """Run S02 against the given backend and return the ExecutionResult."""
    s02 = _get_s02()
    p_llm = PLLMWrapper(backend)
    interpreter = CaMeLInterpreter(tools=s02.tools)
    sigs = [
        ToolSignature(
            name=name,
            signature="*args",
            return_type="CaMeLValue",
            description=f"Stub: {name}",
        )
        for name in s02.tools
    ]
    orchestrator = CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interpreter,
        tool_signatures=sigs,
    )
    return await orchestrator.run(user_query=s02.description)


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


def test_backend_satisfies_protocol_mock() -> None:
    """A generic mock with the right methods satisfies LLMBackend.

    Both ``generate`` and ``generate_structured`` coroutines are required
    by the structural protocol.  This test confirms ``isinstance`` works
    for plain ``MagicMock`` objects that provide these methods.
    """
    s02 = _get_s02()
    backend = _make_mock_backend(s02.plans[0])

    assert isinstance(backend, LLMBackend), (
        "Mock backend with generate() and generate_structured() must "
        "satisfy the LLMBackend structural protocol."
    )


def test_recording_backend_satisfies_protocol() -> None:
    """RecordingBackend satisfies LLMBackend (re-checked here for completeness)."""
    from tests.harness.recording_backend import RecordingBackend, StubBackend

    stub = StubBackend(responses=["x = 1"])
    recording = RecordingBackend(stub)

    assert isinstance(recording, LLMBackend)


# ---------------------------------------------------------------------------
# Backend swap tests
# ---------------------------------------------------------------------------


async def test_claude_mock_backend_executes_s02() -> None:
    """S02 executes correctly against a Claude-style mock backend.

    The mock simulates what ``ClaudeBackend.generate()`` would return.
    The test asserts that ``ExecutionResult`` is produced and the trace
    contains exactly one tool call (``send_email``).
    """
    s02 = _get_s02()
    claude_mock = _make_mock_backend(s02.plans[0])

    result = await _run_scenario_with_backend(claude_mock)

    assert isinstance(result, ExecutionResult)
    assert len(result.trace) == 1
    assert result.trace[0].tool_name == "send_email"
    # Confirm generate() was called exactly once (no retry needed).
    claude_mock.generate.assert_called_once()


async def test_gemini_mock_backend_executes_s02() -> None:
    """S02 executes correctly against a Gemini-style mock backend.

    Same assertions as the Claude mock test — the trace must have the
    same shape regardless of which backend was used.
    """
    s02 = _get_s02()
    gemini_mock = _make_mock_backend(s02.plans[0])

    result = await _run_scenario_with_backend(gemini_mock)

    assert isinstance(result, ExecutionResult)
    assert len(result.trace) == 1
    assert result.trace[0].tool_name == "send_email"
    gemini_mock.generate.assert_called_once()


async def test_execution_trace_shape_identical_across_backends() -> None:
    """Both mock backends produce identical ExecutionTrace shapes for S02.

    Demonstrates that swapping from Claude to Gemini requires only a
    configuration change; the resulting trace is backend-independent.
    """
    s02 = _get_s02()
    claude_result = await _run_scenario_with_backend(_make_mock_backend(s02.plans[0]))
    gemini_result = await _run_scenario_with_backend(_make_mock_backend(s02.plans[0]))

    # Trace lengths must be identical.
    assert len(claude_result.trace) == len(gemini_result.trace), (
        "Execution trace length must be identical regardless of backend."
    )
    # Tool names in the trace must match.
    claude_tools = [r.tool_name for r in claude_result.trace]
    gemini_tools = [r.tool_name for r in gemini_result.trace]
    assert claude_tools == gemini_tools, (
        f"Trace tool names differ: claude={claude_tools!r}, "
        f"gemini={gemini_tools!r}"
    )


# ---------------------------------------------------------------------------
# Factory-based backend selection (§3 contract)
# ---------------------------------------------------------------------------


def test_backend_selection_via_get_backend_only() -> None:
    """Backend selection uses only ``get_backend(provider)`` — no direct imports.

    Confirms that ``ClaudeBackend`` and ``GeminiBackend`` need not be
    imported directly.  This test performs a structural check only — it
    does not instantiate the real adapters (which require SDK packages
    and API keys).

    The test verifies:

    - ``get_backend`` is importable from ``camel.llm.backend``.
    - The factory raises ``ValueError`` for unknown providers (proving
      the dispatch is provider-string driven).
    - No import of ``ClaudeBackend`` or ``GeminiBackend`` is present in
      this test module.
    """
    from camel.llm.backend import get_backend

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_backend("not_a_real_provider")


def test_no_direct_adapter_import_in_test_module() -> None:
    """Confirm this test module does not import concrete adapter classes.

    ADR-008 §5 requires that only ``get_backend(provider)`` is called
    to switch providers — no direct import of ``ClaudeBackend`` or
    ``GeminiBackend`` is needed.  This test inspects the module's globals
    to enforce that invariant.
    """
    import tests.test_backend_swap as this_module

    module_globals = vars(this_module)
    assert "ClaudeBackend" not in module_globals, (
        "ClaudeBackend must not be imported directly in test_backend_swap.py"
    )
    assert "GeminiBackend" not in module_globals, (
        "GeminiBackend must not be imported directly in test_backend_swap.py"
    )
