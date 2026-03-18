# API Reference — LLM Backend (`camel.llm.backend`)

The `camel.llm.backend` module defines the provider-agnostic interface all LLM
backends must satisfy, the unified error type all adapters raise, and a
factory function for constructing adapters by provider name.

---

## `LLMBackend` Protocol

```python
class LLMBackend(Protocol):
    ...
```

Structural interface for LLM backends used by the P-LLM wrapper.

Implementations must provide:

- `generate` — free-form text completion (P-LLM planning path).
- `generate_structured` — Pydantic-schema-constrained output (Q-LLM path).
- `get_backend_id` — stable provider identifier string.
- `supports_structured_output` — capability advertisement flag.

The protocol is `runtime_checkable`, so test doubles can be verified with
`isinstance`.

**Isolation contract:** Callers must not pass `CaMeLValue` instances (tool
return values) into the `messages` list.  The P-LLM wrapper enforces this
via a runtime guard in `PLLMWrapper._build_messages`.

**Multi-backend scope (NFR-8):** Validated for Claude (Anthropic),
Gemini 2.5 Pro/Flash (Google), and GPT-4.1/o3/o4-mini (OpenAI).

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

### `LLMBackend.get_backend_id`

```python
def get_backend_id(self) -> str:
```

Return a stable, credential-free identifier string for this backend.

Used in audit log entries and Prometheus/OpenTelemetry metrics labels
(e.g. `camel_pllm_retry_count_histogram{backend="claude:claude-opus-4-6"}`).

**Returns** `str` — format convention: `"<provider>:<model>"`.

**Examples**

| Backend | Return value |
|---|---|
| `ClaudeBackend(model="claude-opus-4-6")` | `"claude:claude-opus-4-6"` |
| `GeminiBackend(model="gemini-2.5-pro")` | `"gemini:gemini-2.5-pro"` |
| `OpenAIBackend(model="gpt-4.1")` | `"openai:gpt-4.1"` |

---

### `LLMBackend.supports_structured_output`

```python
def supports_structured_output(self) -> bool:
```

Return `True` if this backend natively supports schema-constrained output.

Backends returning `True` use a provider-native mechanism rather than
prompt engineering alone:

| Backend | Mechanism | Returns |
|---|---|---|
| `ClaudeBackend` | Synthetic extraction tool (`tool_choice` forced) | `True` |
| `GeminiBackend` | `response_mime_type="application/json"` + `response_schema` | `True` |
| `OpenAIBackend` (GPT-4.1/4o) | `response_format` with `json_schema` | `True` |
| `OpenAIBackend` (o3/o4-mini) | Prompt-based JSON extraction | `False` |

---

## `LLMBackendError`

```python
class LLMBackendError(Exception):
    cause: BaseException | None
```

Raised when an LLM backend API call fails.  All adapters (`ClaudeBackend`,
`GeminiBackend`, `OpenAIBackend`) wrap their native SDK exceptions in this
class so callers handle a single unified exception type.

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
(`anthropic`, `google-generativeai`, `openai`) are only required when the
corresponding backend is actually used.

**Parameters**

| Name | Type | Description |
|---|---|---|
| `provider` | `str` | Provider identifier: `"claude"`, `"gemini"`, or `"openai"`. |
| `**kwargs` | `Any` | Constructor arguments forwarded to the adapter (e.g. `api_key`, `model`). |

**Returns** `LLMBackend` — a concrete adapter instance.

**Raises**

| Exception | When |
|---|---|
| `ValueError` | `provider` is not a recognised string. |
| `ImportError` | The required SDK package for `provider` is not installed. |

**Usage examples**

```python
from camel.llm.backend import get_backend

# Claude (Anthropic)
backend = get_backend("claude", api_key="sk-...", model="claude-opus-4-6")

# Gemini (Google)
backend = get_backend("gemini", api_key="AI...", model="gemini-2.5-flash")

# OpenAI GPT-4.1
backend = get_backend("openai", api_key="sk-...", model="gpt-4.1")

# OpenAI reasoning model (o3/o4-mini)
backend = get_backend("openai", api_key="sk-...", model="o4-mini")

# Independent P-LLM / Q-LLM backends
p_llm = get_backend("claude", model="claude-opus-4-6")
q_llm = get_backend("claude", model="claude-haiku-4-5-20251001")

# Mixed-provider
p_llm = get_backend("claude", model="claude-opus-4-6")
q_llm = get_backend("gemini", model="gemini-2.5-flash")
```

---

## Concrete Adapters

All adapters satisfy `LLMBackend` via structural subtyping (no inheritance).

| Class | Module | Provider SDK | Validated Models |
|---|---|---|---|
| `ClaudeBackend` | `camel.llm.adapters.claude` | `anthropic>=0.25` | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` |
| `GeminiBackend` | `camel.llm.adapters.gemini` | `google-generativeai>=0.7` | `gemini-2.5-pro`, `gemini-2.5-flash` |
| `OpenAIBackend` | `camel.llm.adapters.openai` | `openai>=1.30` | `gpt-4.1`, `o3`, `o4-mini` |

Import:

```python
from camel.llm.adapters import ClaudeBackend, GeminiBackend, OpenAIBackend
# or via factory:
from camel.llm.backend import get_backend
backend = get_backend("openai", api_key="sk-...")
```

---

*See also: [P-LLM Wrapper API](p_llm.md) · [Q-LLM Wrapper API](q_llm.md) ·
[Backend Adapter Developer Guide](../backend-adapter-developer-guide.md)*
