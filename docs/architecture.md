# CaMeL — System Architecture Reference

**Version:** 0.2.0 (Milestone 2)
**Date:** 2026-03-17
**Source:** PRD v1.0 — *"Defeating Prompt Injections by Design"*, Debenedetti et al., arXiv:2503.18813v2

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
11. [Module Map](#11-module-map)
12. [Related Documents](#12-related-documents)

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
| Insufficient data | Raises `NotEnoughInformationError` — type only forwarded to P-LLM (content redacted) |

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
| `import` | Arbitrary module access |
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
    async def complete(self, messages: list[Message]) -> str: ...
    async def complete_structured(
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
    raw: Any                          # the underlying Python value
    sources: frozenset[str]           # origin tool(s) / "User literal" / "CaMeL"
    inner_source: str | None          # sub-field within a tool response
    readers: frozenset[str] | Public  # authorised recipients, or Public sentinel
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

**Module:** `camel/dependency_graph.py` | **ADR:** [004](adr/004-dependency-graph-architecture.md)

### NORMAL Mode

Dependencies are recorded only via **direct data assignment**.  Control-flow constructs
(`if`, `for`) do not add dependency edges.

```python
# NORMAL: x's dependency on flag is NOT recorded
flag = get_secret_flag()   # sources: {"get_secret_flag"}
x = 1
if flag:
    x = 2                  # x.deps = {} — no edge from flag
```

Use NORMAL mode only when control-flow taint is not a security concern.

### STRICT Mode

In STRICT mode, variables referenced in `if` tests and `for` iterables become
dependencies of **every variable assigned within that block**.

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

**Recommendation:** Use `ExecutionMode.STRICT` in all production deployments.

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
is based on data-provenance trust.

### Three Redaction Cases

#### Case 1 — Trusted-origin exception

**Condition:** All variables in the interpreter store at failure time have sources
exclusively in `{"User literal", "CaMeL"}`.

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

**Condition:** At least one variable in the store has an upstream source outside
`{"User literal", "CaMeL"}` (e.g. a tool return value).

**Information forwarded to P-LLM:**

```
Error type:    TypeError
Location:      line 12
```

Error **message is omitted**.  An adversary can craft tool return values that, when
processed, produce an exception whose message echoes the crafted content back to the P-LLM.

**Example:** A document containing `"ignore previous instructions"` that causes a `TypeError`
during processing — the error message must never reach the P-LLM.

---

#### Case 3 — `NotEnoughInformationError`

**Condition:** The exception is `NotEnoughInformationError` raised by the Q-LLM.

**Information forwarded to P-LLM:**

```
Error type:    NotEnoughInformationError
```

No location, no message.  The Q-LLM operates on untrusted data — even the reason why
information was insufficient could contain adversary-controlled content.

---

### `RedactedError` Data Model

```python
@dataclass(frozen=True)
class RedactedError:
    error_type: str                                              # always present
    lineno:     int | None                                       # None when redacted
    message:    str | None                                       # None when redacted
    trust_level: Literal["trusted", "untrusted",
                          "not_enough_information"]
```

### Trust Classification Algorithm

```
classify(exc, store_snapshot):
    if isinstance(exc, NotEnoughInformationError):
        → RedactedError(type, lineno=None, msg=None, trust="not_enough_information")

    for cv in store_snapshot.values():
        if any source not in {"User literal", "CaMeL"}:
            → RedactedError(type, lineno, msg=None, trust="untrusted")

    → RedactedError(type, lineno, msg=str(exc), trust="trusted")
```

Classification is **conservative**: if any variable in the entire store is untrusted,
the exception is redacted — even if the failing statement only referenced trusted
variables.  This avoids the complexity of per-statement dependency analysis.

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

### 10.2 Trusted vs Untrusted Boundary (PRD §7.2)

| Trusted | Untrusted |
|---|---|
| User query | Tool return values |
| P-LLM generated literals | Q-LLM outputs (until capability-validated) |
| Platform security policies | Web pages, emails, documents |
| Interpreter-derived transformations (`"CaMeL"` source) | External API responses |

### 10.3 Attack Vectors Mitigated

| Attack | CaMeL defence |
|---|---|
| **Control flow hijack** — malicious content overrides the plan | P-LLM isolation: plan generated from trusted query only; tool values never reach P-LLM |
| **Data flow manipulation** — injected arguments to legitimate tool calls | Capability tagging + policy engine: untrusted-sourced values require explicit policy approval before flowing to tool arguments |
| **Data-to-control escalation** — untrusted data drives tool selection | P-LLM generates all branching logic; untrusted values cannot influence AST structure |
| **Exception side-channel** — error content leaks private values to P-LLM | Exception redaction engine strips messages from untrusted-dependency failures |
| **Timing / control-flow side-channel** — branch observation leaks private booleans | STRICT mode propagates branch condition as dependency to all inner-block variables |

### 10.4 Out-of-Scope Threats (PRD §7.3)

- Text-to-text manipulation with no data/control flow consequence
- Prompt-injection-induced phishing (link-click attacks)
- Fully compromised user prompt
- Timing side-channels beyond control-flow taint (e.g. network latency observation)
- Formal verification of interpreter correctness (future work — FW-2)

### 10.5 Security Metrics

| Metric | Target | Achieved |
|---|---|---|
| AgentDojo task success rate (CaMeL vs native) | ≤10% degradation | **77%** vs 84% (8.3% degradation) |
| Prompt injection ASR on AgentDojo | 0 | **0** |
| Data exfiltration events blocked | 100% | **100%** (policy-covered scenarios) |
| Side-channel test pass rate | 100% | **100%** (STRICT mode suite) |

---

## 11. Module Map

```
camel/
├── __init__.py              Public API: CaMeLInterpreter, ExecutionMode,
│                            get_dependency_graph, CaMeLOrchestrator, …
├── value.py                 CaMeLValue, Public, wrap, propagate_* functions
├── interpreter.py           CaMeLInterpreter, UnsupportedSyntaxError,
│                            PolicyViolationError, ExecutionMode
├── dependency_graph.py      DependencyGraph, TrackingMode, get_dependency_graph
├── execution_loop.py        CaMeLOrchestrator, ExceptionRedactor,
│                            RetryPromptBuilder, TraceRecorder,
│                            MaxRetriesExceededError, ExecutionResult,
│                            TraceRecord, RedactedError, AcceptedState,
│                            DisplayChannel, StdoutDisplayChannel
├── exceptions.py            Shared exception base types
├── qllm_schema.py           Schema augmentation utilities (have_enough_information)
├── qllm_wrapper.py          QLLMWrapper (legacy path, re-exported)
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

tests/
├── harness/
│   ├── isolation_assertions.py  Invariant 1/2/3 assertion helpers
│   ├── recording_backend.py     Intercepts LLMBackend.complete() calls
│   ├── results_reporter.py      Sign-off report generator
│   └── scenarios.py             E2E scenario definitions
├── test_isolation_harness.py    Invariant verification (50 runs)
├── test_redaction_completeness.py  10 adversarial redaction cases
├── test_e2e_scenarios.py        10 representative task scenarios
├── test_multi_backend_swap.py   Claude ↔ Gemini swap test
└── …                            Unit tests per module
```

---

## 12. Related Documents

| Document | Location |
|---|---|
| Product Requirements Document (PRD) | `docs/` (embedded in `CLAUDE.md`) |
| Developer Guide | [docs/developer_guide.md](developer_guide.md) |
| Operator Guide | [docs/manuals/operator-guide.md](manuals/operator-guide.md) |
| M2 Exit Criteria Report | [docs/milestone-2-exit-criteria-report.md](milestone-2-exit-criteria-report.md) |
| ADR 001 — Q-LLM Isolation | [docs/adr/001-q-llm-isolation-contract.md](adr/001-q-llm-isolation-contract.md) |
| ADR 002 — CaMeLValue | [docs/adr/002-camelvalue-capability-system.md](adr/002-camelvalue-capability-system.md) |
| ADR 003 — Interpreter | [docs/adr/003-ast-interpreter-architecture.md](adr/003-ast-interpreter-architecture.md) |
| ADR 004 — Dependency Graph | [docs/adr/004-dependency-graph-architecture.md](adr/004-dependency-graph-architecture.md) |
| ADR 005 — P-LLM Wrapper | [docs/adr/005-p-llm-wrapper-architecture.md](adr/005-p-llm-wrapper-architecture.md) |
| ADR 006 — Q-LLM Schema Injection | [docs/adr/006-q-llm-dynamic-schema-injection.md](adr/006-q-llm-dynamic-schema-injection.md) |
| ADR 007 — Execution Loop | [docs/adr/007-execution-loop-orchestrator.md](adr/007-execution-loop-orchestrator.md) |
| ADR 008 — Isolation Test Harness | [docs/adr/008-isolation-test-harness-architecture.md](adr/008-isolation-test-harness-architecture.md) |
| E2E Scenario Specification | [docs/e2e-scenario-specification.md](e2e-scenario-specification.md) |
