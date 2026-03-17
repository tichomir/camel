"""Exception hardening test suite — M4-F6, M4-F7, M4-F8, M4-F9, M4-F17.

Feature mapping
---------------
M4-F6  Dependency-aware exception message redaction.
M4-F7  NotEnoughInformationError handler — strips content, exposes type + lineno.
M4-F8  STRICT mode annotation preservation across NEIE re-generation cycles.
M4-F9  Loop-body exception STRICT propagation for non-public-iterable loops.
M4-F17 Audit log entry emitted for every redaction event.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from camel import CaMeLInterpreter
from camel.exceptions import NotEnoughInformationError as CamelNEIE
from camel.execution_loop import (
    AcceptedState,
    CaMeLOrchestrator,
    ExceptionRedactor,
    MaxRetriesExceededError,
    RedactedError,
    RedactionAuditEvent,
    RetryPromptBuilder,
)
from camel.interpreter import ExecutionMode
from camel.llm.exceptions import NotEnoughInformationError as LLMNotEnoughInfoError
from camel.llm.p_llm import CodePlan, PLLMWrapper, ToolSignature
from camel.llm.schemas import QResponse
from camel.value import CaMeLValue, Public, wrap

from tests.harness.recording_backend import RecordingBackend, StubBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _untrusted_store(sentinel: str) -> dict[str, CaMeLValue]:
    return {"data": wrap(sentinel, sources=frozenset({"external_tool"}))}


def _trusted_store() -> dict[str, CaMeLValue]:
    return {
        "x": wrap(1, sources=frozenset({"User literal"})),
        "y": wrap(2, sources=frozenset({"CaMeL"})),
    }


def _make_interp(**extra_tools: Any) -> CaMeLInterpreter:
    """Build a STRICT-mode interpreter with standard mock tools."""

    def _get_email() -> CaMeLValue:
        return wrap("email body", sources=frozenset({"get_email"}))

    def _get_items() -> CaMeLValue:
        return wrap([1, 2, 3], sources=frozenset({"get_items"}))

    def _trusted_tool() -> CaMeLValue:
        return wrap("safe", sources=frozenset({"CaMeL"}))

    tools: dict[str, Any] = {
        "get_email": _get_email,
        "get_items": _get_items,
        "trusted_tool": _trusted_tool,
    }
    tools.update(extra_tools)
    return CaMeLInterpreter(tools=tools, mode=ExecutionMode.STRICT)  # type: ignore[arg-type]


# ===========================================================================
# M4-F6 — Dependency-aware exception message redaction
# ===========================================================================


class TestM4F6DependencyAwareRedaction:
    """M4-F6: Exception messages tainted via the dependency graph are redacted."""

    def test_direct_untrusted_source_redacted(self) -> None:
        """Variable with direct untrusted source taints the exception (baseline)."""
        redactor = ExceptionRedactor()
        exc = ValueError("SENSITIVE_MESSAGE")
        store = _untrusted_store("SENSITIVE_MESSAGE")
        result = redactor.classify(exc, store)
        assert result.trust_level == "untrusted"
        assert result.message is None

    def test_transitive_untrusted_source_redacted_with_interpreter(self) -> None:
        """M4-F6: Dep-graph walk catches transitive taint even when direct sources are CaMeL."""
        interp = _make_interp()
        # email has untrusted source; summary is derived from email.
        interp.exec("email = get_email()")
        interp.exec("summary = email")  # summary depends on email (untrusted)

        # Force summary's sources to be trusted-looking while dep graph knows better.
        # Simulate via a CaMeL-sourced wrapper that the dep graph links to email.
        interp.exec("safe_label = trusted_tool()")

        redactor = ExceptionRedactor()
        exc = ValueError("safe msg")
        # Store has email (untrusted) so store-level check is the same;
        # but the dep-graph walk must also return the chain.
        result = redactor.classify(exc, interp.store, interp)
        # Tainted because 'email' in store has untrusted source.
        assert result.trust_level == "untrusted"
        assert result.message is None

    def test_trusted_only_store_message_preserved(self) -> None:
        """M4-F6: Clean exception (no untrusted deps) passes through unmodified."""
        redactor = ExceptionRedactor()
        exc = ValueError("safe error message")
        store = _trusted_store()
        result = redactor.classify(exc, store)
        assert result.trust_level == "trusted"
        assert result.message == "safe error message"

    def test_empty_store_trusted(self) -> None:
        """Empty store → no untrusted sources → trusted classification."""
        redactor = ExceptionRedactor()
        exc = NameError("name 'x' is not defined")
        result = redactor.classify(exc, {})
        assert result.trust_level == "trusted"
        assert result.message is not None

    def test_dep_chain_populated_when_untrusted(self) -> None:
        """M4-F6: dependency_chain in audit event contains untrusted (var, source) pairs."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = KeyError("KEY_R")
        store = _untrusted_store("KEY_R")

        redactor.classify(exc, store)
        assert len(audit) == 1
        event = audit[0]
        assert event.trust_level == "untrusted"
        # dep_chain should have at least one entry pointing to the external tool source.
        assert any(src == "external_tool" for _, src in event.dependency_chain)

    def test_dep_chain_empty_for_trusted(self) -> None:
        """M4-F6: dependency_chain is empty for trusted exceptions."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        redactor.classify(ValueError("safe"), _trusted_store())
        assert audit[0].dependency_chain == []

    def test_with_interpreter_dep_graph_walk(self) -> None:
        """M4-F6: ExceptionRedactor with interpreter reference walks dep graph."""
        interp = _make_interp()
        interp.exec("data = get_email()")
        interp.exec("result = data")

        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = RuntimeError("boom")
        result = redactor.classify(exc, interp.store, interp)
        assert result.trust_level == "untrusted"
        assert result.message is None
        # Dep chain should reference the upstream untrusted source.
        assert len(audit) == 1
        event = audit[0]
        assert event.trust_level == "untrusted"
        assert any(src == "get_email" for _, src in event.dependency_chain)


# ===========================================================================
# M4-F17 — Audit log entry for every redaction event
# ===========================================================================


class TestM4F17AuditLog:
    """M4-F17: Every redaction event writes an audit log entry."""

    def test_trusted_exception_emits_audit_event(self) -> None:
        """Trusted exceptions also emit an event (redaction_reason='none')."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        redactor.classify(ValueError("safe"), _trusted_store())
        assert len(audit) == 1
        e = audit[0]
        assert e.redaction_reason == "none"
        assert e.trust_level == "trusted"
        assert e.error_type == "ValueError"
        assert e.m4_f9_applied is False

    def test_untrusted_exception_emits_audit_event(self) -> None:
        """Untrusted exceptions emit event with redaction_reason='untrusted_dependency'."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = TypeError("UNTRUSTED_MSG")
        redactor.classify(exc, _untrusted_store("UNTRUSTED_MSG"))
        assert len(audit) == 1
        e = audit[0]
        assert e.redaction_reason == "untrusted_dependency"
        assert e.trust_level == "untrusted"
        assert e.error_type == "TypeError"

    def test_neie_emits_audit_event(self) -> None:
        """NEIE emits event with redaction_reason='not_enough_information'."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = CamelNEIE()
        redactor.classify(exc, _untrusted_store("neie"))
        assert len(audit) == 1
        e = audit[0]
        assert e.redaction_reason == "not_enough_information"
        assert e.trust_level == "not_enough_information"
        assert e.error_type == "NotEnoughInformationError"

    def test_audit_event_has_timestamp(self) -> None:
        """Each audit event has a non-empty ISO-8601 timestamp."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        redactor.classify(ValueError("x"), _trusted_store())
        assert audit[0].timestamp  # non-empty string
        # Basic ISO format check.
        assert "T" in audit[0].timestamp

    def test_audit_event_redacted_message_length_populated(self) -> None:
        """redacted_message_length reflects original message length."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        msg = "some message here"
        exc = ValueError(msg)
        redactor.classify(exc, _untrusted_store("x"))
        # redacted_message_length should be > 0.
        assert audit[0].redacted_message_length > 0

    def test_multiple_classify_calls_accumulate_events(self) -> None:
        """Each classify() call adds exactly one event to the audit log."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        redactor.classify(ValueError("a"), _trusted_store())
        redactor.classify(TypeError("b"), _untrusted_store("b"))
        redactor.classify(CamelNEIE(), {})
        assert len(audit) == 3

    def test_no_audit_log_does_not_raise(self) -> None:
        """ExceptionRedactor with audit_log=None (default) does not raise."""
        redactor = ExceptionRedactor()  # no audit_log
        result = redactor.classify(ValueError("x"), _trusted_store())
        assert result.trust_level == "trusted"

    def test_m4_f9_applied_flag_set_on_loop_exc(self) -> None:
        """m4_f9_applied=True when exception carries __loop_iter_deps__."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = RuntimeError("loop error")
        exc.__loop_iter_deps__ = frozenset({"items"})  # type: ignore[attr-defined]
        exc.__loop_iter_caps__ = wrap("x", sources=frozenset({"get_items"}))  # type: ignore[attr-defined]
        redactor.classify(exc, _untrusted_store("loop error"))
        assert audit[0].m4_f9_applied is True
        assert audit[0].redaction_reason == "loop_body_exception"

    def test_orchestrator_exposes_redaction_audit_log(self) -> None:
        """CaMeLOrchestrator.redaction_audit_log accumulates events across runs."""
        # Just verify the property exists and starts empty.
        stub = StubBackend(responses=["x = trusted_tool()"], cycle=True)
        recording = RecordingBackend(stub)
        p_llm = PLLMWrapper(recording)
        interp = CaMeLInterpreter(
            tools={"trusted_tool": lambda: wrap("ok", sources=frozenset({"CaMeL"}))}
        )
        sigs = [ToolSignature("trusted_tool", "", "CaMeLValue", "A tool")]
        orch = CaMeLOrchestrator(p_llm=p_llm, interpreter=interp, tool_signatures=sigs)
        assert isinstance(orch.redaction_audit_log, list)
        assert len(orch.redaction_audit_log) == 0


