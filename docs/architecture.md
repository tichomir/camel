# CaMeL — System Architecture Reference

**Version:** 1.6 (Milestone 5 — Policy Testing Harness & Consent UX)
**Date:** 2026-03-18
**Source:** PRD v1.5 — *"Defeating Prompt Injections by Design"*, Debenedetti et al., arXiv:2503.18813v2

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Full Data / Control Flow Diagram](#2-full-data--control-flow-diagram)
3. [Component Descriptions](#3-component-descriptions)
4. [Isolation Invariants](#4-isolation-invariants)
5. [CaMeLValue & Capability Propagation](#5-camelvalue--capability-propagation)
6. [Interpreter Execution Modes](#6-interpreter-execution-modes)
7. [Exception Redaction Logic](#7-exception-redaction-logic)
8. [Retry Loop Mechanics](#8-retry-loop-mechanics)
9. [Execution Trace Recorder](#9-execution-trace-recorder)
10. [Security Model](#10-security-model)
11. [Policy Engine](#11-policy-engine) _(includes §11.7 Three-Tier Policy Governance — ADR-011; §11.8 Developer Testing Tools — ADR-012)_
12. [Capability Assignment Engine](#12-capability-assignment-engine)
13. [Reference Policy Library](#13-reference-policy-library)
14. [Enforcement Integration & Consent Flow](#14-enforcement-integration--consent-flow) _(includes §14.2 ConsentHandler & ConsentDecision — ADR-012; §14.3 Session Consent Cache)_
15. [SDK Layer — camel-security Public API](#15-sdk-layer--camel-security-public-api)
16. [Module Map (File Tree)](#16-module-map-file-tree)
17. [Related Documents](#17-related-documents)

---

## 1. System Overview

CaMeL (CApabilities for MachinE Learning) is a security wrapper for LLM-based agentic
systems.  It prevents **prompt injection attacks** — both control-flow and data-flow
variants — by enforcing strict separation between:

- **Trusted data:** user queries, P-LLM-generated literals, platform policies
- **Untrusted data:** tool return values, Q-LLM outputs, external API responses

The architecture has three active layers:

| Layer | Component | Role |
|---|---|---|
| Planning | Privileged LLM (P-LLM) | Generates the execution plan; never sees tool outputs |
| Execution | CaMeL Interpreter | Runs the plan, tracks capabilities, enforces policies |
| Data extraction | Quarantined LLM (Q-LLM) | Structured extraction from untrusted data; no tool access |

---

## 2. Full Data / Control Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                          User Query                              │
│                  (trusted — flows to P-LLM only)                 │
└─────────────────────────────┬────────────────────────────────────┘
                              │
                              ▼
            ┌─────────────────────────────────┐
            │        PLLMWrapper               │
            │  • System prompt with tool sigs  │
            │  • User query + context          │
            │  • NEVER receives tool values    │
            │  • Retries up to 10× on error    │
            └──────────────┬──────────────────┘
                           │ Pseudo-Python plan
                           │ (```python ... ```)
                           ▼
            ┌─────────────────────────────────┐
            │        CaMeLOrchestrator         │
            │  • Parses plan into AST stmts    │
            │  • Executes stmt-by-stmt         │
            │  • Outer retry loop (≤10)        │
            │  • Exception redaction engine    │
            │  • Execution trace recorder      │
            │  • print() → DisplayChannel      │
            └────────────┬───────┬────────────┘
                         │       │
         ┌───────────────┘       └──────────────────────────┐
         │ Unstructured data                                 │ Tool call (policy-guarded)
         ▼                                                   ▼
┌─────────────────────┐                    ┌────────────────────────────────┐
│     QLLMWrapper      │                    │         Tool Executor          │
│  • No tool access    │                    │  (email, drive, calendar, …)   │
│  • Schema injection  │                    │                                │
│  • Pydantic output   │                    │  Before each call:             │
│  • NotEnoughInfo →   │                    │    policy_engine.evaluate()    │
│    error (redacted)  │                    │  Returns CaMeLValue            │
└──────────┬──────────┘                    └────────────────────────────────┘
           │ Structured CaMeLValue                      │
           │ (tagged as untrusted)                       │ CaMeLValue
           └─────────────────────────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │    CaMeLInterpreter       │
                    │  • Variable store         │
                    │  • DependencyGraph        │
                    │  • NORMAL / STRICT mode   │
                    └──────────────────────────┘
```

**Legend:**
- Solid arrows: data flow
- Dashed boundary between P-LLM and interpreter: **isolation barrier** — no raw values cross this boundary upward

---

## 3. Component Descriptions

### 3.1 Privileged LLM (P-LLM)

**Module:** `camel/llm/p_llm.py` | **ADR:** [005](adr/005-p-llm-wrapper-architecture.md)

The P-LLM generates the agent's execution plan as pseudo-Python code.

| Property | Detail |
|---|---|
| Input | User query + tool signatures (as Python stubs) + optional user context |
| Output | A fenced `python` code block containing the execution plan |
| Isolation guarantee | **Never** receives tool return values or Q-LLM free-form output |
| Retry behaviour | Inner retry loop: up to 10 attempts on `CodeBlockNotFoundError` / `SyntaxError` |
| Re-planning input | On outer-loop retry: variable names already in scope (no raw values) + redacted error |

The `PLLMWrapper` enforces the isolation invariant at call time via `PLLMIsolationError`
if any `CaMeLValue` is detected in the user query or user context arguments.

### 3.2 Quarantined LLM (Q-LLM)

**Module:** `camel/llm/qllm.py` | **ADR:** [001](adr/001-q-llm-isolation-contract.md), [006](adr/006-q-llm-dynamic-schema-injection.md)

The Q-LLM extracts structured data from unstructured tool outputs.

| Property | Detail |
|---|---|
| Input | Raw unstructured string (tool output) + caller-specified Pydantic schema |
| Output | Schema-validated `CaMeLValue` (tagged as untrusted) |
| Isolation guarantee | No tool-calling capability; cannot produce free-form text to P-LLM |
| Schema injection | `have_enough_information: bool` field automatically injected into every schema |
| Insufficient data | Raises `NotEnoughInformationError` (NEIE) — `error_type` + call-site `lineno` forwarded to P-LLM; missing-information content is **never** included (M4-F7) |
| Annotation preservation | On NEIE, the interpreter snapshots its dependency graph and dep-ctx stack before the retry cycle and restores them before the regenerated plan executes (M4-F8) |

The Q-LLM is callable from within the execution plan via `query_quarantined_llm(data, Schema)`.

### 3.3 CaMeL Interpreter

**Module:** `camel/interpreter.py` | **ADR:** [003](adr/003-ast-interpreter-architecture.md)

Custom AST-walking interpreter for a restricted Python subset.

| Property | Detail |
|---|---|
| Input | Python source string (the execution plan) |
| Execution model | Recursive AST node interpretation via `ast` stdlib |
| Value wrapping | Every expression result is wrapped in `CaMeLValue` |
| Policy enforcement | Policy evaluated before every tool call; raises `PolicyViolationError` on denial |
| Unsupported constructs | Raises `UnsupportedSyntaxError` with `node_type` and `lineno` |
| Session state | Variable store persists across sequential `exec()` calls on the same instance |
| Exception redaction | Dependency-graph-aware taint check (M4-F6): failing statement's dependency graph consulted; untrusted-tainted messages replaced with `[REDACTED]` (`message=None` in `RedactedError`) |
| Loop-body propagation | In STRICT mode, exceptions inside a non-public-iterable `for`-loop body carry the iterable's dependency set into the retry cycle (M4-F9) |
| Redaction audit log | Every redaction decision emitted as `RedactionAuditEvent` to the security audit log (M4-F17) |
| Import blocking | Any `import` / `from … import` statement raises `ForbiddenImportError` at AST-walk time before any module loading occurs (M4-F10) |
| Builtin allowlist | Interpreter namespace restricted to 16 approved names; any other name access raises `ForbiddenNameError` with the offending identifier (M4-F11, M4-F14) |
| Timing exclusion | `time`, `datetime`, and all timing primitives excluded from namespace at construction time, closing the direct timing side-channel vector (M4-F12) |
| Allowlist config | Single source of truth: `camel/config/allowlist.yaml`; subject to mandatory security review gate before any modification (M4-F13) |

**Restricted builtin namespace (M4-F11) — approved names:**

| Category | Names |
|---|---|
| Type constructors | `list`, `dict`, `set`, `str`, `int`, `float`, `bool` |
| Type checking | `isinstance` |
| Sequence utilities | `len`, `range`, `enumerate`, `zip`, `sorted`, `min`, `max` |
| Output | `print` (routed to display channel, never to P-LLM context — M2-F10) |

Any name absent from the above 16-entry set — including `eval`, `exec`, `open`,
`__import__`, `__builtins__`, `time`, `datetime`, `os`, `sys` — raises
`ForbiddenNameError(name, lineno)` with the offending identifier included verbatim.

**Exception classes (M4-F10, M4-F14):**

```python
class ForbiddenImportError(Exception):
    module_name: str   # e.g. "os", "time"
    lineno: int        # 1-based source line of the import statement

class ForbiddenNameError(NameError):
    name: str          # exact identifier from ast.Name node
    lineno: int        # 1-based source line of the name access
```

Both exceptions are defined in `camel/exceptions.py` and exported via `camel/__init__.py`.
Violation events are written to the security audit log as `ForbiddenImportEvent` and
`ForbiddenNameEvent` dataclasses respectively (NFR-6).

**Restricted grammar — supported:**

```
Statements:  assignment (=, +=), if/elif/else, for…in, bare expression
Expressions: constants, Name, BinOp, UnaryOp, BoolOp, Compare,
             Call, Attribute, Subscript, List, Tuple, Dict, JoinedStr (f-string)
```

**Restricted grammar — explicitly unsupported:**

| Construct | Reason |
|---|---|
| `while` | Unbounded iteration; timing side-channel |
| `def`, `class`, `lambda` | Arbitrary code outside P-LLM plan |
| `import` / `from … import` | Blocked by `ForbiddenImportError` (M4-F10) before any module loading |
| `try` / `except` / `raise` | Exception side-channel |
| `with` / `async with` | Context manager side effects |
| Comprehensions | Hard to taint-track; lazy evaluation |
| `yield` / `await` | Async/generator models |
| `assert` / `del` | Out of scope |

### 3.4 LLM Backend Adapters

**Module:** `camel/llm/adapters/` | **ADR:** [005](adr/005-p-llm-wrapper-architecture.md)

Provider-agnostic interface via the `LLMBackend` Protocol:

```python
class LLMBackend(Protocol):
    async def generate(self, messages: list[Message], **kwargs: Any) -> str: ...
    async def generate_structured(
        self, messages: list[Message], schema: type[BaseModel]
    ) -> BaseModel: ...
```

Concrete adapters: `ClaudeBackend` (Anthropic), `GeminiBackend` (Google).  Backends are
interchangeable without code changes beyond configuration.

### 3.5 Execution Loop Orchestrator

**Module:** `camel/execution_loop.py` | **ADR:** [007](adr/007-execution-loop-orchestrator.md)

Wires P-LLM, interpreter, Q-LLM, and tool dispatch into a complete execution loop.

See §8 (Retry Loop) and §9 (Execution Trace) for mechanics.

### 3.6 Isolation Test Harness

**Module:** `tests/harness/` | **ADR:** [008](adr/008-isolation-test-harness-architecture.md)

Automated verification of the three isolation invariants across 50+ execution runs.
See §4 (Isolation Invariants).

---

## 4. Isolation Invariants

Three invariants are enforced by design and verified by the automated test harness:

### Invariant 1 — No tool output in P-LLM context

**Statement:** No raw value returned by any tool call may appear in any message sent
to the P-LLM's `LLMBackend.complete()` method.

**Mechanism:** The `CaMeLInterpreter` stores all tool return values as `CaMeLValue`
objects in its variable store.  The `PLLMWrapper` only receives variable *names* (not
values) during retry prompt construction.  `PLLMIsolationError` is raised if a
`CaMeLValue` is detected in P-LLM inputs.

**Verification:** The `RecordingBackend` intercepts all `complete()` calls and asserts
no tool-return content appears in any P-LLM prompt across 50 execution runs
(`tests/test_isolation_harness.py`).

### Invariant 2 — No free-form Q-LLM output in P-LLM context

**Statement:** The Q-LLM may only communicate with the P-LLM via Pydantic schema fields —
never via free-form text strings.

**Mechanism:** `QLLMWrapper.extract()` validates all Q-LLM responses against the declared
schema.  Unvalidated responses are rejected before they can reach the interpreter or P-LLM.

**Verification:** Automated tests confirm `QLLMWrapper` rejects unvalidated output and
that schema-validated values are tagged as untrusted before entering the interpreter store.

### Invariant 3 — Redaction completeness

**Statement:** Exception messages from failures involving any untrusted-dependency variable
must never be forwarded to the P-LLM.

**Mechanism:** The `ExceptionRedactor` classifies each exception against the interpreter
store snapshot.  Messages are omitted for untrusted-dependency and
`NotEnoughInformationError` cases.

**Verification:** 10 adversarial cases with crafted exception messages confirm redaction
across all three redaction cases (`tests/test_redaction_completeness.py`).

---

## 5. CaMeLValue & Capability Propagation

**Module:** `camel/value.py` | **ADR:** [002](adr/002-camelvalue-capability-system.md)

Every runtime value in the interpreter is wrapped in `CaMeLValue`:

```python
@dataclass(frozen=True)
class CaMeLValue:
    value: Any                        # the underlying Python value (access via .raw property)
    sources: frozenset[str]           # origin tool(s) / "User literal" / "CaMeL"
    inner_source: str | None          # sub-field within a tool response
    readers: frozenset[str] | Public  # authorised recipients, or Public sentinel

    @property
    def raw(self) -> Any:             # preferred accessor — do not use .value directly
        return self.value
```

### Propagation Rules

| Operation | Rule |
|---|---|
| Assignment `c = a` | `c.sources = a.sources`, `c.readers = a.readers` |
| Binary op `c = a op b` | `c.sources = a.sources ∪ b.sources`, `c.readers = a.readers ∩ b.readers` |
| List construction `[a, b]` | Union of all element sources and intersection of readers |
| Dict construction `{k: v}` | Union of all key and value sources |
| Subscript `a[i]` | `sources = a.sources ∪ i.sources`, readers from `a` |
| Tool return | Annotated by capability annotation function; defaults to `sources={tool_name}` |
| Q-LLM output | Tagged as untrusted: `sources = q_llm_source_tag` |
| User literal | `sources={"User literal"}`, `readers=Public` |

The `Public` sentinel indicates no reader restriction — data may flow to any recipient.

---

## 6. Interpreter Execution Modes

**Module:** `camel/dependency_graph.py`, `camel/interpreter.py` | **ADR:** [004](adr/004-dependency-graph-architecture.md)

CaMeL supports two dependency-tracking modes.  **`ExecutionMode.STRICT` is the default**
as of v0.4.0 (Milestone 4, M4-F5).  NORMAL mode requires explicit opt-in:

```python
from camel import CaMeLInterpreter, ExecutionMode

# Default — STRICT mode; no argument needed
interp = CaMeLInterpreter(tools=my_tools)

# Explicit NORMAL mode opt-in (debugging / non-security-sensitive scenarios only)
interp = CaMeLInterpreter(tools=my_tools, mode=ExecutionMode.NORMAL)
```

| Mode | Default | Dependency tracking | Side-channel mitigation |
|------|---------|---------------------|------------------------|
| `STRICT` | **Yes** (v0.4.0+) | Data + control-flow (if-test, for-iterable, post-Q-LLM) | Closes timing/control-flow channels |
| `NORMAL` | No — explicit opt-in | Data assignments only | No control-flow taint |

### NORMAL Mode

Dependencies are recorded only via **direct data assignment**.  Control-flow constructs
(`if`, `for`) and Q-LLM calls do not add dependency edges.

```python
# NORMAL: x's dependency on flag is NOT recorded
flag = get_secret_flag()   # sources: {"get_secret_flag"}
x = 1
if flag:
    x = 2                  # x.deps = {} — no edge from flag
```

Use NORMAL mode only when control-flow taint is not a security concern.

### STRICT Mode

In STRICT mode, variables referenced in `if` tests, `for` iterables, and statements
following a `query_quarantined_llm()` call become dependencies of **every variable
assigned within those blocks**.

```python
# STRICT: x's dependency on flag IS recorded
flag = get_secret_flag()   # sources: {"get_secret_flag"}
x = 1
if flag:
    x = 2                  # x.deps = {"flag"} — taint propagated
```

**Why STRICT matters:** Without it, an adversary can learn the value of a private boolean
by observing which branch the agent takes (control-flow side-channel, PRD §7.3).  STRICT
mode closes this by ensuring that any variable assigned inside a branch carries the
branch condition as a dependency — which is then checked by the security policy engine.

### STRICT Mode Propagation Rules

Three dependency-propagation rules apply exclusively in STRICT mode (delivered in Milestone 4):

| Rule | ID | AST Node | Dependency propagated to |
|------|----|----------|--------------------------|
| For-loop iterable | M4-F1 | `ast.For` | Every assignment inside the loop body (including nested blocks) |
| If/else test | M4-F2 | `ast.If` | Every assignment in both the `body` and `orelse` branches |
| Post-Q-LLM remainder | M4-F3/F4 | `ast.Call` (`query_quarantined_llm`) | Every assignment in the same statement list after the Q-LLM call |

**M4-F1 — For-loop iterable propagation:**  The capability set and variable-dependency set
of the iterable expression are merged into every assignment inside the loop body.  This
prevents an adversary who controls iteration order from influencing downstream tool arguments
without detection.

**M4-F2 — If/else test propagation:**  The capability set and variable-dependency set of
the `if` condition are merged into every assignment in both the true and false branches.
This closes the timing/branch-observation side channel (PRD §7.3).

**M4-F3/F4 — Post-Q-LLM remainder propagation:**  When `query_quarantined_llm()` is called
anywhere in a statement list, STRICT mode activates a "remainder flag" for the rest of that
code block.  Every subsequent assignment in the same block (and nested blocks after the call)
inherits the Q-LLM result's `CaMeLValue` as additional context capabilities.  The flag is
scoped to the current `_exec_statements` frame and resets on block exit.

**Dependency-graph mutation table:**

| Rule | Graph mutation |
|------|---------------|
| M4-F1 | `record(target, direct ∪ iter_deps ∪ ctx_deps)` for all assignments in loop body |
| M4-F2 | `record(target, direct ∪ test_deps ∪ ctx_deps)` for all assignments in both branches |
| M4-F3/F4 | `record(target, direct ∪ qllm_deps ∪ ctx_deps)` for all subsequent assignments in block |

### Dependency Graph API

```python
from camel import get_dependency_graph

dg = get_dependency_graph(interpreter, "variable_name")
dg.direct_deps      # frozenset[str] — immediate upstream variable names
dg.all_upstream     # frozenset[str] — transitive upstream (recursive)
```

---

## 7. Exception Redaction Logic

**Module:** `camel/execution_loop.py` — `ExceptionRedactor` | **ADR:** [007](adr/007-execution-loop-orchestrator.md)

When an exception occurs during plan execution, the orchestrator must decide what
information is safe to forward to the P-LLM for re-planning.  The redaction decision
is based on data-provenance trust, using the **dependency graph** as the authoritative
taint record (Milestone 4, M4-F6 through M4-F9, M4-F17).

### Three Redaction Cases

#### Case 1 — Trusted-origin exception

**Condition:** No variable referenced by the failing statement has any upstream source
outside `{"User literal", "CaMeL"}` (checked via the full dependency graph — M4-F6).

**Information forwarded to P-LLM:**

```
Error type:    SyntaxError
Location:      line 7
Message:       invalid syntax
```

Full error message is safe — it cannot echo attacker-controlled content.

**Example:** `NameError` on a variable the P-LLM invented without any tool data in scope.

---

#### Case 2 — Untrusted-dependency exception

**Condition:** The dependency graph for any variable referenced in the failing statement
contains an upstream source outside `{"User literal", "CaMeL"}` (M4-F6 taint check).

**Information forwarded to P-LLM:**

```
Error type:    TypeError
Location:      line 12
```

Error **message is omitted** (`message=None` in `RedactedError`; `[REDACTED]` appears in
the audit log only).  An adversary can craft tool return values that, when processed,
produce an exception whose message echoes the crafted content back to the P-LLM.

**Example:** A document containing `"ignore previous instructions"` that causes a `TypeError`
during processing — the error message must never reach the P-LLM.

---

#### Case 3 — `NotEnoughInformationError`

**Condition:** The exception is `NotEnoughInformationError` (NEIE) raised by the Q-LLM
wrapper (M4-F7).

**Information forwarded to P-LLM:**

```
Error type:    NotEnoughInformationError
Location:      line 9   ← call-site line number (M4-F7)
Advisory:      The call to query_quarantined_llm() at line 9 reported that the
               provided context did not contain enough information to populate
               the requested schema.  Consider restructuring the query or
               passing additional context variables.
```

No message content.  The Q-LLM operates on untrusted data — even the reason why
information was insufficient could contain adversary-controlled content.  The advisory
sentence contains only the line number and a fixed string (no untrusted data).

---

### Exception Hardening — M4-F8 & M4-F9

#### M4-F8 — STRICT Mode Annotation Preservation Across NEIE Re-generation

When NEIE is raised and the orchestrator triggers P-LLM re-generation, the interpreter's
STRICT mode state is preserved:

1. The interpreter snapshots its `DependencyGraph` and `_dep_ctx_stack` into the
   `AcceptedState` object (new fields: `dependency_graph_snapshot`, `dep_ctx_stack_snapshot`).
2. After the regenerated plan is received from the P-LLM, the snapshot is restored into
   the interpreter before execution begins.
3. This ensures all STRICT mode taint annotations accumulated before the failing Q-LLM
   call are available to the regenerated code.

#### M4-F9 — Loop-Body Exception STRICT Propagation

When an exception originates inside a `for`-loop body and all three conditions are met:

1. The loop is in `ExecutionMode.STRICT`.
2. The iterable's `CaMeLValue` has at least one non-trusted source or non-`Public` readers.
3. The exception propagates out of `_exec_For`.

Then `_exec_For` attaches the iterable's dependency set (`__loop_iter_deps__`) and
capability context (`__loop_iter_caps__`) to the exception before re-raising.  The
orchestrator reads these fields and pre-seeds the interpreter's `_dep_ctx_stack` before
executing the regenerated plan, ensuring the iterable's taint is never silently dropped
across retry cycles.

---

### `RedactedError` Data Model

```python
@dataclass(frozen=True)
class RedactedError:
    error_type: str                                              # always present
    lineno:     int | None                                       # None when redacted; call-site lineno for NEIE (M4-F7)
    message:    str | None                                       # None when redacted; never set for NEIE
    trust_level: Literal["trusted", "untrusted",
                          "not_enough_information"]
```

### `RedactionAuditEvent` Schema (M4-F17)

Emitted by `ExceptionRedactor.classify()` for every exception processed — regardless
of whether redaction was applied.  Injected into the audit log via
`ExceptionRedactor.__init__(audit_log=...)`.

```python
@dataclass(frozen=True)
class RedactionAuditEvent:
    timestamp: str                          # ISO-8601 UTC
    line_number: int | None
    redaction_reason: str                   # "untrusted_dependency" | "not_enough_information"
                                            # | "loop_body_exception" | "none"
    dependency_chain: list[tuple[str, str]] # [(var_name, source_label), ...] — max 50 entries
    trust_level: str                        # "trusted" | "untrusted" | "not_enough_information"
    error_type: str
    redacted_message_length: int            # length of original message before redaction, or 0
    m4_f9_applied: bool
```

### Trust Classification Algorithm (M4-F6 — Dependency-Graph-Aware)

```
classify(exc, interpreter):
    # M4-F7: NEIE is always fully redacted
    if isinstance(exc, NotEnoughInformationError):
        lineno = exc.__lineno__ or getattr(exc, "lineno", None)
        emit RedactionAuditEvent(redaction_reason="not_enough_information", ...)
        → RedactedError(type, lineno, msg=None, trust="not_enough_information")

    # M4-F6: dependency-graph-aware taint check
    failing_vars = interpreter.get_variables_at_line(exc.lineno) or store.keys()
    for var in failing_vars:
        dep_graph = interpreter.get_dependency_graph(var)
        for upstream_var in dep_graph.all_upstream():
            cv = store.get(upstream_var)
            if cv and any(src not in TRUSTED_SOURCES for src in cv.sources):
                emit RedactionAuditEvent(redaction_reason="untrusted_dependency", ...)
                → RedactedError(type, lineno, msg=None, trust="untrusted")
        cv = store.get(var)
        if cv and any(src not in TRUSTED_SOURCES for src in cv.sources):
            emit RedactionAuditEvent(redaction_reason="untrusted_dependency", ...)
            → RedactedError(type, lineno, msg=None, trust="untrusted")

    emit RedactionAuditEvent(redaction_reason="none", ...)
    → RedactedError(type, lineno, msg=str(exc), trust="trusted")
```

`TRUSTED_SOURCES = frozenset({"User literal", "CaMeL"})`.

Classification is **dependency-graph-aware** (M4-F6): only variables referenced in the
failing statement (and their transitive upstream) are inspected, rather than the entire
store.  This is more precise than the earlier coarse store-scan approach, but retains
a conservative bias — any untrusted upstream source triggers full redaction.

---

## 8. Retry Loop Mechanics

**Module:** `camel/execution_loop.py` — `CaMeLOrchestrator` | **ADR:** [007](adr/007-execution-loop-orchestrator.md)

### State Machine

```
INITIAL
  │
  ▼
PLAN_GENERATION ──── PLLMWrapper.generate_plan() ─────────────────────►
  │   ▲                (inner retry ≤ 10 for CodeBlockNotFoundError / SyntaxError)
  │   │
  │   └── PLLMRetryExhaustedError ─────────────────────────────► FAILED
  │
  ▼
EXECUTING ──── CaMeLInterpreter.exec() per statement ──────────────────►
  │
  │──── all statements complete without exception ────────────► SUCCEEDED
  │
  │──── exception raised
  ▼
EXCEPTION_RECEIVED
  │
  ▼
REDACTING ──── ExceptionRedactor.classify() ──────────────────────────►
  │
  │──── loop_attempts >= max_loop_retries ───────────────────── FAILED
  │                                                              (MaxRetriesExceededError)
  ▼
RETRY_PROMPT_BUILD ──── RetryPromptBuilder.build() ───────────────────►
  │
  ▼
PLAN_REGENERATION ──── PLLMWrapper.generate_plan() ───────────────────►
  │
  │──── PLLMRetryExhaustedError ─────────────────────────────── FAILED
  │
  └──► EXECUTING  (continue with regenerated remaining code)
```

### Two Independent Retry Ceilings

| Loop | Default ceiling | Exhaustion error |
|---|---|---|
| **Outer loop** (execution failures) | 10 | `MaxRetriesExceededError` |
| **Inner loop** (P-LLM malformed output) | 10 | `PLLMRetryExhaustedError` |

The two ceilings are independent.  In the worst case, the total number of P-LLM calls
before failure is `max_loop_retries × (max_inner_retries + 1)`.

### `MaxRetriesExceededError`

```python
class MaxRetriesExceededError(Exception):
    attempts:   int              # outer-loop retries exhausted
    last_error: RedactedError | None
```

Raised when `loop_attempts >= max_loop_retries` and the plan has not yet completed
successfully.

### Partial Re-Execution Prompt (M2-F14)

On retry, the P-LLM is **not** asked to regenerate the full plan.  It receives:

1. The redacted error (type + optional line + optional message)
2. The **names** of variables already defined in the interpreter store (no raw values)
3. A directive to generate only the **remaining** statements

This avoids re-triggering already-completed side-effecting tool calls (e.g. sending an
email twice) and saves tokens.

```
## PARTIAL RE-EXECUTION REQUEST

The following variables are already defined — treat them as opaque handles;
do NOT recompute them:

  email_1, recipient_addr, subject_line

The plan failed at the next step with:

  Error type:   TypeError
  Location:     line 8
  Message:      (redacted)

Generate ONLY the remaining steps starting from after the last successful
statement.  Emit a single corrected ```python``` code block.
```

---

## 9. Execution Trace Recorder

**Module:** `camel/execution_loop.py` — `TraceRecorder` | **ADR:** [007](adr/007-execution-loop-orchestrator.md)

Every successful tool call is appended to an ordered `ExecutionTrace`.

### Data Model

```python
@dataclass(frozen=True)
class TraceRecord:
    tool_name:       str                    # registered tool name
    args:            dict[str, Any]         # raw argument values (CaMeLValue.raw)
    memory_snapshot: dict[str, CaMeLValue]  # full store after the call (with caps)

ExecutionTrace = list[TraceRecord]
```

### Separation from DisplayChannel

| Channel | Contents | Consumer |
|---|---|---|
| `ExecutionTrace` | Successful tool call records | Security audit log |
| `DisplayChannel` | Values passed to `print()` in the plan | User / UI |

`print()` calls in the execution plan are intercepted and routed to the `DisplayChannel`
implementation (default: `StdoutDisplayChannel`).  They are **never** included in the
execution trace.

### `ExecutionResult`

Returned by `CaMeLOrchestrator.run()` on success:

```python
@dataclass(frozen=True)
class ExecutionResult:
    trace:         ExecutionTrace        # ordered tool call records
    print_outputs: list[CaMeLValue]      # print() outputs from plan
    final_store:   dict[str, CaMeLValue] # interpreter store snapshot
    loop_attempts: int                   # outer-loop retries consumed (0 = first attempt)
```

---

## 10. Security Model

### 10.1 PI-SEC Formal Security Game (PRD §7.1)

Given a user prompt, define **Ω_prompt** as the set of allowed `(tool, args, memory_state)`
tuples.  An **adversary wins** if they can construct an initial memory state `mem*` that
causes the agent to produce a trace containing any action outside Ω_prompt.

CaMeL's design prevents this by ensuring:

1. **Control flow** (the execution plan) is generated exclusively from the trusted user query
   via the P-LLM, which never observes tool return values.
2. **Data flow** (values passed to tool arguments) is governed by capability tags and security
   policies evaluated synchronously before every tool call.
3. **Tool selection at runtime** is guarded against data-to-control-flow escalation: the
   interpreter detects, via `DataToControlFlowWarning` (M4-F15), any attempt to invoke a
   callable whose identity was derived from untrusted data, and blocks the call via the
   elevated consent gate (M4-F16) before any policy evaluation or tool dispatch occurs.

### 10.2 Trusted vs Untrusted Boundary (PRD §7.2)

| Trusted | Untrusted |
|---|---|
| User query | Tool return values |
| P-LLM generated literals (including literal tool names in code) | Q-LLM outputs (until capability-validated) |
| Platform security policies | Web pages, emails, documents |
| Interpreter-derived transformations (`"CaMeL"` source) | External API responses |
| Direct tool-name tokens in P-LLM-generated code | Variables whose value was derived from tool output or Q-LLM results |

**Escalation boundary (M4-F15/F16):** The `func` operand of any call expression is
inspected at runtime.  If it resolves to a `CaMeLValue` with sources outside
`{"User literal", "CaMeL"}`, the call is treated as a data-to-control-flow escalation
attempt and blocked by the elevated consent gate regardless of existing policy outcomes.

### 10.3 Attack Vectors Mitigated

| Attack | CaMeL defence |
|---|---|
| **Control flow hijack** — malicious content overrides the plan | P-LLM isolation: plan generated from trusted query only; tool values never reach P-LLM |
| **Data flow manipulation** — injected arguments to legitimate tool calls | Capability tagging + policy engine: untrusted-sourced values require explicit policy approval before flowing to tool arguments |
| **Data-to-control escalation** — untrusted data drives tool selection | P-LLM generates all branching logic (structural guarantee); M4-F15/F16 runtime detection and elevated consent gate blocks any attempt to call a variable-derived callable whose provenance includes untrusted sources |
| **Exception side-channel** — error content leaks private values to P-LLM | Exception redaction engine strips messages from untrusted-dependency failures |
| **Timing / control-flow side-channel** — branch observation leaks private booleans | STRICT mode propagates branch condition as dependency to all inner-block variables |

### 10.3a Side-Channel Attack Classes — Formal Table

The following table formally documents all three side-channel attack classes addressed in
Milestone 4, together with the specific mitigations delivered and their validation status.
This table satisfies PRD §11 success metric traceability.

| Attack Class | Description | CaMeL Mitigation(s) | Feature IDs | Test Class | Pass Rate (M4 suite) |
|---|---|---|---|---|---|
| **Indirect inference via loop count** | An adversary who can observe which tool calls are made (or measure total execution time) infers the value of a private variable by counting loop iterations when an untrusted iterable controls the loop body. | STRICT mode merges the iterable's capability and dependency set into every variable assigned inside the loop body. The policy engine sees the untrusted provenance before any downstream tool call, blocking exfiltration. | M4-F1 | `tests/side_channel/test_loop_count_inference.py` | **100%** (10/10) ✅ |
| **Exception-based bit leakage** | An adversary crafts tool return values that, when processed, trigger exceptions whose messages echo adversary-controlled content back to the P-LLM via the retry prompt, crossing the trusted/untrusted boundary. | (1) Dependency-graph-aware taint check: exception messages replaced with `None` when any upstream source is outside `{"User literal", "CaMeL"}`. (2) `NotEnoughInformationError` content stripped entirely; only error type and call-site line number forwarded. (3) Loop-body exception STRICT propagation: iterable taint pre-seeded into the regenerated plan's dependency context. | M4-F6, M4-F7, M4-F9, M4-F17 | `tests/side_channel/test_exception_bit_leakage.py` | **100%** (17/17) ✅ |
| **Timing primitive exclusion** | If the interpreter exposes `time.perf_counter`, `time.sleep`, `datetime.now`, or any other timing primitive, an adversary can encode private values into time-delayed tool calls or measure elapsed time to infer branch outcomes. | All import statements raise `ForbiddenImportError` at AST-walk time. The interpreter namespace is restricted to 16 approved builtins; any other name access raises `ForbiddenNameError`. The `time` module and all timing-related names are unconditionally excluded from the namespace, defined in the central auditable `allowlist.yaml`. | M4-F10, M4-F11, M4-F12, M4-F13, M4-F14 | `tests/side_channel/test_timing_primitive_exclusion.py` | **100%** (62/62) ✅ |

**Overall side-channel test pass rate: 100% (89/89) — PRD §11 target MET.**
See the full test execution report: `docs/reports/milestone4_side_channel_test_report.md`.

### 10.4 Out-of-Scope Threats (PRD §7.3)

- Text-to-text manipulation with no data/control flow consequence
- Prompt-injection-induced phishing (link-click attacks)
- Fully compromised user prompt
- **Timing side-channels (partial mitigation — NG4):** The `time` module and all timing
  primitives (`time.sleep`, `time.perf_counter`, `datetime.now`, etc.) are excluded
  from the interpreter namespace as of M4-F12, eliminating the *direct* timing
  side-channel vector.  **Milestone 4 outcome:** The direct timing primitive vector is
  closed; the timing primitive exclusion test class passes at 100% (62/62).  Residual
  timing channels — indirect iteration-count channels (partially addressed by M4-F1
  STRICT propagation), CPython instruction-dispatch variance, tool-implementation timing
  leakage, and OS-level process-time observation — remain out of scope per NG4 (PRD §3.2).
  **Explicit caveat:** Full timing side-channel immunity is not guaranteed in all
  configurations; this is an explicit non-goal of CaMeL.  See
  `docs/security_hardening_allowlist.md §9` for the full residual risk catalogue.
- Formal verification of interpreter correctness (future work — FW-2; see §10.7)

### 10.5 Known Limitations & Residual Risks

_Updated for Milestone 4 completion (Version 1.4, 2026-03-17)._

| # | Limitation | Severity | Milestone 4 Outcome | Residual Caveat |
|---|---|---|---|---|
| L1 | Data-requires-action failure: P-LLM cannot plan actions that depend on reading untrusted data | Medium | Documented design constraint; no change in M4 | Future: nested P-LLM tool (FW-4) |
| L3 | **Exception-based side channel (residual):** Primary vectors — loop-body taint (M4-F1), exception message redaction (M4-F6), NEIE stripping (M4-F7), loop annotation propagation (M4-F9) — are all implemented and validated at 100% (17/17 tests pass). | Low | ✅ Primary vectors closed by M4-F6, M4-F7, M4-F9, M4-F17. Side-channel test class 2 passes at 100%. | **Residual:** Deeply nested tool call exception chains — where an exception propagates through multiple tool call frames before reaching the interpreter's exception handler — remain a documented residual risk not covered by the M4 mitigations. See `docs/design/milestone4-exception-hardening.md §9.2`. |
| L4 | User fatigue from policy / escalation denials | **Low–Medium** (mitigated) | ✅ **Three-tier governance (ADR-011, Milestone 5) formally mitigates L4** by distributing policy authority across Platform, Tool-Provider, and User tiers with explicit precedence rules and a `non_overridable` flag. User-tier policies allow end-users to pre-approve common actions, reducing consent-prompt frequency. Platform policies with `non_overridable=False` still surface consent prompts, but `non_overridable=True` rules bypass the consent path entirely — preventing fatigue-driven bypasses of critical controls. Elevated consent (M4-F16) provides clear escalation reason for data-to-control violations. | Granular tier assignment and the `non_overridable` selection criteria in `docs/policy_authorship_guide.md` provide deployment guidance to minimise denial rates on well-annotated tools. |
| L5 | Token cost overhead (~2.82× input tokens vs native tool-calling) | Low-Medium | No change in M4 | Expected to decrease as models follow subset spec more closely |
| L6 | **ROP-analogue action chaining:** An adversary may chain individually-approved tool calls to produce a collectively malicious outcome. | Medium-High | ✅ **Single-step escalation blocked** by M4-F15 (detection) and M4-F16 (elevated consent gate). | **Residual:** The ROP-analogue scenario — where each individual tool call is to a statically-named (trusted) tool but the *sequence* of calls collectively exfiltrates data — is **not** eliminated by M4-F15/F16. STRICT mode (M4-F1/F2) propagates untrusted branch conditions to all inner-block assignments, but an adversary constructing a sequence of fully-legitimate, individually policy-approved calls can still produce a collectively malicious trace. Future work: FW-6 (action-sequence anomaly detection via dependency graph analysis). |
| NG4 | **Partial timing mitigation (explicit non-goal):** CaMeL does not guarantee side-channel immunity in all configurations. | Low | ✅ **Direct timing primitive vector closed** by M4-F12 (timing names excluded from namespace). Side-channel test class 3 (timing primitive exclusion) passes at 100% (62/62 tests). | **Residual / explicit caveat:** Indirect timing channels — iteration-count channels (wall-clock observable without any timing call), CPython instruction-dispatch variance, tool-implementation timing leakage, OS-level process-time observation — remain out of scope per PRD §3.2 NG4. Full timing immunity is not a CaMeL guarantee. See `docs/security_hardening_allowlist.md §9` for the complete residual risk register. |

**L6 detail (updated for M4):** M4-F15 detects and M4-F16 blocks the case where a *single*
tool call's function identity derives from untrusted data.  The ROP-analogue scenario — where
each individual tool call is to a statically-named (trusted) tool but the sequence of calls,
driven by untrusted branching, collectively exfiltrates data — is **not** eliminated by M4-F15/F16.
STRICT mode (M4-F1/F2) ensures all inner-block assignments carry the correct taint, but an
adversary crafting a sequence of fully-legitimate, individually policy-approved calls can still
construct a collectively malicious trace.  This is the subject of future work FW-6.

**NG4 detail (updated for M4):** The allowlist enforcement layer (M4-F10–F14) and the STRICT mode
propagation rules (M4-F1–F4) together provide defence in depth against timing side channels, but
they are not a complete solution.  The direct timing primitive vector is closed.  The residual
risks (iteration-count channels, CPython variance, tool timing leakage, OS-level observation) are
acknowledged, documented, and consistent with the explicit non-goal NG4 stated in the PRD.

### 10.6 Security Metrics

_Milestone 4 outcomes recorded per PRD §11 success metric targets._

| Metric | PRD §11 Target | Achieved | Status |
|---|---|---|---|
| AgentDojo task success rate (CaMeL vs native) | ≤10% degradation | **77%** vs 84% (8.3% degradation) | ✅ Met |
| Prompt injection ASR on AgentDojo | 0 | **0** | ✅ Met |
| Data exfiltration events blocked | 100% of policy-covered scenarios | **100%** | ✅ Met |
| **Side-channel test pass rate — loop count inference** | 100% for implemented mitigations | **100%** (10/10 tests) | ✅ Met |
| **Side-channel test pass rate — exception bit leakage** | 100% for implemented mitigations | **100%** (17/17 tests) | ✅ Met |
| **Side-channel test pass rate — timing primitive exclusion** | 100% for implemented mitigations | **100%** (62/62 tests) | ✅ Met |
| **Overall side-channel test pass rate** | 100% for implemented mitigations | **100%** (89/89 tests) | ✅ Met |

Full test execution report: `docs/reports/milestone4_side_channel_test_report.md`.

### 10.7 Open Questions & Future Work

_Updated for Milestone 4 completion (Version 1.4, 2026-03-17)._

| # | Topic | Milestone 4 Finding |
|---|---|---|
| FW-1 | **Alternative execution language** | Python's exception model creates residual side-channel risks (L3) that the M4 exception hardening features reduce but cannot fully eliminate. Adversarial exception triggering via deeply nested tool call chains remains a documented residual risk. The Python exception model's implicit control-flow semantics make it structurally harder to achieve exception-side-channel immunity compared to languages with explicit error handling (e.g., Haskell's `Either`/`Result` types or Rust's `Result<T, E>`). **Milestone 4 strengthens the case for evaluating a Haskell or Rust DSL for the P-LLM code plan** as an alternative execution target that would eliminate the exception side-channel class entirely at the language level, rather than requiring layered mitigation. Evaluate feasibility as part of Milestone 5 planning. |
| FW-2 | **Formal verification** | The CaMeL interpreter has grown substantially in Milestone 4 — STRICT mode propagation rules (M4-F1–F4), exception redaction engine (M4-F6–F9, M4-F17), allowlist enforcement (M4-F10–F14), and escalation detection (M4-F15–F16, M4-F18) all add non-trivial enforcement logic. **Milestone 4 increases the value of formal verification:** the more complex the enforcement logic, the greater the risk that an edge case in the implementation diverges from the intended security property. A machine-verified proof of interpreter correctness (e.g., using Coq or Lean) would close this gap and provide provable guarantees that complement the test suite. Recommended as a medium-priority research investment for Milestone 5. |
| FW-5 | **Multi-party policy governance** | ✅ **Resolved in Milestone 5 (ADR-011).** The three-tier `TieredPolicyRegistry` + `PolicyConflictResolver` design is implemented in `camel/policy/governance.py`. It introduces explicit `PolicyTier` authorship (PLATFORM → TOOL_PROVIDER → USER), deterministic conflict resolution with per-tier short-circuiting, the `non_overridable` flag on Platform policies, and `MergedPolicyResult` with a full `TierEvaluationRecord` audit trail. The `non_overridable` flag closes the consent-fatigue attack vector on critical controls while preserving user agency on soft guardrails. Full guidance: `docs/policy_authorship_guide.md`. |
| FW-6 | **ROP-analogue attack detection** | M4-F15/F16 close the single-step data-to-control escalation vector. The remaining challenge (L6 residual) is detecting whether a sequence of individually-approved tool calls constitutes a collectively malicious trace. Dependency graph analysis — tracking whether the combined data flow graph of a completed trace contains paths from untrusted sources to sensitive sinks — is a candidate technique. Recommend scoping a detection algorithm and prototype as part of Milestone 5. |

---

## 11. Policy Engine

**Module:** `camel/policy/` | **ADR:** [009](adr/009-policy-engine-architecture.md)

The policy engine is the *enforcement layer* that the interpreter consults
before every tool call.  It is synchronous, deterministic, and contains no
LLM calls (NFR-2).

### 11.1 SecurityPolicyResult — sealed type

Two concrete variants; no other subclasses are permitted:

```python
class Allowed(SecurityPolicyResult):
    """The tool call may proceed."""

class Denied(SecurityPolicyResult):
    reason: str   # human-readable denial reason, safe for audit logs
```

### 11.2 PolicyFn — type alias

```python
PolicyFn = Callable[
    [str, Mapping[str, CaMeLValue]],   # (tool_name, kwargs)
    SecurityPolicyResult,
]
```

Policy functions must be **pure, synchronous, and deterministic**.  No I/O,
no LLM calls.

### 11.3 PolicyRegistry

```python
class PolicyRegistry:
    def register(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
    ) -> PolicyFn: ...
    # Also works as a decorator: @registry.register("send_email")

    def evaluate(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult: ...

    @classmethod
    def load_from_env(cls) -> PolicyRegistry: ...
```

**Composition rule:** All registered policies for a tool are required to
return `Allowed`.  The first `Denied` short-circuits — remaining policies
are not evaluated.  If no policies are registered for a tool, `Allowed()` is
returned (implicit allow).

### 11.4 Helper functions

| Function | Signature | Purpose |
|---|---|---|
| `is_trusted` | `(CaMeLValue) -> bool` | `True` if sources ⊆ `{"User literal", "CaMeL"}` |
| `can_readers_read_value` | `(CaMeLValue, str) -> bool` | `True` if `reader` is in `value.readers` or readers is `Public` |
| `get_all_sources` | `(CaMeLValue) -> frozenset[str]` | Returns `value.sources` |

**`is_trusted` — empty sources is not trusted:** A value with an empty
`sources` set cannot be asserted safe.

### 11.5 Configuration-driven policy loading

```
CAMEL_POLICY_MODULE=myapp.security.policies
```

The module must export:

```python
def configure_policies(registry: PolicyRegistry) -> None:
    registry.register("send_email", email_recipient_policy)
    registry.register("write_file", file_write_policy)
    ...
```

Load at startup with:

```python
registry = PolicyRegistry.load_from_env()
```

No core code changes are required to change the active policy set across
deployments.

### 11.6 Example policy

```python
from camel.policy import PolicyRegistry, Allowed, Denied, is_trusted
from camel.value import CaMeLValue
from collections.abc import Mapping

def email_recipient_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    \"\"\"Block send_email if the recipient address comes from untrusted data.\"\"\"
    to_addr = kwargs.get("to")
    if to_addr is not None and not is_trusted(to_addr):
        return Denied(
            "recipient address originates from untrusted data — "
            "possible data exfiltration attempt"
        )
    return Allowed()
```

### 11.7 Three-Tier Policy Governance (ADR-011, Milestone 5)

**Module:** `camel/policy/governance.py` | **ADR:** [011](adr/011-three-tier-policy-governance.md)
**Authorship guide:** [docs/policy_authorship_guide.md](policy_authorship_guide.md)

The three-tier governance model extends the flat `PolicyRegistry` with explicit
authorship tiers, a `non_overridable` flag, and a deterministic
`PolicyConflictResolver` that produces a merged result with a full audit trail.
It directly addresses PRD Risk L4 (user fatigue from over-strict policies) and
resolves PRD Open Question FW-5 (multi-party policy governance).

#### PolicyTier — authorship enum

```python
class PolicyTier(Enum):
    PLATFORM      = "Platform"       # Highest authority — deployment operator
    TOOL_PROVIDER = "ToolProvider"   # Middle — tool author constraints
    USER          = "User"           # Lowest — end-user personalisation
```

Only `PLATFORM`-tier entries may carry `non_overridable=True`.  Setting
`non_overridable=True` on any other tier raises `ValueError` at registration time.

#### TieredPolicyRegistry — storage layer

```python
class TieredPolicyRegistry:
    def register(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        tier: PolicyTier,
        *,
        non_overridable: bool = False,   # Only valid for PLATFORM tier
        name: str = "",
    ) -> PolicyFn: ...

    # Convenience wrappers
    def register_platform(
        self, tool_name: str, policy_fn: PolicyFn, *, non_overridable: bool = False,
        name: str = "",
    ) -> PolicyFn: ...

    def register_tool_provider(
        self, tool_name: str, policy_fn: PolicyFn, *, name: str = "",
    ) -> PolicyFn: ...

    def register_user(
        self, tool_name: str, policy_fn: PolicyFn, *, name: str = "",
    ) -> PolicyFn: ...

    def get_entries(self, tool_name: str, tier: PolicyTier) -> list[TieredPolicyEntry]: ...
    def registered_tools(self, tier: PolicyTier | None = None) -> frozenset[str]: ...

    @classmethod
    def load_from_env(cls) -> TieredPolicyRegistry: ...   # CAMEL_TIERED_POLICY_MODULE
```

All `register*` methods return `policy_fn` unchanged, enabling decorator syntax:

```python
registry = TieredPolicyRegistry()

@registry.register_platform("send_email", non_overridable=True)
def no_external_recipients(tool_name, kwargs):
    ...
```

#### PolicyConflictResolver — merge algorithm

Evaluation order: **Platform → Tool-Provider → User**.  Within each tier:
registration order.  First `Denied` in any tier short-circuits all lower tiers.

| Phase | Tier evaluated | `authoritative_tier` on `Denied` | `non_overridable_denial` on `Denied` |
|---|---|---|---|
| 1 | `PLATFORM` | `PLATFORM` | `entry.non_overridable` |
| 2 | `TOOL_PROVIDER` | `TOOL_PROVIDER` | `False` |
| 3 | `USER` | `USER` | `False` |
| 4 (all `Allowed`) | — | `None` | `False` |

```python
resolver = PolicyConflictResolver(registry)

merged: MergedPolicyResult = resolver.evaluate("send_email", kwargs)

# Use MergedPolicyResult to drive enforcement decision:
if merged.is_allowed:
    call_tool()
elif merged.can_be_consented:     # False when non_overridable_denial=True
    if consent_callback(...):
        call_tool()
else:
    raise PolicyViolationError(...)     # Absolute denial — no consent path

# Backward-compatible flat result (drop-in for PolicyRegistry.evaluate):
result: SecurityPolicyResult = resolver.evaluate_flat("send_email", kwargs)
```

#### MergedPolicyResult — output type

```python
@dataclass(frozen=True)
class MergedPolicyResult:
    outcome:                SecurityPolicyResult      # Allowed() or Denied(reason)
    authoritative_tier:     PolicyTier | None         # None if all tiers Allowed
    non_overridable_denial: bool                      # True → consent must NOT be invoked
    audit_trail:            tuple[TierEvaluationRecord, ...]

    @property
    def is_allowed(self) -> bool: ...        # outcome.is_allowed()
    @property
    def can_be_consented(self) -> bool: ...  # not non_overridable_denial
```

#### TierEvaluationRecord — audit trail entry

```python
@dataclass(frozen=True)
class TierEvaluationRecord:
    tier:             PolicyTier
    policy_name:      str
    result:           SecurityPolicyResult
    non_overridable:  bool              # Always False for non-Platform tiers
    authoritative:    bool              # True if this record determined the outcome
```

Policies in tiers skipped due to a higher-tier short-circuit are **not** included
in the audit trail.

#### Environment-based loading

```python
# Set CAMEL_TIERED_POLICY_MODULE=myapp.tiered_policies in environment.
# Module must export: configure_tiered_policies(registry: TieredPolicyRegistry) -> None

resolver = PolicyConflictResolver.load_from_env()
```

#### `non_overridable` semantics — summary

| Scenario | `can_be_consented` | Consent callback invoked? |
|---|---|---|
| All tiers `Allowed` | `True` (irrelevant) | No (not needed) |
| Platform `Denied`, `non_overridable=False` | `True` | Yes (PRODUCTION mode) |
| Platform `Denied`, `non_overridable=True` | **`False`** | **Never** |
| Tool-Provider `Denied` | `True` | Yes (PRODUCTION mode) |
| User `Denied` | `True` | Yes (PRODUCTION mode) |

### 11.8 Developer Testing Tools (ADR-012, Milestone 5)

**Module:** `camel/testing/` | **ADR:** [012](adr/012-policy-testing-harness-consent-handler.md)

Three developer-facing utilities ship in `camel/testing/` to enable policy
authors to test and simulate policies without a live interpreter, LLM, or
network connection (NFR-9).

#### `CaMeLValueBuilder` — fluent value construction

```python
# Module: camel/testing/value_builder.py

class CaMeLValueBuilder:
    def with_value(self, value: Any) -> Self: ...
    def with_sources(self, *sources: str) -> Self: ...
    def with_inner_source(self, inner_source: str) -> Self: ...
    def with_readers(self, *readers: str) -> Self: ...
    def with_public_readers(self) -> Self: ...
    def with_no_readers(self) -> Self: ...
    def with_dependency_chain(self, *dep_values: CaMeLValue) -> Self: ...
    def build(self) -> CaMeLValue: ...

    @classmethod
    def trusted(cls, value: Any, source: str = "User literal") -> Self: ...
    @classmethod
    def untrusted(cls, value: Any, source: str = "external_tool") -> Self: ...
    @classmethod
    def from_tool(cls, value: Any, tool_name: str) -> Self: ...
```

`with_dependency_chain(*dep_values)` merges `sources` and `readers` from
upstream `CaMeLValue` dependencies using union semantics — equivalent to the
propagation rules the interpreter applies during execution.

#### `PolicyTestRunner` — structured policy test execution

```python
# Module: camel/testing/policy_runner.py

@dataclass(frozen=True)
class PolicyTestCase:
    description: str
    tool_name: str
    kwargs: dict[str, CaMeLValue]
    expected_outcome: Literal["Allowed", "Denied"]
    expected_reason_fragment: str | None = None

@dataclass(frozen=True)
class PolicyTestResult:
    test_case: PolicyTestCase
    passed: bool
    actual_outcome: SecurityPolicyResult
    failure_reason: str | None = None

@dataclass(frozen=True)
class PolicyTestReport:
    policy_name: str
    tool_name: str
    results: tuple[PolicyTestResult, ...]
    passed: int
    failed: int
    total: int
    coverage_notes: str | None = None

    @property
    def all_passed(self) -> bool: ...

class PolicyTestRunner:
    def run(
        self,
        policy_fn: PolicyFn,
        test_cases: Sequence[PolicyTestCase],
        *,
        policy_name: str = "",
    ) -> PolicyTestReport: ...
```

`PolicyTestRunner.run()` evaluates `policy_fn` against each test case, compares
the actual outcome to the expected outcome, and returns a `PolicyTestReport`
with per-case pass/fail detail and aggregate coverage statistics.

#### `PolicySimulator` — dry-run execution

```python
# Module: camel/testing/simulator.py

@dataclass(frozen=True)
class SimulatedPolicyTrigger:
    tool_name: str
    argument_snapshot: dict[str, str]    # {arg_name: repr(raw)[:80]}
    policy_name: str
    outcome: SecurityPolicyResult
    tier: PolicyTier | None              # None when flat PolicyRegistry used

@dataclass(frozen=True)
class SimulationReport:
    code_plan: str
    triggered_policies: tuple[SimulatedPolicyTrigger, ...]
    suppressed_tool_calls: tuple[str, ...]
    would_have_succeeded: bool
    denial_triggers: tuple[SimulatedPolicyTrigger, ...]

class PolicySimulator:
    def __init__(
        self,
        policy_registry: PolicyRegistry,
        tool_names: list[str],
    ) -> None: ...

    def simulate(
        self,
        code_plan: str,
        variable_store: dict[str, CaMeLValue] | None = None,
    ) -> SimulationReport: ...
```

`PolicySimulator.simulate()` executes the code plan against the live interpreter
in `EVALUATION` mode with every tool replaced by a no-op stub that returns
`CaMeLValue(value=None, sources=frozenset({"CaMeL"}), ...)`.  All policy
evaluations run; no real tool is called.  The `SimulationReport` lists every
triggered policy and whether the run `would_have_succeeded`.

---

## 12. Capability Assignment Engine

**Module:** `camel/capabilities/` | **Module:** `camel/tools/registry.py`

The Capability Assignment Engine bridges raw tool return values and the
`CaMeLValue` capability system.  It ensures every value entering the
interpreter's variable store carries correct provenance metadata.

### 12.1 CapabilityAnnotationFn Contract

```python
CapabilityAnnotationFn = Callable[
    [Any, Mapping[str, Any]],   # (return_value, tool_kwargs)
    CaMeLValue,
]
```

A capability annotation function receives the **raw return value** from the
underlying tool and a mapping of the **keyword arguments** passed to the tool.
It returns a fully-tagged `CaMeLValue`.

### 12.2 Default Capability Annotation

When no custom annotator is registered, `default_capability_annotation` is applied:

```python
CaMeLValue(
    value=return_value,
    sources=frozenset({tool_id}),
    inner_source=None,
    readers=Public,
)
```

This is the safe baseline: the return value originates from the tool (tainted),
with no reader restriction (`Public`).

### 12.3 Built-in Tool Annotators

Three annotators ship with CaMeL for tools that require fine-grained tagging:

#### `annotate_read_email`

| Field | Value |
|---|---|
| `sources` | `frozenset({"read_email"})` |
| `inner_source` | Sender email address string (e.g. `"alice@example.com"`) |
| `readers` | `Public` |
| `value` | Modified dict with `body`, `subject`, `sender` each wrapped as nested `CaMeLValue` |

Nested field wrappers each have `inner_source` set to the field name, allowing
policies to reason at the sub-field level.

**Expected tool return shape:**

```python
{
    "sender":  str,
    "subject": str,
    "body":    str,
    # additional fields are left unwrapped
}
```

#### `annotate_read_document` / `annotate_get_file`

Cloud storage annotators that extract `readers` from a `"permissions"` key:

| Permissions shape | `readers` value |
|---|---|
| `{"type": "public"}` | `Public` |
| `{"type": "restricted", "readers": [...]}` | `frozenset[str]` of addresses |
| Absent / unrecognised | `Public` (safe fallback) |

**Expected tool return shape:**

```python
{
    "content": str,
    "permissions": {
        "type": "public" | "restricted",
        "readers": ["alice@example.com", ...]
    },
}
```

### 12.4 ToolRegistry

The `ToolRegistry` (`camel/tools/registry.py`) associates each tool name with
its callable and optional annotator.  `as_interpreter_tools()` returns wrapped
callables that:

1. Call the underlying tool with raw Python values (unwrapped from `CaMeLValue`).
2. Pass the return value through the registered annotator (or default).
3. Return the resulting `CaMeLValue` to the interpreter.

```python
registry = ToolRegistry()
registry.register("read_email", real_read_email_fn,
                  capability_annotation=annotate_read_email)
registry.register("send_email", real_send_email_fn)   # default annotation

interp = CaMeLInterpreter(tools=registry.as_interpreter_tools())
```

If a tool already returns a `CaMeLValue`, the annotation step is skipped and
the pre-annotated value is used as-is.

### 12.5 `register_built_in_tools` Convenience Helper

```python
from camel.capabilities.annotations import register_built_in_tools

register_built_in_tools(
    registry,
    read_email_fn=real_read_email,
    read_document_fn=real_read_document,
    get_file_fn=real_get_file,
)
```

Registers any subset of the three built-in tools with their correct annotators
in a single call.  Pass `None` to omit a tool.

---

## 13. Reference Policy Library

**Module:** `camel/policy/reference_policies.py`

Six baseline security policies ship with CaMeL.  Together they enforce
**G2** (prevent data-flow manipulation) and **G3** (prevent private data
exfiltration) for the tool categories most commonly found in AgentDojo and
real-world LLM agent deployments.

### 13.1 Policy Inventory

| Policy function | Tool | Primary checks | Attack mitigated |
|---|---|---|---|
| `send_email_policy` | `send_email` | Recipient trust; content-readers superset | Injected recipient / data exfiltration via email |
| `send_money_policy` | `send_money` | Recipient and amount fully trusted | Financial fraud via data-flow manipulation |
| `create_calendar_event_policy` | `create_calendar_event` | Participant trust; content-readers per participant | Injected attendees / private-content exposure |
| `make_write_file_policy(owner)` | `write_file` | Path trust; content readers include `owner` | Path injection / unauthorised content write |
| `post_message_policy` | `post_message` | Channel trust; untrusted message must have `Public` readers | Channel injection / private-content exfiltration |
| `fetch_external_url_policy` | `fetch_external_url` | URL, params, and body all trusted | SSRF / data exfiltration via URL parameters or body |

### 13.2 Policy Logic Detail

#### `send_email_policy`

1. **Recipient trust:** If `to` is trusted (sources ⊆ `{"User literal", "CaMeL"}`),
   allow immediately.
2. **Content-readers:** If `to` is untrusted, every recipient must be an
   authorised reader (`can_readers_read_value`) of both `body` and `subject`.
   The first failing combination is denied.

#### `send_money_policy`

Both `recipient` and `amount` must be fully trusted (`is_trusted`).  Any
untrusted taint — even one hop removed — blocks the transfer.  `memo` and
`currency` are not checked.

#### `create_calendar_event_policy`

If `participants` is trusted, allow unconditionally.  If untrusted, every
participant must be an authorised reader of every content field (`title`,
`description`, `location`).  `start_time` and `end_time` are not checked.

#### `make_write_file_policy(owner: str)`

Factory returning a policy closure parameterised by the file system owning
user identity:

1. **Path provenance:** `path` must be trusted.
2. **Content readers:** `content.readers` must include `owner`.

#### `post_message_policy`

1. **Channel provenance:** `channel` must be trusted.
2. **Message content:** If `message` is untrusted and `readers` is not `Public`,
   the post is denied.

#### `fetch_external_url_policy`

`url`, `params`, and `body` must each be trusted.  The `method` field is not
checked.

### 13.3 Quick Setup

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")
# All six reference policies are now active.
```

`register_all` is an alias of `configure_reference_policies`.

### 13.4 Extending or Restricting the Library

- **Exclude a policy:** Register individual policies manually instead of
  calling `configure_reference_policies`.
- **Add a policy:** Call `registry.register(tool_name, extra_policy_fn)` after
  `configure_reference_policies`.  All registered policies for a tool must
  return `Allowed` (AND composition).
- **Deploy-time configuration:** Use `CAMEL_POLICY_MODULE` (see §11.5) to load
  a custom policy module that calls `configure_reference_policies` plus any
  deployment-specific additions.

---

## 14. Enforcement Integration & Consent Flow

**Module:** `camel/interpreter.py` | **ADR:** [010](adr/010-enforcement-hook-consent-audit-harness.md)

Milestone 3 wires the policy engine into the interpreter's tool-call
pre-execution hook and introduces two enforcement modes.

### 14.1 `EnforcementMode`

```python
class EnforcementMode(Enum):
    EVALUATION  # default
    PRODUCTION
```

| Mode | Behaviour on policy denial |
|---|---|
| `EVALUATION` | Raises `PolicyViolationError` immediately; no user interaction.  Used for automated AgentDojo benchmarking and unit tests (NFR-2). |
| `PRODUCTION` | Invokes `ConsentCallback`; proceeds on approval; raises `PolicyViolationError(consent_decision="UserRejected")` on rejection. |

### 14.2 `ConsentHandler` Protocol and `ConsentDecision` enum (ADR-012, Milestone 5)

**Module:** `camel/consent.py` | **ADR:** [012](adr/012-policy-testing-harness-consent-handler.md)

`ConsentHandler` supersedes the legacy `bool`-returning `ConsentCallback`
Protocol.  The new interface uses the `ConsentDecision` enum to express a
richer set of outcomes, including session-level approval caching that
addresses PRD Risk L4 (user fatigue from repeated policy denials).

#### `ConsentDecision` enum

```python
class ConsentDecision(Enum):
    APPROVE             = "Approve"           # Approve this specific call
    REJECT              = "Reject"            # Reject this call
    APPROVE_FOR_SESSION = "ApproveForSession" # Approve + cache for session
```

#### `ConsentHandler` Protocol

```python
class ConsentHandler(Protocol):
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision: ...
```

**Extension points:**

| Deployment context | Recommended approach |
|---|---|
| CLI (default) | `CLIConsentHandler` — prints formatted prompt, reads stdin |
| Web UI | Synchronous wrapper blocking on `threading.Event` set by async HTTP handler |
| Mobile | Native sync bridge (JNI, `ctypes`, `concurrent.futures.Future`) |
| Async frameworks | `asyncio.get_event_loop().run_until_complete()` inside `handle_consent`; avoid when event loop is already running — use `ThreadPoolExecutor` bridge instead |

#### `CLIConsentHandler` — default implementation

```python
class CLIConsentHandler:
    """Prints a formatted consent prompt to stdout; reads user choice from stdin."""
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision: ...
```

**Backward compatibility with `ConsentCallback`:** The interpreter wraps
legacy `bool`-returning callables via an internal `_LegacyConsentHandlerAdapter`
(not part of the public API), mapping `True → APPROVE` and `False → REJECT`.

### 14.3 Session-Level Consent Cache (ADR-012)

**Module:** `camel/consent.py`

```python
@dataclass(frozen=True)
class ConsentCacheKey:
    tool_name: str
    argument_hash: str    # SHA-256 hex digest of canonical arg representation

def compute_consent_key(
    tool_name: str,
    kwargs: dict[str, CaMeLValue],
) -> ConsentCacheKey: ...

class ConsentCache:
    def get(self, key: ConsentCacheKey) -> ConsentDecision | None: ...
    def set(self, key: ConsentCacheKey, decision: ConsentDecision) -> None: ...
    def invalidate(self, key: ConsentCacheKey) -> None: ...
    def clear(self) -> None: ...
    def __len__(self) -> int: ...
```

**Scope:** One `ConsentCache` instance per interpreter session.  Entries expire
at session end — no wall-clock TTL (TTL would introduce non-determinism into
policy enforcement, violating NFR-2).

### 14.4 Pre-Execution Enforcement Hook

Before every tool call the interpreter executes:

```
1. policy_engine.evaluate(tool_name, kwargs_mapping)
   → Allowed():
       append AuditLogEntry(outcome="Allowed"), proceed to tool call.
   → Denied(reason) — EVALUATION mode:
       append AuditLogEntry(outcome="Denied", consent_decision=None)
       raise PolicyViolationError.
   → Denied(reason) — PRODUCTION mode:
       key = compute_consent_key(tool_name, kwargs_mapping)
       if consent_cache.get(key) == APPROVE_FOR_SESSION:
           append AuditLogEntry(consent_decision="CacheHit"), proceed.
       else:
           decision = consent_handler.handle_consent(tool_name, summary, reason)
           if APPROVE:
               append AuditLogEntry(consent_decision="UserApproved"), proceed.
           if APPROVE_FOR_SESSION:
               consent_cache.set(key, APPROVE_FOR_SESSION)
               append AuditLogEntry(consent_decision="UserApprovedForSession"),
               proceed.
           if REJECT:
               append AuditLogEntry(consent_decision="UserRejected")
               raise PolicyViolationError(consent_decision="UserRejected").
2. Call tool with raw values (CaMeLValue.raw for each argument).
3. Pass return value through capability annotator → CaMeLValue.
4. Store result in variable store.
```

Blocked calls **never proceed** to tool execution without explicit resolution.

### 14.5 `AuditLogEntry` Data Model (extended — ADR-012)

```python
@dataclass(frozen=True)
class AuditLogEntry:
    tool_name:        str
    outcome:          Literal["Allowed", "Denied"]
    reason:           str | None               # Denied.reason, or None when Allowed
    timestamp:        str                      # ISO-8601 UTC
    consent_decision: Literal[
                          "UserApproved",
                          "UserApprovedForSession",
                          "UserRejected",
                          "CacheHit",
                          None,
                      ]
    argument_summary: str | None = None        # Human-readable arg snapshot (≤80 chars/arg)
```

`consent_decision` values:

| Value                   | Meaning                                                 |
|-------------------------|---------------------------------------------------------|
| `None`                  | `EVALUATION` mode, or policy returned `Allowed`         |
| `"UserApproved"`        | User approved this specific call                        |
| `"UserApprovedForSession"` | User approved; decision cached for remainder of session |
| `"UserRejected"`        | User rejected the call                                  |
| `"CacheHit"`            | Session cache returned a prior `APPROVE_FOR_SESSION`    |

One entry is appended for every `policy_engine.evaluate()` call regardless of
outcome — both `Allowed` and `Denied` events are recorded (NFR-6).

### 14.6 Security Audit Log

```python
# Read audit entries after execution
entries: list[AuditLogEntry] = interp.audit_log
```

`CaMeLInterpreter.audit_log` returns a snapshot in chronological order.  The
log is in-memory; callers are responsible for persisting it to durable storage.

### 14.7 Interpreter Construction

```python
# Evaluation / test mode (default)
interp = CaMeLInterpreter(
    tools=registry.as_interpreter_tools(),
    policy_engine=policy_registry,
    enforcement_mode=EnforcementMode.EVALUATION,
)

# Production mode with ConsentHandler and session cache
from camel.consent import CLIConsentHandler, ConsentCache

interp = CaMeLInterpreter(
    tools=registry.as_interpreter_tools(),
    policy_engine=policy_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_handler=CLIConsentHandler(),   # ValueError raised at construction if omitted
    consent_cache=ConsentCache(),          # Optional; enables session-level approval caching
)

# Production mode (legacy ConsentCallback — backward compatible)
interp = CaMeLInterpreter(
    tools=registry.as_interpreter_tools(),
    policy_engine=policy_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_callback=lambda tool, summary, reason: True,  # bool-returning callable
)
```

### 14.8 NFR Compliance

| NFR | Requirement | Verification |
|---|---|---|
| NFR-1 | Interpreter operates in a sandboxed environment: no arbitrary module imports, no timing primitives, builtins restricted to the 16-name approved set defined in `camel/config/allowlist.yaml` (M4-F10 – F14) | `tests/test_interpreter.py` — `ForbiddenImportError` and `ForbiddenNameError` suites; allowlist positive/negative cases |
| NFR-2 | No LLM in policy evaluation path | `test_e2e_enforcement.py` asserts zero LLM calls during `evaluate()` |
| NFR-4 | ≤100ms interpreter overhead per tool call (including policy evaluation) | `scripts/benchmark_interpreter.py` |
| NFR-6 | All policy evaluation outcomes (Allowed and Denied), consent decisions (UserApproved, UserApprovedForSession, UserRejected, CacheHit), exception redactions, and allowlist violations written as immutable `AuditLogEntry` records to the security audit log.  Each consent-related entry includes `tool_name`, `outcome`, `reason`, `timestamp`, `consent_decision`, and `argument_summary`. | `test_e2e_enforcement.py` log-completeness assertions; `ForbiddenImportEvent` / `ForbiddenNameEvent` emission tests; ADR-012 consent decision audit trail tests |
| NFR-7 | Adding a new tool requires only: (a) constructing a `Tool(name, fn)` dataclass, (b) optionally providing a `capability_annotation`, and (c) optionally appending policy functions to `Tool.policies`.  No changes to core interpreter or policy engine code. | `camel_security/tool.py` — `Tool` dataclass design; `camel/tools/registry.py` — `ToolRegistry.register()` |
| NFR-8 | `CaMeLAgent` is compatible with any LLM backend that satisfies the `LLMBackend` protocol (implements `generate` and `generate_structured`).  Provider switching requires only changing the backend object passed to `CaMeLAgent(p_llm=..., q_llm=...)`. | `tests/test_multi_backend_swap.py`; `tests/test_backend_swap.py` |
| NFR-9 | Policy engine, capability system, enforcement hook, `PolicyTestRunner`, `CaMeLValueBuilder`, and `PolicySimulator` are all independently unit-testable without a live interpreter, LLM, or network connection. | `tests/harness/policy_harness.py`, `tests/test_policy.py`, `camel/testing/` unit tests |

---

## 15. SDK Layer — camel-security Public API

**Package:** `camel_security/` | **Introduced:** Milestone 5 (v0.5.0)
**Design document:** [docs/design/milestone5-sdk-packaging.md](design/milestone5-sdk-packaging.md)

The `camel-security` package exposes a stable, typed, thread-safe public API
surface on top of the internal `camel` implementation package.  Users install
`pip install camel-security` and import from `camel_security`.

### 15.1 Module Layout

```
camel_security/
├── __init__.py      # Stable public API exports (camel_security.__all__)
├── agent.py         # CaMeLAgent, AgentResult, PolicyDenialRecord
└── tool.py          # Tool dataclass (registration interface)
```

The `camel` package (internal implementation) continues to be importable
for advanced users who need lower-level access.

### 15.2 Public API Exports

The following names form the **stable public API** (all in `camel_security.__all__`):

| Name | Module | Description |
|---|---|---|
| `CaMeLAgent` | `camel_security.agent` | Main entry point |
| `AgentResult` | `camel_security.agent` | Frozen return type of `agent.run()` |
| `PolicyDenialRecord` | `camel_security.agent` | One policy denial event |
| `Tool` | `camel_security.tool` | Tool registration dataclass |
| `ExecutionMode` | re-exported from `camel` | `STRICT` / `NORMAL` flag |
| `PolicyRegistry` | re-exported from `camel.policy` | Policy container |
| `Allowed` / `Denied` | re-exported from `camel.policy` | Policy result types |
| `CaMeLValue` | re-exported from `camel` | Capability-tagged value |
| `Public` | re-exported from `camel` | Open-readers sentinel |
| `get_backend` | re-exported from `camel.llm` | Backend factory |
| `__version__` | `camel_security` | Package version string |

### 15.3 CaMeLAgent — Constructor Signature

```python
class CaMeLAgent:
    def __init__(
        self,
        p_llm: LLMBackend,        # Privileged LLM — generates plans
        q_llm: LLMBackend,        # Quarantined LLM — structured extraction
        tools: Sequence[Tool],    # At least one tool required
        policies: PolicyRegistry | None = None,  # None → empty registry (all-allow)
        mode: ExecutionMode = ExecutionMode.STRICT,  # Production default
        max_retries: int = 10,    # Outer-loop retry ceiling (M2-F8)
    ) -> None: ...

    async def run(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> AgentResult: ...

    def run_sync(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> AgentResult: ...

    @property
    def tools(self) -> tuple[Tool, ...]: ...

    @property
    def mode(self) -> ExecutionMode: ...
```

### 15.4 AgentResult — Stable Dataclass

```python
@dataclass(frozen=True)
class AgentResult:
    execution_trace: list[TraceRecord]     # One record per successful tool call
    display_output:  list[str]             # print() outputs from execution plan
    policy_denials:  list[PolicyDenialRecord]  # Denials in production mode
    audit_log_ref:   str                   # "camel-audit:<hex_id>" token
    loop_attempts:   int                   # 0-based outer-loop retry count
    success:         bool                  # False if MaxRetriesExceededError
    final_store:     dict[str, Any]        # Interpreter variable store snapshot
```

All fields have **stability guarantee**: not removed or renamed without a
major-version bump.  New fields may be added in minor releases (always with
defaults).  See `VERSIONING.md` for the full policy.

### 15.5 Tool — Registration Interface

```python
@dataclass
class Tool:
    name:                   str                         # Python identifier; unique
    fn:                     Callable[..., Any]          # The underlying callable
    description:            str = ""                    # For P-LLM system prompt
    params:                 str = ""                    # Param signature string
    return_type:            str = "Any"                 # Return type string
    capability_annotation:  CapabilityAnnotationFn | None = None
    policies:               list[PolicyFn] = field(default_factory=list)
```

**NFR-7 compliance:** Adding a new tool requires constructing one `Tool`
object — no core code changes.

### 15.6 Thread-Safety Contract

Each `CaMeLAgent.run()` call creates a **fresh** `CaMeLInterpreter` and
`CaMeLOrchestrator` instance.  The agent holds only immutable references
after construction:

| Component | Mutability | Thread-safe? |
|---|---|---|
| `CaMeLInterpreter` | Created fresh per `run()` | ✅ Yes — no shared instance |
| `CaMeLOrchestrator` | Created fresh per `run()` | ✅ Yes — no shared instance |
| `ToolRegistry` | Built once at construction; never mutated | ✅ Yes |
| `PolicyRegistry` (base) | Read-only after `CaMeLAgent.__init__` | ✅ Yes (callers must not mutate after passing) |
| `LLMBackend` objects | Shared across concurrent `run()` calls | ⚠️ Depends on provider SDK |

**Implication:** multiple threads or async tasks may call `agent.run()` in
parallel without external locking, provided the underlying `LLMBackend`
implementations support concurrent async calls (both `ClaudeBackend` and
`GeminiBackend` do).

### 15.7 Wiring Diagram

```
CaMeLAgent.run(user_query)
    │
    ├── [per-run] CaMeLInterpreter(
    │       tools = ToolRegistry.as_interpreter_tools()
    │               + {"query_quarantined_llm": make_query_quarantined_llm(q_llm)}
    │       policy_engine = _build_run_policy_registry()
    │       mode = self._mode  [default: STRICT]
    │   )
    │
    ├── [per-run] PLLMWrapper(backend=p_llm)
    │
    ├── [per-run] CaMeLOrchestrator(
    │       p_llm=PLLMWrapper,
    │       interpreter=CaMeLInterpreter,
    │       tool_signatures=[ToolSignature(...)],
    │   )
    │
    └── await orchestrator.run(user_query) → ExecutionResult
            │
            └── AgentResult(
                    execution_trace = result.trace,
                    display_output  = [str(v.raw) for v in result.print_outputs],
                    audit_log_ref   = "camel-audit:<run_id>",
                    loop_attempts   = result.loop_attempts,
                    success         = True,
                    final_store     = result.final_store,
                )
```

---

## 16. Module Map (File Tree)

```
camel/
├── __init__.py              Public API: CaMeLInterpreter, ExecutionMode,
│                            get_dependency_graph, CaMeLOrchestrator, …
├── value.py                 CaMeLValue, Public, wrap, propagate_* functions
├── interpreter.py           CaMeLInterpreter, UnsupportedSyntaxError,
│                            PolicyViolationError, ExecutionMode,
│                            EnforcementMode, ConsentCallback, AuditLogEntry
├── dependency_graph.py      DependencyGraph, TrackingMode, get_dependency_graph
├── execution_loop.py        CaMeLOrchestrator, ExceptionRedactor,
│                            RetryPromptBuilder, TraceRecorder,
│                            MaxRetriesExceededError, ExecutionResult,
│                            TraceRecord, RedactedError, AcceptedState,
│                            DisplayChannel, StdoutDisplayChannel
├── exceptions.py            Shared exception base types: NotEnoughInformationError,
│                            SchemaValidationError, ForbiddenImportError (M4-F10),
│                            ForbiddenNameError (M4-F14), ConfigurationSecurityError
├── config/
│   ├── __init__.py          AllowlistLoader, AllowlistConfig (Pydantic model)
│   └── allowlist.yaml       Single source of truth for permitted builtins and
│                            excluded timing names; mandatory security review gate
├── qllm_schema.py           Schema augmentation utilities (have_enough_information)
├── qllm_wrapper.py          QLLMWrapper (legacy path, re-exported)
├── capabilities/
│   ├── __init__.py          CapabilityAnnotationFn, default_capability_annotation,
│   │                        annotate_read_email, annotate_read_document,
│   │                        annotate_get_file, register_built_in_tools
│   ├── types.py             CapabilityAnnotationFn protocol, type definitions
│   └── annotations.py       Tool-specific capability annotators
├── policy/
│   ├── __init__.py          SecurityPolicyResult, Allowed, Denied, PolicyFn,
│   │                        PolicyRegistry, is_trusted, can_readers_read_value,
│   │                        get_all_sources; also re-exports three-tier API
│   ├── interfaces.py        Full type definitions, stubs, and TRUSTED_SOURCE_LABELS
│   ├── governance.py        Three-tier API (ADR-011): PolicyTier, TieredPolicyEntry,
│   │                        TierEvaluationRecord, MergedPolicyResult,
│   │                        TieredPolicyRegistry, PolicyConflictResolver
│   └── reference_policies.py  Six reference security policies +
│                               configure_reference_policies()
├── tools/
│   ├── __init__.py
│   └── registry.py          ToolRegistry with capability_annotation support
└── llm/
    ├── __init__.py          PLLMWrapper, QLLMWrapper, ToolSignature, …
    ├── backend.py           LLMBackend Protocol, Message
    ├── p_llm.py             PLLMWrapper, PLLMRetryExhaustedError,
    │                        PLLMIsolationError, CodeBlockNotFoundError
    ├── qllm.py              QLLMWrapper, NotEnoughInformationError
    ├── schemas.py           ToolSignature, UserContext, SystemPromptBuilder
    ├── protocols.py         Shared protocol types
    ├── query_interface.py   query_quarantined_llm callable interface
    ├── exceptions.py        LLM-layer exceptions
    └── adapters/
        ├── __init__.py
        ├── claude.py        ClaudeBackend (Anthropic)
        └── gemini.py        GeminiBackend (Google)

camel_security/                    ← Public SDK (Milestone 5, v0.5.0)
├── __init__.py          Stable public API: CaMeLAgent, AgentResult,
│                        Tool, ExecutionMode, PolicyRegistry, …
├── agent.py             CaMeLAgent, AgentResult, PolicyDenialRecord
└── tool.py              Tool registration dataclass

tests/
├── harness/
│   ├── isolation_assertions.py  Invariant 1/2/3 assertion helpers
│   ├── policy_harness.py        Policy testing harness (AgentDojo scenarios)
│   ├── recording_backend.py     Intercepts LLMBackend.complete() calls
│   ├── results_reporter.py      Sign-off report generator
│   └── scenarios.py             E2E scenario definitions
├── policies/
│   ├── test_reference_policies.py  Reference policy unit tests
│   └── test_agentdojo_mapping.py   AgentDojo adversarial scenario mapping
├── test_capability_assignment.py   Capability annotation engine unit tests
├── test_policy.py                  Policy engine & registry unit tests
├── test_policy_harness.py          Policy testing harness validation
├── test_e2e_enforcement.py         End-to-end enforcement integration tests
├── test_isolation_harness.py       Invariant verification (50 runs)
├── test_redaction_completeness.py  10 adversarial redaction cases
├── test_e2e_scenarios.py           10 representative task scenarios
├── test_multi_backend_swap.py      Claude ↔ Gemini swap test
└── …                               Unit tests per module
```

---

## 17. Related Documents

| Document | Location |
|---|---|
| API Reference | [docs/api/index.md](api/index.md) |
| Product Requirements Document (PRD) | `docs/` (embedded in `CLAUDE.md`) |
| Developer Guide | [docs/developer_guide.md](developer_guide.md) |
| Operator Guide | [docs/manuals/operator-guide.md](manuals/operator-guide.md) |
| Reference Policy Specification | [docs/policies/reference-policy-spec.md](policies/reference-policy-spec.md) |
| M2 Exit Criteria Report | [docs/milestone-2-exit-criteria-report.md](milestone-2-exit-criteria-report.md) |
| **Milestone 4 Design Document** | [docs/milestone4_design.md](milestone4_design.md) |
| **Security Hardening Design Document** | [docs/security_hardening_allowlist.md](security_hardening_allowlist.md) |
| **M4 Side-Channel Test Report** | [docs/reports/milestone4_side_channel_test_report.md](reports/milestone4_side_channel_test_report.md) |
| M4 STRICT Mode Extension Design | [docs/design/milestone4-strict-mode-extension.md](design/milestone4-strict-mode-extension.md) |
| M4 Exception Hardening Design | [docs/design/milestone4-exception-hardening.md](design/milestone4-exception-hardening.md) |
| M4 Escalation Detection Design | [docs/design/milestone4-escalation-detection.md](design/milestone4-escalation-detection.md) |
| **Milestone 5 SDK Packaging Design** | [docs/design/milestone5-sdk-packaging.md](design/milestone5-sdk-packaging.md) |
| **Semantic Versioning Policy** | [VERSIONING.md](../VERSIONING.md) |
| ADR 001 — Q-LLM Isolation | [docs/adr/001-q-llm-isolation-contract.md](adr/001-q-llm-isolation-contract.md) |
| ADR 002 — CaMeLValue | [docs/adr/002-camelvalue-capability-system.md](adr/002-camelvalue-capability-system.md) |
| ADR 003 — Interpreter | [docs/adr/003-ast-interpreter-architecture.md](adr/003-ast-interpreter-architecture.md) |
| ADR 004 — Dependency Graph | [docs/adr/004-dependency-graph-architecture.md](adr/004-dependency-graph-architecture.md) |
| ADR 005 — P-LLM Wrapper | [docs/adr/005-p-llm-wrapper-architecture.md](adr/005-p-llm-wrapper-architecture.md) |
| ADR 006 — Q-LLM Schema Injection | [docs/adr/006-q-llm-dynamic-schema-injection.md](adr/006-q-llm-dynamic-schema-injection.md) |
| ADR 007 — Execution Loop | [docs/adr/007-execution-loop-orchestrator.md](adr/007-execution-loop-orchestrator.md) |
| ADR 008 — Isolation Test Harness | [docs/adr/008-isolation-test-harness-architecture.md](adr/008-isolation-test-harness-architecture.md) |
| ADR 009 — Policy Engine | [docs/adr/009-policy-engine-architecture.md](adr/009-policy-engine-architecture.md) |
| ADR 010 — Enforcement Hook & Audit | [docs/adr/010-enforcement-hook-consent-audit-harness.md](adr/010-enforcement-hook-consent-audit-harness.md) |
| **ADR 011 — Three-Tier Policy Governance** | [docs/adr/011-three-tier-policy-governance.md](adr/011-three-tier-policy-governance.md) |
| **Policy Authorship Guide** | [docs/policy_authorship_guide.md](policy_authorship_guide.md) |
| Three-Tier Policy Authorship Guide (detailed) | [docs/policies/three-tier-policy-authorship-guide.md](policies/three-tier-policy-authorship-guide.md) |
| E2E Scenario Specification | [docs/e2e-scenario-specification.md](e2e-scenario-specification.md) |
