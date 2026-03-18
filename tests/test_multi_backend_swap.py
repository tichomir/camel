"""Multi-backend swap tests for the CaMeL execution stack.

Verifies that the CaMeL stack is agnostic to the concrete LLMBackend
implementation: swapping between a Claude-adapter mock and a Gemini-adapter
mock (via a single configuration value) produces identical execution traces.

Contract (§3 of docs/e2e-scenario-specification.md)
----------------------------------------------------
1. Construct a ClaudeBackend-like mock and a GeminiBackend-like mock, both
   satisfying the ``LLMBackend`` protocol via ``MagicMock(spec=…)``.
2. Run representative scenarios against each mock.
3. Assert ``isinstance(backend, LLMBackend)`` for both.
4. Assert the resulting ``ExecutionTrace`` is identical in shape for both
   backends.
5. No import of ``ClaudeBackend`` or ``GeminiBackend`` is required in the
   test module — backend selection happens via ``get_test_backend(config)``.

Backend swap configuration
---------------------------
The swap is achieved by changing a single config key::

    config = {"provider": "mock_claude"}   # → Claude-like mock
    config = {"provider": "mock_gemini"}   # → Gemini-like mock

The ``CAMEL_TEST_BACKEND`` environment variable is also honoured, matching
the §3 specification in ``e2e-scenario-specification.md``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from pydantic import BaseModel

from camel import CaMeLInterpreter
from camel.execution_loop import CaMeLOrchestrator, ExecutionTrace
from camel.llm.backend import LLMBackend
from camel.llm.p_llm import PLLMWrapper, ToolSignature
from camel.llm.protocols import Message
from camel.value import CaMeLValue, wrap

# ---------------------------------------------------------------------------
# Shared plan strings used in swap tests
# ---------------------------------------------------------------------------

#: S02 — simplest single-step plan (send_email with literal args)
_S02_PLAN = (
    'result = send_email(to="alice@example.com", subject="Hello", body="Hi Alice")'
)

#: S04 — two-step plan (get_email → send_email)
_S04_PLAN = (
    "email = get_email()\n"
    'result = send_email(to="bob@example.com", subject="Fwd", body="See below")'
)


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


class _MockClaudeBackend:
    """Minimal LLMBackend mock resembling a Claude adapter.

    Returns a single pre-configured *plan_source* for every ``generate``
    call.  ``generate_structured`` is not used by the P-LLM path and raises
    ``NotImplementedError`` to surface accidental calls.
    """

    def __init__(self, plan_source: str) -> None:
        """Initialise with the plan that will be returned on every generate call."""
        self._plan_source = plan_source

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        """Return a Markdown-fenced python block wrapping *plan_source*."""
        return f"```python\n{self._plan_source}\n```"

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Not used in P-LLM path — raises to catch accidental calls."""
        raise NotImplementedError("generate_structured should not be called in P-LLM path")

    def get_backend_id(self) -> str:
        """Return stable mock identifier."""
        return "mock-claude:test-model"

    def supports_structured_output(self) -> bool:
        """Return True for mock."""
        return True


class _MockGeminiBackend:
    """Minimal LLMBackend mock resembling a Gemini adapter.

    Behaviourally identical to :class:`_MockClaudeBackend`; exists as a
    separate class so ``isinstance`` checks and type annotations can
    distinguish the two.
    """

    def __init__(self, plan_source: str) -> None:
        """Initialise with the plan returned on every generate call."""
        self._plan_source = plan_source

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        """Return a Markdown-fenced python block wrapping *plan_source*."""
        return f"```python\n{self._plan_source}\n```"

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Not used in P-LLM path — raises to catch accidental calls."""
        raise NotImplementedError("generate_structured should not be called in P-LLM path")

    def get_backend_id(self) -> str:
        """Return stable mock identifier."""
        return "mock-gemini:test-model"

    def supports_structured_output(self) -> bool:
        """Return True for mock."""
        return True


class _MockOpenAIBackend:
    """Minimal LLMBackend mock resembling an OpenAI adapter.

    Behaviourally identical to :class:`_MockClaudeBackend`; exists as a
    separate class so ``isinstance`` checks and type annotations can
    distinguish the two.
    """

    def __init__(self, plan_source: str) -> None:
        """Initialise with the plan returned on every generate call."""
        self._plan_source = plan_source

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        """Return a Markdown-fenced python block wrapping *plan_source*."""
        return f"```python\n{self._plan_source}\n```"

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Not used in P-LLM path — raises to catch accidental calls."""
        raise NotImplementedError("generate_structured should not be called in P-LLM path")

    def get_backend_id(self) -> str:
        """Return stable mock identifier."""
        return "mock-openai:test-model"

    def supports_structured_output(self) -> bool:
        """Return True for mock."""
        return True


