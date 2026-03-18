# Security Audit Log Reference

_CaMeL v0.5.0 | Last updated: 2026-03-18_

CaMeL maintains several immutable audit log streams inside `CaMeLInterpreter`
to satisfy NFR-6: all tool calls, policy evaluations, consent decisions,
capability assignments, and exception redaction events must be recorded.

---

## Log Streams Overview

| Property | Type | Description |
|---|---|---|
| `interp.audit_log` | `list[AuditLogEntry]` | One entry per policy evaluation (every tool call) |
| `interp.consent_audit_log` | `list[ConsentAuditEntry]` | One entry per consent prompt (including cache hits) |
| `interp.security_audit_log` | `list[ForbiddenImportEvent \| ForbiddenNameEvent \| DataToControlFlowAuditEvent]` | Security violation events (forbidden imports, names, escalation detections) |
| `interp.strict_dep_audit_log` | `list[StrictDependencyAdditionEvent]` | Per-statement STRICT mode dependency additions |

Each stream is a read-only copy (returns `list(self._internal_log)`).

---

## `AuditLogEntry` — Policy Evaluation Log

One entry is appended for **every** call to `PolicyRegistry.evaluate`,
regardless of outcome.  This covers all tool calls made during plan execution.

```python
from camel.interpreter import CaMeLInterpreter

interp = CaMeLInterpreter(tools=my_tools, policy_engine=my_registry)
interp.exec(plan)

for entry in interp.audit_log:
    print(entry.tool_name, entry.outcome, entry.reason)
```

### Schema

| Field | Type | Description |
|---|---|---|
| `tool_name` | `str` | Name of the tool whose call was evaluated |
| `outcome` | `Literal["Allowed", "Denied"]` | Policy evaluation outcome |
| `reason` | `str \| None` | Denial reason string; `None` when allowed |
| `timestamp` | `str` | UTC ISO-8601 timestamp of the policy evaluation |
| `consent_decision` | `Literal["UserApproved", "UserRejected"] \| None` | Set only in `PRODUCTION` mode after a consent prompt; `None` in `EVALUATION` mode and on allowed calls |
| `authoritative_tier` | `str \| None` | The policy tier (`"Platform"`, `"ToolProvider"`, `"User"`) that produced the final decision in the three-tier model; `None` for single-tier registries |
| `non_overridable_denial` | `bool` | `True` when the denial came from a Platform-tier `non-overridable` policy; `False` otherwise |

### `consent_decision` values

| Value | Meaning |
|---|---|
| `None` | Call was allowed by policy, or interpreter is in `EVALUATION` mode |
| `"UserApproved"` | Policy denied the call; user approved it via the consent handler |
| `"UserRejected"` | Policy denied the call; user rejected it — `PolicyViolationError` raised |

---

## `ConsentAuditEntry` — Consent Decision Log

One entry is appended for **every consent prompt** that fires, whether the
decision came from the user (via `ConsentHandler`) or from the session cache
(via `ConsentDecisionCache`).

```python
from camel.interpreter import CaMeLInterpreter, EnforcementMode
from camel_security.consent import DefaultCLIConsentHandler, ConsentDecisionCache

interp = CaMeLInterpreter(
    tools=my_tools,
    policy_engine=my_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_handler=DefaultCLIConsentHandler(),
    consent_cache=ConsentDecisionCache(),
)
interp.exec(plan)

for entry in interp.consent_audit_log:
    cache_label = "(cached)" if entry.session_cache_hit else "(prompted)"
    print(
        f"{entry.timestamp}  {entry.tool_name}  "
        f"{entry.decision.value}  {cache_label}"
    )
```

### Schema

