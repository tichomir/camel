# ADR-007: Execution Loop Orchestrator Architecture

| Field         | Value                                                                   |
|---------------|-------------------------------------------------------------------------|
| Status        | Accepted                                                                |
| Date          | 2026-03-17                                                              |
| Author        | Software Architect Persona                                              |
| Supersedes    | —                                                                       |
| Superseded by | —                                                                       |
| Related       | ADR-003 (AST Interpreter), ADR-005 (P-LLM Wrapper), ADR-006 (Q-LLM)   |

---

## Context

Milestone 2 requires wiring the P-LLM wrapper, Q-LLM wrapper, and the Milestone 1
interpreter into a cohesive execution loop.  The loop must:

- Invoke the P-LLM once to obtain an initial execution plan.
- Execute the plan statement-by-statement via the `CaMeLInterpreter`.
- On exception: **redact** the error based on data-provenance trust, rebuild the
  P-LLM prompt with already-accepted state and remaining code, then retry.
- Enforce a hard ceiling of **10 outer-loop retries** (`MaxRetriesExceededError`).
- Record an **execution trace** of every successful tool call.
- Route `print()` calls to a **display channel** that is distinct from the trace.

Five concrete deliverables govern this ADR:

| ID     | Deliverable                                         | PRD ref  |
|--------|-----------------------------------------------------|----------|
| M2-F8  | Retry loop ceiling + `MaxRetriesExceededError`      | PRD §6.1 |
| M2-F10 | `print()` → display channel (not execution trace)  | PRD §6.1 |
| M2-F12 | Execution trace: ordered `(tool, args, snapshot)`  | PRD §6.1 |
| M2-F14 | Partial re-execution prompt builder                 | PRD §6.1 |
| REDACT | Exception redaction contract                        | PRD §6.1 |

---

## Decision

### 1. Orchestrator State Machine

The `CaMeLOrchestrator` is the top-level coordinator.  Its internal state machine
drives a single `run()` call from initial plan generation through to success or
exhaustion.

```
INITIAL
  │
  ▼
PLAN_GENERATION ──── PLLMWrapper.generate_plan() ────────────────────────────►
  │   ▲                 (inner retry ≤ 10 for CodeBlockNotFoundError/SyntaxError)
  │   │
  │   └── PLLMRetryExhaustedError ──────────────────────────────► FAILED
  │
  ▼
EXECUTING ──── CaMeLInterpreter.exec_statement() per statement ──────────────►
  │
  │──── all statements complete without exception ───────────────► SUCCEEDED
  │
  │──── exception raised
  ▼
EXCEPTION_RECEIVED
  │
  ▼
REDACTING ──── ExceptionRedactor.classify() ─────────────────────────────────►
  │
  │──── loop_attempts >= MAX_LOOP_RETRIES ───────────────────────► FAILED
  │                                                                (MaxRetriesExceededError)
  ▼
RETRY_PROMPT_BUILD ──── RetryPromptBuilder.build() ──────────────────────────►
  │
  ▼
PLAN_REGENERATION ──── PLLMWrapper.generate_plan() ──────────────────────────►
  │   (inner retry ≤ 10 for CodeBlockNotFoundError/SyntaxError)
  │
  │──── PLLMRetryExhaustedError ──────────────────────────────────► FAILED
  │
  └──► EXECUTING  (continue with regenerated remaining code)
```

**State invariants:**

- `SUCCEEDED` and `FAILED` are terminal states; `run()` returns or raises.
- The outer loop retry counter increments on every `EXCEPTION_RECEIVED →
  REDACTING` transition.
- The inner P-LLM retry counter is managed by `PLLMWrapper` independently;
  exhaustion there produces `PLLMRetryExhaustedError`, which propagates as a
  `FAILED` transition regardless of the outer loop counter.
- `loop_attempts` starts at 0; the ceiling is `max_loop_retries` (default 10).

---

### 2. `CaMeLOrchestrator` Class Interface

