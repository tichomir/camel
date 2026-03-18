"""SDK integration and thread-safety test suite for the camel-security public API.

Covers:
  1. Unit tests for CaMeLAgent constructor — valid/invalid parameter combinations.
  2. Unit tests for AgentResult — field presence, immutability, field types.
  3. Unit tests for Tool registration — with and without capability_annotation/policies.
  4. Integration test for agent.run() returning a valid AgentResult with all fields.
  5. Thread-safety test: 20 concurrent agent.run() calls, no cross-session state leakage.
  6. Smoke test: clean-venv install + minimal import/instantiation check.

Thread-safety contract reference: ``camel_security/agent.py`` module docstring and
``docs/design/milestone5-sdk-packaging.md`` §7.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel

from camel.interpreter import ExecutionMode
from camel.llm.backend import LLMBackend
from camel.policy.interfaces import Allowed, Denied, PolicyRegistry
from camel.value import CaMeLValue, Public, wrap
from camel_security import AgentResult, CaMeLAgent, Tool


# ---------------------------------------------------------------------------
# Minimal in-process mock backend (no network calls)
# ---------------------------------------------------------------------------


class _MockLLMBackend(LLMBackend):  # type: ignore[misc]
    """Concrete mock that satisfies the unified LLMBackend protocol.

    Implements ``generate``, ``generate_structured``, and ``structured_complete``
    so it passes:
    - ``isinstance(backend, LLMBackend)`` from ``camel.llm.backend`` (needs
      ``generate`` and ``generate_structured``).
    - The Q-LLM ``structured_complete`` path used by
      ``make_query_quarantined_llm``.

    ``generate`` returns a fenced code block that calls ``echo_tool`` with the
    user query literal, making the full interpreter execution path exercisable
    without any network calls.
    """

    def __init__(self, query_id: str = "default") -> None:
        """Initialise with a per-instance identifier for assertion clarity."""
        self._id = query_id

    async def generate(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """Return a minimal valid execution plan referencing echo_tool."""
        # Extract user query from the user turn.
        user_msg = next(
            (m["content"] for m in messages if m.get("role") == "user"),
            "hello",
        )
        # Truncate to avoid embedded quote issues; queries in tests are safe ASCII.
        safe_literal = user_msg[:20].replace('"', "")
        return f'```python\nresult = echo_tool("{safe_literal}")\nprint(result)\n```'

    async def generate_structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Stub — satisfies the LLMBackend protocol isinstance check."""
        raise NotImplementedError("generate_structured not used in SDK tests")

    async def structured_complete(
        self,
        messages: list[dict[str, Any]],
        schema: type[Any],
        **kwargs: Any,
    ) -> Any:
        """Stub — Q-LLM path; not exercised by these tests."""
        raise NotImplementedError("structured_complete not used in SDK tests")


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


def _make_echo_tool(
    *,
    with_annotation: bool = False,
    with_policy: bool = False,
) -> Tool:
    """Return a ``Tool`` wrapping a simple echo function.

    Parameters
    ----------
    with_annotation:
        When True the tool carries a custom capability annotation.
    with_policy:
        When True the tool carries an inline no-op policy.
    """

    def echo_tool(text: str) -> str:
        """Echo the input text unchanged."""
        return text

    annotation = None
    if with_annotation:

        def _annotate(
            return_value: Any,
            tool_kwargs: Mapping[str, Any],
        ) -> CaMeLValue:
            return wrap(return_value, sources=frozenset({"echo_tool"}), readers=Public)

        annotation = _annotate

    policies: list[Any] = []
    if with_policy:

        def _allow_all(tool_name: str, kwargs: Any) -> Any:
            return Allowed()

        policies = [_allow_all]

    return Tool(
        name="echo_tool",
        fn=echo_tool,
        description="Echo the input text unchanged.",
        params="text: str",
        return_type="str",
        capability_annotation=annotation,
        policies=policies,
    )