| Field | Type | Description |
|---|---|---|
| `decision` | `ConsentDecision` | The consent outcome: `APPROVE`, `REJECT`, or `APPROVE_FOR_SESSION` |
| `timestamp` | `str` | UTC ISO-8601 timestamp of the consent decision, e.g. `"2026-03-18T12:00:00.000000+00:00"` |
| `tool_name` | `str` | Name of the tool whose call triggered the consent prompt |
| `argument_summary` | `str` | Human-readable summary of the tool's arguments — contains only value types and provenance labels, never raw sensitive data (e.g. `"to: str (User literal); body: str (read_email)"`) |
| `session_cache_hit` | `bool` | `True` when the decision was retrieved from `ConsentDecisionCache` without invoking the handler; `False` when the handler was actually called |

### `ConsentDecision` values

| Value | Meaning |
|---|---|
| `APPROVE` | User approved this specific invocation once; not cached |
| `REJECT` | User rejected this invocation; `PolicyViolationError` raised |
| `APPROVE_FOR_SESSION` | User approved and requested future identical calls be auto-approved for the session |

### Example entry

```python
from camel.consent import ConsentAuditEntry, ConsentDecision

entry = ConsentAuditEntry(
    decision=ConsentDecision.APPROVE_FOR_SESSION,
    timestamp="2026-03-18T12:00:00.000000+00:00",
    tool_name="send_email",
    argument_summary="to: str (User literal); body: str (read_email)",
    session_cache_hit=False,
)
assert entry.decision is ConsentDecision.APPROVE_FOR_SESSION
assert not entry.session_cache_hit
```

---

## `ForbiddenImportEvent` — Import Violation Log

Appended to `security_audit_log` whenever a `ForbiddenImportError` is raised
(M4-F10, M4-F13).

### Schema

| Field | Type | Description |
|---|---|---|
| `event_type` | `Literal["ForbiddenImport"]` | Fixed discriminant |
| `module_name` | `str` | Module name from the `import` or `from … import` statement |
| `line_number` | `int` | 1-based source line number of the offending statement |
| `timestamp` | `str` | UTC ISO-8601 timestamp |
| `error_message` | `str` | String representation of the raised `ForbiddenImportError` |

---

## `ForbiddenNameEvent` — Disallowed Name Access Log

Appended to `security_audit_log` whenever a `ForbiddenNameError` is raised
(M4-F14).  Covers both off-allowlist builtins and timing primitives (M4-F12).

### Schema

| Field | Type | Description |
|---|---|---|
| `event_type` | `Literal["ForbiddenNameAccess"]` | Fixed discriminant |
| `offending_name` | `str` | The identifier that was not in the allowlist |
| `line_number` | `int` | 1-based source line number of the name access |
| `timestamp` | `str` | UTC ISO-8601 timestamp |
| `is_timing_primitive` | `bool` | `True` when the name is in the `excluded_timing_names` list in `allowlist.yaml` (timing side-channel mitigation) |
| `error_message` | `str` | String representation of the raised `ForbiddenNameError` |

---

## `DataToControlFlowAuditEvent` — Escalation Detection Log

Appended to `security_audit_log` when M4-F15 detects that a tool name
resolution depends on untrusted data (data-to-control-flow escalation).

### Schema

| Field | Type | Description |
|---|---|---|
| `event_type` | `Literal["DataToControlFlow"]` | Fixed discriminant |
| `line_number` | `int` | Line number of the offending `Call` expression |
| `timestamp` | `str` | UTC ISO-8601 timestamp |
| `untrusted_sources` | `frozenset[str]` | Source labels of the untrusted data influencing the tool name |
| `tool_name_candidate` | `str \| None` | The raw tool name expression (if statically resolvable) |
| `elevated_consent_approved` | `bool` | `True` if M4-F16 elevated consent was approved; `False` if rejected or not prompted |

---

## `StrictDependencyAdditionEvent` — STRICT Mode Trace Log

Appended to `strict_dep_audit_log` for every per-statement dependency addition
in STRICT mode (M4-F18), providing a post-execution trace of how STRICT mode
taint propagated through the plan.

### Schema

| Field | Type | Description |
|---|---|---|
| `variable_name` | `str` | Variable whose dependency set was extended |
| `added_deps` | `frozenset[str]` | Dependency labels added by the STRICT rule |
| `rule` | `str` | Which STRICT rule triggered: `"for_iterable"`, `"if_test"`, or `"post_qllm"` |
| `line_number` | `int` | Source line number of the assignment |
| `timestamp` | `str` | UTC ISO-8601 timestamp |

