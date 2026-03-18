"""Comprehensive unit tests for PolicyConflictResolver conflict resolution.

Covers all 12 conflict scenarios required by the three-tier governance sprint:

 1.  Platform-only allow
 2.  Platform-only deny
 3.  Tool-Provider-only allow / deny
 4.  User-only allow / deny
 5.  All tiers allow
 6.  All tiers deny
 7.  Mixed — Platform allow + Tool-Provider deny
 8.  Mixed — Platform deny + User allow (User never reached)
 9.  Non-overridable Platform deny with lower-tier allow attempts
10.  Non-overridable Platform allow with lower-tier deny (lower tiers still restrict)
11.  Missing tier defaults to allow
12.  Audit trail correctness — authoritative_tier correctly identified in every scenario

Each test asserts:
- The final ``SecurityPolicyResult`` (Allowed / Denied).
- The ``authoritative_tier`` on the :class:`~camel.policy.governance.MergedPolicyResult`.
- The ``audit_trail`` contents (tiers evaluated, ``authoritative`` flag, ``non_overridable`` flag).

No LLM calls, no external services.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

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
# Shared policy fixtures
# ---------------------------------------------------------------------------


def _allow(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """Policy that always returns Allowed."""
    return Allowed()


def _deny(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """Policy that always returns Denied."""
    return Denied("always denied")


def _make_deny(reason: str, fn_name: str | None = None) -> object:
    """Return a Denied policy with a custom reason string."""

    def _policy(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> SecurityPolicyResult:
        return Denied(reason)

    _policy.__name__ = fn_name or f"deny_{reason}"
    return _policy


# ---------------------------------------------------------------------------
# Helper — build a resolver from simple per-tier result fixtures
# ---------------------------------------------------------------------------


def _resolver(
    *,
    platform: SecurityPolicyResult | None = None,
    platform_non_overridable: bool = False,
    tool_provider: SecurityPolicyResult | None = None,
    user: SecurityPolicyResult | None = None,
    tool_name: str = "tool",
) -> PolicyConflictResolver:
    """Build a :class:`PolicyConflictResolver` from simple per-tier fixtures."""
    reg = TieredPolicyRegistry()

    if platform is not None:
        p = platform

        def _p(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return p

        _p.__name__ = "platform_policy"
        reg.register_platform(tool_name, _p, non_overridable=platform_non_overridable)

    if tool_provider is not None:
        tp = tool_provider

        def _tp(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return tp

        _tp.__name__ = "tp_policy"
        reg.register_tool_provider(tool_name, _tp)

    if user is not None:
        u = user

        def _u(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            return u

        _u.__name__ = "user_policy"
        reg.register_user(tool_name, _u)

    return PolicyConflictResolver(reg)


# ---------------------------------------------------------------------------
# Scenario 1 — Platform-only allow
# ---------------------------------------------------------------------------


class TestScenario01PlatformOnlyAllow:
    """Scenario 1: only a Platform policy registered; it returns Allowed."""

    def test_outcome_is_allowed(self) -> None:
        """Final outcome is Allowed."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert isinstance(merged.outcome, Allowed)
        assert merged.is_allowed

    def test_authoritative_tier_is_none(self) -> None:
        """When everything allows, authoritative_tier is None."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert merged.authoritative_tier is None

    def test_non_overridable_denial_is_false(self) -> None:
        """non_overridable_denial is False for an allow outcome."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert merged.non_overridable_denial is False

    def test_audit_trail_has_one_platform_record(self) -> None:
        """Audit trail contains exactly one Platform record."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        assert merged.audit_trail[0].tier is PolicyTier.PLATFORM

    def test_platform_record_not_authoritative(self) -> None:
        """Platform allow record has authoritative=False (no denial determined it)."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert merged.audit_trail[0].authoritative is False


# ---------------------------------------------------------------------------
# Scenario 2 — Platform-only deny
# ---------------------------------------------------------------------------


