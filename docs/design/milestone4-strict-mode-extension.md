# Milestone 4 — STRICT Mode Extension Design

_Author: Software Architect | Date: 2026-03-17 | Status: Implemented — all M4-F1 through M4-F5 features delivered and verified_

---

## 1. Overview

This document specifies the design for extending CaMeL's STRICT execution mode to
cover three additional dependency-propagation rules (M4-F1 through M4-F4) and
establishes STRICT mode as the default execution mode (M4-F5).

The existing interpreter already implements STRICT mode for `ast.If` test
expressions and `ast.For` iterable expressions.  This design note documents those
rules formally, extends them to cover post-`query_quarantined_llm()` statement
tainting, and specifies the configuration flag change.

---

## 2. Feature Specifications

### 2.1 M4-F1 — For-Loop Iterable Dependency Propagation (`ast.For`)

**Rule:** In STRICT mode, the capability set and variable-dependency set of the
`for`-loop iterable expression are merged into **every assignment** that occurs
inside the loop body (including nested blocks).

**Motivation:** If an adversary can influence which elements a loop iterates over,
they indirectly influence every value assigned inside the loop.  Without this rule,
a policy engine could approve a tool call that carries the iterable's untrusted
provenance without realising it.

**AST node hooked:** `ast.For`

**Graph mutation performed in `_exec_For`:**

```
iter_cv  = self._eval(node.iter, ctx_caps)          # evaluate iterable
iter_deps = {vars referenced in node.iter}           # from _tracking set

# Capability context passed to loop body:
inner_ctx_caps = _merge_ctx_caps(iter_cv, ctx_caps) # STRICT only

# Dependency context passed to loop body:
inner_dep_ctx  = outer_ctx_deps | iter_deps          # STRICT only

# Every ast.Assign / ast.AugAssign inside the body calls:
#   _dep_graph.record(target, direct_deps | inner_dep_ctx)
```

Loop target assignment additionally receives `iter_deps ∪ outer_ctx_deps` as its
dependency set (via `_dep_ctx_stack`), so the loop variable itself carries the
iterable's dependency.

**NORMAL mode behaviour:** `inner_ctx_caps = ctx_caps` (unchanged); loop body
receives `frozenset()` as context dependency.

**Status:** Already implemented.  This document formalises the existing behaviour.

---

### 2.2 M4-F2 — If/Else Conditional Test Dependency Propagation (`ast.If`)

**Rule:** In STRICT mode, the capability set and variable-dependency set of the
`if`-statement's test expression are merged into **every assignment** in both the
`body` (true-branch) and the `orelse` (false-branch).

**Motivation:** If the branch taken depends on untrusted data, every value
produced inside either branch is implicitly influenced by that untrusted data.
Closing this channel prevents an adversary from learning the value of a private
boolean by observing which downstream tool calls are made.

**AST node hooked:** `ast.If`

**Graph mutation performed in `_exec_If`:**

```
test_cv   = self._eval(node.test, ctx_caps)          # evaluate condition
test_deps = {vars referenced in node.test}            # from _tracking set

# Capability context forwarded to both branches:
inner_ctx_caps = _merge_ctx_caps(test_cv, ctx_caps)  # STRICT only

# Dependency context forwarded to both branches:
inner_dep_ctx  = outer_ctx_deps | test_deps           # pushed to _dep_ctx_stack

# Both node.body and node.orelse are executed with inner_ctx_caps / inner_dep_ctx.
# Every ast.Assign / ast.AugAssign inside either branch calls:
#   _dep_graph.record(target, direct_deps | inner_dep_ctx)
```

**NORMAL mode behaviour:** `inner_ctx_caps = ctx_caps` (unchanged); branches
receive `frozenset()` as context dependency.

**Status:** Already implemented.  This document formalises the existing behaviour.

---

### 2.3 M4-F3 / M4-F4 — Post-Q-LLM-Call Statement Dependency Propagation

**Rule (M4-F3):** When `query_quarantined_llm()` is called anywhere in a
statement list (as an `ast.Expr` bare call _or_ on the RHS of an `ast.Assign`),
STRICT mode activates a **"STRICT remainder" flag** for the rest of the current
statement list.  Every subsequent `ast.Assign` and `ast.AugAssign` in that
statement list — at the same nesting level — inherits the Q-LLM call's result
`CaMeLValue` as additional `ctx_caps`, and the Q-LLM call's variable name (if
bound) as an additional context dependency.

**Rule (M4-F4):** The STRICT remainder flag is scoped to the **current code
block** (`_exec_statements` frame).  It does _not_ retroactively affect statements
that already executed before the Q-LLM call, but it does propagate into nested
blocks encountered after the call.  The flag is reset when the block exits.

**Motivation:** The Q-LLM processes untrusted data and returns a structured
(but still untrusted) result.  In STRICT mode, any variable assigned after a
Q-LLM call may be transitively influenced by the Q-LLM's output — even if the
assignment does not directly reference the Q-LLM result variable.  Tainting
subsequent assignments closes this implicit flow channel.

**AST node hooked:** `ast.Call` where `node.func` is `ast.Name` with
`id == "query_quarantined_llm"` (or any name registered as the Q-LLM tool).
Detection occurs inside `_eval_Call` (for the `ast.Assign` path) and
`_exec_Expr` (for the bare-call path).

**Implementation approach — `_exec_statements` redesign:**

The current `_exec_statements` iterates statements and forwards a fixed
`ctx_caps`.  To support M4-F3/F4, `_exec_statements` is modified to carry a
**mutable** `ctx_caps` accumulator that can be updated mid-loop:

```
def _exec_statements(
    self,
    stmts: list[ast.stmt],
    ctx_caps: CaMeLValue | None,
) -> None:
    # ctx_caps is now a local accumulator (not mutated on the caller's side).
    current_ctx = ctx_caps
    for stmt in stmts:
        self._exec_statement(stmt, current_ctx)
        if self._mode is ExecutionMode.STRICT:
            # After executing the statement, check if a Q-LLM call was made.
            # The call sets self._last_qllm_result_cv when it returns.
            if self._last_qllm_result_cv is not None:
                current_ctx = _merge_ctx_caps(self._last_qllm_result_cv, current_ctx)
                self._last_qllm_result_cv = None  # consume the signal
```

**New interpreter state field:**

```python
self._last_qllm_result_cv: CaMeLValue | None = None
```

This field is set to the Q-LLM call's returned `CaMeLValue` immediately after the
call returns (inside `_eval_Call`, in the tool dispatch branch, when `name ==
"query_quarantined_llm"`).  It is consumed (set back to `None`) by
`_exec_statements` after updating `current_ctx`.

**Graph mutation:**

For every assignment occurring after the Q-LLM call in the same block,
`_dep_graph.record(target, direct_deps | ctx_deps)` already handles the
additional dependency — because `ctx_deps` is derived from the updated
`current_ctx` (via `_dep_ctx_stack`).  The dependency-stack update mirrors the
`ctx_caps` update: push `outer_ctx_deps | {qllm_variable_name}` (if the Q-LLM
call was assigned to a variable) onto `_dep_ctx_stack` for the remainder of the
block, and pop it when the block exits.

**Interaction with M4-F1 / M4-F2:** The STRICT remainder `ctx_caps` is forwarded
into nested `if` and `for` blocks encountered after the Q-LLM call, where it is
merged with the test/iterable `ctx_caps` by the existing M4-F1 / M4-F2 logic.
No special casing is required.

