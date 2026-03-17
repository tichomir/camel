# Milestone 4 — Data-to-Control-Flow Escalation Detection Design

_Author: Software Architect | Date: 2026-03-17 | Status: ✅ Implemented — M4-F15, M4-F16, M4-F18 delivered and verified_

---

## 1. Overview

This document specifies the design for the data-to-control-flow escalation detection phase
of Milestone 4.  The goal is to detect, at runtime, any attempt by untrusted data to
influence **which tool** is invoked — and to block that attempt with an elevated user
consent gate before any tool call proceeds.

### 1.1 Threat: Data-to-Control-Flow Escalation

A data-to-control-flow escalation attack occurs when adversary-controlled content
embedded in tool return values (or Q-LLM outputs) is used to determine the identity
of the function or tool being called.  Unlike pure data-flow attacks (which inject
malicious *arguments* into legitimate tool calls), this class of attack influences
the *control path* — which tool fires — by smuggling the tool selection through the
data plane.

Concrete attack pattern:

```python
# P-LLM-generated plan — seemingly innocuous
doc = read_document("report.pdf")          # untrusted tool output
action_name = query_quarantined_llm(       # Q-LLM extracts from untrusted doc
    doc, ActionSchema
).action                                   # action is adversary-controlled string
result = action_name(sensitive_data)       # data-to-control escalation:
                                           # "which tool fires?" depends on doc
```

Without M4-F15/F16, the interpreter would attempt to dispatch whatever callable
`action_name` resolves to.  If `action_name` is a registered tool name, this bypasses
the policy engine's ability to predict and audit the tool selection.

### 1.2 Relationship to Existing Defences

The P-LLM isolation guarantee (PRD §7.1) ensures the *code plan* is generated
from the trusted user query.  However, if P-LLM-generated code calls a variable as
a function — and that variable was populated with untrusted data — the tool selection
at execution time is still adversary-influenced, even though the plan itself looks
legitimate at generation time.

M4-F15/F16 close this gap by adding a runtime check that fires at the exact point
where the interpreter resolves which callable to dispatch.

### 1.3 Feature Register

| Feature ID | Description | Status |
|---|---|---|
| M4-F15 | `DataToControlFlowWarning` detector — runtime check on function-call operand provenance | ✅ Implemented |
| M4-F16 | Elevated user consent gate — execution pause and elevated prompt on M4-F15 detection | ✅ Implemented |
| M4-F18 | Audit log enhancement — per-statement STRICT mode dependency addition recording | ✅ Implemented |

---

## 2. M4-F15 — DataToControlFlowWarning Detection

### 2.1 Check Point: `_eval_Call` in the Interpreter

All function and tool calls in the CaMeL interpreter flow through `_eval_Call` in
`camel/interpreter.py`.  This method evaluates the `func` sub-expression before
dispatching the call.

The detection check is inserted **immediately after** the `func` operand is evaluated
and **before** any tool dispatch occurs:

```
_eval_Call(node: ast.Call, ctx_caps: CaMeLValue | None) -> CaMeLValue:

    # Step 1 — evaluate the function operand
    func_cv = self._eval_expr(node.func, ctx_caps)

    # ─── M4-F15 CHECK ─────────────────────────────────────────────────────
    #  Determine the offending variable name for diagnostic purposes.
    offending_var = node.func.id if isinstance(node.func, ast.Name) else None

    # Build the untrusted-sources set for the func operand.
    untrusted_func_sources = {
        s for s in func_cv.sources if s not in TRUSTED_SOURCES
    }

    # In STRICT mode, also inspect the full dependency graph for func_cv.
    # This catches indirect escalation where the called function variable is
    # derived from (rather than assigned directly from) untrusted data.
    untrusted_dep_sources = _collect_untrusted_dep_sources(
        func_cv, offending_var, self._dep_graph, self._store
    )
    all_untrusted = untrusted_func_sources | untrusted_dep_sources

    if all_untrusted:
        warning = DataToControlFlowWarning(
            lineno=node.lineno,
            offending_variable=offending_var,
            untrusted_sources=frozenset(all_untrusted),
            dependency_chain=_build_dep_chain(func_cv, offending_var,
                                              self._dep_graph, self._store),
        )
        self._emit_escalation_audit_event(warning)
        self._handle_escalation(warning, node)   # → M4-F16
    # ─── END M4-F15 CHECK ─────────────────────────────────────────────────

    # ... existing dispatch logic continues
```