class TestScenario02PlatformOnlyDeny:
    """Scenario 2: only a Platform policy registered; it returns Denied."""

    def test_outcome_is_denied(self) -> None:
        """Final outcome is Denied."""
        merged = _resolver(platform=Denied("p_reason")).evaluate("tool", {})
        assert isinstance(merged.outcome, Denied)
        assert not merged.is_allowed

    def test_authoritative_tier_is_platform(self) -> None:
        """Authoritative tier is PLATFORM."""
        merged = _resolver(platform=Denied("p_reason")).evaluate("tool", {})
        assert merged.authoritative_tier is PolicyTier.PLATFORM

    def test_non_overridable_denial_is_false_by_default(self) -> None:
        """Without non_overridable flag, non_overridable_denial is False."""
        merged = _resolver(platform=Denied("p_reason")).evaluate("tool", {})
        assert merged.non_overridable_denial is False
        assert merged.can_be_consented is True

    def test_audit_trail_has_one_authoritative_record(self) -> None:
        """Audit trail has one record with authoritative=True."""
        merged = _resolver(platform=Denied("p_reason")).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        assert merged.audit_trail[0].authoritative is True
        assert merged.audit_trail[0].tier is PolicyTier.PLATFORM

    def test_denial_reason_propagated(self) -> None:
        """Denial reason from policy is propagated to the outcome."""
        merged = _resolver(platform=Denied("specific_reason")).evaluate("tool", {})
        assert isinstance(merged.outcome, Denied)
        assert merged.outcome.reason == "specific_reason"


# ---------------------------------------------------------------------------
# Scenario 3 — Tool-Provider-only allow / deny
# ---------------------------------------------------------------------------


class TestScenario03ToolProviderOnly:
    """Scenario 3: only a Tool-Provider policy registered."""

    def test_tool_provider_allow_outcome(self) -> None:
        """Tool-Provider allow → Allowed outcome, authoritative_tier=None."""
        merged = _resolver(tool_provider=Allowed()).evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None

    def test_tool_provider_allow_audit_trail(self) -> None:
        """Tool-Provider allow: one TOOL_PROVIDER record, not authoritative."""
        merged = _resolver(tool_provider=Allowed()).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        record = merged.audit_trail[0]
        assert record.tier is PolicyTier.TOOL_PROVIDER
        assert record.authoritative is False
        assert record.non_overridable is False

    def test_tool_provider_deny_outcome(self) -> None:
        """Tool-Provider deny → Denied outcome."""
        merged = _resolver(tool_provider=Denied("tp_reason")).evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.TOOL_PROVIDER

    def test_tool_provider_deny_audit_trail(self) -> None:
        """Tool-Provider deny: one TOOL_PROVIDER record, authoritative=True."""
        merged = _resolver(tool_provider=Denied("tp_reason")).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        record = merged.audit_trail[0]
        assert record.tier is PolicyTier.TOOL_PROVIDER
        assert record.authoritative is True
        assert record.non_overridable is False

    def test_tool_provider_deny_not_non_overridable(self) -> None:
        """Tool-Provider denial is always overridable (can_be_consented=True)."""
        merged = _resolver(tool_provider=Denied("tp")).evaluate("tool", {})
        assert merged.non_overridable_denial is False
        assert merged.can_be_consented is True


# ---------------------------------------------------------------------------
# Scenario 4 — User-only allow / deny
# ---------------------------------------------------------------------------


class TestScenario04UserOnly:
    """Scenario 4: only a User policy registered."""

    def test_user_allow_outcome(self) -> None:
        """User allow → Allowed outcome, authoritative_tier=None."""
        merged = _resolver(user=Allowed()).evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None

    def test_user_allow_audit_trail(self) -> None:
        """User allow: one USER record, not authoritative."""
        merged = _resolver(user=Allowed()).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        record = merged.audit_trail[0]
        assert record.tier is PolicyTier.USER
        assert record.authoritative is False
        assert record.non_overridable is False

    def test_user_deny_outcome(self) -> None:
        """User deny → Denied outcome, authoritative_tier=USER."""
        merged = _resolver(user=Denied("u_reason")).evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.USER

    def test_user_deny_audit_trail(self) -> None:
        """User deny: one USER record, authoritative=True."""
        merged = _resolver(user=Denied("u_reason")).evaluate("tool", {})
        assert len(merged.audit_trail) == 1
        record = merged.audit_trail[0]
        assert record.tier is PolicyTier.USER
        assert record.authoritative is True
        assert record.non_overridable is False

    def test_user_deny_not_non_overridable(self) -> None:
        """User denial is always overridable."""
        merged = _resolver(user=Denied("u")).evaluate("tool", {})
        assert merged.non_overridable_denial is False
        assert merged.can_be_consented is True


# ---------------------------------------------------------------------------
# Scenario 5 — All tiers allow
# ---------------------------------------------------------------------------


