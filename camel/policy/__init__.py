"""CaMeL Policy Engine.

This package provides the security policy engine for the CaMeL runtime.  It
implements the :class:`~camel.policy.interfaces.PolicyRegistry` and the
:class:`~camel.policy.interfaces.SecurityPolicyResult` sealed type, together
with the three helper functions used to author policy functions.

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
    Registry that stores and evaluates security policies per tool.
:func:`is_trusted`
    Return ``True`` if a :class:`~camel.value.CaMeLValue` originates
    exclusively from trusted sources.
:func:`can_readers_read_value`
    Return ``True`` if a given principal is an authorised reader of a value.
:func:`get_all_sources`
    Return the complete set of origin labels for a value.
"""

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
    "SecurityPolicyResult",
    "Allowed",
    "Denied",
    "PolicyFn",
    "PolicyRegistry",
    "is_trusted",
    "can_readers_read_value",
    "get_all_sources",
]