---

## `RedactionAuditEvent` — Exception Redaction Log

Emitted by `ExceptionRedactor.classify()` for every exception it processes
(M4-F17).  Stored inside the `CaMeLOrchestrator`'s redaction audit sink (not
directly on the interpreter).

### Schema

| Field | Type | Description |
|---|---|---|
| `timestamp` | `str` | UTC ISO-8601 timestamp |
| `line_number` | `int` | Source line number of the failing statement |
| `redaction_reason` | `str` | One of `"untrusted_dependency"`, `"not_enough_information"`, `"loop_body_exception"`, `"none"` |
| `dependency_chain` | `list[tuple[str, str]]` | Up to 50 `(variable_name, source_label)` pairs from the dependency graph at the point of failure |
| `trust_level` | `str` | Computed trust level of the failing statement's variables |
| `error_type` | `str` | Exception class name (e.g. `"ValueError"`, `"NotEnoughInformationError"`) |
| `redacted_message_length` | `int` | Length of the original exception message (before redaction) |
| `m4_f9_applied` | `bool` | `True` when M4-F9 loop-body exception STRICT propagation was applied |

---

## Reading All Logs in One Pass

```python
from camel.interpreter import CaMeLInterpreter, EnforcementMode, AuditLogEntry
from camel.consent import ConsentAuditEntry
from camel_security.consent import DefaultCLIConsentHandler, ConsentDecisionCache

interp = CaMeLInterpreter(
    tools=my_tools,
    policy_engine=my_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_handler=DefaultCLIConsentHandler(),
    consent_cache=ConsentDecisionCache(),
)
interp.exec(plan)

print("=== Policy Evaluation Log ===")
for entry in interp.audit_log:
    print(f"  {entry.timestamp}  {entry.tool_name}  {entry.outcome}")

print("\n=== Consent Decision Log ===")
for entry in interp.consent_audit_log:
    cache = " (cached)" if entry.session_cache_hit else ""
    print(f"  {entry.timestamp}  {entry.tool_name}  {entry.decision.value}{cache}")

print("\n=== Security Violation Log ===")
for event in interp.security_audit_log:
    print(f"  {event.timestamp}  {event.event_type}")

print("\n=== STRICT Mode Dependency Trace ===")
for ev in interp.strict_dep_audit_log:
    print(f"  L{ev.line_number}  {ev.variable_name}  +{ev.added_deps}  [{ev.rule}]")
```

---

## NFR-6 Compliance Summary

NFR-6 requires all of the following to be recorded in the security audit log:

| Event | Log stream | Status |
|---|---|---|
| Every tool call (allowed or denied) | `audit_log` | ✅ |
| Policy evaluation outcome (tool, policy, result, reason) | `audit_log` | ✅ |
| User consent decision (approved/rejected, cache hit) | `consent_audit_log` | ✅ |
| Exception redaction events (M4-F17) | `RedactionAuditEvent` via `ExceptionRedactor` | ✅ |
| Forbidden import violations (M4-F13) | `security_audit_log` | ✅ |
| Forbidden name access (M4-F14) | `security_audit_log` | ✅ |
| Data-to-control-flow escalation detection (M4-F15) | `security_audit_log` | ✅ |
| STRICT mode per-statement dependency additions (M4-F18) | `strict_dep_audit_log` | ✅ |

---

## See Also

- [Consent Handler Integration Guide](consent-handler-integration.md) — implementing custom `ConsentHandler`
- [Policy Authoring Tutorial](policy-authoring-tutorial.md) — writing and testing policies
- [Architecture Reference](architecture.md) — full system design
- [ADR-010 — Enforcement Hook, Consent Flow & Audit](adr/010-enforcement-hook-consent-audit-harness.md)
- [ADR-012 — Policy Testing Harness, ConsentHandler](adr/012-policy-testing-harness-consent-handler.md)
