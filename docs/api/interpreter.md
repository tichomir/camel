# API Reference — CaMeL Interpreter (`camel.interpreter`)

The `camel.interpreter` module implements the **CaMeL AST interpreter** — a
custom Python interpreter that executes P-LLM-generated pseudo-Python plans
over a restricted grammar subset.  Every runtime value is stored as a
`CaMeLValue` carrying full capability metadata (sources, readers) that the
security policy engine inspects before each tool call.

---

## Supported Grammar

### Statements

| Construct | Notes |
|---|---|
| `ast.Assign` | Simple assignment, including tuple unpacking. |
| `ast.AugAssign` | Augmented assignment (`x += expr`). |
| `ast.If` | Conditional with optional `else`. |
| `ast.For` | For loop (`else` clause not supported). |
| `ast.Expr` | Bare expression (function call for side effects). |

All other statement types raise `UnsupportedSyntaxError`.

### Expressions

| Construct | Notes |
|---|---|
| `ast.Constant` | Integer, float, string, boolean, `None` literals. |
| `ast.Name` | Variable reference (load only). |
| `ast.BinOp` | Arithmetic, bitwise, and matrix-multiply operators. |
| `ast.UnaryOp` | `-`, `+`, `not`, `~`. |
| `ast.BoolOp` | `and` / `or` (all operands evaluated for capabilities). |
| `ast.Compare` | `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `not in`, `is`, `is not`. |
| `ast.Call` | Function / tool calls. |
| `ast.Attribute` | Attribute access (`obj.field`). |
| `ast.Subscript` | Subscript access (`container[key]`). |
| `ast.List` | List literal. |
| `ast.Tuple` | Tuple literal. |
| `ast.Dict` | Dict literal. |
| `ast.JoinedStr` | f-string (`f"..."`), including `ast.FormattedValue`. |

---

## Execution Modes

```python
class ExecutionMode(str, Enum):
    NORMAL = "normal"
    STRICT = "strict"
```

| Mode | Behaviour |
|---|---|
| `NORMAL` (default) | Capabilities propagate only via data assignments and operations. |
| `STRICT` | Additionally, entering an `if` or `for` block merges the test/iterable's capabilities into every assignment within the block, closing timing side-channel vectors. |

---

## `CaMeLInterpreter`

```python
class CaMeLInterpreter:
    def __init__(
        self,
        tools: Mapping[str, Callable[..., CaMeLValue]] | None = None,
        builtins: Mapping[str, Callable[..., Any]] | None = None,
        mode: ExecutionMode = ExecutionMode.NORMAL,
        policy_engine: Any | None = None,
    ) -> None:
```

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `tools` | `Mapping[str, Callable[..., CaMeLValue]] \| None` | `None` | Tool callables keyed by name. Each tool must return a `CaMeLValue`. |
| `builtins` | `Mapping[str, Callable[..., Any]] \| None` | `None` | Additional builtins merged with defaults (`len`, `range`, `str`, `int`, `float`, `bool`, `list`, `dict`, `abs`, `min`, `max`, `sum`, `sorted`). Values in `builtins` override defaults. |
| `mode` | `ExecutionMode` | `ExecutionMode.NORMAL` | Initial execution mode for this session. |
| `policy_engine` | `Any \| None` | `None` | Optional security policy engine; evaluated before each tool call. |

**Session state:** The variable store (`_store`) is initialised empty and
persists across sequential `exec()` calls on the same instance.

---

### `CaMeLInterpreter.exec`

```python
def exec(self, code: str) -> None:
```

Parse and execute a restricted-Python code string.

The code is parsed with `ast.parse(code, mode="exec")` and each top-level
statement is executed in order.  The variable store is updated in place; it
is **not** cleared before execution.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `code` | `str` | A string of pseudo-Python source code in the supported grammar subset. |

**Raises**

| Exception | When |
|---|---|
| `UnsupportedSyntaxError` | The code contains any AST node outside the supported grammar. |
| `PolicyViolationError` | A policy engine is set and denies a tool call. |
| `NameError` | A `Name` node references a variable not in the store, tools, or builtins. |
| `SyntaxError` | The code is not valid Python (propagated from `ast.parse`). |
| `TypeError` | A registered tool returns a value that is not a `CaMeLValue`. |

**Usage example**

```python
from camel.interpreter import CaMeLInterpreter
from camel.value import wrap

def get_subject(email_id: int):
    return wrap("Re: Q4 results", sources=frozenset({"get_subject"}))

interp = CaMeLInterpreter(tools={"get_subject": get_subject})
interp.exec('subject = get_subject(42)')
print(interp.get("subject").raw)  # "Re: Q4 results"