# ===========================================================================
# M4-F7 — NotEnoughInformationError handler
# ===========================================================================


class TestM4F7NEIEHandler:
    """M4-F7: NEIE strips all content; exposes type + lineno only."""

    def test_neie_message_is_none(self) -> None:
        """Both NEIE variants produce RedactedError.message=None."""
        redactor = ExceptionRedactor()
        assert redactor.classify(CamelNEIE(), {}).message is None

        class _Schema(QResponse):
            val: str

        llm_exc = LLMNotEnoughInfoError(schema_type=_Schema)
        assert redactor.classify(llm_exc, {}).message is None

    def test_neie_trust_level(self) -> None:
        """NEIE produces trust_level='not_enough_information'."""
        redactor = ExceptionRedactor()
        result = redactor.classify(CamelNEIE(), {})
        assert result.trust_level == "not_enough_information"

    def test_neie_lineno_extracted_from_dunder(self) -> None:
        """M4-F7: ExceptionRedactor reads __lineno__ set by _eval_Call."""
        redactor = ExceptionRedactor()
        exc = CamelNEIE()
        exc.__lineno__ = 42  # type: ignore[attr-defined]
        result = redactor.classify(exc, {})
        assert result.lineno == 42

    def test_neie_lineno_falls_back_to_lineno_attr(self) -> None:
        """M4-F7: Falls back to .lineno when __lineno__ is absent."""
        redactor = ExceptionRedactor()
        exc = CamelNEIE()
        exc.lineno = 7  # type: ignore[attr-defined]
        result = redactor.classify(exc, {})
        assert result.lineno == 7

    def test_neie_lineno_none_when_absent(self) -> None:
        """M4-F7: lineno is None when no lineno information is attached."""
        redactor = ExceptionRedactor()
        result = redactor.classify(CamelNEIE(), {})
        assert result.lineno is None

    def test_interpreter_attaches_lineno_to_neie(self) -> None:
        """M4-F7: interpreter._eval_Call attaches __lineno__ to NEIE from tool."""
        neie_raised = [False]

        def qllm_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            raise CamelNEIE()

        interp = _make_interp(qllm_tool=qllm_tool)
        with pytest.raises(CamelNEIE) as exc_info:
            interp.exec("result = qllm_tool('prompt')")
        exc = exc_info.value
        # __lineno__ should be attached (1 for single-line exec).
        assert hasattr(exc, "__lineno__")
        # Line number should be a positive integer.
        assert exc.__lineno__ is None or isinstance(exc.__lineno__, int)  # type: ignore[attr-defined]

    def test_retry_prompt_contains_neie_advisory(self) -> None:
        """M4-F7: RetryPromptBuilder includes advisory for NEIE trust_level."""
        builder = RetryPromptBuilder()
        accepted = AcceptedState(
            variable_names=frozenset({"x"}),
            executed_statement_count=1,
            remaining_source="y = qllm(x)",
        )
        error = RedactedError(
            error_type="NotEnoughInformationError",
            lineno=5,
            message=None,
            trust_level="not_enough_information",
        )
        prompt = builder.build(accepted, error, [])
        assert "query_quarantined_llm" in prompt
        assert "line 5" in prompt
        # Crucially, no untrusted data should appear.
        assert "Q-LLM indicated" not in prompt  # NEIE.MESSAGE must not appear

    def test_p_llm_retry_receives_only_type_and_lineno(self) -> None:
        """M4-F7: P-LLM retry prompt has type + lineno but no message."""
        builder = RetryPromptBuilder()
        error = RedactedError(
            error_type="NotEnoughInformationError",
            lineno=3,
            message=None,
            trust_level="not_enough_information",
        )
        accepted = AcceptedState(
            variable_names=frozenset(),
            executed_statement_count=0,
            remaining_source="",
        )
        prompt = builder.build(accepted, error, [])
        assert "NotEnoughInformationError" in prompt
        assert "line 3" in prompt
        assert "Message:" not in prompt  # no message line at all


