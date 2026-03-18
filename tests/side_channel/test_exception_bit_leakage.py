"""Side-channel test class 2: Exception-based bit leakage.

Attack vector
-------------
An exception raised during execution may carry the untrusted data that
triggered it — either directly in the exception message, or indirectly
through the type of the exception.  If the exception text is forwarded
to the P-LLM (which regenerates the plan), the attacker's data reaches
the trusted side of the boundary.

CaMeL mitigations
-----------------
M4-F6  Dependency-graph-aware taint check: when the failing statement
       depends (transitively) on an untrusted source, the exception message
       is replaced with ``None`` (represented as ``RedactedError.message=None``).
       Only ``error_type`` and ``lineno`` are forwarded to the P-LLM.

M4-F9  Loop-body exception STRICT propagation: when an exception originates
       inside a for-loop body whose iterable has non-public provenance, the
       iterable's dependency set and capability context are attached to the
       exception and pre-seeded into the regenerated plan's dependency context,
       ensuring the iterable's taint is never silently dropped.

PRD Section 11 target: 100% pass rate for implemented mitigations.

Test structure
--------------
Each test documents the attack vector, the expected redaction outcome, and
where applicable a NORMAL-mode contrast showing unmitigated behaviour.
"""

from __future__ import annotations

from typing import Any

from camel.exceptions import NotEnoughInformationError as CamelNEIE
from camel.execution_loop import ExceptionRedactor, RedactionAuditEvent
from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.value import CaMeLValue, wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNTRUSTED_SOURCES = frozenset({"external_tool"})
_TRUSTED_SOURCES = frozenset({"User literal"})
_CAMEL_SOURCES = frozenset({"CaMeL"})


def _make_interp(**tools: Any) -> CaMeLInterpreter:
    """Build a STRICT-mode interpreter with standard mock tools."""

    def _get_untrusted() -> CaMeLValue:
        return wrap("SECRET_DATA_123", sources=_UNTRUSTED_SOURCES)

    def _get_trusted() -> CaMeLValue:
        return wrap("safe_value", sources=_TRUSTED_SOURCES)

    def _get_untrusted_list() -> CaMeLValue:
        return wrap([1, 2, 3], sources=_UNTRUSTED_SOURCES)

    registered: dict[str, Any] = {
        "get_untrusted": _get_untrusted,
        "get_trusted": _get_trusted,
        "get_untrusted_list": _get_untrusted_list,
    }
    registered.update(tools)
    return CaMeLInterpreter(tools=registered, mode=ExecutionMode.STRICT)  # type: ignore[arg-type]


def _redactor(audit_log: list[RedactionAuditEvent] | None = None) -> ExceptionRedactor:
    return ExceptionRedactor(audit_log=audit_log)


# ---------------------------------------------------------------------------
# Scenario 1 — Exception triggered by untrusted data is redacted (M4-F6)
#
# Attack: adversary embeds a value that causes a TypeError/ValueError when
#   used in the plan; the error message contains the raw adversary data.
# Mitigation: ExceptionRedactor checks dependency graph; message=None when
#   any upstream source is outside the trusted set.
# ---------------------------------------------------------------------------


