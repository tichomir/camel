"""CaMeL Three-Tier Policy Governance — multi-party policy authorship and conflict resolution.

This module implements the three-tier policy governance model described in
ADR-011.  It introduces explicit authorship tiers (Platform, Tool-Provider,
User) with strict precedence rules, a ``non_overridable`` flag for Platform
policies, and a deterministic :class:`PolicyConflictResolver` that merges all
three tiers into a single :class:`MergedPolicyResult` with a full audit trail.

Design document: ``docs/adr/011-three-tier-policy-governance.md``

Tier precedence
---------------
Evaluation always proceeds in this order:

1. **Platform** — highest authority; defines the organisational security floor.
2. **Tool-Provider** — mid-tier; tool authors constrain their own tool's usage.
3. **User** — lowest authority; personalises within guardrails set by higher
   tiers.

Within each tier, policies are evaluated in registration order.  The first
``Denied`` encountered short-circuits evaluation of the current tier **and all
remaining tiers**.

``non_overridable`` semantics
------------------------------
A Platform policy may carry ``non_overridable=True``.  When such a policy
returns ``Denied``:

* Lower-tier evaluation is skipped.
* The consent callback (ADR-010) is **not** invoked in PRODUCTION mode.
* :attr:`MergedPolicyResult.can_be_consented` returns ``False``.

A ``non_overridable`` Platform policy returning ``Allowed`` has no additional
effect — lower tiers can still impose ``Denied`` results (that strengthens the
posture, not weakens it).

The ``non_overridable`` flag is **only** accepted on Platform-tier entries; a
:exc:`ValueError` is raised at registration time if it is set on any other
tier.

Backward compatibility
----------------------
This module is additive.  The existing :class:`~camel.policy.interfaces.PolicyRegistry`
(ADR-009) is not modified.  :class:`PolicyConflictResolver` can be used as a
drop-in for ``PolicyRegistry`` via :meth:`PolicyConflictResolver.evaluate_flat`.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import final

from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyFn,
    SecurityPolicyResult,
)
from camel.value import CaMeLValue


# ---------------------------------------------------------------------------
# PolicyTier — authorship tier enum
# ---------------------------------------------------------------------------


class PolicyTier(Enum):
    """Authorship tier for a security policy.

    Members are evaluated in descending precedence order:
    ``PLATFORM`` → ``TOOL_PROVIDER`` → ``USER``.

    Members
    -------
    PLATFORM:
        Highest authority.  Defines the organisational security floor.  Only
        Platform-tier policies may carry the ``non_overridable`` flag.
    TOOL_PROVIDER:
        Mid-tier.  Tool authors constrain their own tool's usage.  Cannot set
        ``non_overridable``.
    USER:
        Lowest authority.  End-user personalisation within platform guardrails.
        Cannot set ``non_overridable``.
    """

    PLATFORM = "Platform"
    TOOL_PROVIDER = "ToolProvider"
    USER = "User"


# ---------------------------------------------------------------------------
# TieredPolicyEntry — wraps a policy function with its tier metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TieredPolicyEntry:
    """A policy function bundled with its tier metadata.

    Attributes
    ----------
    policy_fn:
        The :data:`~camel.policy.interfaces.PolicyFn` to evaluate.
    tier:
        The :class:`PolicyTier` this entry belongs to.
    non_overridable:
        When ``True`` (only valid for ``PLATFORM`` tier), a ``Denied`` result
        from this entry cannot be overridden by the consent callback and skips
        all lower-tier evaluation.
    name:
        Human-readable name for this policy entry, used in audit trail records.
        Defaults to ``policy_fn.__name__``.
    """

    policy_fn: PolicyFn
    tier: PolicyTier
    non_overridable: bool
    name: str


# ---------------------------------------------------------------------------
# TierEvaluationRecord — one entry in the MergedPolicyResult audit trail
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierEvaluationRecord:
    """Record of evaluating a single policy, for use in the audit trail.

    One :class:`TierEvaluationRecord` is created for each policy evaluated
    during :meth:`PolicyConflictResolver.evaluate`.  Policies in tiers that
    were skipped due to a higher-tier short-circuit are **not** included.

    Attributes
    ----------
    tier:
        The :class:`PolicyTier` this policy belongs to.
    policy_name:
        The human-readable name of the evaluated policy.
    result:
        The :class:`~camel.policy.interfaces.SecurityPolicyResult` returned
        by the policy function.
    non_overridable:
        Whether this entry carried the ``non_overridable`` flag.  Always
        ``False`` for non-Platform tiers.
    authoritative:
        ``True`` when this record is the entry that determined the final
        outcome (i.e. it is the first ``Denied`` encountered, or all policies
        returned ``Allowed`` and this is the last one evaluated).
    """

    tier: PolicyTier
    policy_name: str
    result: SecurityPolicyResult
    non_overridable: bool
    authoritative: bool = False


# ---------------------------------------------------------------------------
# MergedPolicyResult — output of PolicyConflictResolver.evaluate()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergedPolicyResult:
    """The merged outcome produced by :meth:`PolicyConflictResolver.evaluate`.

    Attributes
    ----------
    outcome:
        The authoritative :class:`~camel.policy.interfaces.SecurityPolicyResult`
        for the tool call.
    authoritative_tier:
        The :class:`PolicyTier` whose policy determined ``outcome``, or
        ``None`` when no policies were registered for any tier (implicit
        ``Allowed``).
    non_overridable_denial:
        ``True`` only when a Platform policy with ``non_overridable=True``
        returned ``Denied``.  When ``True``, the consent callback must **not**
        be invoked.
    audit_trail:
        Ordered tuple of :class:`TierEvaluationRecord` for every policy that
        was evaluated.  Policies in tiers skipped by a higher-tier
        short-circuit are not included.

    Examples
    --------
    ::

        resolver = PolicyConflictResolver(registry)
        merged = resolver.evaluate("send_email", kwargs)
        if merged.is_allowed:
            call_tool()
        elif merged.can_be_consented:
            ask_user()
        else:
            raise PolicyViolationError(...)
    """

    outcome: SecurityPolicyResult
    authoritative_tier: PolicyTier | None
    non_overridable_denial: bool
    audit_trail: tuple[TierEvaluationRecord, ...]

    @property
    def is_allowed(self) -> bool:
        """Return ``True`` when the merged outcome permits the tool call."""
        return self.outcome.is_allowed()

    @property
    def can_be_consented(self) -> bool:
        """Return ``False`` when the denial is absolute (non-overridable).

        The consent callback (ADR-010) **must not** be invoked when this
        property returns ``False``.
        """
        return not self.non_overridable_denial


# ---------------------------------------------------------------------------
# TieredPolicyRegistry — storage layer for three-tier policy entries
# ---------------------------------------------------------------------------


class TieredPolicyRegistry:
    """Storage layer for three-tier policy entries.

    Stores :class:`TieredPolicyEntry` objects keyed by ``(tier, tool_name)``.
    Does not evaluate policies — evaluation is delegated to
    :class:`PolicyConflictResolver`.

    Registration
    ------------
    Use :meth:`register` for full control, or the convenience wrappers
    :meth:`register_platform`, :meth:`register_tool_provider`, and
    :meth:`register_user`.

    All ``register*`` methods return the ``policy_fn`` unchanged, enabling
    decorator usage::

        registry = TieredPolicyRegistry()

        @registry.register_platform("send_email", non_overridable=True)
        def no_external_recipients(tool_name, kwargs):
            ...

    ``non_overridable`` validation
    --------------------------------
    :exc:`ValueError` is raised at registration time if ``non_overridable=True``
    is supplied for a non-Platform tier.  This surfaces misconfiguration
    immediately, before any tool call is made.

    Examples
    --------
    ::

        reg = TieredPolicyRegistry()
        reg.register_platform("send_email", email_platform_policy, non_overridable=True)
        reg.register_tool_provider("send_email", email_tp_policy)
        reg.register_user("send_email", email_user_policy)
        entries = reg.get_entries("send_email", PolicyTier.PLATFORM)
    """

    def __init__(self) -> None:
        """Initialise an empty tiered policy registry."""
        # _entries[(tier, tool_name)] = ordered list of TieredPolicyEntry
        self._entries: dict[tuple[PolicyTier, str], list[TieredPolicyEntry]] = {}

    def register(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        tier: PolicyTier,
        *,
        non_overridable: bool = False,
        name: str = "",
    ) -> PolicyFn:
        """Register a policy function for a tool in the given tier.

        Parameters
        ----------
        tool_name:
            The registered tool name.
        policy_fn:
            A callable conforming to :data:`~camel.policy.interfaces.PolicyFn`.
        tier:
            The :class:`PolicyTier` for this policy.
        non_overridable:
            When ``True``, a ``Denied`` result cannot be overridden by the
            consent callback.  **Only valid for** ``PolicyTier.PLATFORM``.
        name:
            Human-readable label for audit trail records.  Defaults to
            ``policy_fn.__name__``.

        Returns
        -------
        PolicyFn
            ``policy_fn`` unchanged (enables decorator usage).

        Raises
        ------
        TypeError
            If ``policy_fn`` is not callable.
        ValueError
            If ``non_overridable=True`` is used with a non-Platform tier.
        """
        if not callable(policy_fn):
            raise TypeError(
                f"policy_fn must be callable, got {type(policy_fn).__name__!r}"
            )
        if non_overridable and tier is not PolicyTier.PLATFORM:
            raise ValueError(
                f"non_overridable=True is only valid for PolicyTier.PLATFORM; "
                f"got tier={tier!r}"
            )
        entry_name = name if name else getattr(policy_fn, "__name__", repr(policy_fn))
        entry = TieredPolicyEntry(
            policy_fn=policy_fn,
            tier=tier,
            non_overridable=non_overridable,
            name=entry_name,
        )
        key = (tier, tool_name)
        if key not in self._entries:
            self._entries[key] = []
        self._entries[key].append(entry)
        return policy_fn

    def register_platform(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        non_overridable: bool = False,
        name: str = "",
    ) -> PolicyFn:
        """Register a Platform-tier policy for a tool.

        Convenience wrapper around :meth:`register` with
        ``tier=PolicyTier.PLATFORM``.

        Parameters
        ----------
        tool_name:
            The registered tool name.
        policy_fn:
            A callable conforming to :data:`~camel.policy.interfaces.PolicyFn`.
        non_overridable:
            When ``True``, a ``Denied`` result is absolute and cannot be
            overridden by the consent callback.
        name:
            Optional human-readable label for audit records.

        Returns
        -------
        PolicyFn
            ``policy_fn`` unchanged (enables decorator usage).
        """
        return self.register(
            tool_name,
            policy_fn,
            PolicyTier.PLATFORM,
            non_overridable=non_overridable,
            name=name,
        )

    def register_tool_provider(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        name: str = "",
    ) -> PolicyFn:
        """Register a Tool-Provider-tier policy for a tool.

        Convenience wrapper around :meth:`register` with
        ``tier=PolicyTier.TOOL_PROVIDER``.

        Parameters
        ----------
        tool_name:
            The registered tool name.
        policy_fn:
            A callable conforming to :data:`~camel.policy.interfaces.PolicyFn`.
        name:
            Optional human-readable label for audit records.

        Returns
        -------
        PolicyFn
            ``policy_fn`` unchanged (enables decorator usage).
        """
        return self.register(
            tool_name, policy_fn, PolicyTier.TOOL_PROVIDER, name=name
        )

    def register_user(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        name: str = "",
    ) -> PolicyFn:
        """Register a User-tier policy for a tool.

        Convenience wrapper around :meth:`register` with
        ``tier=PolicyTier.USER``.

        Parameters
        ----------
        tool_name:
            The registered tool name.
        policy_fn:
            A callable conforming to :data:`~camel.policy.interfaces.PolicyFn`.
        name:
            Optional human-readable label for audit records.

        Returns
        -------
        PolicyFn
            ``policy_fn`` unchanged (enables decorator usage).
        """
        return self.register(tool_name, policy_fn, PolicyTier.USER, name=name)

    def get_entries(
        self,
        tool_name: str,
        tier: PolicyTier,
    ) -> list[TieredPolicyEntry]:
        """Return all entries for the given tool name and tier, in registration order.

        Parameters
        ----------
        tool_name:
            The tool name to query.
        tier:
            The :class:`PolicyTier` to query.

        Returns
        -------
        list[TieredPolicyEntry]
            Ordered list of entries; empty list if none are registered.
        """
        return list(self._entries.get((tier, tool_name), []))

    def registered_tools(
        self,
        tier: PolicyTier | None = None,
    ) -> frozenset[str]:
        """Return tool names that have at least one registered policy.

        Parameters
        ----------
        tier:
            When given, restrict to tools registered under that tier.  When
            ``None``, return the union across all tiers.

        Returns
        -------
        frozenset[str]
            Set of tool names with at least one registered policy entry.
        """
        if tier is None:
            return frozenset(tool for (_, tool) in self._entries.keys())
        return frozenset(
            tool for (t, tool) in self._entries.keys() if t is tier
        )

    @classmethod
    def load_from_env(cls) -> "TieredPolicyRegistry":
        """Create a registry pre-populated from a deployment-specific module.

        Reads the ``CAMEL_TIERED_POLICY_MODULE`` environment variable.  If set,
        it must be a dotted Python module path.  That module is imported and its
        ``configure_tiered_policies(registry: TieredPolicyRegistry) -> None``
        function is called with the fresh registry instance.

        Returns
        -------
        TieredPolicyRegistry
            A newly created registry populated by the configuration module, or
            an empty registry if ``CAMEL_TIERED_POLICY_MODULE`` is unset.

        Raises
        ------
        ImportError
            If the module specified by ``CAMEL_TIERED_POLICY_MODULE`` cannot be
            imported.
        AttributeError
            If the module does not define a ``configure_tiered_policies``
            callable.
        """
        registry = cls()
        module_path = os.environ.get("CAMEL_TIERED_POLICY_MODULE", "").strip()
        if not module_path:
            return registry
        module = importlib.import_module(module_path)
        configure = getattr(module, "configure_tiered_policies")
        if not callable(configure):
            raise AttributeError(
                f"'{module_path}.configure_tiered_policies' is not callable"
            )
        configure(registry)
        return registry


# ---------------------------------------------------------------------------
# PolicyConflictResolver — the merging engine
# ---------------------------------------------------------------------------


@final
class PolicyConflictResolver:
    """Merges three-tier policies into a single authoritative result.

    Wraps a :class:`TieredPolicyRegistry` and implements the deterministic
    conflict resolution algorithm defined in ADR-011 §Decision 4.

    Evaluation order
    ----------------
    ``Platform`` → ``Tool-Provider`` → ``User``.  Within each tier, policies
    are evaluated in registration order.  The first ``Denied`` in any tier
    terminates evaluation of that tier and all lower tiers.

    ``non_overridable`` handling
    ----------------------------
    A non-overridable Platform ``Denied`` halts all evaluation immediately and
    sets :attr:`MergedPolicyResult.non_overridable_denial` to ``True``,
    signalling that the consent callback must not be invoked.

    Backward-compatible drop-in
    ---------------------------
    :meth:`evaluate_flat` returns a plain
    :class:`~camel.policy.interfaces.SecurityPolicyResult` and can be used as
    a drop-in replacement for :meth:`~camel.policy.interfaces.PolicyRegistry.evaluate`
    at existing call sites.

    Examples
    --------
    ::

        reg = TieredPolicyRegistry()
        reg.register_platform("send_email", platform_policy, non_overridable=True)
        reg.register_tool_provider("send_email", tp_policy)
        reg.register_user("send_email", user_policy)

        resolver = PolicyConflictResolver(reg)
        merged = resolver.evaluate("send_email", kwargs)
        print(merged.is_allowed, merged.non_overridable_denial)
        for record in merged.audit_trail:
            print(record.tier.value, record.policy_name, record.result)
    """

    def __init__(self, registry: TieredPolicyRegistry) -> None:
        """Initialise the resolver with a :class:`TieredPolicyRegistry`.

        Parameters
        ----------
        registry:
            The :class:`TieredPolicyRegistry` that stores the three-tier policy
            entries.
        """
        self._registry = registry

    def evaluate(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> MergedPolicyResult:
        """Run all registered policies for ``tool_name`` across all three tiers.

        Implements the conflict resolution algorithm from ADR-011 §4.1:

        1. Evaluate all Platform policies in registration order.
        2. If any Platform policy returns ``Denied``, return immediately with
           ``authoritative_tier=PLATFORM`` and
           ``non_overridable_denial=entry.non_overridable``.
        3. Evaluate all Tool-Provider policies in registration order.
        4. If any Tool-Provider policy returns ``Denied``, return immediately
           with ``authoritative_tier=TOOL_PROVIDER``.
        5. Evaluate all User policies in registration order.
        6. If any User policy returns ``Denied``, return immediately with
           ``authoritative_tier=USER``.
        7. If all tiers are ``Allowed`` (or empty), return
           ``authoritative_tier=None``.

        Parameters
        ----------
        tool_name:
            The name of the tool about to be called.
        kwargs:
            Argument mapping (name → :class:`~camel.value.CaMeLValue`).
            Must not be mutated by policy functions.

        Returns
        -------
        MergedPolicyResult
            The merged outcome with audit trail.  Never raises; exceptions from
            policy functions propagate to the caller unchanged.
        """
        audit_trail: list[TierEvaluationRecord] = []

        # --- Phase 1: Platform tier ---
        platform_entries = self._registry.get_entries(
            tool_name, PolicyTier.PLATFORM
        )
        for entry in platform_entries:
            result = entry.policy_fn(tool_name, kwargs)
            if isinstance(result, Denied):
                audit_trail.append(
                    TierEvaluationRecord(
                        tier=PolicyTier.PLATFORM,
                        policy_name=entry.name,
                        result=result,
                        non_overridable=entry.non_overridable,
                        authoritative=True,
                    )
                )
                return MergedPolicyResult(
                    outcome=result,
                    authoritative_tier=PolicyTier.PLATFORM,
                    non_overridable_denial=entry.non_overridable,
                    audit_trail=tuple(audit_trail),
                )
            audit_trail.append(
                TierEvaluationRecord(
                    tier=PolicyTier.PLATFORM,
                    policy_name=entry.name,
                    result=result,
                    non_overridable=entry.non_overridable,
                    authoritative=False,
                )
            )

        # --- Phase 2: Tool-Provider tier ---
        tp_entries = self._registry.get_entries(
            tool_name, PolicyTier.TOOL_PROVIDER
        )
        for entry in tp_entries:
            result = entry.policy_fn(tool_name, kwargs)
            if isinstance(result, Denied):
                audit_trail.append(
                    TierEvaluationRecord(
                        tier=PolicyTier.TOOL_PROVIDER,
                        policy_name=entry.name,
                        result=result,
                        non_overridable=False,
                        authoritative=True,
                    )
                )
                return MergedPolicyResult(
                    outcome=result,
                    authoritative_tier=PolicyTier.TOOL_PROVIDER,
                    non_overridable_denial=False,
                    audit_trail=tuple(audit_trail),
                )
            audit_trail.append(
                TierEvaluationRecord(
                    tier=PolicyTier.TOOL_PROVIDER,
                    policy_name=entry.name,
                    result=result,
                    non_overridable=False,
                    authoritative=False,
                )
            )

        # --- Phase 3: User tier ---
        user_entries = self._registry.get_entries(tool_name, PolicyTier.USER)
        for entry in user_entries:
            result = entry.policy_fn(tool_name, kwargs)
            if isinstance(result, Denied):
                audit_trail.append(
                    TierEvaluationRecord(
                        tier=PolicyTier.USER,
                        policy_name=entry.name,
                        result=result,
                        non_overridable=False,
                        authoritative=True,
                    )
                )
                return MergedPolicyResult(
                    outcome=result,
                    authoritative_tier=PolicyTier.USER,
                    non_overridable_denial=False,
                    audit_trail=tuple(audit_trail),
                )
            audit_trail.append(
                TierEvaluationRecord(
                    tier=PolicyTier.USER,
                    policy_name=entry.name,
                    result=result,
                    non_overridable=False,
                    authoritative=False,
                )
            )

        # --- Phase 4: All allowed ---
        return MergedPolicyResult(
            outcome=Allowed(),
            authoritative_tier=None,
            non_overridable_denial=False,
            audit_trail=tuple(audit_trail),
        )

    def evaluate_flat(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        """Return only the :class:`~camel.policy.interfaces.SecurityPolicyResult`.

        Convenience wrapper equivalent to ``self.evaluate(...).outcome``.
        Allows :class:`PolicyConflictResolver` to be used as a drop-in for
        :meth:`~camel.policy.interfaces.PolicyRegistry.evaluate` at existing
        call sites.

        Parameters
        ----------
        tool_name:
            The name of the tool about to be called.
        kwargs:
            Argument mapping (name → :class:`~camel.value.CaMeLValue`).

        Returns
        -------
        SecurityPolicyResult
            :class:`~camel.policy.interfaces.Allowed` or
            :class:`~camel.policy.interfaces.Denied`.
        """
        return self.evaluate(tool_name, kwargs).outcome

    @classmethod
    def load_from_env(cls) -> "PolicyConflictResolver":
        """Create a resolver pre-populated from a deployment-specific module.

        Reads ``CAMEL_TIERED_POLICY_MODULE`` via
        :meth:`TieredPolicyRegistry.load_from_env` and wraps the resulting
        registry in a new :class:`PolicyConflictResolver`.

        Returns
        -------
        PolicyConflictResolver
            A resolver backed by the environment-configured registry.
        """
        registry = TieredPolicyRegistry.load_from_env()
        return cls(registry)


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "PolicyTier",
    "TieredPolicyEntry",
    "TierEvaluationRecord",
    "MergedPolicyResult",
    "TieredPolicyRegistry",
    "PolicyConflictResolver",
]

