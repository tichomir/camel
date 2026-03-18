# Changelog

All notable changes to CaMeL are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
CaMeL uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.6.0] — 2026-03-18

### Milestone 5 — Multi-Backend LLM Support & Observability

This release validates and ships `LLMBackend` adapters for Claude (Anthropic),
Gemini 2.5 Pro/Flash (Google), and GPT-4.1/o3/o4-mini (OpenAI), confirming that
CaMeL security guarantees hold equivalently across all three provider backends.
Delivers Prometheus/OpenTelemetry-compatible operational metrics and structured JSON
audit log with configurable sink.

**PRD references:** M5-F23 through M5-F28, NFR-3, NFR-6, NFR-8

#### Added

**`camel.llm.adapters.openai` — OpenAI GPT-4.1/o3/o4-mini adapter**

- **M5-F23** — `OpenAIBackend`: full `LLMBackend` adapter for OpenAI models.
  Supports `gpt-4.1` (native `response_format` JSON schema), `o3`, and `o4-mini`
  (prompt-based JSON extraction fallback, `supports_structured_output()=False`).
  Installed via `pip install camel-security[openai]`.

**Multi-backend validation** (`tests/integration/test_multi_backend_adapters.py`)

- **M5-F23** — Adapter protocol conformance tests: all three backends satisfy the
  `LLMBackend` structural protocol (`isinstance` check).
- **M5-F23** — `get_backend_id()` credential-safety tests: verified no API keys
  appear in `"<provider>:<model>"` identifier strings.
- **M5-F23** — `supports_structured_output()` model-class tests: correct `True`/`False`
  per model variant for all three adapters.
- **M5-F23** — `generate()` / `generate_structured()` contract tests with mock SDK
  clients (no live API calls); error wrapping as `LLMBackendError` confirmed.
- **M5-F26** — Independent P-LLM and Q-LLM backend assignment confirmed: same-provider
  splits, cross-provider splits (Claude+OpenAI, Gemini+Claude), and P-LLM generate()
  isolation invariant verified.
- **M5-F27** — Security equivalence: 0 / 15 injection successes across all three
  providers (5 AgentDojo-style adversarial fixtures × 3 backends).

**`camel.observability` — Prometheus/OpenTelemetry metrics** (`camel/observability/metrics.py`)

- **M5-F24** — `CamelMetricsCollector`: five operational metrics exposed, all scoped
  by `session_id`, `tool_name`, and `policy_name` labels:
  - `camel_policy_denial_rate` (Counter)
  - `camel_qlm_error_rate` (Counter)
  - `camel_pllm_retry_count_histogram` (Histogram, buckets: 0–10)
  - `camel_task_success_rate` (Gauge)
  - `camel_consent_prompt_rate` (Counter)
- **M5-F24** — `get_global_collector()` — module-level singleton collector.
- **M5-F25** — `start_metrics_server(port)` — lightweight HTTP server serving
  Prometheus text format on `GET /metrics`.
- **M5-F25** — Optional `prometheus_client` integration: when the package is installed,
  metrics are also registered with the global Prometheus registry.
- **M5-F25** — OpenTelemetry OTLP push: when `CAMEL_OTEL_ENDPOINT` env var is set,
  metric snapshots are pushed via OTLP/HTTP every 15 seconds.
- Thread-safe: all counter/histogram/gauge operations use a `threading.Lock`.

**`camel.observability` — Structured JSON audit log sink** (`camel/observability/audit_sink.py`)

- **M5-F28** — `AuditSink`: configurable structured JSON audit log sink.
  Three modes via `SinkMode` enum:
  - `SinkMode.FILE`: writes newline-delimited JSON to a file path from
    `CAMEL_AUDIT_SINK_PATH`.
  - `SinkMode.STDOUT`: writes to `sys.stdout`.
  - `SinkMode.EXTERNAL`: invokes a caller-supplied callback with `AuditLogRecord`.
