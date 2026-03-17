# Milestone 4 — Side-Channel Test Execution Report

**Report Date:** 2026-03-17
**Prepared By:** QA Engineer
**Interpreter Version:** CaMeL v0.4.0
**Python Version:** 3.11.15
**pytest Version:** 9.0.2
**Test Suite Location:** `tests/side_channel/`

---

## 1. Executive Summary

The full side-channel test suite was executed against the hardened CaMeL interpreter
(v0.4.0, Milestone 4). All **89 tests passed** with **0 failures, 0 skips, and 0 errors**.

The PRD Section 11 success metric target of **100% pass rate for implemented mitigations**
is **MET**.

---

## 2. Overall Results

| Metric                          | Value |
|---------------------------------|-------|
| Total tests collected           | 89    |
| Tests passed                    | 89    |
| Tests failed                    | 0     |
| Tests skipped                   | 0     |
| Tests errored                   | 0     |
| **Overall pass rate**           | **100.0%** |
| PRD §11 100% target met?        | **YES** |
| Execution time                  | ~0.10 s |

---

## 3. Per-Class Results

### 3.1 Class 1 — Indirect Inference via Loop Count

**Test file:** `tests/side_channel/test_loop_count_inference.py`
**Attack vector:** An observer who can see tool calls may infer a private variable's
value by counting loop iterations when an untrusted iterable controls the loop.
**CaMeL mitigation:** M4-F1 — STRICT mode merges the iterable's dependency set and
capability sources into every variable assigned inside the loop body, causing the
policy engine to detect untrusted provenance before any downstream tool call.
**Interpreter configuration:** STRICT mode (default); one negative test class uses
NORMAL mode to confirm the mitigation is absent without STRICT mode.

| Test Class                            | Tests | Passed | Failed | Pass Rate |
|---------------------------------------|-------|--------|--------|-----------|
| `TestLoopCountInferenceBasic`         | 4     | 4      | 0      | 100%      |
| `TestLoopCountInferenceUpstreamTaint` | 5     | 5      | 0      | 100%      |
| `TestSideChannelReportLoopCount`      | 1     | 1      | 0      | 100%      |
| **Class 1 Total**                     | **10**| **10** | **0**  | **100%**  |

**Key scenarios validated:**
- Loop body variable carries iterable's direct dependency in STRICT mode (M4-F1).
- Loop target variable itself carries iterable dependency in STRICT mode (M4-F1).
- Iterable's capability sources propagate to loop body variables in STRICT mode (M4-F1).
- All variables assigned inside a loop body carry iterable taint, regardless of their value origin (M4-F1).
- Nested loops: inner body carries both outer and inner iterable dependencies (M4-F1).
- Loop over trusted literals does NOT introduce untrusted taint (no over-tainting).
- Post-loop variable retains iterable's sources after loop exits.
- **Negative test (NORMAL mode):** iterable dep does NOT propagate in NORMAL mode — confirming the mitigation requires STRICT mode.

---

### 3.2 Class 2 — Exception-Based Bit Leakage

**Test file:** `tests/side_channel/test_exception_bit_leakage.py`
**Attack vector:** An exception triggered by adversary-controlled data may carry
the raw payload in its message. If forwarded to the P-LLM, this crosses the
trusted/untrusted boundary.
**CaMeL mitigations:**
- M4-F6: dependency-graph-aware taint check — exception messages are replaced with
  `None` when any upstream source in the store is outside `{"User literal", "CaMeL"}`.
- M4-F7: `NotEnoughInformationError` content is fully stripped; only error type and
  call-site line number are forwarded to the P-LLM.
- M4-F9: loop-body exception STRICT propagation — the iterable's taint annotation is
  attached to the exception via `__loop_iter_deps__` and pre-seeded into the
  regenerated plan's dependency context.
- M4-F17: every classification event is written to the security audit log.
**Interpreter configuration:** STRICT mode (default) for all tests in this class.
The `ExceptionRedactor` is tested directly as well as via the interpreter.

| Test Class                                   | Tests | Passed | Failed | Pass Rate |
|----------------------------------------------|-------|--------|--------|-----------|
| `TestExceptionMessageRedactionUntrusted`     | 6     | 6      | 0      | 100%      |
| `TestNEIEContentStripping`                   | 4     | 4      | 0      | 100%      |
| `TestLoopBodyExceptionTaint`                 | 3     | 3      | 0      | 100%      |
| `TestStrictModeExceptionSideChannelClosure`  | 4     | 4      | 0      | 100%      |
| **Class 2 Total**                            | **17**| **17** | **0**  | **100%**  |