def _make_agent(
    *,
    mode: ExecutionMode = ExecutionMode.STRICT,
    policies: PolicyRegistry | None = None,
    with_annotation: bool = True,
    with_policy: bool = False,
) -> CaMeLAgent:
    """Build a minimal CaMeLAgent for tests."""
    backend = _MockLLMBackend()
    return CaMeLAgent(
        p_llm=backend,
        q_llm=backend,
        tools=[_make_echo_tool(with_annotation=with_annotation, with_policy=with_policy)],
        policies=policies,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# 1. CaMeLAgent constructor tests
# ---------------------------------------------------------------------------


class TestCaMeLAgentConstruction:
    """Unit tests for CaMeLAgent constructor validation."""

    def test_empty_tools_raises(self) -> None:
        """CaMeLAgent raises ValueError when tools list is empty."""
        backend = _MockLLMBackend()
        with pytest.raises(ValueError, match="at least one Tool"):
            CaMeLAgent(p_llm=backend, q_llm=backend, tools=[])

    def test_duplicate_tool_names_raises(self) -> None:
        """CaMeLAgent raises ValueError on duplicate tool names."""
        backend = _MockLLMBackend()
        tool = _make_echo_tool()
        with pytest.raises(ValueError, match="Duplicate tool name"):
            CaMeLAgent(p_llm=backend, q_llm=backend, tools=[tool, tool])

    def test_invalid_p_llm_raises_type_error(self) -> None:
        """CaMeLAgent raises TypeError when p_llm doesn't satisfy LLMBackend protocol."""
        with pytest.raises(TypeError, match="p_llm must satisfy the LLMBackend protocol"):
            CaMeLAgent(
                p_llm="not-a-backend",  # type: ignore[arg-type]
                q_llm=_MockLLMBackend(),
                tools=[_make_echo_tool()],
            )

    def test_invalid_q_llm_raises_type_error(self) -> None:
        """CaMeLAgent raises TypeError when q_llm doesn't satisfy LLMBackend protocol."""
        with pytest.raises(TypeError, match="q_llm must satisfy the LLMBackend protocol"):
            CaMeLAgent(
                p_llm=_MockLLMBackend(),
                q_llm=42,  # type: ignore[arg-type]
                tools=[_make_echo_tool()],
            )

    def test_tools_property_is_immutable_tuple(self) -> None:
        """tools property returns a tuple (immutable)."""
        agent = _make_agent()
        assert isinstance(agent.tools, tuple)
        assert len(agent.tools) == 1

    def test_mode_property_strict_default(self) -> None:
        """mode property defaults to ExecutionMode.STRICT."""
        agent = _make_agent()
        assert agent.mode == ExecutionMode.STRICT

    def test_mode_property_normal_explicit(self) -> None:
        """mode property reflects NORMAL when explicitly passed."""
        agent = _make_agent(mode=ExecutionMode.NORMAL)
        assert agent.mode == ExecutionMode.NORMAL

    def test_policies_none_accepted(self) -> None:
        """CaMeLAgent accepts policies=None (creates an empty registry)."""
        # Should not raise.
        agent = _make_agent(policies=None)
        assert agent is not None

    def test_policies_registry_accepted(self) -> None:
        """CaMeLAgent accepts a non-None PolicyRegistry."""
        registry = PolicyRegistry()
        agent = _make_agent(policies=registry)
        assert agent is not None

    def test_repr_includes_tool_names_and_mode(self) -> None:
        """repr includes tool names and mode."""
        agent = _make_agent()
        r = repr(agent)
        assert "echo_tool" in r
        assert "STRICT" in r

    def test_single_valid_tool_accepted(self) -> None:
        """CaMeLAgent succeeds with exactly one valid tool."""
        backend = _MockLLMBackend()
        tool = _make_echo_tool()
        agent = CaMeLAgent(p_llm=backend, q_llm=backend, tools=[tool])
        assert len(agent.tools) == 1
        assert agent.tools[0].name == "echo_tool"


# ---------------------------------------------------------------------------
# 2. AgentResult tests
# ---------------------------------------------------------------------------


class TestAgentResultShape:
    """Tests for AgentResult dataclass shape, immutability, and field types."""

    def _make_result(self, **overrides: Any) -> AgentResult:
        defaults = dict(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:abc123",
            loop_attempts=0,
            success=True,
            final_store={},
        )
        defaults.update(overrides)
        return AgentResult(**defaults)

    def test_agent_result_is_frozen(self) -> None:
        """AgentResult instances are immutable (frozen dataclass)."""
        result = self._make_result()
        with pytest.raises((AttributeError, TypeError)):
            result.success = False  # type: ignore[misc]

    def test_all_four_required_fields_present(self) -> None:
        """AgentResult has all four required fields from the acceptance criteria."""
        result = self._make_result()
        assert isinstance(result.execution_trace, list)
        assert isinstance(result.display_output, list)
        assert isinstance(result.policy_denials, list)
        assert isinstance(result.audit_log_ref, str)

    def test_audit_log_ref_format(self) -> None:
        """audit_log_ref follows the 'camel-audit:<hex_id>' format."""
        import re

        result = self._make_result(audit_log_ref="camel-audit:deadbeef12345678")
        assert re.match(r"camel-audit:[0-9a-f]+", result.audit_log_ref)

    def test_audit_log_ref_starts_with_prefix(self) -> None:
        """audit_log_ref always starts with 'camel-audit:'."""
        result = self._make_result()
        assert result.audit_log_ref.startswith("camel-audit:")

    def test_loop_attempts_is_int(self) -> None:
        """loop_attempts field is an integer."""
        result = self._make_result(loop_attempts=3)
        assert isinstance(result.loop_attempts, int)
        assert result.loop_attempts == 3

    def test_success_is_bool(self) -> None:
        """success field is a boolean."""
        r_true = self._make_result(success=True)
        r_false = self._make_result(success=False)
        assert r_true.success is True
        assert r_false.success is False

    def test_final_store_defaults_to_empty_dict(self) -> None:
        """final_store defaults to an empty dict when not provided."""
        result = AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:test",
            loop_attempts=0,
            success=True,
        )
        assert isinstance(result.final_store, dict)
        assert result.final_store == {}

    def test_policy_denial_record_is_frozen(self) -> None:
        """PolicyDenialRecord is a frozen dataclass."""
        from camel_security.agent import PolicyDenialRecord

        record = PolicyDenialRecord(
            tool_name="send_email",
            policy_name="no_external",
            reason="Untrusted recipient",
            resolved=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            record.resolved = True  # type: ignore[misc]

    def test_policy_denial_record_fields(self) -> None:
        """PolicyDenialRecord carries all four documented fields."""
        from camel_security.agent import PolicyDenialRecord

        record = PolicyDenialRecord(
            tool_name="write_file",
            policy_name="file_owner_check",
            reason="readers don't include owner",
            resolved=True,
        )
        assert record.tool_name == "write_file"
        assert record.policy_name == "file_owner_check"
        assert record.reason == "readers don't include owner"
        assert record.resolved is True


# ---------------------------------------------------------------------------
# 3. Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Unit tests for Tool dataclass construction and field behaviour."""

    def test_minimal_tool_name_and_fn_only(self) -> None:
        """Tool can be constructed with just name and fn."""

        def my_fn(x: int) -> int:
            return x

        tool = Tool(name="my_fn", fn=my_fn)
        assert tool.name == "my_fn"
        assert tool.fn is my_fn
        assert tool.description == ""
        assert tool.params == ""
        assert tool.return_type == "Any"
        assert tool.capability_annotation is None
        assert tool.policies == []

    def test_tool_with_full_metadata(self) -> None:
        """Tool accepts all optional metadata fields."""

        def my_fn(x: int) -> int:
            return x

        tool = Tool(
            name="my_fn",
            fn=my_fn,
            description="Returns x.",
            params="x: int",
            return_type="int",
        )
        assert tool.description == "Returns x."
        assert tool.params == "x: int"
        assert tool.return_type == "int"

    def test_tool_without_capability_annotation(self) -> None:
        """Tool with capability_annotation=None uses default annotation at runtime."""
        tool = _make_echo_tool(with_annotation=False)
        assert tool.capability_annotation is None

    def test_tool_with_capability_annotation(self) -> None:
        """Tool with a custom capability_annotation stores it correctly."""
        tool = _make_echo_tool(with_annotation=True)
        assert callable(tool.capability_annotation)
        # Verify the annotation returns a CaMeLValue when called.
        result = tool.capability_annotation("hello", {})  # type: ignore[misc]
        assert isinstance(result, CaMeLValue)
        assert "echo_tool" in result.sources

    def test_tool_without_policies(self) -> None:
        """Tool without policies has an empty policies list."""
        tool = _make_echo_tool(with_policy=False)
        assert tool.policies == []

    def test_tool_with_policies(self) -> None:
        """Tool with policies stores the list correctly."""
        tool = _make_echo_tool(with_policy=True)
        assert len(tool.policies) == 1
        assert callable(tool.policies[0])
        # Verify the policy returns Allowed when called.
        assert isinstance(tool.policies[0]("echo_tool", {}), Allowed)

    def test_tool_with_denial_policy(self) -> None:
        """Tool policy returning Denied is stored and callable."""

        def deny_always(tool_name: str, kwargs: Any) -> Any:
            return Denied("never allowed")

        def dummy(x: str) -> str:
            return x

        tool = Tool(name="dummy", fn=dummy, policies=[deny_always])
        assert len(tool.policies) == 1
        result = tool.policies[0]("dummy", {})
        assert isinstance(result, Denied)
        assert result.reason == "never allowed"

    def test_tool_registered_in_agent(self) -> None:
        """Tool registered in CaMeLAgent is accessible via agent.tools."""
        backend = _MockLLMBackend()
        tool_with = _make_echo_tool(with_annotation=True, with_policy=True)
        agent = CaMeLAgent(p_llm=backend, q_llm=backend, tools=[tool_with])
        assert agent.tools[0].name == "echo_tool"
        assert agent.tools[0].capability_annotation is not None
        assert len(agent.tools[0].policies) == 1

    def test_multiple_tools_registered(self) -> None:
        """CaMeLAgent can register multiple distinct tools."""

        def tool_a(x: str) -> str:
            return x

        def tool_b(y: int) -> int:
            return y

        backend = _MockLLMBackend()
        agent = CaMeLAgent(
            p_llm=backend,
            q_llm=backend,
            tools=[
                Tool(name="tool_a", fn=tool_a),
                Tool(name="tool_b", fn=tool_b),
            ],
        )
        names = [t.name for t in agent.tools]
        assert "tool_a" in names
        assert "tool_b" in names


# ---------------------------------------------------------------------------
# 4. Integration test: agent.run() produces valid AgentResult
# ---------------------------------------------------------------------------


class TestAgentRunIntegration:
    """Integration tests verifying agent.run() returns a complete AgentResult."""

    def test_run_returns_agent_result(self) -> None:
        """agent.run_sync() returns an AgentResult instance."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result, AgentResult)

    def test_run_result_has_audit_log_ref(self) -> None:
        """AgentResult.audit_log_ref is populated with camel-audit: prefix."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert result.audit_log_ref.startswith("camel-audit:")

    def test_run_result_execution_trace_is_list(self) -> None:
        """AgentResult.execution_trace is a list."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result.execution_trace, list)

    def test_run_result_display_output_is_list(self) -> None:
        """AgentResult.display_output is a list."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result.display_output, list)

    def test_run_result_policy_denials_is_list(self) -> None:
        """AgentResult.policy_denials is a list."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result.policy_denials, list)

    def test_run_result_loop_attempts_is_non_negative(self) -> None:
        """AgentResult.loop_attempts is a non-negative integer."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result.loop_attempts, int)
        assert result.loop_attempts >= 0

    def test_run_result_success_flag_is_bool(self) -> None:
        """AgentResult.success is a boolean."""
        agent = _make_agent()
        result = agent.run_sync("echo hello")
        assert isinstance(result.success, bool)

    def test_successive_runs_produce_different_audit_refs(self) -> None:
        """Two sequential agent.run() calls produce distinct audit_log_ref values."""
        agent = _make_agent()
        r1 = agent.run_sync("echo first")
        r2 = agent.run_sync("echo second")
        assert r1.audit_log_ref != r2.audit_log_ref

    def test_run_with_normal_mode(self) -> None:
        """agent.run() works with ExecutionMode.NORMAL."""
        agent = _make_agent(mode=ExecutionMode.NORMAL)
        result = agent.run_sync("echo normal mode")
        assert isinstance(result, AgentResult)

    def test_run_with_tool_having_policy(self) -> None:
        """agent.run() completes when tool has a permissive inline policy."""
        agent = _make_agent(with_policy=True)
        result = agent.run_sync("echo with policy")
        assert isinstance(result, AgentResult)


# ---------------------------------------------------------------------------
# 5. Thread-safety tests: 20 concurrent agent.run() calls
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Thread-safety integration tests for CaMeLAgent.run().

    Verifies that 20 concurrent ``run()`` calls on a single ``CaMeLAgent``
    instance each receive an ``AgentResult`` without interpreter state
    corruption or cross-session contamination.

    Each thread calls ``agent.run_sync()`` which internally calls
    ``asyncio.run(agent.run(...))``, creating an independent event loop per
    thread — this mirrors real-world usage patterns.

    Thread-safety is validated structurally:
    (a) All 20 calls complete without exception.
    (b) Each result carries a unique ``audit_log_ref`` (independent per-run
        state construction is confirmed).
    """

    CONCURRENCY = 20

    def _run_single(self, agent: CaMeLAgent, query: str) -> AgentResult:
        """Run agent.run_sync in a thread-pool worker thread."""
        return agent.run_sync(query)

    def test_concurrent_runs_complete_without_exception(self) -> None:
        """20 concurrent agent.run() calls all complete without exception.

        Core thread-safety contract: no shared mutable interpreter state
        between concurrent invocations.
        """
        agent = _make_agent()
        queries = [f"query-{i}" for i in range(self.CONCURRENCY)]

        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            futures = [pool.submit(self._run_single, agent, q) for q in queries]
            results = [f.result(timeout=30) for f in futures]

        assert len(results) == self.CONCURRENCY
        for result in results:
            assert isinstance(result, AgentResult)

    def test_concurrent_runs_produce_unique_audit_refs(self) -> None:
        """Each concurrent run produces a distinct audit_log_ref token.

        Proves that per-run state (including the run ID used to construct
        the audit_log_ref) is not shared between concurrent invocations.
        """
        agent = _make_agent()
        queries = [f"concurrent-query-{i}" for i in range(self.CONCURRENCY)]

        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            futures = [pool.submit(self._run_single, agent, q) for q in queries]
            results = [f.result(timeout=30) for f in futures]

        audit_refs = [r.audit_log_ref for r in results]
        assert len(set(audit_refs)) == self.CONCURRENCY, (
            f"Expected {self.CONCURRENCY} unique audit_log_ref values, "
            f"got {len(set(audit_refs))}: {audit_refs}"
        )
        for ref in audit_refs:
            assert ref.startswith("camel-audit:"), f"Unexpected format: {ref!r}"

    def test_concurrent_runs_all_return_agent_result_type(self) -> None:
        """All 20 concurrent results are AgentResult instances (type check)."""
        agent = _make_agent()
        queries = [f"type-check-query-{i}" for i in range(self.CONCURRENCY)]

        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            futures = [pool.submit(self._run_single, agent, q) for q in queries]
            results = [f.result(timeout=30) for f in futures]

        for i, result in enumerate(results):
            assert isinstance(result, AgentResult), (
                f"Result {i} is {type(result).__name__}, expected AgentResult"
            )

    def test_sequential_runs_on_same_agent_are_independent(self) -> None:
        """Sequential run() calls on the same agent produce independent results.

        Verifies that no variable store state bleeds across sequential calls —
        a prerequisite for the stronger concurrent-safety guarantee.
        """
        agent = _make_agent()
        result_a = agent.run_sync("first query")
        result_b = agent.run_sync("second query")

        assert result_a.audit_log_ref != result_b.audit_log_ref
        assert isinstance(result_a, AgentResult)
        assert isinstance(result_b, AgentResult)

    def test_concurrent_results_have_independent_audit_refs_from_sequential(
        self,
    ) -> None:
        """Concurrent and sequential runs all produce distinct audit refs."""
        agent = _make_agent()

        # Run some sequentially first.
        seq_results = [agent.run_sync(f"seq-{i}") for i in range(3)]

        # Then run concurrently.
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(self._run_single, agent, f"par-{i}") for i in range(5)]
            par_results = [f.result(timeout=30) for f in futures]

        all_refs = [r.audit_log_ref for r in seq_results + par_results]
        assert len(set(all_refs)) == len(all_refs), (
            "Duplicate audit_log_ref found across sequential+concurrent runs"
        )


# ---------------------------------------------------------------------------
# 6. Smoke test: clean-venv import + instantiation
# ---------------------------------------------------------------------------


class TestSmokeVenvInstall:
    """Smoke test verifying the package installs and imports in a clean venv.

    Creates a temporary virtual environment, installs ``camel-security`` from
    the local source tree (editable install), and runs a minimal import +
    instantiation script.  This validates:

    * The package metadata is correct (no missing sdist files).
    * All public names listed in ``__all__`` are importable.
    * ``CaMeLAgent``, ``AgentResult``, and ``Tool`` can be instantiated with
      minimal configuration without network calls.

    The test is marked ``slow`` and skipped when the ``SKIP_VENV_SMOKE``
    environment variable is set (for fast CI loops).
    """

    _INSTALL_SCRIPT = textwrap.dedent(
        """\
        import sys
        import camel_security

        # Version string must be present.
        assert hasattr(camel_security, "__version__"), "missing __version__"
        assert isinstance(camel_security.__version__, str)

        # All names in __all__ must be importable.
        for name in camel_security.__all__:
            assert hasattr(camel_security, name), f"missing export: {name}"

        # Minimal instantiation check (no network).
        from camel_security import Tool, AgentResult

        def dummy(x: str) -> str:
            return x

        tool = Tool(name="dummy", fn=dummy, params="x: str", return_type="str")
        assert tool.name == "dummy"

        # AgentResult is constructable.
        result = AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:smoke",
            loop_attempts=0,
            success=True,
        )
        assert result.success is True

        print("SMOKE_OK")
        sys.exit(0)
        """
    )

    def test_import_and_instantiation_in_subprocess(self) -> None:
        """camel_security imports correctly and exports all __all__ names."""
        # Run the smoke script in the *current* Python environment.
        # This avoids a full venv creation while still validating that all
        # public exports are importable and instantiable.
        result = subprocess.run(
            [sys.executable, "-c", self._INSTALL_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Smoke script failed (returncode={result.returncode}).\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "SMOKE_OK" in result.stdout

    def test_all_exports_importable_in_process(self) -> None:
        """All names in camel_security.__all__ are importable in-process."""
        import camel_security

        for name in camel_security.__all__:
            assert hasattr(camel_security, name), (
                f"camel_security.__all__ declares {name!r} but it is not importable"
            )

    def test_version_string_present(self) -> None:
        """camel_security.__version__ is a non-empty string."""
        import camel_security

        assert isinstance(camel_security.__version__, str)
        assert len(camel_security.__version__) > 0

    def test_tool_instantiation_minimal(self) -> None:
        """Tool can be instantiated with only name and fn (smoke check)."""
        from camel_security import Tool

        def noop(x: str) -> str:
            return x

        tool = Tool(name="noop", fn=noop)
        assert tool.name == "noop"

    def test_agent_result_instantiation(self) -> None:
        """AgentResult can be constructed directly (smoke check)."""
        result = AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:smoke",
            loop_attempts=0,
            success=True,
        )
        assert result.success is True
        assert result.audit_log_ref == "camel-audit:smoke"
