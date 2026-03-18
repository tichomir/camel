# ADR-012: Policy Testing Harness, CaMeLValueBuilder, PolicySimulator, and ConsentHandler

| Field         | Value                                                                                    |
|---------------|------------------------------------------------------------------------------------------|
| Status        | Accepted                                                                                 |
| Date          | 2026-03-18                                                                               |
| Author        | Software Architect Persona                                                               |
| Supersedes    | —                                                                                        |
| Superseded by | —                                                                                        |
| Related       | ADR-010 (Enforcement Hook & Consent Flow), ADR-011 (Three-Tier Policy Governance)       |
| PRD refs      | M5-F12 through M5-F19, NFR-6, Risk L4                                                   |

---

## Context

Milestone 5 Phase: Policy Testing Harness & User Consent UX introduces four new
components that complete the developer tooling story and promote the consent flow
from a simple `bool`-returning callback to a production-grade interface:

| Component             | Purpose                                                                   |
|-----------------------|---------------------------------------------------------------------------|
| `PolicyTestRunner`    | Batch-evaluates a policy function against typed test cases; structured report |
| `CaMeLValueBuilder`   | Fluent builder for constructing `CaMeLValue` instances in test code       |
| `PolicySimulator`     | Dry-run traversal of an execution plan without invoking side-effecting tools |
| `ConsentHandler`      | Pluggable production-grade consent interface replacing `ConsentCallback`  |

The existing `ConsentCallback` Protocol (ADR-010 §Decision 2) is a `bool`-returning
callable.  It lacks:

- A richer `ConsentDecision` enum that can express session-level approvals.
- A session-level consent cache that prevents repeat prompts for the same
  `(tool, argument_hash)` pair.
- A standard `AuditLogEntry` field for the `APPROVE_FOR_SESSION` variant.
- An officially supported CLI default implementation.

Addressing these gaps satisfies PRD Risk L4 (user fatigue from policy denials) and
milestone features M5-F12 through M5-F19.

---

## Decisions

### Decision 1 — `PolicyTestCase` and `PolicyTestReport` schemas

**Decision:** Introduce two frozen dataclasses — `PolicyTestCase` (input descriptor)
and `PolicyTestReport` (aggregate result) — as the core data model for the
`PolicyTestRunner`.

#### `PolicyTestCase`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from camel.policy.interfaces import SecurityPolicyResult
from camel.value import CaMeLValue


@dataclass(frozen=True)
class PolicyTestCase:
    """A single unit test case for a security policy function.

    Attributes
    ----------
    description:
        Human-readable name for the test case (used in report output).
    tool_name:
        The tool name to pass to the policy function during evaluation.
    kwargs:
        The argument mapping to evaluate against the policy.
    expected_outcome:
        ``"Allowed"`` or ``"Denied"``.
    expected_reason_fragment:
        When set and ``expected_outcome="Denied"``, the test passes only if
        ``Denied.reason`` contains this substring (case-insensitive).
        Ignored when ``expected_outcome="Allowed"``.
    """

    description: str
    tool_name: str
    kwargs: dict[str, CaMeLValue]
    expected_outcome: Literal["Allowed", "Denied"]
    expected_reason_fragment: str | None = None
```

**Field rationale:**

- `description` is mandatory — un-named test cases are nearly impossible to
  diagnose from a batch report without it.
- `kwargs` holds pre-built `CaMeLValue` objects so that the test case is fully
  self-contained and can be authored with `CaMeLValueBuilder` (see Decision 2)
  without requiring a live interpreter.

#### `PolicyTestResult`

```python
@dataclass(frozen=True)
class PolicyTestResult:
    """Result of evaluating a single :class:`PolicyTestCase`.

    Attributes
    ----------
    test_case:
        The :class:`PolicyTestCase` that was evaluated.
    passed:
        ``True`` if the actual outcome matched the expected outcome
        (and, when applicable, the expected reason fragment).
    actual_outcome:
        The :class:`~camel.policy.interfaces.SecurityPolicyResult` returned
        by the policy function.
    failure_reason:
        Human-readable explanation of why the test failed; ``None`` when
        ``passed=True``.
    """

    test_case: PolicyTestCase
    passed: bool
    actual_outcome: SecurityPolicyResult
    failure_reason: str | None = None
```

#### `PolicyTestReport`

```python
@dataclass(frozen=True)
class PolicyTestReport:
    """Aggregate result of a :class:`PolicyTestRunner` batch run.

    Attributes
    ----------
    policy_name:
        The human-readable name of the policy under test.
    tool_name:
        The tool name all test cases in this report target.
    results:
        Ordered tuple of :class:`PolicyTestResult` — one per input
        :class:`PolicyTestCase`.
    passed:
        Count of test cases whose outcome matched expectations.
    failed:
        Count of test cases whose outcome did not match expectations.
    total:
        ``passed + failed``.
    coverage_notes:
        Optional free-text note from the runner (e.g. un-covered edge
        cases identified during run).
    """

    policy_name: str
    tool_name: str
    results: tuple[PolicyTestResult, ...]
    passed: int
    failed: int
    total: int
    coverage_notes: str | None = None

    @property
    def all_passed(self) -> bool:
        """Return ``True`` if every test case passed."""
        return self.failed == 0