```python
class CaMeLOrchestrator:
    """Wires P-LLM, CaMeLInterpreter, Q-LLM, and tool dispatch into a complete
    execution loop.

    Parameters
    ----------
    p_llm:
        Configured :class:`~camel.llm.PLLMWrapper` for plan generation and
        re-generation.
    interpreter:
        A :class:`~camel.CaMeLInterpreter` instance pre-loaded with tool
        callables and optional policy engine.
    tool_signatures:
        Ordered list of :class:`~camel.llm.ToolSignature` objects passed to
        the P-LLM system prompt.
    display_channel:
        :class:`DisplayChannel` implementation receiving all ``print()``
        output from the execution plan.  Defaults to
        :class:`StdoutDisplayChannel`.
    max_loop_retries:
        Maximum number of outer-loop retries (M2-F8).  Default: 10.
    """

    async def run(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> ExecutionResult:
        """Execute *user_query* through the full CaMeL pipeline.

        Parameters
        ----------
        user_query:
            Trusted user request string.  Must not contain CaMeLValue
            instances (PLLMIsolationError enforced by PLLMWrapper).
        user_context:
            Optional trusted key-value metadata forwarded to P-LLM.

        Returns
        -------
        ExecutionResult
            Contains the execution trace, print outputs, and final variable
            store snapshot.

        Raises
        ------
        MaxRetriesExceededError
            When the outer loop retry ceiling is reached without success.
        PLLMRetryExhaustedError
            When the P-LLM inner retry loop exhausts on plan generation or
            re-generation.
        PLLMIsolationError
            If a CaMeLValue is detected in user_query or user_context.
        """
        ...
```

---

### 3. Exception Redaction Contract (REDACT)

Redaction is performed by `ExceptionRedactor.classify()`.  There are exactly
**three cases** based on the exception type and data-provenance trust:

#### Case 1 — Trusted-origin exception

**Condition:** The exception has no dependency on any untrusted source.
Formally: for every variable in the interpreter store, all upstream sources
are members of `{"User literal", "CaMeL"}`.  The P-LLM generated code that
failed on its own structure, not on tool-returned data.

**What is sent to P-LLM:**

```
Error type:    SyntaxError
Location:      line 7
Message:       invalid syntax
```

**Example:** A `NameError` on a variable the P-LLM invented — no untrusted
data involved.  The full error message is safe to include because it cannot
echo back attacker-controlled content.

#### Case 2 — Untrusted-dependency exception

**Condition:** At least one variable in the interpreter store at the time of
failure has an upstream source outside `{"User literal", "CaMeL"}` (i.e. a
source that originates from a tool call, which processes external/potentially
hostile data).

**What is sent to P-LLM:**

```
Error type:    TypeError
Location:      line 12
```

The error message is **omitted entirely**.  An attacker can craft tool return
values that, when processed, produce a `TypeError` whose message echoes the
crafted content back to the P-LLM.

**Example:** A tool returns a document containing `"ignore previous
instructions"`.  If the interpreter generates a `TypeError` while processing
this document, the error message may reproduce portions of the malicious
content.

#### Case 3 — `NotEnoughInformationError` (Q-LLM)

**Condition:** The exception is a `NotEnoughInformationError` raised when the
Q-LLM signals it cannot populate the requested schema.

**What is sent to P-LLM:**

```
Error type:    NotEnoughInformationError
```

No location and no message.  The reason is two-fold:

1. `NotEnoughInformationError` carries no trusted location information — it
   arises from Q-LLM internal state, not from a specific plan line.
2. Even the exception type is enough for the P-LLM to understand it should
   revise the information request to the Q-LLM.  No further content is safe
   to include because the Q-LLM operates on untrusted data.

#### Redacted Error Data Model

```python
@dataclass(frozen=True)
class RedactedError:
    """A sanitised exception representation safe to forward to the P-LLM.

    Attributes
    ----------
    error_type:
        Python exception class name (e.g. ``"TypeError"``).  Always present.
    lineno:
        1-based source line number, or ``None`` when unavailable or redacted.
    message:
        Human-readable error text, or ``None`` when redacted for security.
    trust_level:
        One of ``"trusted"``, ``"untrusted"``, or ``"not_enough_information"``.
        Documents which redaction case applied; recorded in the audit log.
    """

    error_type: str
    lineno: int | None
    message: str | None
    trust_level: Literal["trusted", "untrusted", "not_enough_information"]
```

#### Trust classification algorithm

