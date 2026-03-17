# API Reference — Execution Loop (`camel.execution_loop`)

The `camel.execution_loop` module wires the P-LLM wrapper, CaMeL interpreter,
Q-LLM wrapper, and tool dispatch into a **complete execution loop** with retry,
exception redaction, execution trace recording, and `print()` output routing.

---

## Architecture

```
User query
    │
    ▼
CaMeLOrchestrator.run()
    │
    ├─► PLLMWrapper.generate_plan()   — generates pseudo-Python execution plan
    │
    ├─► CaMeLInterpreter.exec()       — executes plan statement-by-statement
    │       │
    │       ├─► tool calls            → TraceRecorder appends TraceRecord
    │       └─► print() calls         → DisplayChannel.write()
    │
    └─► On exception:
            ExceptionRedactor.classify()   — produces RedactedError
            RetryPromptBuilder.build()     — constructs retry prompt (M2-F14)
            PLLMWrapper.generate_plan()    — regenerates remaining code
            (up to max_loop_retries times)
```

**Routing contract:**

- `print()` calls → `DisplayChannel` (M2-F10).
- Successful tool calls → `TraceRecord` appended to `ExecutionTrace` (M2-F12).
- These two output streams are **strictly separated**.

---

## `CaMeLOrchestrator`

```python
class CaMeLOrchestrator:
    def __init__(
        self,
        p_llm: PLLMWrapper,
        interpreter: CaMeLInterpreter,
        tool_signatures: list[ToolSignature],
        display_channel: DisplayChannel | None = None,
        max_loop_retries: int = 10,
    ) -> None:
```

Wires P-LLM, `CaMeLInterpreter`, Q-LLM, and tool dispatch into a complete
CaMeL execution loop.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `p_llm` | `PLLMWrapper` | — | Configured `PLLMWrapper` for plan generation and re-generation. |
| `interpreter` | `CaMeLInterpreter` | — | Pre-loaded interpreter with tool callables and optional policy engine. |
| `tool_signatures` | `list[ToolSignature]` | — | Tool signatures injected into the P-LLM system prompt. |
| `display_channel` | `DisplayChannel \| None` | `StdoutDisplayChannel()` | Sink for `print()` output from execution plans (M2-F10). |
| `max_loop_retries` | `int` | `10` | Maximum outer-loop retry count (M2-F8). |

---

### `CaMeLOrchestrator.run`

```python
async def run(
    self,
    user_query: str,
    user_context: UserContext | None = None,
) -> ExecutionResult:
```

Execute `user_query` through the full CaMeL pipeline.

State machine:

1. Calls P-LLM to generate an initial `CodePlan`.
2. Executes the plan statement-by-statement via the interpreter.
3. On exception: redacts the error, captures accepted state, builds a retry
   prompt, increments the loop counter, and re-generates a plan for the
   remaining code.
4. On success: returns an `ExecutionResult` with the full trace, print
   outputs, and final store.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `user_query` | `str` | Trusted user request string. Must not contain `CaMeLValue` instances. |
| `user_context` | `UserContext \| None` | Optional trusted key-value metadata forwarded to the P-LLM system prompt. |

**Returns** `ExecutionResult` — execution trace, print outputs, final variable store, and attempt count.

**Raises**

| Exception | When |
|---|---|
| `MaxRetriesExceededError` | `max_loop_retries` outer-loop retries exhausted without successful plan completion (M2-F8). |
| `PLLMRetryExhaustedError` | The P-LLM inner retry loop exhausts on plan generation or re-generation. |
| `PLLMIsolationError` | A `CaMeLValue` is detected in `user_query` or `user_context`. |

---

### `CaMeLOrchestrator.get_trace`

```python
def get_trace(self) -> ExecutionTrace:
```

Return the current execution trace (M2-F12).

**Returns** `ExecutionTrace` — ordered list of `TraceRecord` instances appended after
each successful tool call in the most recent `run()` call.

---

## Data Models

### `ExecutionResult`

```python
@dataclass(frozen=True)
class ExecutionResult:
    trace: ExecutionTrace
    print_outputs: list[CaMeLValue]
    final_store: dict[str, CaMeLValue]
    loop_attempts: int
```

The outcome of a successful `CaMeLOrchestrator.run()` call.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `trace` | `ExecutionTrace` | Ordered `ExecutionTrace` — one `TraceRecord` per successful tool call (M2-F12). |
| `print_outputs` | `list[CaMeLValue]` | Ordered list of `CaMeLValue` instances passed to `print()` during execution (M2-F10). |
| `final_store` | `dict[str, CaMeLValue]` | Shallow snapshot of the interpreter variable store after the plan completes. |
| `loop_attempts` | `int` | Number of outer-loop retries consumed. `0` means the first attempt succeeded. |

---

### `TraceRecord`

