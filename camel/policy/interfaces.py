"""CaMeL Policy Engine — types, interfaces, and helper functions.

This module is the authoritative source for the CaMeL security policy API.
It defines the :class:`SecurityPolicyResult` sealed type, the
:class:`PolicyRegistry` class, the :data:`PolicyFn` type alias, and the
three standard helper functions used to author security policies.

Architecture overview
---------------------
The policy engine is the *enforcement layer* that sits between the CaMeL
interpreter and every tool call.  Before executing a tool, the interpreter
calls :meth:`PolicyRegistry.evaluate`, passing the tool name and a mapping
of argument names to :class:`~camel.value.CaMeLValue` objects.  The registry
evaluates all registered policies for that tool in registration order and
returns either :class:`Allowed` or :class:`Denied`.  If any policy returns
:class:`Denied`, execution is blocked and a ``PolicyViolationError`` is raised.

Design principles
-----------------
* **Synchronous and deterministic** — policy functions are plain synchronous
  callables.  No ``async`` code, no LLM calls, no I/O.  (NFR-2)
* **First-Denied short-circuits** — as soon as one registered policy for a
  tool returns :class:`Denied`, no further policies are evaluated and
  :class:`Denied` is returned immediately.  All policies must agree to
  allow a call.
* **Read-only access to capabilities** — policies receive the full
  :class:`~camel.value.CaMeLValue` for each argument; they must not mutate
  these values.
* **Configuration-driven** — deployment-specific policies are loaded by
  pointing the ``CAMEL_POLICY_MODULE`` environment variable at a Python
  dotted module path.  That module must expose a
  ``configure_policies(registry: PolicyRegistry) -> None`` callable.  Core
  code requires no modification to change the active policy set.

SecurityPolicyResult sealed type
---------------------------------
:class:`SecurityPolicyResult` is the abstract base.  Only two concrete
subclasses exist:

* :class:`Allowed` — zero-argument; signals that the tool call may proceed.
* :class:`Denied` — carries a human-readable ``reason: str`` string.

No other subclasses may be created outside this module.  Type checkers that
support exhaustiveness (e.g. mypy with ``assert_never``) will flag unhandled
variants.

PolicyFn type alias
-------------------
A *policy function* has the signature::

    def my_policy(
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        ...

It receives the name of the tool being invoked and the full argument mapping
(with capability metadata intact) and returns :class:`Allowed` or
:class:`Denied`.

PolicyRegistry
--------------
::

    from camel.policy import PolicyRegistry, Allowed, Denied

    registry = PolicyRegistry()

    @registry.register("send_email")
    def no_exfiltration(tool_name, kwargs):
        recipient = kwargs.get("to")
        if recipient is None:
            return Allowed()
        # Block if recipient address came from an untrusted source
        if not is_trusted(recipient):
            return Denied("recipient address originates from untrusted data")
        return Allowed()

    result = registry.evaluate("send_email", {"to": some_camel_value})

Helper functions
----------------
:func:`is_trusted`
    Check whether a :class:`~camel.value.CaMeLValue` originates exclusively
    from the trusted source labels ``"User literal"`` and ``"CaMeL"``.

:func:`can_readers_read_value`
    Check whether a given principal string is an authorised reader of a
    :class:`~camel.value.CaMeLValue`.

:func:`get_all_sources`
    Return the complete ``frozenset[str]`` of origin labels recorded on a
    :class:`~camel.value.CaMeLValue`.

Configuration-driven loading
-----------------------------
Set the ``CAMEL_POLICY_MODULE`` environment variable to a dotted Python
module path, e.g.::

    CAMEL_POLICY_MODULE=myapp.security.policies

That module must export::

    def configure_policies(registry: PolicyRegistry) -> None:
        registry.register("send_email", my_email_policy)
        registry.register("write_file", my_file_policy)
        ...

Then call::

    registry = PolicyRegistry.load_from_env()

to get a registry pre-populated with all deployment-specific policies.  If
the environment variable is not set, an empty registry is returned (no
policies registered — all tool calls are implicitly allowed).
"""

from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Final, final

from camel.value import CaMeLValue, Public

# ---------------------------------------------------------------------------
# Trusted-source labels
# ---------------------------------------------------------------------------

