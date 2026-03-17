# Milestone 4 — Exception Hardening & Redaction Design

_Author: Software Architect | Date: 2026-03-17 | Status: Implemented — all M4-F6 through M4-F9, M4-F17 features delivered and verified_

---

## 1. Overview

This document specifies the design for the exception hardening phase of Milestone 4.
The goal is to harden all exception-handling pathways in the CaMeL interpreter so that
no untrusted data — not even an error message — can reach the P-LLM through the exception
channel.

### 1.1 Threat: Exception-Based Information Leakage

Without hardening, an adversary controlling tool return values can engineer a scenario
where:

1. Untrusted content is assigned to a variable (e.g., from `get_last_email()`).
2. A subsequent operation on that variable raises an exception whose **message** contains
   a fragment of the untrusted content (e.g., `TypeError: expected str, got '<attacker payload>'`).
3. The exception message is forwarded verbatim to the P-LLM in the retry prompt.
4. The P-LLM, now carrying attacker-controlled text in its context, is susceptible to
   prompt injection via the exception channel.

This attack is orthogonal to control-flow injection — it does not require the attacker
to redirect the agent's behaviour directly.  It only requires that the adversary can
craft tool return values that trigger exceptions with informative messages.

### 1.2 Feature Register

| Feature ID | Description | Status |
|---|---|---|
| M4-F6 | Dependency-graph-aware exception message redaction | ✅ Implemented |
| M4-F7 | `NotEnoughInformationError` content stripping | ✅ Implemented |
| M4-F8 | STRICT mode annotation preservation across NEIE re-generation | ✅ Implemented |
| M4-F9 | Loop-body exception STRICT propagation | ✅ Implemented |
| M4-F17 | `RedactionAuditEvent` emission for every redaction decision | ✅ Implemented |

All features are implemented in `camel/execution_loop.py` (`ExceptionRedactor`,
`RedactedError`, `RedactionAuditEvent`, `AcceptedState`) and `camel/interpreter.py`
(`_exec_For`, `snapshot_dep_state`, `restore_dep_state`, `_is_non_public`).

---

## 2. Architecture

### 2.1 Component Map

```
CaMeLOrchestrator.run()
  │
  ├─► CaMeLInterpreter.exec()
  │     │
  │     ├─ [statement execution]
  │     │
  │     └─ exception raised
  │           │
  │           │ M4-F9: _exec_For attaches __loop_iter_deps__
  │           │        and __loop_iter_caps__ to exception
  │           │        when iterable is non-public in STRICT mode
  │           │
  │           ▼
  ├─► ExceptionRedactor.classify(exc, store_snapshot, interpreter)
  │     │
  │     ├─ M4-F7: isinstance(exc, NotEnoughInformationError)?
  │     │         → RedactedError(type, lineno, msg=None, "not_enough_information")
  │     │
  │     ├─ M4-F6: _is_tainted(exc, store, interpreter)?
  │     │         → RedactedError(type, lineno, msg=None, "untrusted")
  │     │
  │     ├─ else: trusted
  │     │         → RedactedError(type, lineno, msg=str(exc), "trusted")
  │     │
  │     └─ M4-F17: _emit_audit_event() → RedactionAuditEvent → audit_log
  │
  ├─► [build AcceptedState for retry]
  │     ├─ M4-F8: if NEIE — snapshot DependencyGraph + _dep_ctx_stack
  │     └─ M4-F9: if loop — attach loop_iter_deps + loop_iter_caps
  │
  └─► RetryPromptBuilder.build(accepted_state, redacted_error)
        → P-LLM receives only: error_type, lineno (+ static advisory for NEIE)
          Never: message content, untrusted data
```

### 2.2 Data Flow Isolation Invariant

After exception hardening, the P-LLM **never** receives untrusted data through the
exception channel.  The only information forwarded is:

| Case | Forwarded to P-LLM | Withheld |
|---|---|---|
| Trusted exception | error type, line number, full message | — |
| Untrusted-dependency exception | error type, line number | message (→ `[REDACTED]` in audit log) |
| `NotEnoughInformationError` | error type, line number, fixed advisory | all content |

---

## 3. M4-F6 — Dependency-Graph-Aware Exception Redaction

### 3.1 Motivation

When an exception occurs during execution, the error message may contain a fragment
of the value that caused the error.  If that value was derived from untrusted tool
output, the exception message is itself an untrusted data channel back to the P-LLM.

