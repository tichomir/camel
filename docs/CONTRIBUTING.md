# Contributing to CaMeL

Thank you for contributing to CaMeL.  This document covers the contribution
workflow, coding conventions, and the three-step process for adding a new tool
(NFR-7).

---

## Table of Contents

1. [Development Setup](#1-development-setup)
2. [Coding Conventions](#2-coding-conventions)
3. [Adding a New Tool — 3-Step Guide (NFR-7)](#3-adding-a-new-tool--3-step-guide-nfr-7)
4. [Swapping LLM Backends](#4-swapping-llm-backends)
5. [Running Tests](#5-running-tests)
6. [Pull Request Checklist](#6-pull-request-checklist)

---

## 1. Development Setup

See [docs/setup.md](setup.md) for the full step-by-step environment setup,
including Python version requirements, dependency installation, and API key
configuration.

Short version:

```bash
git clone <repo-url> camel && cd camel
pip install -e ".[dev]"
pre-commit install
```

---

## 2. Coding Conventions

| Area | Convention |
|---|---|
| Python version | 3.11+ only. No 3.10 compatibility shims. |
| Line length | 100 characters (`ruff` + `black` both enforce this). |
| Imports | Sorted and grouped by `ruff --select I`. |
| Type annotations | All public functions and methods must be fully annotated. `mypy --strict` must pass. |
| Docstrings | All public symbols require docstrings (`interrogate` enforces ≥90% coverage). Use NumPy-style docstrings with `Parameters`, `Returns`, `Raises` sections. |
| Formatting | `black`. Run `black .` before committing; the pre-commit hook enforces this. |
| `CaMeLValue` returns | Every tool callable registered with `CaMeLInterpreter` **must** return a `CaMeLValue`. Returning a bare Python value raises `TypeError` at runtime. |
| No bare `except` | Catch specific exception types. Use `except Exception as exc` only for top-level boundary handlers. |

---

## 3. Adding a New Tool — 3-Step Guide (NFR-7)

CaMeL's extensibility contract (NFR-7) requires exactly three steps to add a
new tool:

> 1. Register the tool signature.
> 2. (Optional) Provide a capability annotation function.
> 3. (Optional) Register a security policy.

### Step 1 — Register the tool signature

Write a Python callable that accepts `CaMeLValue` arguments and returns a
`CaMeLValue`.  Pass it to `CaMeLInterpreter` in the `tools` dict.

```python
from camel import CaMeLInterpreter
from camel.value import CaMeLValue, wrap, Public


def get_weather(city: CaMeLValue) -> CaMeLValue:
    """Fetch current weather for *city* (example tool)."""
    raw_city: str = city.raw
    # --- call the real external API here ---
    weather_data = {"temp": 22, "condition": "sunny"}
    return CaMeLValue(
        value=weather_data,
        sources=frozenset({"get_weather"}),
        inner_source=None,
        readers=Public,          # public data; any reader may see it
    )


interp = CaMeLInterpreter(
    tools={"get_weather": get_weather},
)
```

The interpreter makes `get_weather` available in every execution plan it runs.
The P-LLM learns the function signature from the tool signatures list passed to
`PLLMWrapper` (see below).

**Registering with PLLMWrapper:**

```python
from camel.llm.p_llm import PLLMWrapper, ToolSignature
from camel.llm.backend import get_backend

tool_signatures = [
    ToolSignature(
        name="get_weather",
        description="Return current weather for a given city.",
        parameters={"city": "str"},
        return_type="dict",
    ),
]

wrapper = PLLMWrapper(
    backend=get_backend("claude"),
    tool_signatures=tool_signatures,
)
```

### Step 2 — (Optional) Provide a capability annotation function

If the tool returns structured data with multiple fields, annotate each field's
`inner_source` and `readers` so downstream policies can make fine-grained
decisions.

```python
def get_email(email_id: CaMeLValue) -> CaMeLValue:
    """Fetch an email by ID."""
    raw_id: int = email_id.raw
    # --- call email API ---
    email = _fetch_email(raw_id)

    return CaMeLValue(
        value=email,
        sources=frozenset({"get_email"}),
        inner_source=None,
        # Restrict readers to the intended recipient:
        readers=frozenset({email["to"]}),
    )
```

For tools that return multiple sub-fields (e.g. an email object with `sender`,
`subject`, `body`), the interpreter propagates capabilities field-by-field when
the plan accesses `email.sender` — each field access calls `propagate_subscript`,
inheriting the parent object's capabilities.  Set a specific `inner_source` on
the top-level return value to help policies trace the sub-field origin.

### Step 3 — (Optional) Register a security policy

Policies are Python callables with signature
`(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> PolicyResult`.

```python
from typing import Mapping
from camel.interpreter import PolicyViolationError
from camel.value import CaMeLValue, Public


class Allowed:
    """Tool call is permitted."""


class Denied:
    def __init__(self, reason: str) -> None:
        self.reason = reason


def send_email_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> Allowed | Denied:
    """Block send_email if the 'to' address comes from an untrusted source."""
    if tool_name != "send_email":
        return Allowed()

    to_value: CaMeLValue = kwargs["to"]
    # Allow only if 'to' was a user-typed literal (trusted source):
    if "User literal" in to_value.sources:
        return Allowed()
    # Allow if 'to' is already an authorized reader of the body:
    body_value: CaMeLValue = kwargs.get("body", CaMeLValue(
        value="", sources=frozenset(), inner_source=None, readers=Public
    ))
    if isinstance(body_value.readers, frozenset) and to_value.raw in body_value.readers:
        return Allowed()
    return Denied(
        reason=f"Recipient '{to_value.raw}' not from a trusted source."
    )


class MyPolicyEngine:
    def check(
        self, tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        return send_email_policy(tool_name, kwargs)


interp = CaMeLInterpreter(
    tools={"send_email": send_email, "get_email": get_email},
    policy_engine=MyPolicyEngine(),
)
```

When `policy_engine.check()` returns `Denied`, the interpreter raises
`PolicyViolationError(tool_name, reason)` and the execution loop triggers a
P-LLM retry (up to 10 times, M2-F8).

---

## 4. Swapping LLM Backends

The `LLMBackend` protocol is provider-agnostic.  Swapping between Claude and
Gemini requires only environment variable or constructor argument changes — no
code modifications.

### 4.1 Via environment variable

```bash
# Use Claude (default)
export CAMEL_LLM_PROVIDER=claude
export ANTHROPIC_API_KEY="sk-ant-..."

# Switch to Gemini — no code change needed
export CAMEL_LLM_PROVIDER=gemini
export GOOGLE_API_KEY="AI..."
```

```python
import os
from camel.llm.backend import get_backend

backend = get_backend(os.environ.get("CAMEL_LLM_PROVIDER", "claude"))
```

### 4.2 Via constructor arguments

```python
from camel.llm.backend import get_backend

# Claude
claude_backend = get_backend(
    "claude",
    api_key="sk-ant-...",
    model="claude-opus-4-6",
)

# Gemini — identical interface, different provider string
gemini_backend = get_backend(
    "gemini",
    api_key="AI...",
    model="gemini-2.0-flash",
)
```

Both backends satisfy the same `LLMBackend` protocol (`generate` /
`generate_structured`), so `PLLMWrapper` and `QLLMWrapper` accept either
without modification.

### 4.3 Custom backend

Implement the `LLMBackend` structural protocol (two async methods: `generate`
and `generate_structured`) and pass your instance directly to `PLLMWrapper` or
`QLLMWrapper`:

```python
from camel.llm.protocols import Message
from pydantic import BaseModel


class MyCustomBackend:
    async def generate(self, messages: list[Message], **kwargs) -> str:
        ...   # call your provider

    async def generate_structured(
        self, messages: list[Message], schema: type[BaseModel]
    ) -> BaseModel:
        ...   # call your provider with schema enforcement
```

No registration step is required — structural typing means any object
implementing the two methods is a valid `LLMBackend`.

---

## 5. Running Tests

See [docs/setup.md § 4](setup.md#4-running-the-test-suite) for the complete
test command reference.

Quick reference:

```bash
# Full suite
pytest

# Unit tests only (no network / LLM calls)
pytest tests/test_value.py tests/test_interpreter.py tests/test_dependency_graph.py

# Lint + type check + docstring coverage
ruff check . && black --check . && mypy camel/ && interrogate camel/
```

---

## 6. Pull Request Checklist

Before opening a PR, confirm:

- [ ] `pytest` passes with zero failures.
- [ ] `ruff check .` reports no errors.
- [ ] `black --check .` reports no reformatting needed.
- [ ] `mypy camel/` reports no type errors.
- [ ] `interrogate camel/` reports ≥90% docstring coverage.
- [ ] New tools follow the 3-step NFR-7 guide (§3 above).
- [ ] New public symbols have docstrings with `Parameters` / `Returns` /
      `Raises` sections.
- [ ] `CHANGELOG.md` updated if the change affects the public API or
      user-visible behaviour.
