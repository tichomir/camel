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

## 7. Open Questions

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

## 8. Review Checklist

_Signed off post-implementation — all items verified._

- [x] All five feature IDs (M4-F1 through M4-F5) are covered by this document
- [x] Each AST node hook is named explicitly (§3 table)
- [x] Each graph mutation is specified precisely (§2.x algorithm blocks)
- [x] Default-mode flag design specifies config key, default value, and constructor
      signature change (§2.4)
- [x] New state field `_last_qllm_result_cv` is described with type, lifecycle,
      and producer/consumer sites (§4, §5)
- [x] Files requiring modification are enumerated (§6)
- [x] Open questions are captured for the implementation team (§7)
- [x] Document is internally consistent (no contradictions between §2 and §3–§5)

---

## 9. Verification

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
