# API Reference — LLM Backend (`camel.llm.backend`)

The `camel.llm.backend` module defines the provider-agnostic interface all LLM
backends must satisfy, the unified error type both adapters raise, and a
factory function for constructing adapters by provider name.

---

## `LLMBackend` Protocol

```python
class LLMBackend(Protocol):
    ...
```

Structural interface for LLM backends used by the P-LLM wrapper.

Implementations must provide two async methods:

- `generate` — free-form text completion (P-LLM planning path).
- `generate_structured` — Pydantic-schema-constrained output (Q-LLM path).

The protocol is `runtime_checkable`, so test doubles can be verified with
`isinstance`.

**Isolation contract:** Callers must not pass `CaMeLValue` instances (tool
return values) into the `messages` list.  The P-LLM wrapper enforces this
via a runtime guard in `PLLMWrapper._build_messages`.

---

### `LLMBackend.generate`

```python
async def generate(
    self,
    messages: list[Message],
    **kwargs: Any,
) -> str:
```

Return a free-form completion string for `messages`.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `messages` | `list[Message]` | Ordered list of chat messages (`"role"` + `"content"` dicts). |
| `**kwargs` | `Any` | Backend-specific options (e.g. `temperature`, `max_tokens`). |

**Returns** `str` — the model's raw text response.

**Raises**

| Exception | When |
|---|---|
| `LLMBackendError` | Any API-level failure (network error, rate limit, auth failure). |

---

### `LLMBackend.generate_structured`

```python
async def generate_structured(
    self,
    messages: list[Message],
    schema: type[BaseModel],
) -> BaseModel:
```

Return a Pydantic-validated structured response conforming to `schema`.

Implementations must request structured/JSON output from the underlying
provider.  They must **not** pass tool definitions to the API call for this
path.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `messages` | `list[Message]` | Ordered list of chat messages providing the content to parse. |
| `schema` | `type[BaseModel]` | A `pydantic.BaseModel` subclass describing the expected output shape. |

**Returns** `BaseModel` — a validated instance of `schema`.

**Raises**

| Exception | When |
|---|---|
| `LLMBackendError` | Any API-level failure. |

---

## `LLMBackendError`

```python
class LLMBackendError(Exception):
    cause: BaseException | None
```

Raised when an LLM backend API call fails.  Both `ClaudeBackend` and
`GeminiBackend` wrap their native SDK exceptions in this class so callers
handle a single unified exception type.

**Attributes**

| Name | Type | Description |
|---|---|---|
| `cause` | `BaseException \| None` | The original SDK exception, or `None` for adapter-internal errors. |

---

## `get_backend`

```python
def get_backend(provider: str, **kwargs: Any) -> LLMBackend:
```

Factory — create and return an `LLMBackend` for the given `provider`.

The factory performs a lazy import of the adapter module so SDK packages
(`anthropic`, `google-generativeai`) are only required when the
corresponding backend is actually used.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `provider` | `str` | Provider identifier: `"claude"` or `"gemini"`. |
| `**kwargs` | `Any` | Constructor arguments forwarded to the adapter (e.g. `api_key`, `model`). |

**Returns** `LLMBackend` — a concrete adapter instance.

**Raises**

| Exception | When |
|---|---|
| `ValueError` | `provider` is not a recognised string. |
| `ImportError` | The required SDK package for `provider` is not installed. |

**Usage example**

```python
from camel.llm.backend import get_backend

# Claude backend
backend = get_backend("claude", api_key="sk-...", model="claude-opus-4-6")

# Gemini backend
backend = get_backend("gemini", api_key="AI...", model="gemini-2.0-flash")

# Generating a plan (free-form)
response = await backend.generate([
    {"role": "system", "content": "You are a planning assistant."},
    {"role": "user", "content": "Summarise my inbox."},
])
```

---

## Concrete Adapters

Both adapters satisfy `LLMBackend` via structural subtyping (no inheritance).

| Class | Module | Provider SDK |
|---|---|---|
| `ClaudeBackend` | `camel.llm.adapters.claude` | `anthropic` |
| `GeminiBackend` | `camel.llm.adapters.gemini` | `google-generativeai` |

Import:

```python
from camel.llm.adapters import ClaudeBackend, GeminiBackend
# or via factory:
from camel.llm.backend import get_backend
backend = get_backend("claude", api_key="sk-...")
```

---

*See also: [P-LLM Wrapper API](p_llm.md) · [Q-LLM Wrapper API](q_llm.md)*
