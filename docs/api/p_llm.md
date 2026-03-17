# API Reference — P-LLM Wrapper (`camel.llm.p_llm`)

The `camel.llm.p_llm` module implements the **Privileged LLM (P-LLM)** planning
path.  `PLLMWrapper` builds a structured system prompt, generates a
restricted-Python execution plan from the user query, and enforces the
isolation invariant that **tool return values never reach the P-LLM**.

---

## Isolation Contract

`PLLMWrapper` enforces the P-LLM isolation invariant at three levels:

| Level | Mechanism |
|---|---|
| **Architectural** | `generate_plan` accepts only `str` for content sent to the model; `CaMeLValue` has no parameter path. |
| **Type-system** | `user_query` and `user_context` values are typed as `str`; passing a `CaMeLValue` is a mypy strict error. |
| **Runtime** | `_guard_no_camel_values` raises `PLLMIsolationError` if the type checker is bypassed (e.g. via `# type: ignore`). |

---

## `PLLMWrapper`

```python
class PLLMWrapper:
    def __init__(
        self,
        backend: LLMBackend,
        max_retries: int = 10,
    ) -> None:
```

Privileged LLM wrapper for CaMeL execution plan generation.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `backend` | `LLMBackend` | — | Any object satisfying the `LLMBackend` protocol (e.g. `ClaudeBackend`, `GeminiBackend`). |
| `max_retries` | `int` | `10` | Maximum code-generation attempts before raising `PLLMRetryExhaustedError` (PRD §6.1). |

---

### `PLLMWrapper.generate_plan`

```python
async def generate_plan(
    self,
    user_query: str,
    tool_signatures: list[ToolSignature],
    user_context: UserContext | None = None,
) -> CodePlan:
```

Generate a CaMeL pseudo-Python execution plan for `user_query`.

Builds the system prompt, sends `user_query` as the human turn, and parses
the fenced code block from the P-LLM response.  Retries up to `max_retries`
times on `CodeBlockNotFoundError` or `SyntaxError`.  Each retry includes
only the **error type** and **line number** — the message text is omitted as
it may echo attacker-controlled content.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `user_query` | `str` | Trusted user request. Tool return values must not appear here. |
| `tool_signatures` | `list[ToolSignature]` | Available tools injected into the system prompt's tool-signatures section. |
| `user_context` | `UserContext \| None` | Optional trusted key-value metadata (e.g. date, session ID). Must come from trusted platform sources. |

**Returns** `CodePlan` — extracted, syntactically valid source string.

**Raises**

| Exception | When |
|---|---|
| `PLLMIsolationError` | `user_query` or any `user_context` value is a `CaMeLValue` instance. |
| `PLLMRetryExhaustedError` | All `max_retries` attempts fail to produce a parseable, syntactically valid code block. |
| `LLMBackendError` | Any backend API failure; propagated immediately without retrying. |

---

### `PLLMWrapper.build_system_prompt`

```python
def build_system_prompt(
    self,
    tool_signatures: list[ToolSignature],
    user_context: UserContext | None = None,
) -> str:
```

Assemble the five-section P-LLM system prompt.

Sections (in order):

1. CaMeL Python subset specification.
2. Opaque-variable instruction (M2-F13) — tool return values are opaque handles.
3. Tool signatures — Python function stubs with docstrings.
4. User context — trusted key-value metadata.
5. `print()` usage guidance (M2-F10).

An output-format instruction section is appended after the five required sections.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `tool_signatures` | `list[ToolSignature]` | Tools available in this session. |
| `user_context` | `UserContext \| None` | Optional trusted metadata. Must not contain tool return values. |

**Returns** `str` — fully assembled system prompt string.

---

### `PLLMWrapper.parse_code_plan`

```python
def parse_code_plan(self, response: str) -> CodePlan:
```

Extract a `CodePlan` from a raw P-LLM response string.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `response` | `str` | Raw text returned by the P-LLM backend. |

