"""Tests for the ConsentHandler, ConsentDecisionCache, and consent audit logging.

Covers:
- ConsentDecision enum values
- ConsentAuditEntry creation and immutability
- ConsentHandler ABC cannot be instantiated directly
- DefaultCLIConsentHandler prompt rendering and input parsing
- ConsentDecisionCache: lookup miss, store/lookup for APPROVE_FOR_SESSION,
  APPROVE and REJECT are never cached, clear(), __len__()
- _resolve_consent: cache miss -> handler invoked, cache hit -> handler skipped,
  audit entries recorded, session_cache_hit flag correctness
- Interpreter integration: consent_handler wired into PRODUCTION enforcement mode,
  ConsentAuditEntry recorded on denial, APPROVE proceeds, REJECT raises,
  APPROVE_FOR_SESSION caches and proceeds without re-prompting
- camel_security.consent re-exports all public names
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from camel.consent import (
    ConsentAuditEntry,
    ConsentDecision,
    ConsentDecisionCache,
    ConsentHandler,
    DefaultCLIConsentHandler,
    _resolve_consent,
)
from camel.interpreter import (
    CaMeLInterpreter,
    EnforcementMode,
    PolicyViolationError,
)
from camel.policy.interfaces import Allowed, Denied, PolicyRegistry
from camel.value import CaMeLValue, Public, wrap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _untrusted_value(raw: Any, source: str = "read_email") -> CaMeLValue:
    return wrap(raw, sources=frozenset({source}), readers=Public)


def _trusted_value(raw: Any) -> CaMeLValue:
    return wrap(raw, sources=frozenset({"User literal"}), readers=Public)


def _make_deny_tool() -> tuple[dict[str, Any], PolicyRegistry]:
    """Return (tools, registry) where send_email is always denied."""

    def send_email(to: str, body: str) -> CaMeLValue:
        return wrap(True, sources=frozenset({"send_email"}))

    tools = {"send_email": send_email}
    registry = PolicyRegistry()
    registry.register(
        "send_email",
        lambda tool_name, kwargs: Denied("recipient address comes from untrusted data"),
    )
    return tools, registry


# ---------------------------------------------------------------------------
# ConsentDecision
# ---------------------------------------------------------------------------


class TestConsentDecision:
    """Tests for the ConsentDecision enum."""

    def test_members_exist(self) -> None:
        assert ConsentDecision.APPROVE
        assert ConsentDecision.REJECT
        assert ConsentDecision.APPROVE_FOR_SESSION

    def test_values(self) -> None:
        assert ConsentDecision.APPROVE.value == "APPROVE"
        assert ConsentDecision.REJECT.value == "REJECT"
        assert ConsentDecision.APPROVE_FOR_SESSION.value == "APPROVE_FOR_SESSION"


# ---------------------------------------------------------------------------
# ConsentAuditEntry
# ---------------------------------------------------------------------------


class TestConsentAuditEntry:
    """Tests for ConsentAuditEntry dataclass."""

    def test_creation(self) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        entry = ConsentAuditEntry(
            decision=ConsentDecision.APPROVE,
            timestamp=ts,
            tool_name="send_email",
            argument_summary="send_email(to='x@y.com')",
            session_cache_hit=False,
        )
        assert entry.decision is ConsentDecision.APPROVE
        assert entry.timestamp == ts
        assert entry.tool_name == "send_email"
        assert entry.argument_summary == "send_email(to='x@y.com')"
        assert entry.session_cache_hit is False

    def test_immutable(self) -> None:
        entry = ConsentAuditEntry(
            decision=ConsentDecision.REJECT,
            timestamp="2026-01-01T00:00:00+00:00",
            tool_name="write_file",
            argument_summary="write_file(path='x')",
            session_cache_hit=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            entry.decision = ConsentDecision.APPROVE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConsentHandler ABC
# ---------------------------------------------------------------------------


class TestConsentHandlerABC:
    """Tests for the ConsentHandler ABC."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            ConsentHandler()  # type: ignore[abstract]

    def test_subclass_must_implement_handle_consent(self) -> None:
        class IncompleteHandler(ConsentHandler):
            pass

        with pytest.raises(TypeError):
            IncompleteHandler()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class AlwaysApprove(ConsentHandler):
            def handle_consent(
                self,
                tool_name: str,
                argument_summary: str,
                denial_reason: str,
            ) -> ConsentDecision:
                return ConsentDecision.APPROVE

        handler = AlwaysApprove()
        result = handler.handle_consent("t", "args", "reason")
        assert result is ConsentDecision.APPROVE