The naive defence — redacting all exceptions whenever the store contains any untrusted
value — is too conservative.  It would redact exceptions caused by P-LLM coding errors
(e.g., a `SyntaxError` or `NameError` that has nothing to do with untrusted data),
making the retry loop unhelpful.

M4-F6 applies a **dependency-graph-aware** taint check: for each variable in the
interpreter store, the full upstream dependency chain is walked.  If any upstream source
is outside `TRUSTED_SOURCES = frozenset({"User literal", "CaMeL"})`, the exception is
considered tainted and the message is redacted.

### 3.2 Taint Check Algorithm

```
TRUSTED_SOURCES = frozenset({"User literal", "CaMeL"})

_is_tainted(exc, store, interpreter) → (bool, dep_chain):

  dep_chain = []

  if interpreter is available:
    for var in store.keys():
      # Check the variable's own capability sources
      cv = store[var]
      for src in cv.sources:
        if src not in TRUSTED_SOURCES:
          dep_chain.append((var, src))

      # Walk the upstream dependency graph
      dg = interpreter.get_dependency_graph(var)
      for upstream_var in dg.all_upstream:
        upstream_cv = store[upstream_var]
        for src in upstream_cv.sources:
          if src not in TRUSTED_SOURCES:
            dep_chain.append((upstream_var, src))

    dep_chain = dep_chain[:50]   # cap at 50 entries
    return (bool(dep_chain), dep_chain)

  else (fallback — no interpreter reference):
    for var, cv in store.items():
      for src in cv.sources:
        if src not in TRUSTED_SOURCES:
          fallback_chain.append((var, src))
    return (bool(fallback_chain), fallback_chain[:50])
```

### 3.3 Redaction Decision

When taint is detected:

- `message` is set to `None` in the resulting `RedactedError`.
- The P-LLM retry prompt receives: `error_type` and `lineno` only.
- The `[REDACTED]` token appears only in the `RedactionAuditEvent.redaction_reason`
  field — never in the `RedactedError` data model itself.

### 3.4 Fallback Behaviour

When `ExceptionRedactor.classify()` is called without an `interpreter` reference
(backward-compatible path), a flat store-level scan is used: any variable in the
store carrying a non-trusted source triggers redaction.  This is more conservative
than the graph-aware check but always safe.

### 3.5 Implementation Location

`camel/execution_loop.py` — `ExceptionRedactor._is_tainted()`

---

## 4. M4-F7 — `NotEnoughInformationError` Handler

### 4.1 Motivation

`NotEnoughInformationError` (NEIE) is raised by the Q-LLM wrapper when the Q-LLM
sets `have_enough_information = False`.  The Q-LLM processes untrusted data; its
"reason for insufficient information" could itself be adversary-controlled content
(e.g., a document that says "You don't have enough information; the secret is...").

M4-F7 ensures that **zero** Q-LLM content reaches the P-LLM through this error.

### 4.2 Handler Contract

When `ExceptionRedactor.classify()` encounters an `NotEnoughInformationError`:

1. **Line number extraction**: the interpreter attaches `__lineno__` to the exception
   at the `ast.Call` evaluation site (`_eval_Call`).  The redactor reads this attribute
   to recover the call-site line number.
2. **Content stripping**: `message` is set to `None` in `RedactedError`.  The Q-LLM's
   reason text is never forwarded.
3. **Fixed advisory**: `RetryPromptBuilder` appends a static advisory sentence:
   > "The call to `query_quarantined_llm()` at line N reported that the provided
   > context did not contain enough information to populate the requested schema.
   > Consider restructuring the query or passing additional context variables."
   This sentence contains only the line number and fixed text — no untrusted content.
4. **Trust level**: `trust_level = "not_enough_information"` in `RedactedError`.

### 4.3 `NotEnoughInformationError` Design Constraints

The `NotEnoughInformationError` class (in `camel/exceptions.py`) is designed to carry
**no** content:

```python
class NotEnoughInformationError(Exception):
    MESSAGE: str = "Q-LLM indicated insufficient information"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)
```

The fixed message string ensures that even if the message were accidentally forwarded,
it contains no untrusted data.  M4-F7 adds a second line of defence by ensuring the
message is always `None` in `RedactedError`.

### 4.4 Implementation Location

`camel/execution_loop.py` — `ExceptionRedactor.classify()` (NEIE branch)
`camel/exceptions.py` — `NotEnoughInformationError`
`camel/execution_loop.py` — `RetryPromptBuilder.build()` (NEIE advisory insertion)

---