```

**Coverage statistics rationale:**

`passed`, `failed`, and `total` are stored as integer fields rather than computed
only from `results` so that downstream consumers (CI pipelines, report formatters)
do not need to traverse the tuple.

---

### Decision 2 — `CaMeLValueBuilder` fluent API

**Decision:** Provide a `CaMeLValueBuilder` class in
`camel/testing/value_builder.py` with a fluent builder API for constructing
`CaMeLValue` instances in test and simulation code, without requiring a live
interpreter.

**Typed signature:**

```python
from __future__ import annotations

from typing import Any, Self

from camel.value import CaMeLValue, Public, Readers


class CaMeLValueBuilder:
    """Fluent builder for constructing :class:`~camel.value.CaMeLValue` instances.

    Use this in test code and :class:`PolicySimulator` scenarios to build
    :class:`~camel.value.CaMeLValue` objects with explicit provenance without
    spinning up a live interpreter.

    All setter methods return ``self`` to enable chaining::

        cv = (
            CaMeLValueBuilder()
            .with_value("attacker@evil.com")
            .with_sources("read_email")
            .with_readers("alice@example.com")
            .build()
        )

    Convenience class-methods provide idiomatic starting points::

        trusted = CaMeLValueBuilder.trusted("alice@example.com").build()
        untrusted = CaMeLValueBuilder.untrusted("evil.com", source="read_email").build()
        tool_result = CaMeLValueBuilder.from_tool(42, "get_balance").build()
    """

    def __init__(self) -> None:
        self._value: Any = None
        self._sources: frozenset[str] = frozenset()
        self._inner_source: str | None = None
        self._readers: Readers = Public

    # ------------------------------------------------------------------
    # Fluent setters
    # ------------------------------------------------------------------

    def with_value(self, value: Any) -> Self:
        """Set the underlying Python value."""

    def with_sources(self, *sources: str) -> Self:
        """Add one or more source labels to the sources set."""

    def with_inner_source(self, inner_source: str) -> Self:
        """Set the inner_source field (sub-field label within a tool response)."""

    def with_readers(self, *readers: str) -> Self:
        """Restrict readers to the given set of principals."""

    def with_public_readers(self) -> Self:
        """Set readers to Public (unrestricted access)."""

    def with_no_readers(self) -> Self:
        """Set readers to an empty frozenset (no principal may receive this value)."""

    def with_dependency_chain(self, *dep_values: CaMeLValue) -> Self:
        """Merge sources and readers from upstream CaMeLValue dependencies.

        This merges ``sources`` and ``readers`` from every ``dep_values``
        entry into the builder state using union semantics, simulating the
        capability propagation that the interpreter applies to derived values.
        ``inner_source`` is not propagated (it is cleared to ``None`` for
        derived values).
        """

    # ------------------------------------------------------------------
    # Terminal builder
    # ------------------------------------------------------------------

    def build(self) -> CaMeLValue:
        """Construct and return the :class:`~camel.value.CaMeLValue`."""

    # ------------------------------------------------------------------
    # Class-level convenience factories
    # ------------------------------------------------------------------

    @classmethod
    def trusted(cls, value: Any, source: str = "User literal") -> Self:
        """Return a builder pre-configured with a trusted source label.

        Equivalent to::

            CaMeLValueBuilder().with_value(value).with_sources(source)
        """

    @classmethod
    def untrusted(cls, value: Any, source: str = "external_tool") -> Self:
        """Return a builder pre-configured with an untrusted source label.

        Equivalent to::

            CaMeLValueBuilder().with_value(value).with_sources(source)
        """

    @classmethod
    def from_tool(cls, value: Any, tool_name: str) -> Self:
        """Return a builder pre-configured with a tool-origin source label.

        Equivalent to ``cls.untrusted(value, source=tool_name)`` with the
        distinction that the intent is tool-output (not arbitrary external
        data).
        """