### 2.2 `DataToControlFlowWarning` Data Structure

```python
@dataclass(frozen=True)
class DataToControlFlowWarning:
    """Runtime warning emitted when a tool call's function operand has untrusted
    provenance, indicating a data-to-control-flow escalation attempt (M4-F15).

    Attributes
    ----------
    lineno:
        1-based source line number of the offending call expression.
        ``None`` when AST line information is unavailable.
    offending_variable:
        The identifier name of the ``ast.Name`` node in the ``func``
        position, if the call is of the form ``variable_name(args)``.
        ``None`` for more complex function-expression forms (e.g.,
        attribute access ``obj.method(...)``).
    untrusted_sources:
        Frozenset of source labels from the function operand's capability
        graph that are outside ``TRUSTED_SOURCES``.  Always non-empty when
        a warning is emitted; the set enumerates the exact tool IDs or
        external sources that contributed untrusted provenance to the
        called function value.
    dependency_chain:
        Ordered list of ``(variable_name, source_label)`` pairs tracing
        the provenance of the function operand back to its untrusted
        origins.  Capped at 50 entries.  Contains only variable names and
        source labels — no raw untrusted values — safe to log.
    """

    lineno: int | None
    offending_variable: str | None
    untrusted_sources: frozenset[str]
    dependency_chain: list[tuple[str, str]]
```

This class is defined in `camel/exceptions.py` and exported from `camel/__init__.py`.

### 2.3 TRUSTED_SOURCES Definition

Consistent with M4-F6 and M4-F9, the trusted source set is:

```python
TRUSTED_SOURCES: frozenset[str] = frozenset({"User literal", "CaMeL"})
```

Any source label outside this set indicates that the callable's identity was
derived from tool return values or Q-LLM output — an escalation attempt.

### 2.4 Dependency Chain Construction

The `_build_dep_chain` helper (in `camel/interpreter.py`) mirrors the algorithm used by
`ExceptionRedactor._is_tainted()`:

```
_build_dep_chain(func_cv, offending_var, dep_graph, store) → list[tuple[str, str]]:

    chain = []

    # Direct sources of the function operand value
    for src in func_cv.sources:
        if src not in TRUSTED_SOURCES:
            chain.append((offending_var or "<expr>", src))

    # Walk upstream dependency graph (if variable name is known)
    if offending_var and offending_var in dep_graph:
        for upstream_var in dep_graph.all_upstream(offending_var):
            if upstream_var in store:
                for src in store[upstream_var].sources:
                    if src not in TRUSTED_SOURCES:
                        chain.append((upstream_var, src))

    return chain[:50]
```

### 2.5 Audit Log Emission (M4-F15 side)

The `_emit_escalation_audit_event` helper emits a `DataToControlFlowAuditEvent` to the
interpreter's `_security_audit_log` immediately before `_handle_escalation` is called:

