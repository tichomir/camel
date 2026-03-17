# ADR-010: Enforcement Hook, Consent Flow, Audit Log, and Policy Testing Harness

| Field         | Value                                                                          |
|---------------|--------------------------------------------------------------------------------|
| Status        | Accepted                                                                       |
| Date          | 2026-03-17                                                                     |
| Author        | Software Architect Persona                                                     |
| Supersedes    | —                                                                              |
| Superseded by | —                                                                              |
| Related       | ADR-003 (AST Interpreter), ADR-009 (Policy Engine), ADR-007 (Execution Loop)  |

---

## Context

Milestone 3 (Phase: Enforcement Integration & Consent Flow) closes the enforcement loop
by wiring the existing `PolicyRegistry` into the interpreter's tool-call pre-execution
hook, introducing dual-mode enforcement, building the security audit log, and delivering
a policy testing harness.

The following existing building blocks are in place and must be extended — **not**
replaced — by this ADR:

| Component                     | Location                                | State     |
|-------------------------------|-----------------------------------------|-----------|
| `PolicyViolationError`        | `camel/interpreter.py`                  | ✅ Exists  |
| `AuditLogEntry`               | `camel/interpreter.py`                  | ✅ Exists  |
| Pre-execution policy check    | `CaMeLInterpreter._eval_Call`           | ✅ Exists  |
| `PolicyRegistry.evaluate()`   | `camel/policy/interfaces.py`            | ✅ Exists  |
| `CaMeLInterpreter.audit_log`  | `camel/interpreter.py`                  | ✅ Exists  |

Gaps that must be addressed:

| Gap                               | NFR      |
|-----------------------------------|----------|
| Dual-mode enforcement             | NFR-2, NFR-9 |
| Consent prompt contract           | NFR-6    |
| `consent_decision` audit field    | NFR-6    |
| Policy testing harness            | NFR-9    |
| NFR-4 timing compliance           | NFR-4    |

---

## Decisions

### Decision 1 — Dual-mode enforcement via `EnforcementMode` enum

**Decision:** Add an `EnforcementMode` enum with two members:

```python
class EnforcementMode(Enum):
    EVALUATION = "evaluation"   # raises PolicyViolationError immediately
    PRODUCTION  = "production"  # invokes consent callback before raising
```

`CaMeLInterpreter` gains an `enforcement_mode: EnforcementMode` constructor
parameter (default `EnforcementMode.EVALUATION`) and a matching `ConsentCallback`
optional constructor parameter.

**Mode-switching mechanism:**

```python
interp = CaMeLInterpreter(
    tools=...,
    policy_engine=registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_callback=my_consent_fn,
)
```

`EnforcementMode` is passed at construction time, not mutated at runtime, to keep
the interpreter deterministic within a single execution session.

**Why not a context variable (`contextvars.ContextVar`):**

A `ContextVar` would allow enforcement mode to be silently switched by tool code or
Q-LLM outputs, creating a side channel for disabling enforcement.  An immutable
constructor parameter eliminates this attack surface.

**Why not a module-level global:**

Module-level globals make parallel test execution fragile and violate NFR-9
(independent testability of each component).

---

### Decision 2 — Consent callback protocol

**Decision:** Define a `ConsentCallback` as a `Protocol` (structural typing):

```python
from typing import Protocol

class ConsentCallback(Protocol):
    def __call__(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> bool:
        """Return True to approve the call, False to reject it."""
        ...
```

**Required fields surfaced to the callback:**

| Field              | Type  | Description                                               |
|--------------------|-------|-----------------------------------------------------------|
| `tool_name`        | `str` | Name of the tool being called                             |
| `argument_summary` | `str` | Human-readable summary of the arguments (see §D3 below)  |
| `denial_reason`    | `str` | Verbatim `Denied.reason` string from the policy engine    |

**Return contract:**

- `True` → the tool call proceeds; a `"UserApproved"` consent entry is written to
  the audit log.
- `False` → the tool call is cancelled; a `"UserRejected"` consent entry is written;
  `PolicyViolationError` is raised exactly as in `EVALUATION` mode.

**Why a Protocol (structural subtyping) rather than an ABC:**

Any callable matching the signature is accepted without requiring explicit
inheritance.  This makes it trivial to inject a lambda, a mock, or a curried
function in tests, satisfying NFR-9.

