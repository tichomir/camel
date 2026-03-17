# ADR-006: Q-LLM Dynamic Schema Injection and `query_quarantined_llm` Interface

| Field        | Value                              |
|--------------|------------------------------------|
| Status       | Accepted                           |
| Date         | 2026-03-17                         |
| Author       | Software Architect Persona         |
| Supersedes   | —                                  |
| Supplements  | ADR-001 (Q-LLM Isolation Contract) |

---

## Context

ADR-001 established the Q-LLM isolation contract and introduced `QResponse` as a
mandatory base class for all Q-LLM output schemas. Callers subclass `QResponse` to
declare their extraction schema and inherit `have_enough_information: bool`
automatically.

This approach works for developer-authored schemas, but introduces friction in two
scenarios that arise in the CaMeL runtime:

1. **Interpreter-callable interface.** P-LLM-generated code calls Q-LLM extraction as
   a plain function from within the restricted Python interpreter:

   ```python
   result = query_quarantined_llm("extract sender and subject", EmailSchema)
   ```

   The interpreter needs a simple two-argument callable that accepts *any*
   `BaseModel` subclass — not just `QResponse` descendants. Requiring P-LLM-generated
   code to subclass `QResponse` would leak the Q-LLM implementation detail into the
   planning prompt.

2. **Third-party / legacy schemas.** Tool providers may supply existing Pydantic
   models that do not inherit from `QResponse`. Requiring them to re-declare their
   schema as a `QResponse` subclass is a barrier to adoption (NFR-7).

The solution is **dynamic schema injection**: the `query_quarantined_llm` callable
accepts any `BaseModel` subclass and transparently injects `have_enough_information:
bool` into a dynamically created augmented class before passing it to the backend.
The original caller-supplied schema class is never mutated.

---

## Decision

### 1. `augment_schema_with_hei` — Dynamic Field Injection

```python
from pydantic import BaseModel, Field, create_model
from typing import TypeVar

T = TypeVar("T", bound=BaseModel)

_HEI_FIELD_NAME = "have_enough_information"

def augment_schema_with_hei(schema: type[T]) -> type[T]:
    """Return a new model class identical to *schema* but with
    ``have_enough_information: bool`` injected as a required field.

    If ``have_enough_information`` already exists in ``schema.model_fields``
    (e.g. the caller passed a ``QResponse`` subclass), the original class
    is returned unchanged — no copy is made.

    The original *schema* is never mutated.
    """
    if _HEI_FIELD_NAME in schema.model_fields:
        return schema

    return create_model(  # type: ignore[call-overload]
        schema.__name__,
        __base__=schema,
        have_enough_information=(
            bool,
            Field(
                ...,
                description=(
                    "Set to True only when the model has sufficient information "
                    "to populate all other fields with meaningful values. When "
                    "False, the caller MUST raise NotEnoughInformationError."
                ),
            ),
        ),
    )
```

**Rationale for `create_model` over `__init_subclass__`:**

`__init_subclass__` hooks fire at class-definition time and require callers to
participate (they must subclass something that defines `__init_subclass__`). This
does not work for third-party models and couples injection to class hierarchy.

`create_model` constructs a *new* class at call time without touching the original.
The new class is a subclass of `schema`, so `isinstance(result, schema)` is `True`
and the caller's type annotation `-> T` is satisfied by the runtime even though the
actual instance is of the augmented type.

**Rationale for idempotency check (`_HEI_FIELD_NAME in schema.model_fields`):**

`QResponse` subclasses already carry `have_enough_information`. Attempting to inject
it again would raise a Pydantic `FieldConflict` error. The idempotency check lets
`augment_schema_with_hei` be called unconditionally regardless of whether the caller
used `QResponse` or a plain `BaseModel`.

---

### 2. `query_quarantined_llm(prompt, output_schema)` Interface

The interpreter-callable interface is defined as an async callable conforming to the
`QueryQLLMCallable` structural protocol (defined in `camel/llm/query_interface.py`):