- **M5-F28** — `AuditLogRecord`: typed dataclass for all audit log entries;
  fields: `session_id`, `event_type`, `tool_name`, `policy_name`, `decision`,
  `capability_summary`, `backend_id`, `timestamp` (ISO-8601 UTC).
- **M5-F28** — `AuditSinkConfig`: configures sink mode from environment variables
  (`CAMEL_AUDIT_SINK`, `CAMEL_AUDIT_SINK_PATH`).
- **M5-F28** — `_config_from_env()` / `_reset_default_sink()` — module-level helpers
  for env-driven configuration and test teardown.

**Test coverage**

- `tests/integration/test_multi_backend_adapters.py` — 8 test classes, 28 test
  functions covering all 7 adapter contract areas.
- `tests/test_observability.py` — metrics and audit log sink validation covering all
  five metrics, three sink modes, and NFR-6 event class coverage.

**Documentation**

- `docs/reports/milestone5_multi_backend_test_report.md` — test execution report
  documenting 0 ASR across all three backends, P/Q configurability evidence, metrics
  validation, and audit log validation.  Confirms NFR-3, NFR-6, NFR-8 validated.
- `docs/backend-adapter-developer-guide.md` — added concrete adapter instantiation
  examples for `ClaudeBackend`, `GeminiBackend`, and `OpenAIBackend`; cross-provider
  P-LLM/Q-LLM pairing examples; `AnthropicBackend` alias note.
- `CLAUDE.md` PRD §6 — `LLMBackend` named as core component (§6.6) with interface
  summary.  PRD §9 NFR table: NFR-3, NFR-6, NFR-8 marked as validated (M5/v0.6.0).
  PRD §11 Success Metrics: backend adapter security equivalence metric added
  (target: 0 injection successes across all backends; result: 0/15 — MET).

---

## [0.5.0] — 2026-03-18

### Milestone 5 — SDK Packaging & Public API + Policy Testing Harness & User Consent UX

This release packages CaMeL as a pip-installable SDK (`camel-security`) with a
stable, typed, thread-safe public API surface.  Delivers `CaMeLAgent`, `AgentResult`,
the `Tool` registration interface, complete type annotations, docstrings, semantic
versioning policy, and the SDK CI pipeline for package publication.  Also delivers
the production-grade Policy Testing Harness and User Consent UX (M5-F12 through
M5-F19).

**PRD references:** M5-F1 through M5-F19, NFR-6, NFR-7, NFR-8, NFR-9, Risk L4

#### Added

**`camel_security` public SDK package** (`camel_security/__init__.py`,
`camel_security/agent.py`, `camel_security/tool.py`)

- **M5-F1** — `camel-security` PyPI package: installable via
  `pip install camel-security` with no native binary dependencies.  Runtime
  dependencies: `pydantic>=2.0`, `typing-extensions>=4.0`, `anthropic>=0.25`.
  Gemini support remains an optional soft dependency.

- **M5-F2** — `CaMeLAgent` stable entry point: fully typed constructor accepting
  `p_llm: LLMBackend`, `q_llm: LLMBackend`, `tools: Sequence[Tool]`,
  `policies: PolicyRegistry | None = None`,
  `mode: ExecutionMode = ExecutionMode.STRICT`, `max_retries: int = 10`.
  STRICT mode is the constructor default and may not be silently downgraded without
  a major-version bump (see `VERSIONING.md §2.3`).

- **M5-F3** — `agent.run(user_query) -> AgentResult` async entry point and
  `agent.run_sync(user_query) -> AgentResult` synchronous convenience wrapper.
  Each call creates a **fresh** `CaMeLInterpreter` and `CaMeLOrchestrator` —
  no state shared between concurrent invocations.

- **M5-F4** — `AgentResult` frozen dataclass with seven stable fields:
  `execution_trace`, `display_output`, `policy_denials`, `audit_log_ref`,
  `loop_attempts`, `success`, `final_store`.  Full stability guarantees
  documented in `VERSIONING.md §3`.

