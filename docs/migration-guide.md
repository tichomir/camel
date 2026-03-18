# CaMeL Migration Guide

_Moving from the Milestone 4 prototype to the `camel-security` v0.6.0 SDK_

---

## Overview

This guide covers every breaking change introduced between the **Milestone 4 prototype**
(`camel` package, v0.4.x) and the **Milestone 5 SDK** (`camel-security`, v0.5.0–v0.6.0).
Follow the sections in order; each section shows a **Before** snippet (M4 prototype)
and an **After** snippet (M5 SDK).

---

## Table of Contents

1. [Package Name & Install](#1-package-name--install)
2. [Primary Entry Point: CaMeLAgent](#2-primary-entry-point-cameleagent)
3. [Tool Registration API](#3-tool-registration-api)
4. [LLM Backend Construction](#4-llm-backend-construction)
5. [Policy Engine: Three-Tier Governance](#5-policy-engine-three-tier-governance)
6. [Execution Result Shape](#6-execution-result-shape)
7. [Audit Log Sink Configuration](#7-audit-log-sink-configuration)
8. [Observability Metrics](#8-observability-metrics)
9. [Provenance API](#9-provenance-api)
10. [Import Path Changes](#10-import-path-changes)
11. [Removed and Deprecated Symbols](#11-removed-and-deprecated-symbols)
12. [Configuration Environment Variables](#12-configuration-environment-variables)
13. [Full Before/After Example](#13-full-beforeafter-example)

---

## 1. Package Name & Install

**Milestone 4 prototype** used the bare `camel` package name.  The SDK ships as
`camel-security` with optional extras for each LLM provider.

**Before (M4):**

```bash
pip install camel
```

**After (M5 SDK):**

```bash
# Core install — Claude backend included
pip install camel-security

# With Gemini support
pip install "camel-security[gemini]"

# With OpenAI support
pip install "camel-security[openai]"

# All backends + observability (Prometheus/OpenTelemetry)
pip install "camel-security[all-backends,observability]"
```

> **Note:** The `camel` package name (internal modules `camel.*`) is unchanged.
> Only the top-level PyPI package name and the new `camel_security` namespace are new.

---

## 2. Primary Entry Point: CaMeLAgent

M5 introduces `CaMeLAgent` — a stable, thread-safe, high-level entry point that
replaces manual wiring of `PLLMWrapper`, `QLLMWrapper`, `CaMeLInterpreter`, and
`CaMeLOrchestrator`.

**Before (M4 — manual wiring):**

```python
import asyncio
from camel import CaMeLInterpreter, ExecutionMode
from camel.execution_loop import CaMeLOrchestrator
from camel.llm import PLLMWrapper, QLLMWrapper, ToolSignature
from camel.llm.adapters import ClaudeBackend
from camel.value import wrap, Public, CaMeLValue


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

result = asyncio.run(orchestrator.run("How many unread emails do I have?"))

for record in result.trace:
    print(record.tool_name, record.args)
```

**After (M5 SDK — CaMeLAgent):**

```python
import asyncio
from camel.llm.adapters import ClaudeBackend
from camel.value import wrap, Public, CaMeLValue
from camel_security import CaMeLAgent, Tool


def get_email_count() -> CaMeLValue:
    return wrap(7, sources=frozenset({"get_email_count"}), readers=Public)


email_count_tool = Tool(
    name="get_email_count",
    fn=get_email_count,
    description="Returns the number of unread emails.",
    params="()",
    return_type="int",
)

backend = ClaudeBackend(model="claude-sonnet-4-6", api_key="YOUR_KEY")

agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[email_count_tool],
)

result = asyncio.run(agent.run("How many unread emails do I have?"))

for record in result.execution_trace:
    print(record.tool_name, record.args)
```

**Key changes:**
- `PLLMWrapper` / `QLLMWrapper` construction is now internal — pass backends directly.
- `ToolSignature` is replaced by `Tool` from `camel_security`.
- `CaMeLOrchestrator` is replaced by `CaMeLAgent`.
- `result.trace` is now `result.execution_trace`.

> **Low-level API still supported:** `CaMeLInterpreter`, `CaMeLOrchestrator`,
> `PLLMWrapper`, and `QLLMWrapper` remain in the `camel.*` namespace for advanced
> use cases.  The `CaMeLAgent` wraps them for convenience.

---

## 3. Tool Registration API

**Before (M4 — `ToolSignature` dict-style tools):**

```python
from camel.llm import ToolSignature
from camel.value import wrap, Public, CaMeLValue


def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap(True, sources=frozenset({"send_email"}), readers=Public)


tool_signatures = [
    ToolSignature(
        name="send_email",
        description="Sends an email.",
        parameters={"to": "str", "subject": "str", "body": "str"},
        return_type="bool",
    ),
]

interpreter = CaMeLInterpreter(
    tools={"send_email": send_email},
    mode=ExecutionMode.STRICT,
)
orchestrator = CaMeLOrchestrator(
    p_llm=p_llm,
    interpreter=interpreter,
    tool_signatures=tool_signatures,
)
```

**After (M5 SDK — `Tool` dataclass):**

```python
from camel_security import Tool
from camel.value import wrap, Public, CaMeLValue


def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap(True, sources=frozenset({"send_email"}), readers=Public)


send_email_tool = Tool(
    name="send_email",
    fn=send_email,
    description="Sends an email.",
    params="to: str, subject: str, body: str",
    return_type="bool",
)
```

**Key change:** `ToolSignature.parameters` (a `dict`) is replaced by `Tool.params`
(a string representation matching the Python function signature).

---

## 4. LLM Backend Construction

Backend classes are unchanged (`ClaudeBackend`, `GeminiBackend`).  The new change
is that `api_key` is now a required keyword argument rather than being read from
the environment by default.  Environment variable fallback still works, but the
recommended pattern passes the key explicitly.

**Before (M4):**

```python
from camel.llm.adapters import ClaudeBackend

backend = ClaudeBackend(model="claude-sonnet-4-6")  # key from ANTHROPIC_API_KEY
```

**After (M5 SDK — recommended):**

```python
import os
from camel.llm.adapters import ClaudeBackend

backend = ClaudeBackend(
    model="claude-sonnet-4-6",
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

**New in M5:** `OpenAIBackend` for GPT-4.1, o3, and o4-mini.

```python
from camel.llm.adapters import OpenAIBackend

backend = OpenAIBackend(model="gpt-4.1", api_key=os.environ["OPENAI_API_KEY"])
```

**New factory function:**

```python
from camel.llm.backend import get_backend

backend = get_backend("claude", model="claude-sonnet-4-6", api_key="...")
```

---

## 5. Policy Engine: Three-Tier Governance

M5 introduces the `TieredPolicyRegistry` and `PolicyConflictResolver` for
multi-party policy governance.  The flat `PolicyRegistry` remains fully supported
with no breaking changes.

**Before (M4 — flat registry):**

```python
from camel.policy import PolicyRegistry
from camel.policy.interfaces import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping


def send_email_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    ...


registry = PolicyRegistry()
registry.register("send_email", send_email_policy)

interpreter = CaMeLInterpreter(
    tools={"send_email": send_email},
    policy_engine=registry,
    mode=ExecutionMode.STRICT,
)
```

**After (M5 SDK — three-tier registry, optional migration):**

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver, PolicyTier
from camel.policy.interfaces import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping


registry = TieredPolicyRegistry()


def send_email_platform_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    # Platform-level: absolute rules
    ...


def send_email_tool_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    # Tool-provider level: tool-specific constraints
    ...


def send_email_user_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    # User level: personal preferences
    ...


registry.register_platform("send_email", send_email_platform_policy, non_overridable=True)
registry.register_tool_provider("send_email", send_email_tool_policy)
registry.register_user("send_email", send_email_user_policy)

resolver = PolicyConflictResolver(registry)

agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[send_email_tool],
    policies=resolver,  # accepts both PolicyRegistry and PolicyConflictResolver
)
```

> **Compatibility note:** `PolicyRegistry` (flat) still accepted by `CaMeLAgent.policies`.
> Adopt `TieredPolicyRegistry` incrementally by wrapping existing flat policies at the
> appropriate tier.  See the migration path in the
> [Policy Authoring Guide](policy-authoring-guide.md#migration-path-from-flat-policyregistry).

---

## 6. Execution Result Shape

**Before (M4 — `OrchestratorResult`):**

```python
result = await orchestrator.run("query")
result.trace          # list[TraceRecord]
result.display_lines  # list[str]
result.success        # bool
result.attempts       # int
```

**After (M5 SDK — `AgentResult`):**

```python
result = await agent.run("query")
result.execution_trace   # list[TraceRecord] — renamed from result.trace
result.display_output    # list[str]         — renamed from result.display_lines
result.success           # bool              — unchanged
result.loop_attempts     # int               — renamed from result.attempts
result.policy_denials    # list[PolicyDenialRecord] — NEW in M5
result.audit_log_ref     # str               — NEW in M5: opaque log correlation token
result.provenance_chains # dict[str, ProvenanceChain] — NEW in M5
```

**Field renames summary:**

| M4 (`OrchestratorResult`) | M5 (`AgentResult`) |
|---|---|
| `result.trace` | `result.execution_trace` |
| `result.display_lines` | `result.display_output` |
| `result.attempts` | `result.loop_attempts` |

---

## 7. Audit Log Sink Configuration

**Before (M4 — no configurable sink):**

Audit events were emitted to an internal in-memory list only.

**After (M5 SDK — configurable JSON sink):**

Configure via environment variables:

```bash
# Write audit log to a file (newline-delimited JSON)
export CAMEL_AUDIT_SINK=file
export CAMEL_AUDIT_SINK_PATH=/var/log/camel-audit.jsonl

# Write to stdout
export CAMEL_AUDIT_SINK=stdout

# Suppress (no-op sink)
# Omit both variables
```

Or configure programmatically:

```python
from camel.observability.audit_sink import AuditSink, SinkMode, AuditSinkConfig

config = AuditSinkConfig(mode=SinkMode.FILE, file_path="/var/log/camel-audit.jsonl")
sink = AuditSink(config=config)
```

---

## 8. Observability Metrics

**Before (M4):**

No Prometheus/OpenTelemetry metrics were available.

**After (M5 SDK):**

```python
from camel.observability.metrics import CamelMetricsCollector, start_metrics_server

# Start a Prometheus-compatible metrics HTTP server
start_metrics_server(port=9090)

# Or access the global collector directly
from camel.observability.metrics import get_global_collector

collector = get_global_collector()
print(collector.get_metrics_text())
```

Available metrics: `camel_policy_denial_rate`, `camel_qlm_error_rate`,
`camel_pllm_retry_count_histogram`, `camel_task_success_rate`,
`camel_consent_prompt_rate`.

All metrics are labelled with `session_id`, `tool_name`, and `policy_name`.

---

## 9. Provenance API

**Before (M4):**

No public provenance API.  Provenance was tracked internally but not queryable.

**After (M5 SDK):**

```python
# After agent.run() completes:
for var_name, chain in result.provenance.items():
    print(f"{var_name}: {chain.to_dict()}")

# Or via the low-level interpreter:
from camel_security import CaMeLAgent

# agent.get_provenance(variable_name) after a run is complete
```

`ProvenanceChain` fields:

| Field | Type | Description |
|---|---|---|
| `hops` | `list[ProvenanceHop]` | Ordered list from root source to final value |
| `to_dict()` | `dict` | JSON-serialisable representation |

Each `ProvenanceHop` has `tool_name`, `inner_source`, and `readers`.

---

## 10. Import Path Changes

| M4 import | M5 import | Notes |
|---|---|---|
| `from camel import CaMeLInterpreter` | unchanged | `camel.*` namespace unchanged |
| `from camel.llm import PLLMWrapper` | unchanged | Still available |
| `from camel.llm import QLLMWrapper` | unchanged | Still available |
| `from camel.llm import ToolSignature` | `from camel_security import Tool` | New `Tool` dataclass replaces `ToolSignature` |
| `from camel.execution_loop import CaMeLOrchestrator` | unchanged (low-level) or `from camel_security import CaMeLAgent` | Use `CaMeLAgent` for new code |
| N/A | `from camel_security import CaMeLAgent, AgentResult, Tool` | New in M5 |
| N/A | `from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver` | New in M5 |
| N/A | `from camel_security.testing import CaMeLValueBuilder, PolicyTestRunner, PolicySimulator` | New in M5 |
| N/A | `from camel.observability.metrics import CamelMetricsCollector` | New in M5 |
| N/A | `from camel.observability.audit_sink import AuditSink` | New in M5 |
| N/A | `from camel.provenance import ProvenanceChain, ProvenanceHop` | New in M5 |

---

## 11. Removed and Deprecated Symbols

| Symbol | Status | Replacement |
|---|---|---|
| `CaMeLOrchestrator` | Soft-deprecated (still works) | `CaMeLAgent` from `camel_security` |
| `PLLMWrapper` (direct construction) | Soft-deprecated | Pass backends directly to `CaMeLAgent` |
| `QLLMWrapper` (direct construction) | Soft-deprecated | Pass backends directly to `CaMeLAgent` |
| `ToolSignature` | Soft-deprecated | `Tool` from `camel_security` |
| `OrchestratorResult` | Renamed | `AgentResult` from `camel_security` |
| `result.trace` | Renamed | `result.execution_trace` |
| `result.display_lines` | Renamed | `result.display_output` |
| `result.attempts` | Renamed | `result.loop_attempts` |

> **Soft-deprecated** means the symbol still works in v0.6.0 but will emit a
> `DeprecationWarning` and will be removed in a future major release.

---

## 12. Configuration Environment Variables

New environment variables introduced in M5:

| Variable | Default | Description |
|---|---|---|
| `CAMEL_AUDIT_SINK` | _(none — no-op)_ | Audit sink mode: `file`, `stdout` |
| `CAMEL_AUDIT_SINK_PATH` | _(none)_ | File path when `CAMEL_AUDIT_SINK=file` |
| `CAMEL_OTEL_ENDPOINT` | _(none)_ | OTLP/HTTP endpoint for OpenTelemetry metric push |
| `CAMEL_TIERED_POLICY_MODULE` | _(none)_ | Python module path exporting `configure_tiered_policies` |

---

## 13. Full Before/After Example

The following side-by-side shows a complete M4 task runner migrated to M5.

### Milestone 4 (prototype)

```python
import asyncio
import os
from camel import CaMeLInterpreter, ExecutionMode
from camel.execution_loop import CaMeLOrchestrator
from camel.llm import PLLMWrapper, QLLMWrapper, ToolSignature
from camel.llm.adapters import ClaudeBackend
from camel.policy import PolicyRegistry
from camel.policy.interfaces import Allowed, Denied
from camel.value import wrap, Public, CaMeLValue
from collections.abc import Mapping


# Tool function
def read_latest_email() -> CaMeLValue:
    return wrap(
        {"from": "alice@co.com", "subject": "Q1 Report", "body": "..."},
        sources=frozenset({"read_latest_email"}),
        readers=frozenset({"bob@co.com", "alice@co.com"}),
    )


# Policy
def read_email_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    return Allowed()


# Wiring
policy_registry = PolicyRegistry()
policy_registry.register("read_latest_email", read_email_policy)

backend = ClaudeBackend(model="claude-sonnet-4-6")

interpreter = CaMeLInterpreter(
    tools={"read_latest_email": read_latest_email},
    policy_engine=policy_registry,
    mode=ExecutionMode.STRICT,
)

orchestrator = CaMeLOrchestrator(
    p_llm=PLLMWrapper(backend=backend),
    interpreter=interpreter,
    tool_signatures=[
        ToolSignature(
            name="read_latest_email",
            description="Reads the most recent email.",
            parameters={},
            return_type="dict",
        )
    ],
)

result = asyncio.run(orchestrator.run("What is in my latest email?"))

print("Success:", result.success)
for record in result.trace:
    print(record.tool_name, record.args)
```

### Milestone 5 SDK

```python
import asyncio
import os
from camel.llm.adapters import ClaudeBackend
from camel.value import wrap, Public, CaMeLValue
from camel_security import CaMeLAgent, Tool


# Tool function — unchanged
def read_latest_email() -> CaMeLValue:
    return wrap(
        {"from": "alice@co.com", "subject": "Q1 Report", "body": "..."},
        sources=frozenset({"read_latest_email"}),
        readers=frozenset({"bob@co.com", "alice@co.com"}),
    )


# Tool registration — new Tool dataclass
email_tool = Tool(
    name="read_latest_email",
    fn=read_latest_email,
    description="Reads the most recent email.",
    params="()",
    return_type="dict",
    # capability_annotation=... (optional custom annotator)
    # policies=[...]             (optional per-tool policies)
)

# Backend — api_key now explicit
backend = ClaudeBackend(
    model="claude-sonnet-4-6",
    api_key=os.environ["ANTHROPIC_API_KEY"],
)

# Agent — no manual wiring
agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[email_tool],
)

result = asyncio.run(agent.run("What is in my latest email?"))

print("Success:", result.success)
for record in result.execution_trace:    # renamed from result.trace
    print(record.tool_name, record.args)
```

---

## Upgrade Checklist

- [ ] Replace `pip install camel` with `pip install camel-security`
- [ ] Add `api_key=...` to all `ClaudeBackend` / `GeminiBackend` constructor calls
- [ ] Replace `ToolSignature` + separate `tools` dict with `Tool` dataclass
- [ ] Replace `CaMeLOrchestrator` / `PLLMWrapper` / `QLLMWrapper` wiring with `CaMeLAgent`
- [ ] Update `result.trace` → `result.execution_trace`
- [ ] Update `result.display_lines` → `result.display_output`
- [ ] Update `result.attempts` → `result.loop_attempts`
- [ ] (Optional) Migrate flat `PolicyRegistry` to `TieredPolicyRegistry` — see [Policy Authoring Guide](policy-authoring-guide.md)
- [ ] (Optional) Configure audit log sink via `CAMEL_AUDIT_SINK` / `CAMEL_AUDIT_SINK_PATH`
- [ ] Run full test suite after migration: `pytest`

---

## See Also

- [Developer Quickstart](quickstart.md) — fresh start with the M5 SDK
- [Tool Onboarding Guide](tool-onboarding.md) — registering tools with annotations and policies
- [Policy Authoring Guide](policy-authoring-guide.md) — three-tier model and `PolicyConflictResolver`
- [Architecture Reference](architecture.md) — full system architecture
- [CHANGELOG](../CHANGELOG.md) — detailed change log for each release
