# ADR-002: CaMeLValue Dataclass and Capability Propagation System

| Field         | Value                              |
|---------------|------------------------------------|
| Status        | Accepted                           |
| Date          | 2026-03-17                         |
| Author        | Software Architect Persona         |
| Supersedes    | —                                  |
| Superseded by | —                                  |
| Related       | ADR-001 (Q-LLM Isolation Contract) |

---

## Context

The CaMeL interpreter tracks the *provenance* of every runtime value so that
the security policy engine can decide, before each tool call, whether the
arguments to that call are authorised.  This requires a first-class container
type that carries a Python value together with its capability metadata.

Three questions must be answered:

1. **What metadata** must accompany each value?
2. **How is metadata propagated** when values are combined by operations?
3. **How does "public" (unrestricted) data** differ from restricted data?

---

## Decision

### 1. `CaMeLValue` — frozen dataclass

All runtime values managed by the interpreter are wrapped in a
`CaMeLValue` (defined in `camel/value.py`):

```python
@dataclass(frozen=True)
class CaMeLValue:
    value:        Any
    sources:      frozenset[str]
    inner_source: str | None
    readers:      frozenset[str] | _PublicType
```

#### Field specification

| Field          | Type                          | Semantics |
|----------------|-------------------------------|-----------|
| `value`        | `Any`                         | The underlying Python value. |
| `sources`      | `frozenset[str]`              | Origin labels — tool names, `"User literal"`, `"CaMeL"`, etc. |
| `inner_source` | `str \| None`                 | Sub-field within the originating tool response (e.g. `"sender"`). `None` for all derived values. |
| `readers`      | `frozenset[str] \| _PublicType` | Authorised audience. `Public` = any reader allowed. Empty frozenset = no reader allowed. |

**Immutability is enforced** via `frozen=True`.  No field may be mutated
after construction; derived values must be produced by the propagation
functions.

---

### 2. `Public` singleton — open-readers sentinel

```python
class _PublicType:
    """Singleton; use the module-level `Public` constant."""
    ...

Public: Final[_PublicType] = _PublicType()
```

**`Public` is categorically different from `frozenset()`:**

| `readers` value | Meaning |
|-----------------|---------|
| `Public`        | Any principal may receive this value. |
| `frozenset({"a@x.com"})` | Only `a@x.com` may receive this value. |
| `frozenset()` (empty)    | **No** principal is authorised; value must not be forwarded. |

**Union semantics — `Public` is absorbing:**

> `Public` ∪ (anything) = `Public`

This guarantees that a derived value is never *more restrictive* than its
least-restrictive input.

**Why a singleton class rather than a sentinel string or `None`?**

- `isinstance(readers, _PublicType)` gives mypy a narrow type at policy
  evaluation sites without requiring string comparison or `is None` checks.
- It is pickle-safe (singleton identity is preserved via `__reduce__`).
- It is unambiguously named in `repr`, making debug output readable.

---

### 3. `Readers` type alias

```python
Readers = frozenset[str] | _PublicType
```

This alias is used throughout `value.py` and in the policy engine.  mypy
resolves it to a proper union; no `Any` leakage occurs at public API
boundaries.

---

### 4. Capability propagation rules

All propagation functions return a **new** `CaMeLValue` (dataclass is frozen).
`inner_source` is always `None` on the result (only direct tool-output
extraction sets a non-`None` `inner_source`).

#### 4.1 `propagate_assignment(source, new_value) → CaMeLValue`

Used when the interpreter evaluates a plain assignment `x = expr`.
Copies `sources` and `readers` from `source`; sets `inner_source = None`.

```
result.sources      = source.sources
result.readers      = source.readers
result.inner_source = None
result.value        = new_value
```

#### 4.2 `propagate_binary_op(left, right, result) → CaMeLValue`

Used for all binary AST nodes (`+`, `-`, `*`, `/`, `and`, `or`, comparisons,
etc.).

```
result.sources      = left.sources ∪ right.sources
result.readers      = left.readers ∪ right.readers   # Public-absorbing
result.inner_source = None
result.value        = result (computed Python value)
```

#### 4.3 `propagate_list_construction(elements, result) → CaMeLValue`

Used for list literals `[e0, e1, …, eN]`.

```
result.sources      = ∪ { e.sources  for e in elements }
result.readers      = ∪ { e.readers  for e in elements }   # Public-absorbing
result.inner_source = None
result.value        = result (Python list)
```

Empty list produces `sources=frozenset()`, `readers=frozenset()`.

#### 4.4 `propagate_dict_construction(keys, values, result) → CaMeLValue`

Used for dict literals `{k0: v0, …, kN: vN}`.  Both keys and values
contribute, because a key derived from untrusted content encodes information
via the presence or absence of entries.

```
result.sources      = ∪ { e.sources for e in keys + values }
result.readers      = ∪ { e.readers for e in keys + values }   # Public-absorbing
result.inner_source = None
result.value        = result (Python dict)
```

