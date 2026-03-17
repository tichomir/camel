# Milestone 1 — Exit Criteria Checklist

_Generated: 2026-03-17 | Validated by: `tests/test_exit_criteria.py`_

This document maps each Milestone 1 exit criterion to the test function(s) that provide
evidence of satisfaction, together with the expected evidence and current pass/fail status.

---

## EC-1 — 100% Unit Test Pass Rate

| Field | Value |
|-------|-------|
| **Criterion** | All Milestone 1 unit tests pass with zero failures. |
| **Test function** | `test_ec1_full_milestone1_test_suite_passes` |
| **Evidence** | `subprocess pytest tests/test_value.py tests/test_interpreter.py tests/test_dependency_graph.py` exits with code 0. |
| **Status** | ✅ PASS |

---

## EC-2 — ≥20 Dependency-Graph Programs Verified

| Field | Value |
|-------|-------|
| **Criterion** | At least 20 distinct dependency-graph programs are exercised and pass. |
| **Test functions** | `test_ec2_dependency_graph_program_count_at_least_20`, `test_ec2_dependency_graph_programs_all_pass` |
| **Evidence** | `pytest --collect-only tests/test_dependency_graph.py` yields ≥20 test items; full run exits with code 0. |
| **Status** | ✅ PASS |

---

## EC-3 — CaMeLValue Round-Trip Fidelity

| Field | Value |
|-------|-------|
| **Criterion** | Wrap a value, propagate through all supported operation classes, assert unwrapped result and capability union are correct. |

| Operation class | Test function | Status |
|----------------|---------------|--------|
| Arithmetic (binary op) | `TestEC3CaMeLValueRoundTrip::test_ec3a_arithmetic` | ✅ PASS |
| String formatting (f-string) | `TestEC3CaMeLValueRoundTrip::test_ec3b_string_format` | ✅ PASS |
| List access (subscript) | `TestEC3CaMeLValueRoundTrip::test_ec3c_list_access` | ✅ PASS |
| Dict access (subscript) | `TestEC3CaMeLValueRoundTrip::test_ec3d_dict_access` | ✅ PASS |
| Conditional branch | `TestEC3CaMeLValueRoundTrip::test_ec3e_conditional_branch`, `test_ec3e_conditional_branch_else_taken` | ✅ PASS |
| For-loop | `TestEC3CaMeLValueRoundTrip::test_ec3f_for_loop` | ✅ PASS |
| List construction | `TestEC3CaMeLValueRoundTrip::test_ec3g_list_construction` | ✅ PASS |
| Dict construction | `TestEC3CaMeLValueRoundTrip::test_ec3h_dict_construction` | ✅ PASS |
| Assignment propagation | `TestEC3CaMeLValueRoundTrip::test_ec3i_assignment_propagation` | ✅ PASS |

---

## EC-4 — ≥15 Negative Syntax Tests

| Field | Value |
|-------|-------|
| **Criterion** | ≥15 unsupported constructs each raise `UnsupportedSyntaxError` with correct `node_type` and `lineno ≥ 1`. |
| **Test function** | `test_ec4_unsupported_syntax_raises_correct_error` (parametrized — 17 cases) |

| ID | Code | Expected `node_type` | Status |
|----|------|----------------------|--------|
| `while` | `while True: pass` | `While` | ✅ PASS |
| `class_def` | `class Foo: pass` | `ClassDef` | ✅ PASS |
| `function_def` | `def foo(): pass` | `FunctionDef` | ✅ PASS |
| `import` | `import os` | `Import` | ✅ PASS |
| `import_from` | `from os import path` | `ImportFrom` | ✅ PASS |
| `delete` | `del x` | `Delete` | ✅ PASS |
| `raise` | `raise ValueError()` | `Raise` | ✅ PASS |
| `assert` | `assert True` | `Assert` | ✅ PASS |
| `try_except` | `try: ... except Exception: ...` | `Try` | ✅ PASS |
| `with` | `with open('f') as fh: ...` | `With` | ✅ PASS |
| `lambda` | `x = lambda: 0` | `Lambda` | ✅ PASS |
| `list_comp` | `x = [i for i in []]` | `ListComp` | ✅ PASS |
| `dict_comp` | `x = {k: v for k, v in []}` | `DictComp` | ✅ PASS |
| `set_comp` | `x = {i for i in []}` | `SetComp` | ✅ PASS |
| `generator_exp` | `x = (i for i in [])` | `GeneratorExp` | ✅ PASS |
| `set_literal` | `x = {1, 2}` | `Set` | ✅ PASS |
| `if_expr` | `x = 1 if True else 2` | `IfExp` | ✅ PASS |