**Returns** `CodePlan` — wraps the extracted source string.

**Raises**

| Exception | When |
|---|---|
| `CodeBlockNotFoundError` | No fenced code block found or the block is empty. |

---

## `ToolSignature`

```python
@dataclass(frozen=True)
class ToolSignature:
    name: str
    signature: str
    return_type: str
    description: str
```

Human-readable description of a CaMeL tool for P-LLM prompt injection.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `name` | `str` | Python function name the P-LLM will use in generated code. |
| `signature` | `str` | Parameter list string, e.g. `"recipient: str, subject: str"`. Pass `""` for zero-parameter tools. |
| `return_type` | `str` | Nominal return type name. Intentionally a plain string — the P-LLM must treat returned objects as opaque. |
| `description` | `str` | One-sentence description shown verbatim in the system prompt. |

---

## `CodePlan`

```python
@dataclass(frozen=True)
class CodePlan:
    source: str
```

A validated CaMeL pseudo-Python execution plan extracted from a P-LLM response.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `source` | `str` | Extracted Python source code (without Markdown fence markers), passed to the CaMeL interpreter for execution. |

---

## `CodeBlockParser`

```python
class CodeBlockParser:
    @classmethod
    def extract(cls, response: str) -> str:
```

Extract the content of the first ` ```python … ``` ` block in `response`.

Accepts both ` ```python ` and bare ` ``` ` fences.  Only the **first** block
is extracted.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `response` | `str` | Raw text returned by the P-LLM backend. |

**Returns** `str` — code content (without fence markers), stripped of leading/trailing whitespace.

**Raises**

| Exception | When |
|---|---|
| `CodeBlockNotFoundError` | No fenced code block found or the extracted block is empty. |

---

## Type Alias

```python
UserContext = Mapping[str, str]
```

Trusted key-value metadata injected into the user-context section of the
system prompt.  Values **must** come from trusted platform sources — never
from tool return values.

---

## Exceptions

### `PLLMError`

```python
class PLLMError(Exception):
```

Base class for all P-LLM wrapper errors.

---

### `CodeBlockNotFoundError`

```python
class CodeBlockNotFoundError(PLLMError):
    response: str
```

Raised when no fenced code block is found in a P-LLM response.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `response` | `str` | Raw P-LLM response text. Stored for debugging; never forwarded to the model as-is. |

---

### `PLLMRetryExhaustedError`

```python
class PLLMRetryExhaustedError(PLLMError):
    attempts: int
```

Raised when all retry attempts fail to produce a valid execution plan.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `attempts` | `int` | Number of generation attempts made before giving up. |

---

### `PLLMIsolationError`

```python
class PLLMIsolationError(PLLMError):
```

Raised when a caller attempts to pass a `CaMeLValue` into the P-LLM.
This is a programming error — tool return values must never reach the P-LLM prompt.

---

## Usage Example

```python
import asyncio
from camel.llm.backend import get_backend
from camel.llm.p_llm import PLLMWrapper, ToolSignature

backend = get_backend("claude", api_key="sk-...")
wrapper = PLLMWrapper(backend)

tools = [
    ToolSignature(
        name="get_email",
        signature="",
        return_type="EmailMessage",
        description="Retrieve the most recent email from the inbox.",
    ),
    ToolSignature(
        name="send_email",
        signature="recipient: str, subject: str, body: str",
        return_type="None",
        description="Send an email to recipient.",
    ),
]

async def main() -> None:
    plan = await wrapper.generate_plan(
        user_query="Forward the last email to alice@example.com",
        tool_signatures=tools,
        user_context={"user_email": "bob@example.com"},
    )
    print(plan.source)

asyncio.run(main())
```

---

*See also: [LLM Backend API](llm_backend.md) · [Q-LLM Wrapper API](q_llm.md) · [Execution Loop API](execution_loop.md)*