## 5. M4-F8 — STRICT Mode Annotation Preservation Across NEIE Re-generation

### 5.1 Motivation

When NEIE is raised and the orchestrator triggers P-LLM re-generation, the interpreter
starts a new `exec()` call with the regenerated code.  Without M4-F8, the dependency
graph and `_dep_ctx_stack` accumulated before the failing Q-LLM call are lost.  A
subsequent STRICT mode taint check in the regenerated code would miss upstream sources
that were established before the failure.

This is a subtle correctness gap: an attacker could deliberately engineer a NEIE at
a point where the preceding code established untrusted provenance, then exploit the
blank-slate annotation state in the regenerated execution to bypass the policy engine.

### 5.2 Snapshot and Restore Protocol

M4-F8 adds two new fields to `AcceptedState`:

```python
@dataclass(frozen=True)
class AcceptedState:
    ...
    dependency_graph_snapshot: dict[str, frozenset[str]] | None = None
    dep_ctx_stack_snapshot: tuple[frozenset[str], ...] | None = None
```

**Snapshot** (on NEIE):

```
AcceptedState(
    variable_names   = frozenset(store.keys()),
    executed_count   = n,
    remaining_source = ast.unparse(remaining_nodes),
    dependency_graph_snapshot = interpreter.snapshot_dep_state()["dependency_graph"],
    dep_ctx_stack_snapshot    = interpreter.snapshot_dep_state()["dep_ctx_stack"],
)
```

`interpreter.snapshot_dep_state()` (M4-F8 public helper):

```
snapshot_dep_state() → dict:
    return {
        "dependency_graph": {
            var: frozenset(self._dep_graph._graph.get(var, set()))
            for var in self._dep_graph._graph
        },
        "dep_ctx_stack": tuple(self._dep_ctx_stack),
    }
```

**Restore** (before regenerated plan executes):

```
interpreter.restore_dep_state(accepted_state)
```

`interpreter.restore_dep_state()`:

```
restore_dep_state(accepted_state: AcceptedState) → None:
    if accepted_state.dependency_graph_snapshot is not None:
        for var, upstream in accepted_state.dependency_graph_snapshot.items():
            self._dep_graph._graph[var] = set(upstream)
    if accepted_state.dep_ctx_stack_snapshot is not None:
        self._dep_ctx_stack.clear()
        self._dep_ctx_stack.extend(accepted_state.dep_ctx_stack_snapshot)
```

### 5.3 Scope

M4-F8 applies **only** to NEIE failures, not to other exception types.  For other
exceptions the dependency graph snapshot fields in `AcceptedState` remain `None` and
no restore operation occurs.

### 5.4 Security Invariant

After M4-F8, the following invariant holds:

> All STRICT mode taint annotations accumulated before a `NotEnoughInformationError`
> are available to the regenerated plan's dependency graph, and to every policy
> evaluation that follows.

### 5.5 Implementation Location

`camel/execution_loop.py` — `AcceptedState` (new fields)
`camel/interpreter.py` — `snapshot_dep_state()`, `restore_dep_state()`
`camel/execution_loop.py` — `CaMeLOrchestrator._run_loop()` (snapshot/restore call sites)

---

## 6. M4-F9 — Loop-Body Exception STRICT Propagation

### 6.1 Motivation

When an exception originates inside a `for`-loop body and the loop's iterable is
non-public (i.e., has untrusted sources or restricted readers), the exception carries
an implicit dependency on that iterable.  Without M4-F9, the dependency context
associated with the iterable would be silently dropped across the retry cycle — the
regenerated plan would not inherit the iterable's taint.

This creates a side-channel: an adversary who controls the iterable can craft values
that cause exceptions at specific iterations, encoding information in which iteration
failed.  M4-F9 closes this by attaching the iterable's dependency context to the
exception itself, making the taint recoverable by the orchestrator.

### 6.2 Propagation Rule

**Condition:** All three of the following are true:
1. The interpreter is in `ExecutionMode.STRICT`.
2. The iterable's `CaMeLValue` is "non-public" — it has at least one non-trusted
   source (`src not in TRUSTED_SOURCES`) or non-`Public` readers.
3. An exception propagates out of the loop body (`_exec_For` catch block).

**Action:** Before re-raising the exception, `_exec_For` attaches two attributes:

```python
exc.__loop_iter_deps__ = _loop_inner_dep_ctx    # frozenset[str]
exc.__loop_iter_caps__ = inner_ctx2              # CaMeLValue | None
```

