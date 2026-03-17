# CaMeL — Capabilities for Machine Learning

[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](#)
[![Version](https://img.shields.io/badge/version-0.3.0-blue)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#)
[![Docstring Coverage](https://img.shields.io/badge/docstring%20coverage-100%25-brightgreen)](#documentation-coverage)

CaMeL is a security layer that protects LLM-based agentic systems from prompt
injection attacks.  Rather than modifying the underlying model, CaMeL wraps it
in a principled architecture that enforces security policies through explicit
control flow management, data flow tracking, and capability-based access control.

> Based on: *"Defeating Prompt Injections by Design"* — Debenedetti et al.,
> Google / Google DeepMind / ETH Zurich (arXiv:2503.18813v2)

---

## Security Guarantees

CaMeL provides **provable, system-level** security against prompt injection — not
probabilistic defences:

| Metric | Result |
|---|---|
| AgentDojo task success rate | **77%** (vs. 84% without any defence) |
| Prompt injection attack success rate (ASR) | **0** on AgentDojo benchmark |
| Data exfiltration events blocked | **100%** of policy-covered scenarios |

The security guarantee is formalised via the **PI-SEC game**: CaMeL prevents any
adversary from constructing tool return values that cause the agent to take actions
outside the set permitted by the original user query.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                        User Query                       │
└──────────────────────────┬──────────────────────────────┘
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
          Untrusted  │        │  Policy-guarded tool calls
          data       ▼        ▼
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

See [docs/architecture.md](docs/architecture.md) for the full system architecture
reference including isolation invariants, exception redaction logic, retry
mechanics, and the security model.

---

## Current Status

**Milestone 3 — Capabilities & Policies (v0.3.0)** — released 2026-03-17

| Component | Module | Status |
|---|---|---|
| `CaMeLValue` & capability system | `camel.value` | ✅ Released |
| AST interpreter (restricted Python subset) | `camel.interpreter` | ✅ Released |
| Dependency graph (NORMAL + STRICT modes) | `camel.dependency_graph` | ✅ Released |
| P-LLM wrapper (plan generation, isolation contract) | `camel.llm.p_llm` | ✅ Released |
| Q-LLM wrapper (schema-validated structured output) | `camel.llm.qllm` | ✅ Released |
| LLM backend adapters (Claude, Gemini) | `camel.llm.adapters` | ✅ Released |
| Execution loop orchestrator (retry, redaction, trace) | `camel.execution_loop` | ✅ Released |
| Isolation verification test harness | `tests/harness/` | ✅ Released |
| Capability assignment engine (tool annotations) | `camel.capabilities` | ✅ Released |
| Policy engine & registry | `camel.policy` | ✅ Released |
| Reference policy library (6 policies) | `camel.policy.reference_policies` | ✅ Released |
| Enforcement hook, consent flow, audit log | `camel.interpreter` | ✅ Released |
| Policy testing harness (AgentDojo scenarios) | `tests/harness/policy_harness.py` | ✅ Released |

---

## Quick Start

```bash
pip install camel
```

### Interpreter only (Milestone 1 API)

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

### Full execution loop (Milestone 2 API)

```python
import asyncio
from camel import CaMeLInterpreter, ExecutionMode
from camel.execution_loop import CaMeLOrchestrator
from camel.llm import PLLMWrapper, QLLMWrapper, ToolSignature
from camel.llm.adapters import ClaudeBackend
from camel.value import wrap, Public, CaMeLValue

# 1. Define tools (must return CaMeLValue)
def get_email_count() -> CaMeLValue:
    return wrap(7, sources=frozenset({"get_email_count"}), readers=Public)

tool_signatures = [
    ToolSignature(
        name="get_email_count",
        description="Returns the number of unread emails.",
        parameters={},
        return_type="int",
    ),
]

# 2. Wire up the components
backend = ClaudeBackend(model="claude-sonnet-4-6")
p_llm = PLLMWrapper(backend=backend)
q_llm = QLLMWrapper(backend=backend)

interpreter = CaMeLInterpreter(
    tools={"get_email_count": get_email_count},
    mode=ExecutionMode.STRICT,
)

orchestrator = CaMeLOrchestrator(
    p_llm=p_llm,
    interpreter=interpreter,
    tool_signatures=tool_signatures,
)

# 3. Run a user query end-to-end
result = asyncio.run(orchestrator.run("How many unread emails do I have?"))

# 4. Inspect the execution trace
for record in result.trace:
    print(record.tool_name, record.args)
```

---

## Documentation

| Document | Description |
|---|---|
| [API Reference](docs/api/index.md) | M2/M3 component API reference: LLM Backend, P-LLM, Q-LLM, Interpreter, Execution Loop, Policy Engine, Capabilities |
| [Architecture Reference](docs/architecture.md) | Full system architecture, isolation invariants, exception redaction, security model, policy engine |
| [Developer Guide](docs/developer_guide.md) | Supported grammar, CaMeLValue schema, propagation rules, dependency graph API |
| [Operator Guide](docs/manuals/operator-guide.md) | Installation, environment config, test execution, STRICT/NORMAL mode, policy configuration, known limitations |
| [Reference Policy Specification](docs/policies/reference-policy-spec.md) | Authoritative spec for all six reference security policies |
| [Exit Criteria Checklist](docs/exit_criteria_checklist.md) | Milestone 1 sign-off checklist with test evidence |
| [M2 Exit Criteria Report](docs/milestone-2-exit-criteria-report.md) | Milestone 2 sign-off report |
| [M3 Exit Criteria Checklist](docs/milestone-3-exit-criteria-checklist.md) | Milestone 3 exit criteria mapped to test evidence (16 criteria, ~291 tests) |
| [M3 Exit Criteria Report](docs/milestone-3-exit-criteria-report.md) | Milestone 3 sign-off report — capability assignment, policy engine, reference policies, enforcement hook |
| [ADR 001 — Q-LLM Isolation Contract](docs/adr/001-q-llm-isolation-contract.md) | Q-LLM isolation guarantees and schema conventions |
| [ADR 002 — CaMeLValue Capability System](docs/adr/002-camelvalue-capability-system.md) | Detailed capability data model spec |
| [ADR 003 — AST Interpreter Architecture](docs/adr/003-ast-interpreter-architecture.md) | Full interpreter design spec |
| [ADR 004 — Dependency Graph Architecture](docs/adr/004-dependency-graph-architecture.md) | Full dependency tracking design spec |
| [ADR 005 — P-LLM Wrapper Architecture](docs/adr/005-p-llm-wrapper-architecture.md) | P-LLM isolation contract and prompt builder design |
| [ADR 006 — Q-LLM Dynamic Schema Injection](docs/adr/006-q-llm-dynamic-schema-injection.md) | Q-LLM schema augmentation and NotEnoughInformationError |
| [ADR 007 — Execution Loop Orchestrator](docs/adr/007-execution-loop-orchestrator.md) | Retry loop, exception redaction, trace recorder design |
| [ADR 008 — Isolation Test Harness](docs/adr/008-isolation-test-harness-architecture.md) | Isolation verification test harness architecture |
| [ADR 009 — Policy Engine Architecture](docs/adr/009-policy-engine-architecture.md) | PolicyRegistry, SecurityPolicyResult sealed type, helper functions |
| [ADR 010 — Enforcement Hook, Consent Flow & Audit](docs/adr/010-enforcement-hook-consent-audit-harness.md) | Dual-mode enforcement, consent callback, audit log, policy harness |
| [E2E Scenario Specification](docs/e2e-scenario-specification.md) | End-to-end test scenario inventory |

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
pytest                                        # full suite
pytest tests/test_value.py                    # capability system
pytest tests/test_interpreter.py              # AST interpreter
pytest tests/test_dependency_graph.py         # dependency graph
pytest tests/test_exit_criteria.py            # M1 exit criteria sign-off
pytest tests/llm/test_pllm.py                # P-LLM wrapper
pytest tests/llm/test_qllm.py                # Q-LLM wrapper
pytest tests/test_execution_loop.py           # execution loop orchestrator
pytest tests/test_isolation_harness.py        # isolation invariant verification
pytest tests/test_e2e_scenarios.py            # end-to-end scenarios (no policies)
pytest tests/test_capability_assignment.py    # capability assignment engine
pytest tests/test_policy.py                   # policy engine & registry
pytest tests/policies/test_reference_policies.py  # reference policy library
pytest tests/test_policy_harness.py           # policy testing harness
pytest tests/test_e2e_enforcement.py          # end-to-end enforcement integration
```

---

## Documentation Coverage

Docstring coverage is enforced in CI using [interrogate](https://github.com/econchick/interrogate).
Magic/dunder methods and `__init__` constructors are excluded (their parameters are documented in
the class-level `Parameters` section).

**Current coverage: 100%** (minimum required: 90%)

To measure locally:

```bash
interrogate camel/ --ignore-init-method --ignore-magic --fail-under 90 -v
```

The CI step fails on any PR that drops public-symbol coverage below 90%.

---

## Core Components

### Privileged LLM (P-LLM)

Receives the user query and a system prompt describing available tools as Python
function signatures.  Generates a pseudo-Python execution plan.  **Never observes
tool return values** — this is the primary isolation invariant.

```python
# camel/llm/p_llm.py
from camel.llm import PLLMWrapper
```

### Quarantined LLM (Q-LLM)

Receives unstructured data from tools and extracts structured output conforming
to a caller-specified Pydantic schema.  Has **no tool-calling capability**.  When
it lacks sufficient information it raises `NotEnoughInformationError` — the
error type is forwarded to the P-LLM but the missing-data content is never
revealed.

```python
# camel/llm/qllm.py
from camel.llm import QLLMWrapper
from pydantic import BaseModel

class EmailSummary(BaseModel):
    subject: str
    sender: str

summary = q_llm.extract(raw_email_text, EmailSummary)
print(summary.subject)
```

### CaMeL Interpreter

Custom AST-walking interpreter for a **restricted Python subset**.  Wraps every
runtime value in `CaMeLValue`, tracks data flow dependencies, and enforces
security policies before each tool call.

### Capabilities (`CaMeLValue`)

Every runtime value carries a capability tag:

| Field | Description | Example |
|---|---|---|
| `sources` | Origin of the data | `{"get_last_email"}`, `{"User literal"}` |
| `inner_source` | Sub-source within a tool | `"sender"` field of an email |
| `readers` | Allowed recipients | `{"alice@co.com"}` or `Public` |

### Capability Assignment Engine (`camel.capabilities`)

Every tool's return value is annotated with provenance metadata via a
`capability_annotation` function.  Built-in annotators are provided for
`read_email`, `read_document`, and `get_file`:

```python
from camel.capabilities import annotate_read_email, register_built_in_tools
from camel.tools.registry import ToolRegistry

registry = ToolRegistry()
register_built_in_tools(registry)   # registers email + cloud storage annotators
```

Default annotation for unannotated tools: `sources={tool_name}`, `readers=Public`.

### Security Policies (`camel.policy`)

Expressed as Python functions `(tool_name, kwargs) → Allowed() | Denied(reason)`.
Evaluated synchronously before every tool call — no LLM involvement (NFR-2).

```python
from camel.policy import PolicyRegistry, Allowed, Denied, is_trusted
from camel.policy.reference_policies import configure_reference_policies

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")

# Inject into the interpreter
from camel import CaMeLInterpreter
interp = CaMeLInterpreter(tools=my_tools, policy_engine=registry)
```

Six reference policies are shipped: `send_email`, `send_money`,
`create_calendar_event`, `write_file`, `post_message`, `fetch_external_url`.
See [Reference Policy Specification](docs/policies/reference-policy-spec.md) for
full logic, denial reasons, and AgentDojo attack-scenario mappings.

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

See the [Developer Guide — Grammar Reference](docs/developer_guide.md#1-supported-python-grammar-reference) for the complete node-by-node specification.

---

## NORMAL vs STRICT Mode

CaMeL tracks data flow dependencies in two modes, configurable per interpreter instance.

| Mode | Dependency tracking | Side-channel mitigation |
|---|---|---|
| `NORMAL` | Data assignments only | No control-flow taint |
| `STRICT` | Data + control-flow (if-test, for-iterable) | Closes timing/control-flow channels |

**STRICT mode is recommended for production.**  It closes the timing side-channel
where an adversary could learn the value of a private boolean by observing which
branch the agent takes (PRD §7.3).

See [Architecture Reference — Interpreter Modes](docs/architecture.md#6-interpreter-execution-modes) for details.

---

## Security Model

CaMeL prevents two classes of prompt injection attack:

- **Control flow hijack** — malicious content overrides the agent's plan
- **Data flow manipulation** — plan is intact but arguments to tool calls are poisoned

It achieves this by:

1. Separating trusted (user query, P-LLM literals) from untrusted (tool returns, Q-LLM outputs)
2. Tagging every runtime value with capability metadata (`sources`, `readers`)
3. Enforcing capability-based policies before every tool call
4. Tracking data flow dependencies (STRICT mode also tracks control-flow taint)
5. Redacting exception messages from untrusted-dependency failures before P-LLM retry

See [Architecture Reference — Security Model](docs/architecture.md#10-security-model) for the full
threat model, PI-SEC game definition, and trusted/untrusted boundary table.

---

## License

See [LICENSE](LICENSE).
