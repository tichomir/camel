# Three-Tier Policy Authorship Guide

_CaMeL v0.5 and later — see [ADR-011](../adr/011-three-tier-policy-governance.md)_

---

## Overview

CaMeL's three-tier policy governance model addresses two competing pressures in
enterprise deployments:

- **Security floor**: Platform operators need to declare rules that cannot be softened
  by any individual user or tool author, even with user consent.
- **Usability ceiling**: PRD Risk L4 — over-strict platform policies fatigue users who
  approve consent prompts without reading them, eroding security in practice.

The three tiers let different stakeholders own different parts of the policy space:

| Tier            | Author                  | Authority                                         | Can be `non_overridable`? |
|-----------------|-------------------------|---------------------------------------------------|---------------------------|
| **Platform**    | Deployment operator     | Highest — sets the security floor for the entire deployment | Yes                       |
| **Tool-Provider** | Tool/plugin author    | Middle — constrains how its own tool may be called | No                        |
| **User**        | End user                | Lowest — personalises within guardrails above     | No                        |

---

## Tier Hierarchy and Precedence Rules

Policies are evaluated in this fixed order for every tool call:

```
Platform → Tool-Provider → User
```

Within each tier, policies run in **registration order**.  The first `Denied`
returned by any policy halts evaluation of that tier and all lower tiers.

**Effect of `non_overridable` on a Platform `Denied`:**

```
Platform Denied + non_overridable=True
  → Evaluation stops immediately.
  → Consent callback is NOT invoked (even in PRODUCTION mode).
  → PolicyViolationError is raised with non_overridable_denial=True.

Platform Denied + non_overridable=False
  → Evaluation stops.
  → Consent callback IS available in PRODUCTION mode (user may override).
  → PolicyViolationError raised if user declines or in EVALUATION mode.
```

If all tiers return `Allowed` (or have no policies registered), the tool call
proceeds.

---

## Quick Start

### 1. Install and import

```python
from camel.policy.governance import (
    TieredPolicyRegistry,
    PolicyConflictResolver,
    PolicyTier,
)
from camel.policy import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping
```

### 2. Build a tiered registry

```python
registry = TieredPolicyRegistry()

# Platform policy — absolute: no external email recipients, ever
@registry.register_platform("send_email", non_overridable=True)
def no_external_recipients(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    to = kwargs.get("to")
    if to is not None and "@external.example.com" in str(to.raw):
        return Denied("Platform rule: external recipients are not allowed")
    return Allowed()

# Tool-Provider policy — send_email tool restricts attachment size
@registry.register_tool_provider("send_email")
def attachment_size_limit(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    attachment = kwargs.get("attachment")
    if attachment is not None and len(str(attachment.raw)) > 100_000:
        return Denied("send_email: attachment exceeds 100 KB limit")
    return Allowed()

# User policy — user only wants to email their team
@registry.register_user("send_email")
def user_team_only(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    to = kwargs.get("to")
    if to is not None and str(to.raw) not in {"alice@corp.com", "bob@corp.com"}:
        return Denied("User preference: only send to my team members")
    return Allowed()
```

### 3. Create the resolver and wire it into the interpreter

```python
resolver = PolicyConflictResolver(registry)

from camel.interpreter import CaMeLInterpreter, EnforcementMode

interpreter = CaMeLInterpreter(
    tools=my_tools,
    conflict_resolver=resolver,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_callback=my_consent_ui,
)
```

### 4. Inspect the audit trail

```python
merged = resolver.evaluate("send_email", kwargs)
print(f"Outcome: {merged.outcome}")
print(f"Authoritative tier: {merged.authoritative_tier}")
print(f"Non-overridable denial: {merged.non_overridable_denial}")
for record in merged.audit_trail:
    print(f"  [{record.tier.value}] {record.policy_name}: {record.result}")
```

---

## When to Use Each Tier

### Platform tier

Use Platform policies for rules derived from:

- **Regulatory or legal requirements** — data residency, GDPR export restrictions,
  SOC 2 controls.  Mark these `non_overridable=True`.
- **Organisational security policy** — e.g., "no AI agent may ever transfer funds
  without a human in the loop".  Almost always `non_overridable=True`.
- **Cross-cutting guardrails** — rules that apply to every tool, regardless of tool
  author or user preference.  E.g., "no tool may contact IP addresses in sanctioned
  country ranges".

**Rule of thumb for `non_overridable`:** If a security auditor would flag a user
consent override as a compliance incident, the rule should be `non_overridable=True`.
If a power user could reasonably override the rule for a legitimate business purpose,
leave it `non_overridable=False`.

### Tool-Provider tier

Use Tool-Provider policies for rules that:

- Are specific to the tool's own security contract — e.g., an email tool that exposes
  a `bcc` field may have a policy that `bcc` must contain only internal addresses.
- Address the tool's known attack surface — e.g., a URL fetcher restricts the URL
  scheme to `https://`.
- Should apply to every caller of the tool regardless of user preferences.

Tool-Provider policies should **not** duplicate Platform policies.  If the deployment
platform already enforces a rule, the tool does not need to re-implement it.

### User tier

Use User policies for:

- **Personal preferences** — e.g., "only schedule meetings on my calendar at hours
  in my working timezone".
- **Role-based self-restriction** — e.g., a developer who only wants the agent to
  modify files in their own workspace directory.
- **Consent fatigue reduction** — registering a user-tier policy that pre-approves
  trusted recipients reduces the number of consent prompts triggered by higher-tier
  policies.

User-tier `Denied` results CAN be overridden by user consent in PRODUCTION mode.
This seems paradoxical (the user denies something, then approves it), but it is useful
for soft guardrails that a user sets as defaults but may want to override on demand.

---

## Non-Overridable Rule Selection Criteria

Apply `non_overridable=True` to a Platform policy when **all three** of the following
hold:

1. **The rule is mandated by an external authority** (law, regulation, or board-level
   policy) rather than an internal preference.
2. **A successful bypass would be a reportable incident** — i.e., the compliance team
   would need to file a report if a user approved the consent prompt and the action
   proceeded.
3. **No legitimate user business case exists for an exception** — if exceptions might
   be needed, they should be handled by adjusting the rule itself (e.g., an allowlist),
   not by relying on per-invocation consent.

If only points 1 and 2 hold but point 3 does not, consider keeping the policy
`non_overridable=False` and relying on the consent callback to surface the risk to
a human approver.

---

## Migration Path from Flat `PolicyRegistry`

If you have an existing `PolicyRegistry`-based deployment, migration is incremental:

### Step 1 — No code changes needed yet

The existing `PolicyRegistry` and `CaMeLInterpreter(policy_engine=...)` API is
unchanged.  Your existing policies continue to work.

### Step 2 — Opt in to tiered governance for new policies

Create a `TieredPolicyRegistry` and register new policies in it.  Wrap it in a
`PolicyConflictResolver` and pass it as `conflict_resolver` to the interpreter:

```python
# Before (flat):
registry = PolicyRegistry()
registry.register("send_email", email_policy)
interp = CaMeLInterpreter(tools=..., policy_engine=registry)

# After (tiered, additive):
tiered = TieredPolicyRegistry()
tiered.register_platform("send_email", email_policy, non_overridable=True)
resolver = PolicyConflictResolver(tiered)
interp = CaMeLInterpreter(tools=..., conflict_resolver=resolver)
# Note: do NOT pass both policy_engine and conflict_resolver — ValueError is raised.
```

### Step 3 — Migrate existing policies to the appropriate tier

Walk through each existing policy and assign it to Platform, Tool-Provider, or User
based on the guidance in "When to Use Each Tier" above.  Use the `non_overridable`
criteria to flag rules that should be absolute.