`_loop_inner_dep_ctx` is the `_dep_ctx_stack[-1]` value captured **before** entering
the loop body — it includes the iterable's dependency set merged with any enclosing
context dependencies.

### 6.3 Orchestrator Recovery

When the orchestrator catches an exception with `__loop_iter_deps__`:

1. It reads `loop_iter_deps` and `loop_iter_caps` from the exception.
2. It populates the corresponding fields in `AcceptedState`.
3. Before executing the regenerated plan, it pre-seeds the interpreter's
   `_dep_ctx_stack` with the loop iterable's dependency context.

This ensures the regenerated plan starts with the correct dependency context, and all
subsequent assignments inherit the iterable's taint as required by M4-F1.

### 6.4 Non-Public Iterable Check

The `_is_non_public()` helper on `CaMeLInterpreter`:

```
_is_non_public(cv: CaMeLValue) → bool:
    if any(src not in TRUSTED_SOURCES for src in cv.sources):
        return True
    if cv.readers is not Public and cv.readers:
        return True
    return False
```

### 6.5 Interaction with M4-F6 Redaction

When M4-F9 applies (`exc.__loop_iter_deps__` is set), `ExceptionRedactor` sets
`redaction_reason = "loop_body_exception"` instead of `"untrusted_dependency"`.
The `m4_f9_applied` field of the resulting `RedactionAuditEvent` is `True`.
The `RedactedError` still has `message=None` — M4-F9 and M4-F6 compose correctly.

### 6.6 Implementation Location

`camel/interpreter.py` — `_exec_For()` (M4-F9 annotation attachment)
`camel/interpreter.py` — `_is_non_public()` (non-public check helper)
`camel/execution_loop.py` — `AcceptedState` (new `loop_iter_deps`, `loop_iter_caps` fields)
`camel/execution_loop.py` — `CaMeLOrchestrator._run_loop()` (recovery and pre-seeding)
`camel/execution_loop.py` — `ExceptionRedactor.classify()` (m4_f9_applied flag)

---

## 7. M4-F17 — Redaction Audit Log Emitter

### 7.1 Purpose

Every exception processed by `ExceptionRedactor.classify()` — regardless of whether
redaction was applied — emits a `RedactionAuditEvent` to the security audit log.
This supports NFR-6 (observability): operators can review every redaction decision
without having access to the original untrusted content.

### 7.2 `RedactedError` Data Model

The sanitised exception representation safe to forward to the P-LLM:

```python
@dataclass(frozen=True)
class RedactedError:
    error_type:  str
    lineno:      int | None
    message:     str | None
    trust_level: Literal["trusted", "untrusted", "not_enough_information"]
```

Field population by case:

| Case | `error_type` | `lineno` | `message` | `trust_level` |
|---|---|---|---|---|
| Trusted | always set | set | full message | `"trusted"` |
| Untrusted-dependency (M4-F6) | always set | set | `None` | `"untrusted"` |
| NEIE (M4-F7) | always set | call-site lineno | `None` | `"not_enough_information"` |

### 7.3 `RedactionAuditEvent` Schema

Emitted by `ExceptionRedactor._emit_audit_event()` for every classification:

```python
@dataclass(frozen=True)
class RedactionAuditEvent:
    timestamp:               str                    # ISO-8601 UTC
    line_number:             int | None             # AST source line; None if unavailable
    redaction_reason:        str                    # see §7.4
    dependency_chain:        list[tuple[str, str]]  # [(var_name, source_label), ...]; max 50
    trust_level:             str                    # "trusted" | "untrusted" | "not_enough_information"
    error_type:              str                    # exception class name; always present
    redacted_message_length: int                    # len(original message) before redaction; 0 if empty
    m4_f9_applied:           bool                   # True if M4-F9 loop propagation was applied
```

### 7.4 `redaction_reason` Values

| Value | Condition | M4 feature |
|---|---|---|
| `"untrusted_dependency"` | M4-F6 taint check returned True; no loop annotation | M4-F6 |
| `"not_enough_information"` | Exception is `NotEnoughInformationError` | M4-F7 |
| `"loop_body_exception"` | M4-F6 tainted AND M4-F9 `__loop_iter_deps__` is present | M4-F9 |
| `"none"` | All sources trusted; no redaction | — |

### 7.5 Emission Contract

