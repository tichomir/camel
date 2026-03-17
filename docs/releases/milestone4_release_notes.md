# CaMeL v0.4.0 — Milestone 4 Release Notes

**Release:** v0.4.0
**Date:** 2026-03-17
**Milestone:** Milestone 4 — Hardening & Side-Channel Mitigations
**Status:** Generally Available

---

## Overview

CaMeL v0.4.0 completes Milestone 4: the comprehensive hardening phase that closes
the primary side-channel and control-flow escalation vectors identified in the threat
model (PRD §7.3, §10).  This release delivers 18 new features (M4-F1 through M4-F18),
a 89-test side-channel test suite (100% pass rate), and a complete documentation
refresh across all milestone documents.

This release marks the completion of all four Milestone 4 sprints and the full
side-channel test suite phase.  All PRD §11 success metric targets for Milestone 4
are met.

---

## Security Improvements

### Sprint 1 — STRICT Mode Validation & Hardening (M4-F1 – F5)

**Problem:** CaMeL's NORMAL mode dependency tracking did not propagate untrusted
provenance through control-flow constructs (for-loops, if/else branches, post-Q-LLM
statements).  This allowed untrusted data to reach tool arguments without the policy
engine detecting its provenance.

**Delivered:**

| Feature | Description |
|---|---|
| **M4-F1** | For-loop iterable dependency propagation: the iterable's capability and dependency set is merged into every assignment inside the loop body, including nested blocks. Closes the loop-count inference side-channel. |
| **M4-F2** | If/else condition dependency propagation: the condition's capability and dependency set is merged into every assignment in both branches. Closes the branch-observation side-channel. |
| **M4-F3** | Post-Q-LLM remainder propagation: all assignments following a `query_quarantined_llm()` call in the same code block inherit the Q-LLM result's capabilities as context dependencies. |
| **M4-F4** | Block-scoped remainder flag: the M4-F3 propagation flag is scoped to the current `_exec_statements` frame and resets on block exit. |
| **M4-F5** | STRICT mode as default: `ExecutionMode.STRICT` is the `CaMeLInterpreter` constructor default. NORMAL mode requires `mode=ExecutionMode.NORMAL` explicit opt-in. |

**Security outcome:** The three primary untrusted-data propagation vectors that
bypassed NORMAL mode tracking are closed.  All new deployments automatically benefit
from STRICT mode without additional configuration.

---

### Sprint 2 — Exception Hardening & Redaction (M4-F6 – F9, M4-F17)

**Problem:** Exception messages raised from operations on untrusted data could carry
adversary-controlled content across the trusted/untrusted boundary to the P-LLM
via the retry prompt.  `NotEnoughInformationError` could also leak Q-LLM content.

**Delivered:**

| Feature | Description |
|---|---|
| **M4-F6** | Dependency-graph-aware exception redaction: exceptions are classified via a full dependency graph walk; messages are replaced with `None` (`[REDACTED]` in audit log) when any upstream source is outside `{"User literal", "CaMeL"}`. |
| **M4-F7** | `NotEnoughInformationError` content stripping: NEIE message is always `None` in `RedactedError`; only error type and call-site line number are forwarded; P-LLM receives a static advisory. |
| **M4-F8** | STRICT mode annotation preservation across NEIE: on NEIE, the dependency graph and `_dep_ctx_stack` are snapshotted and restored before the regenerated plan executes — accumulated taint annotations are never dropped across retry cycles. |
| **M4-F9** | Loop-body exception STRICT propagation: when an exception originates inside a non-public-iterable loop body (STRICT mode), `__loop_iter_deps__` and `__loop_iter_caps__` are attached to the exception and pre-seeded into the regenerated plan's dependency context. |
| **M4-F17** | Redaction audit events: every exception processed by `ExceptionRedactor.classify()` emits a `RedactionAuditEvent` to the security audit log with timestamp, line number, redaction reason, dependency chain, trust level, error type, redacted message length, and `m4_f9_applied` flag. |

**Security outcome:** No untrusted data can reach the P-LLM through the exception
channel.  All redaction decisions are auditable.  STRICT mode taint is preserved
across all retry cycles.

---

### Sprint 3 — Module & Builtin Allowlist Enforcement (M4-F10 – F14)

**Problem:** The interpreter had no namespace restrictions.  P-LLM-generated code
could import the `time` module, access arbitrary Python builtins, or use reflection
primitives to escape the capability-tracking layer.

**Delivered:**