```python
@dataclass
class DataToControlFlowAuditEvent:
    """Audit log entry emitted when M4-F15 detects a data-to-control-flow
    escalation attempt.

    Fields
    ------
    event_type:
        Always ``"DataToControlFlowEscalation"`` — used to filter events.
    timestamp:
        ISO-8601 UTC timestamp of detection.
    lineno:
        1-based source line number.
    offending_variable:
        Name of the function operand variable, or ``None``.
    untrusted_sources:
        List of untrusted source labels found on the function operand.
    dependency_chain:
        Provenance trace (variable, source) pairs — max 50 entries.
    elevated_consent_triggered:
        ``True`` when M4-F16 elevated consent gate was invoked.
    consent_outcome:
        ``"approved"`` | ``"rejected"`` | ``"evaluation_mode_raised"``
        describing the M4-F16 gate outcome.
    """

    event_type: str = "DataToControlFlowEscalation"
    timestamp: str = ""
    lineno: int | None = None
    offending_variable: str | None = None
    untrusted_sources: list[str] = field(default_factory=list)
    dependency_chain: list[tuple[str, str]] = field(default_factory=list)
    elevated_consent_triggered: bool = False
    consent_outcome: str = ""   # "approved" | "rejected" | "evaluation_mode_raised"
```

This event is emitted to `self._security_audit_log` (the same list used for
`ForbiddenImportEvent` and `ForbiddenNameEvent`) rather than the redaction audit log,
keeping security-class events co-located.

---

## 3. M4-F16 — Elevated User Consent Gate

### 3.1 Design Principle

The elevated consent gate is semantically *prior to* and *independent of* the normal
policy evaluation path.  It fires whenever M4-F15 detects an escalation, regardless of:

- Whether the tool call would be `Allowed` or `Denied` by the policy engine.
- Whether an existing `consent_callback` is configured.
- Whether any previous consent decisions have been granted for the same tool.

This ensures that data-to-control-flow escalation cannot be laundered through a
pre-approved policy decision.

### 3.2 Enforcement Mode Behaviour

The gate behaviour mirrors the existing enforcement-mode split used for policy denials:

| Enforcement mode | Behaviour on M4-F15 detection |
|---|---|
| `EVALUATION` | Raise `DataToControlFlowEscalationError` immediately, no UI interaction |
| `PRODUCTION` | Pause execution; invoke `ElevatedConsentCallback`; proceed on approval; raise on rejection |

### 3.3 `DataToControlFlowEscalationError`

```python
@dataclass
class DataToControlFlowEscalationError(Exception):
    """Raised in EVALUATION mode when M4-F16 detects a data-to-control-flow
    escalation attempt (M4-F15/F16).

    Attributes
    ----------
    warning:
        The :class:`DataToControlFlowWarning` that triggered this error.
        Contains the line number, offending variable, untrusted sources,
        and dependency chain.
    message:
        Human-readable description constructed from ``warning`` fields;
        contains only variable names and source labels — no untrusted
        runtime values.
    """

    warning: DataToControlFlowWarning

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"Data-to-control-flow escalation detected at line "
            f"{self.warning.lineno}: function operand "
            f"{self.warning.offending_variable!r} has untrusted sources "
            f"{sorted(self.warning.untrusted_sources)} (M4-F15/F16)"
        )
```

### 3.4 `ElevatedConsentCallback` Protocol

```python
class ElevatedConsentCallback(Protocol):
    """Protocol for elevated-consent callbacks triggered by M4-F15/F16.

    Separate from :class:`ConsentCallback` (used for policy denials) to allow
    deployments to distinguish a routine policy override from an escalation
    event requiring a higher level of operator attention.

    Parameters
    ----------
    warning:
        The :class:`DataToControlFlowWarning` describing the detection event.
    tool_name_candidate:
        The raw callable name the interpreter would have dispatched — provided
        to give the operator context on what action was about to be taken.
        May be ``None`` if the func expression does not resolve to a simple
        name.

    Returns
    -------
    bool
        ``True`` to approve and proceed with the call.
        ``False`` to reject; raises
        :class:`DataToControlFlowEscalationError` with
        ``consent_outcome="rejected"``.
    """

    def __call__(
        self,
        warning: DataToControlFlowWarning,
        tool_name_candidate: str | None,
    ) -> bool: ...
```

### 3.5 Interpreter Constructor Changes

```python
CaMeLInterpreter(
    ...
    elevated_consent_callback: ElevatedConsentCallback | None = None,
)
```