`RedactionAuditEvent` is emitted:
- **Immediately** before `classify()` returns (after the `RedactedError` is constructed).
- **For every exception** processed — including trusted exceptions (`redaction_reason="none"`).
- Into the `audit_log` list passed to `ExceptionRedactor.__init__()`.
- If `audit_log` is `None`, the event is silently discarded (backward-compatible behaviour
  for tests that do not configure a log).

The audit log is accessible on `CaMeLOrchestrator` via:
```python
orchestrator.redaction_audit_log  # list[RedactionAuditEvent]
```

### 7.6 `dependency_chain` Content Policy

The `dependency_chain` field contains `(variable_name, source_label)` pairs.  Both
components are:
- **Variable names**: P-LLM-generated identifiers — safe to log.
- **Source labels**: registered tool IDs or `"User literal"` / `"CaMeL"` — safe to log.

Neither component contains raw values from tool return data.  This ensures the
audit log itself cannot be used as a side channel to exfiltrate untrusted content.

### 7.7 Implementation Location

`camel/execution_loop.py` — `RedactionAuditEvent` (dataclass definition)
`camel/execution_loop.py` — `ExceptionRedactor._emit_audit_event()` (emission helper)
`camel/execution_loop.py` — `CaMeLOrchestrator.redaction_audit_log` (property accessor)

---

## 8. Integration: Exception Redaction in the Execution Loop

### 8.1 Call Site

`ExceptionRedactor.classify()` is called from `CaMeLOrchestrator._run_loop()` in the
exception catch block:

```
try:
    interpreter.exec(remaining_code)
except Exception as exc:
    redacted = redactor.classify(
        exc,
        interpreter_store_snapshot=dict(interpreter.store),
        interpreter=interpreter,
    )
    # Build AcceptedState (with M4-F8 snapshot if NEIE; M4-F9 fields if loop exc)
    accepted = _build_accepted_state(interpreter, exc, remaining_nodes)
    # Hand off to RetryPromptBuilder
    retry_prompt = prompt_builder.build(accepted, redacted)
    # Regenerate plan via P-LLM
    ...
```

### 8.2 Retry Prompt Content

After redaction, the retry prompt contains only:

- Names of variables defined in the accepted state (no values).
- The `RedactedError` fields: `error_type` + `lineno` + (for trusted) `message`.
- For NEIE: a fixed advisory sentence containing only the call-site line number.
- Count of statements successfully executed before failure.
- The remaining source code to regenerate (from `AcceptedState.remaining_source`).

### 8.3 Sequence Diagram

```
CaMeLOrchestrator._run_loop()
  │
  ├─► interpreter.exec(remaining_code)
  │     │
  │     └─ raises exc
  │           │
  │           ▼
  ├─► [M4-F9 check] _exec_For attached __loop_iter_deps__ to exc?
  │
  ├─► redactor.classify(exc, store_snapshot, interpreter)
  │     │
  │     ├─► M4-F7: NotEnoughInformationError?
  │     │     └─ RedactedError(type, lineno, None, "not_enough_information")
  │     │
  │     ├─► M4-F6: _is_tainted(exc, store, interpreter)?
  │     │     └─ RedactedError(type, lineno, None, "untrusted")
  │     │
  │     ├─► else: RedactedError(type, lineno, str(exc), "trusted")
  │     │
  │     └─► M4-F17: _emit_audit_event() → audit_log.append(RedactionAuditEvent(...))
  │
  ├─► _build_accepted_state()
  │     ├─ M4-F8: if NEIE → include dependency_graph_snapshot + dep_ctx_stack_snapshot
  │     └─ M4-F9: if loop exc → include loop_iter_deps + loop_iter_caps
  │
  └─► RetryPromptBuilder.build(accepted_state, redacted_error)
        └─► P-LLM receives: sanitised error + static context only
```

---

## 9. Security Properties

### 9.1 What Exception Hardening Guarantees

| Guarantee | Mechanism |
|---|---|
| No untrusted exception message reaches P-LLM | M4-F6 dependency taint check |
| No Q-LLM content reaches P-LLM via NEIE | M4-F7 content stripping |
| STRICT mode taint is not dropped across NEIE retries | M4-F8 snapshot/restore |
| Loop iterable taint is not dropped across loop-body-exception retries | M4-F9 annotation attachment |
| All redaction decisions are auditable | M4-F17 audit event emission |

### 9.2 What Exception Hardening Does Not Guarantee