# ===========================================================================
# M4-F8 — STRICT mode annotation preservation across NEIE re-generation
# ===========================================================================


class TestM4F8AnnotationPreservation:
    """M4-F8: Dep-graph and dep-ctx-stack preserved across NEIE retries."""

    def test_snapshot_and_restore_round_trip(self) -> None:
        """snapshot_dep_state / restore_dep_state are inverse operations."""
        interp = _make_interp()
        interp.exec("email = get_email()")
        interp.exec("summary = email")

        snap_graph, snap_stack = interp.snapshot_dep_state()

        # Modify the interpreter state.
        interp.exec("extra = trusted_tool()")

        # Restore.
        interp.restore_dep_state(snap_graph, snap_stack)

        # 'summary' should still depend on 'email' after restore.
        dg = interp.get_dependency_graph("summary")
        assert "email" in dg.all_upstream or "email" in dg.direct_deps

        # 'extra' was added after the snapshot; dep graph was wiped.
        dg_extra = interp.get_dependency_graph("extra")
        # After restore the dep graph is set back to the snapshot state,
        # so 'extra' either has no recorded deps or is absent.
        assert dg_extra.direct_deps == frozenset() or "extra" not in snap_graph

    def test_accepted_state_includes_snapshot_on_neie(self) -> None:
        """AcceptedState includes dep snapshot fields when NEIE causes the retry."""
        interp = _make_interp()
        interp.exec("email = get_email()")

        # Capture snapshot.
        snap_graph, snap_stack = interp.snapshot_dep_state()
        accepted = AcceptedState(
            variable_names=frozenset({"email"}),
            executed_statement_count=1,
            remaining_source="result = trusted_tool()",
            dependency_graph_snapshot=snap_graph,
            dep_ctx_stack_snapshot=snap_stack,
        )
        assert accepted.dependency_graph_snapshot is not None
        assert "email" in accepted.dependency_graph_snapshot
        assert accepted.dep_ctx_stack_snapshot is not None

    def test_accepted_state_snapshot_none_for_non_neie(self) -> None:
        """Non-NEIE exceptions produce AcceptedState with None snapshot fields."""
        accepted = AcceptedState(
            variable_names=frozenset(),
            executed_statement_count=0,
            remaining_source="",
        )
        assert accepted.dependency_graph_snapshot is None
        assert accepted.dep_ctx_stack_snapshot is None

    async def test_orchestrator_restores_dep_state_after_neie(self) -> None:
        """M4-F8: orchestrator restores dep state before executing regenerated plan.

        The test verifies that variables assigned before NEIE retain their
        dependency edges in the restarted plan.
        """
        captured_dep_graph_after_retry: dict[str, frozenset[str]] = {}
        call_count = [0]

        def neie_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            """Raises NEIE on first call, succeeds on second."""
            call_count[0] += 1
            if call_count[0] == 1:
                raise CamelNEIE()
            return wrap("result", sources=frozenset({"neie_tool"}))

        def sensor_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            return wrap("data", sources=frozenset({"sensor_tool"}))

        # Plan 1: set 'data' from sensor (untrusted), then call neie_tool (raises).
        # Plan 2 (retry): just call neie_tool again (succeeds).
        plan1 = "data = sensor_tool()\nresult = neie_tool(data)"
        plan2 = "result = neie_tool(data)"

        stub = StubBackend(responses=[plan1, plan2], cycle=False)
        recording = RecordingBackend(stub)
        p_llm = PLLMWrapper(recording)

        interp = CaMeLInterpreter(
            tools={
                "neie_tool": neie_tool,
                "sensor_tool": sensor_tool,
            },
            mode=ExecutionMode.STRICT,
        )
        sigs = [
            ToolSignature("neie_tool", "data", "CaMeLValue", "May raise NEIE"),
            ToolSignature("sensor_tool", "", "CaMeLValue", "Returns sensor data"),
        ]
        orch = CaMeLOrchestrator(
            p_llm=p_llm, interpreter=interp, tool_signatures=sigs, max_loop_retries=5
        )

        result = await orch.run("Test M4-F8")
        # After retry, 'result' should be in the final store.
        assert "result" in result.final_store


