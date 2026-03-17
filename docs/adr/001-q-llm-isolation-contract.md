# ADR-001: Q-LLM Isolation Contract and Schema Conventions

| Field        | Value                              |
|--------------|------------------------------------|
| Status       | Accepted                           |
| Date         | 2026-03-17                         |
| Author       | Software Architect Persona         |
| Supersedes   | —                                  |
| Superseded by| —                                  |

---

## Context

CaMeL operates with two distinct LLM roles:

- **P-LLM (Privileged LLM)** — the orchestration and planning model. It has
  full capability: it can call tools, emit free-form text, and manage
  multi-turn conversation state. Because it is trusted, its outputs may
  directly drive tool calls and state mutations.

- **Q-LLM (Quarantined LLM)** — a read-only extraction model. It is given
  *untrusted* content (e.g. the body of an email, a scraped web page, a user
  upload) and asked to extract structured facts from it. The principal threat
  is **prompt injection**: malicious content inside the untrusted text could
  attempt to hijack the LLM into taking unauthorised actions.

The core safety invariant of CaMeL is:

> **Untrusted content never reaches the P-LLM's tool-calling path.**

To enforce this, the Q-LLM must be architecturally incapable of producing
free-form text or invoking tools — every response must be a validated Pydantic
model instance, and that instance is tagged as an untrusted `CaMeLValue`
before the P-LLM sees any of its fields.

---

## Decision

### 1. QlLMBackend Protocol

The Q-LLM backend is described by the `QlLMBackend` structural protocol
(defined in `camel/llm/protocols.py`), separate from the `LLMBackend`
protocol used by the P-LLM.

```python
class QlLMBackend(Protocol):
    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT: ...
```

**Key design choices:**

- `structured_complete` has **no `tools` parameter**. This is intentional and
  non-negotiable: the isolation contract is enforced at the call site, not
  inside the model.
- The method accepts only `messages` and a `schema` type. Any additional
  configuration (temperature, model name, etc.) is handled at backend
  construction time, not per-call.
- The return type is `QResponseT`, a `TypeVar` bound to `QResponse`. The
  caller always receives a fully validated Pydantic instance.

**Relationship to `LLMBackend`:** The two protocols are intentionally
**not** related by inheritance. A backend implementation MAY satisfy both
protocols (e.g. a single Anthropic client class), but the interfaces are
separate to prevent accidental use of a Q-LLM backend in a P-LLM slot or
vice versa.

---

### 2. QResponse Base Schema Convention

All schemas returned by a `QlLMBackend` MUST inherit from `QResponse`
(defined in `camel/llm/schemas.py`):

```python
class QResponse(BaseModel):
    have_enough_information: bool = Field(
        ...,
        description="True only when the model has sufficient context ...",
    )

    model_config = {
        "extra": "forbid",  # No free-form text smuggling via extra fields.
        "frozen": True,      # Immutable after construction.
    }
```

#### The `have_enough_information` field

- **Mandatory** on every `QResponse` subclass (inherited, not re-declared).
- The model sets it to `True` only when it has sufficient information to
  populate *all* other fields with meaningful, accurate values.
- When `False`, the caller MUST raise `NotEnoughInformationError` and MUST
  NOT forward any field value to the P-LLM.
- The field is the *only* mechanism for the Q-LLM to signal uncertainty.
  Natural-language refusals ("I'm not sure…") are not permitted — they would
  constitute free-form text in the response.

#### Schema definition example

```python
from camel.llm.schemas import QResponse

class EmailExtraction(QResponse):
    sender_address: str
    subject: str
    body_summary: str  # ≤ 3 sentences, no verbatim content from the email
```

#### Usage example (caller side)

```python
from camel.llm.exceptions import NotEnoughInformationError
from camel.values import CaMeLValue  # runtime trust-tagging

result: EmailExtraction = await q_llm.structured_complete(
    messages=build_extraction_prompt(raw_email),
    schema=EmailExtraction,
)

if not result.have_enough_information:
    raise NotEnoughInformationError(
        schema_type=EmailExtraction,
        partial_response=result,
    )

# All fields are now populated, but the data is still untrusted.
# Tag every field as an untrusted CaMeLValue before P-LLM consumption:
untrusted_sender = CaMeLValue(result.sender_address, trusted=False)
untrusted_summary = CaMeLValue(result.body_summary, trusted=False)
```

---

### 3. NotEnoughInformationError Exception

