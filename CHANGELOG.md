# Changelog

All notable changes to CaMeL are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
CaMeL uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-03-17

### Milestone 1 — Foundation

This release delivers the complete Milestone 1 foundation: the capability
system, AST interpreter, and dependency graph — integrated into a single
coherent `camel` package with a stable public API.

#### Added

**CaMeLValue & Capability System** (`camel/value.py`)

- `CaMeLValue` — frozen dataclass wrapping any Python value with capability
  metadata: `sources` (origin labels), `inner_source` (sub-field label), and
  `readers` (authorised audience).
- `Public` singleton (`_PublicType`) — open-readers sentinel; absorbing under
  union semantics.
- `Readers` type alias — `frozenset[str] | _PublicType`.
- `wrap()` — convenience constructor with sensible defaults.
- `raw_value()` / `CaMeLValue.raw` — sanctioned accessors for stripping the
  capability wrapper before tool execution.
- Capability propagation functions:
  - `propagate_assignment` — preserves metadata across simple assignments.
  - `propagate_binary_op` — unions capabilities of both operands.
  - `propagate_list_construction` — unions capabilities of all list elements.
  - `propagate_dict_construction` — unions capabilities of all keys and values.
  - `propagate_subscript` — unions capabilities of container and key.

**AST Interpreter** (`camel/interpreter.py`)

- `CaMeLInterpreter` — AST-walking interpreter for the restricted CaMeL
  pseudo-Python grammar subset, using Python's `ast` library.
- Supported statement types: `Assign`, `AugAssign`, `If`, `For`, `Expr`.
- Supported expression types: `Constant`, `Name`, `BinOp`, `UnaryOp`,
  `BoolOp`, `Compare`, `Call`, `Attribute`, `Subscript`, `List`, `Tuple`,
  `Dict`, `JoinedStr` (f-strings).
- All runtime values stored as `CaMeLValue` throughout execution.
- Session state (variable store) persists across multiple `exec()` calls on
  the same interpreter instance.
- `UnsupportedSyntaxError` — raised for any grammar outside the supported
  subset, with offending node type and line number.
- `PolicyViolationError` — raised when a registered security policy blocks a
  tool call.
- `ExecutionMode` enum — `NORMAL` / `STRICT` per-session configuration flag.

**Dependency Graph** (`camel/dependency_graph.py`)

- `DependencyGraph` — immutable frozen snapshot of upstream variable
  dependencies: `variable`, `direct_deps`, `all_upstream`, `edges`.
- `_InternalGraph` — mutable session-scoped graph maintained by the
  interpreter; not part of the public API.
- `NORMAL` mode — records data-assignment dependencies only.
- `STRICT` mode — additionally propagates `if`-test and `for`-iterable
  variable references as dependencies on all variables assigned within those
  blocks, closing timing side-channel vectors.
- `CaMeLInterpreter.get_dependency_graph(variable)` — public query API
  returning a `DependencyGraph` snapshot.

**Q-LLM Wrapper** (`camel/llm/`)

- `QLLMWrapper` — schema-validated structured-output wrapper for the
  Quarantined LLM component; enforces Pydantic output schemas.
- `have_enough_information` field support with `NotEnoughInformationError`.
- Backend adapters: `ClaudeAdapter`, `GeminiAdapter`.
- `LLMProtocol` — structural protocol for backend interoperability.

**Package Integration** (`camel/__init__.py`, `pyproject.toml`)

- Unified `camel` package exporting all Milestone 1 public APIs from a single
  import path.
- `get_dependency_graph(interpreter, variable)` — module-level helper wrapping
  `CaMeLInterpreter.get_dependency_graph`.
- `__version__ = "0.1.0"`.
- `pyproject.toml` with `setuptools` build backend, pinned `requires-python
  >=3.11`, and `dev` extras group.

#### Architecture Decision Records

- `docs/adr/001-q-llm-isolation-contract.md`
- `docs/adr/002-camelvalue-capability-system.md`
- `docs/adr/003-ast-interpreter-architecture.md`
- `docs/adr/004-dependency-graph-architecture.md`

---

## [0.2.0] — 2026-03-17