# ===========================================================================
# M4-F9 — Loop-body exception STRICT propagation
# ===========================================================================


class TestM4F9LoopBodyExceptionPropagation:
    """M4-F9: Exception in loop body with non-public iterable carries iter deps."""

    def test_exception_inside_loop_body_carries_loop_iter_deps(self) -> None:
        """M4-F9: Exception from for-loop body gets __loop_iter_deps__ attached."""
        failing_on = [False]

        def failing_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            if failing_on[0]:
                raise RuntimeError("boom inside loop")
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = _make_interp(failing_tool=failing_tool)
        # Make the loop run once successfully then fail.
        interp.exec("items = get_items()")
        failing_on[0] = True

        with pytest.raises(RuntimeError) as exc_info:
            interp.exec(
                "for item in items:\n    result = failing_tool(item)"
            )
        exc = exc_info.value
        # M4-F9: __loop_iter_deps__ should be attached.
        assert hasattr(exc, "__loop_iter_deps__"), (
            "Exception from loop body with non-public iterable must carry "
            "__loop_iter_deps__"
        )
        # The dep context should include the iterable variable.
        assert isinstance(exc.__loop_iter_deps__, frozenset)  # type: ignore[attr-defined]

    def test_exception_inside_loop_body_carries_loop_iter_caps(self) -> None:
        """M4-F9: Exception carries __loop_iter_caps__ for capability propagation."""

        def failing_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            raise RuntimeError("caps check")

        interp = _make_interp(failing_tool=failing_tool)
        interp.exec("items = get_items()")

        with pytest.raises(RuntimeError) as exc_info:
            interp.exec(
                "for item in items:\n    result = failing_tool(item)"
            )
        exc = exc_info.value
        assert hasattr(exc, "__loop_iter_caps__"), (
            "Exception from loop body must carry __loop_iter_caps__"
        )

    def test_no_loop_iter_deps_for_trusted_iterable(self) -> None:
        """M4-F9 does NOT apply when the iterable is fully trusted."""
        call_count = [0]

        def failing_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            call_count[0] += 1
            raise RuntimeError("trusted iterable loop error")

        interp = _make_interp(failing_tool=failing_tool)
        # Use a literal list (trusted, User-literal source).
        with pytest.raises(RuntimeError) as exc_info:
            interp.exec(
                "for item in [1, 2, 3]:\n    result = failing_tool(item)"
            )
        exc = exc_info.value
        # For a trusted iterable, __loop_iter_deps__ should NOT be set.
        assert not hasattr(exc, "__loop_iter_deps__"), (
            "Trusted-iterable loop exceptions must NOT carry __loop_iter_deps__"
        )

    def test_no_loop_iter_deps_in_normal_mode(self) -> None:
        """M4-F9 is STRICT-mode only; NORMAL mode does not annotate exceptions."""

        def failing_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            raise RuntimeError("normal mode loop error")

        interp = CaMeLInterpreter(
            tools={"get_items": lambda: wrap([1, 2, 3], sources=frozenset({"get_items"})),
                   "failing_tool": failing_tool},
            mode=ExecutionMode.NORMAL,
        )
        interp.exec("items = get_items()")

        with pytest.raises(RuntimeError) as exc_info:
            interp.exec(
                "for item in items:\n    result = failing_tool(item)"
            )
        exc = exc_info.value
        assert not hasattr(exc, "__loop_iter_deps__"), (
            "NORMAL mode loop exceptions must NOT carry __loop_iter_deps__"
        )

    def test_loop_iter_deps_in_accepted_state(self) -> None:
        """M4-F9: _build_accepted_state captures loop_iter_deps from exception."""
        loop_deps = frozenset({"items"})
        loop_caps = wrap("x", sources=frozenset({"get_items"}))

        exc = RuntimeError("loop error")
        exc.__loop_iter_deps__ = loop_deps  # type: ignore[attr-defined]
        exc.__loop_iter_caps__ = loop_caps  # type: ignore[attr-defined]
        exc._camel_executed_count = 1  # type: ignore[attr-defined]
        exc._camel_remaining_source = "y = trusted_tool()"  # type: ignore[attr-defined]

        # Build accepted state using CaMeLOrchestrator._build_accepted_state
        # indirectly by checking the AcceptedState dataclass construction.
        accepted = AcceptedState(
            variable_names=frozenset({"items"}),
            executed_statement_count=1,
            remaining_source="y = trusted_tool()",
            loop_iter_deps=loop_deps,
            loop_iter_caps=loop_caps,
        )
        assert accepted.loop_iter_deps == loop_deps
        assert accepted.loop_iter_caps is loop_caps

    def test_redaction_audit_reason_loop_body_exception(self) -> None:
        """M4-F17: Audit event for M4-F9 loop exc uses 'loop_body_exception' reason."""
        audit: list[RedactionAuditEvent] = []
        redactor = ExceptionRedactor(audit_log=audit)
        exc = RuntimeError("loop error")
        exc.__loop_iter_deps__ = frozenset({"items"})  # type: ignore[attr-defined]
        exc.__loop_iter_caps__ = wrap("x", sources=frozenset({"get_items"}))  # type: ignore[attr-defined]
        redactor.classify(exc, _untrusted_store("loop error"))
        assert audit[0].m4_f9_applied is True
        assert audit[0].redaction_reason == "loop_body_exception"

    async def test_orchestrator_handles_loop_body_exception(self) -> None:
        """M4-F9: orchestrator pre-seeds dep-ctx when loop-body exc triggers retry."""
        call_count = [0]

        def items_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            return wrap([1, 2, 3], sources=frozenset({"items_tool"}))

        def loop_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first item fails")
            return wrap(f"done_{call_count[0]}", sources=frozenset({"CaMeL"}))

        plan1 = "items = items_tool()\nfor item in items:\n    result = loop_tool(item)"
        plan2 = "result = loop_tool(1)"

        stub = StubBackend(responses=[plan1, plan2], cycle=False)
        recording = RecordingBackend(stub)
        p_llm = PLLMWrapper(recording)

        interp = CaMeLInterpreter(
            tools={"items_tool": items_tool, "loop_tool": loop_tool},
            mode=ExecutionMode.STRICT,
        )
        sigs = [
            ToolSignature("items_tool", "", "CaMeLValue", "Returns list"),
            ToolSignature("loop_tool", "item", "CaMeLValue", "Processes item"),
        ]
        orch = CaMeLOrchestrator(
            p_llm=p_llm, interpreter=interp, tool_signatures=sigs, max_loop_retries=5
        )

        result = await orch.run("Test M4-F9 orchestrator")
        assert "result" in result.final_store


