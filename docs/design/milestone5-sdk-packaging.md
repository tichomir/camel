# Milestone 5 — SDK Packaging & Public API Design

**Document version:** 1.0
**Date:** 2026-03-18
**Status:** Design complete — ready for implementation sprint

---

## 1. Sprint Goal

Package CaMeL as a pip-installable SDK (`camel-security`) with a stable,
typed, thread-safe public API surface.  This milestone delivers `CaMeLAgent`,
`AgentResult`, the `Tool` registration interface, complete type annotations,
docstrings, semantic versioning, and CI pipeline for package publication.

**PRD references:** M5-F1 through M5-F7, NFR-7, NFR-8, NFR-9

---

## 2. PRD — Milestone 5: SDK Packaging & Public API

### M5-F1 — `camel-security` PyPI Package

The package is published as `camel-security` on PyPI.  Installation:

```
pip install camel-security
```

**No native binary dependencies** (no C extensions, no Rust extensions).
The only non-Python dependencies are network calls to LLM provider APIs.

Declared runtime dependencies (from `pyproject.toml`):

```toml
dependencies = [
    "pydantic>=2.0",
    "typing-extensions>=4.0",
    "anthropic>=0.25",
]
```

`google-generativeai` (Gemini) is an **optional** dependency; it is only
imported when `get_backend("gemini", ...)` is called.

### M5-F2 — `CaMeLAgent` Class

Stable constructor accepting `p_llm`, `q_llm`, `tools`, `policies`, and `mode`:

```python
class CaMeLAgent:
    def __init__(
        self,
        p_llm: LLMBackend,
        q_llm: LLMBackend,
        tools: Sequence[Tool],
        policies: PolicyRegistry | None = None,
        mode: ExecutionMode = ExecutionMode.STRICT,
        max_retries: int = 10,
    ) -> None: ...
```

See §4 for the full typed specification.

### M5-F3 — `agent.run(user_query) -> AgentResult`

Async entry point returning a structured result:

```python
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
```

`run_sync` is a convenience wrapper that calls `asyncio.run(self.run(...))`.

### M5-F4 — `AgentResult` Dataclass

Structured return type with documented stability guarantees.  See §5.

### M5-F5 — `Tool` Registration Interface

Dataclass with optional `capability_annotation` and `policies` fields.  See §6.

### M5-F6 — Thread-Safety Validation

Each `agent.run()` call creates a fresh `CaMeLInterpreter` and
`CaMeLOrchestrator` — no interpreter state is shared between concurrent calls.
See §7 for the full contract.

### M5-F7 — Semantic Versioning Policy

A `VERSIONING.md` document at the repository root specifies what constitutes a
breaking change triggering a major-version bump.  See `VERSIONING.md`.

---

## 3. Package Structure

### 3.1 New Package: `camel_security/`

```
camel_security/
├── __init__.py      Stable public API exports
├── agent.py         CaMeLAgent, AgentResult, PolicyDenialRecord
└── tool.py          Tool dataclass
```

### 3.2 Relationship to Existing `camel/` Package

`camel_security` is a **thin stable layer** over the internal `camel`
implementation package.  The two packages coexist in the same repository and
are distributed together under the `camel-security` PyPI name.

```
PyPI package: camel-security
    ├── camel/               internal implementation (all existing modules)
    └── camel_security/      public SDK (new — Milestone 5)
```

Users who need lower-level access can continue to import from `camel` directly;
this is an advanced use case with fewer stability guarantees.

### 3.3 `pyproject.toml` Changes

```toml
[project]
name = "camel-security"           # ← renamed from "camel"
version = "0.5.0"                 # ← bumped from 0.4.0

[tool.setuptools.packages.find]
include = ["camel*", "camel_security*"]  # ← added camel_security*
```

---

## 4. CaMeLAgent — Full Typed Specification

### 4.1 Constructor