#: The set of source labels that are considered *inherently trusted*.
#: Values whose ``sources`` field is a subset of this set are deemed trusted.
TRUSTED_SOURCE_LABELS: Final[frozenset[str]] = frozenset({"User literal", "CaMeL"})

# ---------------------------------------------------------------------------
# SecurityPolicyResult — sealed type
# ---------------------------------------------------------------------------


class SecurityPolicyResult(ABC):
    """Abstract base for security policy evaluation outcomes.

    This is a sealed type.  The only valid concrete instances are
    :class:`Allowed` and :class:`Denied`.  Do not subclass this class
    outside of this module.

    Use :func:`isinstance` checks or pattern matching to handle variants::

        result = registry.evaluate("send_email", kwargs)
        if isinstance(result, Allowed):
            proceed()
        elif isinstance(result, Denied):
            raise PolicyViolationError(result.reason)

    Or with ``typing.assert_never`` for exhaustiveness::

        from typing import assert_never

        match result:
            case Allowed():
                ...
            case Denied(reason=r):
                ...
            case _ as unreachable:
                assert_never(unreachable)
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Prevent subclassing outside this module."""
        # Allow only the two concrete subclasses defined here.
        allowed_subclasses = {"Allowed", "Denied"}
        if cls.__name__ not in allowed_subclasses or cls.__module__ != __name__:
            raise TypeError(
                f"Cannot subclass SecurityPolicyResult outside of "
                f"camel.policy.interfaces.  Got: {cls!r}"
            )
        super().__init_subclass__(**kwargs)

    @abstractmethod
    def is_allowed(self) -> bool:
        """Return ``True`` if this result permits the tool call to proceed."""


@final
class Allowed(SecurityPolicyResult):
    """Policy result indicating the tool call is permitted.

    This is the affirmative variant of :class:`SecurityPolicyResult`.  All
    registered policies for a tool must return :class:`Allowed` for the
    tool call to proceed.

    Examples
    --------
    ::

        def permissive_policy(
            tool_name: str,
            kwargs: Mapping[str, CaMeLValue],
        ) -> SecurityPolicyResult:
            return Allowed()
    """

    def is_allowed(self) -> bool:
        """Return ``True``."""
        return True

    def __repr__(self) -> str:
        """Return string representation of Allowed."""
        return "Allowed()"

    def __eq__(self, other: object) -> bool:
        """Two Allowed instances are always equal."""
        return isinstance(other, Allowed)

    def __hash__(self) -> int:
        """Hash for Allowed."""
        return hash("Allowed")


@final
class Denied(SecurityPolicyResult):
    """Policy result indicating the tool call is blocked.

    This is the negative variant of :class:`SecurityPolicyResult`.  If any
    registered policy for a tool returns :class:`Denied`, the tool call is
    blocked immediately and the ``reason`` is surfaced to the caller.

    Parameters
    ----------
    reason:
        A human-readable explanation of why the tool call was denied.
        This string is safe to include in ``PolicyViolationError`` messages
        and in security audit logs.

    Examples
    --------
    ::

        def no_external_recipients(
            tool_name: str,
            kwargs: Mapping[str, CaMeLValue],
        ) -> SecurityPolicyResult:
            to_addr = kwargs.get("to")
            if to_addr is not None and not is_trusted(to_addr):
                return Denied(
                    "recipient address comes from untrusted data — "
                    "possible data exfiltration attempt"
                )
            return Allowed()
    """

    def __init__(self, reason: str) -> None:
        """Initialise a Denied result with a reason string."""
        self._reason = reason

    @property
    def reason(self) -> str:
        """Human-readable reason the tool call was denied."""
        return self._reason

    def is_allowed(self) -> bool:
        """Return ``False``."""
        return False

    def __repr__(self) -> str:
        """Return string representation of Denied."""
        return f"Denied(reason={self._reason!r})"

    def __eq__(self, other: object) -> bool:
        """Two Denied instances are equal if their reasons match."""
        return isinstance(other, Denied) and self._reason == other._reason

    def __hash__(self) -> int:
        """Hash for Denied based on reason."""
        return hash(("Denied", self._reason))


# ---------------------------------------------------------------------------
# PolicyFn — type alias
# ---------------------------------------------------------------------------