class TestExceptionMessageRedactionUntrusted:
    """Verify that exceptions with untrusted-data dependencies are redacted (M4-F6)."""

    def test_untrusted_variable_exception_message_is_redacted(self) -> None:
        """M4-F6: exception triggered by untrusted variable has message=None.

        PRD §11 side-channel target: exception-based bit leakage closed.
        """
        interp = _make_interp()
        interp.exec("untrusted = get_untrusted()")

        store_snapshot: dict[str, CaMeLValue] = dict(interp.store)
        exc = ValueError("SECRET_DATA_123 triggered this error")
        exc.__lineno__ = 2  # type: ignore[attr-defined]

        redactor = _redactor()
        result = redactor.classify(exc, store_snapshot, interpreter=interp)

        assert result.message is None, (
            "M4-F6: exception message must be None when untrusted data "
            "is present in the interpreter store"
        )
        assert result.trust_level == "untrusted", (
            "M4-F6: trust_level must be 'untrusted' for redacted exceptions"
        )
        assert result.error_type == "ValueError", (
            "M4-F6: error_type must still be present after redaction"
        )

    def test_untrusted_exception_message_does_not_leak_data(self) -> None:
        """M4-F6: the redacted result carries no untrusted data content.

        Even if the exception message contains the exact adversary payload,
        the redacted form must not expose it.
        """
        interp = _make_interp()
        interp.exec("secret = get_untrusted()")

        store_snapshot = dict(interp.store)
        payload = "INJECTED_PAYLOAD_XYZ"
        exc = RuntimeError(f"failed because of {payload}")
        exc.__lineno__ = 1  # type: ignore[attr-defined]

        redactor = _redactor()
        result = redactor.classify(exc, store_snapshot, interpreter=interp)

        assert result.message is None, "M4-F6: adversary payload must not appear in redacted error"
        assert payload not in str(result), (
            "M4-F6: payload must not appear anywhere in str(RedactedError)"
        )

    def test_trusted_exception_message_is_preserved(self) -> None:
        """M4-F6 negative: exception with only trusted sources preserves message.

        When all store variables are trusted, the message is forwarded intact.
        """
        trusted_store: dict[str, CaMeLValue] = {
            "x": wrap(1, sources=_TRUSTED_SOURCES),
            "y": wrap(2, sources=_CAMEL_SOURCES),
        }
        exc = TypeError("expected int, got str")
        exc.__lineno__ = 5  # type: ignore[attr-defined]

        redactor = _redactor()
        result = redactor.classify(exc, trusted_store)

        assert result.message is not None, (
            "M4-F6 negative: trusted exception must keep its message"
        )
        assert result.trust_level == "trusted", (
            "M4-F6 negative: trust_level must be 'trusted'"
        )
        assert "int" in (result.message or ""), (
            "M4-F6 negative: message content must be preserved for trusted exceptions"
        )

    def test_redaction_audit_event_emitted(self) -> None:
        """M4-F17: a RedactionAuditEvent is emitted for every classified exception."""
        interp = _make_interp()
        interp.exec("untrusted = get_untrusted()")

        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        exc = ValueError("bad")
        exc.__lineno__ = 3  # type: ignore[attr-defined]
        redactor.classify(exc, dict(interp.store), interpreter=interp)

        assert len(audit_log) == 1, "M4-F17: exactly one audit event must be emitted"
        event = audit_log[0]
        assert event.error_type == "ValueError"
        assert event.trust_level in ("untrusted", "trusted", "not_enough_information")

    def test_redaction_audit_event_for_trusted_exception(self) -> None:
        """M4-F17: audit event is also emitted for trusted exceptions (no redaction)."""
        trusted_store: dict[str, CaMeLValue] = {
            "x": wrap(42, sources=_TRUSTED_SOURCES),
        }
        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        exc = TypeError("type mismatch")
        redactor.classify(exc, trusted_store)

        assert len(audit_log) == 1, "M4-F17: audit event must be emitted for trusted exceptions"
        event = audit_log[0]
        assert event.trust_level == "trusted"
        assert event.redaction_reason == "none"

    def test_redacted_message_length_recorded_in_audit(self) -> None:
        """M4-F17: audit event records the length of the redacted message."""
        interp = _make_interp()
        interp.exec("untrusted = get_untrusted()")

        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        message = "error message with private data"
        exc = RuntimeError(message)
        exc.__lineno__ = 1  # type: ignore[attr-defined]
        redactor.classify(exc, dict(interp.store), interpreter=interp)

        event = audit_log[0]
        assert event.redacted_message_length > 0, (
            "M4-F17: redacted_message_length must record non-zero original length"
        )


# ---------------------------------------------------------------------------
# Scenario 2 — NotEnoughInformationError strips all content (M4-F7)
#
# Attack: a NEIE could carry missing-data content that the adversary
#   controlled by injecting text that causes the Q-LLM to report insufficiency.
# Mitigation: M4-F7 strips all NEIE content; only error_type + lineno forwarded.
# ---------------------------------------------------------------------------