**Scope boundary:** The `_last_qllm_result_cv` signal is consumed within the same
`_exec_statements` call frame.  If the Q-LLM call is itself inside a nested `if`
or `for` block, the signal propagates only to the remainder of _that_ nested
block, not to the outer block.  (The outer block's `current_ctx` is updated by
M4-F1 / M4-F2 when the nested block's `ctx_caps` bubbles up.)

**NORMAL mode behaviour:** `_last_qllm_result_cv` is still set (detection still
runs) but `_exec_statements` does not update `current_ctx` when in NORMAL mode.

---

### 2.4 M4-F5 — STRICT Mode as Default; NORMAL Mode as Explicit Opt-In

**Rule:** `ExecutionMode.STRICT` becomes the default value for the `mode`
parameter in `CaMeLInterpreter.__init__`.  Callers who need NORMAL mode must
explicitly pass `mode=ExecutionMode.NORMAL`.

**Config key:** `mode` parameter of `CaMeLInterpreter.__init__`.

**Default value change:**

```python
# Before (current):
def __init__(
    self,
    ...
    mode: ExecutionMode = ExecutionMode.NORMAL,
    ...
) -> None: ...

# After (M4-F5):
def __init__(
    self,
    ...
    mode: ExecutionMode = ExecutionMode.STRICT,
    ...
) -> None: ...
```

**Propagation:** `ExecutionMode` is already a first-class enum stored as
`self._mode`.  The `set_mode()` public method remains unchanged; callers can
still switch modes at runtime.

**Documentation string change:** The `mode` parameter docstring in `CaMeLInterpreter`
must be updated to reflect the new default:

```
mode:
    Execution mode controlling side-channel capability propagation.
    Defaults to ``ExecutionMode.STRICT`` (recommended for production use).
    Pass ``ExecutionMode.NORMAL`` only for debugging or performance-sensitive
    scenarios where side-channel mitigations are not required.
```

**PRD section 6.3 update:** The CaMeL Interpreter component description in
`CLAUDE.md` and `docs/architecture.md` must be updated to document STRICT as the
default mode (currently says NORMAL is default).

**Test suite impact:** Any existing test that constructs `CaMeLInterpreter()` with
no `mode` argument and relies on NORMAL-mode behaviour will begin failing.  All
such tests must either:
  - Pass `mode=ExecutionMode.NORMAL` explicitly, or
  - Be updated to assert the new (stricter) dependency graph output.

A compatibility audit of the test suite must be completed before M4-F5 is merged.

---

## 3. AST Node Summary

| Feature | AST Node Hooked | Hook Site | Graph Mutation |
|---------|-----------------|-----------|----------------|
| M4-F1   | `ast.For` | `_exec_For` | `record(target, direct ∪ iter_deps ∪ ctx_deps)` |
| M4-F2   | `ast.If` | `_exec_If` | `record(target, direct ∪ test_deps ∪ ctx_deps)` in both branches |
| M4-F3/F4 | `ast.Call` (name == `"query_quarantined_llm"`) | `_eval_Call` (signal) + `_exec_statements` (consume) | `record(target, direct ∪ qllm_deps ∪ ctx_deps)` for all subsequent stmts in block |
| M4-F5   | _n/a_ | `CaMeLInterpreter.__init__` | Default `mode` parameter changed from `NORMAL` to `STRICT` |

---

## 4. New Interpreter State Fields (M4-F3/F4)

| Field | Type | Initial value | Purpose |
|-------|------|---------------|---------|
| `_last_qllm_result_cv` | `CaMeLValue \| None` | `None` | Signals to `_exec_statements` that a Q-LLM call completed; set in `_eval_Call`, consumed by `_exec_statements` |

No other new fields are required; the existing `_dep_ctx_stack`, `_tracking`, and
`ctx_caps` propagation machinery handles the graph mutations.

---

## 5. Interaction Diagram — M4-F3/F4 Signal Flow

```
_exec_statements(stmts, ctx_caps=None)
  │
  ├─ stmt[0]: x = some_tool()          → _exec_Assign → _eval_Call(some_tool)
  │                                        _last_qllm_result_cv = None  ← no signal
  │
  ├─ stmt[1]: result = query_quarantined_llm(x, Schema)
  │             → _exec_Assign
  │               → _eval_Call("query_quarantined_llm")
  │                   → calls Q-LLM wrapper, gets result_cv
  │                   → self._last_qllm_result_cv = result_cv   ← SIGNAL SET
  │               ← returns result_cv
  │             ← stores result_cv in _store["result"]
  │           ← _exec_statements detects signal, updates:
  │               current_ctx = _merge_ctx_caps(result_cv, current_ctx)
  │               push {result_var_name} onto _dep_ctx_stack
  │               _last_qllm_result_cv = None  ← SIGNAL CONSUMED
  │
  ├─ stmt[2]: y = compute(result)      → records y depends on {result} ∪ {result} (ctx)
  │                                      = depends on {result} with Q-LLM caps merged
  │
  └─ stmt[3]: send_email(to, y)        → policy engine sees y carries Q-LLM taint
                                          → correctly denied if untrusted recipients
```

---

## 6. Files to be Modified

| File | Change |
|------|--------|
| `camel/interpreter.py` | Add `_last_qllm_result_cv` field; modify `_exec_statements` to update `current_ctx` on Q-LLM signal; change default `mode` to `STRICT` |
| `docs/architecture.md` | Update §CaMeL Interpreter to document STRICT as default; add M4-F3/F4 |
| `docs/api/interpreter.md` | Update `mode` parameter docs; document `_last_qllm_result_cv` lifecycle |
| `CLAUDE.md` PRD §6.3 | Update STRICT/NORMAL description to reflect default change |
| `tests/test_interpreter.py` | Audit for implicit NORMAL-mode reliance; fix or annotate |
| `tests/test_dependency_graph.py` | Add ≥5 test programs for M4-F3/F4 post-Q-LLM tainting |

---

## 7. PRD Section 6.3 Exact Wording Changes

This section satisfies the acceptance criterion requiring exact before/after wording for the
CaMeL Interpreter description in `CLAUDE.md` PRD §6.3 and `docs/architecture.md`.

### 7.1 CLAUDE.md — PRD §6.3 Default-Mode Statement

**Before (pre-M4-F5):**
```
Supports two execution modes (NORMAL is the default):
- STRICT mode: ...
- NORMAL mode (default): ...
```

**After (M4-F5):**
```
Supports two execution modes (STRICT is the default as of v0.4.0 — Milestone 4, M4-F5):
- STRICT mode (default): Closes control-flow and Q-LLM side-channel vectors via three
  propagation rules: ...
- NORMAL mode (explicit opt-in: pass mode=ExecutionMode.NORMAL to CaMeLInterpreter):
  Dependencies tracked only via direct data assignments; no control-flow taint propagation.
  Use only for debugging or non-security-sensitive scenarios.
```

**Rationale:** `ExecutionMode.STRICT` is now the constructor default (§2.4).  Any deployment
that does not explicitly opt out is automatically protected by all three propagation rules.

---

### 7.2 CLAUDE.md — PRD §6.3 Interpreter Description Additions

Add the following bullet points to the `CaMeL Interpreter` subsection of §6.3 to document
the three new STRICT mode propagation rules (M4-F1 through M4-F4):

**Add after the STRICT/NORMAL mode paragraph:**
```
  (M4-F1) For-loop iterable — the iterable's capability and dependency set is merged into
  every assignment inside the loop body, including nested blocks.
  (M4-F2) If/else test — the condition's capability and dependency set is merged into every
  assignment in both the true and false branches.
  (M4-F3/F4) Post-query_quarantined_llm() remainder — all assignments following a Q-LLM call
  in the same code block inherit the Q-LLM result's capabilities as additional context
  dependencies; the flag is scoped to the current block and resets on block exit.
```

---

### 7.3 docs/architecture.md — Execution Mode Table Update

**Before (pre-M4-F5):**
```
| `STRICT` | No — opt-in | Data + control-flow | Closes timing/control-flow channels |
| `NORMAL` | **Yes** (default) | Data assignments only | No control-flow taint |
```

**After (M4-F5):**
```
| `STRICT` | **Yes** (v0.4.0+) | Data + control-flow (if-test, for-iterable, post-Q-LLM) | Closes timing/control-flow channels |
| `NORMAL` | No — explicit opt-in | Data assignments only | No control-flow taint |
```

---

### 7.4 docs/api/interpreter.md — `mode` Parameter Docstring

**Before:**
```
mode:
    Execution mode controlling dependency tracking.
    Defaults to ``ExecutionMode.NORMAL``.
```

**After:**
```
mode:
    Execution mode controlling side-channel capability propagation.
    Defaults to ``ExecutionMode.STRICT`` (recommended for production use).
    Pass ``ExecutionMode.NORMAL`` only for debugging or performance-sensitive
    scenarios where side-channel mitigations are not required.
```

---

## 8. Open Questions (Pre-Implementation)

1. **Q-LLM name registration:** Should the Q-LLM detection in `_eval_Call` be
   hardcoded to `"query_quarantined_llm"` or should it check a configurable
   registry of "taint-trigger" tool names?  Recommendation: use a frozenset
   `_QLLM_TOOL_NAMES = frozenset({"query_quarantined_llm"})` on the class, to
   allow future extension without changing the detection logic.

2. **Block-exit dep-stack cleanup:** The M4-F3/F4 dependency-stack push must be
   paired with a pop at block exit.  Confirm that `_exec_statements` cleans up
   the stack on both normal exit and exception paths (use `try/finally`).

3. **Test suite backward compatibility:** A full audit of tests that construct
   `CaMeLInterpreter()` without a `mode` argument is required before M4-F5 lands.
   This audit should be task-001's blocker before task-002 (implementation) begins.

---

## 9. Review Checklist

_Signed off post-implementation — all items verified._

- [x] All five feature IDs (M4-F1 through M4-F5) are covered by this document
- [x] Each AST node hook is named explicitly (§3 table)
- [x] Each graph mutation is specified precisely (§2.x algorithm blocks)
- [x] Default-mode flag design specifies config key, default value, and constructor
      signature change (§2.4)
- [x] New state field `_last_qllm_result_cv` is described with type, lifecycle,
      and producer/consumer sites (§4, §5)
- [x] Files requiring modification are enumerated (§6)
- [x] PRD §6.3 exact wording changes listed (§7)
- [x] Open questions are captured for the implementation team (§8)
- [x] Document is internally consistent (no contradictions between §2 and §3–§6)

---

## 10. Verification

_Added post-implementation to record test evidence for each delivered feature._

| Feature | Test File | Key Test(s) |
|---------|-----------|-------------|
| M4-F1 — For-loop iterable propagation | `tests/test_dependency_graph.py` | `test_strict_for_iterable_*` |
| M4-F2 — If/else test propagation | `tests/test_dependency_graph.py` | `test_strict_if_condition_*` |
| M4-F3 — Post-Q-LLM remainder tainting | `tests/test_dependency_graph.py`, `tests/test_strict_mode.py` | `test_strict_post_qllm_*` |
| M4-F4 — Block-scoped remainder flag | `tests/test_strict_mode.py` | `test_strict_qllm_scope_boundary` |
| M4-F5 — STRICT as default mode | `tests/test_interpreter.py`, `tests/test_strict_mode.py` | `test_default_mode_is_strict` |

**End-to-end integration:** `tests/integration/test_strict_mode_e2e.py` covers all five
features in a complete execution-loop scenario with real capability propagation and
policy evaluation.

**Status:** All test suites pass.  STRICT mode is the production default as of v0.4.0.

---

## 11. Exception Hardening — M4-F6, M4-F7, M4-F8, M4-F9, M4-F17

_Author: Software Architect | Date: 2026-03-17 | Status: Implemented — all M4-F6 through M4-F9 and M4-F17 features delivered and verified — 2026-03-17_

### 11.1 Overview

This section specifies the design for hardening all exception-handling pathways
in the CaMeL interpreter and execution loop to eliminate exception-based
information-leakage channels.  It covers five feature IDs:

| Feature | Short description |
|---------|-------------------|
| M4-F6   | Dependency-aware exception message redaction → `[REDACTED]` |
| M4-F7   | `NotEnoughInformationError` handler — strips content, exposes type + line only |
| M4-F8   | STRICT mode annotation preservation across NEIE re-generation cycles |
| M4-F9   | Loop-body exception STRICT propagation for non-public-iterable loops |
| M4-F17  | Audit log event schema for every redaction event |

These features extend the existing `ExceptionRedactor` class
(`camel/execution_loop.py`) and the CaMeL interpreter
(`camel/interpreter.py`), without altering the public API of either.

---

### 11.2 M4-F6 — Dependency-Aware Exception Message Redaction

#### 11.2.1 Problem

The current `ExceptionRedactor.classify()` inspects the interpreter store
**snapshot** to decide whether any in-scope variable has untrusted provenance.
That check (`_store_has_untrusted`) is coarse: it taints the whole exception
message as soon as *any* store variable carries an untrusted source — even if
the failing statement has no dependency on that variable.

More critically, the check does not consult the **dependency graph**.  A
variable whose `sources` field is `{"CaMeL"}` may still transitively depend on
untrusted data through the graph.  In STRICT mode, the dependency graph is the
authoritative record of taint propagation and must be the primary input to the
redaction decision.

#### 11.2.2 Taint Check Algorithm

The enhanced taint check for a failing statement at line `L` is:

```
function is_tainted(exc, interpreter) -> bool:
    # Step 1: collect variables referenced in the failing statement.
    failing_vars = interpreter.get_variables_at_line(L)
    # If line number is unavailable, fall back to the full store.
    if not failing_vars:
        failing_vars = set(interpreter.store.keys())

    # Step 2: for each referenced variable, walk the dependency graph.
    for var in failing_vars:
        dep_graph = interpreter.get_dependency_graph(var)
        for upstream_var in dep_graph.all_upstream():
            cv = interpreter.store.get(upstream_var)
            if cv is not None:
                for src in cv.sources:
                    if src not in TRUSTED_SOURCES:
                        return True  # tainted

    # Step 3: also check direct capability tags of variables in the failing stmt.
    for var in failing_vars:
        cv = interpreter.store.get(var)
        if cv is not None:
            for src in cv.sources:
                if src not in TRUSTED_SOURCES:
                    return True

    return False
```

`TRUSTED_SOURCES` remains `frozenset({"User literal", "CaMeL"})`.

#### 11.2.3 `[REDACTED]` Substitution Rule

When `is_tainted()` returns `True`:

- `RedactedError.message` is set to `None` (already the existing behaviour).
- The `trust_level` field is set to `"untrusted"`.
- The human-readable retry prompt produced by `RetryPromptBuilder` **must not**
  include the literal exception message string.  The prompt builder already
  omits `message` when it is `None`; no change is required there.

The string literal `"[REDACTED]"` is **not** placed in `RedactedError.message`
— `None` is the sentinel.  `[REDACTED]` appears only in the audit log entry
(§11.6) to make the redaction visible to operators.

#### 11.2.4 Traceback Scrubbing Scope

Tracebacks are **never** forwarded to the P-LLM.  `RetryPromptBuilder.build()`
accepts only `RedactedError` (a dataclass with no traceback field); the raw
exception and its traceback are consumed entirely within
`CaMeLOrchestrator._handle_exception()` and discarded after `classify()`
returns.  No additional traceback-scrubbing logic is needed at the prompt layer.

For the audit log (§11.6), a sanitised traceback summary may be included, but
only the frame function names and line numbers — never local variable values or
exception message text when the exception is tainted.

---

### 11.3 M4-F7 — `NotEnoughInformationError` Handler

#### 11.3.1 Invariant

When `NotEnoughInformationError` (either `camel.exceptions.NotEnoughInformationError`
or `camel.llm.exceptions.NotEnoughInformationError`) propagates out of the
interpreter, **zero bytes** of Q-LLM output may reach the P-LLM.  This applies
regardless of whether the error was raised by the Q-LLM wrapper or re-raised
inside the interpreter.

The following table defines exactly which fields are populated in `RedactedError`
for this case:

| Field | Value | Rationale |
|-------|-------|-----------|
| `error_type` | `"NotEnoughInformationError"` | Exception class name; safe — it is a fixed string |
| `lineno` | call-site line number from the AST node | Needed by P-LLM to locate the failing call; contains no untrusted data |
| `message` | `None` | Never included — would risk exposing Q-LLM output |
| `trust_level` | `"not_enough_information"` | Identifies the redaction rule applied |

#### 11.3.2 Call-Site Line Number Extraction

The current implementation sets `lineno=None` for NEIE.  This must change to
`lineno=<AST line number of the query_quarantined_llm() call>`.

The line number is obtained from the interpreter at the point the exception
is caught, before the store snapshot is taken:

```python
# Inside CaMeLOrchestrator._handle_exception():
lineno = getattr(exc, "lineno", None)
if lineno is None:
    # NEIE may carry a lineno if the interpreter attached it; fall back to None.
    lineno = getattr(exc, "__lineno__", None)
```

To support this, the CaMeL interpreter **must** attach the AST node's
`lineno` attribute to the `NotEnoughInformationError` before re-raising it
from `_eval_Call`, when the call being evaluated is `query_quarantined_llm`.
This is implemented via a lightweight `_attach_lineno` helper:

```python
def _attach_lineno(exc: BaseException, node: ast.AST) -> BaseException:
    """Attach AST line number to exc without altering exc.args."""
    exc.__lineno__ = getattr(node, "lineno", None)  # type: ignore[attr-defined]
    return exc
```

The `ExceptionRedactor.classify()` method reads `exc.__lineno__` for NEIE
(falling back to `getattr(exc, "lineno", None)` for other exception types).

#### 11.3.3 Mapping to the P-LLM Retry Loop (NFR-5)

NFR-5 requires the retry loop to handle up to 10 code-generation failures
gracefully without exposing untrusted error content.  The NEIE path maps into
the existing retry loop as follows:

```
NEIE caught by orchestrator
  │
  ├─ ExceptionRedactor.classify()  → RedactedError(
  │                                     error_type="NotEnoughInformationError",
  │                                     lineno=<call-site>,
  │                                     message=None,
  │                                     trust_level="not_enough_information"
  │                                  )
  │
  ├─ AcceptedState snapshot taken  (see §11.4 for STRICT annotation preservation)
  │
  ├─ RetryPromptBuilder.build()    → user-turn message with:
  │     • error_type only (no message, no Q-LLM content)
  │     • call-site line number
  │     • already-defined variable names (opaque handles)
  │
  └─ P-LLM regenerates remaining plan
       (attempt count incremented; MaxRetriesExceededError after 10)
```

The P-LLM retry prompt for NEIE should include a short advisory sentence (added
by `RetryPromptBuilder` when `trust_level == "not_enough_information"`):

```
The call to query_quarantined_llm() at line {lineno} reported that the provided
context did not contain enough information to populate the requested schema.
Consider restructuring the query or passing additional context variables.
```

This sentence contains **no untrusted data** — only the line number and a
fixed advisory.

---

### 11.4 M4-F8 — STRICT Mode Annotation Preservation Across NEIE Re-generation

#### 11.4.1 Problem

When NEIE is raised and the orchestrator triggers a P-LLM re-generation, the
interpreter is **not** reset.  The accepted state (variable names) is preserved
so the P-LLM can reference already-computed values via opaque handles.  However,
the interpreter's STRICT mode dependency annotations (dependency graph entries
and `_dep_ctx_stack`) are part of its mutable state.

