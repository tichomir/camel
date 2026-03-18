# CaMeL Operator Guide — Milestone 3

**Version:** 0.3.0
**Date:** 2026-03-17
**Status:** Released

This guide is for platform engineers and security engineers deploying the CaMeL
package.  It covers system requirements, installation, configuration (including
policy module setup), test execution, known limitations, and a troubleshooting FAQ.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Installation](#2-installation)
3. [Environment Configuration](#3-environment-configuration)
4. [Running the Test Suite](#4-running-the-test-suite)
5. [STRICT vs NORMAL Mode](#5-strict-vs-normal-mode)
6. [Known Limitations](#6-known-limitations)
7. [Milestone 4 Readiness — Config Surface](#7-milestone-4-readiness--config-surface)
8. [Troubleshooting FAQ](#8-troubleshooting-faq)

---

## 1. System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| Python | 3.11 | 3.12 |
| Operating system | Linux, macOS, Windows (WSL2) | Linux / macOS |
| Memory | 256 MB | 1 GB |
| Disk space | 50 MB | 200 MB (includes dev dependencies) |

### Python version

The package pins `requires-python = ">=3.11"` in `pyproject.toml`.  Python 3.10
and earlier are **not supported** because the codebase relies on:

- `match` / `case` pattern-matching syntax (3.10+, but pinned to 3.11 for
  stability)
- `tomllib` in the standard library (3.11+)
- `Self` type alias from `typing` (3.11+)

Verify your Python version before installing:

```bash
python --version   # must be 3.11 or later
```

---

## 2. Installation

### 2.1 Install from PyPI (production)

```bash
pip install camel
```

> **Note:** The package is currently at v0.3.0 (Milestone 3).  Once published
> to PyPI the above command installs the full Milestone 3 package.

### 2.2 Development install from source

```bash
# 1. Clone the repository
git clone https://github.com/tichomir/camel.git
cd camel

# 2. Create and activate a virtual environment (strongly recommended)
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows (PowerShell)

# 3. Install the package in editable mode with all dev dependencies
pip install -e ".[dev]"

# 4. Verify the installation
python -c "import camel; print(camel.__version__)"
# Expected output: 0.3.0
```

### 2.3 Pre-commit hooks (dev only)

After the dev install, set up pre-commit hooks to enforce formatting and type
checking on every commit:

```bash
pre-commit install
```

The hooks run:

| Hook | Purpose |
|---|---|
| `black` | Auto-format Python code |
| `ruff` | Lint and import-sort |
| `mypy` | Static type checking (strict mode) |

Run all hooks manually against the full tree:

```bash
pre-commit run --all-files
```

### 2.4 Dependency summary

| Package | Constraint | Purpose |
|---|---|---|
| `pydantic` | `>=2.0` | Schema validation for Q-LLM structured output |
| `typing-extensions` | `>=4.0` | Backport type utilities |
| `anthropic` | `>=0.25` | Claude LLM backend adapter |
| `pytest` | `>=8.0` (dev) | Test runner |
| `mypy` | `>=1.9` (dev) | Static type checker |
| `ruff` | `>=0.4` (dev) | Linter |
| `black` | `>=24.0` (dev) | Formatter |
| `pre-commit` | `>=3.0` (dev) | Git hooks |

---

## 3. Environment Configuration

### 3.1 Core modules — no secrets required

The core modules (`camel.interpreter`, `camel.value`, `camel.dependency_graph`,
`camel.capabilities`, `camel.policy`) contain **no network calls** and **require
no environment variables**.  You can import and use these modules in a fully
offline environment.

### 3.2 LLM backend credentials

The `camel.llm` sub-package ships with two LLM backend adapters.  These
adapters require credentials if instantiated:

| Environment variable | Purpose | Required by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | `ClaudeBackend` |
| `GOOGLE_API_KEY` | Google Gemini API key | `GeminiBackend` (if used) |

The interpreter, capability system, dependency graph, and policy engine are
fully functional without these keys (they require no LLM calls).

### 3.3 Policy module configuration

Deploy-specific security policies are loaded via the `CAMEL_POLICY_MODULE`
environment variable:

```bash
export CAMEL_POLICY_MODULE=myapp.security.policies
```

The referenced module must export a `configure_policies(registry)` function:

```python
# myapp/security/policies.py
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies

def configure_policies(registry: PolicyRegistry) -> None:
    configure_reference_policies(registry, file_owner="alice@company.com")
```

Load at application startup:

```python
from camel.policy import PolicyRegistry

registry = PolicyRegistry.load_from_env()
```

To use the six **reference policies** directly without a custom module:

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@company.com")
```

See [Reference Policy Specification](../policies/reference-policy-spec.md) for
full policy logic, denial reasons, and AgentDojo attack-scenario mappings.

### 3.3 Logging

CaMeL does not configure logging itself.  Add a handler in your application:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

All CaMeL log records use the `camel` logger namespace.

---

## 4. Running the Test Suite

### 4.1 Full suite

```bash
pytest
```

`pytest` discovers all tests under the `tests/` directory, as configured in
`pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

### 4.2 Individual test modules

```bash
pytest tests/test_value.py              # CaMeLValue & propagation rules
pytest tests/test_interpreter.py        # AST interpreter core
pytest tests/test_dependency_graph.py   # Dependency graph (NORMAL + STRICT)
pytest tests/test_exit_criteria.py      # Milestone 1 exit-criteria checklist
pytest tests/test_qllm.py               # Q-LLM wrapper
pytest tests/test_qllm_validation.py    # Q-LLM schema validation
pytest tests/llm/test_qllm.py           # LLM integration tests
```

### 4.3 Verbose output

```bash
pytest -v                    # one line per test
pytest -v --tb=short         # compact tracebacks on failure
```

### 4.4 Interpreting results

A clean run looks like:

```
========================= N passed in X.XXs =========================
```

Failure output includes:

- **FAILED** — test assertion error (logic bug)
- **ERROR** — unexpected exception during test setup/teardown
- **xfail** — expected failure (marked with `@pytest.mark.xfail`); not a problem
- **SKIPPED** — test skipped due to missing optional dependency or marker

### 4.5 Exit criteria checklist

`tests/test_exit_criteria.py` validates all Milestone 1 exit criteria:

- 20+ dependency graph programs verified (NORMAL and STRICT)
- CaMeLValue round-trip fidelity across all operations
- ≥15 negative syntax test cases
- Session persistence across ≥3 sequential `exec()` calls
- STRICT mode loop-dependency confirmed

Run it in isolation to verify the full sign-off checklist:

```bash
pytest tests/test_exit_criteria.py -v
```

### 4.6 Performance baseline

The interpreter overhead must be ≤100 ms per simulated tool-call step (NFR-4).
Measure it with the benchmark script:

```bash
python scripts/benchmark_interpreter.py
```

Expected output includes median latency per step; alert if it exceeds 100 ms on
your hardware.

---

## 5. STRICT vs NORMAL Mode

### 5.1 What the modes mean

| Mode | Dependency tracking | Side-channel mitigation |
|---|---|---|
| `NORMAL` | Data-assignment edges only | Partial — control-flow taint not tracked |
| `STRICT` | Data-assignment + control-flow taint | Closes `if`-test and `for`-iterable side-channel vectors |

In `STRICT` mode, the condition variable of every `if` statement and the
iterable of every `for` loop are added as dependency edges on all variables
assigned within those blocks.  This prevents an adversary from encoding
information in branching behaviour.

### 5.2 Configuring the mode

#### At construction time (recommended)

```python
from camel import CaMeLInterpreter, ExecutionMode

# NORMAL mode (default)
interp_normal = CaMeLInterpreter(mode=ExecutionMode.NORMAL)

# STRICT mode
interp_strict = CaMeLInterpreter(mode=ExecutionMode.STRICT)
```

#### At runtime (mid-session change)

```python
interp.set_mode(ExecutionMode.STRICT)
```

> **Warning:** Changing the mode mid-session affects subsequent `exec()` calls
> only.  Variables assigned before the mode change retain their recorded
> dependency edges from the previous mode.

#### Default

`ExecutionMode.NORMAL` is the default if no `mode` argument is supplied.

### 5.3 When to use each mode

| Scenario | Recommended mode |
|---|---|
| Development and unit testing | `NORMAL` |
| Production deployments handling sensitive data | `STRICT` |
| AgentDojo benchmark evaluation | `STRICT` (matches PRD security guarantee) |
| Performance-critical pipelines where timing channels are acceptable | `NORMAL` |

### 5.4 Verifying the active mode

```python
print(interp.mode)   # ExecutionMode.NORMAL or ExecutionMode.STRICT
```

---

## 6. Known Limitations

The table below maps each PRD limitation (§10) to its current status in the
Milestone 1 implementation.

| # | Limitation | Severity | PRD Mitigation | M3 Status |
|---|---|---|---|---|
| L1 | **Data-requires-action failure** — P-LLM cannot plan actions that depend on reading untrusted data (e.g., "do whatever the email says") | Medium | Document as design constraint; future: nested P-LLM tool | **By design.** The interpreter enforces this intentionally. P-LLM never sees tool output values. Workaround: use Q-LLM to extract structured fields first, then act on those fields. |
| L2 | **Underdocumented tool APIs** — P-LLM cannot correctly parse output from tools with undocumented return schemas | Medium | Require typed return schemas; Q-LLM fallback for unstructured parsing | **Mitigated.** Use Pydantic models for structured return types; Q-LLM wrapper available in `camel.llm.qllm`. Capability annotators in `camel.capabilities` improve tool output tagging. |
| L3 | **Exception-based side channel (residual)** — adversary-controlled tool data can still trigger exceptions that STRICT mode doesn't cover | Low–Medium | STRICT mode closes primary vector; adversarial exception triggering via tool data is harder | **Partially mitigated.** STRICT mode and exception redaction engine implemented. Residual risk documented in ADR-007. |
| L4 | **User fatigue from policy denials** — overly strict policies may generate frequent consent prompts | Medium | Granular policies; policy tuning guidance | **Addressed.** Six granular reference policies delivered in M3 with documented allow/deny logic. `EnforcementMode.PRODUCTION` with `ConsentCallback` enables user consent prompts. Tune by adjusting registered policies or using `EnforcementMode.EVALUATION` in non-production. |
| L5 | **Token cost** — CaMeL requires ~2.82× more input tokens than native tool-calling | Low–Medium | Expected to decrease as models improve; cheaper Q-LLM reduces cost | **Not yet measured end-to-end.** Token overhead benchmarking deferred to M4/M5. Q-LLM adapter supports a cheaper model via `CAMEL_QLLM_MODEL`. |
| L6 | **ROP-analogous attacks** — attacker chains individually allowed actions for collectively malicious outcome | Medium–High | Defense-in-depth: combine with model-level robustness; monitor action chains | **Partially addressed.** Policy engine provides fine-grained per-call enforcement. Action-chain anomaly detection is future work (FW-6). |
| L7 | **Ecosystem adoption** — third-party tools without capability annotations degrade policy granularity | Medium | CaMeL agent as central capability authority; adapter templates | **Foundation delivered.** `camel.capabilities` provides the annotation protocol, built-in annotators for `read_email` and cloud storage tools, and `default_capability_annotation` as a fallback. Third-party adapter templates planned for M5. |
| L8 | **Formal verification gap** — no machine-verified proof of interpreter correctness | Low | Future work: formal verification via Coq/Lean | **Accepted risk.** No formal verification at M3. Test coverage (>95% on core modules) is the primary correctness assurance. |

---

## 7. Milestone 4 Readiness — Config Surface

This section documents the environment variables and configuration points for
the Milestone 3 stack, and flags what Milestone 4 will add.

### 7.1 Required environment variables (current)

| Variable | Purpose | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | Authenticate the P-LLM and Q-LLM Claude backends | `sk-ant-...` |
| `CAMEL_CLAUDE_MODEL` | Override the P-LLM Claude model name | `claude-sonnet-4-6` |
| `CAMEL_QLLM_MODEL` | Override the Q-LLM model name (cheaper model reduces cost) | `claude-haiku-4-5-20251001` |
| `CAMEL_MAX_RETRIES` | Maximum P-LLM code-generation retries per task (default: 10) | `10` |
| `CAMEL_EXECUTION_MODE` | Default execution mode: `NORMAL` or `STRICT` | `STRICT` |
| `CAMEL_POLICY_MODULE` | Python module path exporting `configure_policies(registry)` | `myapp.security.policies` |

### 7.2 Policy engine — M3 delivered

The `PolicyRegistry` is now the standard policy engine interface.  Inject at
interpreter construction time:

```python
from camel import CaMeLInterpreter, ExecutionMode
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
from camel.interpreter import EnforcementMode

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")

interp = CaMeLInterpreter(
    tools={"send_email": send_email},
    policy_engine=registry,
    mode=ExecutionMode.STRICT,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_callback=my_consent_fn,
)
```

### 7.3 Audit log — M3 delivered

Per NFR-6, all tool calls, policy evaluations, and consent decisions are written
to `CaMeLInterpreter.audit_log` as a list of `AuditLogEntry` instances.
Retrieve and persist at application level:

```python
import json
from datetime import datetime, timezone

for entry in interp.audit_log:
    record = {
        "timestamp": entry.timestamp,
        "tool_name": entry.tool_name,
        "outcome": entry.outcome,
        "reason": entry.reason,
        "consent_decision": entry.consent_decision,
    }
    # Write to your SIEM / structured log sink:
    write_to_audit_sink(json.dumps(record))
```

### 7.4 Milestone 4 — planned additions

Milestone 4 (Hardening & Side-Channel Mitigations) will address:

- Token cost benchmarking (NFR-3) with optimisation recommendations
- Additional side-channel mitigation test coverage
- Formal timing-channel analysis for STRICT mode
- Extended AgentDojo evaluation suite with M3 policies active

---

## 8. Troubleshooting FAQ

### Q1: `UnsupportedSyntaxError` when running a code plan

**Symptom:**
```
camel.interpreter.UnsupportedSyntaxError: While loops are not supported (node_type='While', lineno=3)
```

**Cause:** The code plan contains a Python construct outside the supported
grammar subset (e.g., `while`, `lambda`, `try/except`, list comprehension).

**Fix:**
1. Review the supported grammar in `docs/developer_guide.md` §1.1–1.2.
2. If using the P-LLM, include the grammar constraints in the system prompt.
3. Replace the unsupported construct with a supported equivalent:
   - `while` → `for` loop with an explicit range or break condition encoded in tool logic
   - List comprehension → `for` loop with `append`-equivalent tool call

---

### Q2: `TypeError: tool 'X' returned non-CaMeLValue`

**Symptom:**
```
TypeError: tool 'get_data' returned <class 'str'>; all tools must return CaMeLValue
```

**Cause:** A registered tool function returns a plain Python value instead of a
`CaMeLValue`.

**Fix:** Wrap the return value using `camel.value.wrap`:

```python
from camel.value import wrap, Public

def get_data() -> CaMeLValue:
    raw = call_external_api()
    return wrap(raw, sources=frozenset({"get_data"}), readers=Public)
```

---

### Q3: `NameError` for a variable or tool that should exist

**Symptom:**
```
NameError: name 'email_body' is not defined
```

**Cause (variable):** The variable was assigned in a previous `exec()` call on a
*different* interpreter instance, or the interpreter was reset.

**Cause (tool):** The tool name used in the code plan does not match the key in
the `tools` dict passed to `CaMeLInterpreter`.

**Fix:**
- Ensure you are calling `exec()` on the **same** `CaMeLInterpreter` instance
  across sequential steps — session state is instance-scoped.
- Check that tool names in generated code exactly match the `tools={}` keys
  (case-sensitive).
- Seed pre-existing values with `interp.seed("var_name", camel_value)`.

---

### Q4: Dependency graph returns empty even though variable is assigned

**Symptom:**

```python
dg = interp.get_dependency_graph("result")
assert dg.direct_deps == frozenset()   # unexpected
```

**Cause:** In `NORMAL` mode, if `result` is assigned from a constant or a tool
call with no variable arguments (`result = my_tool()`), the dependency graph
correctly records no variable dependencies.  The graph tracks *variable*
influences, not data provenance (use `CaMeLValue.sources` for that).

**Fix:**
- Switch to `STRICT` mode to also capture control-flow dependencies.
- If you expected a dependency on a tool argument variable, confirm it is passed
  as a Python variable reference, not an inline literal:
  ```python
  # No dep recorded (inline literal):
  result = my_tool(42)
  # Dep on 'email_id' recorded:
  email_id = get_id()
  result = my_tool(email_id)
  ```

---

### Q5: `PolicyViolationError` unexpectedly raised in tests

**Symptom:**
```
camel.interpreter.PolicyViolationError: tool 'send_email' denied: ...
```

**Cause:** A `policy_engine` was injected (or inadvertently left from a previous
test) and the `send_email` (or other) tool call failed the policy check.

**Fix:**
- For unit tests that should not enforce policies, construct the interpreter
  without a policy engine (the default):
  ```python
  interp = CaMeLInterpreter(tools=my_tools)   # policy_engine=None by default
  ```
- If you intended to test policy enforcement, check that the `CaMeLValue`
  arguments passed to the tool have the correct `sources` / `readers` metadata
  for your policy rules.

---

### Q6: `ImportError` or `ModuleNotFoundError: No module named 'camel'`

**Symptom:**
```
ModuleNotFoundError: No module named 'camel'
```

**Cause:** The package is not installed in the active Python environment, or the
wrong interpreter is on `PATH`.

**Fix:**
```bash
# Check which Python you are using:
which python && python --version

# Ensure camel is installed in this environment:
pip show camel

# If not installed, install in editable mode from the repo root:
pip install -e ".[dev]"
```

---

### Q7: Pre-commit hook fails with `mypy` errors after adding a new tool

**Symptom:**
```
error: Argument 1 to "CaMeLInterpreter" has incompatible type ...
```

**Cause:** The new tool function does not have the correct type annotations to
pass `mypy --strict`.

**Fix:** Annotate the tool signature explicitly:

```python
from camel.value import CaMeLValue, wrap, Public

def my_tool(arg: CaMeLValue) -> CaMeLValue:
    return wrap(arg.raw, sources=frozenset({"my_tool"}), readers=Public)
```

Run `mypy camel/ tests/` locally to iterate before committing.

---

### Q8: Benchmark script reports >100 ms per step

**Symptom:** `scripts/benchmark_interpreter.py` reports median step latency
above the 100 ms NFR-4 threshold.

**Cause:** Large code plans, deep dependency graphs, or slow host hardware.

**Fix:**
- Profile with `python -m cProfile scripts/benchmark_interpreter.py` to
  identify the bottleneck.
- Reduce plan complexity: split multi-step plans across multiple `exec()` calls.
- Ensure no network I/O occurs during interpreter execution (tool functions
  should mock external calls during benchmarking).
- This threshold is for the interpreter overhead alone, excluding LLM calls.

---

*For architecture details see `docs/developer_guide.md`.
For ADRs see `docs/adr/`.
For the full exit-criteria checklist see `docs/exit_criteria_checklist.md`.*
