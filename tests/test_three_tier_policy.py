"""Unit tests for the three-tier policy governance module (ADR-011).

Covers:
- PolicyTier enum values and ordering
- TieredPolicyRegistry.register() validation (non_overridable on wrong tier)
- TieredPolicyRegistry convenience registration methods
- PolicyConflictResolver conflict resolution algorithm (all 9 scenarios from ADR-011)
- MergedPolicyResult properties (is_allowed, can_be_consented)
- Audit trail content and authoritative flag
- CaMeLInterpreter integration with conflict_resolver
- Mutual-exclusion guard (policy_engine + conflict_resolver raises ValueError)
- camel_security public API exports
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from camel.interpreter import CaMeLInterpreter, PolicyViolationError
from camel.policy.governance import (
    MergedPolicyResult,
    PolicyConflictResolver,
    PolicyTier,
    TieredPolicyRegistry,
    TierEvaluationRecord,
)
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult
from camel.value import CaMeLValue, Public, wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allow(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """Policy function that always allows."""
    return Allowed()


def _deny(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """Policy function that always denies."""
    return Denied("always denied")


def _deny_reason(
    reason: str,
) -> object:
    """Factory returning a denial policy with a custom reason."""

    def _policy(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
        return Denied(reason)

    _policy.__name__ = f"deny_{reason}"
    return _policy


def _make_tool() -> tuple[str, CaMeLValue]:
    """Return a trivial tool function and the CaMeLValue it produces."""
    tool_result = wrap("ok", sources=frozenset({"my_tool"}), readers=Public)

    def my_tool() -> CaMeLValue:
        return tool_result

    return "my_tool", my_tool  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# PolicyTier
# ---------------------------------------------------------------------------


class TestPolicyTier:
    """PolicyTier enum sanity checks."""

    def test_values(self) -> None:
        """Members have the expected string values."""
        assert PolicyTier.PLATFORM.value == "Platform"
        assert PolicyTier.TOOL_PROVIDER.value == "ToolProvider"
        assert PolicyTier.USER.value == "User"

    def test_three_members(self) -> None:
        """There are exactly three tiers."""
        assert len(list(PolicyTier)) == 3


# ---------------------------------------------------------------------------
# TieredPolicyRegistry — registration validation
# ---------------------------------------------------------------------------


class TestTieredPolicyRegistryValidation:
    """Registration validation: non_overridable only valid on Platform tier."""

    def test_non_overridable_accepted_on_platform(self) -> None:
        """Platform tier accepts non_overridable=True without error."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, non_overridable=True)

    def test_non_overridable_rejected_on_tool_provider(self) -> None:
        """Tool-Provider tier must reject non_overridable=True."""
        reg = TieredPolicyRegistry()
        with pytest.raises(ValueError, match="non_overridable"):
            reg.register("tool", _allow, PolicyTier.TOOL_PROVIDER, non_overridable=True)

    def test_non_overridable_rejected_on_user(self) -> None:
        """User tier must reject non_overridable=True."""
        reg = TieredPolicyRegistry()
        with pytest.raises(ValueError, match="non_overridable"):
            reg.register("tool", _allow, PolicyTier.USER, non_overridable=True)

    def test_non_callable_policy_fn_raises_type_error(self) -> None:
        """Non-callable policy_fn raises TypeError."""
        reg = TieredPolicyRegistry()
        with pytest.raises(TypeError, match="callable"):
            reg.register("tool", "not_callable", PolicyTier.PLATFORM)  # type: ignore[arg-type]

    def test_returns_policy_fn_for_decorator_usage(self) -> None:
        """register() returns policy_fn unchanged (enables decorator usage)."""
        reg = TieredPolicyRegistry()
        returned = reg.register("tool", _allow, PolicyTier.PLATFORM)
        assert returned is _allow

    def test_register_platform_convenience(self) -> None:
        """register_platform() delegates correctly."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow)
        entries = reg.get_entries("tool", PolicyTier.PLATFORM)
        assert len(entries) == 1
        assert entries[0].tier is PolicyTier.PLATFORM

    def test_register_tool_provider_convenience(self) -> None:
        """register_tool_provider() delegates correctly."""
        reg = TieredPolicyRegistry()
        reg.register_tool_provider("tool", _allow)
        entries = reg.get_entries("tool", PolicyTier.TOOL_PROVIDER)
        assert len(entries) == 1
        assert entries[0].tier is PolicyTier.TOOL_PROVIDER

    def test_register_user_convenience(self) -> None:
        """register_user() delegates correctly."""
        reg = TieredPolicyRegistry()
        reg.register_user("tool", _allow)
        entries = reg.get_entries("tool", PolicyTier.USER)
        assert len(entries) == 1
        assert entries[0].tier is PolicyTier.USER

    def test_custom_name_stored(self) -> None:
        """Custom name is stored in the entry."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="my_named_policy")
        entry = reg.get_entries("tool", PolicyTier.PLATFORM)[0]
        assert entry.name == "my_named_policy"

    def test_default_name_is_fn_name(self) -> None:
        """Default entry name is policy_fn.__name__."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow)
        entry = reg.get_entries("tool", PolicyTier.PLATFORM)[0]
        assert entry.name == "_allow"

    def test_multiple_policies_per_tier_ordered(self) -> None:
        """Multiple policies per tier are stored in registration order."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="first")
        reg.register_platform("tool", _deny, name="second")
        entries = reg.get_entries("tool", PolicyTier.PLATFORM)
        assert [e.name for e in entries] == ["first", "second"]

    def test_registered_tools_all_tiers(self) -> None:
        """registered_tools() returns the union across all tiers."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool_a", _allow)
        reg.register_tool_provider("tool_b", _allow)
        reg.register_user("tool_c", _allow)
        tools = reg.registered_tools()
        assert tools == frozenset({"tool_a", "tool_b", "tool_c"})

    def test_registered_tools_by_tier(self) -> None:
        """registered_tools(tier) returns only tools for that tier."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool_a", _allow)
        reg.register_user("tool_b", _allow)
        assert reg.registered_tools(PolicyTier.PLATFORM) == frozenset({"tool_a"})
        assert reg.registered_tools(PolicyTier.USER) == frozenset({"tool_b"})
        assert reg.registered_tools(PolicyTier.TOOL_PROVIDER) == frozenset()