class TestScenario05AllAllow:
    """Scenario 5: all three tiers registered with Allowed policies."""

    def setup_method(self) -> None:
        """Build resolver with all three tiers allowing."""
        self.merged = _resolver(
            platform=Allowed(),
            tool_provider=Allowed(),
            user=Allowed(),
        ).evaluate("tool", {})

    def test_outcome_is_allowed(self) -> None:
        """Final outcome is Allowed."""
        assert self.merged.is_allowed

    def test_authoritative_tier_is_none(self) -> None:
        """No authoritative tier when all allow."""
        assert self.merged.authoritative_tier is None

    def test_non_overridable_denial_false(self) -> None:
        """non_overridable_denial is False."""
        assert self.merged.non_overridable_denial is False

    def test_audit_trail_has_three_records(self) -> None:
        """All three tiers produce audit records."""
        assert len(self.merged.audit_trail) == 3

    def test_audit_trail_order(self) -> None:
        """Audit trail order: Platform → Tool-Provider → User."""
        tiers = [r.tier for r in self.merged.audit_trail]
        assert tiers == [
            PolicyTier.PLATFORM,
            PolicyTier.TOOL_PROVIDER,
            PolicyTier.USER,
        ]

    def test_no_record_is_authoritative(self) -> None:
        """No record has authoritative=True when everything allows."""
        for record in self.merged.audit_trail:
            assert record.authoritative is False


# ---------------------------------------------------------------------------
# Scenario 6 — All tiers deny (Platform short-circuits)
# ---------------------------------------------------------------------------


class TestScenario06AllDeny:
    """Scenario 6: all three tiers would deny; Platform short-circuits."""

    def setup_method(self) -> None:
        """Build resolver where every tier denies."""
        self.merged = _resolver(
            platform=Denied("p_deny"),
            tool_provider=Denied("tp_deny"),
            user=Denied("u_deny"),
        ).evaluate("tool", {})

    def test_outcome_is_denied(self) -> None:
        """Final outcome is Denied."""
        assert not self.merged.is_allowed

    def test_authoritative_tier_is_platform(self) -> None:
        """Platform short-circuits so authoritative_tier=PLATFORM."""
        assert self.merged.authoritative_tier is PolicyTier.PLATFORM

    def test_only_platform_evaluated(self) -> None:
        """Tool-Provider and User are never evaluated."""
        assert len(self.merged.audit_trail) == 1
        assert self.merged.audit_trail[0].tier is PolicyTier.PLATFORM

    def test_platform_record_is_authoritative(self) -> None:
        """The single Platform record has authoritative=True."""
        assert self.merged.audit_trail[0].authoritative is True

    def test_denial_reason_is_platform_reason(self) -> None:
        """Denial reason comes from the Platform policy."""
        assert isinstance(self.merged.outcome, Denied)
        assert self.merged.outcome.reason == "p_deny"


# ---------------------------------------------------------------------------
# Scenario 7 — Mixed: Platform allow + Tool-Provider deny
# ---------------------------------------------------------------------------


class TestScenario07PlatformAllowTPDeny:
    """Scenario 7: Platform allows, Tool-Provider denies."""

    def setup_method(self) -> None:
        """Build resolver where Platform allows and Tool-Provider denies."""
        self.merged = _resolver(
            platform=Allowed(),
            tool_provider=Denied("tp_deny"),
            user=Allowed(),
        ).evaluate("tool", {})

    def test_outcome_is_denied(self) -> None:
        """Final outcome is Denied."""
        assert not self.merged.is_allowed

    def test_authoritative_tier_is_tool_provider(self) -> None:
        """Authoritative tier is TOOL_PROVIDER."""
        assert self.merged.authoritative_tier is PolicyTier.TOOL_PROVIDER

    def test_user_not_evaluated(self) -> None:
        """User tier is skipped after Tool-Provider denial."""
        tiers_evaluated = [r.tier for r in self.merged.audit_trail]
        assert PolicyTier.USER not in tiers_evaluated

    def test_audit_trail_has_two_records(self) -> None:
        """Platform and Tool-Provider are both in the audit trail."""
        assert len(self.merged.audit_trail) == 2

    def test_audit_trail_tiers_in_order(self) -> None:
        """Audit trail order: Platform → Tool-Provider."""
        assert self.merged.audit_trail[0].tier is PolicyTier.PLATFORM
        assert self.merged.audit_trail[1].tier is PolicyTier.TOOL_PROVIDER

    def test_platform_record_not_authoritative(self) -> None:
        """Platform allow record has authoritative=False."""
        assert self.merged.audit_trail[0].authoritative is False

    def test_tp_record_is_authoritative(self) -> None:
        """Tool-Provider deny record has authoritative=True."""
        assert self.merged.audit_trail[1].authoritative is True

    def test_non_overridable_denial_is_false(self) -> None:
        """Tool-Provider denial is overridable."""
        assert self.merged.non_overridable_denial is False
        assert self.merged.can_be_consented is True