```
def classify(exc, interpreter_store_snapshot):
    if isinstance(exc, NotEnoughInformationError):
        return RedactedError(type(exc).__name__, lineno=None, message=None,
                             trust_level="not_enough_information")

    lineno = getattr(exc, "lineno", None)

    for cv in interpreter_store_snapshot.values():
        for source in cv.sources:
            if source not in {"User literal", "CaMeL"}:
                return RedactedError(type(exc).__name__, lineno=lineno,
                                     message=None, trust_level="untrusted")

    return RedactedError(type(exc).__name__, lineno=lineno,
                         message=str(exc), trust_level="trusted")
```

---

### 4. Partial Re-Execution Prompt Builder (M2-F14)

When a statement fails, the orchestrator captures:

- **`AcceptedState`** — a snapshot of the interpreter variable store at the
  point of failure: variable **names only** (no raw values, to preserve the
  P-LLM isolation invariant).
- **`remaining_source`** — the unexecuted portion of the plan, i.e. the source
  of statements not yet successfully executed.

The `RetryPromptBuilder` constructs a new user-turn message appended to the
existing P-LLM conversation:

```
## PARTIAL RE-EXECUTION REQUEST (M2-F14)

The following variables are already defined in the interpreter — treat them
as opaque handles; do NOT recompute them:

  email_1, recipient_addr, subject_line

The plan failed at the next step with:

  Error type:   TypeError
  Location:     line 8
  Message:      (redacted)

Generate ONLY the remaining steps starting from after the last successful
statement.  Reference the already-defined variables by name.  Do NOT
reproduce completed steps.  Emit a single corrected ```python``` code block.
```

**Interface:**

```python
class RetryPromptBuilder:
    def build(
        self,
        accepted_state: AcceptedState,
        error: RedactedError,
        tool_signatures: list[ToolSignature],
        user_context: UserContext | None = None,
    ) -> str:
        ...
```

**Input/Output types:**

| Parameter        | Type                     | Description                                          |
|------------------|--------------------------|------------------------------------------------------|
| `accepted_state` | `AcceptedState`          | Variable names + remaining source from failure point |
| `error`          | `RedactedError`          | Sanitised exception (type, lineno, message)          |
| `tool_signatures`| `list[ToolSignature]`    | Tool stubs for the retry system prompt               |
| `user_context`   | `UserContext \| None`     | Trusted context forwarded verbatim                   |
| **returns**      | `str`                    | User-turn message to append to P-LLM conversation   |

`AcceptedState` data model:

```python
@dataclass(frozen=True)
class AcceptedState:
    """Snapshot of successfully executed interpreter state at point of failure.

    Attributes
    ----------
    variable_names:
        Frozenset of variable names currently defined in the interpreter store.
        Names only — no values — to maintain the P-LLM isolation invariant.
    executed_statement_count:
        Number of top-level statements successfully executed before failure.
    remaining_source:
        Source text of statements not yet executed (from failure point to end
        of plan).
    """

    variable_names: frozenset[str]
    executed_statement_count: int
    remaining_source: str
```

---

### 5. Execution Trace Recorder (M2-F12)

Every successful tool call is appended to an ordered `ExecutionTrace` by the
`TraceRecorder`.  The trace is available on `ExecutionResult` after a
successful run.

**Data model:**

```python
@dataclass(frozen=True)
class TraceRecord:
    """A single successful tool-call event in the execution trace.

    Attributes
    ----------
    tool_name:
        The registered tool name called (e.g. ``"send_email"``).
    args:
        Raw argument values passed to the tool, keyed by parameter name.
        These are ``CaMeLValue.raw`` extractions — the bare Python values
        without capability wrappers.
    memory_snapshot:
        Shallow copy of the interpreter variable store **after** the tool
        call completes.  Values are full :class:`~camel.CaMeLValue` instances
        (with capability metadata) for policy audit purposes.
    """

    tool_name: str
    args: dict[str, Any]
    memory_snapshot: dict[str, CaMeLValue]


#: Ordered sequence of trace records from a completed execution.
ExecutionTrace = list[TraceRecord]
```

**Mechanics:** The orchestrator wraps each registered tool callable in a
`_tracing_wrapper` closure before passing the tools dict to `CaMeLInterpreter`.
The wrapper records `(tool_name, kwargs, post_call_store_snapshot)` on each
successful invocation and appends a `TraceRecord` to an internal list.  This
requires no changes to the `CaMeLInterpreter` interface.