### Milestone 2 — Dual LLM & Interpreter

This release wires the P-LLM wrapper, Q-LLM wrapper, CaMeL interpreter, and
tool executor into a complete end-to-end execution loop with security isolation
verified by an automated test harness.

#### Added

**P-LLM Wrapper** (`camel/llm/p_llm.py`)

- `PLLMWrapper` — system prompt builder, tool signature injection, user context
  assembly, and Markdown-fenced code-plan parser.
- `ToolSignature` — typed descriptor for tool names, parameters, and return
  types surfaced to the P-LLM.
- `UserContext` — typed container for the user query passed into each
  planning call.
- Isolation contract: `PLLMWrapper` runtime guard rejects any message that
  contains a `CaMeLValue` (tool return value) — the P-LLM never observes tool
  output.

**Q-LLM Wrapper** (`camel/llm/qllm.py`)

- `QLLMWrapper` — schema-validated structured-output wrapper; enforces Pydantic
  output schemas on every Q-LLM response.
- Automatic `have_enough_information: bool` field injection into all caller
  schemas.
- `NotEnoughInformationError` — raised (and redacted) when the Q-LLM signals
  it lacks sufficient data; the missing-data content is never surfaced to the
  P-LLM.

**LLM Backend Layer** (`camel/llm/backend.py`, `camel/llm/adapters/`)

- `LLMBackend` — `runtime_checkable` Protocol with `generate` (free-form) and
  `generate_structured` (Pydantic-constrained) async methods.
- `ClaudeBackend` — Anthropic Claude adapter using the synthetic extraction
  tool pattern for structured output.
- `GeminiBackend` — Google Gemini adapter using `response_mime_type` +
  `response_schema` for JSON-constrained output.
- `get_backend(provider, **kwargs)` — lazy-import factory; swaps providers
  without code changes.
- `LLMBackendError` — unified exception wrapping provider-specific SDK errors.

**Execution Loop Orchestrator** (`camel/execution_loop.py`)

- `CaMeLOrchestrator` — async `run(user_query)` entry point wiring P-LLM,
  interpreter, Q-LLM, and tool dispatch.
- `ExceptionRedactor` — three-tier redaction: full message for trusted
  exceptions; type + line number for exceptions with any untrusted dependency;
  fully redacted for `NotEnoughInformationError`.
- `RetryPromptBuilder` — reconstructs P-LLM retry prompt including
  accepted-state variable names and remaining unexecuted code (M2-F14).
- Retry loop with 10-attempt ceiling; `MaxRetriesExceededError` on exhaustion
  (M2-F8).
- `ExecutionTrace` / `TraceRecord` — ordered `(tool_name, args,
  memory_snapshot)` tuples appended after each successful tool call (M2-F12).
- `DisplayChannel` — `print()` calls in execution plans are routed to a
  separate display stream, distinct from the execution trace (M2-F10).

**Isolation Verification Test Harness** (`tests/harness/`)

- `RecordingBackend` — intercepts all `LLMBackend.complete()` calls for
  post-hoc inspection.
- `IsolationAssertions` — three invariant checkers: no tool output in P-LLM
  context; no free-form Q-LLM output in P-LLM context; redaction completeness.
- `ResultsReporter` — generates a structured sign-off report from harness runs.
- Validated across 50 execution runs and 10 adversarial redaction cases.

#### Architecture Decision Records

- `docs/adr/005-p-llm-wrapper-architecture.md`
- `docs/adr/006-q-llm-dynamic-schema-injection.md`
- `docs/adr/007-execution-loop-orchestrator.md`
- `docs/adr/008-isolation-test-harness-architecture.md`

---

## [0.3.0] — 2026-03-17

### Milestone 3 — Capabilities & Policies

This release delivers the complete Milestone 3 security layer: the capability
assignment engine, the policy engine and registry, six reference security
policies, the enforcement hook with dual-mode consent flow, the security audit
log, and the policy testing harness with AgentDojo scenario replay.

#### Added

**Capability Assignment Engine** (`camel/capabilities/`)

- `CapabilityAnnotationFn` — protocol for tool-specific capability annotation
  functions: `(return_value, tool_kwargs) -> CaMeLValue`.