# ---------------------------------------------------------------------------
# Scenario 8 — Mixed: Platform deny + User allow (User never reached)
# ---------------------------------------------------------------------------


class TestScenario08PlatformDenyUserAllow:
    """Scenario 8: Platform denies; User allow is irrelevant (never reached)."""

    def setup_method(self) -> None:
        """Build resolver where Platform denies and User allows."""
        self.merged = _resolver(
            platform=Denied("platform_blocks"),
            user=Allowed(),
        ).evaluate("tool", {})

    def test_outcome_is_denied(self) -> None:
        """Final outcome is Denied regardless of User allow."""
        assert not self.merged.is_allowed

    def test_authoritative_tier_is_platform(self) -> None:
        """Platform denial short-circuits; authoritative_tier=PLATFORM."""
        assert self.merged.authoritative_tier is PolicyTier.PLATFORM

    def test_user_not_evaluated(self) -> None:
        """User tier is never evaluated when Platform denies first."""
        tiers_evaluated = [r.tier for r in self.merged.audit_trail]
        assert PolicyTier.USER not in tiers_evaluated

    def test_only_platform_in_audit_trail(self) -> None:
        """Only one record in audit trail (Platform)."""
        assert len(self.merged.audit_trail) == 1
        assert self.merged.audit_trail[0].tier is PolicyTier.PLATFORM

    def test_platform_record_is_authoritative(self) -> None:
        """Platform record has authoritative=True."""
        assert self.merged.audit_trail[0].authoritative is True

    def test_denial_is_overridable(self) -> None:
        """Platform deny without non_overridable flag is overridable."""
        assert self.merged.non_overridable_denial is False
        assert self.merged.can_be_consented is True


# ---------------------------------------------------------------------------
# Scenario 9 — Non-overridable Platform deny with lower-tier allow attempts
# ---------------------------------------------------------------------------


class TestScenario09NonOverridablePlatformDeny:
    """Scenario 9: non_overridable Platform deny + lower-tier allow attempts.

    Lower tiers allow but must be completely skipped.  The result is a
    non-overridable denial; the consent callback must not be invoked.
    """

    def setup_method(self) -> None:
        """Build resolver with non-overridable Platform deny."""
        self.merged = _resolver(
            platform=Denied("noo_reason"),
            platform_non_overridable=True,
            tool_provider=Allowed(),
            user=Allowed(),
        ).evaluate("tool", {})

    def test_outcome_is_denied(self) -> None:
        """Final outcome is Denied."""
        assert not self.merged.is_allowed

    def test_authoritative_tier_is_platform(self) -> None:
        """Authoritative tier is PLATFORM."""
        assert self.merged.authoritative_tier is PolicyTier.PLATFORM

    def test_non_overridable_denial_is_true(self) -> None:
        """non_overridable_denial=True is set on the merged result."""
        assert self.merged.non_overridable_denial is True

    def test_can_be_consented_is_false(self) -> None:
        """Consent callback must not be invoked."""
        assert self.merged.can_be_consented is False

    def test_only_platform_evaluated(self) -> None:
        """Tool-Provider and User tiers are completely skipped."""
        assert len(self.merged.audit_trail) == 1
        assert self.merged.audit_trail[0].tier is PolicyTier.PLATFORM

    def test_platform_record_non_overridable_flag(self) -> None:
        """Audit record has non_overridable=True."""
        assert self.merged.audit_trail[0].non_overridable is True

    def test_platform_record_is_authoritative(self) -> None:
        """Platform record has authoritative=True."""
        assert self.merged.audit_trail[0].authoritative is True

    def test_denial_reason_propagated(self) -> None:
        """Denial reason from non-overridable policy is propagated."""
        assert isinstance(self.merged.outcome, Denied)
        assert self.merged.outcome.reason == "noo_reason"