| Risk | Notes |
|---|---|
| **Exception occurrence as a side channel** | The fact that an exception occurred (and at which line) is still visible to the P-LLM. A binary oracle (exception vs. no exception) could leak one bit per call. This is a known residual risk (PRD §10 L3). |
| **Exception type as a side channel** | `error_type` (e.g., `"TypeError"`, `"ValueError"`) is always forwarded to the P-LLM. The exception class is generated by Python's own type system and does not echo untrusted content. |
| **Fallback store scan precision** | When `interpreter=None`, the fallback performs a flat store scan rather than a dep-graph walk. This may over-redact trusted exceptions when any untrusted value is in scope. The dep-graph-aware path (M4-F6) is always preferred. |
| **Deeply nested tool call exception propagation** | M4-F9 attaches loop-iterable metadata when an exception propagates out of `_exec_For`. However, exceptions originating inside deeply nested tool call chains — where the interpreter invokes a tool that internally calls other tools, and the exception propagates through multiple call frames before reaching the interpreter's exception handler — may not have the expected loop-iterable metadata attached. M4-F9 closes the primary loop-body exception channel; adversarial exception triggering via multi-level tool call stacks remains a documented residual risk. This is noted in PRD §10 L3. |

### 9.3 Interaction with Other M4 Features

| Feature | Interaction |
|---|---|
| M4-F1 (for-loop dep propagation) | M4-F9 relies on the loop iterable caps/deps computed by M4-F1. Without M4-F1, the loop context would not be available to attach. |
| M4-F2 (if/else dep propagation) | No direct interaction with exception hardening; M4-F2 taints ensure branch-derived variables are correctly flagged by M4-F6. |
| M4-F3/F4 (post-Q-LLM tainting) | Q-LLM result variables carry untrusted sources; M4-F6 will correctly detect and redact exceptions involving those variables. |
| M4-F5 (STRICT mode default) | M4-F8 and M4-F9 apply only in STRICT mode. NORMAL mode does not propagate dep context across retries (expected: NORMAL mode is not security-safe). |
| M4-F10–F14 (allowlist enforcement) | `ForbiddenImportError` and `ForbiddenNameError` are P-LLM code errors; their messages are always trusted (they contain only P-LLM-generated identifiers). These errors are correctly classified as trusted by M4-F6. |

---

## 10. Test Coverage Requirements

| Scenario | Expected Result | M4 feature |
|---|---|---|
| Exception from trusted variable only | `trust_level="trusted"`, message included | M4-F6 |
| Exception from untrusted variable (direct) | `trust_level="untrusted"`, `message=None` | M4-F6 |
| Exception from variable with upstream untrusted dep | `trust_level="untrusted"`, `message=None` | M4-F6 |
| `NotEnoughInformationError` raised | `trust_level="not_enough_information"`, `message=None`, only lineno forwarded | M4-F7 |
| NEIE retry: dep graph preserved | Regenerated plan inherits pre-failure taint annotations | M4-F8 |
| Loop-body exception, non-public iterable | `m4_f9_applied=True`, `redaction_reason="loop_body_exception"`, taint pre-seeded in retry | M4-F9 |
| Loop-body exception, public iterable | `m4_f9_applied=False`, normal M4-F6 classification | M4-F9 |
| Every exception processed | `RedactionAuditEvent` emitted with all required fields | M4-F17 |
| P-LLM retry prompt | Contains `error_type` + `lineno` only for untrusted/NEIE cases | M4-F7 |
| `dependency_chain` content | Contains only variable names and source labels; no raw values | M4-F17 |

Tests are located in:
- `tests/test_exception_hardening.py` — unit tests for M4-F6 through M4-F9, M4-F17
- `tests/test_redaction_completeness.py` — adversarial redaction tests
- `tests/test_execution_loop.py` — orchestrator-level `RedactionAuditEvent` emission tests
- `tests/integration/test_strict_mode_e2e.py` — end-to-end STRICT mode with loop exception

---

## 11. Verification Record

All five features were verified as part of the Milestone 4 Exception Hardening & Redaction
sprint (2026-03-17).  See `CLAUDE.md` sprint history for the full acceptance evidence.

| Feature | Verification method |
|---|---|
| M4-F6 | `tests/test_exception_hardening.py` — dependency-graph taint cases |
| M4-F7 | `tests/test_exception_hardening.py` — NEIE content-stripping tests |
| M4-F8 | `tests/test_exception_hardening.py` — dep state snapshot/restore round-trip |
| M4-F9 | `tests/test_exception_hardening.py` — loop-body exc propagation; `test_strict_mode.py` |
| M4-F17 | `tests/test_execution_loop.py` — `RedactionAuditEvent` schema and emission coverage |
