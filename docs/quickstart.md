# CaMeL Developer Quickstart Guide

_CaMeL v0.6.0 (camel-security) — 5-minute integration walkthrough_

This guide gets you from zero to a running CaMeL agent in under five minutes.
You will install the SDK, configure P-LLM and Q-LLM backends, register a tool,
run your first task, and verify the security audit log output.

---

## Prerequisites

- Python 3.11 or later
- An Anthropic API key (or Gemini / OpenAI key — see [step 2](#2-configure-llm-backends) below)

---

## 1. Install the SDK

```bash
# Core install — Anthropic (Claude) backend included
pip install camel-security

# With Google Gemini support
pip install "camel-security[gemini]"

# With OpenAI support
pip install "camel-security[openai]"

# All three backends + observability (Prometheus/OpenTelemetry)
pip install "camel-security[all-backends,observability]"
```

---

## 2. Configure LLM Backends

CaMeL requires two LLM roles:

| Role | What it does |
|---|---|
| **P-LLM** (Privileged) | Generates the execution plan from the user query.  Never sees tool return values. |
| **Q-LLM** (Quarantined) | Extracts structured data from untrusted tool outputs.  Has no tool-calling capability. |

The same backend adapter class can serve both roles, or you can use different
models (e.g., a large model for planning, a smaller model for extraction):

```python
from camel.llm.adapters import ClaudeBackend  # or GeminiBackend / OpenAIBackend

# Using Claude for both P-LLM and Q-LLM
p_llm = ClaudeBackend(model="claude-sonnet-4-6", api_key="YOUR_ANTHROPIC_KEY")
q_llm = ClaudeBackend(model="claude-haiku-4-5", api_key="YOUR_ANTHROPIC_KEY")
```

### Backend options

| Class | Provider | Extra install | Example model |
|---|---|---|---|
| `ClaudeBackend` | Anthropic | _(included)_ | `claude-sonnet-4-6` |
| `GeminiBackend` | Google | `pip install "camel-security[gemini]"` | `gemini-2.5-flash` |
| `OpenAIBackend` | OpenAI | `pip install "camel-security[openai]"` | `gpt-4.1` |

You can read keys from environment variables:

```python
import os
from camel.llm.adapters import ClaudeBackend

p_llm = ClaudeBackend(
    model="claude-sonnet-4-6",
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

---

## 3. Define a Tool

Every tool must return a `CaMeLValue` — a Python value wrapped with provenance
metadata.  Use the `wrap` helper for the simplest case:

```python
from camel.value import wrap, Public, CaMeLValue

def get_inbox_count() -> CaMeLValue:
    """Return the number of unread messages."""
    # In production, call your real email API here
    return wrap(
        value=7,
        sources=frozenset({"get_inbox_count"}),
        readers=Public,
    )
```

Register the tool using the `Tool` dataclass:

```python
from camel_security import Tool

inbox_count_tool = Tool(
    name="get_inbox_count",
    fn=get_inbox_count,
    description="Returns the number of unread email messages.",
    params="()",
    return_type="int",
)
```

---

## 4. Create the Agent and Run a Task

```python
import asyncio
from camel_security import CaMeLAgent

agent = CaMeLAgent(
    p_llm=p_llm,
    q_llm=q_llm,
    tools=[inbox_count_tool],
    # policies=None means all-allow (fine for testing; see step 6 below)
)

result = asyncio.run(agent.run("How many unread emails do I have?"))

if result.success:
    print("Plan completed successfully.")
    print("Display output:", result.display_output)
    for record in result.execution_trace:
        print(f"  Tool called: {record.tool_name} → {record.args}")
else:
    print("Execution failed after", result.loop_attempts, "retries.")
```

Expected output:

```
Plan completed successfully.
Display output: ['7']
  Tool called: get_inbox_count → {}
```

### Synchronous variant

If you are not inside an async context, use `run_sync`:

```python
result = agent.run_sync("How many unread emails do I have?")
```

---

## 5. Inspect the Security Audit Log

The audit log records every tool call evaluation, policy decision, and
consent event.  After `run()` completes, read it from the interpreter (via the
lower-level API) or from `AgentResult.audit_log_ref`:

```python
# audit_log_ref is an opaque token for log correlation
print(result.audit_log_ref)   # e.g. "camel-audit:a1b2c3d4"

# For direct audit log access, construct the interpreter manually
# (see docs/api/interpreter.md for the low-level API)
```

To write audit events to a file, configure the structured JSON audit sink:

```python
from camel.observability.audit_sink import FileAuditSink

sink = FileAuditSink(path="/var/log/camel-audit.jsonl")
# Pass sink to CaMeLInterpreter when constructing directly, or set via env:
# CAMEL_AUDIT_SINK=file:///var/log/camel-audit.jsonl
```

Each line in the output is a JSON object:

```json
{"event": "PolicyEvaluation", "tool_name": "get_inbox_count", "outcome": "Allowed",
 "reason": null, "timestamp": "2026-03-18T10:00:00Z", "consent_decision": null}
```

See [Security Audit Log Reference](security-audit-log.md) for the full schema.

---

## 6. Add a Security Policy (recommended)

Running without policies allows all tool calls — fine for development but not
for production.  The quickest way to add policies is via the reference library:

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")

agent = CaMeLAgent(
    p_llm=p_llm,
    q_llm=q_llm,
    tools=[inbox_count_tool],
    policies=registry,
)
```

This activates six reference policies covering `send_email`, `send_money`,
`create_calendar_event`, `write_file`, `post_message`, and `fetch_external_url`.

For custom policies and the three-tier governance model, see the
[Policy Authoring Guide](policy-authoring-guide.md).

---

## 7. Verification Checklist

| Step | What to check |
|---|---|
| SDK installed | `python -c "import camel_security; print(camel_security.__version__)"` prints `0.6.0` |
| Backend configured | Backend constructor does not raise |
| Tool defined | `Tool` constructor accepts the function without errors |
| Agent runs | `result.success == True` on a simple query |
| Execution trace populated | `len(result.execution_trace) > 0` |
| Audit log ref present | `result.audit_log_ref.startswith("camel-audit:")` |

---

## Next Steps

| Guide | What you'll learn |
|---|---|
| [Tool Onboarding Guide](tool-onboarding.md) | Register tools with capability annotations and per-tool policies |
| [Policy Authoring Guide](policy-authoring-guide.md) | Write, test, and debug security policies using the three-tier model |
| [Migration Guide](migration-guide.md) | Migrate from the Milestone 4 prototype to the `camel-security` SDK |
| [Architecture Reference](architecture.md) | Deep-dive into the P-LLM / Q-LLM / Interpreter architecture |
| [Operator Guide](manuals/operator-guide.md) | Production deployment, environment configuration, monitoring |