- `elevated_consent_callback` is **optional** — if not provided, PRODUCTION mode uses
  a default that always rejects escalation attempts (secure default).
- If `enforcement_mode=EVALUATION`, `elevated_consent_callback` is ignored; escalation
  immediately raises `DataToControlFlowEscalationError`.
- `elevated_consent_callback` and `consent_callback` serve different purposes and are
  independent parameters — a deployment can configure one, both, or neither.

### 3.6 Gate Flow (PRODUCTION Mode)

```
_handle_escalation(warning: DataToControlFlowWarning, node: ast.Call) -> None:

    # Extract the tool name candidate for the operator prompt
    tool_name_candidate = (
        node.func.id if isinstance(node.func, ast.Name) else None
    )

    if self._enforcement_mode is EnforcementMode.EVALUATION:
        # EVALUATION: raise immediately, no UI
        _update_audit_event(consent_outcome="evaluation_mode_raised")
        raise DataToControlFlowEscalationError(warning=warning)

    # PRODUCTION: invoke elevated consent gate
    callback = (
        self._elevated_consent_callback
        if self._elevated_consent_callback is not None
        else _default_reject_elevated_consent   # always returns False
    )
    approved = callback(warning, tool_name_candidate)

    if approved:
        _update_audit_event(elevated_consent_triggered=True, consent_outcome="approved")
        return   # execution proceeds to normal policy check + dispatch
    else:
        _update_audit_event(elevated_consent_triggered=True, consent_outcome="rejected")
        raise DataToControlFlowEscalationError(warning=warning)
```

### 3.7 Interaction with Normal Policy Evaluation

When elevated consent is **approved** (PRODUCTION mode), execution resumes at the
point after the M4-F15 check — the normal policy engine evaluation runs next.  The
policy check is NOT bypassed by elevated consent; the call must clear both gates:

```
_eval_Call flow (PRODUCTION mode, escalation approved):

  1. M4-F15 check → warning detected
  2. M4-F16 elevated gate → operator approves
  3. Policy engine evaluation (as normal)
     → Allowed → tool call proceeds
     → Denied  → consent_callback (normal policy denial flow)
```

This ensures defence in depth: an escalation approval does not grant blanket policy
permission.

### 3.8 Bypass-Prevention Guarantee

The M4-F16 gate cannot be bypassed by:

- **Policy outcomes**: the escalation check fires before `_policy_engine.evaluate()`.
- **Existing approvals**: the `DataToControlFlowAuditEvent` records each escalation
  event individually; there is no memoisation of prior elevated consents.
- **Mode configuration errors**: if `elevated_consent_callback` is `None` in
  PRODUCTION mode, `_default_reject_elevated_consent` is used — the gate always
  rejects rather than silently proceeding.

---

## 4. M4-F18 — Per-Statement STRICT Mode Dependency Addition Audit Log

### 4.1 Purpose

M4-F18 provides post-execution observability into the STRICT mode propagation
engine.  For every assignment executed under STRICT mode, the audit log records
exactly which dependencies were *added* by the STRICT context (beyond what direct
data flow would have contributed).

This enables:
- Security reviewers to audit why specific variables carry specific taint.
- Test assertions that STRICT mode is correctly propagating dependencies.
- Investigation of policy denials: which upstream STRICT addition caused the denial.

### 4.2 `StrictDependencyAdditionEvent` Schema