### Step 4 — Remove the flat registry

Once all policies are migrated to the tiered registry, remove the `policy_engine`
parameter from the interpreter constructor.

---

## Example: Enterprise Email Deployment

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver
from camel.policy import Allowed, Denied, is_trusted
from camel.value import CaMeLValue
from collections.abc import Mapping


def build_enterprise_email_policies() -> PolicyConflictResolver:
    registry = TieredPolicyRegistry()

    # --- Platform (absolute) ---

    @registry.register_platform("send_email", non_overridable=True)
    def no_pii_to_unclassified(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Block email body containing data from PII-classified sources."""
        body = kwargs.get("body")
        if body is not None:
            for source in body.sources:
                if source.startswith("pii:"):
                    return Denied(
                        f"Platform rule: body contains PII data from '{source}'; "
                        "cannot send to unclassified recipient"
                    )
        return Allowed()

    @registry.register_platform("send_email", non_overridable=False)
    def prefer_internal_recipients(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Soft platform rule: warn when sending to external recipients.

        non_overridable=False so that users can consent to external sends.
        """
        to = kwargs.get("to")
        if to is not None and not is_trusted(to):
            return Denied(
                "Platform guideline: recipient address comes from untrusted data; "
                "please verify before sending"
            )
        return Allowed()

    # --- Tool-Provider ---

    @registry.register_tool_provider("send_email")
    def no_bcc_exfiltration(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """send_email tool: BCC must only contain trusted addresses."""
        bcc = kwargs.get("bcc")
        if bcc is not None and not is_trusted(bcc):
            return Denied("send_email: BCC address must come from a trusted source")
        return Allowed()

    # --- User ---

    @registry.register_user("send_email")
    def user_approved_contacts(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """User preference: only email pre-approved contacts."""
        approved = {"alice@corp.com", "bob@corp.com", "carol@partner.com"}
        to = kwargs.get("to")
        if to is not None and str(to.raw) not in approved:
            return Denied(
                f"User preference: '{to.raw}' is not in your approved contacts list"
            )
        return Allowed()

    return PolicyConflictResolver(registry)
```

---

## Audit Log Interpretation for Compliance Reporting

Each `AuditLogEntry` produced when `PolicyConflictResolver` is active includes:

| Field                  | Meaning                                                                |
|------------------------|------------------------------------------------------------------------|
| `tool_name`            | The tool that was evaluated                                            |
| `outcome`              | `"Allowed"` or `"Denied"`                                              |
| `reason`               | The denial reason string (None if Allowed)                             |
| `timestamp`            | ISO-8601 UTC timestamp                                                 |
| `consent_decision`     | `"UserApproved"`, `"UserRejected"`, or `None`                         |
| `authoritative_tier`   | `"Platform"`, `"ToolProvider"`, `"User"`, or `None` (all Allowed)    |
| `non_overridable_denial` | `True` if the Platform policy had `non_overridable=True`             |

**Common compliance query patterns:**

```python
# All non-overridable denials (regulatory incidents)
incidents = [
    e for e in audit_log
    if e.non_overridable_denial
]

# All user consent overrides of platform-soft-denials
overrides = [
    e for e in audit_log
    if e.authoritative_tier == "Platform"
    and e.consent_decision == "UserApproved"
]

# All tool-provider denials (tool-specific violations)
tp_denials = [
    e for e in audit_log
    if e.authoritative_tier == "ToolProvider"
    and e.outcome == "Denied"
]
```

---

## Using Provenance Metadata in Capability-Aware Policies

CaMeL's provenance system (ADR-013) provides richer origin information about
argument values than the basic `is_trusted` / `can_readers_read_value` helpers
alone.  Policy authors can use `ProvenanceChain` data to write more expressive,
context-aware policies.

### Accessing provenance within a policy

Inside a policy function, argument values are `CaMeLValue` instances with a
`sources` frozenset.  You can construct a `ProvenanceChain` directly:

```python
from camel.provenance import build_provenance_chain, TRUSTED_SOURCES
from camel.policy import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping

def provenance_aware_send_email_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    """Block send_email when the recipient address came from a tool that
    also produced phishing-pattern content in the same run."""
    to = kwargs.get("to")
    if to is None:
        return Allowed()

    chain = build_provenance_chain("to", to)

    # Reject if the recipient address originates from an untrusted tool
    # AND was extracted from a field named "sender" (inner_source).
    # This blocks a common attack where a malicious email body contains
    # "Reply-To: attacker@evil.com" that gets extracted as the recipient.
    for hop in chain.hops:
        if hop.tool_name not in TRUSTED_SOURCES and hop.inner_source == "sender":
            return Denied(
                f"Recipient address originates from the 'sender' field of an "
                f"untrusted tool ({hop.tool_name!r}).  Verify the recipient "
                f"independently before sending."
            )
    return Allowed()
```

### Checking inner_source in policy logic

`ProvenanceHop.inner_source` records which sub-field of a tool's structured
output a value was extracted from (e.g. `"sender"`, `"body"`, `"subject"`).
Use this to distinguish between different origins within the same tool:

```python
from camel.provenance import build_provenance_chain, TRUSTED_SOURCES

def subject_only_email_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    """Allow forwarding only content extracted from the email subject,
    never from the body (which may contain injected content)."""
    body = kwargs.get("body")
    if body is None:
        return Allowed()

    chain = build_provenance_chain("body", body)
    for hop in chain.hops:
        if hop.tool_name not in TRUSTED_SOURCES and hop.inner_source == "body":
            return Denied(
                "Policy: forwarding email body content is not allowed — "
                "only subject-line content may be forwarded."
            )
    return Allowed()
```

### Surfacing provenance in denial reasons

The `Denied(reason)` string is displayed to the user in consent prompts and
written to the audit log.  Include provenance details in the reason to help
users understand why a call was blocked:

```python
untrusted = [
    hop.tool_name for hop in chain.hops
    if hop.tool_name not in TRUSTED_SOURCES
]
if untrusted:
    return Denied(
        f"Recipient address traces back to untrusted tool(s): "
        f"{', '.join(untrusted)}.  Approve only if you recognise this address."
    )
```

### Relationship to phishing warnings

Phishing warnings (`AgentResult.phishing_warnings`) are **advisory** — they do
not block execution.  Policies using `build_provenance_chain` and `inner_source`
checks are the **enforcement** mechanism.  The two systems are complementary:

- **Policies** gate tool calls before they execute (synchronous, deterministic,
  NFR-2 compliant).
- **Phishing warnings** alert the user after the run completes (advisory,
  pattern-based, surfaced in the UI).

For the maximum protection against injected-sender attacks, combine a
provenance-aware policy (to block suspicious tool calls) with phishing warning
display in the chat UI (to alert the user when suspicious content reaches the
response).

See the [Provenance Badges User Guide](../user-guide/provenance-badges.md) for
the end-user documentation explaining how to interpret provenance badges and
phishing warnings in the chat UI.

---

## Reference

- [ADR-011: Three-Tier Policy Governance](../adr/011-three-tier-policy-governance.md)
- [ADR-009: Policy Engine Architecture](../adr/009-policy-engine-architecture.md)
- [ADR-010: Enforcement Hook, Consent Flow, Audit Log](../adr/010-enforcement-hook-consent-audit-harness.md)
- [ADR-013: Provenance Chain API and Phishing-Content Heuristic](../adr/013-provenance-chain-phishing-heuristic.md)
- [Reference Policy Library Specification](reference-policy-spec.md)
- [Provenance Badges User Guide](../user-guide/provenance-badges.md)
- [PRD Section 6.4 — Capabilities & Provenance](../architecture.md#12-capability-assignment-engine)
- [PRD Section 6.5 — Security Policies](../architecture.md)