| Feature | Description |
|---|---|
| **M4-F10** | `ForbiddenImportError`: immediate raise on any `import` or `from … import` statement, via a pre-scan before any statement executes. Dead-code imports are still blocked. |
| **M4-F11** | Builtin allowlist: interpreter namespace restricted to 16 approved names — `len`, `range`, `list`, `dict`, `str`, `int`, `float`, `bool`, `set`, `isinstance`, `print`, `enumerate`, `zip`, `sorted`, `min`, `max`. |
| **M4-F12** | Timing primitive exclusion: `time` module and all timing-related names (`sleep`, `perf_counter`, `monotonic`, `process_time`, `clock`, `datetime`, `timeit`, etc.) unconditionally removed from the interpreter namespace. |
| **M4-F13** | Central allowlist configuration: `camel/config/allowlist.yaml` is the single source of truth for the permitted namespace; any modification requires a named-reviewer security sign-off. |
| **M4-F14** | `ForbiddenNameError`: raised with the offending name for any disallowed name access; includes `offending_name` and `lineno` attributes. |

**Security outcome:** The interpreter execution environment is now a minimal,
auditable sandbox.  The direct timing side-channel (via explicit timing primitives)
is closed.  All disallowed access attempts are logged to the security audit log.

---

### Sprint 4 — Data-to-Control-Flow Escalation Detection (M4-F15, M4-F16, M4-F18)

**Problem:** P-LLM-generated code could use a variable populated with untrusted
data as a function call operand, allowing adversary-controlled content to determine
*which tool* fires at runtime — bypassing the policy engine's tool-selection audit.

**Delivered:**

| Feature | Description |
|---|---|
| **M4-F15** | `DataToControlFlowWarning` detector: runtime check in `_eval_Call` that fires immediately after evaluating the `func` operand; emits a structured warning when any upstream source of the callable is outside `{"User literal", "CaMeL"}`. Covers both direct and indirect (dependency-graph-resolved) untrusted sources. |
| **M4-F16** | Elevated user consent gate: execution pauses on M4-F15 detection; in PRODUCTION mode, an `ElevatedConsentCallback` is invoked; in EVALUATION mode, `DataToControlFlowEscalationError` is raised immediately. The gate fires before policy engine evaluation and cannot be bypassed by prior policy approvals. Default (no callback configured): always-reject. |
| **M4-F18** | Per-statement STRICT mode dependency addition audit log: `StrictDependencyAdditionEvent` is emitted for each assignment where STRICT mode adds dependencies beyond NORMAL mode tracking; deduplicated per `(lineno, variable)` to prevent log flooding. Accessible via `interpreter.strict_dep_audit_log`. |

**Security outcome:** Data-to-control-flow escalation is now a runtime-detected and
blocked attack vector.  Every escalation event is recorded in the security audit log
with a complete provenance trace.

---

### Sprint 5 — Side-Channel Test Suite (89 tests, 100% pass rate)

The automated side-channel test suite in `tests/side_channel/` validates all three
attack classes defined in PRD §11:

| Test Class | File | Tests | Pass Rate | Vector Closed |
|---|---|---|---|---|
| Indirect inference via loop count | `test_loop_count_inference.py` | 10 | 100% | M4-F1 |
| Exception-based bit leakage | `test_exception_bit_leakage.py` | 17 | 100% | M4-F6, F7, F9, F17 |
| Timing primitive exclusion | `test_timing_primitive_exclusion.py` | 62 | 100% | M4-F10 – F14 |
| **Overall** | `tests/side_channel/` | **89** | **100%** | All M4 mitigations |

Full test execution report: `docs/reports/milestone4_side_channel_test_report.md`.

---

## PRD Section 11 Success Metrics

### Security Metrics

| Metric | Target | Achieved | Status |
|---|---|---|---|
| Prompt injection attack success rate (ASR) | 0 on AgentDojo | 0 (policy coverage maintained from M3) | ✅ Met |
| Data exfiltration events blocked | 100% of policy-covered scenarios | 100% (reference policies, M3 enforcement) | ✅ Met |
| **Side-channel test pass rate** | **100% for implemented mitigations** | **100% (89/89 tests)** | **✅ Met** |

### Milestone 4 Specific Metrics