```python
@dataclass(frozen=True)
class StrictDependencyAdditionEvent:
    """Audit log entry recording STRICT mode dependency additions for a single
    assignment statement (M4-F18).

    Emitted once per assignment in STRICT mode where the context dependency set
    is non-empty — i.e., where STRICT mode adds dependencies beyond what normal
    direct-data-flow tracking would record.

    Attributes
    ----------
    event_type:
        Always ``"StrictDependencyAddition"`` for log filtering.
    timestamp:
        ISO-8601 UTC timestamp at the moment of the assignment.
    statement_lineno:
        1-based source line number of the assignment statement.
        ``None`` when unavailable.
    statement_type:
        AST node type name: ``"Assign"``, ``"AugAssign"``, or ``"For"``
        (for loop variable assignments).
    assigned_variable:
        The name of the variable receiving the assignment.  For tuple
        unpacking, one event is emitted per unpacked name.
    added_dependencies:
        The frozenset of variable names added by the STRICT context
        propagation rule that fired.  Specifically, these are the names in
        the current ``_dep_ctx_stack[-1]`` that would NOT have been added
        under NORMAL mode.  Empty sets are never emitted.
    context_source:
        The STRICT mode rule responsible for the addition:

        ``"for_iterable"``
            M4-F1: the iterable variable's deps were merged into this
            assignment inside the loop body.
        ``"if_condition"``
            M4-F2: the if/else condition's deps were merged into this
            assignment inside the branch body.
        ``"post_qllm"``
            M4-F3/F4: a preceding ``query_quarantined_llm()`` call in the
            same code block contributed its result's deps to this
            assignment.
    """

    event_type: str
    timestamp: str
    statement_lineno: int | None
    statement_type: str
    assigned_variable: str
    added_dependencies: frozenset[str]
    context_source: str   # "for_iterable" | "if_condition" | "post_qllm"
```

### 4.3 Emission Point and Conditions

`StrictDependencyAdditionEvent` is emitted from the `_store_value` helper (or inline
in `_exec_statements`), immediately after an assignment target is stored with
context-augmented dependencies.

**Emission condition:** The event is emitted *only* when ALL of the following are true:

1. `self._mode is ExecutionMode.STRICT`
2. The current `_dep_ctx_stack[-1]` is non-empty (there is an active STRICT context)
3. The non-empty context deps are actually being merged into this assignment's
   dependency set (i.e., `strict_additions = _dep_ctx_stack[-1] - direct_deps`
   is non-empty)

This means events are only emitted when STRICT mode is genuinely *adding* information
beyond what NORMAL mode would have recorded — eliminating noise from statements where
STRICT mode has no effect.

### 4.4 `context_source` Determination

The `context_source` field is set by examining how `_dep_ctx_stack[-1]` was populated:

```
Determination of context_source:

  if currently inside _exec_For body:
      context_source = "for_iterable"
  elif currently inside _exec_If branch:
      context_source = "if_condition"
  elif _last_qllm_result_cv is not None (M4-F3/F4 flag active):
      context_source = "post_qllm"
  else:
      # Combined contexts (nested for inside if, etc.)
      context_source = "combined"   # multi-source context
```

For the common case of nested contexts (a for-loop inside an if-branch), both
context sources contribute.  In this case `context_source = "combined"` and the
`added_dependencies` set includes contributions from both sources.

### 4.5 Audit Log Integration

`StrictDependencyAdditionEvent` is emitted to a dedicated list on the interpreter:

```python
self._strict_dep_audit_log: list[StrictDependencyAdditionEvent] = []
```

This list is separate from `_audit_log` (policy events) and `_security_audit_log`
(allowlist events) to keep event categories queryable independently.

The list is exposed as a public property:

```python
@property
def strict_dep_audit_log(self) -> list[StrictDependencyAdditionEvent]:
    """Per-statement STRICT mode dependency addition events (M4-F18).

    Returns a snapshot of all ``StrictDependencyAdditionEvent`` entries
    emitted during all ``exec()`` calls on this interpreter instance.
    Callers should copy this list if they need a stable snapshot.
    """
    return list(self._strict_dep_audit_log)
```

The `CaMeLOrchestrator` exposes an aggregated view:

```python
@property
def strict_dep_audit_log(self) -> list[StrictDependencyAdditionEvent]:
    """Aggregated STRICT mode dependency addition events from the interpreter."""
    return self._interpreter.strict_dep_audit_log
```

### 4.6 Performance Consideration