If the interpreter's internal tracking state is partially in flight at the point
NEIE is raised (e.g., inside a `for`-loop body or after a Q-LLM call that
previously updated `_last_qllm_result_cv`), the annotations accumulated up to
that point must be **preserved and restored** after re-generation, so that the
regenerated code picks up the correct dependency context.

#### 11.4.2 Variable Scopes Snapshotted

The `AcceptedState` object is extended with two new fields:

```python
@dataclass(frozen=True)
class AcceptedState:
    variable_names: frozenset[str]
    executed_statement_count: int
    remaining_source: str
    # New fields for M4-F8:
    dependency_graph_snapshot: dict[str, frozenset[str]]
    # Mapping: variable_name → frozenset of upstream dependency variable names.
    # Taken by calling interpreter.snapshot_dependency_graph() at the moment of
    # NEIE capture, before any retry-prompt construction.
    dep_ctx_stack_snapshot: list[frozenset[str]]
    # Copy of the interpreter's _dep_ctx_stack at the moment of NEIE capture.
    # Used to restore the STRICT-mode context-dependency stack on re-entry.
```

`dependency_graph_snapshot` covers **all variables currently in the store** —
i.e., every variable that was assigned before the NEIE-raising statement.

`dep_ctx_stack_snapshot` captures the context-dependency stack at the NEIE
capture point.  This is the stack used by STRICT mode to propagate dependencies
from enclosing `for`/`if` blocks and post-Q-LLM remainders.