```

**Why a fluent builder rather than extending `wrap()`:**

- `wrap()` is a one-shot convenience constructor with optional parameters;
  it does not compose well when building values with multi-hop dependency
  chains.
- `CaMeLValueBuilder.with_dependency_chain()` covers multi-hop provenance
  propagation that is currently achievable only by calling multiple
  `propagate_*` functions in sequence — a pattern too verbose for test code.
- The fluent API keeps test code readable and self-documenting; the starting
  factory (`trusted`, `untrusted`, `from_tool`) signals intent immediately.

**Module placement — `camel/testing/`:**

The builder is placed in a new `camel/testing/` sub-package rather than
`tests/harness/` so that downstream users of the `camel-security` SDK can
also use it in their own policy test suites without importing from the
`tests/` tree.

---

### Decision 3 — `PolicySimulator` dry-run protocol

**Decision:** Introduce `PolicySimulator` in `camel/testing/simulator.py`.
The simulator executes the interpreter's tool-call dispatch path in full —
including capability propagation and policy evaluation — but intercepts all
tool calls before they execute and substitutes a no-op result.

**`SimulatedPolicyTrigger` dataclass:**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from camel.policy.interfaces import SecurityPolicyResult
from camel.policy.governance import PolicyTier


@dataclass(frozen=True)
class SimulatedPolicyTrigger:
    """Record of a policy evaluation encountered during a simulation run.

    Attributes
    ----------
    tool_name:
        The tool whose call triggered the policy check.
    argument_snapshot:
        Mapping of ``{arg_name: repr(raw_value)[:80]}`` at the time of
        the policy check.  Truncated to prevent log bloat and avoid leaking
        sensitive data into simulation reports.
    policy_name:
        The name of the policy that produced ``outcome``.  For flat
        :class:`~camel.policy.interfaces.PolicyRegistry` evaluations this
        is the first policy that returned ``Denied``, or an empty string
        when all returned ``Allowed``.
    outcome:
        The :class:`~camel.policy.interfaces.SecurityPolicyResult` produced
        by the evaluation.
    tier:
        The :class:`~camel.policy.governance.PolicyTier` of the authoritative
        policy entry, or ``None`` when a flat ``PolicyRegistry`` (ADR-009)
        is used.
    """

    tool_name: str
    argument_snapshot: dict[str, str]
    policy_name: str
    outcome: SecurityPolicyResult
    tier: PolicyTier | None
```

**`SimulationReport` dataclass:**

```python
@dataclass(frozen=True)
class SimulationReport:
    """Aggregate result of a :class:`PolicySimulator` dry-run.

    Attributes
    ----------
    code_plan:
        The Python code plan that was simulated.
    triggered_policies:
        Ordered tuple of :class:`SimulatedPolicyTrigger` — one per policy
        evaluation encountered during simulation, regardless of outcome.
    suppressed_tool_calls:
        Ordered tuple of tool names whose calls were intercepted and
        suppressed during the dry run (i.e. the tool function was not
        called even when all policies returned ``Allowed``).
    would_have_succeeded:
        ``True`` if all policy evaluations returned ``Allowed``.  ``False``
        if any evaluation returned ``Denied`` (even when
        ``non_overridable_denial=False``).
    denial_triggers:
        Subset of ``triggered_policies`` where ``outcome`` is
        ``Denied``.  Computed from ``triggered_policies`` at construction.
    """

    code_plan: str
    triggered_policies: tuple[SimulatedPolicyTrigger, ...]
    suppressed_tool_calls: tuple[str, ...]
    would_have_succeeded: bool
    denial_triggers: tuple[SimulatedPolicyTrigger, ...]
```

**`PolicySimulator` class:**

```python
from camel.policy.interfaces import PolicyRegistry
from camel.value import CaMeLValue


class PolicySimulator:
    """Dry-run execution of a CaMeL code plan with policy evaluation only.

    The simulator:

    1. Constructs a :class:`~camel.interpreter.CaMeLInterpreter` in
       ``EVALUATION`` mode.
    2. Replaces every registered tool with a **no-op stub** that accepts any
       keyword arguments and returns
       ``CaMeLValue(value=None, sources=frozenset({"CaMeL"}), ...)``.
    3. Executes the ``code_plan`` using the live interpreter against the
       supplied ``policy_registry``.
    4. Captures each policy evaluation event via the interpreter's audit log.
    5. Returns a :class:`SimulationReport` summarising which policies were
       triggered and what the outcome was.

    Side-effecting tools (``send_email``, ``send_money``, ``write_file``,
    etc.) are **never called**.  Only the policy evaluation path runs.

    Parameters
    ----------
    policy_registry:
        The :class:`~camel.policy.interfaces.PolicyRegistry` or
        :class:`~camel.policy.governance.TieredPolicyRegistry` to evaluate
        against.
    tool_names:
        Names of the tools to stub out.  All tools listed here are replaced
        with no-op stubs; calls to unlisted names raise
        ``UnsupportedSyntaxError`` (as if the tool does not exist).
    """

    def __init__(
        self,
        policy_registry: PolicyRegistry,
        tool_names: list[str],
    ) -> None: ...

    def simulate(
        self,
        code_plan: str,
        variable_store: dict[str, CaMeLValue] | None = None,
    ) -> SimulationReport:
        """Execute ``code_plan`` in dry-run mode and return a report.

        Parameters
        ----------
        code_plan:
            Restricted Python plan to simulate.
        variable_store:
            Optional pre-populated variable store to seed the interpreter
            with (e.g. values returned by earlier real tool calls).
            Defaults to an empty store.

        Returns
        -------
        SimulationReport
            Structured result with triggered policy records and suppressed
            tool call names.
        """
```