def get_test_backend(config: dict[str, Any]) -> Any:
    """Return a backend mock according to *config*.

    Configuration key: ``"provider"``

    - ``"mock_claude"`` — returns a :class:`_MockClaudeBackend`.
    - ``"mock_gemini"`` — returns a :class:`_MockGeminiBackend`.

    The ``CAMEL_TEST_BACKEND`` environment variable takes precedence over the
    ``"provider"`` key when set to ``"mock_claude"`` or ``"mock_gemini"``.

    Parameters
    ----------
    config:
        Mapping with at least a ``"provider"`` key and a ``"plan_source"``
        key specifying the Python plan the backend will return.

    Returns
    -------
    Any
        A backend instance satisfying the :class:`~camel.llm.backend.LLMBackend`
        protocol.

    Raises
    ------
    ValueError
        When *provider* is not a recognised mock provider string.
    """
    env_provider = os.environ.get("CAMEL_TEST_BACKEND")
    valid_providers = ("mock_claude", "mock_gemini", "mock_openai")
    provider = env_provider if env_provider in valid_providers else config["provider"]
    plan_source: str = config["plan_source"]

    if provider == "mock_claude":
        return _MockClaudeBackend(plan_source=plan_source)
    if provider == "mock_gemini":
        return _MockGeminiBackend(plan_source=plan_source)
    if provider == "mock_openai":
        return _MockOpenAIBackend(plan_source=plan_source)
    raise ValueError(
        f"Unknown test backend provider: {provider!r}. "
        "Use 'mock_claude', 'mock_gemini', or 'mock_openai'."
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, return_raw: Any = None) -> Any:
    """Return a synchronous CaMeL-compatible tool stub."""

    def tool(*args: Any, **kwargs: Any) -> CaMeLValue:
        return wrap(return_raw, sources=frozenset({name}))

    tool.__name__ = name
    return tool


class _NullDisplayChannel:
    """Silent display channel for test runs."""

    def write(self, value: CaMeLValue) -> None:
        """Discard *value*."""


def _run(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _run_scenario(
    backend: Any,
    plan_source: str,
    tools: dict[str, Any],
    tool_signatures: list[ToolSignature],
    query: str,
) -> ExecutionTrace:
    """Run a scenario against *backend* and return the execution trace.

    Constructs a ``PLLMWrapper`` around *backend*, builds an interpreter and
    orchestrator, then executes *query* and returns the resulting trace.
    """
    p_llm = PLLMWrapper(backend=backend)
    interp = CaMeLInterpreter(tools=tools)
    orch = CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interp,
        tool_signatures=tool_signatures,
        display_channel=_NullDisplayChannel(),  # type: ignore[arg-type]
    )
    result = _run(orch.run(query))
    return result.trace


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


class TestBackendProtocolConformance:
    """Both mock backends satisfy the LLMBackend structural protocol."""

    def test_mock_claude_is_llm_backend(self) -> None:
        """_MockClaudeBackend satisfies LLMBackend via isinstance check."""
        backend = _MockClaudeBackend(plan_source="x = 1")
        assert isinstance(backend, LLMBackend)

    def test_mock_gemini_is_llm_backend(self) -> None:
        """_MockGeminiBackend satisfies LLMBackend via isinstance check."""
        backend = _MockGeminiBackend(plan_source="x = 1")
        assert isinstance(backend, LLMBackend)

    def test_get_test_backend_claude_returns_llm_backend(self) -> None:
        """get_test_backend('mock_claude') returns an LLMBackend."""
        backend = get_test_backend({"provider": "mock_claude", "plan_source": "x = 1"})
        assert isinstance(backend, LLMBackend)

    def test_get_test_backend_gemini_returns_llm_backend(self) -> None:
        """get_test_backend('mock_gemini') returns an LLMBackend."""
        backend = get_test_backend({"provider": "mock_gemini", "plan_source": "x = 1"})
        assert isinstance(backend, LLMBackend)

    def test_mock_openai_is_llm_backend(self) -> None:
        """_MockOpenAIBackend satisfies LLMBackend via isinstance check."""
        backend = _MockOpenAIBackend(plan_source="x = 1")
        assert isinstance(backend, LLMBackend)

    def test_get_test_backend_openai_returns_llm_backend(self) -> None:
        """get_test_backend('mock_openai') returns an LLMBackend."""
        backend = get_test_backend({"provider": "mock_openai", "plan_source": "x = 1"})
        assert isinstance(backend, LLMBackend)

    def test_unknown_provider_raises_value_error(self) -> None:
        """get_test_backend raises ValueError for unknown providers."""
        with pytest.raises(ValueError, match="Unknown test backend provider"):
            get_test_backend({"provider": "unknown", "plan_source": "x = 1"})


