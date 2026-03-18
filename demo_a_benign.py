"""Scenario A — benign task: read and summarise the last email."""

import asyncio
import os

from camel.llm.adapters import ClaudeBackend      # swap for GeminiBackend / OpenAIBackend
from camel.observability.audit_sink import AuditSink, AuditSinkConfig, SinkMode
from camel.value import CaMeLValue, wrap
from camel_security import CaMeLAgent, Tool


# ── Mock tool: pretend to fetch the last email ───────────────────────────────
def get_last_email() -> CaMeLValue:
    """Return the most recent email as a capability-tagged CaMeLValue."""
    raw_email = {
        "subject": "Q2 budget review",
        "sender": "cfo@example.com",
        "body": "Please review the attached Q2 numbers before Friday's board meeting.",
    }
    return wrap(
        value=raw_email,
        sources=frozenset({"get_last_email"}),
        readers=frozenset({"alice@example.com"}),  # only Alice may receive this
    )


# ── Tool registration ─────────────────────────────────────────────────────────
email_tool = Tool(
    name="get_last_email",
    fn=get_last_email,
    description="Fetches the most recent email from the user's inbox.",
    params="()",
    return_type="dict",
)

# ── Backends ──────────────────────────────────────────────────────────────────
api_key = os.environ["ANTHROPIC_API_KEY"]
p_llm = ClaudeBackend(model="claude-sonnet-4-6", api_key=api_key)
q_llm = ClaudeBackend(model="claude-haiku-4-5", api_key=api_key)

# ── Audit log to stdout ───────────────────────────────────────────────────────
sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))

# ── Agent ─────────────────────────────────────────────────────────────────────
agent = CaMeLAgent(
    p_llm=p_llm,
    q_llm=q_llm,
    tools=[email_tool],
    # No policies argument → all tool calls allowed (suitable for this demo)
)

# ── Run ───────────────────────────────────────────────────────────────────────
result = asyncio.run(agent.run("Read my last email and give me a one-sentence summary."))

print("\n=== RESULT ===")
if result.success:
    print("Status   : SUCCESS")
    print("Output   :", result.display_output)
    print("Trace    :", [(r.tool_name, r.args) for r in result.execution_trace])
    print("Audit ref:", result.audit_log_ref)
else:
    print("Status   : FAILED after", result.loop_attempts, "retries")
