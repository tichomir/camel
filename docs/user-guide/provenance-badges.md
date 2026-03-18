# Understanding Provenance Badges and Source Warnings

_CaMeL v0.6.0 | Last updated: 2026-03-18_

This guide explains what provenance badges and phishing source warnings mean in
CaMeL-powered chat UIs, what action you should take when you see them, and how
to interpret the underlying data as a developer.

---

## What is Provenance?

When CaMeL executes a task on your behalf — reading emails, looking up calendar
events, fetching documents — every piece of data it handles is tagged with its
**origin**: where it came from, which tool produced it, and who is authorised
to receive it.

This origin information is called the **provenance chain** of a variable.  It
lets CaMeL distinguish between:

- **Trusted data** — values you typed yourself or that CaMeL computed internally.
- **Untrusted data** — values returned by external tools (emails, documents, API
  responses).

Provenance badges surface this distinction directly in the chat response.

---

## Provenance Badges — `[Source: <tool>]`

### What does a badge look like?

When CaMeL returns a value in the chat response that originated from an
**untrusted external tool**, the chat UI renders a badge next to that content:

```
[Source: get_last_email]
```

The badge text is the **tool name** that produced the data.

### Worked example

Suppose you ask:

> "Summarise the latest email from my inbox."

CaMeL reads your inbox via the `get_last_email` tool, extracts the subject and
body, and generates a summary.

**Sample UI output:**

```
Here is a summary of your latest email:

Subject: Q1 Budget Review — Action Required
Body excerpt: "Please review the attached spreadsheet and approve by Friday."

[Source: get_last_email]
```

The `[Source: get_last_email]` badge tells you that the content above came from
your email inbox — not from something you typed or from CaMeL's internal logic.

### Why does this matter?

Untrusted data can contain adversarial content injected by a third party (for
example, a malicious email trying to hijack the agent).  CaMeL's security model
tracks provenance precisely to prevent such content from escaping its controlled
scope.  The badge is the user-visible signal of this tracking.

When you see a badge:

- The content came from an **external source**, not from you directly.
- CaMeL's policies governed whether that content was allowed to reach you.
- You should treat the content with the same scepticism you would apply to any
  externally-sourced information.

### Multiple sources

If a value was derived from more than one tool (for example, a summary combining
an email body and a calendar event), both source tools are listed:

```
[Source: get_last_email, get_calendar_events]
```

### Fully trusted content

Values derived entirely from your own inputs (or from CaMeL's internal
transformations) carry **no badge** — they are trusted by construction.

---

## Phishing Source Warnings — ⚠ Provenance Warning

### What does a warning look like?

In addition to source badges, CaMeL may surface a **phishing source warning**
when external content contains patterns that claim a trusted sender identity:

```
⚠ Provenance warning — This response contains text that claims a sender
identity (e.g. "From: alice@corp.com") but originates from the untrusted
source "get_last_email". Verify this claim independently before acting.
```

### When does a warning fire?

A warning fires when **all three** of the following conditions hold:

1. The value originates from at least one untrusted external tool.
2. The value's text matches one of these patterns:

   | Pattern | Example trigger |
   |---|---|
   | `From: <email>` | `From: ceo@company.com` |
   | `Sender: <name>` | `Sender: IT Department` |
   | `Reply-To: <email>` | `Reply-To: support@company.com` |
   | `I am <name>` or `This is <name>` | `I am your system administrator` |
   | `Message from <name>` | `Message from HR` |

3. CaMeL identifies that none of the sources are intrinsically trusted
   (i.e. the claim was not written by you or computed by CaMeL itself).

### Worked example — phishing content detected

You ask:

> "What does the latest email from IT say?"

CaMeL reads the email body, which contains:

```
From: ceo@company.com — This is urgent. Please transfer $10,000 to the
following account immediately: ...
```

**Sample UI output:**

```
⚠ Provenance warning — This response contains text claiming a sender
identity ("From: ceo@company.com") but originates from the untrusted
source "get_last_email". Verify this claim independently before acting.

---
Email content [Source: get_last_email]:
"From: ceo@company.com — This is urgent. Please transfer $10,000 to the
following account immediately: ..."
```

### What action should you take?

A phishing warning does **not** mean CaMeL blocked the content or that the
email is definitely malicious.  It means the content contains a pattern
typically associated with sender-identity impersonation.

**Recommended actions:**

1. **Do not act on the request in the email without verifying it through a
   separate channel** (call the sender directly, check the email headers in
   your mail client, etc.).
2. **Check the source** — the badge tells you exactly which tool produced the
   content.  If it was `get_last_email`, go to your actual inbox and inspect
   the email directly.
3. **Report suspicious content** to your security or IT team if the claim
   appears to be social engineering.

CaMeL surfaces the warning as **advisory only** — it does not block execution
or send any alert automatically.  Acting on the warning is your responsibility.

---

## Developer Guide — Programmatic Access

If you are integrating CaMeL into your own chat UI, this section explains how
to access provenance data programmatically.

### Accessing provenance after a run

