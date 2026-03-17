# CaMeL Developer Guide — Milestone 1

**Version:** 0.1.0
**Date:** 2026-03-17
**Status:** Released

This guide covers the four core components of the CaMeL Milestone 1 foundation:
the supported Python grammar, the `CaMeLValue` capability schema, the dependency
graph query API, and the interface contracts Milestone 2 must satisfy.

---

## Table of Contents

1. [Supported Python Grammar Reference](#1-supported-python-grammar-reference)
2. [CaMeLValue Schema & Capability Propagation Rules](#2-camelvalue-schema--capability-propagation-rules)
3. [DependencyGraph Query API](#3-dependencygraph-query-api)
4. [Milestone 2 Readiness Notes](#4-milestone-2-readiness-notes)
5. [Architecture Decision Records](#5-architecture-decision-records)

---

## 1. Supported Python Grammar Reference

The `CaMeLInterpreter` parses and executes a **restricted subset** of Python
using the standard `ast` module.  Any AST node not listed in the tables below
raises `UnsupportedSyntaxError`.

### 1.1 Supported Statement Nodes

| AST Node | Python construct | Example |
|---|---|---|
| `ast.Assign` | Simple assignment, including tuple unpacking | `x = get_email(42)` |
| `ast.AugAssign` | Augmented assignment | `counter += 1` |
| `ast.If` | Conditional with optional `else` / `elif` | `if flag: x = 1` |
| `ast.For` | `for` loop (`else` clause **not** supported) | `for item in items: process(item)` |
| `ast.Expr` | Bare expression statement (used for side-effecting tool calls) | `send_email(to, body)` |

**`ast.Assign` — tuple unpacking:**

```python
# Tuple unpacking is supported — the interpreter assigns each element
# to its corresponding target variable with the element's capability
# sub-slice (propagated via propagate_subscript).
sender, subject = get_email_fields(email_id)
```

**`ast.AugAssign` — augmented assignment:**

```python
total += item_cost   # total's deps include both total and item_cost
```

**`ast.If` — conditional:**

```python
if is_internal:
    recipient = internal_list
else:
    recipient = public_list
```

**`ast.For` — for loop:**

```python
for email in inbox:
    forward(email, archive_addr)
# Note: for-else is NOT supported and raises UnsupportedSyntaxError.
```

**`ast.Expr` — bare call:**

```python
send_notification(user_id, message)  # return value discarded
```

---

### 1.2 Supported Expression Nodes

| AST Node | Python construct | Example |
|---|---|---|
| `ast.Constant` | Integer, float, string, boolean, `None` literals | `42`, `3.14`, `"hello"`, `True`, `None` |
| `ast.Name` | Variable reference (load mode only) | `x`, `email_body` |
| `ast.BinOp` | Arithmetic, bitwise, and matrix-multiply operators | `a + b`, `x // 2`, `m @ n` |
| `ast.UnaryOp` | Unary operators: `-`, `+`, `not`, `~` | `-x`, `not flag`, `~mask` |
| `ast.BoolOp` | `and` / `or` (all operands evaluated for capabilities) | `a and b`, `x or y` |
| `ast.Compare` | Comparison operators: `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `not in`, `is`, `is not` | `x == 0`, `item in items` |
| `ast.Call` | Function / tool calls | `get_email(42)`, `len(items)` |
| `ast.Attribute` | Attribute access | `email.sender`, `result.status` |
| `ast.Subscript` | Subscript / index access | `items[0]`, `d["key"]` |
| `ast.List` | List literal | `[a, b, c]` |
| `ast.Tuple` | Tuple literal | `(a, b)` |
| `ast.Dict` | Dict literal | `{"key": value}` |
| `ast.JoinedStr` | f-string (including `ast.FormattedValue`) | `f"Hello {name}"` |

**`ast.BinOp` — arithmetic:**

```python
total = price * quantity    # result sources = price.sources | quantity.sources
```

**`ast.BoolOp` — boolean:**

```python
# All operands are evaluated for capability union even if short-circuit
# would skip them at runtime — conservative for security.
allowed = is_trusted and not is_blocked
```

**`ast.Compare` — chained comparison:**

```python
in_range = 0 < value <= 100  # folded left-to-right via propagate_binary_op
```

**`ast.Call` — tool call:**

```python
result = get_last_email()    # tool must return CaMeLValue directly
```

**`ast.Attribute` — attribute access:**

```python
addr = email_obj.sender      # propagated via propagate_subscript(email_obj, key_cv)
```

**`ast.JoinedStr` — f-string:**

```python
greeting = f"Dear {name}, your order {order_id} is ready."
# All parts folded left-to-right; result carries union of all part capabilities.
```

---

### 1.3 Unsupported Constructs — `UnsupportedSyntaxError`

Any AST node **not** listed in §1.1–1.2 raises `UnsupportedSyntaxError` immediately
when the interpreter encounters it.

| AST Node | Python construct | `UnsupportedSyntaxError.node_type` |
|---|---|---|
| `ast.While` | `while` loops | `"While"` |
| `ast.FunctionDef` | Function definition | `"FunctionDef"` |
| `ast.AsyncFunctionDef` | Async function definition | `"AsyncFunctionDef"` |
| `ast.ClassDef` | Class definition | `"ClassDef"` |
| `ast.Lambda` | Lambda expression | `"Lambda"` |
| `ast.ListComp` | List comprehension | `"ListComp"` |
| `ast.SetComp` | Set comprehension | `"SetComp"` |
| `ast.DictComp` | Dict comprehension | `"DictComp"` |
| `ast.GeneratorExp` | Generator expression | `"GeneratorExp"` |
| `ast.Yield` / `ast.YieldFrom` | Generator yield | `"Yield"` / `"YieldFrom"` |
| `ast.Await` | `await` expression | `"Await"` |
| `ast.AsyncFor` | Async for loop | `"AsyncFor"` |
| `ast.AsyncWith` | Async with statement | `"AsyncWith"` |
| `ast.With` | `with` / `as` statement | `"With"` |
| `ast.Delete` | `del` statement | `"Delete"` |
| `ast.Global` / `ast.Nonlocal` | Scope declarations | `"Global"` / `"Nonlocal"` |
| `ast.Import` / `ast.ImportFrom` | Import statements | `"Import"` / `"ImportFrom"` |
| `ast.Raise` | `raise` statement | `"Raise"` |
| `ast.Try` | `try` / `except` block | `"Try"` |
| `ast.Assert` | `assert` statement | `"Assert"` |
| `ast.Set` | Set literal `{a, b}` | `"Set"` |
| `ast.IfExp` | Ternary expression `x if c else y` | `"IfExp"` |
| `ast.Starred` | Starred expression `*args` (outside assignment) | `"Starred"` |
| `ast.Name` (store/del context) | Assignment to name (only load is supported as an expression) | `"Name"` (del/store) |

**`UnsupportedSyntaxError` fields:**

```python
@dataclass
class UnsupportedSyntaxError(Exception):
    node_type: str   # type(node).__name__ of the offending AST node
    lineno: int      # 1-based source line; 0 when unavailable
    message: str     # human-readable description

# Example:
try:
    interp.exec("while True: pass")
except UnsupportedSyntaxError as exc:
    assert exc.node_type == "While"
    assert exc.lineno == 1
    print(exc.message)   # "while loops are not supported ..."
```

---

### 1.4 Builtin Callables

The following Python builtins are available inside the interpreter sandbox without
tool registration.  They receive **raw** (unwrapped) argument values and their
return is wrapped with the union of all argument capabilities.

```
len  str  int  float  bool  list  dict  tuple  set  range
sorted  reversed  enumerate  zip  map  filter
min  max  sum  abs  round  isinstance  type  repr
```

Additional builtins can be injected at construction time:

```python
interp = CaMeLInterpreter(builtins={"json_loads": json.loads})
```

---

## 2. CaMeLValue Schema & Capability Propagation Rules

### 2.1 Field Definitions

`CaMeLValue` is a **frozen dataclass** (`frozen=True`) in `camel.value`.  Every
runtime value produced by the interpreter is stored as a `CaMeLValue`.

| Field | Type | Description |
|---|---|---|
| `value` | `Any` | The underlying Python value: `str`, `int`, `list`, `dict`, Pydantic model instance, etc. |
| `sources` | `frozenset[str]` | Set of origin labels for this value. Typically tool names (`"get_last_email"`), `"User literal"` for constants the user typed, or `"CaMeL"` for values synthesised by the interpreter itself.  For derived values, the union of all input `sources` sets. |
| `inner_source` | `str \| None` | Optional sub-field label within the originating tool response (e.g. `"sender"` for the sender field of an email).  Always `None` for derived values. |
| `readers` | `frozenset[str] \| Public` | Principals authorised to receive this value.  `frozenset[str]` is a finite allow-list; `Public` (the singleton) means unrestricted.  An empty `frozenset()` means no reader is authorised — the value must not be forwarded to any external principal. |

**`Public` sentinel:**

```python
from camel.value import Public, _PublicType

# Public is a module-level singleton:
assert Public is _PublicType()           # singleton guarantee
assert isinstance(Public, _PublicType)  # preferred isinstance check
```

`Public` is the **top element** in the readers lattice:

- `Public ∪ {any set}` → `Public`  (absorbing under union)
- `Public ≠ frozenset()`            (empty frozenset = *no* readers)

**Constructing a `CaMeLValue`:**

```python
from camel.value import CaMeLValue, Public, wrap

# Full constructor — for tool outputs with known provenance:
email_subject = CaMeLValue(
    value="Project update",
    sources=frozenset({"get_last_email"}),
    inner_source="subject",
    readers=frozenset({"alice@example.com"}),
)

# Convenience wrapper — for trusted user literals:
user_val = wrap("alice@example.com", sources=frozenset({"User literal"}))
# readers defaults to Public; inner_source defaults to None

# Convenience wrapper — for tool outputs with restricted readers:
tool_val = wrap(
    "secret-token",
    sources=frozenset({"get_secret"}),
    readers=frozenset({"alice@example.com"}),
)
```

**Accessing the raw value:**

```python
# Use .raw property or raw_value() function — never .value directly.
from camel.value import raw_value

print(email_subject.raw)          # "Project update"
print(raw_value(email_subject))   # "Project update" (functional form)
```

---

### 2.2 Capability Propagation Rules

All propagation functions return a **new** `CaMeLValue`; `inner_source` is always
`None` in derived values.

#### Union Semantics

The core rule for all derived values:

```
result.sources = union of all input .sources
result.readers = union of all input .readers  (Public absorbs)
result.inner_source = None
```

#### Propagation Rules Table

| Operation | Function | `sources` rule | `readers` rule | `inner_source` |
|---|---|---|---|---|
| Simple assignment `x = expr` | `propagate_assignment(source, new_value)` | copied from `source` | copied from `source` | `None` |
| Binary op `a OP b` | `propagate_binary_op(left, right, result)` | `left.sources ∪ right.sources` | `left.readers ∪ right.readers` | `None` |
| Unary op `-x`, `not x` | `propagate_assignment(operand, result)` | copied from operand | copied from operand | `None` |
| BoolOp `a and b` / `a or b` | fold left-to-right via `propagate_binary_op` | union of all operands | union of all operands | `None` |
| Compare `a == b`, `a < b` | fold left-to-right via `propagate_binary_op` | union of all operands | union of all operands | `None` |
| List literal `[e0, …, eN]` | `propagate_list_construction(elements, result)` | union of all element `sources` | union of all element `readers` | `None` |
| Tuple literal `(e0, …, eN)` | `propagate_list_construction(elements, result)` | union of all element `sources` | union of all element `readers` | `None` |
| Dict literal `{k0: v0, …}` | `propagate_dict_construction(keys, values, result)` | union of all key + value `sources` | union of all key + value `readers` | `None` |
| Subscript `container[key]` | `propagate_subscript(container, key, result)` | `container.sources ∪ key.sources` | `container.readers ∪ key.readers` | `None` |
| Attribute access `obj.field` | `propagate_subscript(obj, key_cv, result)` | `obj.sources ∪ key_cv.sources` | `obj.readers ∪ key_cv.readers` | `None` |
| f-string `f"…{x}…{y}…"` | fold parts left-to-right via `propagate_binary_op` | union of all parts | union of all parts | `None` |
| Tool call (returns `CaMeLValue`) | tool function's own return | set by tool function | set by tool function | set by tool function |
| Builtin call (e.g. `len(x)`) | wrap result with union of all arg capabilities | union of all arg `sources` | union of all arg `readers` | `None` |
| Constant literal `42`, `"str"` | `wrap(value, sources=frozenset({"User literal"}), readers=Public)` | `{"User literal"}` | `Public` | `None` |

**Dict propagation note:** both keys and values contribute to the output capabilities.
This is intentional — an untrusted key can encode information via the *presence or
absence* of entries, so the key's provenance must be tracked.

#### Examples

```python
from camel.value import CaMeLValue, Public, propagate_binary_op, propagate_subscript

a = CaMeLValue(value="hello", sources=frozenset({"tool_a"}),
               inner_source=None, readers=frozenset({"alice@example.com"}))
b = CaMeLValue(value=" world", sources=frozenset({"User literal"}),
               inner_source=None, readers=Public)

# Binary op — sources union, readers Public (absorbing)
c = propagate_binary_op(a, b, a.raw + b.raw)
assert c.value == "hello world"
assert c.sources == frozenset({"tool_a", "User literal"})
assert c.readers is Public          # Public absorbed the frozenset

# Subscript — key provenance contributes
key = CaMeLValue(value=0, sources=frozenset({"tool_b"}),
                 inner_source=None, readers=frozenset({"bob@example.com"}))
container = CaMeLValue(value=["x", "y"], sources=frozenset({"tool_c"}),
                       inner_source=None, readers=frozenset({"alice@example.com"}))
item = propagate_subscript(container, key, container.raw[key.raw])
assert item.sources == frozenset({"tool_b", "tool_c"})
assert item.readers == frozenset({"alice@example.com", "bob@example.com"})
```

---

## 3. DependencyGraph Query API

### 3.1 Overview

The dependency graph tracks which **variables** influenced the value of every
other variable throughout the session.  It is orthogonal to the capability system:

- **Capabilities** answer: *where did this data come from?* (tool identity, authorization)
- **Dependency graph** answers: *which variables influenced this variable?* (execution provenance)

The policy engine uses both dimensions: capabilities for authorization checks,
and the dependency graph to detect control-flow-based taint in STRICT mode.

### 3.2 `CaMeLInterpreter.get_dependency_graph()`

```python
def get_dependency_graph(self, variable: str) -> DependencyGraph: ...
```

Query the dependency graph for a named variable within the current session.

| Parameter | Type | Description |
|---|---|---|
| `variable` | `str` | Name of the variable to look up. |

**Returns:** `DependencyGraph` — a frozen snapshot of the upstream dependency
subgraph.  If `variable` has never been assigned in the session, all fields are
empty.

**Module-level convenience function** (equivalent):

```python
from camel import get_dependency_graph

dg = get_dependency_graph(interp, "result")
```

### 3.3 `DependencyGraph` Type

`DependencyGraph` is a **frozen dataclass** (`frozen=True`) in `camel.dependency_graph`.

| Field | Type | Description |
|---|---|---|
| `variable` | `str` | The root variable name this graph was queried for. |
| `direct_deps` | `frozenset[str]` | Variables that directly appear in the RHS of the most-recent assignment to `variable` (plus control-flow contributions in STRICT mode). |
| `all_upstream` | `frozenset[str]` | Transitive closure of all variable names that contributed to `variable`'s current value. Does **not** include `variable` itself unless there is a cycle (e.g. `x += y`). |
| `edges` | `frozenset[tuple[str, str]]` | All directed edges `(var, dep)` in the subgraph, meaning "var directly depends on dep". Only includes nodes reachable from `variable`. |

### 3.4 NORMAL vs STRICT Mode

#### NORMAL mode (default)

Dependencies are recorded only via **data assignment**.  Control-flow constructs
(`if` / `for`) contribute no dependency edges.

```python
from camel import CaMeLInterpreter, ExecutionMode, get_dependency_graph
from camel.value import wrap

def get_flag():
    return wrap(True, sources=frozenset({"tool_flag"}))

interp = CaMeLInterpreter(
    tools={"get_flag": get_flag},
    mode=ExecutionMode.NORMAL,
)
interp.exec("""
flag = get_flag()
x = 1
if flag:
    x = 2
""")

dg = get_dependency_graph(interp, "x")
assert "flag" not in dg.all_upstream   # NORMAL: if-test contributes no dep edge
assert dg.direct_deps == frozenset()   # x = 2 assigns a constant, no var refs
```

#### STRICT mode

In addition to data-assignment edges, the variables referenced in an `if` test
or `for` iterable are added as dependencies on **every variable assigned within
that block** (including nested blocks).  This closes control-flow timing
side-channel vectors.

```python
interp_strict = CaMeLInterpreter(
    tools={"get_flag": get_flag},
    mode=ExecutionMode.STRICT,
)
interp_strict.exec("""
flag = get_flag()
x = 1
if flag:
    x = 2
""")

dg = get_dependency_graph(interp_strict, "x")
assert "flag" in dg.all_upstream       # STRICT: if-test adds flag as dep
assert "flag" in dg.direct_deps        # direct because it's from the if-test
```

#### For-loop example (STRICT):

```python
interp_strict.exec("""
items = get_items()
total = 0
for item in items:
    total += item
""")

dg = get_dependency_graph(interp_strict, "total")
# STRICT mode: 'items' (the iterable) is a dep on everything assigned in the loop
assert "items" in dg.all_upstream
```

### 3.5 Upstream Traversal

`DependencyGraph.all_upstream` is the **transitive closure** computed via
breadth-first search over the recorded direct-dep edges.

```python
interp = CaMeLInterpreter()
interp.exec("""
a = 1
b = a
c = b + 1
""")

dg = get_dependency_graph(interp, "c")
assert dg.variable == "c"
assert dg.direct_deps == frozenset({"b"})           # c = b + 1
assert dg.all_upstream == frozenset({"a", "b"})     # transitive
assert ("c", "b") in dg.edges
assert ("b", "a") in dg.edges
```

### 3.6 Edge Cases

| Scenario | Behaviour |
|---|---|
| Variable never assigned | All fields are empty `frozenset()` |
| Augmented assignment `x += y` | `x` depends on both `x` (self-loop) and `y`; cycle-safe BFS |
| Constant RHS `x = 42` | `x` is recorded with empty deps (no variable references) |
| Tool call RHS `x = tool()` | `x` is recorded with empty deps (tool call, no variable references in args) |
| Tool call with args `x = tool(a, b)` | `x` depends on `{a, b}` (the argument variable names) |

---

## 4. Milestone 2 Readiness Notes

This section defines the interface contracts that Milestone 2 components must
satisfy to integrate with the Milestone 1 foundation.

### 4.1 Tool Executor Contract

Each tool registered with `CaMeLInterpreter` must conform to the following
callable signature:

```python
from typing import Callable, Mapping
from camel.value import CaMeLValue

ToolCallable = Callable[..., CaMeLValue]
```

**Precise signature (keyword-arguments pattern):**

```python
def my_tool(**kwargs: CaMeLValue) -> CaMeLValue:
    ...
```

Or positional for simple tools:

```python
def get_email(email_id: CaMeLValue) -> CaMeLValue:
    raw_id = email_id.raw
    # ... call external API ...
    return CaMeLValue(
        value=result,
        sources=frozenset({"get_email"}),
        inner_source=None,
        readers=frozenset({"alice@example.com"}),
    )
```

**Constraints the tool executor must satisfy:**

| Constraint | Description |
|---|---|
| Return type | Must return `CaMeLValue` directly. The interpreter raises `TypeError` if the return value is not a `CaMeLValue`. |
| Capability tagging | The returned `CaMeLValue` must have `sources` set to the tool's registered name (or a descriptive label). |
| `readers` assignment | The tool must set `readers` according to the data's intended audience. Use `Public` for unrestricted data; use a `frozenset[str]` of authorized principals for restricted data. |
| `inner_source` | Set to the sub-field name when the value represents a specific field of a structured response (e.g. `"sender"`, `"subject"`). `None` otherwise. |
| No side effects before policy check | The policy engine is invoked **before** the tool callable is called. Tools must not perform side effects in `__init__` or before the body executes. |
| Raw argument extraction | Tool implementations receive `CaMeLValue` arguments. Extract raw values using `arg.raw` or `raw_value(arg)` before passing to external APIs. |

**Policy engine interface** (injected at `CaMeLInterpreter` construction time):

```python
from typing import Protocol, Mapping
from camel.value import CaMeLValue

class PolicyResult:
    """Base type for policy decisions."""

class Allowed(PolicyResult):
    """The tool call is permitted."""

class Denied(PolicyResult):
    def __init__(self, reason: str) -> None: ...
    reason: str

class PolicyEngine(Protocol):
    def check(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> PolicyResult: ...
```

If `policy_engine=None` (default), all tool calls are permitted.  A `Denied`
result causes the interpreter to raise `PolicyViolationError(tool_name, reason)`.

**Tool registration example:**

```python
from camel import CaMeLInterpreter
from camel.value import CaMeLValue, wrap, Public

def get_inbox_count() -> CaMeLValue:
    """Returns the number of unread emails — public information."""
    return wrap(42, sources=frozenset({"get_inbox_count"}), readers=Public)

def send_email(to: CaMeLValue, body: CaMeLValue) -> CaMeLValue:
    """Sends an email. Requires 'to' address to pass the policy check."""
    # Called only after policy engine approves the call.
    result = _actual_send_api(to.raw, body.raw)
    return wrap(True, sources=frozenset({"send_email"}), readers=Public)

interp = CaMeLInterpreter(
    tools={
        "get_inbox_count": get_inbox_count,
        "send_email": send_email,
    },
    policy_engine=MyPolicyEngine(),
)
```

---

### 4.2 P-LLM Code-Plan Input Contract

The P-LLM generates a **pseudo-Python code plan** that `CaMeLInterpreter.exec()`
executes.

#### Input to `exec()`

```python
def exec(self, code: str) -> None: ...
```

`code` is a string containing one or more statements from the supported grammar
(§1.1–1.2).  The interpreter parses it with `ast.parse(code, mode="exec")`.

#### P-LLM Generation Constraints

The P-LLM must generate code that satisfies all of the following constraints:

| Constraint | Requirement |
|---|---|
| Grammar subset | Only AST nodes listed in §1.1–1.2 are permitted. Any other node causes `UnsupportedSyntaxError`. |
| No imports | `import` and `from ... import` are not supported. |
| No function definitions | `def`, `lambda`, `class` are not supported. |
| No exception handling | `try` / `except` / `raise` are not supported. |
| No `while` loops | Only `for` loops over known iterables. |
| No `for` else | The `else` clause of a `for` loop is not supported. |
| Tool names match registration | Every function call must use a name registered in `tools` or `builtins`. Unknown names raise `NameError`. |
| Variable names | Must be valid Python identifiers. |
| No walrus operator | `:=` assignment expressions are not supported. |
| No starred unpacking in calls | `f(*args, **kwargs)` is not supported. |

#### Error Reporting Protocol

When `exec()` raises an exception, the P-LLM may be retried (up to 10 times per
plan per PRD §6.1).  The error type determines what information is safe to
surface back to the P-LLM:

| Exception type | Safe to show P-LLM? | Information to surface |
|---|---|---|
| `UnsupportedSyntaxError` | Yes | `node_type`, `lineno`, `message` (no runtime data) |
| `SyntaxError` (from `ast.parse`) | Yes | `lineno`, `msg` from the parse error |
| `NameError` (unknown variable/tool) | Yes | variable name only |
| `TypeError` (tool return not `CaMeLValue`) | Yes | tool name, expected type |
| `PolicyViolationError` | **Partial** | Tool name only; **do not** surface `reason` (may contain capability/data details) |
| Any exception from tool execution | **No** | Surface only the exception *type name* (not message) — tool output is untrusted |

**Rationale:** Error messages from tool execution may contain adversarially
crafted content.  Surfacing them to the P-LLM would re-introduce the prompt
injection vector the interpreter is designed to close.

#### Multi-Step Session Semantics

Multiple `exec()` calls on the same `CaMeLInterpreter` instance share state:

```python
interp = CaMeLInterpreter(tools={"get_email": get_email})

# Step 1 — P-LLM generates:
interp.exec("email = get_email(1)")

# Step 2 — P-LLM generates (sees variable names from step 1 via schema contract):
interp.exec("subject = email.subject")

# The variable 'email' set in step 1 is visible in step 2.
```

The P-LLM does **not** see variable *values* between steps — it only knows the
variable *names* it assigned in previous steps.  The interpreter communicates
back: which variables are now defined (names only), and which exception type
occurred (if any).

#### Accessing Interpreter State After Execution

```python
# Read a variable's CaMeLValue:
result = interp.get("subject")      # raises KeyError if not defined

# Inspect dependency graph:
dg = interp.get_dependency_graph("subject")

# Inspect all defined variables (snapshot dict):
snapshot = interp.store   # dict[str, CaMeLValue] — shallow copy, safe to mutate

# Seed a variable from outside the interpreter:
interp.seed("pre_loaded", wrap("value", sources=frozenset({"external"})))
```

---

## 5. Architecture Decision Records

The `docs/adr/` directory contains Architecture Decision Records (ADRs) that
capture the key design choices made during Milestone 1.  Each ADR follows the
standard format: Title, Status, Context, Decision, Consequences, Alternatives
Considered, and References.

| ADR | Title | Summary |
|-----|-------|---------|
| [ADR-001](adr/001-q-llm-isolation-contract.md) | Q-LLM Isolation Contract and Schema Conventions | How the Q-LLM is structurally prevented from calling tools or emitting free-form text; `QResponse` base schema; `have_enough_information` field semantics. |
| [ADR-002](adr/002-camelvalue-capability-system.md) | CaMeLValue Dataclass and Capability Propagation System | Design of the `CaMeLValue` container, the `Public` singleton, `Readers` type alias, and all propagation rules (`propagate_assignment`, `propagate_binary_op`, etc.). |
| [ADR-003](adr/003-ast-interpreter-architecture.md) | AST Interpreter Architecture and Supported Grammar Spec | Why Python's `ast` module was chosen over alternatives (custom bytecode VM, Lua, JS sandbox, S-expression DSL); the full supported grammar; `ExecutionMode` (NORMAL/STRICT); session-state lifecycle. |
| [ADR-004](adr/004-dependency-graph-architecture.md) | Dependency Graph Architecture and NORMAL/STRICT Tracking Modes | Variable-level dependency tracking; the control-flow side-channel threat (PRD §7); why STRICT mode closes it; `DependencyGraph` frozen snapshot type; adjacency-set internal representation. |

---

## Appendix A — Quick Reference: Public API Surface (v0.1.0)

```python
# camel package top-level exports
from camel import (
    # Value & capability system
    CaMeLValue,     # frozen dataclass: value, sources, inner_source, readers
    Public,         # singleton sentinel: open-readers
    Readers,        # type alias: frozenset[str] | _PublicType
    wrap,           # convenience constructor
    raw_value,      # strip capability wrapper

    # Propagation functions
    propagate_assignment,
    propagate_binary_op,
    propagate_list_construction,
    propagate_dict_construction,
    propagate_subscript,

    # Interpreter
    CaMeLInterpreter,       # exec(), get(), seed(), store, get_dependency_graph(), set_mode()
    ExecutionMode,          # NORMAL | STRICT
    UnsupportedSyntaxError, # node_type, lineno, message
    PolicyViolationError,   # tool_name, reason

    # Dependency graph
    DependencyGraph,        # variable, direct_deps, all_upstream, edges
    get_dependency_graph,   # module-level helper
)
```

---

## Appendix B — Glossary

| Term | Definition |
|---|---|
| **Capability** | The `(sources, inner_source, readers)` metadata attached to a `CaMeLValue`. |
| **P-LLM** | Privileged LLM — generates the code plan; never sees tool return values. |
| **Q-LLM** | Quarantined LLM — extracts structured data from untrusted content; has no tool access. |
| **NORMAL mode** | Dependency tracking via data assignment only. |
| **STRICT mode** | Dependency tracking including control-flow taint (if-test, for-iterable). |
| **Policy engine** | Component that evaluates `(tool_name, kwargs) → Allowed | Denied` before each tool call. |
| **`Public`** | Singleton sentinel meaning any reader is permitted (top element in the readers lattice). |
| **Session** | A single `CaMeLInterpreter` instance lifetime; variable store persists across `exec()` calls. |
| **Trusted** | Derived from the user query or P-LLM-generated literals (never from tool outputs). |
| **Untrusted** | Any value originating from a tool call, Q-LLM output, or external data source. |
