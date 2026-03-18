# Backend Adapter Developer Guide

**Version:** 1.0 (Milestone 6 — Multi-Backend LLM Support)
**Date:** 2026-03-18

This guide documents the `LLMBackend` interface contract and explains how to
implement a new provider adapter for the CaMeL security framework.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture: LLMBackend Abstraction Layer](#2-architecture-llmbackend-abstraction-layer)
3. [Interface Contract](#3-interface-contract)
4. [Error Semantics](#4-error-semantics)
5. [P-LLM vs Q-LLM Role Configuration](#5-p-llm-vs-q-llm-role-configuration)
6. [Credential Injection](#6-credential-injection)
7. [Retry Expectations](#7-retry-expectations)
8. [Real Adapter Examples](#8-real-adapter-examples)
9. [Skeleton Adapter Example](#9-skeleton-adapter-example)
10. [Registering a New Adapter](#10-registering-a-new-adapter)
11. [Validated Backends](#11-validated-backends)
12. [Security Constraints](#12-security-constraints)

---

## 1. Overview

CaMeL decouples its security architecture from any specific LLM provider by
routing all model calls through the `LLMBackend` protocol.  This allows the
same security guarantees — capability tracking, policy enforcement, exception
redaction — to hold regardless of whether Claude, Gemini, GPT-4.1, or a
future model powers the agent.

Two structural protocols govern backend behaviour:

| Protocol | Module | Role |
|---|---|---|
| `LLMBackend` (backend.py) | `camel.llm.backend` | Unified interface: `generate`, `generate_structured`, `get_backend_id`, `supports_structured_output` |
| `LLMBackend` (protocols.py) | `camel.llm.protocols` | P-LLM path: `complete`, `get_backend_id`, `supports_structured_output` |
| `QlLMBackend` (protocols.py) | `camel.llm.protocols` | Q-LLM path: `structured_complete` |

Production adapters satisfy **all three** protocols via structural subtyping
(no inheritance required).

---

## 2. Architecture: LLMBackend Abstraction Layer

```
┌────────────────────────────────────────────────────────────────┐
│                        CaMeL Runtime                           │
│                                                                │
│   ┌──────────────┐          ┌──────────────────────────────┐   │
│   │  PLLMWrapper  │          │       CaMeLOrchestrator      │   │
│   │  (planning)   │          │  (execution loop + retry)   │   │
│   └──────┬───────┘          └──────────────┬───────────────┘   │
│          │ complete()                       │                   │
│          │                         structured_complete()        │
│          ▼                                  ▼                   │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │               LLMBackend Protocol Layer                  │  │
│   │   generate()  generate_structured()  get_backend_id()   │  │
│   │   supports_structured_output()                          │  │
│   └──────────┬──────────────┬───────────────────────────────┘  │
│              │              │              │                    │
└──────────────┼──────────────┼──────────────┼────────────────────┘
               │              │              │
               ▼              ▼              ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │ ClaudeBackend│ │GeminiBackend │ │OpenAIBackend │
    │  (Anthropic) │ │  (Google)    │ │  (OpenAI)    │
    │  anthropic   │ │google-genera-│ │  openai SDK  │
    │   SDK        │ │tiveai SDK    │ │              │
    └──────────────┘ └──────────────┘ └──────────────┘
               │              │              │
               ▼              ▼              ▼
         Anthropic API    Google AI API   OpenAI API
```

The protocol layer is the only surface the CaMeL runtime calls.  Adding a
new provider requires only implementing this surface — no changes to the
interpreter, policy engine, or audit log subsystems.

---

## 3. Interface Contract

### 3.1 Required Methods

All adapters **must** implement the following methods.  Structural subtyping
is used — inheritance from a base class is not required, but method
signatures must match exactly.

#### `complete(messages, **kwargs) -> str`

**Path:** P-LLM (planning)

```python
async def complete(
    self,
    messages: list[Message],
    **kwargs: Any,
) -> str:
    ...
```

- Returns the model's raw text response for the given conversation.
- `messages` is a list of `{"role": str, "content": str}` dicts.
  Supported roles: `"system"`, `"user"`, `"assistant"`.
- `**kwargs` may carry provider-specific options (`temperature`, `max_tokens`,
  etc.).  Adapters MUST forward unknown kwargs rather than raising.
- **MUST NOT** accept a `tools` parameter on the P-LLM path.  Tool calling
  is managed by the CaMeL interpreter, not the model.
- Raises `LLMBackendError` on any provider API failure.

#### `structured_complete(messages, schema) -> QResponseT`

**Path:** Q-LLM (structured extraction)

```python
async def structured_complete(
    self,
    messages: list[Message],
    schema: type[QResponseT],
) -> QResponseT:
    ...
```

- Returns a validated `QResponse` subclass instance.
- `schema` is a `QResponse` subclass with `have_enough_information: bool`
  auto-injected.
- **MUST NOT** pass tool definitions to the underlying API call.
- The returned object is tagged as untrusted by the caller; the adapter
  itself does not touch capability metadata.
- Raises `LLMBackendError` on provider failure.

#### `generate(messages, **kwargs) -> str`

**Path:** Unified P-LLM (used by `get_backend`)

Delegates to `complete` and wraps provider exceptions as `LLMBackendError`.

#### `generate_structured(messages, schema) -> BaseModel`

**Path:** Unified structured output (used by `get_backend`)

Accepts any `BaseModel` subclass.  Delegates to structured output logic and
wraps provider exceptions as `LLMBackendError`.

#### `get_backend_id() -> str`

```python
def get_backend_id(self) -> str:
    ...
```

- Returns a **stable, synchronous** string identifier.
- Format convention: `"<provider>:<model>"`, e.g. `"claude:claude-opus-4-6"`.
- **MUST NOT** contain API keys, credentials, or session-specific data.
- Used in audit log entries, metrics labels (e.g. `camel_pllm_retry_count`),
  and integration test assertions.

#### `supports_structured_output() -> bool`

```python
def supports_structured_output(self) -> bool:
    ...
```

- Returns `True` if the adapter uses a **provider-native** mechanism to
  constrain output to the declared schema (e.g. `tool_choice` for Anthropic,
  `response_mime_type` for Gemini, `response_format` for OpenAI).
- Returns `False` if the adapter relies on prompt engineering alone (e.g.
  reasoning models that do not support `response_format`).
- The CaMeL Q-LLM wrapper uses this flag to select the appropriate
  validation path and to emit a structured-output capability audit event.

### 3.2 Method Summary Table

| Method | Sync/Async | Returns | Protocol |
|---|---|---|---|
| `complete(messages, **kwargs)` | `async` | `str` | `LLMBackend` (protocols), P-LLM |
| `structured_complete(messages, schema)` | `async` | `QResponseT` | `QlLMBackend` (protocols), Q-LLM |
| `generate(messages, **kwargs)` | `async` | `str` | `LLMBackend` (backend), unified |
| `generate_structured(messages, schema)` | `async` | `BaseModel` | `LLMBackend` (backend), unified |
| `get_backend_id()` | sync | `str` | both `LLMBackend` variants |
| `supports_structured_output()` | sync | `bool` | both `LLMBackend` variants |

---

## 4. Error Semantics

All provider-specific exceptions **must** be caught and re-raised as
`LLMBackendError` from `camel.llm.backend`:

```python
from camel.llm.backend import LLMBackendError

try:
    response = await provider_client.call(...)
except SomeProviderError as exc:
    raise LLMBackendError(str(exc), cause=exc) from exc
```

**`LLMBackendError` attributes:**

| Attribute | Type | Description |
|---|---|---|
| `args[0]` | `str` | Human-readable error message. |
| `cause` | `BaseException \| None` | Original provider SDK exception. |

The CaMeL retry loop catches `LLMBackendError` and applies exponential
backoff for up to 10 attempts before raising `MaxRetriesExceededError`.
Adapters **must not** implement their own retry logic — this is handled at
the orchestrator level.

---

## 5. P-LLM vs Q-LLM Role Configuration

CaMeL supports independent P-LLM and Q-LLM backend configuration.  A
typical deployment uses a larger model for planning (P-LLM) and a smaller,
cheaper model for structured extraction (Q-LLM):

```python
from camel.llm.backend import get_backend
from camel_security import CaMeLAgent

p_llm_backend = get_backend("claude", model="claude-opus-4-6")
q_llm_backend = get_backend("claude", model="claude-haiku-4-5-20251001")

agent = CaMeLAgent(p_llm=p_llm_backend, q_llm=q_llm_backend, ...)
```

You may also mix providers — e.g. Claude for planning and a smaller Gemini
Flash model for Q-LLM extraction:

```python
p_llm_backend = get_backend("claude", model="claude-opus-4-6")
q_llm_backend = get_backend("gemini", model="gemini-2.5-flash")
```

The P-LLM wrapper calls `complete()` / `generate()`.
The Q-LLM wrapper calls `structured_complete()` / `generate_structured()`.
Both are wired independently; the same adapter class may serve both roles or
different classes may be used.

---

## 6. Credential Injection

Adapters receive credentials via constructor kwargs.  The `get_backend`
factory forwards all `**kwargs` to the adapter constructor, enabling
runtime injection without hard-coding:

```python
# Via factory
backend = get_backend("openai", api_key=os.environ["OPENAI_API_KEY"])

# Via direct construction
from camel.llm.adapters import OpenAIBackend
backend = OpenAIBackend(api_key="sk-...", model="gpt-4.1")
```

**Credential contract:**

- If `api_key=None`, the adapter MUST fall back to the provider's standard
  environment variable (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
  `OPENAI_API_KEY`) or Application Default Credentials (ADC).
- Adapters MUST NOT log, print, or include credentials in `get_backend_id()`
  or exception messages.
- Adapters MUST NOT store credentials beyond the constructor; use the
  provider SDK client object instead.

---

## 7. Retry Expectations

Adapters **must not** implement internal retry loops.  All retry behaviour
is managed by `CaMeLOrchestrator`:

- Up to 10 retry attempts on `LLMBackendError`.
- Exponential backoff between retries.
- `MaxRetriesExceededError` raised after the ceiling is reached.
- Exception redaction applied before forwarding error context to the P-LLM
  (see M4-F6 through M4-F9).

Adapters may raise `LLMBackendError` immediately on the first failure from
the provider SDK.

---

## 8. Real Adapter Examples

The following examples show direct instantiation and usage of the three production
adapters included with CaMeL.  These are the same adapters tested and validated in
the Milestone 5 integration suite.

### 8.1 ClaudeBackend (Anthropic)

```python
"""Using ClaudeBackend directly — Anthropic SDK required.

Install: pip install camel-security  (anthropic>=0.25 is a core dependency)
"""
from camel.llm.adapters import ClaudeBackend

# Direct instantiation — api_key falls back to ANTHROPIC_API_KEY env var if None
p_llm = ClaudeBackend(model="claude-opus-4-6", api_key=None)
q_llm = ClaudeBackend(model="claude-haiku-4-5-20251001", api_key=None)

print(p_llm.get_backend_id())   # "claude:claude-opus-4-6"
print(q_llm.get_backend_id())   # "claude:claude-haiku-4-5-20251001"
print(p_llm.supports_structured_output())  # True

# Via factory (equivalent)
from camel.llm.backend import get_backend
p_llm = get_backend("claude", model="claude-opus-4-6")
q_llm = get_backend("claude", model="claude-haiku-4-5-20251001")
```

**Structured output mechanism:** Synthetic extraction tool with forced `tool_choice`.
The model returns a `tool_use` block; the adapter validates it against the Pydantic
schema.  `supports_structured_output()` always returns `True` for Claude models.

### 8.2 GeminiBackend (Google)

```python
"""Using GeminiBackend directly — google-generativeai SDK required.

Install: pip install camel-security[gemini]
"""
from camel.llm.adapters import GeminiBackend

# api_key falls back to Application Default Credentials if None
p_llm = GeminiBackend(model="gemini-2.5-pro", api_key=None)
q_llm = GeminiBackend(model="gemini-2.5-flash", api_key=None)

print(p_llm.get_backend_id())   # "gemini:gemini-2.5-pro"
print(q_llm.get_backend_id())   # "gemini:gemini-2.5-flash"
print(p_llm.supports_structured_output())  # True

# Via factory (equivalent)
from camel.llm.backend import get_backend
p_llm = get_backend("gemini", model="gemini-2.5-pro")
q_llm = get_backend("gemini", model="gemini-2.5-flash")
```

**Structured output mechanism:** `response_mime_type="application/json"` +
`response_schema` parameters on the Gemini API call.  `supports_structured_output()`
always returns `True` for Gemini models.

### 8.3 OpenAIBackend (OpenAI)

```python
"""Using OpenAIBackend directly — openai SDK required.

Install: pip install camel-security[openai]
"""
from camel.llm.adapters import OpenAIBackend

# GPT-4.1 — native JSON schema output mode
p_llm = OpenAIBackend(model="gpt-4.1", api_key=None)
print(p_llm.get_backend_id())              # "openai:gpt-4.1"
print(p_llm.supports_structured_output())  # True

# Reasoning models — prompt-based JSON extraction fallback
q_llm_o3 = OpenAIBackend(model="o3", api_key=None)
q_llm_o4 = OpenAIBackend(model="o4-mini", api_key=None)
print(q_llm_o3.supports_structured_output())   # False (prompt-based fallback)
print(q_llm_o4.supports_structured_output())   # False (prompt-based fallback)

# Via factory (equivalent)
from camel.llm.backend import get_backend
p_llm = get_backend("openai", model="gpt-4.1")
q_llm = get_backend("openai", model="o4-mini")
```

**Structured output mechanism (GPT-4.1/4o):** `response_format` with `json_schema`.
**Structured output mechanism (o3/o4-mini):** Prompt-based JSON extraction;
`supports_structured_output()` returns `False` for these models, signalling to
the Q-LLM wrapper that an extra validation pass is required.

### 8.4 Cross-Provider P-LLM / Q-LLM Pairing

All three adapters satisfy the `LLMBackend` protocol, so any combination is valid:

```python
from camel.llm.backend import get_backend
from camel_security import CaMeLAgent

# Claude for planning (large context), Gemini Flash for cheap extraction
p_llm = get_backend("claude", model="claude-opus-4-6")
q_llm = get_backend("gemini", model="gemini-2.5-flash")

agent = CaMeLAgent(p_llm=p_llm, q_llm=q_llm, tools=[], policies=None)

# Gemini for planning, OpenAI o4-mini for extraction
p_llm = get_backend("gemini", model="gemini-2.5-pro")
q_llm = get_backend("openai", model="o4-mini")

agent = CaMeLAgent(p_llm=p_llm, q_llm=q_llm, tools=[], policies=None)
```

The CaMeL runtime calls `generate()` / `complete()` on the P-LLM backend and
`generate_structured()` / `structured_complete()` on the Q-LLM backend.  Both paths
are fully independent — no state is shared.

---

## 9. Skeleton Adapter Example

The following skeleton implements all required methods for a hypothetical
`AcmeProvider` API:

```python
"""Acme AI backend adapter for CaMeL.

Requirements: acme-sdk>=1.0.0  (pip install acme-sdk)
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from camel.llm.backend import LLMBackendError
from camel.llm.protocols import Message, QResponseT


class AcmeBackend:
    """Acme AI backend satisfying LLMBackend and QlLMBackend protocols.

    Parameters
    ----------
    api_key:
        Acme API key.  Falls back to ACME_API_KEY env var if None.
    model:
        Acme model identifier, e.g. "acme-ultra-v2".
    max_tokens:
        Maximum tokens to generate per request.
    **default_kwargs:
        Extra kwargs forwarded to every API call.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "acme-ultra-v2",
        max_tokens: int = 4096,
        **default_kwargs: Any,
    ) -> None:
        try:
            import acme_sdk  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "The 'acme-sdk' package is required for AcmeBackend. "
                "Install it with: pip install acme-sdk"
            ) from exc

        self._client = acme_sdk.AsyncClient(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._default_kwargs = default_kwargs

    # --- Identity & capability ---

    def get_backend_id(self) -> str:
        return f"acme:{self._model}"

    def supports_structured_output(self) -> bool:
        # Set True only if Acme has a native JSON-schema output mode.
        return True

    # --- P-LLM path ---

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        try:
            response = await self._client.chat(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                **{**self._default_kwargs, **kwargs},
            )
            return str(response.text)
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc

    # --- Q-LLM path ---

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        try:
            json_schema = schema.model_json_schema()
            response = await self._client.chat(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                response_schema=json_schema,  # Acme's JSON schema parameter
            )
            raw: dict[str, Any] = json.loads(response.text)
            return schema.model_validate(raw)
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc

    # --- Unified path (generate / generate_structured) ---

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        return await self.complete(messages, **kwargs)

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        try:
            json_schema = schema.model_json_schema()
            response = await self._client.chat(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                response_schema=json_schema,
            )
            raw: dict[str, Any] = json.loads(response.text)
            return schema.model_validate(raw)
        except LLMBackendError:
            raise
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc
```

---

## 10. Registering a New Adapter

After implementing the adapter class, register it in two places:

### 10.1 `camel/llm/adapters/__init__.py`

```python
from camel.llm.adapters.acme import AcmeBackend  # add this line

__all__ = [
    "ClaudeBackend",
    "GeminiBackend",
    "OpenAIBackend",
    "AcmeBackend",          # add this line
]
```

### 10.2 `camel/llm/backend.py` — `get_backend` factory

```python
if provider == "acme":
    from camel.llm.adapters.acme import AcmeBackend  # noqa: PLC0415
    return AcmeBackend(**kwargs)
```

Also update the factory docstring and the `ValueError` message to include
`"acme"` in the list of supported providers.

### 10.3 Update the `pyproject.toml` optional dependencies

If the new provider SDK is optional (recommended), add it to
`[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
acme = ["acme-sdk>=1.0.0"]
all-backends = ["anthropic>=0.25", "google-generativeai>=0.7", "openai>=1.30", "acme-sdk>=1.0.0"]
```

---

## 11. Validated Backends

The following backends are production-validated (NFR-8).  All confirmed to
produce zero prompt injection successes on the CaMeL security test suite.

| Class | Module | Provider | Models | SDK Package |
|---|---|---|---|---|
| `ClaudeBackend` | `camel.llm.adapters.claude` | Anthropic | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` | `anthropic>=0.25` |
| `GeminiBackend` | `camel.llm.adapters.gemini` | Google | `gemini-2.5-pro`, `gemini-2.5-flash` | `google-generativeai>=0.7` |
| `OpenAIBackend` | `camel.llm.adapters.openai` | OpenAI | `gpt-4.1`, `o3`, `o4-mini` | `openai>=1.30` |

### Structured output mechanisms

| Backend | Mechanism | `supports_structured_output()` |
|---|---|---|
| `ClaudeBackend` | Synthetic extraction tool (`tool_choice` forced) | `True` |
| `GeminiBackend` | `response_mime_type="application/json"` + `response_schema` | `True` |
| `OpenAIBackend` (GPT-4.1/4o) | `response_format` with `json_schema` | `True` |
| `OpenAIBackend` (o3/o4-mini) | Prompt-based JSON extraction (fallback) | `False` |

---

## 12. Security Constraints

Adapters operate within the CaMeL security boundary and **must** respect the
following constraints:

1. **P-LLM isolation** — `complete()` must never include tool return values
   in the messages it sends to the provider.  This is enforced by
   `PLLMWrapper._build_messages`, which raises `PLLMIsolationError` if any
   `CaMeLValue` is detected.  Adapters must not circumvent this guard.

2. **Q-LLM isolation** — `structured_complete()` must not pass tool
   definitions to the underlying provider API.  Structured output must be
   obtained via native schema-constrained mechanisms, not by giving the model
   real tool-calling capability.

3. **No credential leakage** — `get_backend_id()` must return a
   provider/model identifier only.  No API keys, session tokens, or
   personally identifiable information.

4. **LLMBackendError wrapping** — All provider SDK exceptions must be
   wrapped as `LLMBackendError`.  Allowing provider-specific exceptions to
   propagate would bypass the CaMeL exception redaction engine (M4-F6),
   potentially leaking untrusted data to the P-LLM.

5. **No timing primitives** — Adapters must not use `time.sleep`,
   `asyncio.sleep`, or other timing primitives in the critical path.
   Retry backoff is managed externally by `CaMeLOrchestrator`.

---

*See also: [LLM Backend API Reference](api/llm_backend.md) ·
[Architecture Reference](architecture.md) ·
[Security Hardening Design](design/security_hardening_design.md)*