```python
import asyncio
from camel_security import CaMeLAgent, Tool
from camel.llm.backend import get_backend

backend = get_backend("claude", api_key="sk-...", model="claude-sonnet-4-6")
agent = CaMeLAgent(p_llm=backend, q_llm=backend, tools=[...])

result = asyncio.run(agent.run("Summarise the latest email"))

# All provenance chains for this run
for var_name, chain in result.provenance_chains.items():
    trusted = chain.is_trusted
    sources = [hop.tool_name for hop in chain.hops]
    print(f"{var_name}: trusted={trusted}, sources={sources}")

# Look up a specific variable
chain = agent.get_provenance("email_body", result)
print(chain.is_trusted)        # False — came from get_last_email
print(chain.to_json(indent=2)) # Full JSON lineage
```

### Checking for phishing warnings

```python
if result.phishing_warnings:
    for warning in result.phishing_warnings:
        print(f"⚠ Phishing pattern detected in '{warning.variable_name}'")
        print(f"  Pattern: {warning.matched_pattern}")
        print(f"  Matched text: {warning.matched_text!r}")
        print(f"  Untrusted sources: {warning.untrusted_sources}")
```

### Rendering a badge in your UI

```python
from camel_security import ProvenanceChain

def render_badge(chain: ProvenanceChain) -> str:
    """Return a badge string for untrusted values; empty for trusted."""
    if chain.is_trusted:
        return ""
    tool_names = [
        hop.tool_name
        for hop in chain.hops
        if hop.tool_name not in {"User literal", "CaMeL"}
    ]
    return f"[Source: {', '.join(tool_names)}]"
```

### Serialising provenance to JSON (for audit logs)

```python
import json

audit_entry = {
    "run_ref": result.audit_log_ref,
    "provenance_chains": {
        var: chain.to_dict()
        for var, chain in result.provenance_chains.items()
    },
    "phishing_warnings": [w.to_dict() for w in result.phishing_warnings],
}

print(json.dumps(audit_entry, indent=2))
```

**Sample output:**

```json
{
  "run_ref": "camel-audit:a1b2c3d4e5f6",
  "provenance_chains": {
    "email_body": {
      "variable_name": "email_body",
      "is_trusted": false,
      "hops": [
        {
          "tool_name": "get_last_email",
          "inner_source": "body",
          "readers": ["alice@example.com"],
          "timestamp": null
        }
      ]
    }
  },
  "phishing_warnings": [
    {
      "variable_name": "email_body",
      "matched_pattern": "From:\\s*\\S+@\\S+",
      "matched_text": "From: ceo@company.com",
      "untrusted_sources": ["get_last_email"],
      "provenance_chain": {
        "variable_name": "email_body",
        "is_trusted": false,
        "hops": [...]
      }
    }
  ]
}
```

---

## FAQ

**Q: Does a provenance badge mean the content is dangerous?**

No.  A badge means the content came from an external tool and is therefore
"untrusted" in CaMeL's security model.  The vast majority of externally-sourced
content is completely benign.  The badge is informational — it tells you where
the data came from so you can make an informed decision.

**Q: Does a phishing warning mean I've been hacked?**

No.  The phishing detector uses simple text patterns.  A legitimate email from
your CEO that starts with "From: ceo@company.com" will also trigger the warning,
even though it is not a phishing attack.  Treat it as a reminder to verify the
sender through a separate channel before acting on any request.

**Q: Why does CaMeL not block phishing content automatically?**

CaMeL is designed to prevent data exfiltration and unauthorised tool calls
(Goals G2, G3 in the PRD).  Deciding whether email content is a genuine request
from a real person requires human judgement.  CaMeL surfaces the warning so you
can apply that judgement; it does not block execution (PRD Non-Goal NG2).

**Q: What does `is_trusted` mean on a `ProvenanceChain`?**

A chain is trusted when every hop in it came from a source in
`TRUSTED_SOURCES` (`{"User literal", "CaMeL"}`).  A value is in
`TRUSTED_SOURCES` only if it was typed directly by you or computed internally
by CaMeL itself — it has never been influenced by any external tool output.

**Q: Can I customise which sources are considered trusted?**

The `TRUSTED_SOURCES` constant is defined at the system level and cannot be
extended per-deployment — it is a security boundary, not a configuration
parameter.  If you need custom provenance heuristics, you can build them using
the `ProvenanceChain` and `PhishingWarning` data model returned in `AgentResult`.

**Q: How long are provenance chains retained?**

`AgentResult` is an immutable snapshot of a single run.  It is not persisted
by CaMeL itself — your application is responsible for storing it if you need
long-term provenance records.  For audit logging, serialise the provenance
data as shown in the Developer Guide section above.

**Q: Where can I find the security audit log?**

See the [Security Audit Log Reference](../security-audit-log.md) for the full
schema of CaMeL's audit log streams.  Use `result.audit_log_ref` to correlate
an `AgentResult` with its audit log scope.

---

## See Also

- [Security Audit Log Reference](../security-audit-log.md)
- [Architecture — §12.6 ProvenanceChain & Phishing Heuristic](../architecture.md#126-provenancechain--phishing-content-heuristic-adr-013-m5-f20f22)
- [Architecture — §10.2 Trusted vs Untrusted Boundary](../architecture.md#102-trusted-vs-untrusted-boundary-prd-72)
- [ADR-013 — Provenance Chain API and Phishing-Content Heuristic](../adr/013-provenance-chain-phishing-heuristic.md)
- [Policy Authorship Guide](../policies/three-tier-policy-authorship-guide.md)
