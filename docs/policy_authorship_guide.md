# CaMeL Policy Authorship Guide

> **Deprecated — superseded by [Policy Authoring Guide](policy-authoring-guide.md)**
>
> This document is retained for its additional detail on compliance query patterns
> and environment-based deployment configuration not yet present in the combined
> guide. For all new work, use [**docs/policy-authoring-guide.md**](policy-authoring-guide.md),
> which covers policy basics, the three-tier model, test harness, simulator,
> enterprise patterns, and migration in a single place.

_CaMeL v0.5 — Multi-Party Policy Governance (ADR-011, PRD FW-5)_
_See the current guide at [policy-authoring-guide.md](policy-authoring-guide.md) for v0.6.0+._

---

## Overview

CaMeL's three-tier policy governance model addresses two competing pressures in
enterprise deployments:

- **Security floor (PRD G4):** Platform operators need to declare rules that cannot be
  softened by any individual user or tool author, even with user consent.
- **Usability ceiling (PRD Risk L4):** Over-strict platform policies fatigue users who
  approve consent prompts without reading them, eroding security in practice.

The three-tier model resolves this tension by distributing policy authority across
stakeholders, so each tier can own the decisions appropriate to its role, trust level,
and accountability:

| Tier              | Author                  | Authority                                              | `non_overridable` allowed? |
|-------------------|-------------------------|--------------------------------------------------------|----------------------------|
| **Platform**      | Deployment operator     | Highest — sets the security floor for the deployment  | Yes                        |
| **Tool-Provider** | Tool / plugin author    | Middle — constrains how its own tool may be called    | No                         |
| **User**          | End user                | Lowest — personalises within guardrails above         | No                         |

**Key types (all in `camel.policy.governance`):**

| Type | Description |
|---|---|
| `PolicyTier` | Enum: `PLATFORM`, `TOOL_PROVIDER`, `USER` |
| `TieredPolicyRegistry` | Storage layer — register policies per tier |
| `TieredPolicyEntry` | One policy function + its tier metadata |
| `PolicyConflictResolver` | Merging engine — produces `MergedPolicyResult` |
| `MergedPolicyResult` | Final outcome + authoritative tier + audit trail |
| `TierEvaluationRecord` | One record in the audit trail |

---

## Tier Hierarchy and Precedence Rules

Policies are evaluated in this fixed order for every tool call:

```
Platform → Tool-Provider → User
```

Within each tier, policies run in **registration order**.  The first `Denied`
returned by any policy halts evaluation of that tier and all lower tiers.

### Effect of `non_overridable` on a Platform `Denied`

```
Platform Denied + non_overridable=True
  → Evaluation stops immediately.
  → Consent callback is NOT invoked (even in PRODUCTION mode).
  → MergedPolicyResult.non_overridable_denial = True
  → Caller must raise PolicyViolationError unconditionally.

Platform Denied + non_overridable=False
  → Evaluation stops.
  → Consent callback IS available in PRODUCTION mode (user may override).
  → PolicyViolationError raised if user declines or in EVALUATION mode.
```

If all tiers return `Allowed` (or have no policies registered), the tool call
proceeds and `MergedPolicyResult.authoritative_tier` is `None`.

### Conflict Resolution Algorithm (ADR-011 §4.1)

