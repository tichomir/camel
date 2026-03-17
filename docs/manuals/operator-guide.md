# CaMeL Operator Guide — Milestone 1

**Version:** 0.1.0
**Date:** 2026-03-17
**Status:** Released

This guide is for platform engineers and security engineers deploying the CaMeL
Milestone 1 foundation package.  It covers system requirements, installation,
configuration, test execution, known limitations, and a troubleshooting FAQ.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Installation](#2-installation)
3. [Environment Configuration](#3-environment-configuration)
4. [Running the Test Suite](#4-running-the-test-suite)
5. [STRICT vs NORMAL Mode](#5-strict-vs-normal-mode)
6. [Known Limitations](#6-known-limitations)
7. [Milestone 2 Readiness — Config Surface](#7-milestone-2-readiness--config-surface)
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

> **Note:** The package is currently in pre-release (v0.1.0).  Once published to
> PyPI the above command installs the Milestone 1 foundation.

### 2.2 Development install from source

```bash
# 1. Clone the repository
git clone https://github.com/your-org/camel.git
cd camel

# 2. Create and activate a virtual environment (strongly recommended)
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows (PowerShell)

# 3. Install the package in editable mode with all dev dependencies
pip install -e ".[dev]"

# 4. Verify the installation
python -c "import camel; print(camel.__version__)"
# Expected output: 0.1.0
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

### 3.1 Milestone 1 — no secrets required

The Milestone 1 package (`camel.interpreter`, `camel.value`,
`camel.dependency_graph`) contains **no network calls** and **requires no
environment variables**.  You can import and use these modules in a fully offline
environment.

### 3.2 Q-LLM backend — optional at M1, required at M2

The `camel.llm` sub-package ships with two LLM backend adapters:

| Module | Backend | Status at M1 |
|---|---|---|
| `camel.llm.adapters.claude` | Anthropic Claude API | Present but unused by core |
| `camel.llm.adapters.gemini` | Google Gemini API | Present but unused by core |

These adapters require credentials if instantiated:

| Environment variable | Purpose | Required by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | `ClaudeAdapter` |
| `GOOGLE_API_KEY` | Google Gemini API key | `GeminiAdapter` (if used) |

At Milestone 1, the interpreter, capability system, and dependency graph are
fully functional without these keys.

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

| # | Limitation | Severity | PRD Mitigation | M1 Status |
|---|---|---|---|---|
| L1 | **Data-requires-action failure** — P-LLM cannot plan actions that depend on reading untrusted data (e.g., "do whatever the email says") | Medium | Document as design constraint; future: nested P-LLM tool | **By design.** The interpreter enforces this intentionally. P-LLM never sees tool output values. Workaround: use Q-LLM to extract structured fields first, then act on those fields. |
| L2 | **Underdocumented tool APIs** — P-LLM cannot correctly parse output from tools with undocumented return schemas | Medium | Require typed return schemas; Q-LLM fallback for unstructured parsing | **Not applicable at M1.** Tool schemas are defined by the developer registering tools. Use Pydantic models for structured return types. Q-LLM wrapper available in `camel.llm.qllm`. |
| L3 | **Exception-based side channel (residual)** — adversary-controlled tool data can still trigger exceptions that STRICT mode doesn't cover | Low–Medium | STRICT mode closes primary vector; adversarial exception triggering via tool data is harder | **Partially mitigated.** STRICT mode is implemented. The residual risk from adversarially crafted exception messages remains. The P-LLM retry protocol redacts tool-originating error content (see error-reporting protocol in developer guide §4.2). |
| L4 | **User fatigue from policy denials** — overly strict policies may generate frequent consent prompts | Medium | Granular policies; policy tuning guidance | **Not applicable at M1.** Policy engine is an optional interface (`policy_engine=None` default). Implement and tune policies in M3. |
| L5 | **Token cost** — CaMeL requires ~2.82× more input tokens than native tool-calling | Low–Medium | Expected to decrease as models improve; cheaper Q-LLM reduces cost | **Not measured at M1.** Token overhead will be benchmarked in M3/M4 when P-LLM integration is complete. Q-LLM adapter supports using a cheaper model (e.g., `claude-haiku`). |
| L6 | **ROP-analogous attacks** — attacker chains individually allowed actions for collectively malicious outcome | Medium–High | Defense-in-depth: combine with model-level robustness; monitor action chains | **Out of scope at M1.** Requires full P-LLM + policy engine integration (M3+). |
| L7 | **Ecosystem adoption** — third-party tools without capability annotations degrade policy granularity | Medium | CaMeL agent as central capability authority; adapter templates | **Out of scope at M1.** Tool capability annotation API is defined (see developer guide §4.1). Third-party adapter guide planned for M3. |
| L8 | **Formal verification gap** — no machine-verified proof of interpreter correctness | Low | Future work: formal verification via Coq/Lean | **Accepted risk.** No formal verification at M1. Test coverage (>95% on core modules) is the primary correctness assurance. |

---

## 7. Milestone 2 Readiness — Config Surface

This section documents the environment variables, configuration points, and
interface contracts that will be required when integrating Milestone 2
components (Dual LLM + Interpreter wiring).

### 7.1 Required environment variables at M2

| Variable | Purpose | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | Authenticate the P-LLM and Q-LLM Claude backends | `sk-ant-...` |
| `CAMEL_PLLLM_MODEL` | Override the P-LLM model name | `claude-sonnet-4-6` |
| `CAMEL_QLLM_MODEL` | Override the Q-LLM model name (cheaper model reduces cost) | `claude-haiku-4-5-20251001` |
| `CAMEL_MAX_RETRIES` | Maximum P-LLM code-generation retries per task (default: 10, per PRD §6.1) | `10` |
| `CAMEL_EXECUTION_MODE` | Default execution mode: `NORMAL` or `STRICT` | `STRICT` |

### 7.2 Policy engine interface

At M2+, inject a policy engine at interpreter construction time:

```python
from camel import CaMeLInterpreter
from camel.value import CaMeLValue
from typing import Mapping

class MyPolicyEngine:
    def check(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> "Allowed | Denied":
        ...

interp = CaMeLInterpreter(
    tools={"send_email": send_email},
    policy_engine=MyPolicyEngine(),
    mode=ExecutionMode.STRICT,
)
```

See developer guide §4.1 for the full `PolicyEngine` protocol and
`Allowed` / `Denied` return types.

### 7.3 Audit logging interface (M2+)

Per NFR-6, all tool calls, policy evaluations, and capability assignments must
be written to a security audit log.  At M1, no audit logger is wired.  At M2,
the interpreter will accept an optional `audit_logger` parameter:

```python
# Planned M2 interface (not yet implemented):
interp = CaMeLInterpreter(
    tools=...,
    audit_logger=my_structured_logger,  # must implement AuditLogger protocol
)
```

Implement a conforming logger before upgrading to M2:

```python
class AuditLogger(Protocol):
    def log_tool_call(self, tool_name: str, kwargs: Mapping[str, CaMeLValue], result: CaMeLValue) -> None: ...
    def log_policy_decision(self, tool_name: str, decision: "Allowed | Denied") -> None: ...
    def log_capability_assignment(self, variable: str, value: CaMeLValue) -> None: ...
```

### 7.4 P-LLM wiring (M2)

The P-LLM wrapper will call `CaMeLInterpreter.exec()` with the generated code
plan and read back only variable *names* (not values) to inform the next
generation step.  No additional interpreter API changes are required for M2
wiring.

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