| Metric | Target | Achieved | Status |
|---|---|---|---|
| STRICT mode propagation rules implemented | M4-F1 – F4 | All four implemented and verified | ✅ Met |
| STRICT mode as constructor default | M4-F5 | `ExecutionMode.STRICT` is default | ✅ Met |
| Exception redaction completeness | All 4 scenarios covered | M4-F6, F7, F8, F9 cover all cases | ✅ Met |
| Allowlist enforcement | 16 permitted builtins; timing excluded | `allowlist.yaml` with 16 entries; 15+ timing names excluded | ✅ Met |
| Escalation detection | M4-F15, F16 delivered | DataToControlFlowWarning + gate | ✅ Met |
| Audit log coverage | NFR-6: all security events logged | RedactionAuditEvent, DataToControlFlowAuditEvent, StrictDependencyAdditionEvent, ForbiddenImportEvent, ForbiddenNameEvent all emitted | ✅ Met |

### Metrics Remaining Residual (Not Fully Met)

| Metric | Target | Residual | Reference |
|---|---|---|---|
| Side-channel immunity in all configurations | PRD NG4 (explicitly partial) | Tool-call timing channels, CPython instruction variance outside scope | NG4, §5.3 |
| Exception side-channel elimination | Full elimination | Binary oracle residual; deeply nested tool chain residual | L3, §5.1 |
| ROP-analogue attack prevention | PRD L6 (explicitly medium-high risk) | Multi-hop chained calls not covered | L6, §5.4 |

---

## Known Residual Risks (Entering Milestone 5)

The following residual risks are carried forward from Milestone 4.  All are documented
in the PRD Known Limitations & Risks table (§10) and the Security Hardening Design
Document (`docs/design/security_hardening_design.md §5`).

### L3 — Exception Side Channel (Residual) | Severity: Low

**What was closed:** Exception message content reaching the P-LLM (M4-F6); Q-LLM
NEIE content leakage (M4-F7); taint drop on NEIE retry (M4-F8); loop taint drop on
exception retry (M4-F9).

**What remains:**
- Binary oracle: the P-LLM still learns that an exception occurred at a given line.
  A carefully crafted adversary could use this as a one-bit-per-retry oracle.
- Deeply nested tool exception chains: exceptions originating inside multi-level tool
  call frames may not carry loop-iterable metadata expected by M4-F9.

**Explicit caveat:** The binary oracle residual is a known property of any retry-loop
architecture and is acknowledged in PRD §3.2 NG4.  Deeply nested chains require
adversary control over tool internals, which is outside the CaMeL threat model.

---

### L4 — User Fatigue from Policy Denials | Severity: Medium

**What was improved in M4:** M4-F16 introduced a distinct elevated consent class for
escalation events, separating high-risk escalation prompts from routine policy-denial
prompts.

**What remains:** False-positive consent prompt rate on benign tasks remains a function
of policy granularity and tool annotation quality.  Third-party tools without CaMeL
capability annotations may produce higher false-positive denial rates.

**Explicit caveat:** CaMeL provides reference policies and the AgentDojo policy mapping
to assist deployments in minimising false-positive consent rates.  The PRD §11 target
(≤20% of benign tasks on well-annotated domains) is achievable with careful policy
configuration.

---

### NG4 — Partial Timing Mitigation | Severity: Low

**What was closed:** Direct timing primitives (`time`, `sleep`, `perf_counter`, etc.)
via M4-F10, M4-F12, M4-F13, M4-F14.  Timing primitive exclusion test suite: 62/62
tests pass (100%).

**What remains:**
- Indirect iteration-count timing: an adversary-controlled iterable still executes N
  calls; an external observer measuring total execution time can infer N.
- Tool-implementation timing leakage: variable latency of external API calls is
  observable from outside the process.
- CPython instruction-dispatch variance: low-severity theoretical channel.

**Explicit caveat:** PRD §3.2 NG4 explicitly states this is a Non-Goal.  Residual
timing channels require external-observer capabilities outside the threat model scope.
Deployments with strict timing requirements should supplement CaMeL with process
isolation (containers, VMs) or constant-time tool execution wrappers.

---

### L6 — ROP-Analogue Action Chaining | Severity: Medium-High

**What was addressed in M4:** M4-F15/F16 detect direct data-to-control-flow escalation
(a single data-derived callable).

**What remains:** Multi-hop sequences where each individual call is policy-approved
are not detected.  An adversary who chains individually-allowed tool calls to produce
a collectively malicious outcome (the ROP-analogue) is not covered by current
per-call policy enforcement.

**Explicit caveat:** This attack class requires the adversary to construct a call chain
where every individual call satisfies platform security policies — a significantly
higher bar than direct prompt injection.  Defence-in-depth: CaMeL should be combined
with model-level robustness training and action-sequence anomaly detection (FW-6).

---

## Breaking Changes

