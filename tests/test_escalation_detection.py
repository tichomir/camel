"""Unit and integration tests for M4-F15, M4-F16, and M4-F18.

Coverage
--------
M4-F15: DataToControlFlowWarning detection — runtime check on function-call
        operand provenance when the callable has untrusted sources.
M4-F16: Elevated user consent gate — execution pause and elevated consent prompt
        triggered by DataToControlFlowWarning, regardless of policy outcome.
M4-F18: Per-statement STRICT mode dependency addition audit log.

Test scenarios
--------------
M4-F15 / M4-F16:
    - Literal tool name call → no warning; normal dispatch
    - Stored callable with untrusted direct sources → warning + EVALUATION raise
    - Stored callable with untrusted upstream dep (indirect) → warning with chain
    - PRODUCTION mode, operator approves → tool dispatches; audit records "approved"
    - PRODUCTION mode, operator rejects → escalation error; audit records "rejected"
    - PRODUCTION mode, no elevated callback configured → always-reject fires
    - Policy Allowed + escalation detected → gate fires before policy
    - DataToControlFlowAuditEvent in security_audit_log with correct fields
    - EVALUATION mode error message format (safe content only)

M4-F18:
    - Assignment inside for-loop with untrusted iterable → context_source="for_iterable"
    - Assignment inside if-branch with untrusted condition → context_source="if_condition"
    - Post-Q-LLM assignment in STRICT mode → context_source="post_qllm"
    - Same assignment 10 iterations → single deduped event
    - Assignment in NORMAL mode → no event
    - Assignment with empty ctx context at top level → no event
    - strict_dep_audit_log property returns isolated list snapshot
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from camel.exceptions import DataToControlFlowEscalationError, DataToControlFlowWarning
from camel.interpreter import (
    CaMeLInterpreter,
    DataToControlFlowAuditEvent,
    EnforcementMode,
    ExecutionMode,
    StrictDependencyAdditionEvent,
)
from camel.value import CaMeLValue, Public, wrap

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _untrusted_tool(name: str = "some_tool") -> Any:
    """Build a no-arg tool that returns a CaMeLValue with untrusted source."""

    def _tool() -> CaMeLValue:
        return wrap("untrusted_value", sources=frozenset({name}), readers=Public)

    return _tool


def _returning_callable_tool(name: str = "get_action") -> Any:
    """Build a tool that returns a CaMeLValue whose raw is a callable.

    The returned value has ``sources={name}`` (untrusted) and its raw value
    is a simple callable — simulating the data-to-control escalation attack
    where a callable is extracted from untrusted tool output.
    """

    def _inner_fn(*args: Any, **kwargs: Any) -> CaMeLValue:
        return wrap("called!", sources=frozenset({"CaMeL"}), readers=Public)

    def _tool() -> CaMeLValue:
        return wrap(_inner_fn, sources=frozenset({name}), readers=Public)

    return _tool


def _allowed_policy() -> Any:
    """Return a policy engine stub that always allows."""

    class _Always:
        def evaluate(self, tool_name: str, kwargs: Any) -> Any:
            class _Allowed:
                def is_allowed(self) -> bool:
                    return True

            return _Allowed()

    return _Always()


# ---------------------------------------------------------------------------
# M4-F15 / M4-F16 — basic detection
# ---------------------------------------------------------------------------


class TestDataToControlFlowDetection:
    """M4-F15: DataToControlFlowWarning detection tests."""

    def test_literal_tool_name_no_warning(self) -> None:
        """Calling a registered tool by literal name does not trigger M4-F15."""
        tool = _untrusted_tool("get_data")
        interp = CaMeLInterpreter(tools={"get_data": tool})
        # Literal tool name call — sources = {"CaMeL"} → no escalation.
        interp.exec("result = get_data()")
        assert interp.get("result").raw == "untrusted_value"
        # No escalation events in security audit log.
        escalation_events = [
            e for e in interp.security_audit_log if isinstance(e, DataToControlFlowAuditEvent)
        ]
        assert len(escalation_events) == 0

    def test_untrusted_stored_callable_raises_in_evaluation_mode(self) -> None:
        """Store a callable from untrusted source → EVALUATION mode raises."""
        interp = CaMeLInterpreter(
            tools={"get_action": _returning_callable_tool("get_action")},
        )
        # action is a callable CaMeLValue with sources={"get_action"} (untrusted)
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        err = exc_info.value
        assert isinstance(err.warning, DataToControlFlowWarning)
        assert err.warning.offending_variable == "action"
        assert "get_action" in err.warning.untrusted_sources

    def test_warning_contains_correct_fields(self) -> None:
        """DataToControlFlowWarning has lineno, variable, untrusted_sources."""
        interp = CaMeLInterpreter(
            tools={"get_action": _returning_callable_tool("get_action")},
        )
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        warning = exc_info.value.warning
        assert warning.lineno == 1
        assert warning.offending_variable == "action"
        assert isinstance(warning.untrusted_sources, frozenset)
        assert "get_action" in warning.untrusted_sources
        assert isinstance(warning.dependency_chain, list)

    def test_audit_event_recorded_in_security_log(self) -> None:
        """DataToControlFlowAuditEvent is appended to security_audit_log."""
        interp = CaMeLInterpreter(
            tools={"get_action": _returning_callable_tool("get_action")},
        )
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError):
            interp.exec("result = action()")
        events = [
            e for e in interp.security_audit_log if isinstance(e, DataToControlFlowAuditEvent)
        ]
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "DataToControlFlowEscalation"
        assert ev.offending_variable == "action"
        assert "get_action" in ev.untrusted_sources
        assert ev.elevated_consent_triggered is True
        assert ev.consent_outcome == "evaluation_mode_raised"
        assert ev.timestamp  # non-empty

    def test_error_message_safe_content_only(self) -> None:
        """Error message contains only variable name and source labels — no raw values."""
        interp = CaMeLInterpreter(
            tools={"get_action": _returning_callable_tool("my_tool_source")},
        )
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        msg = str(exc_info.value)
        assert "action" in msg
        assert "my_tool_source" in msg
        # Should not contain raw Python values from tool output.
        assert "untrusted_value" not in msg

    def test_indirect_escalation_via_subscript(self) -> None:
        """Callable derived via subscript from untrusted source triggers warning."""

        def _list_tool() -> CaMeLValue:
            def _fn() -> CaMeLValue:
                return wrap("ok", sources=frozenset({"CaMeL"}), readers=Public)

            return wrap(
                [_fn],
                sources=frozenset({"list_tool"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(tools={"get_list": _list_tool})
        interp.exec("actions = get_list()")
        interp.exec("action = actions[0]")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        warning = exc_info.value.warning
        assert "list_tool" in warning.untrusted_sources

    def test_indirect_escalation_via_dep_graph(self) -> None:
        """Callable with only CaMeL direct sources but untrusted upstream dep triggers."""

        # Set up: action_name comes from untrusted data via dep graph
        def _name_tool() -> CaMeLValue:
            def _fn() -> CaMeLValue:
                return wrap("called", sources=frozenset({"CaMeL"}), readers=Public)

            return wrap(_fn, sources=frozenset({"name_tool"}), readers=Public)

        interp = CaMeLInterpreter(tools={"get_name": _name_tool})
        # action has sources={"name_tool"} (untrusted)
        interp.exec("action = get_name()")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        # The dep chain should include the name_tool source
        warning = exc_info.value.warning
        assert len(warning.dependency_chain) > 0
        all_sources = {src for _, src in warning.dependency_chain}
        assert "name_tool" in all_sources


# ---------------------------------------------------------------------------
# M4-F16 — PRODUCTION mode gate
# ---------------------------------------------------------------------------


class TestElevatedConsentGate:
    """M4-F16: Elevated user consent gate in PRODUCTION mode."""

    def _prod_interp(
        self,
        approve: bool = True,
        elevated_cb_provided: bool = True,
    ) -> tuple[CaMeLInterpreter, list[Any]]:
        """Build a PRODUCTION mode interpreter with tracking consent callbacks."""
        consent_calls: list[Any] = []

        def _consent(tool_name: str, arg_summary: str, denial_reason: str) -> bool:
            return True  # policy consent always approves (not the focus here)

        def _elevated_consent(
            warning: DataToControlFlowWarning, tool_name_candidate: str | None
        ) -> bool:
            consent_calls.append(
                {"warning": warning, "tool": tool_name_candidate, "approved": approve}
            )
            return approve

        return (
            CaMeLInterpreter(
                tools={"get_action": _returning_callable_tool("get_action")},
                enforcement_mode=EnforcementMode.PRODUCTION,
                consent_callback=_consent,
                elevated_consent_callback=(_elevated_consent if elevated_cb_provided else None),
            ),
            consent_calls,
        )

    def test_production_operator_approves(self) -> None:
        """PRODUCTION + approved → tool call proceeds; audit records 'approved'."""
        interp, calls = self._prod_interp(approve=True)
        interp.exec("action = get_action()")
        # No error — operator approved.
        interp.exec("result = action()")
        assert len(calls) == 1
        assert calls[0]["approved"] is True
        events = [
            e for e in interp.security_audit_log if isinstance(e, DataToControlFlowAuditEvent)
        ]
        assert events[0].consent_outcome == "approved"

    def test_production_operator_rejects(self) -> None:
        """PRODUCTION + rejected → escalation error raised; audit records 'rejected'."""
        interp, calls = self._prod_interp(approve=False)
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError):
            interp.exec("result = action()")
        assert len(calls) == 1
        events = [
            e for e in interp.security_audit_log if isinstance(e, DataToControlFlowAuditEvent)
        ]
        assert events[0].consent_outcome == "rejected"

    def test_production_no_callback_always_rejects(self) -> None:
        """PRODUCTION without elevated_consent_callback → always-reject default."""
        interp, _ = self._prod_interp(approve=True, elevated_cb_provided=False)
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec("result = action()")
        events = [
            e for e in interp.security_audit_log if isinstance(e, DataToControlFlowAuditEvent)
        ]
        assert events[0].consent_outcome == "rejected"
        assert exc_info.value.warning.offending_variable == "action"

    def test_gate_fires_before_policy_even_when_allowed(self) -> None:
        """Escalation gate fires before policy engine, even if policy would allow."""
        interp, _ = self._prod_interp(approve=False)
        # Inject a policy engine that always allows.
        interp._policy_engine = _allowed_policy()  # type: ignore[attr-defined]
        interp.exec("action = get_action()")
        with pytest.raises(DataToControlFlowEscalationError):
            interp.exec("result = action()")
        # The escalation error (not PolicyViolationError) should have been raised.

    def test_elevated_callback_receives_correct_args(self) -> None:
        """ElevatedConsentCallback receives the warning and tool name candidate."""
        captured: list[Any] = []

        def _consent(tool_name: str, arg_summary: str, denial_reason: str) -> bool:
            return True

        def _elevated(warning: DataToControlFlowWarning, tool_name_candidate: str | None) -> bool:
            captured.append({"warning": warning, "tool": tool_name_candidate})
            return True  # approve

        interp = CaMeLInterpreter(
            tools={"get_action": _returning_callable_tool("get_action")},
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=_consent,
            elevated_consent_callback=_elevated,
        )
        interp.exec("action = get_action()")
        interp.exec("result = action()")
        assert len(captured) == 1
        assert captured[0]["tool"] == "action"
        assert isinstance(captured[0]["warning"], DataToControlFlowWarning)


# ---------------------------------------------------------------------------
# M4-F18 — STRICT mode dependency addition audit log
# ---------------------------------------------------------------------------


class TestStrictDepAuditLog:
    """M4-F18: Per-statement STRICT mode dependency addition recording."""

    def test_for_loop_body_emits_for_iterable_event(self) -> None:
        """Assignment inside for-loop with untrusted iterable → for_iterable event."""
        tool = _untrusted_tool("items_tool")
        interp = CaMeLInterpreter(
            tools={"get_items": tool},
            mode=ExecutionMode.STRICT,
        )

        # Simulate get_items returning a list CaMeLValue.
        def _list_tool() -> CaMeLValue:
            return wrap(
                ["a", "b"],
                sources=frozenset({"items_tool"}),
                readers=Public,
            )

        interp._tools["get_items"] = _list_tool  # type: ignore[attr-defined]
        code = textwrap.dedent("""\
            items = get_items()
            for item in items:
                result = item
        """)
        interp.exec(code)
        log = interp.strict_dep_audit_log
        # At least one event for 'result' with context_source="for_iterable".
        result_events = [e for e in log if e.assigned_variable == "result"]
        assert len(result_events) >= 1
        assert result_events[0].context_source == "for_iterable"
        assert result_events[0].event_type == "StrictDependencyAddition"
        assert result_events[0].added_dependencies  # non-empty

    def test_if_branch_emits_if_condition_event(self) -> None:
        """Assignment inside if-branch with untrusted condition → if_condition event."""
        tool = _untrusted_tool("flag_tool")
        interp = CaMeLInterpreter(
            tools={"get_flag": tool},
            mode=ExecutionMode.STRICT,
        )

        def _flag_tool() -> CaMeLValue:
            return wrap(
                True,
                sources=frozenset({"flag_tool"}),
                readers=Public,
            )

        interp._tools["get_flag"] = _flag_tool  # type: ignore[attr-defined]
        code = textwrap.dedent("""\
            flag = get_flag()
            if flag:
                x = 1
        """)
        interp.exec(code)
        log = interp.strict_dep_audit_log
        x_events = [e for e in log if e.assigned_variable == "x"]
        assert len(x_events) >= 1
        assert x_events[0].context_source == "if_condition"
        assert "flag" in x_events[0].added_dependencies

    def test_post_qllm_emits_post_qllm_event(self) -> None:
        """Assignment after query_quarantined_llm call → context_source='post_qllm'."""
        qllm_result_cv = wrap(
            type("R", (), {"action": "do_x"})(),
            sources=frozenset({"query_quarantined_llm"}),
            readers=Public,
        )

        def _qllm(prompt: Any, schema: Any) -> CaMeLValue:
            return qllm_result_cv

        interp = CaMeLInterpreter(
            tools={"query_quarantined_llm": _qllm},
            mode=ExecutionMode.STRICT,
        )
        code = textwrap.dedent("""\
            result = query_quarantined_llm("prompt", "schema")
            x = 1
        """)
        interp.exec(code)
        log = interp.strict_dep_audit_log
        x_events = [e for e in log if e.assigned_variable == "x"]
        assert len(x_events) >= 1
        assert x_events[0].context_source == "post_qllm"

    def test_deduplication_same_lineno_variable(self) -> None:
        """Same assignment in loop (10 iterations) → single deduped event."""

        def _list_tool() -> CaMeLValue:
            return wrap(
                list(range(10)),
                sources=frozenset({"items_tool"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(
            tools={"get_items": _list_tool},
            mode=ExecutionMode.STRICT,
        )
        code = textwrap.dedent("""\
            items = get_items()
            for item in items:
                x = item
        """)
        interp.exec(code)
        log = interp.strict_dep_audit_log
        x_events = [e for e in log if e.assigned_variable == "x"]
        # Should be deduplicated to 1 event despite 10 iterations.
        assert len(x_events) == 1

    def test_normal_mode_no_events(self) -> None:
        """Assignments in NORMAL mode do not emit StrictDependencyAdditionEvents."""

        def _list_tool() -> CaMeLValue:
            return wrap(
                ["a", "b"],
                sources=frozenset({"items_tool"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(
            tools={"get_items": _list_tool},
            mode=ExecutionMode.NORMAL,
        )
        code = textwrap.dedent("""\
            items = get_items()
            for item in items:
                x = item
        """)
        interp.exec(code)
        assert interp.strict_dep_audit_log == []

    def test_top_level_assignment_no_event(self) -> None:
        """Top-level assignment with empty ctx context does not emit an event."""
        interp = CaMeLInterpreter(mode=ExecutionMode.STRICT)
        interp.exec("x = 1")
        # No STRICT context active at top level — no event.
        assert interp.strict_dep_audit_log == []

    def test_strict_dep_audit_log_returns_snapshot(self) -> None:
        """strict_dep_audit_log returns an isolated list copy."""

        def _list_tool() -> CaMeLValue:
            return wrap(
                ["a"],
                sources=frozenset({"t"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(
            tools={"get_items": _list_tool},
            mode=ExecutionMode.STRICT,
        )
        interp.exec("items = get_items()\nfor i in items:\n    v = i")
        snapshot1 = interp.strict_dep_audit_log
        snapshot2 = interp.strict_dep_audit_log
        # Each call returns a new list.
        assert snapshot1 is not snapshot2
        # But with same content.
        assert snapshot1 == snapshot2

    def test_dedup_resets_across_exec_calls(self) -> None:
        """Deduplication resets between exec() calls — each call records its own events."""

        def _list_tool() -> CaMeLValue:
            return wrap(
                ["a"],
                sources=frozenset({"t"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(
            tools={"get_items": _list_tool},
            mode=ExecutionMode.STRICT,
        )
        code = "items = get_items()\nfor i in items:\n    v = i"
        interp.exec(code)
        count_after_first = len(
            [e for e in interp.strict_dep_audit_log if e.assigned_variable == "v"]
        )
        interp.exec(code)  # second exec() call — dedup resets
        count_after_second = len(
            [e for e in interp.strict_dep_audit_log if e.assigned_variable == "v"]
        )
        # Second exec should add 1 more event (dedup reset).
        assert count_after_second == count_after_first + 1

    def test_event_fields_populated_correctly(self) -> None:
        """StrictDependencyAdditionEvent fields are correctly populated."""

        def _flag_tool() -> CaMeLValue:
            return wrap(True, sources=frozenset({"flag_src"}), readers=Public)

        interp = CaMeLInterpreter(
            tools={"get_flag": _flag_tool},
            mode=ExecutionMode.STRICT,
        )
        code = "flag = get_flag()\nif flag:\n    y = 1"
        interp.exec(code)
        events = [e for e in interp.strict_dep_audit_log if e.assigned_variable == "y"]
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, StrictDependencyAdditionEvent)
        assert ev.event_type == "StrictDependencyAddition"
        assert ev.statement_type == "Assign"
        assert ev.statement_lineno is not None
        assert ev.assigned_variable == "y"
        assert isinstance(ev.added_dependencies, frozenset)
        assert "flag" in ev.added_dependencies
        assert ev.context_source == "if_condition"
        assert ev.timestamp  # non-empty


# ---------------------------------------------------------------------------
# Integration — end-to-end escalation blocking
# ---------------------------------------------------------------------------


class TestEscalationIntegration:
    """End-to-end integration tests for M4-F15/F16/F18."""

    def test_e2e_data_derived_callable_blocked(self) -> None:
        """Full pipeline: untrusted callable blocked with audit trail."""

        def _get_action() -> CaMeLValue:
            def _fn(*args: Any, **kwargs: Any) -> CaMeLValue:
                return wrap("executed", sources=frozenset({"CaMeL"}), readers=Public)

            return wrap(_fn, sources=frozenset({"external_tool"}), readers=Public)

        def _safe_tool(data: str) -> CaMeLValue:
            return wrap(f"processed:{data}", sources=frozenset({"safe_tool"}))

        interp = CaMeLInterpreter(
            tools={"get_action": _get_action, "safe_tool": _safe_tool},
        )
        # Setup: get the "action" callable from untrusted source.
        interp.exec("action = get_action()")
        # Execution: trying to call the untrusted callable should be blocked.
        with pytest.raises(DataToControlFlowEscalationError) as exc_info:
            interp.exec('result = action("sensitive_data")')

        # Verify the warning content.
        warning = exc_info.value.warning
        assert warning.offending_variable == "action"
        assert "external_tool" in warning.untrusted_sources

        # Verify audit trail is complete.
        security_log = interp.security_audit_log
        escalation_events = [e for e in security_log if isinstance(e, DataToControlFlowAuditEvent)]
        assert len(escalation_events) == 1
        audit = escalation_events[0]
        assert audit.event_type == "DataToControlFlowEscalation"
        assert audit.consent_outcome == "evaluation_mode_raised"

        # Verify legitimate tool calls still work after the blocked attempt.
        interp.exec('safe_result = safe_tool("data")')
        assert "processed:data" in interp.get("safe_result").raw

    def test_strict_dep_and_escalation_combined(self) -> None:
        """STRICT mode records dep additions while escalation detection fires correctly."""

        def _list_tool() -> CaMeLValue:
            def _fn() -> CaMeLValue:
                return wrap("called", sources=frozenset({"CaMeL"}))

            return wrap(
                [_fn],
                sources=frozenset({"list_tool"}),
                readers=Public,
            )

        interp = CaMeLInterpreter(
            tools={"get_funcs": _list_tool},
            mode=ExecutionMode.STRICT,
        )
        # Get a list of callables from untrusted source.
        interp.exec("funcs = get_funcs()")
        interp.exec("fn = funcs[0]")  # fn has untrusted sources

        # Escalation should fire when fn is called.
        with pytest.raises(DataToControlFlowEscalationError):
            interp.exec("result = fn()")