```
function evaluate(tool_name, kwargs):
    for entry in platform_entries(tool_name):            # Phase 1
        result = entry.policy_fn(tool_name, kwargs)
        record_to_audit_trail(entry, result, authoritative=result.is_denied)
        if result.is_denied:
            return MergedPolicyResult(
                outcome              = result,
                authoritative_tier   = PLATFORM,
                non_overridable_denial = entry.non_overridable,
                audit_trail          = recorded_so_far,
            )

    for entry in tool_provider_entries(tool_name):       # Phase 2
        result = entry.policy_fn(tool_name, kwargs)
        record_to_audit_trail(entry, result, authoritative=result.is_denied)
        if result.is_denied:
            return MergedPolicyResult(
                outcome              = result,
                authoritative_tier   = TOOL_PROVIDER,
                non_overridable_denial = False,
                audit_trail          = recorded_so_far,
            )

    for entry in user_entries(tool_name):                # Phase 3
        result = entry.policy_fn(tool_name, kwargs)
        record_to_audit_trail(entry, result, authoritative=result.is_denied)
        if result.is_denied:
            return MergedPolicyResult(
                outcome              = result,
                authoritative_tier   = USER,
                non_overridable_denial = False,
                audit_trail          = recorded_so_far,
            )

    return MergedPolicyResult(                           # Phase 4 — all allowed
        outcome              = Allowed(),
        authoritative_tier   = None,
        non_overridable_denial = False,
        audit_trail          = recorded_so_far,
    )
```

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
    status = "AUTHORITATIVE" if record.authoritative else "passed"
    print(f"  [{record.tier.value}] {record.policy_name}: {record.result} ({status})")
```

---

## When to Use Each Tier

### Platform tier

Use Platform policies for rules derived from:

- **Regulatory or legal requirements** — data residency, GDPR export restrictions,
  SOC 2 controls.  Mark these `non_overridable=True`.
- **Organisational security policy** — e.g., "no AI agent may ever transfer funds
  without a human in the loop".  Almost always `non_overridable=True`.
- **Cross-cutting guardrails** — rules that apply regardless of tool author or user
  preference.  E.g., "no tool may contact IP addresses in sanctioned country ranges".

**Rule of thumb for `non_overridable`:** If a security auditor would flag a user
consent override as a compliance incident, the rule should be `non_overridable=True`.
If a power user could reasonably override it for a legitimate business purpose, leave
it `non_overridable=False`.

### Tool-Provider tier

Use Tool-Provider policies for rules that:

- Are specific to the tool's own security contract — e.g., an email tool that exposes
  a `bcc` field may have a policy that `bcc` must contain only internal addresses.
- Address the tool's known attack surface — e.g., a URL fetcher restricts the URL
  scheme to `https://`.
- Should apply to every caller of the tool regardless of user preferences.

Tool-Provider policies should **not** duplicate Platform policies.

### User tier

Use User policies for:

- **Personal preferences** — e.g., "only schedule meetings at hours in my timezone".
- **Role-based self-restriction** — e.g., a developer who only wants the agent to
  modify files in their own workspace directory.
- **Consent-fatigue reduction** — registering a user-tier policy that pre-approves
  trusted recipients reduces the number of consent prompts triggered by higher tiers.

User-tier `Denied` results CAN be overridden by user consent in PRODUCTION mode.
This is useful for soft guardrails that a user sets as defaults but may want to
override on demand.

---

## `non_overridable` Selection Criteria

Apply `non_overridable=True` to a Platform policy when **all three** of the following
hold:

1. **The rule is mandated by an external authority** (law, regulation, or board-level
   policy) rather than an internal preference.
2. **A successful bypass would be a reportable incident** — i.e., the compliance team
   would need to file a report if the action proceeded.
3. **No legitimate user business case exists for an exception** — if exceptions might
   be needed, handle them by adjusting the rule (e.g., an allowlist), not by relying on
   per-invocation consent.

If only 1 and 2 hold but 3 does not, consider `non_overridable=False` and relying on
the consent callback to surface the risk to a human approver.

---

## Audit Trail Field Reference

Each `TierEvaluationRecord` in `MergedPolicyResult.audit_trail` contains:

| Field             | Type                       | Meaning                                                                     |
|-------------------|----------------------------|-----------------------------------------------------------------------------|
| `tier`            | `PolicyTier`               | Which tier this policy belongs to                                           |
| `policy_name`     | `str`                      | Human-readable name (defaults to `policy_fn.__name__`)                     |
| `result`          | `SecurityPolicyResult`     | `Allowed()` or `Denied(reason)`                                             |
| `non_overridable` | `bool`                     | Always `False` for non-Platform tiers                                       |
| `authoritative`   | `bool`                     | `True` if this record determined the final outcome                          |