```python
class CaMeLAgent:
    """Stable, thread-safe public entry point for camel-security."""

    def __init__(
        self,
        p_llm: LLMBackend,
        # Privileged LLM: generates pseudo-Python execution plans.
        # Must implement LLMBackend.generate (free-form text completion).

        q_llm: LLMBackend,
        # Quarantined LLM: structured extraction from untrusted data.
        # Must implement LLMBackend.generate_structured.
        # May be the same object as p_llm (e.g. using the same provider
        # for both roles) or a cheaper/lighter model.

        tools: Sequence[Tool],
        # At least one Tool is required.
        # Tool names must be unique.
        # Order is preserved in the P-LLM system prompt.

        policies: PolicyRegistry | None = None,
        # None → empty PolicyRegistry (all tool calls implicitly allowed,
        # unless per-tool policies are specified on individual Tool instances).

        mode: ExecutionMode = ExecutionMode.STRICT,
        # Production-safe default.
        # Pass ExecutionMode.NORMAL only for debugging or non-security scenarios.

        max_retries: int = 10,
        # Outer-loop retry ceiling (M2-F8). Default: 10.
    ) -> None: ...
```

**Raises:**
- `ValueError` — if `tools` is empty or any two tools share a `name`
- `TypeError` — if `p_llm` or `q_llm` does not satisfy `LLMBackend`

### 4.2 `run()` Method

```python
async def run(
    self,
    user_query: str,
    # Trusted natural-language instruction. Must not contain CaMeLValue instances.

    user_context: UserContext | None = None,
    # Optional metadata injected into the P-LLM system prompt.
) -> AgentResult: ...
```

**Raises:**
- `MaxRetriesExceededError` — when all retry attempts are exhausted
- `PolicyViolationError` — in evaluation mode when a policy denial is not resolved

### 4.3 Properties

```python
@property
def tools(self) -> tuple[Tool, ...]: ...
# Immutable view of registered Tool objects.

@property
def mode(self) -> ExecutionMode: ...
# The ExecutionMode configured at construction.
```

---

## 5. AgentResult — Full Field Specification

```python
@dataclass(frozen=True)
class AgentResult:
    execution_trace: list[TraceRecord]
    # Ordered list — one TraceRecord per successful tool call.
    # TraceRecord.tool_name: str
    # TraceRecord.args: dict[str, Any]  (raw, CaMeLValue wrappers stripped)
    # TraceRecord.memory_snapshot: dict[str, CaMeLValue]  (full caps intact)

    display_output: list[str]
    # print() outputs from the execution plan, as strings.
    # Empty if the plan contains no print() statements.

    policy_denials: list[PolicyDenialRecord]
    # Policy denial events. Populated in PRODUCTION enforcement mode only.
    # Empty in EVALUATION mode (PolicyViolationError is raised instead).

    audit_log_ref: str
    # "camel-audit:<hex_id>" — opaque token identifying this run's audit scope.

    loop_attempts: int
    # 0-based count of outer-loop planning retries consumed.
    # 0 = first plan succeeded. 10 = max retries hit (success=False).

    success: bool
    # True if execution completed. False if MaxRetriesExceededError was raised.

    final_store: dict[str, Any]
    # Shallow snapshot of interpreter variable store after plan completion.
    # Keys: variable names. Values: CaMeLValue instances.
    # Empty dict when success=False.
    # default_factory=dict ensures backwards-compatible construction.
```

### 5.1 Stability Table

| Field | Type | Added | Stable? |
|---|---|---|---|
| `execution_trace` | `list[TraceRecord]` | v0.5.0 | ✅ Major version only |
| `display_output` | `list[str]` | v0.5.0 | ✅ Major version only |
| `policy_denials` | `list[PolicyDenialRecord]` | v0.5.0 | ✅ Major version only |
| `audit_log_ref` | `str` | v0.5.0 | ✅ Major version only |
| `loop_attempts` | `int` | v0.5.0 | ✅ Major version only |
| `success` | `bool` | v0.5.0 | ✅ Major version only |
| `final_store` | `dict[str, Any]` | v0.5.0 | ✅ Major version only |

---

## 6. Tool — Full Field Specification

```python
@dataclass
class Tool:
    name: str
    # Python identifier used in P-LLM-generated code. Must be unique.

    fn: Callable[..., Any]
    # The underlying callable. Called with raw (unwrapped) argument values.
    # May return a raw Python value or a CaMeLValue.

    description: str = ""
    # Human-readable description for P-LLM system prompt.

    params: str = ""
    # Parameter signature string, e.g. "to: str, subject: str, body: str".
    # Injected into P-LLM system prompt to improve plan quality.

    return_type: str = "Any"
    # Return-type string, e.g. "EmailMessage". Injected into system prompt.

    capability_annotation: CapabilityAnnotationFn | None = None
    # Optional: (return_value, tool_kwargs) -> CaMeLValue
    # None → default annotation: sources={name}, readers=Public

    policies: list[PolicyFn] = field(default_factory=list)
    # Per-tool security policies.
    # Evaluated (in order) before every call to this tool.
    # The first Denied(...) blocks the call.
    # Empty list → no per-tool policies (global registry policies still apply).
```