#: Type alias for a *policy function*.
#:
#: A policy function is a synchronous, deterministic callable with the
#: signature::
#:
#:     (tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult
#:
#: Policy functions must:
#:
#: * Be **pure** — no side effects, no I/O, no LLM calls.
#: * Be **synchronous** — ``async`` policy functions are not supported.
#: * Return either :class:`Allowed` or :class:`Denied`.
#: * Not mutate the ``kwargs`` mapping or any :class:`~camel.value.CaMeLValue`
#:   therein.
#:
#: Registering a policy
#: --------------------
#: ::
#:
#:     from camel.policy import PolicyRegistry, PolicyFn, Allowed, Denied
#:     from camel.value import CaMeLValue
#:     from collections.abc import Mapping
#:
#:     def my_policy(
#:         tool_name: str,
#:         kwargs: Mapping[str, CaMeLValue],
#:     ) -> SecurityPolicyResult:
#:         return Allowed()
#:
#:     registry = PolicyRegistry()
#:     registry.register("my_tool", my_policy)
PolicyFn = Callable[[str, Mapping[str, CaMeLValue]], SecurityPolicyResult]

# ---------------------------------------------------------------------------
# PolicyRegistry
# ---------------------------------------------------------------------------


class PolicyRegistry:
    """Registry that stores and evaluates security policies per tool.

    Each tool may have zero or more policies registered against it.  When the
    interpreter is about to execute a tool call, it invokes
    :meth:`evaluate`, which runs all registered policies in registration
    order and returns the first :class:`Denied` encountered, or
    :class:`Allowed` if all policies agree (or none are registered).

    This class is designed to be injected into the
    :class:`~camel.interpreter.CaMeLInterpreter` and called synchronously
    before every tool dispatch.

    Multi-policy composition
    ------------------------
    All registered policies for a tool are *required* to return
    :class:`Allowed` for the tool call to proceed.  The first :class:`Denied`
    short-circuits evaluation; remaining policies are **not** called.  This
    "all-must-allow" semantics ensures that adding a stricter policy never
    weakens the overall security posture.

    Configuration-driven loading
    ----------------------------
    See :meth:`load_from_env` for how to populate a registry from a
    deployment-specific module without modifying core code.

    Examples
    --------
    ::

        registry = PolicyRegistry()
        registry.register("send_email", email_recipient_policy)
        registry.register("send_email", email_content_policy)

        result = registry.evaluate("send_email", kwargs)
        # result is Allowed() only if both policies agree

    ::

        # Decorator-style registration
        @registry.register("write_file")
        def my_write_policy(tool_name, kwargs):
            ...
    """

    def __init__(self) -> None:
        """Initialise an empty policy registry."""
        self._policies: dict[str, list[PolicyFn]] = {}

    def register(self, tool_name: str, policy_fn: PolicyFn) -> PolicyFn:
        """Register a policy function for a named tool.

        Multiple policies may be registered for the same tool; they are
        evaluated in registration order.  This method also works as a
        decorator, returning the policy function unchanged so it can be
        used directly::

            @registry.register("send_email")
            def no_external_recipients(tool_name, kwargs):
                ...

        Parameters
        ----------
        tool_name:
            The registered tool name (must match the name used when the tool
            was registered in :class:`~camel.tools.ToolRegistry`).
        policy_fn:
            A callable conforming to :data:`PolicyFn`.

        Returns
        -------
        PolicyFn
            The ``policy_fn`` argument unchanged (enables decorator usage).

        Raises
        ------
        TypeError
            If ``policy_fn`` is not callable.
        """
        if not callable(policy_fn):
            raise TypeError(f"policy_fn must be callable, got {type(policy_fn).__name__!r}")
        if tool_name not in self._policies:
            self._policies[tool_name] = []
        self._policies[tool_name].append(policy_fn)
        return policy_fn

    def evaluate(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        """Evaluate all registered policies for a tool call.

        Policies are evaluated in registration order.  The first
        :class:`Denied` result short-circuits evaluation; remaining policies
        are not called.  If no policies are registered for ``tool_name``,
        :class:`Allowed` is returned (implicit allow).

        Parameters
        ----------
        tool_name:
            The name of the tool about to be called.
        kwargs:
            A mapping from argument names to their
            :class:`~camel.value.CaMeLValue` wrappers as they would be
            passed to the tool.  This mapping must not be mutated.

        Returns
        -------
        SecurityPolicyResult
            :class:`Allowed` if all registered policies agree (or none are
            registered).  The first :class:`Denied` encountered otherwise.

        Notes
        -----
        This method is **synchronous and deterministic** — it contains no
        I/O, no LLM calls, and no non-deterministic operations.  (NFR-2)
        """
        policies = self._policies.get(tool_name, [])
        for policy_fn in policies:
            result = policy_fn(tool_name, kwargs)
            if not result.is_allowed():
                return result
        return Allowed()

    def _evaluate_and_get_policy_name(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> tuple[SecurityPolicyResult, str]:
        """Evaluate policies and return (result, denying_policy_function_name).

        Like :meth:`evaluate` but additionally returns the ``__name__`` of the
        first policy function that returned :class:`Denied`, or an empty string
        when all policies agree (or none are registered).

        Used internally by the interpreter to populate
        :attr:`~camel.interpreter.PolicyViolationError.policy_name` so that
        :class:`~camel_security.agent.PolicyDenialRecord` carries the actual
        function name rather than a fallback tool-name string.

        Parameters
        ----------
        tool_name:
            The name of the tool about to be called.
        kwargs:
            A mapping from argument names to their
            :class:`~camel.value.CaMeLValue` wrappers.

        Returns
        -------
        tuple[SecurityPolicyResult, str]
            ``(result, policy_fn_name)`` where ``policy_fn_name`` is the
            ``__name__`` of the denying function, or ``""`` when allowed.
        """
        policies = self._policies.get(tool_name, [])
        for policy_fn in policies:
            result = policy_fn(tool_name, kwargs)
            if not result.is_allowed():
                return result, getattr(policy_fn, "__name__", "")
        return Allowed(), ""

    def registered_tools(self) -> frozenset[str]:
        """Return the set of tool names that have at least one policy.

        Returns
        -------
        frozenset[str]
            Names of tools with registered policies.
        """
        return frozenset(self._policies.keys())

    def policy_count(self, tool_name: str) -> int:
        """Return the number of policies registered for a tool.

        Parameters
        ----------
        tool_name:
            The tool name to query.

        Returns
        -------
        int
            Number of registered policies (0 if none registered).
        """
        return len(self._policies.get(tool_name, []))

    @classmethod
    def load_from_env(cls) -> PolicyRegistry:
        """Create a registry pre-populated from a deployment-specific module.

        Reads the ``CAMEL_POLICY_MODULE`` environment variable.  If set, it
        must be a dotted Python module path (e.g.
        ``"myapp.security.policies"``).  That module is imported and its
        ``configure_policies(registry: PolicyRegistry) -> None`` function is
        called with the fresh registry instance.

        If the environment variable is not set, an empty registry is returned
        (all tool calls are implicitly allowed).

        Contract for the configuration module
        --------------------------------------
        The module pointed to by ``CAMEL_POLICY_MODULE`` must export::

            def configure_policies(registry: PolicyRegistry) -> None:
                registry.register("send_email", email_policy)
                registry.register("write_file", file_policy)
                ...

        The function must not perform I/O-bound work (it is called at
        initialisation time), must not raise exceptions other than
        ``ImportError`` or ``AttributeError``, and must not store a
        reference to the registry beyond the duration of the call.

        Returns
        -------
        PolicyRegistry
            A newly created registry populated by the configuration module,
            or an empty registry if ``CAMEL_POLICY_MODULE`` is unset.

        Raises
        ------
        ImportError
            If the module specified by ``CAMEL_POLICY_MODULE`` cannot be
            imported.
        AttributeError
            If the module does not define a ``configure_policies`` callable.

        Examples
        --------
        ::

            # In myapp/security/policies.py:
            from camel.policy import PolicyRegistry, Allowed, Denied
            from collections.abc import Mapping
            from camel.value import CaMeLValue

            def configure_policies(registry: PolicyRegistry) -> None:
                @registry.register("send_email")
                def email_recipient_policy(
                    tool_name: str,
                    kwargs: Mapping[str, CaMeLValue],
                ) -> SecurityPolicyResult:
                    ...

            # At startup:
            import os
            os.environ["CAMEL_POLICY_MODULE"] = "myapp.security.policies"
            registry = PolicyRegistry.load_from_env()
        """
        registry = cls()
        module_path = os.environ.get("CAMEL_POLICY_MODULE", "").strip()
        if not module_path:
            return registry
        module = importlib.import_module(module_path)
        configure = getattr(module, "configure_policies")
        if not callable(configure):
            raise AttributeError(f"'{module_path}.configure_policies' is not callable")
        configure(registry)
        return registry


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def is_trusted(value: CaMeLValue) -> bool:
    """Return ``True`` if a value originates exclusively from trusted sources.

    A value is considered *trusted* when every label in its ``sources``
    field is a member of :data:`TRUSTED_SOURCE_LABELS` (``"User literal"``
    or ``"CaMeL"``).  Values with an empty ``sources`` set are **not**
    considered trusted (conservative default).

    Parameters
    ----------
    value:
        The :class:`~camel.value.CaMeLValue` to inspect.

    Returns
    -------
    bool
        ``True`` if ``value.sources`` is non-empty and is a subset of
        ``{"User literal", "CaMeL"}``.  ``False`` otherwise.

    Examples
    --------
    ::

        from camel.value import wrap

        # Trusted — came directly from the user query
        user_val = wrap("alice@example.com", sources=frozenset({"User literal"}))
        assert is_trusted(user_val) is True

        # Untrusted — came from an external tool
        email_val = wrap("bob@example.com", sources=frozenset({"read_email"}))
        assert is_trusted(email_val) is False

        # Mixed — partially untrusted → not trusted
        mixed_val = wrap(
            "...",
            sources=frozenset({"User literal", "read_email"}),
        )
        assert is_trusted(mixed_val) is False
    """
    if not value.sources:
        return False
    return value.sources <= TRUSTED_SOURCE_LABELS


def can_readers_read_value(value: CaMeLValue, reader: str) -> bool:
    """Return ``True`` if ``reader`` is an authorised reader of ``value``.

    A reader is authorised when either:

    * ``value.readers`` is the :data:`~camel.value.Public` sentinel
      (unrestricted access), or
    * ``reader`` is a member of ``value.readers`` (a ``frozenset[str]``).

    Parameters
    ----------
    value:
        The :class:`~camel.value.CaMeLValue` whose ``readers`` field is
        inspected.
    reader:
        A string identifying the principal whose access is being checked
        (e.g. an email address such as ``"alice@example.com"``).

    Returns
    -------
    bool
        ``True`` if ``reader`` is authorised to receive ``value``.

    Examples
    --------
    ::

        from camel.value import wrap, Public

        # Public value — any reader is allowed
        pub = wrap("hello", readers=Public)
        assert can_readers_read_value(pub, "anyone@example.com") is True

        # Restricted value — only alice is allowed
        restricted = wrap("secret", readers=frozenset({"alice@example.com"}))
        assert can_readers_read_value(restricted, "alice@example.com") is True
        assert can_readers_read_value(restricted, "eve@example.com") is False
    """
    if value.readers is Public:
        return True
    return reader in value.readers  # type: ignore[operator]


def get_all_sources(value: CaMeLValue) -> frozenset[str]:
    """Return the complete set of origin labels recorded on a value.

    This is a convenience accessor that returns ``value.sources`` directly.
    It is provided as a named helper so that policy code reads clearly and
    does not need to reference ``CaMeLValue`` internals.

    For dependency-graph-aware source traversal (i.e. collecting sources
    from all upstream variables too), combine this helper with
    :func:`~camel.dependency_graph.get_dependency_graph` and iterate over
    transitive dependencies.

    Parameters
    ----------
    value:
        The :class:`~camel.value.CaMeLValue` whose sources to return.

    Returns
    -------
    frozenset[str]
        The ``sources`` frozenset of ``value``.  May be empty for values
        constructed without an explicit source (e.g. bare integer literals).

    Examples
    --------
    ::

        from camel.value import wrap

        v = wrap(42, sources=frozenset({"read_db", "transform_step"}))
        assert get_all_sources(v) == frozenset({"read_db", "transform_step"})
    """
    return value.sources


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Sealed result type
    "SecurityPolicyResult",
    "Allowed",
    "Denied",
    # Type alias
    "PolicyFn",
    # Registry
    "PolicyRegistry",
    # Helper functions
    "is_trusted",
    "can_readers_read_value",
    "get_all_sources",
    # Constants
    "TRUSTED_SOURCE_LABELS",
]