# ---------------------------------------------------------------------------
# PolicyConflictResolver — conflict resolution scenarios (ADR-011 §4.1)
# ---------------------------------------------------------------------------


class TestPolicyConflictResolverScenarios:
    """Exhaustive scenario table from ADR-011 Decision 4."""

    def _resolver_with(
        self,
        *,
        platform: SecurityPolicyResult | None = None,
        platform_non_overridable: bool = False,
        tool_provider: SecurityPolicyResult | None = None,
        user: SecurityPolicyResult | None = None,
    ) -> PolicyConflictResolver:
        """Build a resolver from simple result fixtures."""
        reg = TieredPolicyRegistry()
        if platform is not None:
            p = platform

            def _p(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
                return p

            _p.__name__ = "platform_policy"
            reg.register_platform("tool", _p, non_overridable=platform_non_overridable)
        if tool_provider is not None:
            tp = tool_provider

            def _tp(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
                return tp

            _tp.__name__ = "tp_policy"
            reg.register_tool_provider("tool", _tp)
        if user is not None:
            u = user

            def _u(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
                return u

            _u.__name__ = "user_policy"
            reg.register_user("tool", _u)
        return PolicyConflictResolver(reg)

    # Scenario 1 — Platform only, Denied (overridable)
    def test_platform_only_denied_overridable(self) -> None:
        """Platform-only Denied (non_overridable=False)."""
        resolver = self._resolver_with(platform=Denied("p_reason"))
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.PLATFORM
        assert not merged.non_overridable_denial
        assert merged.can_be_consented

    # Scenario 2 — Platform only, Denied (non-overridable)
    def test_platform_only_denied_non_overridable(self) -> None:
        """Platform-only Denied with non_overridable=True."""
        resolver = self._resolver_with(platform=Denied("noo_reason"), platform_non_overridable=True)
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.PLATFORM
        assert merged.non_overridable_denial
        assert not merged.can_be_consented

    # Scenario 3 — Tool-Provider only, Denied
    def test_tool_provider_only_denied(self) -> None:
        """Tool-Provider-only Denied."""
        resolver = self._resolver_with(tool_provider=Denied("tp_reason"))
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.TOOL_PROVIDER
        assert not merged.non_overridable_denial

    # Scenario 4 — User only, Denied
    def test_user_only_denied(self) -> None:
        """User-only Denied."""
        resolver = self._resolver_with(user=Denied("u_reason"))
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.USER
        assert not merged.non_overridable_denial

    # Scenario 5 — All Allowed
    def test_all_allowed(self) -> None:
        """All three tiers return Allowed."""
        resolver = self._resolver_with(
            platform=Allowed(),
            tool_provider=Allowed(),
            user=Allowed(),
        )
        merged = resolver.evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None
        assert not merged.non_overridable_denial
        assert len(merged.audit_trail) == 3

    # Scenario 6 — No policies registered
    def test_no_policies_registered(self) -> None:
        """Empty registry returns Allowed with no audit records."""
        resolver = PolicyConflictResolver(TieredPolicyRegistry())
        merged = resolver.evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None
        assert merged.audit_trail == ()

    # Scenario 7 — Mixed: Platform Allowed, TP Denied
    def test_mixed_platform_allowed_tp_denied(self) -> None:
        """Platform allows but Tool-Provider denies."""
        resolver = self._resolver_with(platform=Allowed(), tool_provider=Denied("tp_deny"))
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.TOOL_PROVIDER
        # Platform record is in the trail
        assert len(merged.audit_trail) == 2
        assert merged.audit_trail[0].tier is PolicyTier.PLATFORM
        assert merged.audit_trail[1].tier is PolicyTier.TOOL_PROVIDER

    # Scenario 8 — Mixed: Platform non-overridable Denied, TP/User skipped
    def test_platform_non_overridable_skips_lower_tiers(self) -> None:
        """Non-overridable Platform Denied skips TP and User evaluation."""
        resolver = self._resolver_with(
            platform=Denied("noo"),
            platform_non_overridable=True,
            tool_provider=Allowed(),
            user=Allowed(),
        )
        merged = resolver.evaluate("tool", {})
        # Only Platform was evaluated
        assert len(merged.audit_trail) == 1
        assert merged.audit_trail[0].tier is PolicyTier.PLATFORM
        assert merged.non_overridable_denial

    # Scenario 9 — All Denied (Platform wins)
    def test_all_denied_platform_wins(self) -> None:
        """All tiers would deny; Platform short-circuits first."""
        resolver = self._resolver_with(
            platform=Denied("p_deny"),
            tool_provider=Denied("tp_deny"),
            user=Denied("u_deny"),
        )
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.PLATFORM
        # Only Platform evaluated
        assert len(merged.audit_trail) == 1


# ---------------------------------------------------------------------------
# Audit trail content
# ---------------------------------------------------------------------------


class TestAuditTrail:
    """Verify TierEvaluationRecord fields and authoritative flag."""

    def test_authoritative_flag_on_denial(self) -> None:
        """The record for the denying policy has authoritative=True."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="p_allow")
        reg.register_tool_provider("tool", _deny, name="tp_deny")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})
        assert not merged.is_allowed
        tp_record = merged.audit_trail[-1]
        assert tp_record.authoritative is True
        platform_record = merged.audit_trail[0]
        assert platform_record.authoritative is False

    def test_audit_trail_all_allowed(self) -> None:
        """When all policies allow, no record is marked authoritative."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="p")
        reg.register_user("tool", _allow, name="u")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})
        assert merged.is_allowed
        for record in merged.audit_trail:
            assert not record.authoritative

    def test_non_overridable_flag_in_audit_record(self) -> None:
        """non_overridable=True is reflected in the TierEvaluationRecord."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _deny, non_overridable=True, name="noo")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})
        record = merged.audit_trail[0]
        assert record.non_overridable is True

    def test_tool_provider_record_always_non_overridable_false(self) -> None:
        """Tool-Provider records always have non_overridable=False."""
        reg = TieredPolicyRegistry()
        reg.register_tool_provider("tool", _deny, name="tp")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})
        record = merged.audit_trail[0]
        assert record.non_overridable is False

    def test_policy_name_in_record(self) -> None:
        """Policy name is correctly stored in TierEvaluationRecord."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="custom_name")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})
        assert merged.audit_trail[0].policy_name == "custom_name"


# ---------------------------------------------------------------------------
# evaluate_flat() — drop-in compatibility
# ---------------------------------------------------------------------------


class TestEvaluateFlat:
    """evaluate_flat() returns plain SecurityPolicyResult."""

    def test_flat_returns_allowed(self) -> None:
        """evaluate_flat() returns Allowed() when all tiers allow."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow)
        resolver = PolicyConflictResolver(reg)
        result = resolver.evaluate_flat("tool", {})
        assert isinstance(result, Allowed)

    def test_flat_returns_denied(self) -> None:
        """evaluate_flat() returns Denied() when any tier denies."""
        reg = TieredPolicyRegistry()
        reg.register_user("tool", _deny)
        resolver = PolicyConflictResolver(reg)
        result = resolver.evaluate_flat("tool", {})
        assert isinstance(result, Denied)


# ---------------------------------------------------------------------------
# CaMeLInterpreter integration
# ---------------------------------------------------------------------------


class TestInterpreterIntegration:
    """Interpreter uses conflict_resolver when provided."""

    def _make_interpreter(self, resolver: PolicyConflictResolver) -> CaMeLInterpreter:
        tool_name, tool_fn = _make_tool()
        return CaMeLInterpreter(
            tools={tool_name: tool_fn},
            conflict_resolver=resolver,
        )

    def test_allowed_policy_permits_tool_call(self) -> None:
        """Tool call proceeds when all tiers allow."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _allow)
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        interp.exec("result = my_tool()")
        assert interp.get("result").raw == "ok"

    def test_denied_policy_blocks_tool_call(self) -> None:
        """Tool call is blocked when any tier denies."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _deny)
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        with pytest.raises(PolicyViolationError):
            interp.exec("result = my_tool()")

    def test_non_overridable_denial_blocks_and_sets_audit_flag(self) -> None:
        """Non-overridable Platform Denied raises and logs correctly."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _deny, non_overridable=True)
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        with pytest.raises(PolicyViolationError):
            interp.exec("result = my_tool()")
        entry = interp.audit_log[-1]
        assert entry.outcome == "Denied"
        assert entry.non_overridable_denial is True
        assert entry.authoritative_tier == "Platform"

    def test_audit_log_records_allowed_with_tier(self) -> None:
        """Allowed tool call records authoritative_tier=None in audit log."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _allow)
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        interp.exec("result = my_tool()")
        entry = interp.audit_log[-1]
        assert entry.outcome == "Allowed"
        assert entry.authoritative_tier is None

    def test_mutual_exclusion_raises_value_error(self) -> None:
        """Supplying both policy_engine and conflict_resolver raises ValueError."""
        from camel.policy.interfaces import PolicyRegistry

        flat_registry = PolicyRegistry()
        reg = TieredPolicyRegistry()
        resolver = PolicyConflictResolver(reg)
        tool_name, tool_fn = _make_tool()
        with pytest.raises(ValueError, match="mutually exclusive"):
            CaMeLInterpreter(
                tools={tool_name: tool_fn},
                policy_engine=flat_registry,
                conflict_resolver=resolver,
            )

    def test_no_resolver_no_engine_allows_all(self) -> None:
        """Without engine or resolver, all tool calls are permitted."""
        tool_name, tool_fn = _make_tool()
        interp = CaMeLInterpreter(tools={tool_name: tool_fn})
        interp.exec("result = my_tool()")
        assert interp.get("result").raw == "ok"

    def test_tool_provider_denied_blocks_call(self) -> None:
        """Tool-Provider-tier denial blocks the tool call."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _allow)
        reg.register_tool_provider(
            "my_tool",
            _deny_reason("tp_reason"),  # type: ignore[arg-type]
        )
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        with pytest.raises(PolicyViolationError):
            interp.exec("result = my_tool()")
        entry = interp.audit_log[-1]
        assert entry.authoritative_tier == "ToolProvider"

    def test_user_tier_denied_blocks_call(self) -> None:
        """User-tier denial blocks the tool call."""
        reg = TieredPolicyRegistry()
        reg.register_platform("my_tool", _allow)
        reg.register_tool_provider("my_tool", _allow)
        reg.register_user("my_tool", _deny_reason("user_reason"))  # type: ignore[arg-type]
        resolver = PolicyConflictResolver(reg)
        interp = self._make_interpreter(resolver)
        with pytest.raises(PolicyViolationError):
            interp.exec("result = my_tool()")
        entry = interp.audit_log[-1]
        assert entry.authoritative_tier == "User"


# ---------------------------------------------------------------------------
# camel_security public API exports
# ---------------------------------------------------------------------------


class TestCamelSecurityExports:
    """New governance types are accessible from camel_security."""

    def test_policy_tier_exported(self) -> None:
        """PolicyTier is exported from camel_security."""
        import camel_security

        assert hasattr(camel_security, "PolicyTier")
        assert camel_security.PolicyTier is PolicyTier

    def test_tiered_registry_exported(self) -> None:
        """TieredPolicyRegistry is exported from camel_security."""
        import camel_security

        assert hasattr(camel_security, "TieredPolicyRegistry")
        assert camel_security.TieredPolicyRegistry is TieredPolicyRegistry

    def test_conflict_resolver_exported(self) -> None:
        """PolicyConflictResolver is exported from camel_security."""
        import camel_security

        assert hasattr(camel_security, "PolicyConflictResolver")
        assert camel_security.PolicyConflictResolver is PolicyConflictResolver

    def test_merged_policy_result_exported(self) -> None:
        """MergedPolicyResult is exported from camel_security."""
        import camel_security

        assert hasattr(camel_security, "MergedPolicyResult")
        assert camel_security.MergedPolicyResult is MergedPolicyResult

    def test_tier_evaluation_record_exported(self) -> None:
        """TierEvaluationRecord is exported from camel_security."""
        import camel_security

        assert hasattr(camel_security, "TierEvaluationRecord")
        assert camel_security.TierEvaluationRecord is TierEvaluationRecord


# ---------------------------------------------------------------------------
# MergedPolicyResult properties
# ---------------------------------------------------------------------------


class TestMergedPolicyResultProperties:
    """is_allowed and can_be_consented behave correctly."""

    def test_is_allowed_true_for_allowed_outcome(self) -> None:
        """is_allowed returns True for an Allowed outcome."""
        result = MergedPolicyResult(
            outcome=Allowed(),
            authoritative_tier=None,
            non_overridable_denial=False,
            audit_trail=(),
        )
        assert result.is_allowed is True

    def test_is_allowed_false_for_denied_outcome(self) -> None:
        """is_allowed returns False for a Denied outcome."""
        result = MergedPolicyResult(
            outcome=Denied("x"),
            authoritative_tier=PolicyTier.PLATFORM,
            non_overridable_denial=False,
            audit_trail=(),
        )
        assert result.is_allowed is False

    def test_can_be_consented_true_when_not_non_overridable(self) -> None:
        """can_be_consented returns True when non_overridable_denial=False."""
        result = MergedPolicyResult(
            outcome=Denied("x"),
            authoritative_tier=PolicyTier.USER,
            non_overridable_denial=False,
            audit_trail=(),
        )
        assert result.can_be_consented is True

    def test_can_be_consented_false_when_non_overridable(self) -> None:
        """can_be_consented returns False when non_overridable_denial=True."""
        result = MergedPolicyResult(
            outcome=Denied("x"),
            authoritative_tier=PolicyTier.PLATFORM,
            non_overridable_denial=True,
            audit_trail=(),
        )
        assert result.can_be_consented is False