- **M5-F5** — `Tool` dataclass registration interface with optional
  `capability_annotation` and `policies` fields.  Satisfies NFR-7:
  adding a new tool requires only constructing one `Tool` object — no changes
  to core interpreter or policy engine code required.

- **M5-F6** — Thread-safety contract: concurrent `agent.run()` calls confirmed
  not to share interpreter state; tested by `tests/test_sdk_thread_safety.py`.

- **M5-F7** — `VERSIONING.md` semantic versioning policy document: defines
  major/minor/patch change categories, `AgentResult` stability policy,
  deprecation policy, and pre-1.0 clause.

**Re-exported names from `camel_security.__all__`:**
`CaMeLAgent`, `AgentResult`, `PolicyDenialRecord`, `Tool`, `ExecutionMode`,
`PolicyRegistry`, `Allowed`, `Denied`, `CaMeLValue`, `Public`, `get_backend`,
`__version__`.

**Documentation updates:**

- `docs/architecture.md` — version bumped to 1.5; Section 15 (SDK Layer) added:
  module layout, public API exports table, `CaMeLAgent` constructor signature,
  `AgentResult` dataclass, `Tool` registration interface, thread-safety contract,
  and wiring diagram.  NFR-7, NFR-8, NFR-9 rows updated to reference `camel_security`.
- `docs/design/milestone5-sdk-packaging.md` — design document status updated to
  Implementation complete.
- `VERSIONING.md` — new document at repository root.
- `README.md` — version badge updated to 0.5.0; SDK quick-start section added.

**SDK CI pipeline** (`.github/workflows/sdk-ci.yml`)

- Build, lint (`ruff`), type-check (`mypy --strict`), and publish-to-test-PyPI
  stages added.  `camel_security/` is included in the `packages.find` configuration.

---

### Milestone 5 — Policy Testing Harness & User Consent UX

This section covers the developer tooling and production consent UX delivered
as part of Milestone 5 (M5-F12 through M5-F19).

#### Added

**`camel_security.testing` — Policy developer tooling** (`camel_security/testing.py`)

- **M5-F12** — `PolicyTestRunner`: batch-evaluates a `PolicyFn` against a list
  of `PolicyTestCase` instances.  Returns a `PolicyTestReport` with per-case
  `PolicyCaseResult` objects and aggregate statistics (`total_cases`, `passed`,
  `failed`, `denied_cases`, `allowed_cases`, `coverage_percent`).  The policy
  is wrapped in a temporary single-policy `PolicyRegistry` for each case so the
  evaluation semantics are identical to production usage.

- **M5-F13** — `CaMeLValueBuilder`: fluent builder for constructing
  `CaMeLValue` instances in test code without a live interpreter.  Supports
  `with_sources()`, `with_readers()`, `with_inner_source()`, `with_value()`,
  and `with_dependency()` chain methods.  `build()` unions explicitly-set and
  accumulated dependency sources/readers using `Public`-absorption semantics.

- **M5-F14** — `PolicySimulator`: dry-run mode that traverses the full
  execution loop against a provided pseudo-Python code plan without executing
  any side-effecting tools.  All registered tools are replaced by no-op stubs
  returning placeholder `CaMeLValue` instances (`sources={tool_name}`,
  `readers=Public`).  Returns a `SimulationReport` listing every
  `SimulatedPolicyEvaluation` recorded during the dry run.  `PolicyViolationError`
  is caught and recorded without aborting the simulation; partial audit data is
  always returned.

- **M5-F15** — `PolicyTestCase` dataclass: describes one test scenario for
  `PolicyTestRunner`.  Fields: `tool_name`, `kwargs: dict[str, CaMeLValue]`,
  `expected_outcome: Literal["Allowed", "Denied"]`, optional
  `expected_reason_contains`, optional `case_id`.