Emitting one event per assignment in STRICT mode in a long loop would produce O(n)
events for n iterations.  To prevent log flooding:

- Events are emitted at most once per unique `(statement_lineno, assigned_variable)`
  pair per `exec()` call.  Subsequent iterations of the same assignment in the same
  loop emit a **single** summary event with the **first** and **last** timestamp
  and an `iteration_count` field.
- If `statement_lineno` is `None` (no line info), deduplication falls back to
  `assigned_variable` alone.

This deduplication is applied inside `_emit_strict_dep_event()`:

```python
_dedup_key = (statement_lineno, assigned_variable)
if _dedup_key not in self._strict_dep_seen:
    self._strict_dep_seen.add(_dedup_key)
    self._strict_dep_audit_log.append(StrictDependencyAdditionEvent(...))
```

`_strict_dep_seen` is reset at the start of each `exec()` call (not across the
interpreter lifetime) so that events from different `exec()` calls are always recorded.

---

## 5. Integration Architecture

### 5.1 Full Call Sequence (PRODUCTION Mode, Escalation Detected)

```
CaMeLInterpreter._eval_Call(node, ctx_caps)
  │
  ├─► _eval_expr(node.func, ctx_caps)  →  func_cv
  │
  ├─► M4-F15: compute untrusted_sources from func_cv.sources ∪ dep_graph
  │     │
  │     └─ if untrusted_sources non-empty:
  │           DataToControlFlowWarning constructed
  │           _emit_escalation_audit_event()  →  _security_audit_log
  │           │
  │           └─► M4-F16: _handle_escalation(warning, node)
  │                 │
  │                 └─ PRODUCTION mode:
  │                       elevated_consent_callback(warning, tool_name_candidate)
  │                         → approved: continue
  │                         → rejected: raise DataToControlFlowEscalationError
  │
  ├─► [Normal tool/builtin dispatch continues from here]
  ├─► Policy engine evaluation  →  Allowed / Denied
  ├─► Tool call with raw args
  └─► Return result CaMeLValue
```

### 5.2 Integration with Existing Audit Logs

| Log name | List attribute | Event types stored |
|---|---|---|
| Policy audit log | `_audit_log` | `AuditLogEntry` |
| Security violations | `_security_audit_log` | `ForbiddenImportEvent`, `ForbiddenNameEvent`, **`DataToControlFlowAuditEvent`** |
| STRICT dep additions | `_strict_dep_audit_log` | **`StrictDependencyAdditionEvent`** |
| Redaction events | (on orchestrator) | `RedactionAuditEvent` |

`DataToControlFlowAuditEvent` is appended to `_security_audit_log` because it is a
security-violation-class event (like forbidden imports), not a normal policy event.

### 5.3 Exception Redaction Interaction

`DataToControlFlowEscalationError` (raised in EVALUATION mode) is a CaMeL-internal
exception whose message contains only:
- Variable names (P-LLM-generated code text)
- Source labels (registered tool IDs)
- The line number

Both categories are safe to include in retry prompts without redaction.  The
`ExceptionRedactor.classify()` method should classify `DataToControlFlowEscalationError`
as `trust_level="trusted"` (the message reveals no untrusted runtime values).

This is consistent with the treatment of `ForbiddenImportError` and `ForbiddenNameError`
documented in `docs/design/milestone4-exception-hardening.md §9.3`.

---

## 6. Implementation Locations