# ---------------------------------------------------------------------------
# Scenario 10 — Non-overridable Platform allow + lower-tier deny
# ---------------------------------------------------------------------------


class TestScenario10NonOverridablePlatformAllow:
    """Scenario 10: non_overridable Platform allow + lower-tier deny.

    The non_overridable flag only affects Denied results from Platform.
    When a non_overridable Platform policy returns Allowed, lower tiers
    can still deny — the final result is Denied.
    """

    def setup_method(self) -> None:
        """Build resolver with non-overridable Platform allow + TP deny."""
        self.merged = _resolver(
            platform=Allowed(),
            platform_non_overridable=True,
            tool_provider=Denied("tp_restricts"),
        ).evaluate("tool", {})

    def test_outcome_is_denied(self) -> None:
        """Lower tier denial takes effect even when Platform is non-overridable allow."""
        assert not self.merged.is_allowed

    def test_authoritative_tier_is_tool_provider(self) -> None:
        """Authoritative tier is TOOL_PROVIDER (lower tier imposed the denial)."""
        assert self.merged.authoritative_tier is PolicyTier.TOOL_PROVIDER

    def test_non_overridable_denial_is_false(self) -> None:
        """The denial comes from Tool-Provider, so non_overridable_denial=False."""
        assert self.merged.non_overridable_denial is False

    def test_can_be_consented_is_true(self) -> None:
        """Lower-tier denial is overridable by consent."""
        assert self.merged.can_be_consented is True

    def test_both_tiers_in_audit_trail(self) -> None:
        """Both Platform and Tool-Provider appear in the audit trail."""
        assert len(self.merged.audit_trail) == 2
        assert self.merged.audit_trail[0].tier is PolicyTier.PLATFORM
        assert self.merged.audit_trail[1].tier is PolicyTier.TOOL_PROVIDER

    def test_platform_record_not_authoritative(self) -> None:
        """Platform allow record is not authoritative."""
        assert self.merged.audit_trail[0].authoritative is False

    def test_tp_record_is_authoritative(self) -> None:
        """Tool-Provider deny record is authoritative."""
        assert self.merged.audit_trail[1].authoritative is True

    def test_user_not_evaluated(self) -> None:
        """User tier is skipped after Tool-Provider denial."""
        tiers = [r.tier for r in self.merged.audit_trail]
        assert PolicyTier.USER not in tiers

    # Variant: non-overridable Platform allow + User deny
    def test_user_deny_after_non_overridable_platform_allow(self) -> None:
        """non_overridable Platform allow + User deny → Denied, auth=USER."""
        merged = _resolver(
            platform=Allowed(),
            platform_non_overridable=True,
            user=Denied("user_restricts"),
        ).evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.USER
        assert merged.non_overridable_denial is False
        assert merged.can_be_consented is True


# ---------------------------------------------------------------------------
# Scenario 11 — Missing tier defaults to allow
# ---------------------------------------------------------------------------


class TestScenario11MissingTierDefaultsToAllow:
    """Scenario 11: absent tiers are treated as implicit Allowed."""

    def test_empty_registry_is_allowed(self) -> None:
        """No policies registered → Allowed, empty audit trail."""
        merged = PolicyConflictResolver(TieredPolicyRegistry()).evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None
        assert merged.audit_trail == ()
        assert merged.non_overridable_denial is False

    def test_platform_missing_tp_and_user_present_and_allowed(self) -> None:
        """Platform absent; TP and User allow → Allowed outcome."""
        merged = _resolver(
            tool_provider=Allowed(),
            user=Allowed(),
        ).evaluate("tool", {})
        assert merged.is_allowed
        assert merged.authoritative_tier is None
        tiers = [r.tier for r in merged.audit_trail]
        assert PolicyTier.PLATFORM not in tiers

    def test_tp_missing_platform_and_user_present_and_allowed(self) -> None:
        """Tool-Provider absent; Platform and User allow → Allowed outcome."""
        merged = _resolver(
            platform=Allowed(),
            user=Allowed(),
        ).evaluate("tool", {})
        assert merged.is_allowed
        tiers = [r.tier for r in merged.audit_trail]
        assert PolicyTier.TOOL_PROVIDER not in tiers

    def test_user_missing_platform_and_tp_present_and_denied(self) -> None:
        """User absent; Platform allows, TP denies → Denied (TP authoritative)."""
        merged = _resolver(
            platform=Allowed(),
            tool_provider=Denied("tp_blocks"),
        ).evaluate("tool", {})
        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.TOOL_PROVIDER
        tiers = [r.tier for r in merged.audit_trail]
        assert PolicyTier.USER not in tiers

    def test_all_tiers_missing_for_unregistered_tool(self) -> None:
        """Querying an unregistered tool name on a populated registry → Allowed."""
        reg = TieredPolicyRegistry()
        reg.register_platform("other_tool", _deny)
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("unregistered_tool", {})
        assert merged.is_allowed
        assert merged.audit_trail == ()


