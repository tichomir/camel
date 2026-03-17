# ADR-005: P-LLM Wrapper Architecture and Isolation Contract

| Field         | Value                                  |
|---------------|----------------------------------------|
| Status        | Accepted                               |
| Date          | 2026-03-17                             |
| Author        | Software Architect Persona             |
| Supersedes    | —                                      |
| Superseded by | —                                      |
| Related       | ADR-001 (Q-LLM Isolation Contract), ADR-003 (AST Interpreter Architecture) |

---

## Context

The P-LLM (Privileged LLM) is the orchestration and planning component of
CaMeL.  It receives the user's query and a description of available tools,
then generates a *pseudo-Python execution plan* — a self-contained code block
that the CaMeL interpreter will execute step by step.

The fundamental security property that the P-LLM wrapper must enforce is:

> **The P-LLM never receives tool return values.**

All variable values produced during plan execution live exclusively in the
interpreter's variable store.  The P-LLM sees only the *structure* of the
code it writes — never the data that flows through it.  This breaks the data
path through which prompt injection attacks escalate from data manipulation to
control-flow hijacking.

A second class of requirement concerns **retry resilience**: the P-LLM may
produce syntactically invalid or interpreter-rejected code.  The wrapper must
retry up to 10 times, feeding back redacted error information (to avoid
leaking untrusted data through error messages).

---

## Decision

### 1. Class Hierarchy

```
camel.llm.protocols.LLMBackend          (Protocol — § 2)
    │
    ├── camel.llm.adapters.ClaudeBackend  (concrete — anthropic SDK)
    └── camel.llm.adapters.GeminiBackend  (concrete — google-generativeai SDK)

camel.llm.pllm.PLLMWrapper              (§ 3)
    uses-a ──► LLMBackend
    produces ► str  (raw P-LLM response)
    parses   ► str  (extracted code block, via CodeBlockParser — § 4)
```

`PLLMWrapper` depends on the `LLMBackend` Protocol, not on any concrete
adapter.  The concrete adapters (`ClaudeBackend`, `GeminiBackend`) already
satisfy this protocol via structural subtyping (see `camel/llm/protocols.py`).

---

### 2. `LLMBackend` Protocol

The protocol is defined in `camel/llm/protocols.py`.  Its minimal surface is:

```python
from typing import Any, Protocol, runtime_checkable

Message = dict[str, Any]

@runtime_checkable
class LLMBackend(Protocol):
    """Structural interface for the Privileged LLM backend.

    Implementations power the P-LLM planning loop.  They MAY support tool
    definitions via additional keyword arguments; the protocol captures only
    the minimal surface required by the runtime.
    """

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion string for *messages*.

        Parameters
        ----------
        messages:
            Ordered list of chat messages (system, user, assistant turns).
        **kwargs:
            Backend-specific options such as ``tools``, ``temperature``, etc.

        Returns
        -------
        str
            The model's text response.
        """
        ...
```

**Design choices:**

- `complete` returns **raw text** (`str`).  The P-LLM is expected to produce a
  Markdown-fenced code block inside this text; extraction is handled by
  `CodeBlockParser` (§ 4), not by the backend.
