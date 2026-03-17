# ADR 004 — Dependency Graph Architecture

**Status:** Accepted
**Date:** 2026-03-17
**Authors:** Software Architect Persona

---

## Context

CaMeL must track, for every variable in the interpreter's session, which other
variables (directly or transitively) contributed to its current value.  This
data-flow record enables the security policy engine to determine whether a
value reaching a tool call was influenced by untrusted sources — and in STRICT
mode — whether a control-flow condition or loop iterable tainted downstream
assignments.

The PRD (§6.3) specifies two tracking modes:

- **NORMAL**: dependencies flow only via direct data assignments.
- **STRICT**: additionally, `if`-test and `for`-iterable variable references
  become dependencies on every variable assigned within those blocks.

---

## Decision

### 1. Data structure: adjacency-set graph

We represent the dependency graph as a directed graph whose nodes are variable
names (strings) and whose edges mean "variable A directly depends on variable B
(B was referenced when computing A)."

Internal representation: `dict[str, set[str]]` — a mapping from each variable
to its set of direct dependency variable names.  This is stored in the private
`_InternalGraph` class in `camel/dependency_graph.py`.

**Why not a matrix or explicit node objects?**  Variable names are sparse;
sessions typically contain ≪100 variables.  An adjacency set is the simplest
correct structure.

### 2. Public snapshot type: `DependencyGraph`

A frozen dataclass carrying four fields:

| Field | Type | Meaning |
|---|---|---|
| `variable` | `str` | Root variable queried |
| `direct_deps` | `frozenset[str]` | Immediately referenced variables |
| `all_upstream` | `frozenset[str]` | Transitive closure of upstream variables |
| `edges` | `frozenset[tuple[str, str]]` | All `(var, dep)` edges in the subgraph |

Callers (policy engine, tests) receive this immutable snapshot from
`CaMeLInterpreter.get_dependency_graph(variable_name)`.

### 3. Variable-access tracking during expression evaluation

To record which variables are referenced in an RHS expression, the interpreter
maintains an optional accumulator field:

```python
self._tracking: set[str] | None = None
```

Protocol:

1. Before evaluating an RHS: `self._tracking = set()`
2. In `_eval_name`: if `self._tracking is not None`, `self._tracking.add(name)`
3. After evaluation: capture `frozenset(self._tracking)`; set `self._tracking = None`
4. Call `self._dep_graph.record(target, captured_deps | ctx_deps)`

This is safe under recursion: `_eval_expr` is synchronous, and all transitively
accessed variables during one statement's evaluation accumulate into the single
flat set.  Nested `_exec_statements` calls (inside `for`/`if` bodies) will set
their own fresh `_tracking` context per assignment they process.

**Why not AST pre-scan?**  Pre-scanning the AST for `ast.Name` nodes would miss
dynamic name resolution (e.g. variables accessed through function arguments
where the name depends on runtime state).  Instrumenting `_eval_name` is
already the execution-time choke point and requires no extra AST pass.

### 4. Context dependencies (`ctx_deps`) threading

The interpreter's `_exec_statements(stmts, ctx_caps)` already threads
`ctx_caps: CaMeLValue | None` for STRICT capability taint.  A parallel
`ctx_deps: frozenset[str]` parameter is added (never `None`; defaults to
`frozenset()`):

```python
def _exec_statements(
    self,
    stmts: list[ast.stmt],
    ctx_caps: CaMeLValue | None,
    ctx_deps: frozenset[str],   # NEW
) -> None: ...
```

In NORMAL mode: `ctx_deps` is always `frozenset()` at every level — control
flow contributes no dependency edges.

In STRICT mode:

- **`if` statement**: before recursing into the body, evaluate the `test`
  expression with tracking enabled.  Merge the captured variable set into
  `ctx_deps` and pass the merged set to `_exec_statements` for both the
  `body` and `orelse` blocks.
- **`for` statement**: before recursing into the body, evaluate the `iter`
  expression with tracking enabled.  Merge the captured variable set into
  `ctx_deps` and pass the merged set to `_exec_statements` for the loop body.