class TestNEIEContentStripping:
    """Verify that NotEnoughInformationError content is fully stripped (M4-F7)."""

    def test_neie_message_is_none(self) -> None:
        """M4-F7: NEIE message must be None after classification."""
        store: dict[str, CaMeLValue] = {}
        redactor = _redactor()
        exc = CamelNEIE()

        result = redactor.classify(exc, store)

        assert result.message is None, (
            "M4-F7: NEIE message must be stripped — no missing-data content "
            "must reach the P-LLM"
        )

    def test_neie_trust_level_is_not_enough_information(self) -> None:
        """M4-F7: NEIE must produce trust_level='not_enough_information'."""
        redactor = _redactor()
        exc = CamelNEIE()
        result = redactor.classify(exc, {})
        assert result.trust_level == "not_enough_information", (
            "M4-F7: NEIE trust_level must be 'not_enough_information'"
        )

    def test_neie_error_type_is_preserved(self) -> None:
        """M4-F7: NEIE must still carry error_type for P-LLM retry signalling."""
        redactor = _redactor()
        exc = CamelNEIE()
        result = redactor.classify(exc, {})
        assert result.error_type == "NotEnoughInformationError", (
            "M4-F7: NEIE error_type must be preserved for P-LLM signalling"
        )

    def test_neie_audit_event_emitted(self) -> None:
        """M4-F17: NEIE classification must emit a RedactionAuditEvent."""
        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        exc = CamelNEIE()
        redactor.classify(exc, {})

        assert len(audit_log) == 1, "M4-F17: NEIE must emit an audit event"
        event = audit_log[0]
        assert event.redaction_reason == "not_enough_information"


# ---------------------------------------------------------------------------
# Scenario 3 — Loop-body exception carries iterable taint annotation (M4-F9)
#
# Attack: an adversary who controls the loop iterable can trigger exceptions
#   inside the loop body; without M4-F9, the iterable's taint would be
#   silently dropped across the retry cycle, allowing a re-generated plan
#   to treat the iterable as if it had no untrusted provenance.
# Mitigation: M4-F9 attaches the iterable's dep set to the exception via
#   __loop_iter_deps__, which ExceptionRedactor detects.
# ---------------------------------------------------------------------------


