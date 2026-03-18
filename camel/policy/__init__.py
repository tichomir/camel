"""CaMeL Policy Engine.

This package provides the security policy engine for the CaMeL runtime.  It
implements the :class:`~camel.policy.interfaces.PolicyRegistry` and the
:class:`~camel.policy.interfaces.SecurityPolicyResult` sealed type, together
with the three helper functions used to author policy functions.

It also exports the three-tier governance API (ADR-011): the
:class:`~camel.policy.governance.TieredPolicyRegistry`,
:class:`~camel.policy.governance.PolicyConflictResolver`, and supporting
data types.

Public API
----------
:class:`SecurityPolicyResult`
    Sealed base type for policy evaluation outcomes.
:class:`Allowed`
    Variant of :class:`SecurityPolicyResult` — the tool call may proceed.
:class:`Denied`
    Variant of :class:`SecurityPolicyResult` — the tool call is blocked.
:data:`PolicyFn`
    Type alias for a policy function callable.
:class:`PolicyRegistry`
    Registry that stores and evaluates security policies per tool (flat model).
:func:`is_trusted`
    Return ``True`` if a :class:`~camel.value.CaMeLValue` originates
    exclusively from trusted sources.
:func:`can_readers_read_value`
    Return ``True`` if a given principal is an authorised reader of a value.
:func:`get_all_sources`
    Return the complete set of origin labels for a value.

Three-tier governance (ADR-011)
--------------------------------
:class:`PolicyTier`
    Enum of authorship tiers: ``PLATFORM``, ``TOOL_PROVIDER``, ``USER``.
:class:`TieredPolicyEntry`
    A policy function bundled with its tier metadata.
:class:`TierEvaluationRecord`
    One record in the :attr:`~MergedPolicyResult.audit_trail`.
:class:`MergedPolicyResult`
    The merged outcome returned by :meth:`~PolicyConflictResolver.evaluate`.
:class:`TieredPolicyRegistry`
    Storage layer for three-tier policy entries.
:class:`PolicyConflictResolver`
    Merges three tiers into a single authoritative result.
"""

from camel.policy.governance import (
    MergedPolicyResult,
    PolicyConflictResolver,
    PolicyTier,
    TieredPolicyEntry,
    TieredPolicyRegistry,
    TierEvaluationRecord,
)
from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyFn,
    PolicyRegistry,
    SecurityPolicyResult,
    can_readers_read_value,
    get_all_sources,
    is_trusted,
)

__all__ = [
    # Flat policy API (ADR-009)
    "SecurityPolicyResult",
    "Allowed",
    "Denied",
    "PolicyFn",
    "PolicyRegistry",
    "is_trusted",
    "can_readers_read_value",
    "get_all_sources",
    # Three-tier governance API (ADR-011)
    "PolicyTier",
    "TieredPolicyEntry",
    "TierEvaluationRecord",
    "MergedPolicyResult",
    "TieredPolicyRegistry",
    "PolicyConflictResolver",
]