| Component | File | Notes |
|---|---|---|
| `DataToControlFlowWarning` | `camel/exceptions.py` | Frozen dataclass |
| `DataToControlFlowEscalationError` | `camel/exceptions.py` | Dataclass; references warning |
| `ElevatedConsentCallback` | `camel/interpreter.py` | Protocol class |
| `StrictDependencyAdditionEvent` | `camel/interpreter.py` | Frozen dataclass |
| `DataToControlFlowAuditEvent` | `camel/interpreter.py` | Dataclass |
| M4-F15 check insertion | `camel/interpreter.py` | `_eval_Call` — after func_cv evaluation |
| `_handle_escalation()` | `camel/interpreter.py` | Called from `_eval_Call` |
| `_emit_escalation_audit_event()` | `camel/interpreter.py` | Appends to `_security_audit_log` |
| `_emit_strict_dep_event()` | `camel/interpreter.py` | Appends to `_strict_dep_audit_log` |
| STRICT dep emission points | `camel/interpreter.py` | `_store_value`, `_exec_statements` |
| `strict_dep_audit_log` property | `camel/interpreter.py` | Public accessor |
| `elevated_consent_callback` param | `camel/interpreter.py` | Added to `__init__` |
| Orchestrator `strict_dep_audit_log` | `camel/execution_loop.py` | Delegates to interpreter |
| Exports | `camel/__init__.py` | Add `DataToControlFlowWarning`, `DataToControlFlowEscalationError`, `StrictDependencyAdditionEvent` |

---

## 7. Security Properties

### 7.1 What M4-F15/F16 Guarantees

| Guarantee | Mechanism |
|---|---|
| No untrusted-derived callable executes without operator awareness | M4-F15 fires before any dispatch when `func_cv.sources ⊄ TRUSTED_SOURCES` |
| Gate fires regardless of policy outcome | M4-F16 check precedes `_policy_engine.evaluate()` |
| Gate fires regardless of prior consent decisions | No memoisation of elevated consent; one event per call site |
| Audit trail for every escalation detection | `DataToControlFlowAuditEvent` with consent outcome emitted unconditionally |
| EVALUATION mode raises without UI | `DataToControlFlowEscalationError` raised immediately |
| PRODUCTION mode secure default | Missing callback → always-reject default; never silently proceeds |

### 7.2 What M4-F15/F16 Does Not Guarantee

| Residual Risk | Notes |
|---|---|
| **ROP-analogue action chaining** | M4-F15/F16 prevents a single data-derived callable from firing without detection. However, an adversary could chain *individually approved* tool calls — each individually trusted — to produce a collectively malicious outcome (the ROP-analogue). This risk is documented in PRD §10 L6. Mitigation: defence-in-depth with model-level training and action-sequence anomaly detection (FW-6). |
| **Indirect escalation via approved elevated consent** | If an operator approves an elevated consent in PRODUCTION mode, the tool fires. The audit event records this approval. CaMeL cannot prevent a human operator from approving a malicious escalation. |
| **P-LLM literal calls** | If P-LLM *generates code* that calls a tool by its literal name, M4-F15 does not fire (literal tool names have `sources={"CaMeL"}`). This is correct: P-LLM code text is trusted. The attack this closes is specifically the runtime data-plane path. |

### 7.3 What M4-F18 Provides

M4-F18 is an *observability* feature, not a security control.  It provides:
- A complete per-statement trace of STRICT mode dependency additions.
- A basis for post-hoc audit of policy decisions ("why was this variable tainted?").
- A testing hook for verifying STRICT mode correctness without reading internal state.

---

## 8. Test Coverage Requirements

### 8.1 M4-F15 / M4-F16 Tests

| Scenario | Expected Result | Feature |
|---|---|---|
| Call with literal tool name | No warning; normal dispatch | M4-F15 (negative) |
| Call where func var has untrusted direct sources | `DataToControlFlowWarning` emitted; `DataToControlFlowEscalationError` raised (EVALUATION) | M4-F15, M4-F16 |
| Call where func var has untrusted upstream dep (indirect) | Warning emitted with `dependency_chain` tracing indirect source | M4-F15 |
| PRODUCTION mode, operator approves | Normal policy eval + tool dispatch proceeds; audit event records `"approved"` | M4-F16 |
| PRODUCTION mode, operator rejects | `DataToControlFlowEscalationError` raised; audit event records `"rejected"` | M4-F16 |
| PRODUCTION mode, no elevated callback configured | Always-reject default fires; `DataToControlFlowEscalationError` raised | M4-F16 |
| Policy `Allowed` + escalation detected | Gate fires before policy; escalation blocks despite `Allowed` | M4-F16 |
| `DataToControlFlowAuditEvent` emitted | Event in `_security_audit_log` with correct fields | M4-F15 |
| EVALUATION mode: `DataToControlFlowEscalationError` message | Contains only variable name + source labels (no raw values) | M4-F16 |