# Multi-step session — state persists across exec() calls
interp.exec('a = 1')
interp.exec('b = a + 2')
print(interp.get("b").raw)  # 3
```

---

### `CaMeLInterpreter.store` (property)

```python
@property
def store(self) -> dict[str, CaMeLValue]:
```

Return a shallow copy of the current variable store.

The returned dict is a snapshot; mutations do not affect the interpreter's
internal state.

**Returns** `dict[str, CaMeLValue]` — all variables currently defined in the session.

---

### `CaMeLInterpreter.get`

```python
def get(self, name: str) -> CaMeLValue:
```

Return the `CaMeLValue` bound to `name`.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `name` | `str` | Variable name to look up. |

**Returns** `CaMeLValue` — the value currently stored under `name`.

**Raises**

| Exception | When |
|---|---|
| `KeyError` | `name` is not defined in the store. |

---

### `CaMeLInterpreter.seed`

```python
def seed(self, name: str, value: CaMeLValue) -> None:
```

Inject a `CaMeLValue` into the store.

Use this to pre-populate variables from outside the interpreter (e.g. to pass
a tool result from a prior orchestration step into a new code block without
re-executing).

**Parameters**

| Name | Type | Description |
|---|---|---|
| `name` | `str` | Variable name to bind. |
| `value` | `CaMeLValue` | The `CaMeLValue` to store. |

**Raises**

| Exception | When |
|---|---|
| `TypeError` | `value` is not a `CaMeLValue` instance. |

---

### `CaMeLInterpreter.set_mode`

```python
def set_mode(self, mode: ExecutionMode | str) -> None:
```

Set the execution mode for this session.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `mode` | `ExecutionMode \| str` | Either an `ExecutionMode` enum member or its string value (`"normal"` or `"strict"`). Change takes effect immediately for subsequent `exec()` calls. |

**Raises**

| Exception | When |
|---|---|
| `ValueError` | `mode` is a string that does not match any `ExecutionMode` member. |

---

### `CaMeLInterpreter.get_dependency_graph`

```python
def get_dependency_graph(self, variable: str) -> DependencyGraph:
```

Return the dependency graph snapshot for `variable`.

Returns an immutable `DependencyGraph` with all upstream variable dependencies
computed transitively.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `variable` | `str` | The variable name to query. |

**Returns** `DependencyGraph` — frozen snapshot.  All fields are empty if `variable`
has never been assigned in this session.

---

## Exceptions

### `UnsupportedSyntaxError`

```python
class UnsupportedSyntaxError(Exception):
    node_type: str
    lineno: int | None
```

Raised when the interpreter encounters an AST node outside the supported grammar.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `node_type` | `str` | The name of the unsupported AST node class (e.g. `"While"`, `"FunctionDef"`). |
| `lineno` | `int \| None` | 1-based source line number, or `None` if unavailable. |

### `PolicyViolationError`

```python
class PolicyViolationError(Exception):
    tool_name: str
    reason: str
```

Raised when the policy engine denies a tool call.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `tool_name` | `str` | The name of the tool whose call was denied. |
| `reason` | `str` | Human-readable denial reason from the policy engine. |

---

## CaMeLValue Wrapping Strategy

| Expression type | Propagation rule |
|---|---|
| Constant | `sources=frozenset({"User literal"})`, `readers=Public`. |
| Variable load | Returns the stored `CaMeLValue` unchanged. |
| Binary / augmented ops | `propagate_binary_op`. |
| Unary ops | `propagate_assignment` (preserve operand caps, new raw value). |
| BoolOp / Compare | Fold operands left-to-right with `propagate_binary_op`. |
| List / Tuple | `propagate_list_construction`. |
| Dict | `propagate_dict_construction` (both keys and values contribute). |
| Subscript / Attribute | `propagate_subscript`. |
| f-string | Fold all parts left-to-right with `propagate_binary_op`. |
| Tool calls | Tool returns a `CaMeLValue` directly. |
| Builtin calls | Return value wraps union of all argument capabilities. |

---

## STRICT Mode Side-Channel Mitigation

In `STRICT` mode, whenever an `if` or `for` block is entered, the capabilities
of the test/iterable expression are merged into every assignment within the
block.  This prevents an adversary from leaking a private value by controlling
whether or how many times a branch executes.

```python
interp = CaMeLInterpreter(tools=tools, mode=ExecutionMode.STRICT)
interp.exec("""
for item in untrusted_list:
    result = process(item)
""")
# In STRICT mode, `result` inherits capabilities from `untrusted_list`.
```

---

*See also: [LLM Backend API](llm_backend.md) · [P-LLM Wrapper API](p_llm.md) · [Execution Loop API](execution_loop.md)*