```python
@dataclass(frozen=True)
class TraceRecord:
    tool_name: str
    args: dict[str, Any]
    memory_snapshot: dict[str, CaMeLValue]
```

A single successful tool-call event in the execution trace (M2-F12).

**Attributes**

| Name | Type | Description |
|---|---|---|
| `tool_name` | `str` | The registered tool name (e.g. `"send_email"`). |
| `args` | `dict[str, Any]` | Raw (unwrapped) argument values passed to the tool, keyed by parameter name. Capability wrappers stripped for serializability. |
| `memory_snapshot` | `dict[str, CaMeLValue]` | Shallow copy of the interpreter variable store **after** the tool call completes. `CaMeLValue` instances (with capability metadata) are preserved for policy audit. |

---

### `ExecutionTrace`

```python
ExecutionTrace = list[TraceRecord]
```

Ordered list of trace records produced by a completed execution (M2-F12).

---

### `RedactedError`

```python
@dataclass(frozen=True)
class RedactedError:
    error_type: str
    lineno: int | None
    message: str | None
    trust_level: Literal["trusted", "untrusted", "not_enough_information"]
```

A sanitised exception representation safe to forward to the P-LLM.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `error_type` | `str` | Python exception class name (e.g. `"TypeError"`). Always present. |
| `lineno` | `int \| None` | 1-based source line number, or `None` when unavailable or redacted. |
| `message` | `str \| None` | Human-readable error text, or `None` when redacted for security. |
| `trust_level` | `Literal[...]` | Which redaction rule applied: `"trusted"`, `"untrusted"`, or `"not_enough_information"`. Written to the security audit log per NFR-6. |

**Redaction cases (in priority order):**

| Case | `trust_level` | Fields included |
|---|---|---|
| `NotEnoughInformationError` | `"not_enough_information"` | `error_type` only. |
| Untrusted-dependency exception | `"untrusted"` | `error_type` + `lineno`. |
| Trusted-origin exception | `"trusted"` | `error_type` + `lineno` + `message`. |

---

### `AcceptedState`

```python
@dataclass(frozen=True)
class AcceptedState:
    variable_names: frozenset[str]
    executed_statement_count: int
    remaining_source: str
```

Snapshot of successfully executed interpreter state at the point of failure.
Passed to `RetryPromptBuilder` for partial re-execution prompt construction (M2-F14).
Variable values are **never** included — only names — to preserve the P-LLM isolation invariant.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `variable_names` | `frozenset[str]` | Variable names currently defined in the interpreter store. |
| `executed_statement_count` | `int` | Number of top-level AST statements successfully executed before failure. |
| `remaining_source` | `str` | Source text of plan statements that had not yet executed, reconstructed via `ast.unparse()`. |

---

## `ExceptionRedactor`

```python
class ExceptionRedactor:
    DEFAULT_TRUSTED_SOURCES: frozenset[str] = frozenset({"User literal", "CaMeL"})

    def __init__(
        self,
        trusted_sources: frozenset[str] | None = None,
    ) -> None:
```

Classifies runtime exceptions and produces sanitised `RedactedError` instances.

Trust is determined by inspecting the `sources` field of every `CaMeLValue` in
the provided interpreter store snapshot.  If any source is outside the trusted
set, the exception is classified as untrusted-dependency.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `trusted_sources` | `frozenset[str] \| None` | `frozenset({"User literal", "CaMeL"})` | Source labels considered trusted for exception message inclusion. |

---

### `ExceptionRedactor.classify`

```python
def classify(
    self,
    exc: BaseException,
    interpreter_store_snapshot: dict[str, CaMeLValue],
) -> RedactedError:
```

Produce a `RedactedError` from `exc` under the store context.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `exc` | `BaseException` | The exception raised during execution. |
| `interpreter_store_snapshot` | `dict[str, CaMeLValue]` | Shallow copy of the interpreter variable store at the moment the exception was caught. |

**Returns** `RedactedError` — sanitised error representation with `trust_level` documenting which rule applied.

---

## `RetryPromptBuilder`

```python
class RetryPromptBuilder:
    def build(
        self,
        accepted_state: AcceptedState,
        error: RedactedError,
        tool_signatures: list[ToolSignature],
        user_context: UserContext | None = None,
    ) -> str:
```

Builds the P-LLM user-turn message for partial re-execution retries (M2-F14).

Constructs a prompt communicating: already-defined variable names (no values),
the redacted error, and an instruction to regenerate **only** the remaining steps.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `accepted_state` | `AcceptedState` | Snapshot of the successfully executed state at the failure point. |
| `error` | `RedactedError` | Sanitised exception representation from `ExceptionRedactor`. |
| `tool_signatures` | `list[ToolSignature]` | Registered tool signatures included in the retry prompt. |
| `user_context` | `UserContext \| None` | Optional trusted metadata forwarded verbatim. |