# ---------------------------------------------------------------------------
# Scenario 12 — Audit trail correctness (authoritative tier identification)
# ---------------------------------------------------------------------------


class TestScenario12AuditTrailCorrectness:
    """Scenario 12: authoritative_tier is correctly identified in every scenario.

    These tests are targeted specifically at the ``authoritative`` flag
    inside each :class:`~camel.policy.governance.TierEvaluationRecord` and
    the ``authoritative_tier`` on :class:`~camel.policy.governance.MergedPolicyResult`.
    """

    def test_platform_allow_authoritative_tier_none(self) -> None:
        """Platform-only allow: authoritative_tier=None, no record is authoritative."""
        merged = _resolver(platform=Allowed()).evaluate("tool", {})
        assert merged.authoritative_tier is None
        for r in merged.audit_trail:
            assert r.authoritative is False

    def test_platform_deny_authoritative_tier_platform(self) -> None:
        """Platform deny: authoritative_tier=PLATFORM, that record is authoritative."""
        merged = _resolver(platform=Denied("p")).evaluate("tool", {})
        assert merged.authoritative_tier is PolicyTier.PLATFORM
        assert merged.audit_trail[0].authoritative is True

    def test_tp_deny_after_platform_allow_authoritative_tier_tp(self) -> None:
        """TP deny after Platform allow: authoritative_tier=TOOL_PROVIDER."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="p_allow")
        reg.register_platform("tool", _allow, name="p_allow_2")
        reg.register_tool_provider("tool", _deny, name="tp_deny")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})

        assert merged.authoritative_tier is PolicyTier.TOOL_PROVIDER
        # Two Platform records, one TP record
        assert len(merged.audit_trail) == 3
        assert merged.audit_trail[0].authoritative is False
        assert merged.audit_trail[1].authoritative is False
        assert merged.audit_trail[2].authoritative is True
        assert merged.audit_trail[2].tier is PolicyTier.TOOL_PROVIDER

    def test_user_deny_authoritative_tier_user(self) -> None:
        """User deny after Platform + TP allow: authoritative_tier=USER."""
        merged = _resolver(
            platform=Allowed(),
            tool_provider=Allowed(),
            user=Denied("u"),
        ).evaluate("tool", {})
        assert merged.authoritative_tier is PolicyTier.USER
        # All three records present
        assert len(merged.audit_trail) == 3
        assert merged.audit_trail[2].tier is PolicyTier.USER
        assert merged.audit_trail[2].authoritative is True

    def test_all_allow_no_authoritative_record(self) -> None:
        """All-allow: no record in audit_trail has authoritative=True."""
        merged = _resolver(
            platform=Allowed(),
            tool_provider=Allowed(),
            user=Allowed(),
        ).evaluate("tool", {})
        assert all(not r.authoritative for r in merged.audit_trail)

    def test_first_deny_in_tier_short_circuits_tier(self) -> None:
        """Within a tier, first Denied short-circuits remaining policies in that tier."""
        reg = TieredPolicyRegistry()
        calls: list[str] = []

        def _p_allow(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            calls.append("p_allow")
            return Allowed()

        def _p_deny(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:
            calls.append("p_deny")
            return Denied("first_deny")

        def _p_should_not_run(
            tn: str, kw: Mapping[str, CaMeLValue]
        ) -> SecurityPolicyResult:  # pragma: no cover
            calls.append("p_should_not_run")
            return Allowed()

        _p_allow.__name__ = "p_allow"
        _p_deny.__name__ = "p_deny"
        _p_should_not_run.__name__ = "p_should_not_run"

        reg.register_platform("tool", _p_allow, name="p_allow")
        reg.register_platform("tool", _p_deny, name="p_deny")
        reg.register_platform("tool", _p_should_not_run, name="p_should_not_run")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})

        assert not merged.is_allowed
        assert merged.authoritative_tier is PolicyTier.PLATFORM
        # p_allow evaluated, p_deny evaluated, p_should_not_run skipped
        assert "p_allow" in calls
        assert "p_deny" in calls
        assert "p_should_not_run" not in calls
        # Audit trail has only two records (up to and including the denial)
        assert len(merged.audit_trail) == 2
        assert merged.audit_trail[1].policy_name == "p_deny"
        assert merged.audit_trail[1].authoritative is True

    def test_non_overridable_denial_audit_record_flags(self) -> None:
        """Non-overridable Platform deny: audit record has correct flags."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _deny, non_overridable=True, name="noo_deny")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})

        assert merged.non_overridable_denial is True
        record = merged.audit_trail[0]
        assert record.non_overridable is True
        assert record.authoritative is True
        assert record.tier is PolicyTier.PLATFORM
        assert record.policy_name == "noo_deny"

    def test_audit_trail_records_result_objects(self) -> None:
        """Each TierEvaluationRecord carries the actual result object."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow, name="p")
        reg.register_tool_provider("tool", _deny, name="tp")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})

        assert isinstance(merged.audit_trail[0].result, Allowed)
        assert isinstance(merged.audit_trail[1].result, Denied)

    def test_multiple_policies_same_tier_all_allow_none_authoritative(
        self,
    ) -> None:
        """Multiple policies in one tier all allowing: none is authoritative."""
        reg = TieredPolicyRegistry()
        reg.register_user("tool", _allow, name="u1")
        reg.register_user("tool", _allow, name="u2")
        reg.register_user("tool", _allow, name="u3")
        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("tool", {})

        assert merged.is_allowed
        assert all(not r.authoritative for r in merged.audit_trail)
        assert len(merged.audit_trail) == 3


# ---------------------------------------------------------------------------
# evaluate_flat() — backward-compatible drop-in
# ---------------------------------------------------------------------------


class TestEvaluateFlat:
    """evaluate_flat() provides a plain SecurityPolicyResult for legacy call sites."""

    def test_flat_returns_allowed_when_all_tiers_allow(self) -> None:
        """evaluate_flat() returns Allowed when the merged result is Allowed."""
        reg = TieredPolicyRegistry()
        reg.register_platform("tool", _allow)
        result = PolicyConflictResolver(reg).evaluate_flat("tool", {})
        assert isinstance(result, Allowed)

    def test_flat_returns_denied_when_any_tier_denies(self) -> None:
        """evaluate_flat() returns Denied when the merged result is Denied."""
        reg = TieredPolicyRegistry()
        reg.register_user("tool", _deny)
        result = PolicyConflictResolver(reg).evaluate_flat("tool", {})
        assert isinstance(result, Denied)

    def test_flat_empty_registry_returns_allowed(self) -> None:
        """evaluate_flat() on empty registry returns Allowed."""
        result = PolicyConflictResolver(TieredPolicyRegistry()).evaluate_flat("tool", {})
        assert isinstance(result, Allowed)


# ---------------------------------------------------------------------------
# Registration validation
# ---------------------------------------------------------------------------


class TestRegistrationValidation:
    """TieredPolicyRegistry enforces non_overridable constraints at registration."""

    def test_non_overridable_rejected_on_tool_provider(self) -> None:
        """non_overridable=True on Tool-Provider tier raises ValueError."""
        reg = TieredPolicyRegistry()
        with pytest.raises(ValueError, match="non_overridable"):
            reg.register("tool", _allow, PolicyTier.TOOL_PROVIDER, non_overridable=True)

    def test_non_overridable_rejected_on_user(self) -> None:
        """non_overridable=True on User tier raises ValueError."""
        reg = TieredPolicyRegistry()
        with pytest.raises(ValueError, match="non_overridable"):
            reg.register("tool", _allow, PolicyTier.USER, non_overridable=True)

    def test_non_callable_raises_type_error(self) -> None:
        """Non-callable policy_fn raises TypeError at registration."""
        reg = TieredPolicyRegistry()
        with pytest.raises(TypeError, match="callable"):
            reg.register("tool", "not_callable", PolicyTier.PLATFORM)  # type: ignore[arg-type]

    def test_register_returns_policy_fn(self) -> None:
        """register() returns the original policy_fn (decorator support)."""
        reg = TieredPolicyRegistry()
        returned = reg.register("tool", _allow, PolicyTier.PLATFORM)
        assert returned is _allow