**NFR-7 compliance proof:**

Adding a new tool requires:
1. Construct `Tool(name="my_tool", fn=my_fn)` — one object
2. Pass it in the `tools` sequence to `CaMeLAgent`
3. Optionally: set `capability_annotation` and/or `policies`

No changes to `CaMeLInterpreter`, `PolicyRegistry`, `ToolRegistry`, or any
core module are required.

---

## 7. Thread-Safety Contract

### 7.1 Per-Run Isolation Design

Each `CaMeLAgent.run()` call executes the following fresh construction:

```python
# --- Per-run construction (thread-isolated) ---
run_tools = {**tool_registry.as_interpreter_tools()}
run_tools["query_quarantined_llm"] = make_query_quarantined_llm(self._q_llm_backend)

interpreter = CaMeLInterpreter(
    tools=run_tools,
    mode=self._mode,
    policy_engine=_build_run_policy_registry(),
)

p_llm_wrapper = PLLMWrapper(backend=self._p_llm_backend)

orchestrator = CaMeLOrchestrator(
    p_llm=p_llm_wrapper,
    interpreter=interpreter,
    tool_signatures=self._tool_signatures,
)

result = await orchestrator.run(user_query)
```

`CaMeLInterpreter` carries all mutable state: `_store`, `_dep_graph`,
`_dep_ctx_stack`, `_audit_log`, etc.  By constructing a fresh instance per
`run()` call, no mutable state is shared between concurrent invocations.

### 7.2 Shared Immutable State

The following are constructed once at `CaMeLAgent.__init__` time and never
mutated afterwards:

| Attribute | Type | Notes |
|---|---|---|
| `_p_llm_backend` | `LLMBackend` | Read-only reference |
| `_q_llm_backend` | `LLMBackend` | Read-only reference |
| `_tools` | `tuple[Tool, ...]` | Immutable tuple |
| `_base_policies` | `PolicyRegistry` | Read-only after construction; callers must not mutate |
| `_tool_registry` | `ToolRegistry` | Built once; `as_interpreter_tools()` always returns new dict |
| `_tool_signatures` | `list[ToolSignature]` | Built once; never mutated |

### 7.3 LLMBackend Thread-Safety

Concurrent `agent.run()` calls share the same `p_llm` and `q_llm` backend
objects.  Thread-safety of these shared objects depends on the provider SDK:

- **`ClaudeBackend`** (Anthropic `anthropic` SDK): Thread-safe for concurrent
  async calls.  Uses `httpx.AsyncClient` which is safe for concurrent use.
- **`GeminiBackend`** (Google `google-generativeai` SDK): Safe for concurrent
  async calls as of the current supported version.

Custom `LLMBackend` implementations must document their own thread-safety
guarantees.

### 7.4 Thread-Safety Validation

Thread-safety is validated by the test suite at:

- `tests/test_multi_backend_swap.py` — confirms backend interchangeability
- Thread-safety integration tests (Milestone 5 sprint) must confirm:
  - `N` concurrent `agent.run()` calls complete without interpreter state corruption
  - Each `AgentResult` contains the correct trace for its own query

---

## 8. Security Model Preserved

The SDK layer does not weaken any security property of the underlying CaMeL
system:

| Security property | How it is preserved |
|---|---|
| P-LLM isolation (never sees tool values) | `PLLMWrapper` is constructed per-run; no tool values reach the P-LLM context |
| STRICT mode default | `CaMeLAgent` constructor defaults to `mode=ExecutionMode.STRICT` |
| Policy enforcement before every tool call | `policy_engine=run_policies` is passed to `CaMeLInterpreter` |
| Exception redaction | `CaMeLOrchestrator` handles all exception redaction internally |
| Allowlist enforcement | Enforced inside `CaMeLInterpreter` regardless of how it is constructed |

### 8.1 Security-Default Versioning Rule