# ---------------------------------------------------------------------------
# DefaultCLIConsentHandler
# ---------------------------------------------------------------------------


class TestDefaultCLIConsentHandler:
    """Tests for DefaultCLIConsentHandler."""

    @pytest.mark.parametrize(
        "user_input,expected",
        [
            ("A", ConsentDecision.APPROVE),
            ("a", ConsentDecision.APPROVE),
            ("R", ConsentDecision.REJECT),
            ("r", ConsentDecision.REJECT),
            ("S", ConsentDecision.APPROVE_FOR_SESSION),
            ("s", ConsentDecision.APPROVE_FOR_SESSION),
        ],
    )
    def test_valid_choices(
        self, user_input: str, expected: ConsentDecision
    ) -> None:
        handler = DefaultCLIConsentHandler()
        with (
            patch("builtins.input", return_value=user_input),
            patch("builtins.print"),
        ):
            result = handler.handle_consent(
                "send_email", "to='x@y.com'", "untrusted recipient"
            )
        assert result is expected

    def test_invalid_then_valid(self) -> None:
        handler = DefaultCLIConsentHandler()
        inputs = iter(["X", "?", "A"])
        with (
            patch("builtins.input", side_effect=inputs),
            patch("builtins.print"),
        ):
            result = handler.handle_consent("send_email", "args", "reason")
        assert result is ConsentDecision.APPROVE

    def test_displays_tool_name_and_reason(self) -> None:
        handler = DefaultCLIConsentHandler()
        printed: list[str] = []
        with (
            patch("builtins.input", return_value="R"),
            patch("builtins.print", side_effect=lambda *a, **kw: printed.append(str(a))),
        ):
            handler.handle_consent(
                "send_email",
                "to=untrusted_addr [sources: read_email]",
                "recipient from untrusted source",
            )
        full_output = " ".join(printed)
        assert "send_email" in full_output
        assert "untrusted_addr" in full_output or "sources" in full_output
        assert "recipient from untrusted source" in full_output


# ---------------------------------------------------------------------------
# ConsentDecisionCache
# ---------------------------------------------------------------------------