**Why stubs return `CaMeLValue(sources=frozenset({"CaMeL"}))` (trusted):**

Stub results are trusted so that downstream data-flow steps in the code plan
can proceed without being blocked by capability restrictions.  If the stubs
returned untrusted values, subsequent policy evaluations might trigger
denials that would only occur because of the simulation, not because of the
real execution path.  Platform teams who want to simulate with untrusted
stub returns should build custom stubs and inject them via `variable_store`.

**Scope boundary — no P-LLM involvement:**

`PolicySimulator.simulate()` accepts a pre-existing code plan string.  It
does not invoke the P-LLM.  Teams that wish to simulate a full end-to-end
run (P-LLM → interpreter) must call `PLLMWrapper.generate_plan()` separately,
then pass the plan to `simulate()`.

---

### Decision 4 — `ConsentHandler` interface and `ConsentDecision` enum

**Decision:** Replace the `bool`-returning `ConsentCallback` Protocol with a
richer `ConsentHandler` Protocol and a `ConsentDecision` enum.

**`ConsentDecision` enum:**

```python
from enum import Enum


class ConsentDecision(Enum):
    """The outcome of a user consent prompt.

    Members
    -------
    APPROVE:
        The user approves this specific tool call.  The call proceeds;
        one ``AuditLogEntry`` with ``consent_decision="UserApproved"`` is
        written.
    REJECT:
        The user rejects this tool call.  The call is blocked;
        ``PolicyViolationError(consent_decision="UserRejected")`` is raised.
        One ``AuditLogEntry`` with ``consent_decision="UserRejected"`` is
        written.
    APPROVE_FOR_SESSION:
        The user approves this tool call **and** all subsequent calls with
        the same ``(tool_name, argument_hash)`` key for the duration of the
        current interpreter session.  The approval is cached in the session
        consent cache so that identical calls do not prompt again.
        One ``AuditLogEntry`` with ``consent_decision="UserApprovedForSession"``
        is written for the first approval; all subsequent hits against the
        cache are recorded as ``consent_decision="CacheHit"``.
    """

    APPROVE = "Approve"
    REJECT = "Reject"
    APPROVE_FOR_SESSION = "ApproveForSession"
```

**`ConsentHandler` Protocol:**

```python
from typing import Protocol


class ConsentHandler(Protocol):
    """Protocol for production-grade user consent handlers.

    All implementations must be synchronous.  Async UI frameworks must
    bridge using ``asyncio.run()`` or equivalent, outside the interpreter.

    Extension points
    ----------------
    CLI (default):
        :class:`CLIConsentHandler` — prints a formatted prompt to ``stdout``
        and reads a single-character response from ``stdin``.  Returns
        ``APPROVE``, ``REJECT``, or ``APPROVE_FOR_SESSION`` based on user
        input.

    Web UI:
        Implement ``handle_consent`` as a synchronous wrapper that blocks on
        a ``threading.Event`` set by the async HTTP handler.  Pass the
        handler instance to ``CaMeLInterpreter`` at construction time.

    Mobile:
        Same pattern as Web UI; the native UI callback signals completion via
        a synchronous bridge (e.g. ``ctypes`` callback, JNI, or a
        ``concurrent.futures.Future``).

    Async workflows:
        Use ``asyncio.get_event_loop().run_until_complete()`` inside
        ``handle_consent`` to await an async consent dialog.  This pattern
        must not be used when an event loop is already running on the calling
        thread; in that case, use a ``ThreadPoolExecutor`` bridge instead.
    """

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        """Display a consent prompt and return the user's decision.

        Parameters
        ----------
        tool_name:
            The name of the tool being blocked.
        argument_summary:
            Human-readable summary of the tool call arguments.  Generated
            by the interpreter's ``_summarise_args`` helper (ADR-010 §D3).
        denial_reason:
            The ``Denied.reason`` string from the policy that blocked the
            call.

        Returns
        -------
        ConsentDecision
            ``APPROVE``, ``REJECT``, or ``APPROVE_FOR_SESSION``.
        """
        ...
```

**`CLIConsentHandler` — default implementation:**