- **M5-F16** — `PolicyTestReport` dataclass with `total_cases`, `passed`,
  `failed`, `denied_cases`, `allowed_cases`, `coverage_percent`, and `results`
  fields.  `coverage_percent` is `denied_cases / total_cases * 100` — measures
  negative-path coverage of the test suite.

- **M5-F17** — `SimulationReport` and `SimulatedPolicyEvaluation` dataclasses:
  `SimulationReport` contains `evaluations`, `denied_tools`, and `allowed_tools`
  lists.  Each `SimulatedPolicyEvaluation` carries `tool_name`, `args_snapshot`,
  `result`, and `reason`.

**`camel_security.consent` + `camel.consent` — Production Consent UX**
(`camel_security/consent.py`, `camel/consent.py`)

- **M5-F18** — `ConsentHandler` ABC: pluggable interface for the user consent
  UX.  Subclass and implement `handle_consent(tool_name, argument_summary,
  denial_reason) -> ConsentDecision`.  The default CLI implementation is
  `DefaultCLIConsentHandler`.  Extension patterns documented in
  `docs/consent-handler-integration.md`: web UI (blocking HTTP), mobile
  (threading.Event callback bridge), asyncio (wrapper pattern), and auto-approve
  (for CI/testing).

- **M5-F18** — `DefaultCLIConsentHandler`: production-ready CLI handler.
  Renders a formatted banner to stdout showing tool name, argument summary, and
  denial reason.  Accepts `A` (Approve once), `R` (Reject), `S` (Approve for
  session).  Repeats on invalid input.

- **M5-F18** — `ConsentDecisionCache`: session-level in-process cache keyed on
  `(tool_name, SHA-256(argument_summary))`.  Populated only on
  `APPROVE_FOR_SESSION`; `APPROVE` and `REJECT` decisions are never stored.
  `lookup()` / `store()` / `clear()` API.  Mitigates user fatigue (Risk L4).

- **M5-F19** — `ConsentAuditEntry` immutable dataclass: appended to
  `CaMeLInterpreter.consent_audit_log` on every consent prompt, including cache
  hits.  Fields: `decision: ConsentDecision`, `timestamp: str` (ISO-8601 UTC),
  `tool_name: str`, `argument_summary: str`, `session_cache_hit: bool`.
  Extends NFR-6 coverage to include consent decisions.

- **M5-F19** — `_resolve_consent()` internal helper: checks the cache first;
  on miss, delegates to the `ConsentHandler`; stores `APPROVE_FOR_SESSION`
  decisions; appends a `ConsentAuditEntry` in both code paths.

**Documentation**

- `docs/policy-authoring-tutorial.md` — new step-by-step policy authoring
  tutorial covering `CaMeLValueBuilder`, `PolicyTestRunner`, and
  `PolicySimulator` with complete worked examples for `send_email` and
  `write_file` policies (including pytest integration).
- `docs/consent-handler-integration.md` — new guide documenting all
  `ConsentHandler` extension patterns (web UI, mobile push, asyncio wrapper,
  auto-approve), `remember_for_session` flag semantics, and `argument_hash`
  computation.
- `docs/security-audit-log.md` — new audit log reference documenting all log
  streams: `AuditLogEntry`, `ConsentAuditEntry`, `ForbiddenImportEvent`,
  `ForbiddenNameEvent`, `DataToControlFlowAuditEvent`,
  `StrictDependencyAdditionEvent`, and `RedactionAuditEvent` schemas.
  Includes NFR-6 compliance summary table.

---

## [0.4.0] — 2026-03-17

### Milestone 4 — STRICT Mode Validation & Hardening + Exception Hardening & Redaction

This release fully validates, extends, and hardens the STRICT execution mode in the
CaMeL interpreter (M4-F1 through M4-F5) and delivers the complete exception hardening
and redaction subsystem (M4-F6 through M4-F9, M4-F17).  All features are delivered and
verified by automated unit and integration tests.

#### Changed

**STRICT mode is now the default** (`camel/interpreter.py`)