- `**kwargs` is intentionally open.  The P-LLM orchestration loop does not
  pass tool definitions via this interface; any tool-related kwargs are
  implementation details of the adapter (e.g. injecting a "generate Python
  plan" instruction).
- The protocol uses `runtime_checkable` so that test doubles and mock backends
  can be checked with `isinstance` in integration tests.

---

### 3. `PLLMWrapper` Class

Located at `camel/llm/pllm.py`.

#### 3.1 Constructor

```python
class PLLMWrapper:
    def __init__(
        self,
        backend: LLMBackend,
        tools: Sequence[ToolSignature],
        max_retries: int = 10,
    ) -> None:
        ...
```

| Parameter     | Description |
|---------------|-------------|
| `backend`     | Any object satisfying `LLMBackend`.  |
| `tools`       | Ordered sequence of `ToolSignature` objects describing available tools.  Used to populate the system prompt's tool-signature section (§ 5.3). |
| `max_retries` | Maximum number of code-generation attempts before raising `PLLMRetryExhaustedError`.  Default: 10 (per PRD §6.1). |

#### 3.2 Primary method: `generate_plan`

```python
async def generate_plan(
    self,
    user_query: str,
    context: Mapping[str, str] | None = None,
    previous_error: PLLMError | None = None,
) -> str:
    """Generate a CaMeL pseudo-Python execution plan for *user_query*.

    Parameters
    ----------
    user_query:
        The trusted user request.  This is the ONLY content that describes
        what the agent should do.  Tool return values MUST NOT appear here.
    context:
        Optional key-value pairs providing additional trusted context
        (e.g. current date/time, session metadata).  Values are injected
        verbatim into the user-context section of the system prompt.
        MUST NOT contain tool return values.
    previous_error:
        When retrying after a failed plan execution, provides structured
        error information (type + location) to guide the next attempt.
        Error *content* from untrusted sources is redacted before being
        included in the prompt (§ 6.2).

    Returns
    -------
    str
        A valid CaMeL Python plan — the extracted code block from the
        P-LLM response.

    Raises
    ------
    PLLMRetryExhaustedError
        When all ``max_retries`` attempts fail to produce a parseable,
        interpreter-valid code block.
    PLLMIsolationError
        When a caller attempts to pass tool return values into
        ``user_query`` or ``context``.  (Runtime guard — see § 7.)
    """
    ...
```

#### 3.3 Retry loop

The wrapper implements the following retry loop (pseudocode):

```
for attempt in range(max_retries):
    messages = build_messages(user_query, context, previous_error)
    raw_response = await backend.complete(messages)

    try:
        code = CodeBlockParser.extract(raw_response)
        validate_syntax(code)           # ast.parse — syntax check only
        return code
    except (CodeBlockNotFoundError, SyntaxError) as exc:
        previous_error = PLLMError(
            attempt=attempt,
            error_type=type(exc).__name__,
            location=_safe_location(exc),   # line/col only, no message
        )
        continue

raise PLLMRetryExhaustedError(attempts=max_retries)
```

Key properties:
- Each retry appends the previous attempt's assistant turn and the error
  feedback to the message list, preserving conversation history for the model.
- `_safe_location` extracts only `lineno` / `col_offset` from `SyntaxError` —
  never the `msg` field, which may echo back attacker-controlled content if
  the error arose from a tool return value that was incorrectly threaded into
  the code.
- The `error_type` is always the Python class name of the exception (`str`
  constant), never a free-form message.
- `CodeBlockNotFoundError` and `SyntaxError` are recoverable.  Any exception
  raised by the backend itself (e.g. network error, rate limit) is propagated
  immediately without retrying.

---

### 4. `CodeBlockParser`

Located at `camel/llm/pllm.py` (private helper) or `camel/llm/_code_parser.py`.

#### 4.1 Parsing contract

```python
class CodeBlockParser:
    """Extract a CaMeL pseudo-Python code block from a P-LLM response."""

    FENCE_PATTERN = re.compile(
        r"```(?:python)?\s*\n(.*?)\n```",
        re.DOTALL | re.IGNORECASE,
    )

    @classmethod
    def extract(cls, response: str) -> str:
        """Return the first ```python … ``` fenced block in *response*.

        Parameters
        ----------
        response:
            Raw text returned by the P-LLM backend.

        Returns
        -------
        str
            The code content (without the fence markers).

        Raises
        ------
        CodeBlockNotFoundError
            When no fenced code block is found in *response*.
        """
        match = cls.FENCE_PATTERN.search(response)
        if match is None:
            raise CodeBlockNotFoundError(response=response)
        return match.group(1).strip()
```

**Design choices:**

- The parser accepts both ` ```python ` and bare ` ``` ` fences (the model
  sometimes omits the language tag).
- Only the **first** fenced block is extracted.  The P-LLM system prompt
  (§ 5) instructs the model to emit exactly one code block; additional blocks
  in the response are ignored.
- The parser does **not** validate interpreter compatibility — that is the
  interpreter's responsibility.  The wrapper performs only a lightweight
  `ast.parse` syntax check (to give the model parseable feedback) before
  returning the plan.

---

### 5. System Prompt Template

The system prompt is constructed from five ordered sections.  All sections
are assembled by `PLLMWrapper._build_system_prompt()`.

#### 5.1 CaMeL Python Subset Specification

Informs the model of the exact grammar it must generate.

```
You are a planning assistant for the CaMeL (Capabilities for Machine
Learning) secure agent runtime.  You generate execution plans expressed as
restricted Python code.

SUPPORTED CONSTRUCTS ONLY — do not use any other Python syntax:

Statements:
  - Assignment:         result = tool_call(arg)
  - Augmented assign:   counter += 1
  - Conditional:        if condition: / else:
  - For loop:           for item in collection:
  - Expression stmt:    tool_call(arg)   # for side-effects

Expressions:
  - Literals:           42, "hello", True, None, [1, 2], {"k": v}, (a, b)
  - Variable reference: my_var
  - Binary operators:   a + b, a - b, a * b, a / b, a // b, a % b, a ** b
  - Unary operators:    -x, not x
  - Boolean operators:  a and b, a or b
  - Comparisons:        ==, !=, <, >, <=, >=, in, not in, is, is not
  - Attribute access:   obj.field
  - Subscript access:   container[key]
  - f-strings:          f"Hello {name}"
  - Function calls:     func(positional, keyword=value)
  - Tuple unpack:       a, b = pair

FORBIDDEN (raises an interpreter error if generated):
  while, def, class, lambda, import, try/except, with, async/await,
  list/dict/set comprehensions, generators, yield, assert, raise, pass,
  break, continue, return, delete, global, nonlocal, starred (*args),
  double-starred (**kwargs).
```

#### 5.2 Opaque-Variable Instruction (M2-F13)

This section enforces the core P-LLM isolation guarantee.

```
CRITICAL — OPAQUE VARIABLES:

You will never see the actual values that tools return.  Every tool call
result is stored in a variable whose contents are completely opaque to you.
You MUST write code that passes these variables to subsequent tool calls
without attempting to read, inspect, or reason about their contents.

Examples of correct usage:
  email = get_last_email()
  send_reply(email, body="Acknowledged.")    # ✓ pass opaque variable

Examples of FORBIDDEN attempts to inspect values:
  if email.sender == "bob@example.com":      # ✗ do not branch on tool data
  body = email.subject.upper()               # ✗ do not assume field access

The interpreter controls all data access.  Your job is to describe the
control flow and tool call sequence — not to process data values.
```

**Rationale for M2-F13:** If the P-LLM were allowed to reason about variable
contents, an adversary could craft tool return values that cause the P-LLM to
change its plan — defeating the control-flow isolation guarantee.  This
instruction reinforces the architectural invariant at the prompt level.

#### 5.3 Tool Signatures

Injected dynamically per invocation from the registered `ToolSignature` list.

```
AVAILABLE TOOLS:

You may call only the following tools.  Each is defined as a Python function
signature.  Do not invent tools not listed here.

{for each tool in tools:}
  def {tool.name}({tool.signature}) -> {tool.return_type}:
      """{tool.description}"""
{end for}
```

`ToolSignature` is a dataclass:

```python
@dataclass(frozen=True)
class ToolSignature:
    name:        str
    signature:   str          # parameter list, e.g. "recipient: str, body: str"
    return_type: str          # e.g. "EmailMessage" or "None"
    description: str          # one-sentence docstring shown to P-LLM
```

Tool return types are intentionally *nominal only* (a name string, not a
schema) — the P-LLM must treat returned objects as opaque per M2-F13.

#### 5.4 User Context

Provides trusted, non-tool context the model may reference.

```
CONTEXT:

{for key, value in context.items():}
  {key}: {value}
{end for}
```

Example entries: `current_date`, `user_timezone`, `session_id`.  Values MUST
come from trusted platform sources, never from tool return values.

#### 5.5 `print()` Usage Guidance (M2-F10)

```
OUTPUT TO USER:

To surface a value to the user, call print() with the variable:

  result = compute_summary(data)
  print(result)              # ✓ instructs the runtime to display this value

The runtime will display the raw value associated with the variable.
Do NOT attempt to format or process the value before printing — pass the
variable directly.

print() is the ONLY side-effectful output mechanism available.  Do not use
it to debug intermediate states; use it only for values that the user should
see as final output.
```

**Rationale for M2-F10:** `print` is not registered as a default builtin in
the interpreter (see ADR-003 §4.5) because it is side-effectful and a
potential side-channel for sensitive data.  The system prompt instructs the
P-LLM to use it only for intended user-visible output, so the runtime can
audit every `print` call against the security policy.

#### 5.6 Output Format Instruction

```
OUTPUT FORMAT:

Respond with exactly ONE fenced Python code block containing the complete
execution plan.  Do not add explanation outside the code block.

```python
# your plan here
```
```

---

### 6. Message Construction and Retry Context

#### 6.1 First attempt

```
[
  {"role": "system",    "content": <full system prompt — §5>},
  {"role": "user",      "content": <user_query>},
]
```

#### 6.2 Retry attempt (N > 0)

Each retry appends the previous assistant response and an error-feedback user
turn to the existing conversation history.  The conversation grows by two
messages per retry:

```
[
  {"role": "system",    "content": <system prompt>},
  {"role": "user",      "content": <user_query>},
  # --- previous attempt ---
  {"role": "assistant", "content": <previous raw response>},
  {"role": "user",      "content": <redacted error feedback>},
]
```

The **redacted error feedback** message follows this template:

```
Your previous plan produced an error.

Error type: {error_type}        # e.g. "SyntaxError", "CodeBlockNotFoundError"
Location:   line {lineno}       # omitted if unavailable

Please revise the plan and emit a single corrected ```python``` code block.
Do NOT include any explanation.
```

**Redaction rule:** Only `error_type` (Python class name) and `lineno` (int)
are included.  The error `message` text is **never** included because:
1. It may echo back attacker-controlled content (e.g. the model generated
   `x = "injected string with syntax chars"` and the SyntaxError message
   reproduces the literal).
2. Line number is sufficient for the model to locate and fix the offending
   construct.

---

### 7. Isolation Contract — P-LLM Inputs

The isolation contract is enforced at three levels:

#### 7.1 Architectural (hard guarantee)

The `PLLMWrapper.generate_plan` method signature accepts only:
- `user_query: str` — trusted user input
- `context: Mapping[str, str] | None` — trusted platform metadata
- `previous_error: PLLMError | None` — redacted interpreter error

Tool return values (`CaMeLValue` instances from the interpreter's variable
store) have no path into any of these parameters.

#### 7.2 Type-system enforcement

`user_query` and `context` values are typed as `str`, not `CaMeLValue`.
Passing a `CaMeLValue` (which is a dataclass, not a `str`) is a static type
error caught by mypy in strict mode.

#### 7.3 Runtime guard (defense in depth)

`PLLMWrapper._build_messages` performs a runtime `isinstance` check on
`user_query` and each `context` value:

```python
if isinstance(user_query, CaMeLValue):
    raise PLLMIsolationError(
        "user_query must be a plain str; got CaMeLValue — "
        "tool return values must not reach the P-LLM."
    )
```

This guards against callers that bypass the type checker (e.g. `# type: ignore`
annotations or dynamic dispatch).

---

### 8. New Exceptions

```python
class PLLMError(Exception):
    """Base class for P-LLM wrapper errors."""

class CodeBlockNotFoundError(PLLMError):
    """Raised when no fenced code block is found in a P-LLM response.

    Attributes
    ----------
    response:
        The raw P-LLM response text.  Stored for debugging; never forwarded
        to the model as-is (redaction rules apply — § 6.2).
    """
    response: str

class PLLMRetryExhaustedError(PLLMError):
    """Raised when all retry attempts fail to produce a valid plan.

    Attributes
    ----------
    attempts:
        The number of attempts made before giving up.
    """
    attempts: int

class PLLMIsolationError(PLLMError):
    """Raised when a caller attempts to pass a CaMeLValue into the P-LLM.

    This is a programming error — tool return values must never reach the
    P-LLM prompt.
    """
```

---

### 9. `ToolSignature` Dataclass

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ToolSignature:
    """Human-readable description of a CaMeL tool for P-LLM prompt injection.

    Attributes
    ----------
    name:
        The Python function name the P-LLM will use in generated code.
    signature:
        Parameter list string, e.g. ``"recipient: str, subject: str, body: str"``.
        Uses Python type-annotation syntax so the P-LLM can generate correctly
        typed calls.
    return_type:
        Nominal return type name shown to the P-LLM.  Intentionally a plain
        string (not a schema) — the P-LLM must treat returned objects as
        opaque per the M2-F13 instruction.
    description:
        One-sentence description of what the tool does.  Shown verbatim in the
        system prompt's tool-signatures section.
    """

    name: str
    signature: str
    return_type: str
    description: str
```

---

### 10. Module Layout

```
camel/llm/
    protocols.py        — LLMBackend, QlLMBackend (existing)
    schemas.py          — QResponse (existing)
    exceptions.py       — NotEnoughInformationError (existing)
    qllm.py             — QLLMWrapper (existing)
    pllm.py             — PLLMWrapper, CodeBlockParser, ToolSignature,
                          PLLMError, CodeBlockNotFoundError,
                          PLLMRetryExhaustedError, PLLMIsolationError  ← NEW
    adapters/
        __init__.py     (existing)
        claude.py       — ClaudeBackend (existing, satisfies both protocols)
        gemini.py       — GeminiBackend (existing, satisfies both protocols)
```

All new symbols live in `camel/llm/pllm.py` and are re-exported from
`camel/llm/__init__.py`.

---

## Consequences

### Positive

- **Provable isolation at the type level.** `generate_plan` accepts only
  `str` parameters for the content it places in the prompt; a `CaMeLValue`
  cannot accidentally reach the P-LLM without a deliberate type bypass.
- **Provider-agnostic.** `PLLMWrapper` is decoupled from every concrete
  backend via the `LLMBackend` Protocol.  Adding a new provider (e.g.
  OpenAI, Mistral) requires only a new adapter satisfying the protocol.
- **Retry loop is auditable.** Each retry appends to the message list, so the
  full conversation is available for post-hoc review.  The security audit log
  (NFR-6) can record every attempt and its redacted error.
- **System prompt is structured and testable.** Each section is assembled by
  a distinct helper method; unit tests can assert on the presence and absence
  of specific content in the generated prompt without spinning up a real LLM.
- **M2-F13 and M2-F10 are enforced at the prompt layer**, complementing the
  architectural isolation enforced by the interpreter's variable store.

### Negative / Trade-offs

- **`print()` dual-use tension.** M2-F10 instructs the P-LLM to use `print()`
  for user-visible output, but `print` is not a default interpreter builtin
  (ADR-003 §4.5).  Callers that want P-LLM output to reach the user must
  explicitly register a policy-aware `print` tool in the interpreter.  This
  is intentional (the policy engine guards what gets printed) but requires
  operator awareness.
- **Opaque-variable instruction is advisory.** M2-F13 is a prompt instruction;
  it does not prevent a model from generating code that inspects tool return
  values.  The interpreter's execution model provides the hard guarantee (it
  never exposes raw values to the P-LLM); M2-F13 only reduces unnecessary
  retry rate.
- **Token overhead.** The system prompt template (§ 5) adds ~400–600 tokens
  per call.  This is within the ≤3× token overhead target (NFR-3) but should
  be monitored as the tool registry grows.

---

## Alternatives Considered

### A. Pass tool return values to P-LLM for richer re-planning

Allow the P-LLM to see sanitised summaries of tool outputs so it can make
conditional decisions based on observed data.

**Rejected.**  This is the classic Dual-LLM weakness identified in the CaMeL
paper: data-flow attacks can still manipulate arguments to legitimate tool
calls even when control-flow attacks are blocked.  Exposing any tool return
value to the P-LLM — even "sanitised" — reintroduces the data injection
vector.  The `get_q_llm_result()` pattern (Q-LLM extraction into structured
schema, tagged as untrusted `CaMeLValue`) is the correct mechanism for the
P-LLM to obtain structured facts from tool outputs without direct exposure.

### B. Single wrapper class handling both P-LLM and Q-LLM paths

Merge `PLLMWrapper` and `QLLMWrapper` into one class that switches behaviour
based on a constructor flag.

**Rejected.**  The P-LLM and Q-LLM have fundamentally different isolation
requirements (free-form text vs. schema-only, tool-calling vs. no tools).
A merged class would require a `mode` parameter that callers could
accidentally set incorrectly, eliminating compile-time safety.  Separate
wrapper classes with non-overlapping APIs are safer and more testable.

### C. Validate interpreter compatibility inside the wrapper

Run the extracted code through the full `CaMeLInterpreter` inside
`PLLMWrapper.generate_plan` to validate that it uses only supported
constructs before returning.

**Rejected at this layer.**  The wrapper is responsible for prompt
construction and code extraction, not execution.  Interpreter validation
requires a live tool registry and policy engine, which the wrapper does not
own.  A lightweight `ast.parse` syntax check (detecting gross syntax errors)
is the correct division of responsibility; interpreter-level validation
(UnsupportedSyntaxError, PolicyViolationError) is handled by the caller.

### D. Structured output (JSON) for plan generation

Have the P-LLM return JSON with a `code` field rather than a
Markdown-fenced code block.

**Rejected.**  Structured output is valuable for the Q-LLM (which must
return schema-validated data) but is unnecessary complexity for the P-LLM.
The P-LLM generates code — a string — and Markdown fencing is the most
natural, model-familiar format for code generation.  Requiring JSON would add
escaping overhead, reduce code readability in traces, and increase the risk of
the model producing malformed JSON rather than syntactically correct Python.

---

## References

- `camel/llm/protocols.py` — `LLMBackend` Protocol (P-LLM interface)
- `camel/llm/adapters/claude.py` — `ClaudeBackend` (concrete adapter)
- `camel/llm/adapters/gemini.py` — `GeminiBackend` (concrete adapter)
- `camel/llm/pllm.py` — `PLLMWrapper`, `CodeBlockParser`, `ToolSignature` (to be implemented)
- `docs/adr/001-q-llm-isolation-contract.md` — Q-LLM isolation (complementary)
- `docs/adr/003-ast-interpreter-architecture.md` — interpreter grammar and `print` builtin exclusion
- CaMeL paper §6.1 (P-LLM), §7.1 (PI-SEC security game), §7.2 (Trusted Boundary)
- PRD §6.1 (P-LLM), §G1–G4 (Goals), NFR-3 (token overhead), NFR-5 (retry loop)