`MergedPolicyResult` top-level fields:

| Field                    | Type                   | Meaning                                                                     |
|--------------------------|------------------------|-----------------------------------------------------------------------------|
| `outcome`                | `SecurityPolicyResult` | The authoritative `Allowed()` or `Denied(reason)` for the tool call        |
| `authoritative_tier`     | `PolicyTier \| None`   | Which tier's policy determined the outcome; `None` if all tiers allowed    |
| `non_overridable_denial` | `bool`                 | `True` only when a Platform `non_overridable=True` policy returned `Denied` |
| `audit_trail`            | `tuple[TierEvaluationRecord, ...]` | All evaluated records in evaluation order                       |
| `is_allowed`             | `bool` (property)      | `True` when `outcome.is_allowed()`                                          |
| `can_be_consented`       | `bool` (property)      | `False` when `non_overridable_denial` is `True`; consent must not be invoked |

**Audit log entries** produced when `PolicyConflictResolver` is active also carry:

| Field                  | Meaning                                                                |
|------------------------|------------------------------------------------------------------------|
| `tool_name`            | The tool that was evaluated                                            |
| `outcome`              | `"Allowed"` or `"Denied"`                                              |
| `reason`               | Denial reason string (`None` if Allowed)                               |
| `timestamp`            | ISO-8601 UTC timestamp                                                 |
| `consent_decision`     | `"UserApproved"`, `"UserRejected"`, or `None`                         |
| `authoritative_tier`   | `"Platform"`, `"ToolProvider"`, `"User"`, or `None` (all Allowed)    |
| `non_overridable_denial` | `True` if the Platform policy had `non_overridable=True`            |

---

## Enterprise Deployment Patterns

### Pattern 1: Regulated Financial Services — Email and Money Transfer

A financial services deployment with strict regulatory requirements around data
exfiltration and fund transfers.

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver
from camel.policy import Allowed, Denied, is_trusted, get_all_sources
from camel.value import CaMeLValue
from collections.abc import Mapping