- **M4-F5** — `ExecutionMode.STRICT` is now the default value for the `mode` parameter
  in `CaMeLInterpreter.__init__`.  Callers requiring NORMAL mode must pass
  `mode=ExecutionMode.NORMAL` explicitly.  This is a **breaking change** for code that
  relied on the previous NORMAL default.

#### Added

**Extended STRICT mode propagation rules** (`camel/interpreter.py`, `camel/dependency_graph.py`)

- **M4-F1** — For-loop iterable dependency propagation: in STRICT mode the capability
  set and variable-dependency set of the `for`-loop iterable are merged into every
  assignment inside the loop body (including nested blocks), closing the iterable
  control-flow side channel.
- **M4-F2** — If/else conditional test dependency propagation: in STRICT mode the
  capability set and variable-dependency set of the `if`-statement test are merged into
  every assignment in both the true and false branches, closing the branch-observation
  side channel (PRD §7.3).
- **M4-F3** — Post-`query_quarantined_llm()` statement dependency propagation: when a
  Q-LLM call appears anywhere in a statement list, STRICT mode activates a "remainder
  flag" causing every subsequent assignment in the same code block to inherit the Q-LLM
  result's `CaMeLValue` as additional context capabilities.
- **M4-F4** — Block-scoped remainder flag: the post-Q-LLM taint flag is scoped to the
  current `_exec_statements` frame; it propagates into nested blocks encountered after
  the call but resets when the block exits.

**Test coverage — STRICT mode**

- `tests/test_strict_mode.py` — unit tests covering all five M4 propagation rules.
- `tests/test_dependency_graph.py` — extended with post-Q-LLM tainting scenarios.
- `tests/integration/test_strict_mode_e2e.py` — end-to-end integration test confirming
  STRICT mode dependency graph correctness across a complete execution-loop scenario.

**Documentation — STRICT mode**

- `docs/architecture.md` — Section 6 rewritten: STRICT-as-default stated; STRICT Mode
  Propagation Rules subsection added with per-rule descriptions and dependency-graph
  mutation table.
- `README.md` — Execution Modes section updated; `InterpreterConfig(mode='NORMAL')`
  opt-in snippet added; version badge bumped to 0.4.0.
- `docs/design/milestone4-strict-mode-extension.md` — status changed to Implemented;
  Verification section added referencing test files.
- `CLAUDE.md` PRD §6.3 — STRICT/NORMAL description rewritten to reflect STRICT as
  default; all three new propagation rules documented.

---

### Milestone 4 — Exception Hardening & Redaction

#### Added

**Exception hardening subsystem** (`camel/execution_loop.py`, `camel/interpreter.py`,
`camel/dependency_graph.py`)

- **M4-F6** — Dependency-graph-aware taint check: `ExceptionRedactor.classify()` now
  consults the full dependency graph for variables referenced in the failing statement.
  Exception messages are omitted (`message=None` in `RedactedError`) when any upstream
  source outside `{"User literal", "CaMeL"}` is found.  The string `[REDACTED]` appears
  only in the audit log — never in the `RedactedError` data model.  This replaces the
  earlier coarse store-scan which tainted on any untrusted store variable regardless of
  the failing statement's actual dependencies.

- **M4-F7** — `NotEnoughInformationError` (NEIE) handler hardened: NEIE is classified
  with `error_type="NotEnoughInformationError"` and `lineno=<call-site line number>`;
  `message` is always `None`.  The interpreter attaches the AST node's `lineno` to the
  exception via `_attach_lineno()` before re-raising.  `RetryPromptBuilder` emits a
  fixed advisory sentence containing only the line number and static text — zero bytes of
  Q-LLM output reach the P-LLM (NFR-5 coverage).