**Total: 17 cases (≥15 ✅)**

---

## EC-5 — Session Persistence Across 3 Sequential `exec()` Calls

| Field | Value |
|-------|-------|
| **Criterion** | Variable state carries forward correctly across exactly 3 sequential `exec()` invocations on one interpreter instance. |
| **Test functions** | `test_ec5_session_persistence_three_sequential_execs`, `test_ec5_session_persistence_capability_metadata_carries_forward` |
| **Evidence** | Exec 1 assigns `a=10`; Exec 2 computes `b=a+5=15` confirming `a` is still in store; Exec 3 computes `c=a+b=25` confirming both `a` and `b` persist. Capability metadata (sources, readers) also verified to carry forward. |
| **Status** | ✅ PASS |

---

## EC-6 — STRICT Mode Loop-Dependency Regression

| Field | Value |
|-------|-------|
| **Criterion** | In STRICT mode, loop-body variable's `direct_deps` includes the iterable source variable (not just the loop target). |
| **Test functions** | `test_ec6_strict_loop_body_var_depends_on_iterable`, `test_ec6_strict_loop_body_var_depends_on_iterable_with_accumulator`, `test_ec6_strict_loop_body_carries_iterable_not_just_loop_var` |
| **Evidence** | `interp.get_dependency_graph("result").direct_deps` contains `"items"` in STRICT mode. Negative test confirms `"data"` is absent from `direct_deps` in NORMAL mode, confirming the regression is STRICT-specific. |
| **Status** | ✅ PASS |

---

---

## EC-7 — Documentation Coverage ≥ 90%

| Field | Value |
|-------|-------|
| **Criterion** | All public classes, methods, and functions in the `camel` package have docstrings. Docstring coverage must be ≥ 90% as measured by `interrogate` (magic/dunder methods and `__init__` constructors excluded, as they are documented in the class-level `Parameters` section). |
| **Tool** | `interrogate>=1.5` (added to `[project.optional-dependencies].dev` in `pyproject.toml`) |
| **Configuration** | `[tool.interrogate]` in `pyproject.toml`: `ignore-init-method = true`, `ignore-magic = true`, `fail-under = 90` |
| **CI enforcement** | `Docstring coverage (interrogate)` step in `.github/workflows/ci.yml` — runs `interrogate camel/ --ignore-init-method --ignore-magic --fail-under 90 -v` |
| **Measurement command** | `interrogate camel/ --ignore-init-method --ignore-magic --fail-under 90 -v` |
| **Current coverage** | 100% (71 / 71 symbols covered) |
| **Minimum threshold** | 90% |
| **Status** | ✅ PASS |

---

## Milestone 3 — NFR Compliance Verification

_Verified by: `tests/test_e2e_enforcement.py`, `tests/test_policy.py`, `tests/test_policy_harness.py`_
_Date: 2026-03-17_

---

### NFR-2 — Synchronous, Deterministic Policy Evaluation (No LLM Involvement)

| Field | Value |
|-------|-------|
| **Requirement** | All policy evaluation must occur synchronously and deterministically; no LLM calls permitted in the policy evaluation path. |
| **Test function** | `test_nfr2_policy_evaluation_no_llm_calls` (in `tests/test_policy.py`) |
| **Evidence** | `PolicyRegistry.evaluate()` is a synchronous function; `PolicyFn` type contract excludes coroutines; no `LLMBackend` reference appears in `camel/policy/` module tree. Verified by static import analysis and runtime call-path inspection. |
| **Status** | ✅ VERIFIED |

---

### NFR-4 — Interpreter Overhead ≤ 100ms Per Tool Call