```python
class CLIConsentHandler:
    """Default CLI consent handler.

    Prints a formatted consent prompt to ``stdout`` and reads a response
    from ``stdin``.

    Prompt format::

        ──────────────────────────────────────────────
        POLICY DENIAL — Consent Required
        ──────────────────────────────────────────────
        Tool         : send_email
        Arguments    : send_email(to='eve@evil.com' [sources: read_email])
        Denial reason: recipient address originates from untrusted data
        ──────────────────────────────────────────────
        Options: [a] Approve  [r] Reject  [s] Approve for this session
        Your choice:

    Returns
    -------
    ConsentDecision
        Based on user input:
        - ``a`` / ``A`` → ``APPROVE``
        - ``s`` / ``S`` → ``APPROVE_FOR_SESSION``
        - anything else (including ``r`` / ``R``, empty, or Ctrl-C) → ``REJECT``
    """

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        """Print prompt to stdout and return user's decision."""
```

**Backward compatibility with `ConsentCallback`:**

The existing `ConsentCallback` Protocol (ADR-010 §Decision 2) returns `bool`.
`CaMeLInterpreter` supports both types via a duck-typed adapter:

- If the supplied callback satisfies `ConsentHandler` (has `handle_consent`
  method), it is called directly.
- If it is a legacy `ConsentCallback` (plain callable returning `bool`), it
  is wrapped in a `_LegacyConsentHandlerAdapter` that maps `True → APPROVE`
  and `False → REJECT`.

This adapter is internal and not part of the public API.

---

### Decision 5 — Session-level consent cache

**Decision:** Introduce `ConsentCache` in `camel/testing/` (also exported via
`camel_security`) as an in-memory, session-scoped consent cache keyed on
`(tool_name, argument_hash)`.

**`ConsentCacheKey` dataclass:**

```python
@dataclass(frozen=True)
class ConsentCacheKey:
    """Immutable cache key for consent decisions.

    Attributes
    ----------
    tool_name:
        Registered tool name.
    argument_hash:
        SHA-256 hex digest of the canonical argument representation.
        Computed by :func:`compute_consent_key` from the raw argument
        mapping.

    Notes
    -----
    The argument hash is computed from a deterministic string representation
    of the tool call arguments: for each ``(key, cv)`` pair (sorted by key),
    the format is ``"{key}={repr(cv.raw)[:200]};sources={sorted(cv.sources)}"``.
    The string is UTF-8 encoded before hashing.  This representation is
    **not** cryptographically collision-resistant against adversarial inputs;
    it is intended solely to distinguish distinct tool call signatures
    within a single session.
    """

    tool_name: str
    argument_hash: str
```

**`compute_consent_key` function:**

```python
def compute_consent_key(
    tool_name: str,
    kwargs: dict[str, CaMeLValue],
) -> ConsentCacheKey:
    """Compute a :class:`ConsentCacheKey` from a tool name and kwargs.

    Parameters
    ----------
    tool_name:
        Registered tool name.
    kwargs:
        Argument mapping to hash.

    Returns
    -------
    ConsentCacheKey
        Deterministic cache key for this ``(tool_name, kwargs)`` pair.
    """
```

**`ConsentCache` class:**

```python
class ConsentCache:
    """In-memory, session-scoped consent decision cache.

    Scope
    -----
    One ``ConsentCache`` instance is created per interpreter session when
    the :class:`ConsentHandler` is wired to the interpreter with a
    ``ConsentCache`` argument.  It is never shared across sessions.

    Lifetime
    --------
    The cache lives only for the duration of the interpreter session (i.e.
    until the ``CaMeLInterpreter`` instance is garbage collected).  There
    is no persistence across sessions by design — security decisions must
    be re-confirmed in each new session.

    TTL
    ---
    No time-based TTL is implemented.  Cache entries expire only when the
    interpreter session ends.  This is intentional: the primary safeguard
    against stale approvals is the session boundary, not a wall-clock
    timeout.  Callers who require time-based expiry should invalidate
    entries explicitly via :meth:`invalidate`.
    """

    def __init__(self) -> None:
        """Initialise an empty consent cache."""

    def get(self, key: ConsentCacheKey) -> ConsentDecision | None:
        """Return the cached decision for ``key``, or ``None`` if not cached."""

    def set(
        self,
        key: ConsentCacheKey,
        decision: ConsentDecision,
    ) -> None:
        """Store a consent decision under ``key``."""

    def invalidate(self, key: ConsentCacheKey) -> None:
        """Remove a specific entry from the cache (no-op if not present)."""

    def clear(self) -> None:
        """Remove all cached entries."""

    def __len__(self) -> int:
        """Return the number of cached entries."""
```

**Cache integration with `CaMeLInterpreter`:**

The interpreter's pre-execution enforcement hook is extended as follows when
`enforcement_mode=PRODUCTION`:

```
1. Compute key = compute_consent_key(tool_name, kwargs_mapping)
2. Check consent_cache.get(key)
   - Cache HIT (APPROVE_FOR_SESSION):
       append AuditLogEntry(consent_decision="CacheHit")
       proceed to tool call.
   - Cache MISS:
       invoke consent_handler.handle_consent(tool_name, arg_summary, reason)
       if APPROVE:
           append AuditLogEntry(consent_decision="UserApproved")
           proceed to tool call.
       if APPROVE_FOR_SESSION:
           consent_cache.set(key, APPROVE_FOR_SESSION)
           append AuditLogEntry(consent_decision="UserApprovedForSession")
           proceed to tool call.
       if REJECT:
           append AuditLogEntry(consent_decision="UserRejected")
           raise PolicyViolationError(consent_decision="UserRejected").
```

