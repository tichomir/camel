"""CaMeL consent handler — production-grade user consent UX components.

This module provides the consent layer that sits between the policy engine
and the interpreter's policy-denial raise path.  When the interpreter runs
in ``EnforcementMode.PRODUCTION`` and a policy returns :class:`~camel.policy.Denied`,
the registered :class:`ConsentHandler` is invoked before a
:class:`~camel.interpreter.PolicyViolationError` is raised.

Components
----------
:class:`ConsentDecision`
    Enum of possible consent outcomes: ``APPROVE``, ``REJECT``, or
    ``APPROVE_FOR_SESSION``.

:class:`ConsentAuditEntry`
    Immutable record of a single consent decision, written to the interpreter's
    consent audit log (``CaMeLInterpreter.consent_audit_log``).

:class:`ConsentHandler`
    Abstract base class that defines the ``handle_consent`` interface.  Provide
    a concrete subclass to customise the consent UX (web UI, async, mobile, etc.).

:class:`DefaultCLIConsentHandler`
    Production-ready CLI implementation: renders a formatted prompt to stdout
    and reads the user's choice from stdin.  Offers ``Approve``, ``Reject``, and
    ``Approve for session`` actions.

:class:`ConsentDecisionCache`
    Session-level, in-process cache keyed on ``(tool_name, argument_hash)``.
    Populated only when the user selects ``APPROVE_FOR_SESSION``; subsequent
    calls for the same ``(tool, args)`` pair return the cached decision without
    re-prompting.  ``APPROVE`` and ``REJECT`` decisions are **never** cached.
    The cache is cleared on :meth:`ConsentDecisionCache.clear` or when the
    agent process restarts.

Integration
-----------
Pass a :class:`ConsentHandler` (and optionally a :class:`ConsentDecisionCache`)
to :class:`~camel.interpreter.CaMeLInterpreter` via the ``consent_handler``
parameter together with ``enforcement_mode=EnforcementMode.PRODUCTION``::

    from camel.interpreter import CaMeLInterpreter, EnforcementMode
    from camel.consent import DefaultCLIConsentHandler, ConsentDecisionCache

    handler = DefaultCLIConsentHandler()
    cache = ConsentDecisionCache()

    interp = CaMeLInterpreter(
        tools=my_tools,
        enforcement_mode=EnforcementMode.PRODUCTION,
        consent_handler=handler,
        consent_cache=cache,
    )

After execution, inspect the consent audit log::

    for entry in interp.consent_audit_log:
        print(entry.tool_name, entry.decision, entry.session_cache_hit)

NFR references
--------------
* **NFR-6** — all consent decisions are recorded in the audit log with
  timestamp, decision, tool name, argument summary, and cache-hit flag.
* **Risk L4** — session caching reduces repeated consent prompts for
  already-approved ``(tool, args)`` combinations, mitigating user fatigue.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import final


# ---------------------------------------------------------------------------
# ConsentDecision
# ---------------------------------------------------------------------------


class ConsentDecision(Enum):
    """Possible outcomes of a consent prompt.

    Members
    -------
    APPROVE:
        User approved this specific invocation.  The decision is **not**
        cached; the user will be prompted again for the same
        ``(tool, args)`` combination on subsequent calls.
    REJECT:
        User rejected this specific invocation.  The decision is **not**
        cached; the interpreter raises
        :class:`~camel.interpreter.PolicyViolationError` with
        ``consent_decision="UserRejected"``.
    APPROVE_FOR_SESSION:
        User approved **and** requested that future invocations with the
        same ``(tool, argument_hash)`` key be automatically approved for
        the remainder of the session.  The :class:`ConsentDecisionCache`
        stores this decision and returns it on subsequent lookups without
        re-prompting.

    Notes
    -----
    ``APPROVE`` and ``REJECT`` decisions are never stored in
    :class:`ConsentDecisionCache`.  Only ``APPROVE_FOR_SESSION`` populates
    the cache.
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    APPROVE_FOR_SESSION = "APPROVE_FOR_SESSION"