| Field | Value |
|-------|-------|
| **Requirement** | Interpreter overhead (non-LLM, including policy evaluation) must be ≤ 100ms per tool call. |
| **Test function** | `test_nfr4_policy_evaluation_overhead` (in `tests/test_e2e_enforcement.py`) |
| **Evidence** | Policy evaluation overhead measured over 100 simulated tool calls with the six reference policies active. Median overhead: <5ms per call. Maximum observed: <20ms per call on reference hardware. Confirmed ≤ 100ms threshold met with >5× margin. |
| **Measurement command** | `python scripts/benchmark_interpreter.py` |
| **Status** | ✅ VERIFIED |

---

### NFR-6 — Security Audit Log Coverage

| Field | Value |
|-------|-------|
| **Requirement** | All tool calls, policy evaluations, consent decisions, and capability assignments must be written to the security audit log. |
| **Test function** | `test_nfr6_audit_log_completeness` (in `tests/test_e2e_enforcement.py`) |
| **Evidence** | `CaMeLInterpreter.audit_log` is populated with one `AuditLogEntry` per tool call attempt (including denied calls). Each entry contains `tool_name`, `outcome` (`"Allowed"` / `"Denied"`), `reason` (denial message or `None`), `timestamp`, and `consent_decision` (`"UserApproved"` / `"UserRejected"` / `None`). End-to-end test confirms all six reference-policy scenarios produce audit log entries. |
| **Status** | ✅ VERIFIED |

---

### NFR-9 — Independent Unit Testability

| Field | Value |
|-------|-------|
| **Requirement** | Each component (interpreter, P-LLM wrapper, Q-LLM wrapper, policy engine, capability system) must be independently unit-testable. |
| **Evidence** | Each component has its own dedicated test module that exercises it in isolation: |

| Component | Test module | Status |
|---|---|---|
| Interpreter | `tests/test_interpreter.py` | ✅ PASS |
| P-LLM wrapper | `tests/llm/test_pllm.py` | ✅ PASS |
| Q-LLM wrapper | `tests/llm/test_qllm.py` | ✅ PASS |
| Policy engine & registry | `tests/test_policy.py` | ✅ PASS |
| Reference policy library | `tests/policies/test_reference_policies.py` | ✅ PASS |
| Capability assignment engine | `tests/test_capability_assignment.py` | ✅ PASS |
| Policy testing harness | `tests/test_policy_harness.py` | ✅ PASS |
| Enforcement hook (end-to-end) | `tests/test_e2e_enforcement.py` | ✅ PASS |

| **Status** | ✅ VERIFIED |

---

## Summary

### Milestone 1 Exit Criteria

| Criterion | Test Count | Status |
|-----------|-----------|--------|
| EC-1 — 100% unit test pass rate | 1 | ✅ PASS |
| EC-2 — ≥20 dep-graph programs | 2 | ✅ PASS |
| EC-3 — CaMeLValue round-trip fidelity (6 operations) | 9 | ✅ PASS |
| EC-4 — ≥15 negative syntax tests | 17 | ✅ PASS |
| EC-5 — Session persistence (3 sequential execs) | 2 | ✅ PASS |
| EC-6 — STRICT mode loop-dependency regression | 3 | ✅ PASS |
| EC-7 — Documentation coverage ≥ 90% | interrogate | ✅ PASS |
| **M1 Total** | **34 tests + interrogate** | **✅ ALL PASS** |

### Milestone 3 NFR Compliance

| NFR | Requirement | Status |
|-----|-------------|--------|
| NFR-2 | Synchronous, deterministic policy evaluation — no LLM calls | ✅ VERIFIED |
| NFR-4 | Interpreter overhead ≤ 100ms per tool call (including policy evaluation) | ✅ VERIFIED |
| NFR-6 | All tool calls, policy evaluations, consent decisions in audit log | ✅ VERIFIED |
| NFR-9 | Each component independently unit-testable | ✅ VERIFIED |
| **M3 NFR Total** | **4 requirements** | **✅ ALL VERIFIED** |

---

_Note: `UnsupportedSyntaxError.lineno` is the correct attribute name (not `line_number`).
All negative syntax tests assert `exc.lineno >= 1`._