**Default consent callback when `enforcement_mode=PRODUCTION` and no callback
supplied:**

Raise `ValueError` at construction time.  Silently ignoring a missing callback
in production mode would be a security defect.

---

### Decision 3 — Argument summary generation

**Decision:** Add a module-level helper
`_summarise_args(tool_name: str, kwargs_mapping: dict[str, CaMeLValue]) -> str`
inside `camel/interpreter.py`.

**Algorithm:**

1. For each `(key, cv)` in `kwargs_mapping.items()`:
   - `raw_repr = repr(cv.raw)[:80]`  (truncate at 80 chars to prevent log bloat)
   - `sources_str = ", ".join(sorted(cv.sources)) or "(none)"`
   - Format as `f"{key}={raw_repr} [sources: {sources_str}]"`
2. Join all formatted pairs with `"; "`.
3. Return `f"{tool_name}({joined})"`.

**Rationale:**

- Truncation at 80 chars prevents oversized prompts from sensitive data leaking
  into the consent UI.
- Including `sources` helps the user understand *why* the policy denied the call
  (e.g. "recipient address comes from untrusted email body").

---

### Decision 4 — Enforcement hook invocation sequence

**Decision:** The pre-execution hook in `CaMeLInterpreter._eval_Call` follows this
exact sequence for every registered-tool call:

```
1.  Evaluate arguments → pos_arg_cvs, kw_arg_cvs
2.  Build kwargs_mapping (positional args mapped to param names)
3.  Call _policy_engine.evaluate(tool_name, kwargs_mapping)
4a. If Allowed:
      append AuditLogEntry(outcome="Allowed", consent_decision=None)
      proceed to tool call
4b. If Denied (EVALUATION mode):
      append AuditLogEntry(outcome="Denied", consent_decision=None)
      raise PolicyViolationError(tool_name, reason)
4c. If Denied (PRODUCTION mode):
      generate argument_summary via _summarise_args()
      invoke consent_callback(tool_name, argument_summary, denial_reason)
      if callback returns True:
        append AuditLogEntry(outcome="Denied", consent_decision="UserApproved")
        proceed to tool call
      if callback returns False:
        append AuditLogEntry(outcome="Denied", consent_decision="UserRejected")
        raise PolicyViolationError(tool_name, reason)
5.  Call tool with raw argument values
6.  Assert result is CaMeLValue; raise TypeError otherwise
7.  Return result_cv
```

**Blocking contract:** If step 3 returns `Denied` and the consent callback (step 4c)
returns `False` (or mode is `EVALUATION`), the tool function is **never called**.
Steps 5–7 are only reached when policy evaluation results in a final allow (either
original `Allowed` or `Denied` + user approval).

**Synchrony requirement:** The consent callback is synchronous (NFR-2).  Async UI
frameworks must bridge to sync using `asyncio.run()` or equivalent, outside the
interpreter.

---

### Decision 5 — Audit log schema

**Decision:** Extend `AuditLogEntry` with a `consent_decision` field.

**Final schema:**

```python
@dataclass(frozen=True)
class AuditLogEntry:
    tool_name: str
    outcome: Literal["Allowed", "Denied"]
    reason: str | None          # None when outcome == "Allowed"
    timestamp: str              # ISO-8601 UTC, e.g. "2026-03-17T14:23:01.123456+00:00"
    consent_decision: Literal["UserApproved", "UserRejected"] | None
    # None when enforcement_mode == EVALUATION, or when outcome == "Allowed"
```

**JSON serialisation (for NFR-6 audit sinks):**

```json
{
  "timestamp":        "2026-03-17T14:23:01.123456+00:00",
  "tool_name":        "send_email",
  "outcome":          "Denied",
  "reason":           "recipient address originates from untrusted data",
  "consent_decision": "UserApproved"
}
```

**Field semantics:**

| Field              | Allowed            | Denied+no consent  | Denied+approved    | Denied+rejected    |
|--------------------|--------------------|--------------------|--------------------|--------------------|
| `outcome`          | `"Allowed"`        | `"Denied"`         | `"Denied"`         | `"Denied"`         |
| `reason`           | `None`             | denial reason str  | denial reason str  | denial reason str  |
| `consent_decision` | `None`             | `None`             | `"UserApproved"`   | `"UserRejected"`   |

**Why `frozen=True` on the dataclass:**