---

### 6. `print()` Display Channel (M2-F10)

`print()` calls in the execution plan are intercepted by a custom builtin
registered with the interpreter.  The interceptor does **not** write to
`sys.stdout` directly; instead it routes the value through the `DisplayChannel`
protocol.

The display channel is strictly separated from the execution trace:

| Channel          | Contents                                   | Consumer            |
|------------------|--------------------------------------------|---------------------|
| `ExecutionTrace` | Successful tool call records               | Security audit log  |
| `DisplayChannel` | Values explicitly `print()`-ed by the plan | User / UI           |

**Protocol:**

```python
class DisplayChannel(Protocol):
    """Routing sink for print() output from execution plans (M2-F10).

    Implementations determine where plan-level print() output goes.
    The default is StdoutDisplayChannel.  UI adapters can supply a
    custom implementation that writes to a message queue or websocket.
    """

    def write(self, value: CaMeLValue) -> None:
        """Route *value* to this channel's output sink.

        Parameters
        ----------
        value:
            The :class:`~camel.CaMeLValue` passed to print() in the plan.
            Callers inspect ``value.raw`` for the display string.
        """
        ...
```

**Registration:** The orchestrator registers a `_print_builtin` in the
interpreter's `builtins` dict.  It calls `display_channel.write(value)` and
returns a `CaMeLValue` wrapping `None` (to satisfy the interpreter's
requirement that builtins return a wrappable value).

---

### 7. `MaxRetriesExceededError` and Retry Ceiling (M2-F8)

```python
class MaxRetriesExceededError(Exception):
    """Raised when the outer execution-loop retry ceiling is reached.

    The orchestrator retries the execution loop up to *max_loop_retries*
    times (default 10) on any runtime exception.  When all attempts are
    exhausted without a successful plan completion, this exception is raised.

    Attributes
    ----------
    attempts:
        The number of outer-loop retries attempted before giving up.
    last_error:
        The most recent :class:`RedactedError` produced by the exception
        redactor.  May be ``None`` if failure occurred before any exception
        was classified.
    """

    def __init__(
        self,
        attempts: int,
        last_error: RedactedError | None = None,
    ) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"CaMeL execution loop exhausted after {attempts} retries."
        )
```

**Ceiling contract:**

- Default ceiling: `max_loop_retries = 10`.
- Counter increments on every outer-loop `EXCEPTION_RECEIVED → REDACTING`
  transition.
- The P-LLM's own inner retry loop (up to 10 per `PLLMWrapper`) counts
  independently; its exhaustion produces `PLLMRetryExhaustedError` which is
  propagated immediately (not counted against the outer loop).
- `PolicyViolationError` and `UnsupportedSyntaxError` increment the outer
  counter like any other runtime exception; they are classified as
  trusted-origin (their message text is derived from the interpreter logic,
  not tool data).

---

### 8. `ExecutionResult` Data Model

Returned by `CaMeLOrchestrator.run()` on success.

```python
@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of a successful CaMeL execution loop run.

    Attributes
    ----------
    trace:
        Ordered list of :class:`TraceRecord` entries — one per successful
        tool call (M2-F12).
    print_outputs:
        Ordered list of :class:`~camel.CaMeLValue` instances passed to
        ``print()`` during execution (M2-F10).
    final_store:
        Shallow snapshot of the interpreter variable store after the plan
        completes.
    loop_attempts:
        Number of outer-loop retries consumed (0 = first attempt succeeded).
    """

    trace: ExecutionTrace
    print_outputs: list[CaMeLValue]
    final_store: dict[str, CaMeLValue]
    loop_attempts: int
```

---

### 9. Module Layout

```
camel/
    execution_loop.py    ← NEW: all interfaces, data models, stub classes
    interpreter.py       ← existing (unchanged)
    value.py             ← existing (unchanged)
    dependency_graph.py  ← existing (unchanged)
    llm/
        p_llm.py         ← existing
        qllm.py          ← existing
        ...
docs/adr/
    007-execution-loop-orchestrator.md  ← THIS FILE
```

All symbols are defined in `camel/execution_loop.py` and re-exported from
`camel/__init__.py`.

---

## Consequences

### Positive