#### 11.4.3 Snapshot and Restore Protocol

```
On NEIE caught by orchestrator:
  1. Call interpreter.snapshot_accepted_state():
       → dependency_graph_snapshot = interpreter.dep_graph.export()
       → dep_ctx_stack_snapshot    = list(interpreter._dep_ctx_stack)
       → variable_names            = frozenset(interpreter.store.keys())
       → executed_statement_count  = <count before failing stmt>
       → remaining_source          = <ast.unparse of unexecuted stmts>
  2. Construct AcceptedState with all five fields.
  3. Build retry prompt (see §11.3.3).
  4. Obtain regenerated plan from P-LLM.
  5. Before executing the regenerated plan, call:
       interpreter.restore_accepted_state(accepted_state):
         → interpreter.dep_graph.import(accepted_state.dependency_graph_snapshot)
         → interpreter._dep_ctx_stack[:] = accepted_state.dep_ctx_stack_snapshot
     The variable store is already intact (it was not cleared between retries).

On successful plan completion after retry:
  6. dep_ctx_stack_snapshot is discarded; the stack is managed normally by the
     interpreter as the regenerated plan executes.
```

#### 11.4.4 Scope of Preservation

Only NEIE triggers the snapshot-restore cycle.  Other exception types that
trigger the retry loop do **not** use this mechanism:

- **Trusted exceptions:** The interpreter may be reset (depending on the
  orchestrator's retry strategy) or continue from the accepted state.  Either
  way, the dependency graph and dep-ctx stack are reset or continue normally.
- **Untrusted-dependency exceptions:** Same as trusted — no special snapshot
  required beyond the existing `AcceptedState` mechanism.

The rationale: NEIE is the only exception class where the interpreter's state
is explicitly valid up to the failing Q-LLM call, and where re-generation is
expected to produce code that references those accumulated variables.  Other
exception types indicate plan logic errors where a full or partial re-plan is
the correct response.

---

### 11.5 M4-F9 — Loop-Body Exception STRICT Propagation for Non-Public-Iterable Loops

#### 11.5.1 Problem

In STRICT mode, M4-F1 ensures that every **assignment** inside a `for`-loop
body carries the iterable's dependency.  However, if an exception is raised
inside the loop body, the statements that **would have executed after the
exception** are never reached.  When the orchestrator retries with a regenerated
plan, those post-exception statements in the regenerated code must still inherit
the iterable's dependency — otherwise a partial execution followed by retry
could silently drop the iterable's taint from downstream values.

#### 11.5.2 Trigger Condition

M4-F9 applies when ALL of the following are true:

1. The exception originates inside a `for`-loop body (i.e., the call stack at
   the point of exception includes an `_exec_For` frame).
2. The loop's iterable expression has a **non-public** dependency — i.e., at
   least one upstream source of the iterable `CaMeLValue` is not in
   `TRUSTED_SOURCES` or the `readers` field is not `Public`.
3. The interpreter is in `ExecutionMode.STRICT`.

If any condition is false, M4-F9 does not apply and normal redaction rules
(M4-F6/F7) govern the exception.

#### 11.5.3 Implementation — Loop-Exception Context Annotation

The `_exec_For` method in the interpreter is modified to wrap the loop body
execution in a `try/except` block that, on exception, **attaches the iterable's
dependency set** to the exception before re-raising:

```python
def _exec_For(self, node: ast.For, ctx_caps, dep_ctx) -> None:
    iter_cv = self._eval(node.iter, ctx_caps)
    iter_deps = self._collect_var_refs(node.iter)

    if self._mode is ExecutionMode.STRICT:
        inner_ctx = _merge_ctx_caps(iter_cv, ctx_caps)
        inner_dep_ctx = dep_ctx | iter_deps
    else:
        inner_ctx = ctx_caps
        inner_dep_ctx = dep_ctx

    try:
        for elem in iter_cv.raw:
            # ... assign loop variable, exec body stmts ...
    except Exception as exc:
        if self._mode is ExecutionMode.STRICT and self._is_non_public(iter_cv):
            # Attach loop-context dependency metadata to the exception.
            exc.__loop_iter_deps__ = inner_dep_ctx          # type: ignore[attr-defined]
            exc.__loop_iter_caps__ = inner_ctx               # type: ignore[attr-defined]
        raise
```

#### 11.5.4 Use in Post-Exception Statement Annotation

When the orchestrator catches an exception carrying `__loop_iter_deps__`, the
`AcceptedState` snapshot is extended with:

```python
loop_iter_deps: frozenset[str] | None
# Set to exc.__loop_iter_deps__ when M4-F9 applies; None otherwise.
loop_iter_caps: CaMeLValue | None
# Set to exc.__loop_iter_caps__ when M4-F9 applies; None otherwise.
```

On re-entry into the regenerated plan, the interpreter's `_dep_ctx_stack` is
pre-seeded with `loop_iter_deps` and `_ctx_caps` is set to `loop_iter_caps`
before the regenerated code executes.  This ensures the first assignment in the
regenerated code already carries the iterable's dependency — exactly as if the
loop body had continued executing.

#### 11.5.5 `_is_non_public` Helper

```python
def _is_non_public(self, cv: CaMeLValue) -> bool:
    """Return True if cv has any non-trusted source or non-Public readers."""
    from camel.value import Public
    if cv.readers is not Public and cv.readers:
        return True
    return any(src not in TRUSTED_SOURCES for src in cv.sources)
```

---

### 11.6 M4-F17 — Audit Log Event Schema for Redaction Events

#### 11.6.1 Overview

Per NFR-6, every redaction event must be written to the security audit log.
The existing audit log infrastructure (introduced in Milestone 3 Enforcement
Integration) emits `AuditLogEntry` records for policy evaluation outcomes and
user consent decisions.  M4-F17 extends this with a new event type:
`RedactionAuditEvent`.

#### 11.6.2 Event Schema

```python
@dataclass(frozen=True)
class RedactionAuditEvent:
    """Audit log entry emitted whenever exception message redaction occurs.

    Emitted by ExceptionRedactor.classify() for every exception it processes,
    regardless of whether redaction was applied (trust_level documents the
    outcome). This provides a complete audit trail of all exception events.

    Attributes
    ----------
    timestamp:
        ISO-8601 UTC timestamp of the redaction decision.
        Format: "YYYY-MM-DDTHH:MM:SS.ffffffZ"
    line_number:
        1-based AST source line number of the failing statement, or None
        if unavailable.
    redaction_reason:
        Human-readable explanation of why redaction was applied (or "none"
        if no redaction). One of:
          - "untrusted_dependency"  — M4-F6 taint check returned True
          - "not_enough_information" — M4-F7 NEIE handler applied
          - "loop_body_exception"   — M4-F9 loop propagation applied
          - "none"                  — trusted exception; no redaction
    dependency_chain:
        Ordered list of (variable_name, source_label) pairs representing
        the taint propagation path that triggered redaction. Empty list
        when redaction_reason is "none".
        Example: [("email_body", "get_last_email"), ("summary", "CaMeL")]
    trust_level:
        The trust_level field from the resulting RedactedError. One of
        "trusted", "untrusted", "not_enough_information".
    error_type:
        Exception class name (always included; never untrusted data).
    redacted_message_length:
        Length in characters of the original exception message before
        redaction, or 0 if the message was empty or None. Allows operators
        to gauge information-density of redacted content without
        re-exposing the content itself.
    m4_f9_applied:
        True if M4-F9 loop-body exception STRICT propagation was applied
        to this event.
    """

    timestamp: str                          # ISO-8601 UTC
    line_number: int | None
    redaction_reason: str                   # see docstring values above
    dependency_chain: list[tuple[str, str]] # [(var_name, source_label), ...]
    trust_level: str                        # "trusted" | "untrusted" | "not_enough_information"
    error_type: str
    redacted_message_length: int
    m4_f9_applied: bool
```

#### 11.6.3 Emission Point

`RedactionAuditEvent` is emitted inside `ExceptionRedactor.classify()`,
immediately after the `RedactedError` is constructed and before the method
returns.  The emitter is injected via a constructor parameter:

```python
class ExceptionRedactor:
    def __init__(
        self,
        trusted_sources: frozenset[str] | None = None,
        audit_log: AuditLog | None = None,   # NEW — injected sink
    ) -> None: ...
```

`AuditLog` is the existing audit-log sink interface from Milestone 3.  If
`audit_log` is `None`, the event is silently dropped (backward-compatible
default for tests that do not configure a log).

#### 11.6.4 Dependency Chain Population

The `dependency_chain` field is populated by traversing the dependency graph
for each variable involved in the failing statement:

```
dependency_chain = []
for var in failing_vars:
    dep_graph = interpreter.get_dependency_graph(var)
    for upstream_var in dep_graph.all_upstream():
        cv = interpreter.store.get(upstream_var)
        if cv:
            for src in cv.sources:
                if src not in TRUSTED_SOURCES:
                    dependency_chain.append((upstream_var, src))
# Deduplicate while preserving order.
dependency_chain = list(dict.fromkeys(dependency_chain))
```

The chain is truncated to a maximum of **50 entries** to prevent runaway log
growth in deeply nested dependency graphs.

---

### 11.7 Interaction Summary

The five features interact as follows during a single execution cycle:

```
Exception raised inside interpreter
  │
  ├─ [M4-F9] Is it inside a for-loop body with non-public iterable?
  │     Yes → attach __loop_iter_deps__, __loop_iter_caps__ to exc
  │
  ├─ ExceptionRedactor.classify(exc, store_snapshot)
  │     │
  │     ├─ [M4-F7] isinstance(exc, NEIE)?
  │     │     Yes → RedactedError(type, lineno, message=None, "not_enough_information")
  │     │           emit RedactionAuditEvent(redaction_reason="not_enough_information") [M4-F17]
  │     │
  │     ├─ [M4-F6] is_tainted(exc, interpreter)?
  │     │     Yes → RedactedError(type, lineno, message=None, "untrusted")
  │     │           emit RedactionAuditEvent(redaction_reason="untrusted_dependency") [M4-F17]
  │     │
  │     └─ else → RedactedError(type, lineno, message, "trusted")
  │               emit RedactionAuditEvent(redaction_reason="none") [M4-F17]
  │
  ├─ AcceptedState snapshot
  │     [M4-F8] If NEIE: include dependency_graph_snapshot + dep_ctx_stack_snapshot
  │     [M4-F9] If loop exc: include loop_iter_deps + loop_iter_caps
  │
  ├─ RetryPromptBuilder.build(accepted_state, redacted_error)
  │     NEIE case: adds advisory sentence (no untrusted data) [M4-F7]
  │
  └─ Interpreter restore before re-execution
        [M4-F8] Restore dep graph + dep-ctx stack if NEIE
        [M4-F9] Pre-seed dep-ctx stack if loop exc
```

---

### 11.8 Files to be Modified

| File | Change |
|------|--------|
| `camel/execution_loop.py` | Enhance `ExceptionRedactor.classify()` with dependency-graph taint check (M4-F6); add `lineno` extraction for NEIE (M4-F7); extend `AcceptedState` with snapshot fields (M4-F8, M4-F9); add `RedactionAuditEvent` dataclass and emission (M4-F17); update `RetryPromptBuilder.build()` with NEIE advisory (M4-F7) |
| `camel/interpreter.py` | Add `_attach_lineno` helper; modify `_exec_For` to catch and annotate loop-body exceptions (M4-F9); add `snapshot_dependency_graph()` and `restore_accepted_state()` methods (M4-F8); expose `dep_graph.export()` / `dep_graph.import()` surface |
| `camel/dependency_graph.py` | Add `export() -> dict[str, frozenset[str]]` and `import_(snapshot)` methods for M4-F8 snapshot/restore |
| `tests/test_redaction_completeness.py` | Add M4-F6 dependency-graph-deep taint scenarios; add M4-F7 NEIE lineno verification; add M4-F8 annotation-preservation test; add M4-F9 loop-exception propagation test |
| `tests/test_execution_loop.py` | Add `RedactionAuditEvent` emission tests (M4-F17); add NEIE advisory sentence test |
| `docs/api/execution_loop.md` | Document `RedactionAuditEvent`, updated `AcceptedState`, enhanced `ExceptionRedactor` |
| `docs/api/interpreter.md` | Document `snapshot_dependency_graph()`, `restore_accepted_state()`, `_attach_lineno` |

---

### 11.9 PRD Cross-References

This design updates the following PRD sections (to be reflected in
`docs/architecture.md`):

**PRD §6.2 — Q-LLM Wrapper:**
> `have_enough_information` boolean field; if false, raises
> `NotEnoughInformationError` — communicated to P-LLM **with call-site line
> number only**; no missing-data content is ever forwarded (M4-F7).
> STRICT mode annotation preservation (M4-F8) ensures re-generation cycles do
> not lose dependency context accumulated before the failing Q-LLM call.

**PRD §6.3 — CaMeL Interpreter:**
> Exception messages are redacted via a dependency-graph-aware taint check
> (M4-F6): if the failing statement's dependency graph includes any untrusted
> upstream source, the message is replaced with `[REDACTED]`.  Loop-body
> exceptions from non-public-iterable loops carry STRICT mode dependency
> annotations into the retry cycle (M4-F9).  All redaction events are written
> to the security audit log (M4-F17).

---

### 11.10 Open Questions

1. **`dep_graph.export()` format:** Should the export format be a plain
   `dict[str, frozenset[str]]` (variable → upstream set) or a full serialised
   graph object?  Recommendation: plain dict, sufficient for snapshot/restore
   and avoids coupling the format to internal graph representation.

2. **Audit log sink availability:** The `ExceptionRedactor` currently has no
   reference to the audit log.  The cleanest injection point is the constructor
   (as specified in §11.6.3).  Alternative: use a module-level singleton.
   Recommendation: constructor injection for testability (NFR-9).

3. **`lineno` attachment for non-NEIE exceptions:** Should `_attach_lineno` be
   called for all exceptions raised inside `_eval_Call`, not just NEIE?  This
   would improve the line-number accuracy of M4-F6 redaction decisions.
   Recommendation: yes — attach to all exceptions from `_eval_Call`.

4. **M4-F9 and nested loops:** If a loop is nested inside another loop with a
   non-public iterable, both iterables' deps should be merged.  The proposed
   design attaches only the innermost loop's deps.  The outer loop's deps are
   already in `dep_ctx_stack_snapshot` (M4-F8), so the combination is correct
   for NEIE; for other exception types the outer loop's context is carried
   by the existing `_dep_ctx_stack` machinery.

---

### 11.11 Review Checklist

_Signed off post-implementation — all items verified._

- [x] M4-F6: dependency taint check algorithm specified (§11.2.2)
- [x] M4-F6: `[REDACTED]` substitution rule clarified — `None` in model,
      `[REDACTED]` in audit log only (§11.2.3)
- [x] M4-F6: traceback scrubbing scope confirmed — tracebacks never reach P-LLM (§11.2.4)
- [x] M4-F7: exact fields populated in `RedactedError` for NEIE (§11.3.1 table)
- [x] M4-F7: call-site lineno extraction mechanism specified (§11.3.2)
- [x] M4-F7: mapping to P-LLM retry loop (NFR-5) with advisory sentence (§11.3.3)
- [x] M4-F8: variable scopes snapshotted: dep graph + dep-ctx stack (§11.4.2)
- [x] M4-F8: snapshot-restore protocol (§11.4.3)
- [x] M4-F8: scope limited to NEIE; other exception types excluded with rationale (§11.4.4)
- [x] M4-F9: trigger condition (3 conditions) specified (§11.5.2)
- [x] M4-F9: `_exec_For` modification with `_is_non_public` helper (§11.5.3–§11.5.5)
- [x] M4-F17: `RedactionAuditEvent` schema with all fields defined (§11.6.2)
- [x] M4-F17: emission point and audit-log injection mechanism (§11.6.3)
- [x] M4-F17: dependency chain population algorithm with 50-entry cap (§11.6.4)
- [x] §11.7 interaction diagram covers all five features
- [x] Files to be modified enumerated (§11.8)
- [x] PRD §6.2 and §6.3 cross-references specified (§11.9)
- [x] Open questions captured (§11.10)

### 11.12 Verification

_Added post-implementation to record test evidence for each delivered feature._

| Feature | Test File | Key Test(s) |
|---------|-----------|-------------|
| M4-F6 — Dependency-graph-aware redaction | `tests/test_exception_hardening.py`, `tests/test_redaction_completeness.py` | `test_redaction_*_deep_graph`, `test_m4_f6_*` |
| M4-F7 — NEIE handler (type + lineno only) | `tests/test_exception_hardening.py`, `tests/test_execution_loop.py` | `test_neie_lineno_*`, `test_neie_advisory_*` |
| M4-F8 — STRICT annotation preservation on NEIE | `tests/test_exception_hardening.py` | `test_m4_f8_annotation_preservation_*` |
| M4-F9 — Loop-body exception STRICT propagation | `tests/test_exception_hardening.py` | `test_m4_f9_loop_*` |
| M4-F17 — RedactionAuditEvent emission | `tests/test_exception_hardening.py`, `tests/test_execution_loop.py` | `test_redaction_audit_event_*` |

**Status:** All test suites pass.  Exception hardening is production-complete as of 2026-03-17.