# ---------------------------------------------------------------------------
# S02 — Single-step swap test
# ---------------------------------------------------------------------------

_S02_TOOLS: dict[str, Any] = {
    "send_email": _make_tool("send_email", return_raw={"status": "sent"}),
}

_S02_SIGS = [
    ToolSignature(
        "send_email", "to: str, subject: str, body: str", "dict", "Send an email."
    ),
]


class TestS02BackendSwap:
    """S02: Claude-mock and Gemini-mock produce identical traces."""

    def _trace_for_provider(self, provider: str) -> ExecutionTrace:
        """Run S02 with the given provider config and return the trace."""
        config = {"provider": provider, "plan_source": _S02_PLAN}
        backend = get_test_backend(config)
        return _run_scenario(
            backend=backend,
            plan_source=_S02_PLAN,
            tools=_S02_TOOLS,
            tool_signatures=_S02_SIGS,
            query="Send an email to alice@example.com with subject 'Hello'.",
        )

    def test_claude_trace_shape(self) -> None:
        """Claude-mock produces a single send_email trace record."""
        trace = self._trace_for_provider("mock_claude")
        assert len(trace) == 1
        assert trace[0].tool_name == "send_email"

    def test_gemini_trace_shape(self) -> None:
        """Gemini-mock produces a single send_email trace record."""
        trace = self._trace_for_provider("mock_gemini")
        assert len(trace) == 1
        assert trace[0].tool_name == "send_email"

    def test_traces_are_identical_in_shape(self) -> None:
        """Both backends produce traces with the same tool_name sequence."""
        claude_trace = self._trace_for_provider("mock_claude")
        gemini_trace = self._trace_for_provider("mock_gemini")
        assert [r.tool_name for r in claude_trace] == [r.tool_name for r in gemini_trace]

    def test_traces_have_identical_args(self) -> None:
        """Both backends produce identical argument values in the trace."""
        claude_trace = self._trace_for_provider("mock_claude")
        gemini_trace = self._trace_for_provider("mock_gemini")
        for c_rec, g_rec in zip(claude_trace, gemini_trace):
            assert c_rec.args == g_rec.args, (
                f"Arg mismatch for {c_rec.tool_name}: "
                f"claude={c_rec.args}, gemini={g_rec.args}"
            )

    def test_openai_trace_shape(self) -> None:
        """OpenAI-mock produces a single send_email trace record."""
        trace = self._trace_for_provider("mock_openai")
        assert len(trace) == 1
        assert trace[0].tool_name == "send_email"

    def test_swap_requires_only_config_change(self) -> None:
        """Changing only the provider config key produces the alternate backend."""
        config_claude = {"provider": "mock_claude", "plan_source": _S02_PLAN}
        config_gemini = {"provider": "mock_gemini", "plan_source": _S02_PLAN}
        config_openai = {"provider": "mock_openai", "plan_source": _S02_PLAN}
        backend_claude = get_test_backend(config_claude)
        backend_gemini = get_test_backend(config_gemini)
        backend_openai = get_test_backend(config_openai)
        # All three are different types — but all conform to LLMBackend.
        assert type(backend_claude) is not type(backend_gemini)
        assert type(backend_claude) is not type(backend_openai)
        assert isinstance(backend_claude, LLMBackend)
        assert isinstance(backend_gemini, LLMBackend)
        assert isinstance(backend_openai, LLMBackend)


# ---------------------------------------------------------------------------
# S04 — Two-step swap test
# ---------------------------------------------------------------------------

_S04_TOOLS: dict[str, Any] = {
    "get_email": _make_tool(
        "get_email",
        return_raw={"subject": "Re: project", "body": "See attached."},
    ),
    "send_email": _make_tool("send_email", return_raw={"status": "sent"}),
}