#### 4.5 `propagate_subscript(container, key, result) → CaMeLValue`

Used for `container[key]` expressions.

```
result.sources      = container.sources ∪ key.sources
result.readers      = container.readers ∪ key.readers   # Public-absorbing
result.inner_source = None
result.value        = result (extracted Python value)
```

The key contributes because an untrusted index used to select from a trusted
container transfers the untrusted provenance into the result.

---

### 5. `raw` / `raw_value` accessor contract

Tool execution (Milestone 2) needs to unwrap a `CaMeLValue` to pass the bare
Python value to an external API.  Two equivalent accessors are provided:

```python
# Property (preferred for direct field access)
value: Any = camel_value.raw

# Module-level function (preferred when a callable is needed, e.g. map())
value: Any = raw_value(camel_value)
```

**Contract:**

- Both always return `camel_value.value` without side effects.
- Callers in tool-execution code **must** use one of these forms; direct
  `.value` access is discouraged to maintain the naming contract and allow
  future refactoring.
- Neither accessor performs any capability check.  Capability enforcement is
  exclusively the policy engine's responsibility.

---

### 6. mypy compatibility

The module uses `from __future__ import annotations` for PEP 563 deferred
evaluation.  All public API types are concrete or explicitly typed:

- `frozenset[str]` — not `frozenset` (generic alias required for mypy strict)
- `Readers = frozenset[str] | _PublicType` — named alias, no `Any` in
  the public surface
- `CaMeLValue.value: Any` — intentional; the container is generic by design.
  A fully generic `CaMeLValue[T]` was considered (§ Alternatives) but
  rejected.

---

## Consequences

### Positive

- **Complete provenance tracking.** Every value carries its full origin,
  enabling deterministic policy evaluation with no LLM involvement.
- **Immutability.** `frozen=True` + `frozenset` fields prevent accidental
  mutation that could strip or forge capability metadata.
- **`Public` absorption.** Union semantics guarantee monotonicity: derived
  values can never be more trusted than their least-trusted input.
- **Clear "no readers" vs "all readers" distinction.** `frozenset()` and
  `Public` are unambiguous; there is no null/None ambiguity.
- **mypy-clean.** No `Any` leakage at public API boundaries; narrow type guards
  work correctly at policy evaluation sites.

### Negative / Trade-offs

- **`value: Any` field.** The container is not generic (`CaMeLValue[T]`), so
  the type of `raw` / `raw_value` is `Any` at call sites.  This is acceptable
  because the interpreter is dynamically typed by nature (it executes arbitrary
  P-LLM-generated Python).
- **`inner_source` cleared on derivation.** Once a value passes through any
  operation, its sub-field provenance is lost.  Fine-grained sub-field tracking
  would require a recursive capability graph; deferred to future work.
- **No partial trust.** A value is either fully trusted (Public) or restricted
  to a set of readers.  Per-field confidence scores are out of scope (see
  ADR-001 §Consequences).

---

## Alternatives Considered

### A. Generic `CaMeLValue[T]`

A `CaMeLValue[T]` with `value: T` and `raw: T` would give type-safe access
at call sites.  Rejected because:

- The interpreter operates on dynamically typed AST nodes; the type `T` cannot
  be statically resolved at interpreter implementation time.
- Every propagation function would require complex `TypeVar` bounds or
  `overload` declarations, adding significant boilerplate with no practical
  benefit.

### B. Boolean `trusted: bool` field (as in ADR-001 examples)

ADR-001 used a simplified `CaMeLValue(value, trusted=False)` sketch.  The
full capability system replaces this because:

- `trusted` is binary; `sources` and `readers` are fine-grained.  The policy
  engine needs to distinguish between "came from email" and "came from calendar"
  to apply per-tool policies.
- `readers` must distinguish "no readers" (empty frozenset) from "all readers"
  (Public); a boolean cannot express this.

### C. `readers: frozenset[str] | None` (None = Public)

Using `None` as the "no restriction" sentinel was considered and rejected:

- `isinstance(readers, frozenset)` in the policy engine is cleaner than
  `readers is not None`.
- `None` conventionally means "absent" or "not set"; it does not communicate
  "unrestricted access".
- `_PublicType` gives a self-documenting `repr("Public")` in logs.

### D. Mutable dataclass + capability-mutation methods

Rejected.  Mutable capability fields create a TOCTOU risk: a policy check
could be evaluated against one capability state while the value is mutated
concurrently or between AST steps.  `frozen=True` eliminates this class of
bug entirely.

---

## References

- `camel/value.py` — implementation
- `camel/llm/protocols.py` — `QlLMBackend` protocol (consumer of `CaMeLValue`)
- `camel/llm/schemas.py` — `QResponse` (values wrapped after Q-LLM extraction)
- CaMeL paper §6.4 (Capabilities) and §6.5 (Security Policies)
- ADR-001: Q-LLM Isolation Contract and Schema Conventions