- **M4-F8** — STRICT mode annotation preservation across NEIE re-generation cycles:
  `AcceptedState` gains two new fields — `dependency_graph_snapshot: dict[str,
  frozenset[str]]` and `dep_ctx_stack_snapshot: list[frozenset[str]]`.  On NEIE, the
  orchestrator snapshots the interpreter's dependency graph and context-dependency stack
  before building the retry prompt, then restores them before the regenerated plan
  executes.  This ensures all STRICT mode taint accumulated before the failing Q-LLM
  call is available to regenerated code.  `DependencyGraph` gains `export()` and
  `import_()` methods to support this.

- **M4-F9** — Loop-body exception STRICT propagation: `_exec_For` wraps the loop body
  in a `try/except`; when an exception propagates from a non-public-iterable loop body
  in STRICT mode, the iterable's dependency set (`__loop_iter_deps__`) and capability
  context (`__loop_iter_caps__`) are attached to the exception before re-raising.  The
  orchestrator reads these fields and pre-seeds the interpreter's `_dep_ctx_stack` before
  the regenerated plan executes, so the iterable's taint is never silently dropped across
  retry cycles.  The `_is_non_public()` helper on `CaMeLInterpreter` encapsulates the
  non-public check.

- **M4-F17** — `RedactionAuditEvent` dataclass: emitted by `ExceptionRedactor.classify()`
  for every exception it processes (redacted or not), providing a complete audit trail.
  Fields: `timestamp` (ISO-8601 UTC), `line_number`, `redaction_reason` (`"untrusted_dependency"` |
  `"not_enough_information"` | `"loop_body_exception"` | `"none"`), `dependency_chain`
  (≤50 `(var_name, source_label)` pairs), `trust_level`, `error_type`,
  `redacted_message_length`, `m4_f9_applied`.  The audit log sink is injected via
  `ExceptionRedactor.__init__(audit_log=...)` (backward-compatible; `None` drops events
  silently).  Extends NFR-6 coverage to include exception redaction events.

**Test coverage — exception hardening**

- `tests/test_exception_hardening.py` — comprehensive unit test suite: dependency-graph
  deep taint scenarios (M4-F6), NEIE lineno extraction and advisory sentence (M4-F7),
  annotation preservation across re-generation (M4-F8), loop-body exception propagation
  (M4-F9), and `RedactionAuditEvent` emission for all four redaction reasons (M4-F17).
- `tests/test_redaction_completeness.py` — extended with dependency-graph-deep taint
  scenarios and NEIE line-number verification.
- `tests/test_execution_loop.py` — extended with `RedactionAuditEvent` emission tests
  and NEIE advisory sentence content tests.

**Documentation — exception hardening**

- `docs/architecture.md` — §3.2 (Q-LLM) updated: NEIE handler rows for M4-F7 and M4-F8
  added to component property table.  §3.3 (CaMeL Interpreter) updated: exception
  redaction, loop-body propagation, and redaction audit log rows added.  §7 (Exception
  Redaction Logic) fully rewritten: dependency-graph-aware algorithm, M4-F7/F8/F9
  subsections, `RedactionAuditEvent` schema, and updated trust classification algorithm.
  Version bumped to 0.4.1.
- `docs/design/milestone4-strict-mode-extension.md` — §10 status changed from
  "Design — pending implementation" to "Implemented — all M4-F6 through M4-F9 and M4-F17
  features delivered and verified"; §10.11 review checklist fully ticked; §10.12
  Verification section added with test file references.
- `CLAUDE.md` PRD §6.2 — updated: NEIE handler strips missing-info content, passes only
  `error_type` + `lineno` to P-LLM (M4-F7); STRICT annotation preservation (M4-F8)
  documented.  PRD §6.3 — updated: exception redaction engine, `[REDACTED]` rule, loop-
  body propagation, and audit log emission documented.  NFR-6 updated to include exception
  redaction events.  L3 (exception side-channel risk) severity reduced from Low-Medium to
  Low; mitigation updated to list M4-F6 through M4-F9.
- `README.md` — milestone status table updated with five new exception hardening rows
  (M4-F6 through M4-F9, M4-F17).

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