```python
async def query_quarantined_llm(
    prompt: str,
    output_schema: type[T],
) -> T:
    ...
```

| Parameter     | Type               | Description                                        |
|---------------|--------------------|----------------------------------------------------|
| `prompt`      | `str`              | Extraction instruction + untrusted content to parse|
| `output_schema` | `type[T]`        | Any `BaseModel` subclass describing the expected output |

**Return value:** A validated instance of `output_schema` (not the augmented subclass)
with all fields populated. The `have_enough_information` field is stripped before
returning so callers receive a clean `T` instance matching their original schema.

**Exceptions raised:**

| Exception                  | Condition                                        |
|---------------------------|--------------------------------------------------|
| `NotEnoughInformationError` | `have_enough_information` is `False`           |
| `pydantic.ValidationError` | Backend response does not conform to schema     |
| `ValueError`               | Backend returns malformed JSON / non-dict data  |

The function is **not** defined in global scope. It is created via the factory
`make_query_quarantined_llm(backend)` and injected into the interpreter's tool
namespace. This binds the backend at registration time without exposing it to
P-LLM-generated code.

---

### 3. Schema Validation Pipeline

The end-to-end pipeline for a single `query_quarantined_llm` call is:

```
prompt, output_schema
        │
        ▼
┌──────────────────────────────┐
│ augment_schema_with_hei      │  Inject have_enough_information (idempotent)
│ → augmented_schema           │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ _build_messages(prompt,      │  Wrap prompt in isolation delimiters;
│   augmented_schema)          │  embed JSON Schema description
│ → list[Message]              │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ backend.structured_complete  │  QlLMBackend call — NO tools parameter.
│ (messages, augmented_schema) │  Provider structured-output mode enforces
│ → raw (unvalidated dict or   │  schema at the API level.
│   partial model)             │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ augmented_schema             │  Full Pydantic validation.
│   .model_validate(raw)       │  Raises ValidationError on malformed data.
│ → validated_augmented        │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Check validated_augmented    │  Guards against partial/uncertain extractions.
│   .have_enough_information   │  Raises NotEnoughInformationError (fixed msg).
└──────────────┬───────────────┘
               │ True
               ▼
┌──────────────────────────────┐
│ output_schema                │  Re-validate against original schema to strip
│   .model_validate(           │  the injected field and satisfy return type T.
│     validated_augmented      │
│     .model_dump(             │
│       exclude={_HEI_FIELD})) │
│ → result: T                  │
└──────────────────────────────┘
```

The double validation step (augmented → original) is intentional:

- The **first** validation (against augmented schema) ensures `have_enough_information`
  is present and is a valid `bool` — it cannot be omitted or spoofed as a non-bool
  string.
- The **second** validation (against original schema) strips the injected field and
  confirms all caller-declared fields pass their own validators. This is important when
  the original schema has custom field validators or constraints — the augmented
  subclass inherits them, but the re-validation step gives the original schema a clean
  chance to run them.

---

### 4. `NotEnoughInformationError` — Fixed, Content-Free Message

The error message MUST NOT interpolate untrusted data (e.g. field values from the
partial Q-LLM response). The `schema_type.__name__` is a developer-declared Python
class name and is considered trusted. The optional `message` parameter that existed in
earlier drafts is removed — any message override could inadvertently receive
untrusted content from a careless caller.

**Final constructor signature:**

```python
class NotEnoughInformationError(Exception):
    def __init__(
        self,
        schema_type: type[QResponse],
        partial_response: QResponse | None = None,
    ) -> None:
        self.schema_type = schema_type
        self.partial_response = partial_response
        super().__init__(
            f"Q-LLM reported insufficient information to populate "
            f"{schema_type.__name__!r}"
        )
```

The message template `"Q-LLM reported insufficient information to populate {name!r}"`
is fixed. There is no way for untrusted data (email bodies, web page content, tool
return values) to appear in the exception message:

- `schema_type.__name__` is the class name as written by the developer.
- `partial_response` is stored as an attribute (for debugging inspection) but never
  interpolated into `str(error)`.

---

### 5. No-Tool-Calling Prohibition — Enforcement Layers

The `query_quarantined_llm` interface prohibits tool calling at three levels:

| Layer          | Mechanism                                                             |
|----------------|-----------------------------------------------------------------------|
| **Protocol**   | `QlLMBackend.structured_complete` has no `tools` parameter           |
| **API surface**| `query_quarantined_llm(prompt, output_schema)` has no `tools` param |
| **Runtime**    | Factory `make_query_quarantined_llm` never passes tools to backend   |

Even if a concrete `QlLMBackend` implementation has an internal method that could
accept tools (e.g. because it reuses the same API client as the P-LLM backend), the
`structured_complete` call path MUST NOT forward tool definitions. This is verified
by the isolation test suite (`tests/test_qllm.py`).

---

## Consequences

### Positive

- **Caller ergonomics.** Any `BaseModel` can be passed to `query_quarantined_llm`
  without inheriting from `QResponse`. The Q-LLM implementation detail is invisible
  to P-LLM-generated code.
- **Backward compatibility.** Existing `QResponse` subclasses continue to work — the
  idempotency check in `augment_schema_with_hei` is a no-op for them.
- **Type safety.** The return type of `query_quarantined_llm` is `T` (the caller's
  original schema type), so static analysis remains accurate without casting.
- **Injection-proof error messages.** Removing the `message` parameter eliminates the
  risk of untrusted content surfacing in `NotEnoughInformationError.__str__`.

### Negative / Trade-offs

- **Extra model class created per call.** `create_model` produces a new class object
  on every call where `have_enough_information` is not already present. For
  high-frequency calls this should be memoised (e.g. `functools.lru_cache` keyed on
  the original schema class).
- **Double validation overhead.** Two `model_validate` calls per Q-LLM response adds
  negligible latency relative to the LLM call itself, but test mocks should be aware
  that both calls occur.
- **Augmented class name collision.** `create_model(schema.__name__, ...)` reuses the
  original class name for the augmented class. This aids prompt readability
  (the JSON Schema description still shows the original name) but means
  `type(result).__name__ == type(original_schema_instance).__name__` — distinguish
  via `isinstance` checks, not name comparisons.

---

## Alternatives Considered

### A. Keep mandatory `QResponse` inheritance only

Rejected for the interpreter-callable use case. P-LLM-generated code would need to
import or reference `QResponse` explicitly, which leaks implementation details into
the planning prompt and forces tool providers to depend on `camel.llm.schemas`.

### B. Use `__init_subclass__` for automatic injection

Considered. A metaclass or `__init_subclass__` on `QResponse` could auto-inject
`have_enough_information` into any subclass. Rejected because:
- Callers must still inherit from `QResponse` (same limitation as Option A).
- `__init_subclass__` fires at class-definition time — you cannot retrofit an existing
  class without modifying it.

### C. Accept `dict` as output and skip schema validation

Rejected. Untyped dict output is the exact vector through which free-form text could
re-enter the trusted runtime. Pydantic validation at the boundary is non-negotiable
(ADR-001, §3).

### D. Return `None` instead of raising `NotEnoughInformationError`

Rejected (per ADR-001 §3.C). `None` propagation is silent; an exception forces
explicit handling and produces an actionable stack trace.

---

## References

- ADR-001: Q-LLM Isolation Contract and Schema Conventions
- `camel/llm/query_interface.py` — `augment_schema_with_hei`, `QueryQLLMCallable`,
  `make_query_quarantined_llm`
- `camel/llm/schemas.py` — `QResponse` base class
- `camel/llm/exceptions.py` — `NotEnoughInformationError`
- `camel/llm/protocols.py` — `QlLMBackend` protocol
- Pydantic docs: `pydantic.create_model`