### Default Execution Mode Changed to STRICT (M4-F5)

`CaMeLInterpreter` now defaults to `ExecutionMode.STRICT`.  Existing code that
relied on NORMAL mode behaviour (no control-flow dependency propagation) will observe
more frequent policy denials for untrusted-provenance arguments.

**Migration:** If a non-production test or debugging scenario requires NORMAL mode,
pass `mode=ExecutionMode.NORMAL` explicitly.  Security deployments must not opt out
of STRICT mode without explicit risk acceptance.

### New Exceptions: ForbiddenImportError, ForbiddenNameError, DataToControlFlowEscalationError

Three new exception types are raised by the interpreter in hardened mode.  Code that
catches `Exception` broadly in integration tests may need to handle these.

**Migration:** Import from `camel.exceptions` or `camel` and handle them explicitly.

---

## New Exports

The following symbols are newly exported from `camel/__init__.py` in v0.4.0:

```python
from camel import (
    # Exceptions
    ForbiddenImportError,
    ForbiddenNameError,
    DataToControlFlowWarning,
    DataToControlFlowEscalationError,
    # Execution mode
    ExecutionMode,          # now includes STRICT (default) and NORMAL
    # Audit event types
    StrictDependencyAdditionEvent,
    RedactionAuditEvent,
    # Callback protocols
    ElevatedConsentCallback,
)
```

---

## Documentation Published

| Document | Location | Notes |
|---|---|---|
| Security Hardening Design Document | `docs/design/security_hardening_design.md` | New in v0.4.0 — standalone consolidated reference |
| Milestone 4 Release Notes | `docs/releases/milestone4_release_notes.md` | This document |
| Milestone 4 Design Document | `docs/milestone4_design.md` | Updated to v1.4 — all 5 sections complete |
| Side-Channel Test Report | `docs/reports/milestone4_side_channel_test_report.md` | 89 tests, 100% pass rate |
| STRICT Mode Extension Design | `docs/design/milestone4-strict-mode-extension.md` | M4-F1 – F5 implementation details |
| Exception Hardening Design | `docs/design/milestone4-exception-hardening.md` | M4-F6 – F9, M4-F17 implementation details |
| Escalation Detection Design | `docs/design/milestone4-escalation-detection.md` | M4-F15, M4-F16, M4-F18 implementation details |
| Security Hardening Allowlist | `docs/security_hardening_allowlist.md` | Updated to v1.4 — full allowlist audit trail |
| Architecture Reference | `docs/architecture.md` | Updated to v1.4 |

---

## Entry Criteria for Milestone 5

All Milestone 4 exit criteria are met.  The following conditions are satisfied for
Milestone 5 (Production Readiness & Ecosystem Integration) to proceed:

| Criterion | Status |
|---|---|
| All 18 Milestone 4 features (M4-F1 – M4-F18) implemented and verified | ✅ |
| PRD §11 side-channel test pass rate target (100%) achieved | ✅ |
| STRICT mode is the production default and cannot be accidentally disabled | ✅ |
| All exception-handling pathways redact untrusted content before P-LLM | ✅ |
| Module/builtin allowlist enforced with central auditable configuration | ✅ |
| Data-to-control-flow escalation detection and blocking operational | ✅ |
| Residual risks L3, L4, NG4, L6 documented with explicit caveats | ✅ |
| All security events written to audit log (NFR-6 compliance) | ✅ |
| Security Hardening Design Document published | ✅ |
| Milestone 4 release notes published | ✅ |

**Milestone 5 may proceed.** Recommended Milestone 5 focus areas based on residual
risk priorities:

1. **Production packaging and deployment hardening** (NFR-1 process-level isolation).
2. **AgentDojo full benchmark evaluation** — measure utility vs. security tradeoff with
   all Milestone 4 hardening active (confirm ≤10% degradation target, PRD §3.1 G5).
3. **Third-party tool adapter templates** (PRD §10 L7 ecosystem adoption gap).
4. **ROP-analogue detection research** (PRD §12 FW-6 — recommended given L6 severity).
5. **Formal verification scoping** (PRD §12 FW-2 — at least a partial proof of
   interpreter enforcement properties).

---

## Acknowledgements

CaMeL Milestone 4 is based on:

> "Defeating Prompt Injections by Design" — Debenedetti et al.,
> Google / Google DeepMind / ETH Zurich (arXiv:2503.18813v2)

The side-channel mitigation design (STRICT mode propagation rules, exception redaction,
timing exclusion) addresses threat classes identified in §3 and §5.3 of the source paper.