**Key scenarios validated:**
- Exception with untrusted dependency has `message=None` after classification (M4-F6).
- Redacted result carries no adversary payload content (M4-F6).
- Exception with only trusted/CaMeL sources preserves its message (M4-F6 negative).
- `RedactionAuditEvent` is emitted for every classified exception (M4-F17).
- Audit event records `redacted_message_length` for redacted exceptions (M4-F17).
- `NotEnoughInformationError` message is always `None` (M4-F7).
- NEIE produces `trust_level="not_enough_information"` (M4-F7).
- NEIE error type is preserved for P-LLM retry signalling (M4-F7).
- `m4_f9_applied=True` in audit event when `__loop_iter_deps__` is present (M4-F17/M4-F9).
- `m4_f9_applied=False` when no loop annotation is present (M4-F17).
- End-to-end: STRICT + M4-F1 + M4-F6 closes the loop-body exception side channel.
- Empty store (no untrusted variables) produces `trust_level="trusted"` — no over-redaction.

---

### 3.3 Class 3 — Timing Primitive Exclusion

**Test file:** `tests/side_channel/test_timing_primitive_exclusion.py`
**Attack vector:** If the interpreter allows the `time` module or any timing
primitives, an adversary could encode a private value into time-delayed tool calls
or use elapsed-time differences to infer private data.
**CaMeL mitigations:**
- M4-F10: any `import` statement in P-LLM-generated code raises `ForbiddenImportError`
  immediately, before any execution.
- M4-F11/M4-F12: the interpreter namespace is restricted to the approved builtin set;
  the `time` module and all timing-related names are unconditionally excluded.
- M4-F13: exclusions are defined in the central `allowlist.yaml` configuration.
- M4-F14: accessing a disallowed name raises `ForbiddenNameError` with the offending
  name.
**Interpreter configuration:** Default (STRICT mode); tests operate on the interpreter
namespace and allowlist configuration directly.

| Test Class                               | Tests | Passed | Failed | Pass Rate |
|------------------------------------------|-------|--------|--------|-----------|
| `TestTimingModuleImportsBlocked`         | 10    | 10     | 0      | 100%      |
| `TestTimingNameAccessBlocked`            | 28    | 28     | 0      | 100%      |
| `TestTimingNamesAbsentFromNamespace`     | 16    | 16     | 0      | 100%      |
| `TestTimingSleepLoopChannelBlocked`      | 3     | 3      | 0      | 100%      |
| `TestPRDSection11TimingSideChannel`      | 5     | 5      | 0      | 100%      |
| **Class 3 Total**                        | **62**| **62** | **0**  | **100%**  |

**Key scenarios validated (selected):**
- `import time`, `from time import sleep/perf_counter/monotonic`, `import datetime`,
  `import timeit` all raise `ForbiddenImportError` with correct `module_name` (M4-F10).
- Import in dead code (`if False: import time`) is still blocked — AST pre-scan runs
  before execution (M4-F10).
- Import check occurs before any side effects: prior assignments are not executed (M4-F10).
- `ForbiddenImportError.lineno` is a positive integer (M4-F10).
- All timing primitives (`time`, `sleep`, `perf_counter`, `monotonic`, `process_time`,
  `clock`, `time_ns`) raise `ForbiddenNameError` on name access (M4-F14).
- `ForbiddenNameError.offending_name` matches the accessed name (M4-F14).
- `str(ForbiddenNameError)` contains the offending name (M4-F14).
- `ForbiddenNameError.lineno` is a positive integer (M4-F14).
- All timing primitives absent from `build_permitted_namespace()` (M4-F12).
- All timing primitives present in `get_excluded_timing_names()` (M4-F13).
- `get_permitted_names()` and `get_excluded_timing_names()` are disjoint sets (M4-F12/M4-F13).
- `datetime` absent from permitted namespace (M4-F12).
- `sleep` as a bare name in a loop body raises `ForbiddenNameError` (sleep-loop covert channel blocked).
- `perf_counter()` call pattern raises `ForbiddenNameError` (timing diff pattern blocked).
- All 15 approved builtins remain present in permitted namespace after timing exclusion (M4-F11).
- Blocked timing import appears in the interpreter's security audit log (NFR-6).

---

## 4. Interpreter Configuration per Test Class

| Test Class                               | Execution Mode  | Notes                                                      |
|------------------------------------------|-----------------|------------------------------------------------------------|
| Loop Count Inference (positive tests)    | STRICT (default)| M4-F1 iterable taint propagation active                   |
| Loop Count Inference (negative test)     | NORMAL (opt-in) | Confirms M4-F1 is absent in NORMAL mode                   |
| Exception Bit Leakage                    | STRICT (default)| M4-F6, M4-F7, M4-F9, M4-F17 all active                   |
| Timing Primitive Exclusion               | STRICT (default)| M4-F10–F14 active; mode does not affect import/name blocks |