**Why no TTL — design rationale:**

Wall-clock TTLs introduce non-determinism into policy enforcement (the same
code executed at different times behaves differently), which is at odds with
NFR-2 (synchronous, deterministic policy evaluation).  Session-boundary
expiry is the correct scope because it mirrors the lifecycle of the execution
plan itself: a new user query starts a new session, requiring fresh consent.

---

### Decision 6 — Extended `AuditLogEntry` for consent decisions

**Decision:** Extend the `consent_decision` field of `AuditLogEntry` to
accommodate the new `ConsentDecision` variants.

**Updated `AuditLogEntry` schema:**

```python
@dataclass(frozen=True)
class AuditLogEntry:
    """Immutable audit log entry written for every policy evaluation.

    Attributes
    ----------
    tool_name:
        Registered name of the tool whose call triggered the entry.
    outcome:
        ``"Allowed"`` or ``"Denied"`` — the raw policy engine result.
    reason:
        ``Denied.reason`` string, or ``None`` when ``outcome="Allowed"``.
    timestamp:
        ISO-8601 UTC timestamp of the evaluation event.
    consent_decision:
        Consent resolution when ``enforcement_mode=PRODUCTION`` and the
        policy returned ``Denied``.  ``None`` in all other cases.

        Possible non-None values:

        ============================  ===========================================
        Value                         Meaning
        ============================  ===========================================
        ``"UserApproved"``            User approved this specific call.
        ``"UserApprovedForSession"``  User approved; decision cached for session.
        ``"UserRejected"``            User rejected the call.
        ``"CacheHit"``                Session cache returned a prior approval;
                                      no user interaction.
        ============================  ===========================================
    argument_summary:
        Human-readable summary of the tool call arguments (same string
        passed to ``ConsentHandler.handle_consent``).  ``None`` when
        ``consent_decision`` is ``None`` (``EVALUATION`` mode).
    """

    tool_name: str
    outcome: Literal["Allowed", "Denied"]
    reason: str | None
    timestamp: str
    consent_decision: Literal[
        "UserApproved",
        "UserApprovedForSession",
        "UserRejected",
        "CacheHit",
        None,
    ]
    argument_summary: str | None = None
```

**JSON serialisation example (NFR-6 compliance):**

```json
{
    "timestamp":        "2026-03-18T14:23:01.123456+00:00",
    "tool_name":        "send_email",
    "outcome":          "Denied",
    "reason":           "recipient address originates from untrusted data",
    "consent_decision": "UserApprovedForSession",
    "argument_summary": "send_email(to='alice@example.com' [sources: User literal])"
}
```

**Immutability rationale:**

`AuditLogEntry` is `frozen=True`.  Mutable log entries would allow policy
functions or tool implementations to tamper with the audit trail after the
fact, undermining the auditability guarantee (NFR-6).

**Backward compatibility:**

The `argument_summary` field has a default of `None`.  Existing code that
constructs `AuditLogEntry` without `argument_summary` continues to work.
The `consent_decision` literal type is extended (not replaced), so
exhaustiveness checks in downstream code only need to add the two new
literals (`"UserApprovedForSession"` and `"CacheHit"`).

---

### Decision 7 — Module placement and package exports

**Decision:**

| Class / Function              | Module                            | Exported via               |
|-------------------------------|-----------------------------------|----------------------------|
| `PolicyTestCase`              | `camel/testing/policy_runner.py`  | `camel.testing`            |
| `PolicyTestResult`            | `camel/testing/policy_runner.py`  | `camel.testing`            |
| `PolicyTestReport`            | `camel/testing/policy_runner.py`  | `camel.testing`            |
| `PolicyTestRunner`            | `camel/testing/policy_runner.py`  | `camel.testing`            |
| `CaMeLValueBuilder`           | `camel/testing/value_builder.py`  | `camel.testing`            |
| `PolicySimulator`             | `camel/testing/simulator.py`      | `camel.testing`            |
| `SimulatedPolicyTrigger`      | `camel/testing/simulator.py`      | `camel.testing`            |
| `SimulationReport`            | `camel/testing/simulator.py`      | `camel.testing`            |
| `ConsentDecision`             | `camel/consent.py`                | `camel.consent`, `camel_security` |
| `ConsentHandler`              | `camel/consent.py`                | `camel.consent`, `camel_security` |
| `CLIConsentHandler`           | `camel/consent.py`                | `camel.consent`, `camel_security` |
| `ConsentCacheKey`             | `camel/consent.py`                | `camel.consent`            |
| `compute_consent_key`         | `camel/consent.py`                | `camel.consent`            |
| `ConsentCache`                | `camel/consent.py`                | `camel.consent`            |