class TestLoopBodyExceptionTaint:
    """Verify that loop-body exceptions carry iterable taint annotation (M4-F9)."""

    def test_exception_in_loop_body_is_redacted(self) -> None:
        """M4-F6/M4-F9: exception inside a loop with untrusted iterable is redacted.

        The loop body variable inherits the iterable's untrusted sources
        (M4-F1), so any exception triggered from it must be redacted (M4-F6).
        """
        interp = _make_interp()
        interp.exec("items = get_untrusted_list()")

        # Manually simulate a loop-body exception with __loop_iter_deps__
        exc = TypeError("loop body error from untrusted data")
        exc.__lineno__ = 5  # type: ignore[attr-defined]
        # Simulate M4-F9: attach loop iter deps annotation
        exc.__loop_iter_deps__ = {"items": _UNTRUSTED_SOURCES}  # type: ignore[attr-defined]

        store_snapshot = dict(interp.store)
        redactor = _redactor()
        result = redactor.classify(exc, store_snapshot, interpreter=interp)

        # With untrusted data in the store, the message must be redacted.
        assert result.message is None, (
            "M4-F6/M4-F9: exception from loop body with untrusted iterable "
            "must be redacted"
        )

    def test_m4_f9_applied_flag_in_audit_event(self) -> None:
        """M4-F17: audit event m4_f9_applied flag is True when __loop_iter_deps__ present."""
        interp = _make_interp()
        interp.exec("items = get_untrusted_list()")

        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        exc = TypeError("loop error")
        exc.__lineno__ = 3  # type: ignore[attr-defined]
        exc.__loop_iter_deps__ = {"items": _UNTRUSTED_SOURCES}  # type: ignore[attr-defined]

        redactor.classify(exc, dict(interp.store), interpreter=interp)

        event = audit_log[0]
        assert event.m4_f9_applied is True, (
            "M4-F17: m4_f9_applied must be True when __loop_iter_deps__ is present"
        )

    def test_exception_without_loop_annotation_m4_f9_not_applied(self) -> None:
        """M4-F17: audit event m4_f9_applied is False when __loop_iter_deps__ absent."""
        trusted_store: dict[str, CaMeLValue] = {
            "x": wrap(1, sources=_TRUSTED_SOURCES),
        }
        audit_log: list[RedactionAuditEvent] = []
        redactor = _redactor(audit_log=audit_log)
        exc = TypeError("no loop context")
        redactor.classify(exc, trusted_store)

        event = audit_log[0]
        assert event.m4_f9_applied is False, (
            "M4-F17: m4_f9_applied must be False when no loop annotation present"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — STRICT mode closes primary exception side-channel vectors
#
# The combination of M4-F1 (for-loop taint) + M4-F6 (redaction) means an
# adversary cannot learn the value of the iterable by triggering an exception
# inside the loop body — the exception message is always redacted.
# ---------------------------------------------------------------------------


class TestStrictModeExceptionSideChannelClosure:
    """End-to-end tests verifying STRICT mode closes the exception side channel."""

    def test_strict_mode_exception_in_loop_body_is_redacted(self) -> None:
        """STRICT + M4-F1 + M4-F6: exception in loop body with untrusted iterable is redacted.

        End-to-end scenario: adversary controls the iterable, causes an
        exception inside the loop — the combined effect of M4-F1 (which
        propagates iterable taint to loop body variables) and M4-F6 (which
        redacts exceptions with untrusted dependency) ensures the adversary
        cannot exfiltrate data via the exception message.
        """
        interp = _make_interp()
        interp.exec("items = get_untrusted_list()")

        # Simulate a loop-body assignment variable (inherits iterable taint per M4-F1)
        # by directly inserting a tainted value into the store.
        interp._store["loop_var"] = wrap(  # type: ignore[attr-defined]
            "data", sources=_UNTRUSTED_SOURCES
        )

        exc = ValueError("loop_var data revealed: SECRET")
        exc.__lineno__ = 7  # type: ignore[attr-defined]

        redactor = _redactor()
        result = redactor.classify(exc, dict(interp.store), interpreter=interp)

        assert result.message is None, (
            "STRICT + M4-F1 + M4-F6: exception message must be None — "
            "loop-body exception side channel is closed"
        )
        assert result.trust_level == "untrusted"

    def test_empty_store_trusted_exception_not_redacted(self) -> None:
        """Baseline: exception with empty store (no untrusted variables) is trusted.

        Validates that the redaction logic does not over-redact when no
        untrusted data is present.
        """
        redactor = _redactor()
        exc = KeyError("missing_key")
        result = redactor.classify(exc, {})

        assert result.trust_level == "trusted", (
            "Baseline: exception with empty store must be trusted (not over-redacted)"
        )
        assert result.message is not None, (
            "Baseline: trusted exception message must not be None"
        )

    def test_multiple_untrusted_variables_all_trigger_redaction(self) -> None:
        """M4-F6: any untrusted variable in the store triggers redaction.

        Even if the exception is not obviously related to one variable,
        the presence of any untrusted source in the store is sufficient.
        """
        store: dict[str, CaMeLValue] = {
            "a": wrap("safe", sources=_TRUSTED_SOURCES),
            "b": wrap("unsafe", sources=_UNTRUSTED_SOURCES),
            "c": wrap("safe2", sources=_CAMEL_SOURCES),
        }
        redactor = _redactor()
        exc = RuntimeError("error involving b")
        result = redactor.classify(exc, store)

        assert result.message is None, (
            "M4-F6: presence of any untrusted variable must trigger redaction"
        )

    def test_prd_section11_exception_bit_leakage_target_100_percent(self) -> None:
        """PRD §11: exception-based bit leakage mitigation achieves 100% pass rate.

        Exercises all three redaction cases in sequence and verifies each
        produces the expected result, confirming the PRD §11 target.
        """
        redactor = ExceptionRedactor()

        # Case 1: NEIE — always fully stripped (M4-F7)
        neie_result = redactor.classify(CamelNEIE(), {})
        assert neie_result.message is None
        assert neie_result.trust_level == "not_enough_information"

        # Case 2: exception with untrusted store — message redacted (M4-F6)
        untrusted_store: dict[str, CaMeLValue] = {
            "data": wrap("secret", sources=_UNTRUSTED_SOURCES)
        }
        untrusted_exc = ValueError("secret revealed")
        untrusted_result = redactor.classify(untrusted_exc, untrusted_store)
        assert untrusted_result.message is None
        assert untrusted_result.trust_level == "untrusted"

        # Case 3: exception with trusted store — message preserved
        trusted_store: dict[str, CaMeLValue] = {
            "x": wrap(1, sources=_TRUSTED_SOURCES)
        }
        trusted_exc = TypeError("expected int")
        trusted_result = redactor.classify(trusted_exc, trusted_store)
        assert trusted_result.message is not None
        assert trusted_result.trust_level == "trusted"
