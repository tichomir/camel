# CaMeL — Capabilities for Machine Learning

[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](#)
[![Version](https://img.shields.io/badge/version-0.1.0-blue)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#)

CaMeL is a security layer that protects LLM-based agentic systems from prompt
injection attacks.  Rather than modifying the underlying model, CaMeL wraps it
in a principled architecture that enforces security policies through explicit
control flow management, data flow tracking, and capability-based access control.

> Based on: *"Defeating Prompt Injections by Design"* — Debenedetti et al.,
> Google / Google DeepMind / ETH Zurich (arXiv:2503.18813v2)

---

## Current Status

**Milestone 1 — Foundation (v0.1.0)** — released 2026-03-17

The Milestone 1 package delivers the core runtime foundation:

| Component | Module | Status |
|---|---|---|
| `CaMeLValue` & capability system | `camel.value` | ✅ Released |
| AST interpreter (restricted Python subset) | `camel.interpreter` | ✅ Released |
| Dependency graph (NORMAL + STRICT modes) | `camel.dependency_graph` | ✅ Released |
| Q-LLM wrapper (schema-validated structured output) | `camel.llm.qllm` | ✅ Released |

---

## Quick Start

```bash
pip install camel
```

```python
from camel import CaMeLInterpreter, ExecutionMode
from camel.value import CaMeLValue, wrap, Public

# Register tools — each must return a CaMeLValue
def get_inbox_count() -> CaMeLValue:
    return wrap(42, sources=frozenset({"get_inbox_count"}), readers=Public)

# Create an interpreter in STRICT mode (recommended for production)
interp = CaMeLInterpreter(
    tools={"get_inbox_count": get_inbox_count},
    mode=ExecutionMode.STRICT,
)

# Execute a pseudo-Python plan
interp.exec("count = get_inbox_count()")

# Read back the capability-tagged result
result = interp.get("count")
print(result.raw)      # 42
print(result.sources)  # frozenset({'get_inbox_count'})
```

---

## Documentation

| Document | Description |
|---|---|
| [Developer Guide](docs/developer_guide.md) | Supported grammar, CaMeLValue schema, propagation rules, dependency graph API, Milestone 2 readiness |
| [Operator Guide](docs/manuals/operator-guide.md) | Installation, environment config, test execution, STRICT/NORMAL mode, known limitations, troubleshooting |
| [Exit Criteria Checklist](docs/exit_criteria_checklist.md) | Milestone 1 sign-off checklist with test evidence |
| [ADR-0001 — Python AST Interpreter Substrate](docs/adr/ADR-0001-python-ast-interpreter-substrate.md) | Why Python AST over a custom parser or bytecode approach |
| [ADR-0002 — CaMeLValue Capability Wrapper](docs/adr/ADR-0002-camelvalue-capability-wrapper.md) | CaMeLValue design: capability tagging vs. alternatives |
| [ADR-0003 — NORMAL/STRICT Dependency Tracking](docs/adr/ADR-0003-normal-strict-dependency-tracking.md) | Dual-mode dependency tracking and side-channel rationale |
| [ADR-0004 — Excluded Python Constructs](docs/adr/ADR-0004-excluded-python-constructs.md) | Which constructs are excluded and why |
| [ADR 001 — Q-LLM Isolation Contract](docs/adr/001-q-llm-isolation-contract.md) | Q-LLM isolation guarantees and schema conventions |
| [ADR 002 — CaMeLValue Capability System (detailed)](docs/adr/002-camelvalue-capability-system.md) | Detailed capability data model spec |
| [ADR 003 — AST Interpreter Architecture (detailed)](docs/adr/003-ast-interpreter-architecture.md) | Full interpreter design spec |
| [ADR 004 — Dependency Graph Architecture (detailed)](docs/adr/004-dependency-graph-architecture.md) | Full dependency tracking design spec |

---

## Installation

### Production

```bash
pip install camel
```

### Development (from source)

```bash
git clone https://github.com/your-org/camel.git
cd camel
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

**Requirements:** Python ≥ 3.11.  See the [Operator Guide](docs/manuals/operator-guide.md) for full system requirements.

---

## Running Tests

```bash
pytest                          # full suite
pytest tests/test_value.py      # capability system
pytest tests/test_interpreter.py
pytest tests/test_dependency_graph.py
pytest tests/test_exit_criteria.py   # M1 exit criteria sign-off
```

---

## Architecture Overview

```
User Query
    │ Trusted input
    ▼
┌────────────────────────┐
│    Privileged LLM      │  Generates pseudo-Python plan
│    (P-LLM)             │  Never sees tool output values
└────────────┬───────────┘
             │ Code (control + data flow)
             ▼
┌────────────────────────┐
│   CaMeL Interpreter    │  Executes plan step-by-step
│                        │  Maintains data flow graph
│   ┌────────────────┐   │  Propagates capabilities
│   │ Data Flow Graph│   │  Enforces security policies
│   └────────────────┘   │
└──────┬────────┬────────┘
       │        │
       │        │  Policy-guarded tool calls
       ▼        ▼
┌──────────────────┐   ┌──────────────────┐
│  Quarantined LLM │   │  Tool Executor   │
│  (Q-LLM)         │   │  (email, drive,  │
│  No tool access  │   │   calendar, etc.)│
│  Structured I/O  │   └──────────────────┘
└──────────────────┘
```

**Key principle:** The P-LLM sees only the user's query and the code it
generates — never the values returned by tools.  Values live in the
interpreter's memory, tagged with capabilities.

---

## Supported Python Grammar

The interpreter executes a **restricted Python subset** to minimise the attack surface.

**Supported statements:** `x = expr`, `x += expr`, `if / elif / else`, `for … in …`, bare expression statements (side-effecting calls)

**Supported expressions:** constants, variable references (`ast.Name`), binary/unary/boolean/comparison operators, function calls, attribute access (`obj.field`), subscript access (`x[i]`), list/tuple/dict literals, f-strings

**Explicitly unsupported** (raises `UnsupportedSyntaxError` immediately):

| Construct | Reason excluded |
|---|---|
| `while` loops | Unbounded iteration; timing side-channel risk |
| `def`, `class`, `lambda` | Arbitrary code definition outside P-LLM plan |
| `import` / `from … import` | Arbitrary module access |
| `try` / `except` / `raise` | Exception-based side-channel (see PRD §7) |
| `with` / `async with` | Context manager side effects |
| List/dict/set comprehensions | Generator lazy evaluation; hard to taint-track |
| `yield` / `await` | Async/generator execution models |
| `assert` / `del` | Out of scope for the core execution model |

```python
from camel import CaMeLInterpreter, UnsupportedSyntaxError

interp = CaMeLInterpreter()
try:
    interp.exec("while True: pass")
except UnsupportedSyntaxError as e:
    print(e.node_type)  # "While"
    print(e.lineno)     # 1
```

See the [Developer Guide — Grammar Reference](docs/developer_guide.md#1-supported-python-grammar-reference) for the complete node-by-node specification.

---

## NORMAL vs STRICT Mode

CaMeL tracks data flow dependencies in two modes, configurable per interpreter instance.

### NORMAL mode (default)

Dependencies are recorded only via **data assignment**.  Control-flow constructs
do not add dependency edges.

```python
from camel import CaMeLInterpreter, ExecutionMode, get_dependency_graph
from camel.value import wrap

def get_flag():
    return wrap(True, sources=frozenset({"tool_flag"}))

interp = CaMeLInterpreter(tools={"get_flag": get_flag}, mode=ExecutionMode.NORMAL)
interp.exec("""
flag = get_flag()
x = 1
if flag:
    x = 2
""")

dg = get_dependency_graph(interp, "x")
assert "flag" not in dg.all_upstream   # if-test adds no dependency in NORMAL
```

### STRICT mode (recommended for production)

In STRICT mode, variables referenced in `if` tests and `for` iterables become
dependencies of **every variable assigned within that block**.  This closes the
timing side-channel where an adversary could learn the value of a private boolean
by observing which branch the agent takes (PRD §7.3).

```python
interp_strict = CaMeLInterpreter(
    tools={"get_flag": get_flag},
    mode=ExecutionMode.STRICT,
)
interp_strict.exec("""
flag = get_flag()
x = 1
if flag:
    x = 2
""")

dg = get_dependency_graph(interp_strict, "x")
assert "flag" in dg.all_upstream   # STRICT: if-test taint propagates to x
assert "flag" in dg.direct_deps
```

| Mode | Dependency tracking | Side-channel mitigation |
|---|---|---|
| `NORMAL` | Data assignments only | No control-flow taint |
| `STRICT` | Data + control-flow (if-test, for-iterable) | Closes timing/control-flow channels |

---

## Security Model

CaMeL prevents two classes of prompt injection attack:

- **Control flow hijack** — malicious content overrides the agent's plan
- **Data flow manipulation** — plan is intact but arguments to tool calls are poisoned

It achieves this by:

1. Separating trusted (user query, P-LLM literals) from untrusted (tool returns, Q-LLM outputs) data
2. Tagging every runtime value with capability metadata (`sources`, `readers`)
3. Enforcing capability-based policies before every tool call
4. Tracking data flow dependencies (STRICT mode also tracks control-flow taint)

See the [Developer Guide](docs/developer_guide.md) for the full security model.

---

## License

See [LICENSE](LICENSE).