- **Complete isolation preserved.** The P-LLM never receives raw exception
  messages from untrusted-dependency exceptions or `NotEnoughInformationError`,
  closing the side channel where a malicious tool response could inject content
  via error messages.
- **Statement-level retry granularity.** The orchestrator re-executes only the
  remaining statements after a failure, avoiding unnecessary re-execution of
  already-accepted work (saves tokens and avoids re-triggering side effects).
- **Display vs. audit separation.** `print()` output goes to `DisplayChannel`;
  tool call records go to `ExecutionTrace`.  UI adapters, logging systems, and
  security audit consumers all receive exactly the data they need.
- **Composable with policy engine.** `PolicyViolationError` flows through the
  same redaction and retry path as any other exception, without special-casing.
- **Testable in isolation.** `ExceptionRedactor`, `RetryPromptBuilder`, and
  `TraceRecorder` are independent classes with well-typed interfaces; each can
  be unit-tested with mock inputs.

### Negative / Trade-offs

- **Dual retry ceilings.** Two independent retry ceilings (inner P-LLM: 10,
  outer loop: 10) mean the maximum number of LLM calls before failure is
  bounded by `max_loop_retries × (max_inner_retries + 1)`.  Operators should
  tune these values together.
- **Statement-level execution granularity requires AST splitting.** To track
  "how many statements have been executed", the orchestrator must parse the plan
  into individual statement nodes and call `interpreter.exec()` per statement
  (passing each as a single-statement code string).  This adds a thin
  orchestration layer around the interpreter that must handle AST-level source
  reconstruction (via `ast.unparse()`).
- **Trust classification uses store-level heuristic.** The current algorithm
  classifies an exception as untrusted if *any* variable in the store has an
  untrusted source, not specifically the variables involved in the failing
  statement.  This is conservative (may over-redact) but safe.  A more precise
  implementation would analyse the failing statement's AST node to identify
  only the argument variables involved.

---

## Alternatives Considered

### A. Single retry loop handling both plan-generation and execution failures

Merge the P-LLM inner retry (for `CodeBlockNotFoundError`/`SyntaxError`) and
the outer execution-loop retry into a single counter.

**Rejected.**  Plan-generation failures (malformed code blocks) are structurally
different from execution failures (runtime exceptions during tool calls).  A
single counter mixes unrelated failure modes, making it harder to tune the
ceiling for each independently and harder to diagnose failure patterns in logs.

### B. Expose full exception messages to P-LLM for richer re-planning

Always pass the full exception message (including `str(exc)`) to the P-LLM
to improve its ability to fix the failing plan.

**Rejected.**  An adversary who controls tool return values can craft data that,
when processed, produces an exception whose message echoes the adversary's
content.  Forwarding that message to the P-LLM opens the data-to-control
injection channel that CaMeL is specifically designed to close.

### C. Restart from the beginning on every exception

Discard the interpreter store and re-execute the full plan from statement 1
on each retry.

**Rejected.**  Restarting re-triggers all side-effecting tool calls (e.g.
sending an email, writing a file) that already completed successfully.  This
violates the principle of least surprise, could produce duplicate side effects,
and wastes tokens.

### D. Store `CaMeLValue` instances (with raw values) in `AcceptedState`

Pass the full interpreter store (including raw values) to the retry prompt
builder and expose them to the P-LLM as a structured "context".

**Rejected.**  This would break the P-LLM isolation invariant (ADR-005 §7).
Tool return values must never reach the P-LLM.  Only variable *names* are safe
to include in the retry prompt.

---

## References

- `camel/execution_loop.py` — implementation stubs (companion to this ADR)
- `camel/interpreter.py` — `CaMeLInterpreter`, `UnsupportedSyntaxError`, `PolicyViolationError`
- `camel/llm/p_llm.py` — `PLLMWrapper`, `PLLMRetryExhaustedError`
- `camel/llm/qllm.py` — `QLLMWrapper`, `NotEnoughInformationError`
- `camel/value.py` — `CaMeLValue`, capability fields
- ADR-003 — AST interpreter architecture and `print()` builtin exclusion
- ADR-005 — P-LLM isolation contract
- ADR-006 — Q-LLM schema injection and `NotEnoughInformationError`
- CaMeL paper §6.1 (P-LLM retry loop), §6.3 (side-channel mitigations)
- PRD §6.1, NFR-2, NFR-5, NFR-6
