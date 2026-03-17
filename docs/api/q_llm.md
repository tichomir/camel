# API Reference — Q-LLM Wrapper (`camel.llm.qllm`, `camel.llm.schemas`, `camel.llm.exceptions`)

The Q-LLM (Quarantined LLM) subsystem performs **schema-validated structured
extraction** from untrusted content.  It has no tool-calling capability and
cannot return free-form text to the P-LLM.

---

## Isolation Guarantee

- `QLLMWrapper` accepts no tool signatures and forwards no `tools` / `functions`
  parameters to the backend.
- Every response is validated against the caller-supplied Pydantic schema before
  being returned.
- When the model lacks sufficient context it sets `have_enough_information=False`
  and `NotEnoughInformationError` is raised — the partial response never reaches
  the P-LLM.

---

## `QResponse` (base schema)

```python
class QResponse(BaseModel):
    have_enough_information: bool
```

**Module:** `camel.llm.schemas`

Base class for all Q-LLM response schemas.  Every schema returned by a Q-LLM
backend must inherit from `QResponse`.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `have_enough_information` | `bool` | `True` when the model had sufficient context to populate all other fields meaningfully. `False` signals that the caller must raise `NotEnoughInformationError`. |

**Model config**

| Option | Value | Reason |
|---|---|---|
| `extra` | `"forbid"` | Prevents extra fields from smuggling free-form text. |
| `frozen` | `True` | Prevents callers from mutating untrusted data in place. |

**Usage — defining a domain schema**

```python
from camel.llm.schemas import QResponse

class EmailExtraction(QResponse):
    sender: str
    subject: str
    body_summary: str
```

---

## `QLLMWrapper`

```python
class QLLMWrapper:
    def __init__(self, backend: QlLMBackend) -> None:
```

**Module:** `camel.llm.qllm`

Wraps a `QlLMBackend` for safe structured extraction from untrusted content.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `backend` | `QlLMBackend` | Any object satisfying the `QlLMBackend` structural protocol (e.g. `ClaudeBackend`, `GeminiBackend`). |

---

### `QLLMWrapper.extract`

```python
async def extract(self, data: str, schema: type[T]) -> T:
```

Extract structured data from `data` conforming to `schema`.

The method:

1. Constructs a prompt with isolation rules and the schema description.
2. Wraps `data` in delimiters to prevent prompt injection from leaking into
   the instruction space.
3. Calls the backend with `structured_complete`.
4. Validates the backend's return value against the caller-supplied schema.
5. Raises `NotEnoughInformationError` if `have_enough_information` is `False`.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `data` | `str` | Raw, untrusted string content to extract information from. Any instructions embedded within it are ignored. |
| `schema` | `type[T]` | A `QResponse` subclass defining the expected output shape. |

**Returns** `T` — a validated instance of `schema` with `have_enough_information=True`.

**Raises**

| Exception | When |
|---|---|
| `NotEnoughInformationError` | The model sets `have_enough_information=False`, indicating insufficient content. |
| `pydantic.ValidationError` | The backend returns data that does not conform to `schema`. |

---

## `make_qllm_wrapper`

```python
def make_qllm_wrapper(backend: QlLMBackend) -> QLLMWrapper:
```

**Module:** `camel.llm.qllm`

Factory function — create a `QLLMWrapper` from a backend.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `backend` | `QlLMBackend` | Any object satisfying the `QlLMBackend` structural protocol. |

**Returns** `QLLMWrapper` — configured wrapper ready to call `extract`.

---

## `NotEnoughInformationError`

```python
class NotEnoughInformationError(Exception):
    schema_type: type[QResponse]
    partial_response: QResponse | None
```

**Module:** `camel.llm.exceptions`

Raised when a Q-LLM backend returns a response whose `have_enough_information`
field is `False`.

The Q-LLM is not allowed to produce free-form text or refuse via a natural-language
message.  When the model lacks sufficient context it sets `have_enough_information=False`
and the caller **must** raise this exception rather than forwarding the partial
response to the P-LLM.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `schema_type` | `type[QResponse]` | The `QResponse` subclass that was requested. |
| `partial_response` | `QResponse \| None` | Raw (untrusted) model output that triggered the error. Must not be used in trusted computation. |

**Security note:** The exception message interpolates only the developer-declared
class name — no untrusted field values or LLM output may appear in the message string.

---

## `have_enough_information` Field — Security Semantics

The `have_enough_information` field is the **only** mechanism the Q-LLM has to
signal uncertainty.  The field must be the last semantic decision the model makes:

- **`True`**: all other fields are populated with meaningful values.
- **`False`**: all other fields must be treated as meaningless placeholders;
  `NotEnoughInformationError` must be raised; no field value may reach the P-LLM.

This design prevents the Q-LLM from using a natural-language refusal as a
side channel for injected content.

---

## Full Usage Example

```python
import asyncio
from camel.llm.adapters import ClaudeBackend
from camel.llm.qllm import make_qllm_wrapper
from camel.llm.schemas import QResponse
from camel.llm.exceptions import NotEnoughInformationError

class EmailExtraction(QResponse):
    sender: str
    subject: str
    body_summary: str

backend = ClaudeBackend(model="claude-opus-4-6")
wrapper = make_qllm_wrapper(backend)

raw_email = """
From: alice@example.com
Subject: Project update
Body: We finished the migration last night. All tests pass.
"""

async def main() -> None:
    try:
        result: EmailExtraction = await wrapper.extract(raw_email, EmailExtraction)
        print(result.sender)        # "alice@example.com"
        print(result.subject)       # "Project update"
        print(result.body_summary)  # "Migration completed; all tests pass."
    except NotEnoughInformationError as e:
        print(f"Q-LLM could not extract from {e.schema_type.__name__!r}")

asyncio.run(main())
```

---

*See also: [LLM Backend API](llm_backend.md) · [P-LLM Wrapper API](p_llm.md) · [Execution Loop API](execution_loop.md)*