# ---------------------------------------------------------------------------
# ConsentAuditEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentAuditEntry:
    """Immutable audit log record for a single consent decision.

    One entry is appended to :attr:`~camel.interpreter.CaMeLInterpreter.consent_audit_log`
    for every consent prompt that fires (including cache-hit returns).

    Attributes
    ----------
    decision:
        The :class:`ConsentDecision` returned by the handler (or retrieved
        from the session cache).
    timestamp:
        ISO-8601 UTC timestamp of the decision,
        e.g. ``"2026-03-18T12:00:00.000000+00:00"``.
    tool_name:
        Name of the tool whose call triggered the consent prompt.
    argument_summary:
        Human-readable summary of the tool's arguments as produced by
        :func:`~camel.interpreter._summarise_args`.  Contains value types
        and provenance labels — never raw sensitive data.
    session_cache_hit:
        ``True`` when the decision was retrieved from
        :class:`ConsentDecisionCache` without invoking the handler.
        ``False`` when the handler was actually called.

    Examples
    --------
    ::

        entry = ConsentAuditEntry(
            decision=ConsentDecision.APPROVE_FOR_SESSION,
            timestamp="2026-03-18T12:00:00+00:00",
            tool_name="send_email",
            argument_summary="to: str (User literal); body: str (read_email)",
            session_cache_hit=False,
        )
        assert entry.decision is ConsentDecision.APPROVE_FOR_SESSION
        assert not entry.session_cache_hit
    """

    decision: ConsentDecision
    timestamp: str
    tool_name: str
    argument_summary: str
    session_cache_hit: bool


# ---------------------------------------------------------------------------
# ConsentHandler ABC
# ---------------------------------------------------------------------------


class ConsentHandler(ABC):
    """Abstract base class for consent prompt handlers.

    Subclass this to customise the consent UX for your deployment.
    Implementations may render a web UI dialog, send a mobile push
    notification, or resolve via an async approval workflow — all are
    supported provided the ``handle_consent`` method ultimately returns a
    :class:`ConsentDecision`.

    The default CLI implementation is :class:`DefaultCLIConsentHandler`.

    Extension points
    ----------------
    * Override :meth:`handle_consent` to change how consent is collected.
    * Pass custom instances to :class:`~camel.interpreter.CaMeLInterpreter`
      via the ``consent_handler`` constructor parameter.
    * For async workflows, subclass and internally use ``asyncio.run()``
      or a dedicated event loop inside :meth:`handle_consent`; the
      interpreter's consent path is synchronous.

    Examples
    --------
    Minimal custom handler::

        class AlwaysApproveHandler(ConsentHandler):
            def handle_consent(
                self,
                tool_name: str,
                argument_summary: str,
                denial_reason: str,
            ) -> ConsentDecision:
                return ConsentDecision.APPROVE
    """

    @abstractmethod
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        """Prompt the user (or an approval system) for a consent decision.

        This method is called synchronously by the interpreter whenever a
        policy returns :class:`~camel.policy.Denied` and the interpreter is
        in ``EnforcementMode.PRODUCTION``.  It must return a
        :class:`ConsentDecision` before the interpreter can continue.

        Parameters
        ----------
        tool_name:
            Name of the tool whose call was denied by a policy (e.g.
            ``"send_email"``).
        argument_summary:
            Human-readable summary of the arguments that were going to be
            passed to the tool.  Contains only value types and provenance
            labels — never raw sensitive field values — so it is safe to
            display to the user.
        denial_reason:
            Verbatim ``Denied.reason`` string from the policy engine.
            Describes *why* the call was blocked (e.g.
            ``"recipient address originates from untrusted data"``).

        Returns
        -------
        ConsentDecision
            * :attr:`ConsentDecision.APPROVE` — approve this call once.
            * :attr:`ConsentDecision.REJECT` — reject; interpreter raises
              :class:`~camel.interpreter.PolicyViolationError`.
            * :attr:`ConsentDecision.APPROVE_FOR_SESSION` — approve and
              cache for the remainder of the session.
        """


# ---------------------------------------------------------------------------
# DefaultCLIConsentHandler
# ---------------------------------------------------------------------------