**Returns** `str` — user-turn message string to append to the P-LLM conversation.

---

## `TraceRecorder`

```python
@dataclass
class TraceRecorder:
    @property
    def trace(self) -> ExecutionTrace: ...
    def wrap_tools(self, tools: dict[str, Any], interpreter: CaMeLInterpreter) -> dict[str, Any]: ...
    def reset(self) -> None: ...
```

Records successful tool-call events into an `ExecutionTrace` (M2-F12).

The orchestrator uses `wrap_tools` to inject tracing closures around each
registered tool callable before handing the tools dict to `CaMeLInterpreter`.

### `TraceRecorder.trace` (property)

Returns the accumulated execution trace (ordered list of `TraceRecord` instances).  Returns a copy — mutations do not affect the recorder.

### `TraceRecorder.wrap_tools`

```python
def wrap_tools(
    self,
    tools: dict[str, Any],
    interpreter: CaMeLInterpreter,
) -> dict[str, Any]:
```

Wrap each tool callable with a tracing closure.  For each `(name, fn)` pair,
returns a new callable that calls the original, appends a `TraceRecord` on
success, and re-raises without recording on exception.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `tools` | `dict[str, Any]` | Dict of tool callables accepted by `CaMeLInterpreter`. |
| `interpreter` | `CaMeLInterpreter` | The interpreter whose store will be snapshotted after each successful tool call. |

**Returns** `dict[str, Any]` — new dict with tracing-wrapped callables.

### `TraceRecorder.reset`

```python
def reset(self) -> None:
```

Clear the accumulated trace, resetting to an empty list.
Called by `CaMeLOrchestrator` at the start of each outer-loop iteration.

---

## `DisplayChannel` Protocol

```python
@runtime_checkable
class DisplayChannel(Protocol):
    def write(self, value: CaMeLValue) -> None: ...
```

Routing sink for `print()` output from execution plans (M2-F10).

Implementations may write to a terminal, message queue, websocket, or in-memory
buffer.  The default implementation is `StdoutDisplayChannel`.

### `DisplayChannel.write`

**Parameters**

| Name | Type | Description |
|---|---|---|
| `value` | `CaMeLValue` | The `CaMeLValue` passed to `print()` in the execution plan. Implementations should display `value.raw` to the end user. |

---

## `StdoutDisplayChannel`

```python
class StdoutDisplayChannel:
    def write(self, value: CaMeLValue) -> None:
```

Default `DisplayChannel` — calls `print(value.raw)` for each value.  Suitable
for CLI usage and development.  Production deployments should supply a custom
implementation.

---

## `MaxRetriesExceededError`

```python
class MaxRetriesExceededError(Exception):
    attempts: int
    last_error: RedactedError | None
```

Raised when the outer execution-loop retry ceiling is reached (M2-F8).

**Attributes**

| Name | Type | Description |
|---|---|---|
| `attempts` | `int` | Number of outer-loop retries attempted before giving up. |
| `last_error` | `RedactedError \| None` | The most recent `RedactedError`, or `None` if failure occurred before any exception was classified. |

---

## Full Usage Example

```python
import asyncio
from camel.execution_loop import CaMeLOrchestrator
from camel.llm import PLLMWrapper, ToolSignature
from camel.interpreter import CaMeLInterpreter
from camel.llm.backend import get_backend
from camel.value import wrap

# --- Set up tools ---
def get_email():
    return wrap({"from": "alice@example.com", "body": "Hello"},
                sources=frozenset({"get_email"}))

def send_email(to, body):
    print(f"[TOOL] Sending to {to}: {body}")
    return wrap(None, sources=frozenset({"send_email"}))

tools = {"get_email": get_email, "send_email": send_email}

# --- Set up P-LLM ---
backend = get_backend("claude", api_key="sk-...")
p_llm = PLLMWrapper(backend)

# --- Set up interpreter ---
interp = CaMeLInterpreter(tools=tools)

# --- Define tool signatures for the P-LLM ---
sigs = [
    ToolSignature("get_email", "", "EmailMessage", "Fetch the latest email."),
    ToolSignature("send_email", "to: str, body: str", "None", "Send an email."),
]

# --- Run the orchestrator ---
orchestrator = CaMeLOrchestrator(p_llm=p_llm, interpreter=interp, tool_signatures=sigs)
result = asyncio.run(orchestrator.run("Forward the last email to bob@example.com"))

print(f"Tool calls made: {len(result.trace)}")
print(f"Loop attempts: {result.loop_attempts}")
for record in result.trace:
    print(f"  {record.tool_name}({record.args})")
```

---

*See also: [LLM Backend API](llm_backend.md) · [P-LLM Wrapper API](p_llm.md) · [Q-LLM Wrapper API](q_llm.md) · [Interpreter API](interpreter.md)*