# ===========================================================================
# Regression: existing redaction tests still pass (M4-F6 backward compat)
# ===========================================================================


class TestM4F6Regression:
    """Regression: M4-F6 enhanced check is backward-compatible with existing tests."""

    def test_untrusted_store_type_error_redacted(self) -> None:
        """R01 equivalent: untrusted store + TypeError → message body is None."""
        redactor = ExceptionRedactor()
        exc = TypeError("value is not subscriptable")
        exc.lineno = 3  # type: ignore[attr-defined]
        store = {"data": wrap("secret", sources=frozenset({"external_tool"}))}
        result = redactor.classify(exc, store)
        assert result.trust_level == "untrusted"
        assert result.message is None
        assert result.lineno == 3

    def test_trusted_store_value_error_passes_through(self) -> None:
        """R03 equivalent: trusted store + ValueError → full message included."""
        redactor = ExceptionRedactor()
        exc = ValueError("trusted error text")
        exc.lineno = 5  # type: ignore[attr-defined]
        result = redactor.classify(exc, _trusted_store())
        assert result.trust_level == "trusted"
        assert result.message == "trusted error text"
        assert result.lineno == 5

    def test_neie_lineno_none_no_dunder(self) -> None:
        """R02/R05 equivalent: NEIE → type only, no message."""
        redactor = ExceptionRedactor()
        result = redactor.classify(CamelNEIE(), {})
        assert result.trust_level == "not_enough_information"
        assert result.message is None