@final
class DefaultCLIConsentHandler(ConsentHandler):
    """Production-ready CLI consent handler.

    Renders a formatted banner to ``stdout`` showing the tool name, argument
    summary, and policy denial reason, then prompts the user to choose one
    of three actions:

    * ``A`` — Approve (once, not cached).
    * ``R`` — Reject (raises :class:`~camel.interpreter.PolicyViolationError`).
    * ``S`` — Approve for session (cached in :class:`ConsentDecisionCache`).

    Invalid input causes the prompt to repeat until a valid choice is
    entered.

    Examples
    --------
    ::

        from camel.consent import DefaultCLIConsentHandler
        from camel.interpreter import CaMeLInterpreter, EnforcementMode

        interp = CaMeLInterpreter(
            tools=my_tools,
            policy_engine=my_registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_handler=DefaultCLIConsentHandler(),
        )
    """

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        """Display a formatted CLI prompt and return the user's decision.

        Prints a banner to ``stdout`` containing the tool name, argument
        summary, and denial reason, then reads a single-character response
        from ``stdin``.  Repeats until the user enters ``A``, ``R``, or
        ``S``.

        Parameters
        ----------
        tool_name:
            Name of the tool that was denied.
        argument_summary:
            Human-readable argument summary (safe to display).
        denial_reason:
            Verbatim denial reason from the policy engine.

        Returns
        -------
        ConsentDecision
            ``APPROVE`` for ``A``, ``REJECT`` for ``R``,
            ``APPROVE_FOR_SESSION`` for ``S``.
        """
        separator = "=" * 60
        print()
        print(separator)
        print("  POLICY DENIAL — USER CONSENT REQUIRED")
        print(separator)
        print(f"  Tool    : {tool_name}")
        print(f"  Args    : {argument_summary}")
        print(f"  Reason  : {denial_reason}")
        print("-" * 60)
        print("  [A]  Approve this call (once)")
        print("  [R]  Reject this call")
        print("  [S]  Approve for session (remember this decision)")
        print(separator)
        while True:
            raw = input("  Your choice [A/R/S]: ").strip().upper()
            if raw == "A":
                return ConsentDecision.APPROVE
            elif raw == "R":
                return ConsentDecision.REJECT
            elif raw == "S":
                return ConsentDecision.APPROVE_FOR_SESSION
            else:
                print(f"  Invalid choice {raw!r}. Please enter A, R, or S.")


# ---------------------------------------------------------------------------
# ConsentDecisionCache
# ---------------------------------------------------------------------------


class ConsentDecisionCache:
    """Session-level in-process cache for ``APPROVE_FOR_SESSION`` consent decisions.

    The cache is keyed on ``(tool_name, argument_hash)`` where
    ``argument_hash`` is a SHA-256 digest of the ``argument_summary`` string.
    It is populated **only** when a :class:`ConsentHandler` returns
    :attr:`ConsentDecision.APPROVE_FOR_SESSION`; ``APPROVE`` and ``REJECT``
    decisions are never stored.

    Subsequent calls with the same ``(tool_name, argument_summary)`` pair
    return the cached :attr:`ConsentDecision.APPROVE_FOR_SESSION` without
    invoking the handler.

    The cache is **in-process and session-scoped**: it lives for the lifetime
    of the :class:`ConsentDecisionCache` instance and is cleared either by
    calling :meth:`clear` or when the agent process restarts.

    Thread safety
    -------------
    The cache is **not** thread-safe.  If multiple threads share a single
    :class:`ConsentDecisionCache` instance, external locking is required.
    Each :class:`~camel.interpreter.CaMeLInterpreter` call in
    :class:`~camel_security.agent.CaMeLAgent` uses its own interpreter
    instance; sharing a cache across agent runs is intentional (session
    persistence) but sharing across threads requires a lock.

    Examples
    --------
    ::

        cache = ConsentDecisionCache()

        # Nothing cached yet
        assert cache.lookup("send_email", "to: str") is None

        # Store an APPROVE_FOR_SESSION decision
        cache.store("send_email", "to: str", ConsentDecision.APPROVE_FOR_SESSION)
        assert cache.lookup("send_email", "to: str") is ConsentDecision.APPROVE_FOR_SESSION

        # APPROVE and REJECT are never cached
        cache.store("send_email", "to: str", ConsentDecision.APPROVE)
        # (no effect on the cached APPROVE_FOR_SESSION)
    """

    def __init__(self) -> None:
        """Initialise an empty consent decision cache."""
        self._cache: dict[tuple[str, str], ConsentDecision] = {}

    @staticmethod
    def _hash(argument_summary: str) -> str:
        """Return a SHA-256 hex digest of *argument_summary*.

        Parameters
        ----------
        argument_summary:
            The argument summary string to hash.

        Returns
        -------
        str
            Hex-encoded SHA-256 digest (64 characters).
        """
        return hashlib.sha256(argument_summary.encode("utf-8")).hexdigest()

    def lookup(
        self,
        tool_name: str,
        argument_summary: str,
    ) -> ConsentDecision | None:
        """Return the cached decision for ``(tool_name, argument_summary)``, or ``None``.

        Parameters
        ----------
        tool_name:
            The tool name to look up.
        argument_summary:
            The argument summary string to look up (hashed internally).

        Returns
        -------
        ConsentDecision | None
            The cached :attr:`ConsentDecision.APPROVE_FOR_SESSION` if present,
            or ``None`` if no cached decision exists.
        """
        key = (tool_name, self._hash(argument_summary))
        return self._cache.get(key)

    def store(
        self,
        tool_name: str,
        argument_summary: str,
        decision: ConsentDecision,
    ) -> None:
        """Store a decision in the cache if it is ``APPROVE_FOR_SESSION``.

        ``APPROVE`` and ``REJECT`` decisions are silently ignored — they are
        never cached.

        Parameters
        ----------
        tool_name:
            The tool name to associate with the decision.
        argument_summary:
            The argument summary string (hashed internally as the cache key).
        decision:
            The consent decision to store.  Only
            :attr:`ConsentDecision.APPROVE_FOR_SESSION` is actually stored.
        """
        if decision is not ConsentDecision.APPROVE_FOR_SESSION:
            return
        key = (tool_name, self._hash(argument_summary))
        self._cache[key] = decision

    def clear(self) -> None:
        """Remove all cached consent decisions.

        After this call, all subsequent :meth:`lookup` calls return ``None``
        until new decisions are stored.
        """
        self._cache.clear()

    def __len__(self) -> int:
        """Return the number of cached decisions.

        Returns
        -------
        int
            Count of ``(tool_name, argument_hash)`` entries currently cached.
        """
        return len(self._cache)