### 8.2 M4-F18 Tests

| Scenario | Expected Result | Feature |
|---|---|---|
| Assignment inside for-loop with untrusted iterable (STRICT) | Event emitted with `context_source="for_iterable"`, `added_dependencies` non-empty | M4-F18 |
| Assignment inside if-branch with untrusted condition (STRICT) | Event emitted with `context_source="if_condition"` | M4-F18 |
| Post-Q-LLM assignment (STRICT) | Event emitted with `context_source="post_qllm"` | M4-F18 |
| Same assignment in loop (10 iterations) | Single deduped event (not 10 events) | M4-F18 |
| Assignment in NORMAL mode | No event emitted | M4-F18 (negative) |
| Assignment with empty ctx context (STRICT, top level) | No event emitted (empty context) | M4-F18 (negative) |
| `strict_dep_audit_log` property | Returns list snapshot; isolation from internal list | M4-F18 |

### 8.3 End-to-End Integration Tests

| Scenario | Expected Result |
|---|---|
| User query → P-LLM plan with data-derived callable → escalation blocked | `DataToControlFlowEscalationError` propagates out of orchestrator; audit trail complete |
| User query → normal plan → STRICT loop assigns variables | `StrictDependencyAdditionEvent` log populated; policy evaluation uses correct taint |

Tests should be added to:
- `tests/test_allowlist_enforcement.py` — or a new `tests/test_escalation_detection.py`
- `tests/integration/test_strict_mode_e2e.py` — end-to-end scenario

---

## 9. Verification Record

_Status: ✅ Implemented and verified — 2026-03-17_

| Feature | Verification method | Status |
|---|---|---|
| M4-F15 | Unit tests for `DataToControlFlowWarning` detection (direct and indirect sources) | ✅ Verified |
| M4-F16 | Unit tests for EVALUATION/PRODUCTION gate modes; integration test for policy bypass prevention | ✅ Verified |
| M4-F18 | Unit tests for `StrictDependencyAdditionEvent` emission, context_source tagging, deduplication | ✅ Verified |

### 9.1 Implementation Locations

| Component | File |
|---|---|
| `DataToControlFlowWarning`, `DataToControlFlowEscalationError` | `camel/exceptions.py` |
| `ElevatedConsentCallback` Protocol | `camel/interpreter.py` |
| `DataToControlFlowAuditEvent`, `StrictDependencyAdditionEvent` | `camel/interpreter.py` |
| M4-F15 check — `_eval_Call` insertion point | `camel/interpreter.py` |
| `_handle_escalation()`, `_emit_escalation_audit_event()` | `camel/interpreter.py` |
| `_emit_strict_dep_event()`, deduplication logic | `camel/interpreter.py` |
| `strict_dep_audit_log` property | `camel/interpreter.py` |
| Orchestrator `strict_dep_audit_log` delegation | `camel/execution_loop.py` |
| Exports | `camel/__init__.py` |
| Unit + integration tests | `tests/test_escalation_detection.py` |

### 9.2 Audit Trail

**2026-03-17:** M4-F15, M4-F16, M4-F18 implementation complete.  All unit tests
pass.  PRD §7.1, §7.2, and Known Limitations L6 updated in
`docs/architecture.md §10.1`, `§10.2`, `§10.5`.  Milestone 4 design document
section 4 marked ✅ Implemented.  Data-to-control-flow escalation is now a
runtime-detected and blocked attack vector in the CaMeL security model.
