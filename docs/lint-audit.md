# Ruff Lint Audit Report

**Generated:** 2026-03-18
**Tool:** Ruff (project config from `pyproject.toml`)
**Total violations:** 33
**Auto-fixable:** 10
**Manual fix required:** 23

---

## Summary by Rule Code

| Rule | Category | Count | Auto-fixable |
|------|----------|-------|--------------|
| E501 | Line too long (> 100 chars) | 23 | No |
| F841 | Local variable assigned but never used | 9 | Yes |
| E731 | Lambda assigned to variable (use `def`) | 1 | Yes |

---

## Per-File Breakdown

| File | Violations |
|------|-----------|
| `tests/policies/test_agentdojo_mapping.py` | 14 |
| `tests/test_strict_mode.py` | 2 |
| `tests/test_exit_criteria.py` | 2 |
| `tests/test_exception_hardening.py` | 2 |
| `tests/test_isolation_harness.py` | 2 |
| `tests/integration/test_multi_backend_adapters.py` | 2 |
| `scripts/benchmark_agentdojo.py` | 2 |
| `tests/test_allowlist_enforcement.py` | 1 |
| `tests/test_capability_assignment.py` | 1 |
| `tests/test_e2e_enforcement.py` | 1 |
| `tests/test_provenance.py` | 1 |
| `tests/policies/test_reference_policies.py` | 1 |
| `tests/test_policy.py` | 1 |
| `camel/execution_loop.py` | 1 |

---

## E501 — Line Too Long (manual fix)

All 23 violations exceed the 100-character line limit configured in `pyproject.toml`.

| File | Line | Length | Content summary |
|------|------|--------|-----------------|
| `scripts/benchmark_agentdojo.py` | 400 | 106 | — |
| `tests/policies/test_agentdojo_mapping.py` | 10 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 12 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 13 | 118 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 14 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 15 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 16 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 17 | 118 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 18 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 19 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 20 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 21 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 22 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 23 | 119 | import / comment line |
| `tests/policies/test_agentdojo_mapping.py` | 24 | 119 | import / comment line |
| `tests/test_allowlist_enforcement.py` | 457 | 103 | — |
| `tests/test_capability_assignment.py` | 379 | 102 | — |
| `tests/test_e2e_enforcement.py` | 840 | 102 | — |
| `tests/test_exit_criteria.py` | 6 | 103 | — |
| `tests/test_exit_criteria.py` | 408 | 103 | — |
| `tests/test_provenance.py` | 8 | 101 | — |
| `tests/test_strict_mode.py` | 41 | 104 | — |
| `tests/test_strict_mode.py` | 398 | 108 | — |

---

## F841 — Unused Local Variable (auto-fixable)

9 violations where a variable is assigned but never read. Can be fixed by prefixing with `_` or removing the assignment.

| File | Line | Variable |
|------|------|----------|
| `camel/execution_loop.py` | 450 | `orig_message` |
| `scripts/benchmark_agentdojo.py` | 856 | `utility_ok` |
| `tests/integration/test_multi_backend_adapters.py` | 607 | `q_backend` |
| `tests/integration/test_multi_backend_adapters.py` | 656 | `backend` |
| `tests/policies/test_reference_policies.py` | 280 | `email_sender` |
| `tests/test_exception_hardening.py` | 350 | `neie_raised` |
| `tests/test_exception_hardening.py` | 470 | `captured_dep_graph_after_retry` |
| `tests/test_isolation_harness.py` | 167 | `i1_passed` |
| `tests/test_isolation_harness.py` | 220 | `result` |

---

## E731 — Lambda Assignment (auto-fixable)

1 violation where a lambda is assigned to a variable instead of using `def`.

| File | Line | Description |
|------|------|-------------|
| `tests/test_policy.py` | 768 | `lambda` assigned to a name — replace with `def` |

---

## Fix Strategy

### Auto-fixable violations (10 total)
Run `ruff check --fix .` to automatically resolve all F841 and E731 violations.

**Note:** F841 fixes prefix unused variables with `_`. Verify each case — some unused
variables in tests may be intentional (e.g., asserting no exception is raised). In those
cases, keep the `_` prefix rather than removing the assignment entirely.

### Manual fix violations (23 total — all E501)
All E501 violations require manual line-wrapping. Common approaches:
- Break long string literals using implicit concatenation or parentheses
- Wrap long import lists with parentheses over multiple lines
- Split long function call arguments across lines

The bulk (13 of 23) are concentrated in `tests/policies/test_agentdojo_mapping.py`
lines 10–24, likely a block of long import statements or string constants that can be
wrapped with parentheses.

---

## Ruff Configuration (from `pyproject.toml`)

```
line-length = 100
```