# ---------------------------------------------------------------------------
# _resolve_consent — internal helper used by the interpreter
# ---------------------------------------------------------------------------


def _resolve_consent(
    tool_name: str,
    argument_summary: str,
    denial_reason: str,
    handler: ConsentHandler,
    cache: ConsentDecisionCache,
    audit_log: list[ConsentAuditEntry],
) -> bool:
    """Resolve a consent decision and record an audit entry.

    This is an internal helper called by the interpreter's enforcement paths.
    It first checks the cache; if no cached decision exists, it delegates to
    the ``handler``.  In either case it appends a :class:`ConsentAuditEntry`
    to ``audit_log`` and returns a boolean indicating whether the call should
    proceed.

    Parameters
    ----------
    tool_name:
        Name of the tool requiring consent.
    argument_summary:
        Human-readable argument summary string.
    denial_reason:
        Verbatim denial reason from the policy engine.
    handler:
        The :class:`ConsentHandler` to invoke on a cache miss.
    cache:
        The :class:`ConsentDecisionCache` to consult and update.
    audit_log:
        The mutable list to which the new :class:`ConsentAuditEntry` is
        appended.

    Returns
    -------
    bool
        ``True`` if the decision was ``APPROVE`` or ``APPROVE_FOR_SESSION``.
        ``False`` if the decision was ``REJECT``.
    """
    cached = cache.lookup(tool_name, argument_summary)
    if cached is not None:
        entry = ConsentAuditEntry(
            decision=cached,
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            argument_summary=argument_summary,
            session_cache_hit=True,
        )
        audit_log.append(entry)
        return cached in (ConsentDecision.APPROVE, ConsentDecision.APPROVE_FOR_SESSION)

    decision = handler.handle_consent(tool_name, argument_summary, denial_reason)
    cache.store(tool_name, argument_summary, decision)
    entry = ConsentAuditEntry(
        decision=decision,
        timestamp=datetime.now(timezone.utc).isoformat(),
        tool_name=tool_name,
        argument_summary=argument_summary,
        session_cache_hit=False,
    )
    audit_log.append(entry)
    return decision in (ConsentDecision.APPROVE, ConsentDecision.APPROVE_FOR_SESSION)


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    "ConsentDecision",
    "ConsentAuditEntry",
    "ConsentHandler",
    "DefaultCLIConsentHandler",
    "ConsentDecisionCache",
    "_resolve_consent",
]