class TestConsentDecisionCache:
    """Tests for ConsentDecisionCache."""

    def test_lookup_miss(self) -> None:
        cache = ConsentDecisionCache()
        assert cache.lookup("send_email", "args") is None

    def test_store_and_lookup_approve_for_session(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args", ConsentDecision.APPROVE_FOR_SESSION)
        assert cache.lookup("send_email", "args") is ConsentDecision.APPROVE_FOR_SESSION

    def test_approve_not_cached(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args", ConsentDecision.APPROVE)
        assert cache.lookup("send_email", "args") is None

    def test_reject_not_cached(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args", ConsentDecision.REJECT)
        assert cache.lookup("send_email", "args") is None

    def test_different_tool_names_are_separate_keys(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args", ConsentDecision.APPROVE_FOR_SESSION)
        assert cache.lookup("write_file", "args") is None

    def test_different_arg_summaries_are_separate_keys(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args_A", ConsentDecision.APPROVE_FOR_SESSION)
        assert cache.lookup("send_email", "args_B") is None

    def test_clear(self) -> None:
        cache = ConsentDecisionCache()
        cache.store("send_email", "args", ConsentDecision.APPROVE_FOR_SESSION)
        cache.clear()
        assert cache.lookup("send_email", "args") is None
        assert len(cache) == 0

    def test_len(self) -> None:
        cache = ConsentDecisionCache()
        assert len(cache) == 0
        cache.store("a", "x", ConsentDecision.APPROVE_FOR_SESSION)
        assert len(cache) == 1
        cache.store("b", "x", ConsentDecision.APPROVE_FOR_SESSION)
        assert len(cache) == 2
        cache.store("a", "x", ConsentDecision.APPROVE)  # not stored
        assert len(cache) == 2


# ---------------------------------------------------------------------------
# _resolve_consent helper
# ---------------------------------------------------------------------------


class TestResolveConsent:
    """Tests for the _resolve_consent internal helper."""

    def _make_handler(self, decision: ConsentDecision) -> ConsentHandler:
        class _Fixed(ConsentHandler):
            def handle_consent(self, tn: str, args: str, reason: str) -> ConsentDecision:
                return decision

        return _Fixed()

    def test_cache_miss_invokes_handler_and_records_entry(self) -> None:
        handler = self._make_handler(ConsentDecision.APPROVE)
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        result = _resolve_consent("tool", "args", "reason", handler, cache, audit)

        assert result is True
        assert len(audit) == 1
        assert audit[0].decision is ConsentDecision.APPROVE
        assert audit[0].session_cache_hit is False
        assert audit[0].tool_name == "tool"
        assert audit[0].argument_summary == "args"

    def test_approve_for_session_is_cached(self) -> None:
        handler = self._make_handler(ConsentDecision.APPROVE_FOR_SESSION)
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        _resolve_consent("tool", "args", "reason", handler, cache, audit)
        # Now cache should have the decision
        assert cache.lookup("tool", "args") is ConsentDecision.APPROVE_FOR_SESSION

    def test_cache_hit_skips_handler(self) -> None:
        call_count = 0

        class _Counting(ConsentHandler):
            def handle_consent(self, tn: str, args: str, reason: str) -> ConsentDecision:
                nonlocal call_count
                call_count += 1
                return ConsentDecision.APPROVE_FOR_SESSION

        handler = _Counting()
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        # First call — handler invoked, decision cached
        _resolve_consent("tool", "args", "reason", handler, cache, audit)
        assert call_count == 1

        # Second call — should use cache, not call handler
        result = _resolve_consent("tool", "args", "reason", handler, cache, audit)
        assert call_count == 1  # handler NOT called again
        assert result is True

    def test_cache_hit_records_session_cache_hit_true(self) -> None:
        handler = self._make_handler(ConsentDecision.APPROVE_FOR_SESSION)
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        _resolve_consent("tool", "args", "reason", handler, cache, audit)
        _resolve_consent("tool", "args", "reason", handler, cache, audit)

        assert len(audit) == 2
        assert audit[0].session_cache_hit is False
        assert audit[1].session_cache_hit is True

    def test_reject_returns_false(self) -> None:
        handler = self._make_handler(ConsentDecision.REJECT)
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        result = _resolve_consent("tool", "args", "reason", handler, cache, audit)
        assert result is False
        assert audit[0].decision is ConsentDecision.REJECT

    def test_approve_not_cached(self) -> None:
        handler = self._make_handler(ConsentDecision.APPROVE)
        cache = ConsentDecisionCache()
        audit: list[ConsentAuditEntry] = []

        _resolve_consent("tool", "args", "reason", handler, cache, audit)
        # APPROVE should NOT be cached
        assert cache.lookup("tool", "args") is None


# ---------------------------------------------------------------------------
# Interpreter integration
# ---------------------------------------------------------------------------


class _AutoApproveHandler(ConsentHandler):
    """Test handler that always approves (once)."""

    def handle_consent(
        self, tool_name: str, argument_summary: str, denial_reason: str
    ) -> ConsentDecision:
        return ConsentDecision.APPROVE


class _AutoRejectHandler(ConsentHandler):
    """Test handler that always rejects."""

    def handle_consent(
        self, tool_name: str, argument_summary: str, denial_reason: str
    ) -> ConsentDecision:
        return ConsentDecision.REJECT


class _ApproveForSessionHandler(ConsentHandler):
    """Test handler that always approves for session."""

    def __init__(self) -> None:
        self.call_count = 0

    def handle_consent(
        self, tool_name: str, argument_summary: str, denial_reason: str
    ) -> ConsentDecision:
        self.call_count += 1
        return ConsentDecision.APPROVE_FOR_SESSION


class TestInterpreterConsentHandlerIntegration:
    """Integration tests: ConsentHandler wired into the interpreter."""

    def _make_interp(
        self,
        handler: ConsentHandler,
        cache: ConsentDecisionCache | None = None,
    ) -> CaMeLInterpreter:
        tools, registry = _make_deny_tool()
        return CaMeLInterpreter(
            tools=tools,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_handler=handler,
            consent_cache=cache,
        )

    def test_production_mode_requires_consent_handler_or_callback(self) -> None:
        with pytest.raises(ValueError, match="consent_callback or consent_handler"):
            CaMeLInterpreter(
                enforcement_mode=EnforcementMode.PRODUCTION,
            )

    def test_approve_allows_tool_call(self) -> None:
        interp = self._make_interp(_AutoApproveHandler())
        # Seed untrusted value as 'addr'
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")
        assert interp.get("result").raw is True

    def test_reject_raises_policy_violation(self) -> None:
        interp = self._make_interp(_AutoRejectHandler())
        interp.seed("addr", _untrusted_value("evil@x.com"))
        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec("result = send_email(to=addr, body='hello')")
        assert exc_info.value.consent_decision == "UserRejected"

    def test_consent_audit_entry_recorded_on_approve(self) -> None:
        interp = self._make_interp(_AutoApproveHandler())
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")
        log = interp.consent_audit_log
        assert len(log) == 1
        entry = log[0]
        assert entry.decision is ConsentDecision.APPROVE
        assert entry.tool_name == "send_email"
        assert entry.session_cache_hit is False
        assert "send_email" in entry.argument_summary

    def test_consent_audit_entry_recorded_on_reject(self) -> None:
        interp = self._make_interp(_AutoRejectHandler())
        interp.seed("addr", _untrusted_value("evil@x.com"))
        with pytest.raises(PolicyViolationError):
            interp.exec("result = send_email(to=addr, body='hello')")
        log = interp.consent_audit_log
        assert len(log) == 1
        assert log[0].decision is ConsentDecision.REJECT

    def test_approve_for_session_caches_decision(self) -> None:
        handler = _ApproveForSessionHandler()
        cache = ConsentDecisionCache()
        interp = self._make_interp(handler, cache)
        interp.seed("addr", _untrusted_value("evil@x.com"))

        # First call — handler invoked
        interp.exec("result = send_email(to=addr, body='hello')")
        assert handler.call_count == 1

        # Second call with same args — should use cache
        interp.exec("result2 = send_email(to=addr, body='hello')")
        assert handler.call_count == 1  # not called again

        log = interp.consent_audit_log
        assert len(log) == 2
        assert log[0].session_cache_hit is False
        assert log[1].session_cache_hit is True

    def test_consent_audit_log_returns_copy(self) -> None:
        interp = self._make_interp(_AutoApproveHandler())
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")

        log1 = interp.consent_audit_log
        log1.clear()
        log2 = interp.consent_audit_log
        assert len(log2) == 1  # not affected by clearing log1

    def test_consent_audit_entry_has_valid_timestamp(self) -> None:
        interp = self._make_interp(_AutoApproveHandler())
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")
        entry = interp.consent_audit_log[0]
        # Should parse as a valid ISO-8601 datetime
        dt = datetime.fromisoformat(entry.timestamp)
        assert dt.tzinfo is not None  # UTC timezone present

    def test_legacy_consent_callback_still_works(self) -> None:
        """Backward compatibility: existing consent_callback interface unchanged."""
        tools, registry = _make_deny_tool()
        approved_calls: list[str] = []

        def legacy_callback(
            tool_name: str, arg_summary: str, denial_reason: str
        ) -> bool:
            approved_calls.append(tool_name)
            return True

        interp = CaMeLInterpreter(
            tools=tools,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=legacy_callback,
        )
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")
        assert approved_calls == ["send_email"]
        # consent_audit_log is empty when using legacy callback
        assert interp.consent_audit_log == []

    def test_consent_handler_takes_precedence_over_callback(self) -> None:
        """When both are set, consent_handler is used."""
        tools, registry = _make_deny_tool()
        callback_calls: list[str] = []

        def legacy_callback(tn: str, args: str, reason: str) -> bool:
            callback_calls.append(tn)
            return True

        interp = CaMeLInterpreter(
            tools=tools,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=legacy_callback,
            consent_handler=_AutoApproveHandler(),
        )
        interp.seed("addr", _untrusted_value("evil@x.com"))
        interp.exec("result = send_email(to=addr, body='hello')")

        # legacy callback should NOT have been called
        assert callback_calls == []
        # consent_handler was used, so audit log has entry
        assert len(interp.consent_audit_log) == 1


# ---------------------------------------------------------------------------
# camel_security.consent namespace re-exports
# ---------------------------------------------------------------------------


class TestCamelSecurityConsentNamespace:
    """Verify all names are exported from camel_security.consent."""

    def test_all_names_importable(self) -> None:
        from camel_security.consent import (  # noqa: F401
            ConsentAuditEntry,
            ConsentDecision,
            ConsentDecisionCache,
            ConsentHandler,
            DefaultCLIConsentHandler,
        )

    def test_all_names_in_all(self) -> None:
        import camel_security.consent as m

        expected = {
            "ConsentDecision",
            "ConsentAuditEntry",
            "ConsentHandler",
            "DefaultCLIConsentHandler",
            "ConsentDecisionCache",
        }
        assert expected <= set(m.__all__)