def build_financial_services_policies() -> PolicyConflictResolver:
    registry = TieredPolicyRegistry()

    # ── Platform (absolute — regulatory requirements) ──────────────────────

    @registry.register_platform("send_email", non_overridable=True)
    def no_pii_to_unclassified(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Block email body containing data from PII-classified sources.

        Regulatory basis: GDPR Article 25 — data protection by design.
        non_overridable=True: bypass would be a reportable incident.
        """
        body = kwargs.get("body")
        if body is not None:
            for source in get_all_sources(body):
                if source.startswith("pii:"):
                    return Denied(
                        f"Platform rule: body contains PII data from '{source}'; "
                        "export blocked under GDPR Article 25"
                    )
        return Allowed()

    @registry.register_platform("send_money", non_overridable=True)
    def require_trusted_transfer_params(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Both recipient and amount must originate from the user (trusted).

        Regulatory basis: PSD2 strong customer authentication requirement.
        """
        for field_name in ("recipient", "amount"):
            field = kwargs.get(field_name)
            if field is not None and not is_trusted(field):
                return Denied(
                    f"Platform rule: '{field_name}' must come from a trusted source "
                    "(PSD2 compliance — no AI-derived financial instructions)"
                )
        return Allowed()

    @registry.register_platform("send_email", non_overridable=False)
    def prefer_internal_recipients(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Soft rule: warn on external recipients (user may consent).

        non_overridable=False: legitimate cross-org collaboration exists.
        """
        to = kwargs.get("to")
        if to is not None and not is_trusted(to):
            return Denied(
                "Platform guideline: recipient address is not user-supplied; "
                "please verify before sending to an untrusted address"
            )
        return Allowed()

    # ── Tool-Provider (email security tool constraints) ─────────────────────

    @registry.register_tool_provider("send_email")
    def no_bcc_exfiltration(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """BCC must only contain trusted addresses (anti-exfiltration)."""
        bcc = kwargs.get("bcc")
        if bcc is not None and not is_trusted(bcc):
            return Denied(
                "send_email tool: BCC address must come from a trusted source "
                "to prevent silent exfiltration"
            )
        return Allowed()

    # ── User (personal preferences) ─────────────────────────────────────────

    @registry.register_user("send_email")
    def user_approved_contacts(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """User preference: only email pre-approved contacts."""
        approved = {
            "trading-desk@corp.com",
            "risk-team@corp.com",
            "alice@corp.com",
        }
        to = kwargs.get("to")
        if to is not None and str(to.raw) not in approved:
            return Denied(
                f"User preference: '{to.raw}' is not in your approved contacts"
            )
        return Allowed()

    return PolicyConflictResolver(registry)
```

**What this pattern demonstrates:**
- Two `non_overridable=True` Platform policies protect PII and fund-transfer params
  under regulatory mandates — user consent cannot bypass these.
- One `non_overridable=False` Platform policy acts as a soft guardrail — useful when
  the risk is real but legitimate overrides exist.
- A Tool-Provider policy covers the BCC attack surface without duplicating Platform logic.
- A User policy reduces consent-prompt frequency by pre-approving trusted contacts.

---

### Pattern 2: Multi-Tenant SaaS Platform — File System and Messaging

A multi-tenant SaaS deployment where each tenant has an isolated workspace, and the
platform enforces cross-tenant data isolation as an absolute constraint.

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver
from camel.policy import Allowed, Denied, is_trusted, can_readers_read_value
from camel.value import CaMeLValue, Public
from collections.abc import Mapping


def build_saas_tenant_policies(
    tenant_id: str,
    tenant_workspace_prefix: str,  # e.g. "/tenants/acme-corp/"
    tenant_users: frozenset[str],  # authorised tenant member emails
) -> PolicyConflictResolver:
    registry = TieredPolicyRegistry()

    # ── Platform (absolute — cross-tenant isolation) ────────────────────────

    @registry.register_platform("write_file", non_overridable=True)
    def enforce_tenant_workspace_isolation(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Block writes to paths outside this tenant's workspace prefix.

        Prevents cross-tenant data pollution regardless of agent instructions.
        """
        path = kwargs.get("path")
        if path is not None:
            raw_path = str(path.raw)
            if not raw_path.startswith(tenant_workspace_prefix):
                return Denied(
                    f"Platform rule: write path '{raw_path}' is outside "
                    f"tenant workspace '{tenant_workspace_prefix}'. "
                    "Cross-tenant writes are never permitted."
                )
        return Allowed()

    @registry.register_platform("post_message", non_overridable=True)
    def block_cross_tenant_channel_injection(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Block posting to channels not owned by this tenant."""
        channel = kwargs.get("channel")
        if channel is not None and not is_trusted(channel):
            return Denied(
                "Platform rule: channel identifier must come from a trusted source; "
                "injected channel names are never permitted"
            )
        return Allowed()

    # ── Platform (soft — content readers must include tenant members) ────────

    @registry.register_platform("write_file", non_overridable=False)
    def tenant_content_readers(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """Content readers must include all authorised tenant members.

        non_overridable=False: a user with explicit consent may write public
        content (e.g. publishing a release note).
        """
        content = kwargs.get("content")
        if content is not None and content.readers is not Public:
            for member in tenant_users:
                if not can_readers_read_value(content, member):
                    return Denied(
                        f"Platform guideline: tenant member '{member}' cannot read "
                        "the file content — data would be inaccessible to the team"
                    )
        return Allowed()

    # ── Tool-Provider (write_file tool path validation) ─────────────────────

    @registry.register_tool_provider("write_file")
    def no_path_traversal(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """write_file tool: block path traversal sequences."""
        path = kwargs.get("path")
        if path is not None and ".." in str(path.raw):
            return Denied(
                "write_file tool: path must not contain '..' traversal sequences"
            )
        return Allowed()

    # ── User (developer self-restriction to personal workspace dir) ──────────

    personal_dir = f"{tenant_workspace_prefix}personal/{tenant_id}/"

    @registry.register_user("write_file")
    def restrict_to_personal_dir(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        """User preference: only write files inside my personal directory."""
        path = kwargs.get("path")
        if path is not None and not str(path.raw).startswith(personal_dir):
            return Denied(
                f"User preference: writes are restricted to '{personal_dir}'; "
                "use a different session to write to shared workspace dirs"
            )
        return Allowed()

    return PolicyConflictResolver(registry)
```

**What this pattern demonstrates:**
- `non_overridable=True` Platform policies enforce cross-tenant isolation as a hard
  invariant — no injection attack or user approval can bypass them.
- A `non_overridable=False` Platform policy enforces a softer data-access requirement
  that an operator can still override for legitimate exceptions (e.g., public posts).
- Tool-Provider policy addresses the tool's own attack surface (path traversal).
- User policy applies a personal-workspace restriction that the user may lift by
  approving the consent prompt when intentionally writing to a shared directory.

---

## Compliance Query Patterns

```python
# All non-overridable denials (regulatory incidents requiring investigation)
incidents = [
    e for e in audit_log
    if e.non_overridable_denial
]

# All user consent overrides of platform soft-denials (audit-worthy)
overrides = [
    e for e in audit_log
    if e.authoritative_tier == "Platform"
    and e.consent_decision == "UserApproved"
]

# All tool-provider denials (tool-specific security violations)
tp_denials = [
    e for e in audit_log
    if e.authoritative_tier == "ToolProvider"
    and e.outcome == "Denied"
]

# User-tier denials overridden by consent (user changed their mind)
user_overrides = [
    e for e in audit_log
    if e.authoritative_tier == "User"
    and e.consent_decision == "UserApproved"
]
```

---

## Migration Path from Flat `PolicyRegistry`

### Step 1 — No code changes needed yet

The existing `PolicyRegistry` and `CaMeLInterpreter(policy_engine=...)` API is
unchanged.  Your existing policies continue to work.

### Step 2 — Opt in to tiered governance for new policies

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
# Note: do NOT pass both policy_engine and conflict_resolver.
```

### Step 3 — Migrate existing policies to the appropriate tier

Walk through each existing policy and assign it to Platform, Tool-Provider, or User
based on the guidance in "When to Use Each Tier" above.

### Step 4 — Remove the flat registry

Once all policies are migrated, remove the `policy_engine` parameter.

---

## Environment-Based Deployment Configuration

Use the `CAMEL_TIERED_POLICY_MODULE` environment variable to load policies at startup
without modifying core code:

```bash
export CAMEL_TIERED_POLICY_MODULE=myapp.security.tiered_policies
```

The module must export a `configure_tiered_policies` callable:

```python
# myapp/security/tiered_policies.py
from camel.policy.governance import TieredPolicyRegistry

def configure_tiered_policies(registry: TieredPolicyRegistry) -> None:
    registry.register_platform("send_email", no_external_recipients,
                                non_overridable=True)
    registry.register_tool_provider("send_email", attachment_size_limit)
    registry.register_user("send_email", user_team_only)
```

Load at startup:

```python
resolver = PolicyConflictResolver.load_from_env()
```

---

## Reference

- [ADR-011: Three-Tier Policy Governance](adr/011-three-tier-policy-governance.md)
- [ADR-009: Policy Engine Architecture](adr/009-policy-engine-architecture.md)
- [ADR-010: Enforcement Hook, Consent Flow, Audit Log](adr/010-enforcement-hook-consent-audit-harness.md)
- [Reference Policy Library Specification](policies/reference-policy-spec.md)
- [Architecture Reference §11 — Policy Engine](architecture.md#11-policy-engine)
- [Architecture Reference §10.7 — Open Questions (FW-5 Resolved)](architecture.md#107-open-questions--future-work)