**Rationale for `camel/testing/` sub-package:**

Test utilities (`PolicyTestRunner`, `CaMeLValueBuilder`, `PolicySimulator`)
are placed under `camel/testing/` rather than `tests/harness/` so that
downstream `camel-security` users can import them for their own policy test
suites without reaching into the repository's `tests/` tree.

**Rationale for `camel/consent.py` (not `camel/testing/consent.py`):**

`ConsentDecision`, `ConsentHandler`, `CLIConsentHandler`, and `ConsentCache`
are production-runtime components, not test utilities.  They belong in the
core `camel` package and are exported via `camel_security` for SDK users.

---

## Interface Summary

### `PolicyTestCase`

```python
@dataclass(frozen=True)
class PolicyTestCase:
    description: str
    tool_name: str
    kwargs: dict[str, CaMeLValue]
    expected_outcome: Literal["Allowed", "Denied"]
    expected_reason_fragment: str | None = None
```

### `PolicyTestResult`

```python
@dataclass(frozen=True)
class PolicyTestResult:
    test_case: PolicyTestCase
    passed: bool
    actual_outcome: SecurityPolicyResult
    failure_reason: str | None = None
```

### `PolicyTestReport`

```python
@dataclass(frozen=True)
class PolicyTestReport:
    policy_name: str
    tool_name: str
    results: tuple[PolicyTestResult, ...]
    passed: int
    failed: int
    total: int
    coverage_notes: str | None = None

    @property
    def all_passed(self) -> bool: ...
```

### `PolicyTestRunner`

```python
class PolicyTestRunner:
    def run(
        self,
        policy_fn: PolicyFn,
        test_cases: Sequence[PolicyTestCase],
        *,
        policy_name: str = "",
    ) -> PolicyTestReport: ...
```

### `CaMeLValueBuilder`

```python
class CaMeLValueBuilder:
    def with_value(self, value: Any) -> Self: ...
    def with_sources(self, *sources: str) -> Self: ...
    def with_inner_source(self, inner_source: str) -> Self: ...
    def with_readers(self, *readers: str) -> Self: ...
    def with_public_readers(self) -> Self: ...
    def with_no_readers(self) -> Self: ...
    def with_dependency_chain(self, *dep_values: CaMeLValue) -> Self: ...
    def build(self) -> CaMeLValue: ...

    @classmethod
    def trusted(cls, value: Any, source: str = "User literal") -> Self: ...
    @classmethod
    def untrusted(cls, value: Any, source: str = "external_tool") -> Self: ...
    @classmethod
    def from_tool(cls, value: Any, tool_name: str) -> Self: ...
```

### `SimulatedPolicyTrigger`

```python
@dataclass(frozen=True)
class SimulatedPolicyTrigger:
    tool_name: str
    argument_snapshot: dict[str, str]    # {arg_name: repr(raw)[:80]}
    policy_name: str
    outcome: SecurityPolicyResult
    tier: PolicyTier | None
```

### `SimulationReport`

```python
@dataclass(frozen=True)
class SimulationReport:
    code_plan: str
    triggered_policies: tuple[SimulatedPolicyTrigger, ...]
    suppressed_tool_calls: tuple[str, ...]
    would_have_succeeded: bool
    denial_triggers: tuple[SimulatedPolicyTrigger, ...]
```

### `PolicySimulator`

```python
class PolicySimulator:
    def __init__(
        self,
        policy_registry: PolicyRegistry,
        tool_names: list[str],
    ) -> None: ...

    def simulate(
        self,
        code_plan: str,
        variable_store: dict[str, CaMeLValue] | None = None,
    ) -> SimulationReport: ...
```

### `ConsentDecision`

```python
class ConsentDecision(Enum):
    APPROVE              = "Approve"
    REJECT               = "Reject"
    APPROVE_FOR_SESSION  = "ApproveForSession"
```

### `ConsentHandler` Protocol

```python
class ConsentHandler(Protocol):
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision: ...
```

### `CLIConsentHandler`

```python
class CLIConsentHandler:
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision: ...
```

### `ConsentCacheKey`

```python
@dataclass(frozen=True)
class ConsentCacheKey:
    tool_name: str
    argument_hash: str    # SHA-256 hex digest

def compute_consent_key(
    tool_name: str,
    kwargs: dict[str, CaMeLValue],
) -> ConsentCacheKey: ...
```

### `ConsentCache`

```python
class ConsentCache:
    def get(self, key: ConsentCacheKey) -> ConsentDecision | None: ...
    def set(self, key: ConsentCacheKey, decision: ConsentDecision) -> None: ...
    def invalidate(self, key: ConsentCacheKey) -> None: ...
    def clear(self) -> None: ...
    def __len__(self) -> int: ...
```