Defined in `camel/llm/exceptions.py`.

```python
class NotEnoughInformationError(Exception):
    schema_type: type[QResponse]
    partial_response: QResponse | None
```

- Raised by the **caller** (not the backend) when `result.have_enough_information is False`.
- Carries the `schema_type` for logging and the `partial_response` for
  debugging. Consumers MUST NOT extract field values from `partial_response`.
- Propagates up to the P-LLM orchestration loop, which may retry with more
  context, fall back to a different strategy, or surface the error to the user.

---

### 4. No-Tool-Calling Isolation Contract

The `QlLMBackend.structured_complete` signature enforces isolation at the
**type level** — there is no parameter through which tools could be supplied.
Concrete implementations MUST additionally:

1. **Not pass tool/function definitions to the underlying API call.** Even if
   the chosen model supports function calling, the implementation must omit
   the `tools` / `functions` parameter entirely.
2. **Use the provider's structured-output feature**, not prompt engineering,
   to guarantee schema conformance (e.g. Anthropic's synthetic extraction
   tool pattern with `tool_choice={"type": "tool"}`, or OpenAI's
   `response_format={"type": "json_schema", ...}`).
3. **Reject any response that does not validate against the schema.** If the
   provider returns malformed JSON or a response that fails Pydantic
   validation, the implementation must raise a `ValueError` (or a subclass),
   never silently coerce.

---

### 5. Untrusted CaMeLValue Tagging

The `QlLMBackend` contract does not itself import or reference `CaMeLValue`
— that type lives in `camel.values` (to be defined). The tagging obligation
is a **caller responsibility**, documented here:

> Every field of a `QResponse` instance (even one with
> `have_enough_information == True`) originates from untrusted external
> content. The caller MUST wrap each field in a `CaMeLValue(value,
> trusted=False)` before passing it into any context where the P-LLM or a
> tool could act on it.

The `QResponse.model_config["frozen"] = True` setting helps enforce this:
because the instance is immutable, callers cannot accidentally overwrite a
field to strip the "untrusted" annotation. They must always create a new
`CaMeLValue` wrapper.

---

## Consequences

### Positive

- **Prompt injection is structurally contained.** Malicious content in an
  email cannot cause the Q-LLM to call tools or emit arbitrary instructions,
  because neither capability exists in the `QlLMBackend` interface.
- **Schema conformance is guaranteed.** Pydantic validation runs on every
  Q-LLM response before the caller sees it.
- **Trust tracking is explicit.** The `have_enough_information` field and
  `CaMeLValue` tagging make the trust boundary visible in code rather than
  relying on developer discipline.
- **Testability.** Structural protocols + Pydantic models mean test doubles
  are trivial to write without inheriting from concrete classes.

### Negative / Trade-offs

- **Two backend interfaces to maintain.** `LLMBackend` and `QlLLMBackend`
  diverge over time. Mitigated by keeping them intentionally minimal.
- **Structured output has provider-specific implementation cost.** Each
  concrete backend must implement the provider's structured-output API, not
  just call `chat.completions.create`. Mitigated by centralising this in
  concrete backend classes.
- **`have_enough_information = False` is a blunt instrument.** The model
  cannot signal *which* fields it is uncertain about. Future work may
  introduce optional confidence fields per-field, but this ADR intentionally
  keeps the initial contract minimal.

---

## Alternatives Considered

### A. Inherit `QlLMBackend` from `LLMBackend`

Rejected. Inheritance would imply a Q-LLM can substitute for a P-LLM. The
Liskov Substitution Principle would then require a Q-LLM to accept `tools`
parameters, defeating the isolation goal.

### B. Validate `have_enough_information` inside `QlLMBackend.structured_complete`

Rejected. The backend raising `NotEnoughInformationError` would couple the
transport layer to application logic. Keeping the raise in the caller gives
orchestration code control over retry and fallback strategies.

### C. Return `None` instead of raising on `have_enough_information = False`

Rejected. `None` propagation is silent and easily missed. An exception forces
the caller to handle the case explicitly and produces a clear stack trace.

---

## References

- CaMeL paper: "CaMeL: Capability and Memory Language for Secure LLM Agents"
- `camel/llm/protocols.py` — `LLMBackend` and `QlLMBackend` protocols
- `camel/llm/schemas.py` — `QResponse` base class
- `camel/llm/exceptions.py` — `NotEnoughInformationError`