- `default_capability_annotation` — applied automatically to unannotated tools;
  sets `sources={tool_id}`, `readers=Public`.
- `annotate_read_email` — annotates email tool returns: `inner_source` per
  field (`sender`, `subject`, `body`); `readers` from email recipient metadata.
- `annotate_read_document` / `annotate_get_file` — cloud storage annotators;
  `readers` populated from document sharing permissions (`Set[str]` or `Public`).
- `register_built_in_tools` — registers all built-in annotators with a
  `ToolRegistry` instance in one call.

**Policy Engine & Registry** (`camel/policy/`)

- `SecurityPolicyResult` — sealed base type; exactly two concrete subclasses
  permitted: `Allowed()` and `Denied(reason: str)`.
- `PolicyFn` — type alias `Callable[[str, Mapping[str, CaMeLValue]],
  SecurityPolicyResult]`.
- `PolicyRegistry` — `register(tool_name, policy_fn)` and
  `evaluate(tool_name, kwargs)` interfaces; all-must-allow multi-policy
  composition with first-`Denied` short-circuit; `load_from_env()` for
  configuration-driven deployment-specific policy loading
  (`CAMEL_POLICY_MODULE`).
- Helper functions: `is_trusted`, `can_readers_read_value`, `get_all_sources`.
- NFR-2 verified: policy evaluation path contains no LLM calls.

**Reference Policy Library** (`camel/policy/reference_policies.py`)

- `send_email` — recipient trust + readers superset check; mitigates email
  exfiltration and injected-recipient attacks.
- `send_money` — recipient and amount must have `User` as sole source;
  mitigates financial fraud and amount manipulation.
- `create_calendar_event` — untrusted participant list checked against content
  readers; mitigates calendar injection.
- `write_file` (factory: `make_write_file_policy(owner)`) — path trust check +
  content readers check; mitigates path injection and storage exfiltration.
- `post_message` — channel trust check + restricted-readers content check;
  mitigates Slack channel injection and message exfiltration.
- `fetch_external_url` — URL, params, and body must be trusted; mitigates SSRF
  and URL-parameter exfiltration.
- `configure_reference_policies(registry, file_owner)` — convenience function
  to register all six reference policies.

**Enforcement Hook, Consent Flow & Audit Log** (`camel/interpreter.py`)

- `EnforcementMode` enum — `EVALUATION` (raises `PolicyViolationError`
  immediately) and `PRODUCTION` (invokes consent callback before raising).
- `ConsentCallback` Protocol — synchronous `(tool_name, argument_summary,
  denial_reason) -> bool` interface; surface denial context to user.
- `CaMeLInterpreter` updated: new constructor parameters `enforcement_mode`
  and `consent_callback`; `ValueError` raised if `PRODUCTION` mode is selected
  without a callback.
- `AuditLogEntry` extended with `consent_decision` field
  (`"UserApproved"` | `"UserRejected"` | `None`) per NFR-6.
- `PolicyViolationError` extended with `consent_decision` field; allows
  execution loop to distinguish terminal user rejections from automatic denials.
- NFR-4 verified: policy evaluation overhead ≤100ms per tool call.
- NFR-6 verified: all tool calls, policy evaluations, and consent decisions
  written to `CaMeLInterpreter.audit_log`.
- NFR-9 verified: policy engine, capability system, and enforcement hook each
  independently unit-testable without requiring the other components.

**Policy Testing Harness** (`tests/harness/policy_harness.py`)

- `make_trusted_value` / `make_untrusted_value` / `make_mixed_value` — value
  factory helpers for constructing test `CaMeLValue` fixtures.
- `assert_allowed` / `assert_denied` — policy assertion helpers with optional
  `reason_contains` sub-string check.
- `AgentDojoScenario` dataclass + `replay_agentdojo_scenario` — structured
  adversarial scenario replay against a `PolicyRegistry`.
- `AGENTDOJO_SCENARIOS` — pre-defined catalogue covering all six reference
  policy attack classes.

#### Architecture Decision Records

- `docs/adr/009-policy-engine-architecture.md`
- `docs/adr/010-enforcement-hook-consent-audit-harness.md`