_S04_SIGS = [
    ToolSignature("get_email", "", "dict", "Retrieve the latest email."),
    ToolSignature(
        "send_email", "to: str, subject: str, body: str", "dict", "Send an email."
    ),
]


class TestS04BackendSwap:
    """S04: Claude-mock and Gemini-mock produce identical two-step traces."""

    def _trace_for_provider(self, provider: str) -> ExecutionTrace:
        """Run S04 with the given provider config and return the trace."""
        config = {"provider": provider, "plan_source": _S04_PLAN}
        backend = get_test_backend(config)
        return _run_scenario(
            backend=backend,
            plan_source=_S04_PLAN,
            tools=_S04_TOOLS,
            tool_signatures=_S04_SIGS,
            query="Get the latest email and forward it to bob@example.com.",
        )

    def test_claude_trace_length(self) -> None:
        """Claude-mock produces a two-record trace."""
        trace = self._trace_for_provider("mock_claude")
        assert len(trace) == 2

    def test_gemini_trace_length(self) -> None:
        """Gemini-mock produces a two-record trace."""
        trace = self._trace_for_provider("mock_gemini")
        assert len(trace) == 2

    def test_claude_trace_order(self) -> None:
        """Claude-mock trace: get_email then send_email."""
        trace = self._trace_for_provider("mock_claude")
        assert trace[0].tool_name == "get_email"
        assert trace[1].tool_name == "send_email"

    def test_gemini_trace_order(self) -> None:
        """Gemini-mock trace: get_email then send_email."""
        trace = self._trace_for_provider("mock_gemini")
        assert trace[0].tool_name == "get_email"
        assert trace[1].tool_name == "send_email"

    def test_traces_shape_identical(self) -> None:
        """Tool call order is the same regardless of backend."""
        claude_trace = self._trace_for_provider("mock_claude")
        gemini_trace = self._trace_for_provider("mock_gemini")
        assert [r.tool_name for r in claude_trace] == [r.tool_name for r in gemini_trace]

    def test_openai_trace_length(self) -> None:
        """OpenAI-mock produces a two-record trace."""
        trace = self._trace_for_provider("mock_openai")
        assert len(trace) == 2

    def test_openai_trace_order(self) -> None:
        """OpenAI-mock trace: get_email then send_email."""
        trace = self._trace_for_provider("mock_openai")
        assert trace[0].tool_name == "get_email"
        assert trace[1].tool_name == "send_email"

    def test_traces_args_identical(self) -> None:
        """Argument values are identical for all three backends."""
        claude_trace = self._trace_for_provider("mock_claude")
        gemini_trace = self._trace_for_provider("mock_gemini")
        openai_trace = self._trace_for_provider("mock_openai")
        for c_rec, g_rec, o_rec in zip(claude_trace, gemini_trace, openai_trace):
            assert c_rec.args == g_rec.args
            assert c_rec.args == o_rec.args


# ---------------------------------------------------------------------------
# Parametrised swap test across both scenarios and all three providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan,tools,sigs,query,expected_shape",
    [
        (
            _S02_PLAN,
            _S02_TOOLS,
            _S02_SIGS,
            "Send an email to alice@example.com with subject 'Hello'.",
            ["send_email"],
        ),
        (
            _S04_PLAN,
            _S04_TOOLS,
            _S04_SIGS,
            "Get the latest email and forward it to bob@example.com.",
            ["get_email", "send_email"],
        ),
    ],
    ids=["S02", "S04"],
)
@pytest.mark.parametrize("provider", ["mock_claude", "mock_gemini", "mock_openai"])
def test_backend_swap_trace_shape(
    plan: str,
    tools: dict[str, Any],
    sigs: list[ToolSignature],
    query: str,
    expected_shape: list[str],
    provider: str,
) -> None:
    """Provider-agnostic: both backends produce the expected trace shape.

    This parametrised test is the canonical multi-backend swap assertion.
    Changing only the *provider* parameter is the sole code change required
    to run the same scenario against a different backend.
    """
    config = {"provider": provider, "plan_source": plan}
    backend = get_test_backend(config)
    trace = _run_scenario(
        backend=backend,
        plan_source=plan,
        tools=tools,
        tool_signatures=sigs,
        query=query,
    )
    actual_shape = [r.tool_name for r in trace]
    assert actual_shape == expected_shape, (
        f"[{provider}] Expected {expected_shape}, got {actual_shape}"
    )
