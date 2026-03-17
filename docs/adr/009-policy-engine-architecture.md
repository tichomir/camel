# ADR 009 — Policy Engine Architecture

**Status:** Accepted
**Date:** 2026-03-17
**Deciders:** Software Architect
**Milestone:** 3 — Capabilities & Policies

---

## Context

The CaMeL interpreter must enforce security policies before every tool call.
The policies determine whether a particular combination of tool name and
argument capabilities is permitted.  Several design decisions needed to be
made:

1. How to represent the policy evaluation outcome (allow / deny).
2. How to store and look up policies per tool.
3. How multiple policies for the same tool compose.
4. How policy authors query capability metadata.
5. How deployment-specific policies are loaded without modifying core code.

---

## Decision 1 — SecurityPolicyResult as a sealed type

**Decision:** Represent the outcome as a sealed type hierarchy with two
concrete subclasses, `Allowed` and `Denied(reason)`.

**Alternatives considered:**

| Option | Rejected because |
|---|---|
| `bool` return value | No way to attach a denial reason; poor readability |
| `Enum` | Cannot carry a payload (the denial reason) without extra machinery |
| Exception-based denial | Mixes control flow with policy logic; harder to compose |
| `Optional[str]` (None=allow, str=reason) | Less explicit; mypy cannot enforce exhaustive handling |

**Why sealed type:** The `__init_subclass__` guard in `SecurityPolicyResult`
prevents third-party code from introducing a third outcome.  This means any
code that handles `Allowed` and `Denied` is provably exhaustive.  The `@final`
decorator on both concrete classes prevents further subclassing within the
module.

---

## Decision 2 — PolicyFn type alias

**Decision:** Define `PolicyFn = Callable[[str, Mapping[str, CaMeLValue]],
SecurityPolicyResult]`.

**Rationale:**

* Passing `tool_name` as the first argument enables a single policy function
  to handle multiple tools (glob-style or category-based policies) without
  requiring a separate registration per tool.
* `Mapping[str, CaMeLValue]` (read-only) prevents policies from accidentally
  mutating argument values.
* Synchronous return type enforces NFR-2 (no LLM calls in policy evaluation
  path).

---

## Decision 3 — All-must-allow composition with first-Denied short-circuit

**Decision:** `PolicyRegistry.evaluate` runs policies in registration order
and returns the first `Denied` immediately without evaluating remaining policies.

**Rationale:**

Adding a stricter policy must never weaken the overall security posture.
The all-must-allow model satisfies this monotonicity requirement: any
registered policy can veto a tool call, but no policy can override a veto
from another.

Short-circuiting on the first `Denied` is an optimisation that avoids
unnecessary computation.  Because policies are pure functions, skipping
subsequent policies does not affect correctness.

---

## Decision 4 — Implicit allow when no policies registered

**Decision:** If no policies are registered for a tool, `evaluate` returns
`Allowed()`.

**Rationale:**

An empty policy set means "no restrictions have been declared for this tool".
Defaulting to deny-all would prevent the system from functioning at all
during the development phase before policies are written.  The system
documentation makes clear that production deployments should register at
least a default policy for all tools.  Teams that need deny-by-default can
register a catch-all policy.

---

## Decision 5 — Three helper functions (is_trusted, can_readers_read_value, get_all_sources)

**Decision:** Provide three named helper functions as the idiomatic API for
policy authors to inspect `CaMeLValue` capability metadata.

**Rationale:**

Policy functions repeatedly need to answer the same questions:

* "Does this value come only from the user's query / the system itself?"
  → `is_trusted`
* "Is a specific principal allowed to receive this value?"
  → `can_readers_read_value`
* "What are all the origin systems that contributed to this value?"
  → `get_all_sources`

Named helpers make policy code self-documenting and decouple policy authors
from `CaMeLValue` internals.  If the internal field names change, only the
helpers need updating.

**`is_trusted` semantics — empty sources is not trusted:**

A value with an empty `sources` set is treated as untrusted.  This is a
conservative default: if we do not know the origin of a value, we cannot
assert it is safe.  An empty set is not the same as `{"CaMeL"}`.

---

## Decision 6 — Configuration-driven policy loading via CAMEL_POLICY_MODULE

**Decision:** The canonical mechanism for loading deployment-specific
policies is the `CAMEL_POLICY_MODULE` environment variable, consumed by
`PolicyRegistry.load_from_env()`.

**Contract:**

```
CAMEL_POLICY_MODULE=myapp.security.policies
```

The module must export:

```python
def configure_policies(registry: PolicyRegistry) -> None: ...
```

**Alternatives considered:**

| Option | Rejected because |
|---|---|
| TOML / YAML config file | Cannot express arbitrary policy logic; requires a custom DSL or is too restrictive |
| Subclassing `PolicyRegistry` | Creates tight coupling; obscures where policies come from |
| Hardcoded import in core | Violates the NFR that adding a new deployment does not require core code changes |
| Plugin entry-points | Adds packaging complexity without meaningful benefit over a simple env var |

**Why a Python module:** The PRD explicitly states that policies are expressed
as Python functions, because Python allows arbitrary logic without language
restrictions.  Pointing at a Python module preserves this flexibility while
keeping the loading mechanism dead-simple.

---

## Consequences

### Positive

* Exhaustive handling of `SecurityPolicyResult` is enforced by the type
  checker and at runtime.
* Policy evaluation is synchronous and contains no LLM calls, satisfying
  NFR-2.
* Multiple policies per tool compose safely with monotonic "all-must-allow"
  semantics.
* Deployment-specific policies require zero changes to core code.
* Helper functions produce readable, self-documenting policy code.

### Negative / Trade-offs

* The `__init_subclass__` guard in `SecurityPolicyResult` prevents extending
  the result type with a third variant (e.g. "Defer to user").  If a third
  variant is ever needed, this module must be changed.
* `load_from_env` is a module-level side effect triggered at object
  construction time; it is not suitable for environments where importing
  policy modules at startup is expensive.

---

## Related ADRs

| ADR | Topic |
|---|---|
| [002](002-camelvalue-capability-system.md) | CaMeLValue data structure and propagation rules |
| [003](003-ast-interpreter-architecture.md) | Interpreter — policy enforcement hook in tool dispatch |
| [007](007-execution-loop-orchestrator.md) | Orchestrator — PolicyViolationError handling |