Audit entries must be immutable after creation.  Mutable log entries would allow
tool implementations or policy functions to tamper with the audit trail.

**Persistence / NFR-6 compliance:**

`CaMeLInterpreter` stores audit entries in-memory (`_audit_log: list[AuditLogEntry]`).
Callers are responsible for persisting entries to an external sink (file, structured
log, SIEM).  A helper `camel.audit.write_jsonl(entries, path)` utility is out of scope
for this ADR but is the recommended sink interface.

---

### Decision 6 — PolicyViolationError structure

**Decision:** Extend `PolicyViolationError` (already defined in `camel/interpreter.py`)
with a `consent_decision` field:

```python
@dataclass
class PolicyViolationError(Exception):
    tool_name: str
    reason: str
    consent_decision: Literal["UserRejected"] | None = None
    # None → EVALUATION mode denial
    # "UserRejected" → PRODUCTION mode, user explicitly rejected
```

**Rationale:**

Callers (e.g. the execution loop orchestrator) need to distinguish between an
automatic evaluation-mode denial (which may trigger a P-LLM retry) and an explicit
user rejection (which should surface as a terminal error rather than retry).

---

### Decision 7 — Policy testing harness API

**Decision:** Deliver `tests/harness/policy_harness.py` (under the existing
`tests/harness/` package) with the following public API:

#### 7.1 Value factory helpers

```python
def make_trusted_value(
    raw: object,
    readers: frozenset[str] | type[Public] = Public,
) -> CaMeLValue:
    """Return a CaMeLValue with sources={'User literal'} (trusted)."""

def make_untrusted_value(
    raw: object,
    source: str = "external_tool",
    readers: frozenset[str] | type[Public] = Public,
) -> CaMeLValue:
    """Return a CaMeLValue with sources={source} (untrusted)."""

def make_mixed_value(
    raw: object,
    trusted_sources: frozenset[str],
    untrusted_sources: frozenset[str],
    readers: frozenset[str] | type[Public] = Public,
) -> CaMeLValue:
    """Return a CaMeLValue with sources = trusted_sources | untrusted_sources."""
```

**Why three separate factories rather than one generic factory:**

Each factory has a single, readable intent.  Test code reading
`make_trusted_value("alice@example.com")` is self-documenting; a generic
`make_value(..., trusted=True)` is more error-prone.

#### 7.2 Policy assertion helpers

```python
def assert_allowed(
    registry: PolicyRegistry,
    tool_name: str,
    kwargs: dict[str, CaMeLValue],
) -> None:
    """Assert that registry.evaluate returns Allowed; raise AssertionError otherwise."""

def assert_denied(
    registry: PolicyRegistry,
    tool_name: str,
    kwargs: dict[str, CaMeLValue],
    *,
    reason_contains: str | None = None,
) -> Denied:
    """Assert that registry.evaluate returns Denied.

    Optionally assert that Denied.reason contains reason_contains (case-insensitive).
    Returns the Denied result for further assertions.
    """
```

#### 7.3 AgentDojo scenario replay

```python
@dataclass
class AgentDojoScenario:
    """A single adversarial scenario from the AgentDojo benchmark."""
    scenario_id: str
    tool_name: str
    trusted_kwargs: dict[str, object]   # values the user provided / are trusted
    injected_kwargs: dict[str, object]  # values injected by adversary
    expected_outcome: Literal["Allowed", "Denied"]
    expected_reason_fragment: str | None = None

def replay_agentdojo_scenario(
    registry: PolicyRegistry,
    scenario: AgentDojoScenario,
) -> None:
    """Run an AgentDojo adversarial scenario against the registry.

    Constructs CaMeLValue wrappers:
      - trusted_kwargs → make_trusted_value for each value
      - injected_kwargs → make_untrusted_value for each value
    Merges both into a single kwargs mapping (injected wins on key collision).
    Asserts the expected outcome.
    """
```

**Pre-defined scenario catalogue:**

A module-level constant `AGENTDOJO_SCENARIOS: list[AgentDojoScenario]` in
`tests/harness/policy_harness.py` contains at least one scenario per reference
policy (six total), covering the primary AgentDojo attack classes mapped in
`docs/policies/reference-policy-spec.md`.

---

### Decision 8 — NFR-4 timing compliance verification

**Decision:** Add a dedicated timing assertion in `tests/test_policy_enforcement.py`:

```python
def test_policy_evaluation_overhead_under_100ms():
    """Policy evaluation (non-LLM) must complete in ≤100ms per NFR-4."""
    registry = build_full_reference_registry()   # all 6 reference policies
    kwargs = {k: make_untrusted_value(v) for k, v in WORST_CASE_KWARGS.items()}

    start = time.perf_counter()
    for _ in range(1000):
        registry.evaluate("send_email", kwargs)
    elapsed_ms = (time.perf_counter() - start) / 1000 * 1000

    assert elapsed_ms < 100, f"Policy evaluation took {elapsed_ms:.2f}ms (limit 100ms)"
```

A single `evaluate()` call over all six reference policies is expected to complete
in well under 1ms.  The test uses 1000-iteration averaging to reduce timing noise.

---

## Interface Summary

### `EnforcementMode` (new, in `camel/interpreter.py`)

```python
class EnforcementMode(Enum):
    EVALUATION = "evaluation"
    PRODUCTION  = "production"
```

### `ConsentCallback` Protocol (new, in `camel/interpreter.py`)

```python
class ConsentCallback(Protocol):
    def __call__(self, tool_name: str, argument_summary: str, denial_reason: str) -> bool: ...
```

### `CaMeLInterpreter` constructor additions

```python
CaMeLInterpreter(
    tools: dict[str, Callable[..., CaMeLValue]],
    policy_engine: PolicyRegistry | None = None,
    mode: ExecutionMode = ExecutionMode.NORMAL,
    enforcement_mode: EnforcementMode = EnforcementMode.EVALUATION,
    consent_callback: ConsentCallback | None = None,
)
```

`ValueError` is raised at construction time if
`enforcement_mode=PRODUCTION` and `consent_callback=None`.

### `AuditLogEntry` (extended, in `camel/interpreter.py`)

```python
@dataclass(frozen=True)
class AuditLogEntry:
    tool_name: str
    outcome: Literal["Allowed", "Denied"]
    reason: str | None
    timestamp: str
    consent_decision: Literal["UserApproved", "UserRejected"] | None
```

### `PolicyViolationError` (extended, in `camel/interpreter.py`)

```python
@dataclass
class PolicyViolationError(Exception):
    tool_name: str
    reason: str
    consent_decision: Literal["UserRejected"] | None = None
```

### `tests/harness/policy_harness.py` (new)

Public exports:

```python
__all__ = [
    "make_trusted_value",
    "make_untrusted_value",
    "make_mixed_value",
    "assert_allowed",
    "assert_denied",
    "AgentDojoScenario",
    "replay_agentdojo_scenario",
    "AGENTDOJO_SCENARIOS",
]
```

---

## Consequences

### Positive

- Dual-mode enforcement cleanly separates automated benchmarking (`EVALUATION`)
  from production use (`PRODUCTION`) without any shared mutable state.
- `ConsentCallback` as a Protocol enables zero-dependency mocking in tests.
- Extending `AuditLogEntry` with `consent_decision` satisfies NFR-6 without
  breaking existing test assertions that do not check this field (dataclass
  field has default `None`).
- The testing harness provides a one-call API for every common policy test
  pattern, lowering the barrier for policy authors to write thorough test suites.
- `PolicyViolationError.consent_decision` allows the execution loop orchestrator
  to distinguish terminal rejections from automatic evaluation denials and apply
  different retry logic.

### Negative / Trade-offs

- Adding `consent_decision` to `AuditLogEntry` as a non-optional field (even
  with `None` default) requires all existing code that constructs `AuditLogEntry`
  literals to be updated.  There are two call sites in `_eval_Call`; both must
  be updated to pass `consent_decision=None`.
- The synchronous `ConsentCallback` contract means production UIs built on async
  frameworks must use a sync bridge.  This is a known limitation documented in
  Decision 4.
- `AGENTDOJO_SCENARIOS` is a static catalogue; it will drift from the live
  AgentDojo benchmark unless actively maintained.

---

## Related ADRs

| ADR                                              | Topic                                |
|--------------------------------------------------|--------------------------------------|
| [003](003-ast-interpreter-architecture.md)       | AST interpreter — tool dispatch path |
| [009](009-policy-engine-architecture.md)         | Policy engine and registry           |
| [007](007-execution-loop-orchestrator.md)        | Execution loop — error handling      |
| [002](002-camelvalue-capability-system.md)       | CaMeLValue capability propagation    |