- Nesting: `ctx_deps` is merged cumulatively; inner blocks inherit outer
  context deps plus their own block's condition/iterable deps.

### 5. `record` merge semantics

`_InternalGraph.record(variable, deps)` **merges** (union) the new `deps` into
any existing edges for `variable`.  Rationale: a variable may be assigned
multiple times across `exec()` calls; recording the union captures the full
history of what contributed to it.  For the security policy this is
conservative (safe) — the policy sees all possible data sources that ever
reached a variable.

### 6. Public API on `CaMeLInterpreter`

```python
def get_dependency_graph(self, variable: str) -> DependencyGraph:
    """Return the upstream dependency subgraph for *variable*.

    Parameters
    ----------
    variable:
        Name of the variable to query.

    Returns
    -------
    DependencyGraph
        Frozen snapshot.  If *variable* has never been assigned, all fields
        are empty (variable is set, others are frozenset()).

    Notes
    -----
    This method does NOT raise KeyError for unknown variables; it returns
    an empty DependencyGraph.  This matches the contract that the policy
    engine should be able to query any variable without crashing.
    """
    return self._dep_graph.subgraph(variable)
```

---

## Consequences

### Positive

- The dependency graph and capability system remain orthogonal and independently
  testable.  The graph answers "which variables contributed?" while capabilities
  answer "from which tools and with what reader authorization?".
- STRICT mode closes the control-flow side-channel described in the CaMeL paper
  §6.3 without modifying capability propagation rules.
- `_InternalGraph` is a simple dict-based structure with O(V + E) BFS; no
  external dependencies.
- The frozen `DependencyGraph` snapshot is safe to share across threads or
  serialize.

### Negative / Trade-offs

- The merge-union semantics for repeated assignments to the same variable are
  over-approximate: after `x = a; x = b`, x appears to depend on both a and b
  even though the first assignment is dead.  This is intentional and
  conservative (safe for security), but may trigger spurious policy denials in
  edge cases.  Future work (FW-6) can explore per-assignment version tracking.
- `ctx_deps` adds a second parameter to `_exec_statements` and all callers.
  Existing call sites must be updated to pass `ctx_deps=frozenset()` (NORMAL
  mode default).

---

## Integration checklist for the backend developer

- [ ] Add `_dep_graph: _InternalGraph` to `CaMeLInterpreter.__init__`
- [ ] Add `_tracking: set[str] | None = None` to `CaMeLInterpreter.__init__`
- [ ] In `_eval_name`: add `if self._tracking is not None: self._tracking.add(name)`
- [ ] Add `_track_rhs(self, node) -> tuple[CaMeLValue, frozenset[str]]` helper
      that sets `_tracking`, evaluates, captures, resets, and returns both the
      value and the captured dep set
- [ ] Update `_exec_assign` and `_exec_augassign` to use `_track_rhs` and call
      `self._dep_graph.record(target, data_deps | ctx_deps)` for each target
- [ ] Add `ctx_deps: frozenset[str]` parameter to `_exec_statements`,
      `_exec_if`, `_exec_for` (and their callers)
- [ ] In `_exec_if` (STRICT mode): track variables in `test`; merge into
      `ctx_deps` for body/orelse recursion
- [ ] In `_exec_for` (STRICT mode): track variables in `iter`; merge into
      `ctx_deps` for body recursion
- [ ] Expose `get_dependency_graph(self, variable: str) -> DependencyGraph` on
      `CaMeLInterpreter`
- [ ] Update `exec()` call to `_exec_statements(..., ctx_deps=frozenset())`

---

## Files produced

| File | Role |
|---|---|
| `camel/dependency_graph.py` | `_InternalGraph` + `DependencyGraph` (spec + stubs) |
| `docs/adr/004-dependency-graph-architecture.md` | This document |
| `tests/test_dependency_graph.py` | ≥20 programs (to be written by QA) |