The default value of `mode` in `CaMeLAgent.__init__` is `ExecutionMode.STRICT`.
**This default may never be changed to `NORMAL` without a major-version bump.**
See `VERSIONING.md §2.3` (Security Model Changes).

---

## 9. NFR Compliance

| NFR | Requirement | How Achieved |
|---|---|---|
| NFR-7 | Adding a new tool requires only: Tool dataclass + optional annotation + optional policies | `Tool` dataclass bundles all registration metadata; `CaMeLAgent` builds `ToolRegistry` from the list automatically |
| NFR-8 | Compatible with any `LLMBackend`-conforming implementation | `CaMeLAgent.__init__` validates against the `LLMBackend` protocol; any conforming object accepted |
| NFR-9 | Each component independently unit-testable | `CaMeLAgent` can be tested with mock `LLMBackend` objects; `Tool` and `AgentResult` are pure dataclasses; policy engine is independently testable |

---

## 10. Decisions and Rationale

### D1 — Two separate packages (`camel` + `camel_security`) vs. a single package

**Decision:** Two coexisting packages in the same repository, distributed
together as `camel-security` on PyPI.

**Rationale:**
- The internal `camel` package has already-stabilised APIs used by advanced
  users and tests.  Renaming modules would be a large breaking change.
- `camel_security` provides a curated, stable public surface without requiring
  any refactoring of internal modules.
- The two-package layout is conventional for Python libraries with a
  public/private split (e.g., `django` vs `django.internal`).

### D2 — Fresh interpreter per `run()` vs. long-lived interpreter

**Decision:** Create a fresh `CaMeLInterpreter` for each `run()` call.

**Rationale:**
- Thread-safety without locking — no shared mutable state.
- Predictable security boundary — each run starts with a clean variable store;
  no cross-run capability bleed.
- Cost: one interpreter construction per run is negligible vs. LLM call cost.
- Rejected alternative: a pool of reusable interpreters would require locking
  and a reset protocol — complexity not justified.

### D3 — `run_sync()` as a convenience wrapper

**Decision:** Provide `run_sync()` in addition to the async `run()`.

**Rationale:**
- Many users run CaMeL in scripts or Jupyter notebooks without an existing
  event loop.  `asyncio.run(agent.run(...))` is boilerplate.
- `run_sync()` is explicitly documented as not safe within an existing event
  loop (will raise `RuntimeError: This event loop is already running`).

### D4 — `audit_log_ref` as an opaque string token

**Decision:** `AgentResult.audit_log_ref` is a string token, not a live
reference to the audit log object.

**Rationale:**
- Keeps `AgentResult` serialisable (frozen dataclass, no mutable references).
- Decouples the result type from the audit log storage layer — the token can
  be used to query any audit log backend (file, database, SIEM) by the caller.
- The in-memory `CaMeLInterpreter.audit_log` is accessible via the interpreter
  instance, but the interpreter is not exposed in `AgentResult` (it is local
  to the `run()` call scope).

### D5 — `PolicyDenialRecord.resolved` field

**Decision:** Track whether a denial was resolved (user approved) or rejected.

**Rationale:**
- Distinguishes user-approved overrides from user-rejected cancellations in
  production mode.
- Enables downstream audit analysis: a high volume of `resolved=True` records
  may indicate overly strict policies; `resolved=False` records indicate
  user-cancelled operations.
- In EVALUATION mode, `policy_denials` is always empty (denials raise
  `PolicyViolationError` immediately), so this field only matters in
  production deployments.

---

## 11. Open Items for Implementation Sprint

1. **Thread-safety integration test** — concurrent `agent.run()` test with
   `N=10` parallel queries confirming no state corruption.
2. **CI publish pipeline** — `pyproject.toml` update + GitHub Actions workflow
   for `pip install camel-security` validation and test-PyPI publication.
3. **mypy clean on `camel_security/`** — ensure all public types are fully
   annotated and pass `mypy --strict`.
4. **interrogate docstring coverage** — `camel_security/` must meet the 90%
   docstring coverage threshold set in `pyproject.toml`.
5. **`AgentResult.execution_trace` type** — currently `list[Any]` to avoid
   import cycle with `camel.execution_loop.TraceRecord`.  Consider exporting
   `TraceRecord` from `camel_security` for proper typing.

---

_Document status: Architecture design complete.  Implementation tasks tracked in sprint backlog._