### `AuditLogEntry` (updated)

```python
@dataclass(frozen=True)
class AuditLogEntry:
    tool_name:        str
    outcome:          Literal["Allowed", "Denied"]
    reason:           str | None
    timestamp:        str
    consent_decision: Literal[
                          "UserApproved",
                          "UserApprovedForSession",
                          "UserRejected",
                          "CacheHit",
                          None,
                      ]
    argument_summary: str | None = None
```

---

## PRD Section Updates

### PRD §6.5 — Security Policies (updated)

The following developer tools and consent components are added to PRD §6.5:

**Developer testing tools** (`camel.testing`):

| Tool                 | Description                                                                      |
|----------------------|----------------------------------------------------------------------------------|
| `PolicyTestRunner`   | Evaluates a policy function against `PolicyTestCase` instances; returns `PolicyTestReport` with pass/fail counts and per-case failure reasons. |
| `CaMeLValueBuilder`  | Fluent builder for constructing `CaMeLValue` test fixtures without a live interpreter; supports multi-hop dependency chain propagation. |
| `PolicySimulator`    | Dry-run execution of a code plan against registered policies, suppressing all side-effecting tool calls and returning a `SimulationReport` listing triggered policies and their outcomes. |

**Consent subsystem** (`camel.consent`):

| Component          | Description                                                                            |
|--------------------|----------------------------------------------------------------------------------------|
| `ConsentDecision`  | Enum with three members: `APPROVE`, `REJECT`, `APPROVE_FOR_SESSION`.                  |
| `ConsentHandler`   | Protocol with single method `handle_consent(tool, arg_summary, denial_reason) -> ConsentDecision`. |
| `CLIConsentHandler`| Default CLI implementation that prints a formatted prompt and reads user input.        |
| `ConsentCache`     | In-memory session-scoped cache keyed on `(tool_name, argument_hash)`; populated on `APPROVE_FOR_SESSION` decisions. |

### PRD §9 — NFR-6 (updated)

NFR-6 is updated to explicitly include consent decision entries:

> **NFR-6 (Observability):** All tool calls, policy evaluation outcomes
> (Allowed and Denied), consent decisions (UserApproved, UserApprovedForSession,
> UserRejected, CacheHit), exception redaction events, and allowlist violation
> events must be written as immutable entries to the security audit log.  Each
> consent-related `AuditLogEntry` must include: `tool_name`, `outcome`,
> `reason`, `timestamp`, `consent_decision`, and `argument_summary`.

---

## Consequences

### Positive

- `PolicyTestRunner` + `CaMeLValueBuilder` give policy authors a test loop
  that requires no live interpreter, LLM, or network access — satisfying NFR-9.
- `PolicySimulator` enables pre-deployment validation of policies against
  realistic execution plans without triggering side-effecting tools.
- `ConsentDecision.APPROVE_FOR_SESSION` + `ConsentCache` directly addresses
  PRD Risk L4 (user fatigue) by reducing repeat prompts for identical tool
  calls within a session.
- `CLIConsentHandler` provides a ready-to-use default for terminal-based
  deployments; the `ConsentHandler` Protocol ensures all UI backends are
  interchangeable.
- The extended `AuditLogEntry` (with `argument_summary` and new
  `consent_decision` literals) satisfies NFR-6 without breaking existing code
  that constructs entries without `argument_summary` (default `None`).

### Negative / Trade-offs

- The `argument_summary` field in `AuditLogEntry` truncates raw values at 80
  characters, which may be insufficient for debugging complex policy denials.
  Callers requiring full argument values should inspect `CaMeLValue.raw`
  directly before the tool call rather than relying on the audit log.
- The session cache's argument hash is computed from `repr()`, which is not
  cryptographically collision-resistant.  An adversary with the ability to
  craft argument values that produce hash collisions could bypass re-consent.
  This is considered acceptable because: (a) the adversary must already control
  the tool call arguments (a more serious breach), and (b) the session boundary
  limits the blast radius.
- `PolicySimulator` stubs return trusted `CaMeLValue` results; this means
  simulated runs may not faithfully reproduce capability-propagation paths that
  depend on the actual tool return value.  Platform teams should treat
  simulation as "necessary but not sufficient" validation.

---

## Related ADRs

| ADR                                                     | Topic                                       |
|---------------------------------------------------------|---------------------------------------------|
| [002](002-camelvalue-capability-system.md)              | `CaMeLValue` capability propagation         |
| [009](009-policy-engine-architecture.md)                | Policy engine and `PolicyRegistry`          |
| [010](010-enforcement-hook-consent-audit-harness.md)    | Enforcement hook, consent flow, audit log   |
| [011](011-three-tier-policy-governance.md)              | Three-tier policy governance                |