STRICT mode is the production-default as of v0.4.0 (M4-F5). NORMAL mode requires
explicit opt-in (`mode=ExecutionMode.NORMAL`) and is provided only for debugging
or non-security-sensitive scenarios.

---

## 5. PRD Section 11 Success Metric Evaluation

PRD Section 11 defines the following success metric for the side-channel test suite:

> **Side-channel test pass rate:** 100% for implemented mitigations

| Metric                                  | Target | Actual | Status    |
|-----------------------------------------|--------|--------|-----------|
| Loop count inference pass rate          | 100%   | 100%   | ✅ MET    |
| Exception-based bit leakage pass rate   | 100%   | 100%   | ✅ MET    |
| Timing primitive exclusion pass rate    | 100%   | 100%   | ✅ MET    |
| **Overall side-channel pass rate**      | **100%** | **100%** | **✅ MET** |

**Conclusion:** The PRD Section 11 target of 100% pass rate for implemented
mitigations is **fully met** by the CaMeL v0.4.0 interpreter in STRICT mode.

---

## 6. Failures and Skips

**There are no failures, skips, or errors in this test run.**

No residual risk classification is required from this test run.

---

## 7. Residual Risks (Documented Per PRD Known Limitations)

Although all implemented mitigations pass at 100%, the following residual risks are
carried forward as documented in the PRD Known Limitations & Risks table:

### L3 — Exception Side Channel (Residual)

**Classification:** Low
**Description:** The primary exception side-channel vectors (loop-body taint via M4-F1,
message redaction via M4-F6, NEIE stripping via M4-F7, loop annotation via M4-F9) are
implemented and validated at 100%. However, deeply nested tool call chains — where an
exception propagates through multiple tool call frames before reaching the interpreter's
exception handler — remain a documented residual risk. The test suite covers the
implemented mitigations but does not claim to eliminate this residual vector.
**Reference:** PRD §10 L3; `docs/design/milestone4-exception-hardening.md §9.2`

### NG4 — Partial Timing Mitigation

**Classification:** Low (explicitly a Non-Goal)
**Description:** The interpreter excludes the `time` module and all known timing
primitives, closing the primary timing side-channel vectors. However, PRD NG4
explicitly states that CaMeL does not guarantee side-channel immunity in all
configurations. Covert timing channels that do not rely on the `time` module
(e.g., timing variation from external tool calls, OS-level scheduling jitter)
are outside scope. This is a documented design constraint, not a test failure.
**Reference:** PRD §3.2 NG4; PRD §7.3

### L6 — ROP-Analogue Attacks

**Classification:** Medium-High
**Description:** Side-channel tests validate per-tool-call policy enforcement.
They do not cover action-chaining scenarios where individually-allowed calls
collectively produce a malicious outcome (ROP-analogue). This is tracked separately
as a defence-in-depth concern; detection via dependency graph analysis is listed as
future work FW-6.
**Reference:** PRD §10 L6; PRD §12 FW-6

---

## 8. Test Environment

| Property                  | Value                                  |
|---------------------------|----------------------------------------|
| Platform                  | macOS (darwin 25.3.0)                  |
| Python                    | 3.11.15 (`/opt/homebrew/opt/python@3.11/bin/python3.11`) |
| pytest                    | 9.0.2                                  |
| pytest-asyncio            | 1.3.0                                  |
| CaMeL version             | 0.4.0                                  |
| Default execution mode    | STRICT (M4-F5)                         |
| Test suite path           | `tests/side_channel/`                  |
| Command                   | `python3.11 -m pytest tests/side_channel/ -v --tb=short` |

---

## 9. Sign-Off

| Criterion                                                      | Status  |
|----------------------------------------------------------------|---------|
| All side-channel tests collected and executed                  | ✅ PASS |
| 100% pass rate — loop count inference class                    | ✅ PASS |
| 100% pass rate — exception bit leakage class                   | ✅ PASS |
| 100% pass rate — timing primitive exclusion class              | ✅ PASS |
| PRD §11 100% target explicitly stated and met                  | ✅ PASS |
| Interpreter configuration documented per test class            | ✅ PASS |
| Residual risks documented (L3, NG4, L6)                        | ✅ PASS |
| No failures requiring root-cause documentation                 | ✅ N/A  |

**Milestone 4 side-channel test phase: COMPLETE — all exit criteria met.**
