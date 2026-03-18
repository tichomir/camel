"""Tests for camel.policy — SecurityPolicyResult, PolicyRegistry, and helpers.

Covers:
- SecurityPolicyResult sealed type (Allowed, Denied)
- PolicyRegistry.register and .evaluate
- Multi-policy composition (all-must-allow)
- First-Denied short-circuit semantics
- is_trusted, can_readers_read_value, get_all_sources helpers
- PolicyRegistry.load_from_env configuration-driven loading
- NFR-2 compliance: no LLM calls or async code in evaluation path
- Interpreter integration: PolicyRegistry.evaluate called before tool dispatch
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Mapping
from unittest.mock import patch

import pytest

from camel.interpreter import CaMeLInterpreter, PolicyViolationError
from camel.policy import (
    Allowed,
    Denied,
    PolicyFn,
    PolicyRegistry,
    SecurityPolicyResult,
    can_readers_read_value,
    get_all_sources,
    is_trusted,
)
from camel.policy.interfaces import TRUSTED_SOURCE_LABELS
from camel.value import CaMeLValue, Public, wrap

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> PolicyRegistry:
    """Return a fresh empty PolicyRegistry."""
    return PolicyRegistry()


def _make_user_val(raw: str) -> CaMeLValue:
    """Wrap a string as a trusted user literal."""
    return wrap(raw, sources=frozenset({"User literal"}))


def _make_tool_val(raw: str, tool: str = "read_email") -> CaMeLValue:
    """Wrap a string as an untrusted tool output."""
    return wrap(raw, sources=frozenset({tool}))


def _make_camel_val(raw: str) -> CaMeLValue:
    """Wrap a string as a CaMeL-synthesised value."""
    return wrap(raw, sources=frozenset({"CaMeL"}))


# ---------------------------------------------------------------------------
# SecurityPolicyResult — sealed type
# ---------------------------------------------------------------------------


class TestSecurityPolicyResultSealing:
    """SecurityPolicyResult cannot be subclassed outside the module."""

    def test_allowed_is_allowed(self) -> None:
        assert Allowed().is_allowed() is True

    def test_denied_is_not_allowed(self) -> None:
        assert Denied("blocked").is_allowed() is False

    def test_denied_carries_reason(self) -> None:
        assert Denied("test reason").reason == "test reason"

    def test_allowed_repr(self) -> None:
        assert repr(Allowed()) == "Allowed()"

    def test_denied_repr(self) -> None:
        assert "blocked" in repr(Denied("blocked"))

    def test_allowed_equality(self) -> None:
        assert Allowed() == Allowed()

    def test_denied_equality_same_reason(self) -> None:
        assert Denied("r") == Denied("r")

    def test_denied_inequality_different_reason(self) -> None:
        assert Denied("r1") != Denied("r2")

    def test_allowed_denied_not_equal(self) -> None:
        assert Allowed() != Denied("x")

    def test_cannot_subclass_outside_module(self) -> None:
        with pytest.raises(TypeError, match="Cannot subclass SecurityPolicyResult"):

            class BadResult(SecurityPolicyResult):  # type: ignore[misc]
                def is_allowed(self) -> bool:
                    return True

    def test_allowed_is_instance_of_base(self) -> None:
        assert isinstance(Allowed(), SecurityPolicyResult)

    def test_denied_is_instance_of_base(self) -> None:
        assert isinstance(Denied("x"), SecurityPolicyResult)

    def test_allowed_hashable(self) -> None:
        s = {Allowed(), Allowed()}
        assert len(s) == 1

    def test_denied_hashable(self) -> None:
        s = {Denied("a"), Denied("a"), Denied("b")}
        assert len(s) == 2


# ---------------------------------------------------------------------------
# PolicyRegistry — register
# ---------------------------------------------------------------------------


class TestPolicyRegistryRegister:
    """PolicyRegistry.register stores policies and supports decorator syntax."""

    def test_register_stores_policy(self, registry: PolicyRegistry) -> None:
        def my_policy(
            tool_name: str, kwargs: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return Allowed()

        registry.register("my_tool", my_policy)
        assert registry.policy_count("my_tool") == 1

    def test_register_multiple_policies_accumulate(
        self, registry: PolicyRegistry
    ) -> None:
        def p1(
            tool_name: str, kwargs: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return Allowed()

        def p2(
            tool_name: str, kwargs: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return Allowed()

        registry.register("tool", p1)
        registry.register("tool", p2)
        assert registry.policy_count("tool") == 2

    def test_register_returns_policy_fn(self, registry: PolicyRegistry) -> None:
        def my_policy(
            tool_name: str, kwargs: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return Allowed()

        result = registry.register("tool", my_policy)
        assert result is my_policy

    def test_register_returns_same_policy_fn(self, registry: PolicyRegistry) -> None:
        """register() returns the policy function unchanged (enables chaining)."""

        def email_policy(
            tool_name: str, kwargs: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return Allowed()

        returned = registry.register("send_email", email_policy)
        assert returned is email_policy
        assert registry.policy_count("send_email") == 1

    def test_register_non_callable_raises_type_error(
        self, registry: PolicyRegistry
    ) -> None:
        with pytest.raises(TypeError, match="callable"):
            registry.register("tool", "not_a_function")  # type: ignore[arg-type]

    def test_zero_policies_for_unknown_tool(self, registry: PolicyRegistry) -> None:
        assert registry.policy_count("unknown") == 0

    def test_registered_tools_empty_initially(self, registry: PolicyRegistry) -> None:
        assert registry.registered_tools() == frozenset()

    def test_registered_tools_after_registration(self, registry: PolicyRegistry) -> None:
        registry.register("t1", lambda tn, kw: Allowed())
        registry.register("t2", lambda tn, kw: Allowed())
        assert registry.registered_tools() == frozenset({"t1", "t2"})


# ---------------------------------------------------------------------------
# PolicyRegistry — evaluate
# ---------------------------------------------------------------------------


class TestPolicyRegistryEvaluate:
    """PolicyRegistry.evaluate runs policies in order with short-circuit semantics."""

    def test_no_policies_returns_allowed(self, registry: PolicyRegistry) -> None:
        result = registry.evaluate("any_tool", {})
        assert isinstance(result, Allowed)

    def test_single_allow_returns_allowed(self, registry: PolicyRegistry) -> None:
        registry.register("tool", lambda tn, kw: Allowed())
        assert isinstance(registry.evaluate("tool", {}), Allowed)

    def test_single_deny_returns_denied(self, registry: PolicyRegistry) -> None:
        registry.register("tool", lambda tn, kw: Denied("blocked"))
        result = registry.evaluate("tool", {})
        assert isinstance(result, Denied)
        assert result.reason == "blocked"

    def test_all_allow_returns_allowed(self, registry: PolicyRegistry) -> None:
        registry.register("tool", lambda tn, kw: Allowed())
        registry.register("tool", lambda tn, kw: Allowed())
        registry.register("tool", lambda tn, kw: Allowed())
        assert isinstance(registry.evaluate("tool", {}), Allowed)

    def test_first_deny_short_circuits(self, registry: PolicyRegistry) -> None:
        call_log: list[str] = []

        def p1(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            call_log.append("p1")
            return Denied("first denial")

        def p2(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            call_log.append("p2")
            return Allowed()

        registry.register("tool", p1)
        registry.register("tool", p2)
        result = registry.evaluate("tool", {})
        assert isinstance(result, Denied)
        assert result.reason == "first denial"
        # p2 must NOT have been called
        assert call_log == ["p1"]

    def test_second_deny_stops_further_evaluation(
        self, registry: PolicyRegistry
    ) -> None:
        call_log: list[str] = []

        def p1(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            call_log.append("p1")
            return Allowed()

        def p2(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            call_log.append("p2")
            return Denied("second denial")

        def p3(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            call_log.append("p3")
            return Allowed()

        registry.register("tool", p1)
        registry.register("tool", p2)
        registry.register("tool", p3)
        result = registry.evaluate("tool", {})
        assert isinstance(result, Denied)
        assert "p3" not in call_log

    def test_policies_receive_tool_name(self, registry: PolicyRegistry) -> None:
        received: list[str] = []

        def p(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            received.append(tn)
            return Allowed()

        registry.register("my_tool", p)
        registry.evaluate("my_tool", {})
        assert received == ["my_tool"]

    def test_policies_receive_kwargs(self, registry: PolicyRegistry) -> None:
        received: list[Mapping[str, CaMeLValue]] = []
        val = _make_user_val("hello")

        def p(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            received.append(kw)
            return Allowed()

        registry.register("tool", p)
        registry.evaluate("tool", {"arg": val})
        assert received[0]["arg"] is val

    def test_evaluate_is_synchronous(self, registry: PolicyRegistry) -> None:
        """evaluate() must not return a coroutine — it must be synchronous."""
        import inspect

        registry.register("tool", lambda tn, kw: Allowed())
        result = registry.evaluate("tool", {})
        assert not inspect.iscoroutine(result)

    def test_evaluate_contains_no_llm_calls(self, registry: PolicyRegistry) -> None:
        """Confirm evaluate() invokes no LLM adapter or async machinery."""
        # We simply verify it works without any LLM infrastructure configured.
        registry.register("tool", lambda tn, kw: Denied("deny"))
        result = registry.evaluate("tool", {})
        assert isinstance(result, Denied)

    def test_evaluate_different_tools_independent(
        self, registry: PolicyRegistry
    ) -> None:
        registry.register("tool_a", lambda tn, kw: Denied("only a"))
        registry.register("tool_b", lambda tn, kw: Allowed())

        assert isinstance(registry.evaluate("tool_a", {}), Denied)
        assert isinstance(registry.evaluate("tool_b", {}), Allowed)
        # Unregistered tool defaults to Allowed
        assert isinstance(registry.evaluate("tool_c", {}), Allowed)


# ---------------------------------------------------------------------------
# Helper: is_trusted
# ---------------------------------------------------------------------------


class TestIsTrusted:
    """is_trusted returns True only for values with purely trusted sources."""

    def test_user_literal_is_trusted(self) -> None:
        assert is_trusted(_make_user_val("alice@example.com")) is True

    def test_camel_source_is_trusted(self) -> None:
        assert is_trusted(_make_camel_val("derived")) is True

    def test_both_trusted_sources_is_trusted(self) -> None:
        v = wrap("x", sources=frozenset({"User literal", "CaMeL"}))
        assert is_trusted(v) is True

    def test_tool_output_is_not_trusted(self) -> None:
        assert is_trusted(_make_tool_val("bob@example.com")) is False

    def test_mixed_trusted_and_untrusted_is_not_trusted(self) -> None:
        v = wrap("x", sources=frozenset({"User literal", "read_email"}))
        assert is_trusted(v) is False

    def test_empty_sources_is_not_trusted(self) -> None:
        v = wrap("x", sources=frozenset())
        assert is_trusted(v) is False

    def test_no_sources_default_is_not_trusted(self) -> None:
        v = wrap("x")  # sources defaults to frozenset()
        assert is_trusted(v) is False

    def test_trusted_source_labels_constant(self) -> None:
        assert TRUSTED_SOURCE_LABELS == frozenset({"User literal", "CaMeL"})


# ---------------------------------------------------------------------------
# Helper: can_readers_read_value
# ---------------------------------------------------------------------------


class TestCanReadersReadValue:
    """can_readers_read_value handles Public sentinel and frozenset[str] readers."""

    def test_public_readers_allows_any_reader(self) -> None:
        v = wrap("data", readers=Public)
        assert can_readers_read_value(v, "anyone@example.com") is True

    def test_public_readers_allows_empty_string(self) -> None:
        v = wrap("data", readers=Public)
        assert can_readers_read_value(v, "") is True

    def test_specific_reader_in_set_is_allowed(self) -> None:
        v = wrap("data", readers=frozenset({"alice@example.com"}))
        assert can_readers_read_value(v, "alice@example.com") is True

    def test_reader_not_in_set_is_denied(self) -> None:
        v = wrap("data", readers=frozenset({"alice@example.com"}))
        assert can_readers_read_value(v, "eve@example.com") is False

    def test_empty_readers_set_denies_all(self) -> None:
        v = wrap("data", readers=frozenset())
        assert can_readers_read_value(v, "alice@example.com") is False

    def test_multiple_readers_in_set(self) -> None:
        v = wrap("data", readers=frozenset({"alice@example.com", "bob@example.com"}))
        assert can_readers_read_value(v, "alice@example.com") is True
        assert can_readers_read_value(v, "bob@example.com") is True
        assert can_readers_read_value(v, "carol@example.com") is False

    def test_default_readers_is_public(self) -> None:
        v = wrap("data")  # readers defaults to Public
        assert can_readers_read_value(v, "anyone") is True


# ---------------------------------------------------------------------------
# Helper: get_all_sources
# ---------------------------------------------------------------------------


class TestGetAllSources:
    """get_all_sources returns the sources frozenset from a CaMeLValue."""

    def test_returns_sources_frozenset(self) -> None:
        v = wrap("x", sources=frozenset({"tool_a", "tool_b"}))
        result = get_all_sources(v)
        assert result == frozenset({"tool_a", "tool_b"})

    def test_empty_sources(self) -> None:
        v = wrap("x", sources=frozenset())
        assert get_all_sources(v) == frozenset()

    def test_user_literal_source(self) -> None:
        v = _make_user_val("hello")
        assert get_all_sources(v) == frozenset({"User literal"})

    def test_returns_frozenset_type(self) -> None:
        v = wrap("x", sources=frozenset({"s1"}))
        assert isinstance(get_all_sources(v), frozenset)


# ---------------------------------------------------------------------------
# Configuration-driven loading (CAMEL_POLICY_MODULE)
# ---------------------------------------------------------------------------


class TestLoadFromEnv:
    """PolicyRegistry.load_from_env reads CAMEL_POLICY_MODULE."""

    def test_empty_env_var_returns_empty_registry(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAMEL_POLICY_MODULE", None)
            registry = PolicyRegistry.load_from_env()
        assert registry.registered_tools() == frozenset()

    def test_unset_env_var_returns_empty_registry(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "CAMEL_POLICY_MODULE"}
        with patch.dict(os.environ, env, clear=True):
            registry = PolicyRegistry.load_from_env()
        assert registry.registered_tools() == frozenset()

    def test_load_from_env_calls_configure_policies(self) -> None:
        module_name = "_test_policy_module_cfg"
        mod = types.ModuleType(module_name)

        called: list[bool] = []

        def configure_policies(reg: PolicyRegistry) -> None:
            called.append(True)
            reg.register("configured_tool", lambda tn, kw: Allowed())

        mod.configure_policies = configure_policies  # type: ignore[attr-defined]
        sys.modules[module_name] = mod
        try:
            with patch.dict(os.environ, {"CAMEL_POLICY_MODULE": module_name}):
                registry = PolicyRegistry.load_from_env()
            assert called == [True]
            assert "configured_tool" in registry.registered_tools()
        finally:
            del sys.modules[module_name]

    def test_load_from_env_raises_import_error_for_missing_module(self) -> None:
        with patch.dict(
            os.environ, {"CAMEL_POLICY_MODULE": "nonexistent._module_xyz"}
        ):
            with pytest.raises(ImportError):
                PolicyRegistry.load_from_env()

    def test_load_from_env_raises_attribute_error_if_no_configure(self) -> None:
        module_name = "_test_policy_no_configure"
        mod = types.ModuleType(module_name)
        # deliberately no configure_policies attribute
        sys.modules[module_name] = mod
        try:
            with patch.dict(os.environ, {"CAMEL_POLICY_MODULE": module_name}):
                with pytest.raises(AttributeError):
                    PolicyRegistry.load_from_env()
        finally:
            del sys.modules[module_name]

    def test_policies_loaded_from_env_are_evaluated(self) -> None:
        module_name = "_test_policy_module_eval"
        mod = types.ModuleType(module_name)

        def configure_policies(reg: PolicyRegistry) -> None:
            reg.register("secured_tool", lambda tn, kw: Denied("env-policy blocked"))

        mod.configure_policies = configure_policies  # type: ignore[attr-defined]
        sys.modules[module_name] = mod
        try:
            with patch.dict(os.environ, {"CAMEL_POLICY_MODULE": module_name}):
                registry = PolicyRegistry.load_from_env()
            result = registry.evaluate("secured_tool", {})
            assert isinstance(result, Denied)
            assert result.reason == "env-policy blocked"
        finally:
            del sys.modules[module_name]


# ---------------------------------------------------------------------------
# Interpreter integration: PolicyRegistry.evaluate called before tool dispatch
# ---------------------------------------------------------------------------


class TestInterpreterPolicyIntegration:
    """The interpreter calls PolicyRegistry.evaluate before executing tool calls."""

    def _make_tool(self) -> tuple[dict[str, object], list[str]]:
        """Return a tools dict and a call log."""
        call_log: list[str] = []

        def dummy_tool(x: str) -> CaMeLValue:
            call_log.append("executed")
            return wrap(x, sources=frozenset({"dummy_tool"}))

        return {"dummy_tool": dummy_tool}, call_log

    def test_allowed_policy_permits_tool_execution(self) -> None:
        tools, call_log = self._make_tool()
        registry = PolicyRegistry()
        registry.register("dummy_tool", lambda tn, kw: Allowed())

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        interp.exec('result = dummy_tool("hello")')
        assert call_log == ["executed"]
        assert interp.get("result").raw == "hello"

    def test_denied_policy_raises_policy_violation_error(self) -> None:
        tools, call_log = self._make_tool()
        registry = PolicyRegistry()
        registry.register("dummy_tool", lambda tn, kw: Denied("access denied"))

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec('result = dummy_tool("hello")')
        assert "access denied" in str(exc_info.value)
        # tool must NOT have been executed
        assert call_log == []

    def test_tool_not_executed_when_denied(self) -> None:
        call_log: list[str] = []

        def side_effect_tool(x: str) -> CaMeLValue:
            call_log.append(f"executed:{x}")
            return wrap(x, sources=frozenset({"t"}))

        tools = {"side_effect_tool": side_effect_tool}
        registry = PolicyRegistry()
        registry.register("side_effect_tool", lambda tn, kw: Denied("blocked"))

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        with pytest.raises(PolicyViolationError):
            interp.exec('r = side_effect_tool("secret")')
        assert call_log == []

    def test_no_policy_engine_permits_all_tools(self) -> None:
        tools, call_log = self._make_tool()
        interp = CaMeLInterpreter(tools=tools)
        interp.exec('result = dummy_tool("no-policy")')
        assert call_log == ["executed"]

    def test_evaluate_called_with_correct_tool_name(self) -> None:
        seen_names: list[str] = []

        def spy_policy(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            seen_names.append(tn)
            return Allowed()

        def my_fn(x: str) -> CaMeLValue:
            return wrap(x, sources=frozenset({"my_fn"}))

        registry = PolicyRegistry()
        registry.register("my_fn", spy_policy)

        interp = CaMeLInterpreter(
            tools={"my_fn": my_fn}, policy_engine=registry  # type: ignore[arg-type]
        )
        interp.exec('r = my_fn("hi")')
        assert seen_names == ["my_fn"]

    def test_policy_receives_camelvalue_kwargs(self) -> None:
        received_kwargs: list[Mapping[str, CaMeLValue]] = []

        def spy_policy(tn: str, kw: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
            received_kwargs.append(kw)
            return Allowed()

        def greet(name: str) -> CaMeLValue:
            return wrap(f"hello {name}", sources=frozenset({"greet"}))

        registry = PolicyRegistry()
        registry.register("greet", spy_policy)

        interp = CaMeLInterpreter(
            tools={"greet": greet}, policy_engine=registry  # type: ignore[arg-type]
        )
        interp.exec('r = greet("world")')
        assert len(received_kwargs) == 1
        assert "name" in received_kwargs[0]
        assert isinstance(received_kwargs[0]["name"], CaMeLValue)

    def test_multi_policy_composition_all_must_allow(self) -> None:
        tools, call_log = self._make_tool()
        registry = PolicyRegistry()
        registry.register("dummy_tool", lambda tn, kw: Allowed())
        registry.register("dummy_tool", lambda tn, kw: Denied("second policy denies"))
        registry.register("dummy_tool", lambda tn, kw: Allowed())

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec('result = dummy_tool("x")')
        assert "second policy denies" in str(exc_info.value)
        assert call_log == []

    def test_policy_violation_error_contains_tool_name(self) -> None:
        def t(x: str) -> CaMeLValue:
            return wrap(x, sources=frozenset({"t"}))

        registry = PolicyRegistry()
        registry.register("restricted_fn", lambda tn, kw: Denied("reason"))

        interp = CaMeLInterpreter(
            tools={"restricted_fn": t}, policy_engine=registry  # type: ignore[arg-type]
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec('r = restricted_fn("v")')
        assert "restricted_fn" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Audit log (NFR-6): every evaluate() call is logged
# ---------------------------------------------------------------------------


class TestInterpreterAuditLog:
    """CaMeLInterpreter.audit_log records every policy evaluate() call."""

    def _make_tool(self, name: str = "my_tool") -> dict[str, object]:
        def tool_fn(x: str) -> CaMeLValue:
            return wrap(x, sources=frozenset({name}))

        return {name: tool_fn}

    def test_allowed_call_appends_audit_entry(self) -> None:
        tools = self._make_tool()
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Allowed())

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        interp.exec('r = my_tool("hi")')

        log = interp.audit_log
        assert len(log) == 1
        assert log[0].tool_name == "my_tool"
        assert log[0].outcome == "Allowed"
        assert log[0].reason is None

    def test_denied_call_appends_audit_entry(self) -> None:
        tools = self._make_tool()
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("blocked reason"))

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        with pytest.raises(PolicyViolationError):
            interp.exec('r = my_tool("hi")')

        log = interp.audit_log
        assert len(log) == 1
        assert log[0].tool_name == "my_tool"
        assert log[0].outcome == "Denied"
        assert log[0].reason == "blocked reason"

    def test_audit_entry_has_timestamp(self) -> None:
        tools = self._make_tool()
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Allowed())

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        interp.exec('r = my_tool("hi")')

        entry = interp.audit_log[0]
        # timestamp must be a non-empty ISO-8601 string
        assert isinstance(entry.timestamp, str)
        assert len(entry.timestamp) > 0

    def test_multiple_tool_calls_accumulate_log(self) -> None:
        def tool_a(x: str) -> CaMeLValue:
            return wrap(x, sources=frozenset({"tool_a"}))

        def tool_b(x: str) -> CaMeLValue:
            return wrap(x, sources=frozenset({"tool_b"}))

        registry = PolicyRegistry()
        registry.register("tool_a", lambda tn, kw: Allowed())
        registry.register("tool_b", lambda tn, kw: Denied("b denied"))

        interp = CaMeLInterpreter(
            tools={"tool_a": tool_a, "tool_b": tool_b},
            policy_engine=registry,  # type: ignore[arg-type]
        )
        interp.exec('r = tool_a("x")')
        with pytest.raises(PolicyViolationError):
            interp.exec('s = tool_b("y")')

        log = interp.audit_log
        assert len(log) == 2
        assert log[0].outcome == "Allowed"
        assert log[0].tool_name == "tool_a"
        assert log[1].outcome == "Denied"
        assert log[1].tool_name == "tool_b"
        assert log[1].reason == "b denied"

    def test_no_policy_engine_produces_empty_log(self) -> None:
        tools = self._make_tool()
        interp = CaMeLInterpreter(tools=tools)
        interp.exec('r = my_tool("hi")')
        assert interp.audit_log == []

    def test_audit_log_returns_snapshot(self) -> None:
        """audit_log returns a copy; mutating it does not affect internal state."""
        tools = self._make_tool()
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Allowed())

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        interp.exec('r = my_tool("x")')
        snap = interp.audit_log
        snap.clear()
        assert len(interp.audit_log) == 1

    def test_denied_entry_before_exception(self) -> None:
        """Audit entry is written before PolicyViolationError propagates."""
        tools = self._make_tool()
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("stop"))

        interp = CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]
        try:
            interp.exec('r = my_tool("x")')
        except PolicyViolationError:
            pass
        assert len(interp.audit_log) == 1
        assert interp.audit_log[0].outcome == "Denied"


# ---------------------------------------------------------------------------
# NFR-2: policy evaluation is purely synchronous with no LLM calls
# ---------------------------------------------------------------------------


class TestNFR2Compliance:
    """Confirm policy evaluation path has no async, threading, or LLM calls."""

    def test_evaluate_returns_non_coroutine(self) -> None:
        import inspect

        registry = PolicyRegistry()
        result = registry.evaluate("any_tool", {})
        assert not inspect.iscoroutine(result)
        assert not inspect.isawaitable(result)

    def test_evaluate_with_policy_returns_non_coroutine(self) -> None:
        import inspect

        registry = PolicyRegistry()
        registry.register("tool", lambda tn, kw: Denied("test"))
        result = registry.evaluate("tool", {})
        assert not inspect.iscoroutine(result)

    def test_policy_fn_type_alias_is_synchronous_callable(self) -> None:
        """PolicyFn type alias requires synchronous callable signature."""
        # Verify a sync callable satisfies the PolicyFn contract
        def _always_allowed(tn: str, kw: object) -> SecurityPolicyResult:
            return Allowed()

        fn: PolicyFn = _always_allowed
        result = fn("tool", {})
        assert isinstance(result, SecurityPolicyResult)

    def test_registry_uses_no_llm_infrastructure(self) -> None:
        """PolicyRegistry must function without any LLM backend configured."""
        # This test would fail if evaluate() tried to import or call LLM code
        registry = PolicyRegistry()
        registry.register("tool", lambda tn, kw: Denied("ok"))
        # Should not raise ImportError, AttributeError, or any LLM-related error
        result = registry.evaluate("tool", {})
        assert isinstance(result, Denied)


# ---------------------------------------------------------------------------
# Public API exports from camel.policy
# ---------------------------------------------------------------------------


class TestPublicExports:
    """All documented public symbols must be importable from camel.policy."""

    def test_security_policy_result_exported(self) -> None:
        from camel.policy import SecurityPolicyResult

        assert SecurityPolicyResult is not None

    def test_allowed_exported(self) -> None:
        from camel.policy import Allowed

        assert Allowed is not None

    def test_denied_exported(self) -> None:
        from camel.policy import Denied

        assert Denied is not None

    def test_policy_fn_exported(self) -> None:
        from camel.policy import PolicyFn

        assert PolicyFn is not None

    def test_policy_registry_exported(self) -> None:
        from camel.policy import PolicyRegistry

        assert PolicyRegistry is not None

    def test_is_trusted_exported(self) -> None:
        from camel.policy import is_trusted

        assert callable(is_trusted)

    def test_can_readers_read_value_exported(self) -> None:
        from camel.policy import can_readers_read_value

        assert callable(can_readers_read_value)

    def test_get_all_sources_exported(self) -> None:
        from camel.policy import get_all_sources

        assert callable(get_all_sources)
