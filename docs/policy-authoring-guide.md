# CaMeL Policy Authoring Guide

_CaMeL v0.6.0 (camel-security) — Three-tier model, test harness, and simulator walkthrough_

This guide covers everything you need to write, test, and debug CaMeL security policies:

1. **Policy basics** — function signature, `Allowed`/`Denied`, helper functions
2. **Three-tier governance model** — Platform, Tool-Provider, User tiers and conflict resolution
3. **Test harness** — `CaMeLValueBuilder`, `PolicyTestRunner`, and `PolicyTestReport`
4. **Policy simulator** — `PolicySimulator` dry-run walkthrough
5. **Enterprise deployment patterns** — worked examples for regulated and multi-tenant
   environments
6. **Migration path** — moving from flat `PolicyRegistry` to `TieredPolicyRegistry`

---

## 1. Policy Basics

### 1.1 Policy Function Signature

A CaMeL security policy is a plain Python callable:

```python
(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult
```

It returns `Allowed()` to permit the call, or `Denied(reason)` to block it:

```python
from collections.abc import Mapping
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult
from camel.value import CaMeLValue


def my_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    # Access arguments
    recipient = kwargs.get("to")
    if recipient is None:
        return Denied("Missing 'to' argument")
    # ... logic ...
    return Allowed()
```

### 1.2 Helper Functions

| Function | Import | Description |
|---|---|---|
| `is_trusted(value)` | `camel.policy.interfaces` | Returns `True` if the value's entire dependency graph originates from `"User literal"` or `"CaMeL"` only |
| `can_readers_read_value(value, reader)` | `camel.policy.interfaces` | Returns `True` if the string `reader` is an authorised reader of `value` |
| `get_all_sources(value)` | `camel.policy.interfaces` | Returns the `frozenset` of all sources (direct and transitive) for `value` |

```python
from camel.policy.interfaces import is_trusted, can_readers_read_value, get_all_sources

# True only if value came from user input or CaMeL transformation
if not is_trusted(kwargs["to"]):
    return Denied("Recipient must come from a trusted source")

# True if the string in kwargs["to"].raw is a declared reader of the body value
if not can_readers_read_value(kwargs["body"], kwargs["to"].raw):
    return Denied("Recipient is not authorised to read the body")

# The full set of tool sources (e.g. {"read_email", "get_calendar"})
sources = get_all_sources(kwargs["content"])
if "read_email" in sources:
    return Denied("Content originates from an untrusted email source")
```

---

## 2. Three-Tier Governance Model

CaMeL's three-tier model distributes policy authority across stakeholders:

| Tier | Author | Authority | `non_overridable` allowed? |
|---|---|---|---|
| **Platform** | Deployment operator | Highest — sets the security floor | Yes |
| **Tool-Provider** | Tool / plugin author | Middle — constrains its own tool | No |
| **User** | End user | Lowest — personalises within guardrails | No |

Policies are evaluated in **Platform → Tool-Provider → User** order.  The first
`Denied` from any tier halts evaluation.

### 2.1 `non_overridable` Flag

A Platform policy may set `non_overridable=True` to prevent user consent from
overriding a denial:

```
Platform Denied + non_overridable=True
  → Evaluation stops immediately.
  → Consent callback is NOT invoked.
  → MergedPolicyResult.non_overridable_denial = True

Platform Denied + non_overridable=False
  → Evaluation stops.
  → Consent callback IS available in PRODUCTION mode.
```

Apply `non_overridable=True` when all three of the following hold:

1. The rule is mandated by an external authority (law, regulation, board policy).
2. A successful bypass would be a reportable compliance incident.
3. No legitimate user business case exists for an exception.

### 2.2 Building a Tiered Registry

```python
from camel.policy.governance import (
    TieredPolicyRegistry,
    PolicyConflictResolver,
    PolicyTier,
)
from camel.policy.interfaces import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping

registry = TieredPolicyRegistry()


# ── Platform (absolute — no external email ever) ──────────────────────────────
def no_external_recipients(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    to = kwargs.get("to")
    if to is not None and "@external.example.com" in str(to.raw):
        return Denied("Platform rule: external recipients are not allowed")
    return Allowed()

registry.register_platform("send_email", no_external_recipients, non_overridable=True)


# ── Tool-Provider (attachment size constraint) ────────────────────────────────
def attachment_size_limit(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    attachment = kwargs.get("attachment")
    if attachment is not None and len(str(attachment.raw)) > 100_000:
        return Denied("send_email tool: attachment exceeds 100 KB limit")
    return Allowed()

registry.register_tool_provider("send_email", attachment_size_limit)


# ── User (personal preference — team-only) ────────────────────────────────────
def user_team_only(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    to = kwargs.get("to")
    if to is not None and str(to.raw) not in {"alice@corp.com", "bob@corp.com"}:
        return Denied("User preference: only send to my team members")
    return Allowed()

registry.register_user("send_email", user_team_only)

resolver = PolicyConflictResolver(registry)
```

### 2.3 Wiring into CaMeLAgent

`CaMeLAgent.policies` accepts a flat `PolicyRegistry`.  Wrap the
`PolicyConflictResolver` in a thin adapter registered on a flat registry:

```python
from camel_security import CaMeLAgent
from camel.policy.interfaces import PolicyRegistry

flat_registry = PolicyRegistry()

def _tiered_send_email(tool_name, kwargs):
    return resolver.evaluate(tool_name, kwargs).outcome

flat_registry.register("send_email", _tiered_send_email)

agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[send_email_tool],
    policies=flat_registry,
)
```

### 2.4 Inspecting the Audit Trail

After a policy evaluation, inspect `MergedPolicyResult.audit_trail`:

```python
merged = resolver.evaluate("send_email", kwargs)
print(f"Outcome: {merged.outcome}")
print(f"Authoritative tier: {merged.authoritative_tier}")
print(f"Non-overridable denial: {merged.non_overridable_denial}")
for record in merged.audit_trail:
    status = "AUTHORITATIVE" if record.authoritative else "passed"
    print(f"  [{record.tier.value}] {record.policy_name}: {record.result} ({status})")
```

### 2.5 Conflict Resolution Algorithm

```
function evaluate(tool_name, kwargs):
    for entry in platform_entries(tool_name):         # Phase 1
        result = entry.policy_fn(tool_name, kwargs)
        if result.is_denied:
            return MergedPolicyResult(
                outcome=result, authoritative_tier=PLATFORM,
                non_overridable_denial=entry.non_overridable,
                audit_trail=...
            )

    for entry in tool_provider_entries(tool_name):    # Phase 2
        result = entry.policy_fn(tool_name, kwargs)
        if result.is_denied:
            return MergedPolicyResult(
                outcome=result, authoritative_tier=TOOL_PROVIDER,
                non_overridable_denial=False, audit_trail=...
            )

    for entry in user_entries(tool_name):             # Phase 3
        result = entry.policy_fn(tool_name, kwargs)
        if result.is_denied:
            return MergedPolicyResult(
                outcome=result, authoritative_tier=USER,
                non_overridable_denial=False, audit_trail=...
            )

    return MergedPolicyResult(                        # Phase 4 — all allowed
        outcome=Allowed(), authoritative_tier=None,
        non_overridable_denial=False, audit_trail=...
    )
```

---

## 3. Test Harness: CaMeLValueBuilder & PolicyTestRunner

The test harness lets you unit-test policies without a live interpreter.
All imports come from `camel_security.testing`.

### 3.1 Building Test Fixtures with `CaMeLValueBuilder`

```python
from camel_security.testing import CaMeLValueBuilder
from camel.value import Public

# Trusted value (typed by the user)
trusted_email = (
    CaMeLValueBuilder("alice@example.com")
    .with_sources("User literal")
    .build()
)

# Untrusted value (from a tool)
injected_email = (
    CaMeLValueBuilder("evil@attacker.com")
    .with_sources("read_email")
    .with_inner_source("body")
    .build()
)

# Restricted readers
private_doc = (
    CaMeLValueBuilder("Q4 financials — confidential")
    .with_sources("read_document")
    .with_readers(frozenset({"alice@company.com", "bob@company.com"}))
    .build()
)

# Derived value (inherits upstream capabilities)
upstream = CaMeLValueBuilder("data").with_sources("external_tool").build()
derived = CaMeLValueBuilder("result").with_sources("CaMeL").with_dependency(upstream).build()
assert "external_tool" in derived.sources  # capability union
```

### 3.2 Writing Policy Tests with `PolicyTestRunner`

```python
from camel_security.testing import CaMeLValueBuilder, PolicyTestCase, PolicyTestRunner
from camel.policy.interfaces import Allowed, Denied
from camel.value import CaMeLValue
from collections.abc import Mapping


def send_email_policy(
    tool_name: str, kwargs: Mapping[str, CaMeLValue]
) -> Allowed | Denied:
    from camel.policy.interfaces import is_trusted, can_readers_read_value
    recipient = kwargs.get("to")
    if recipient is None:
        return Denied("Missing 'to' argument")
    if is_trusted(recipient):
        return Allowed()
    body = kwargs.get("body")
    if body is not None and not can_readers_read_value(body, recipient.raw):
        return Denied("Recipient is untrusted and not an authorised reader of the body")
    return Allowed()


runner = PolicyTestRunner()

report = runner.run(
    send_email_policy,
    [
        # Allowed: trusted recipient
        PolicyTestCase(
            case_id="trusted_recipient",
            tool_name="send_email",
            kwargs={
                "to": CaMeLValueBuilder("alice@example.com").with_sources("User literal").build(),
                "body": CaMeLValueBuilder("Hello Alice!").with_sources("User literal").build(),
            },
            expected_outcome="Allowed",
        ),
        # Denied: injected recipient
        PolicyTestCase(
            case_id="injected_recipient",
            tool_name="send_email",
            kwargs={
                "to": CaMeLValueBuilder("evil@attacker.com")
                    .with_sources("read_email").with_inner_source("body").build(),
                "body": CaMeLValueBuilder("Secret data").with_sources("User literal").build(),
            },
            expected_outcome="Denied",
            expected_reason_contains="untrusted",
        ),
    ],
)

assert report.passed == 2
assert report.failed == 0
print(f"Denied-path coverage: {report.coverage_percent}%")
```

### 3.3 `PolicyTestReport` Field Reference

| Field | Type | Meaning |
|---|---|---|
| `total_cases` | `int` | Total test cases evaluated |
| `passed` | `int` | Cases where actual outcome == expected |
| `failed` | `int` | Cases where actual ≠ expected, or reason substring not found |
| `denied_cases` | `int` | Cases that returned `Denied` (pass or fail) |
| `allowed_cases` | `int` | Cases that returned `Allowed` (pass or fail) |
| `coverage_percent` | `float` | `denied_cases / total_cases * 100` |
| `results` | `list[PolicyCaseResult]` | Per-case result objects |

Each `PolicyCaseResult`:

| Field | Type | Meaning |
|---|---|---|
| `case_id` | `str` | Case identifier |
| `passed` | `bool` | Whether the case passed |
| `actual_outcome` | `str` | `"Allowed"` or `"Denied"` |
| `actual_reason` | `str \| None` | Denial reason, or `None` |
| `failure_message` | `str \| None` | Why the case failed, or `None` |

### 3.4 Running Tests Inside pytest

```python
# tests/test_my_policies.py
from camel_security.testing import CaMeLValueBuilder, PolicyTestCase, PolicyTestRunner


def test_send_email_allows_trusted():
    from camel.policy.reference_policies import send_email_policy

    runner = PolicyTestRunner()
    report = runner.run(
        send_email_policy,
        [
            PolicyTestCase(
                tool_name="send_email",
                kwargs={
                    "to": CaMeLValueBuilder("bob@example.com")
                        .with_sources("User literal").build(),
                    "body": CaMeLValueBuilder("Hey Bob!")
                        .with_sources("User literal").build(),
                },
                expected_outcome="Allowed",
            )
        ],
    )
    assert report.passed == 1


def test_send_email_denies_injected():
    from camel.policy.reference_policies import send_email_policy

    runner = PolicyTestRunner()
    report = runner.run(
        send_email_policy,
        [
            PolicyTestCase(
                tool_name="send_email",
                kwargs={
                    "to": CaMeLValueBuilder("evil@attacker.com")
                        .with_sources("read_email").build(),
                    "body": CaMeLValueBuilder("secret").with_sources("User literal").build(),
                },
                expected_outcome="Denied",
                expected_reason_contains="untrusted",
            )
        ],
    )
    assert report.passed == 1
```

Run with: `pytest tests/test_my_policies.py -v`

---

## 4. Policy Simulator Walkthrough

`PolicySimulator` runs a pseudo-Python execution plan through the CaMeL interpreter
**without executing any real tool side-effects**.  All tools are replaced by no-op stubs
that return placeholder `CaMeLValue` instances.  Policy evaluations are captured in a
`SimulationReport`.

### 4.1 Basic Setup

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
from camel_security.testing import PolicySimulator, CaMeLValueBuilder

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")

simulator = PolicySimulator()
```

### 4.2 Simulating an Allowed Plan

```python
report = simulator.simulate(
    plan="result = send_email(to=_to, subject=_subj, body=_body)",
    tools=["send_email"],
    policies=registry,
    preset_vars={
        "_to": CaMeLValueBuilder("alice@example.com").with_sources("User literal").build(),
        "_subj": CaMeLValueBuilder("Weekly update").with_sources("User literal").build(),
        "_body": CaMeLValueBuilder("Here is the update.").with_sources("User literal").build(),
    },
)

assert report.allowed_tools == ["send_email"]
assert report.denied_tools == []
print("Policy simulation: all calls allowed.")
```

### 4.3 Simulating a Denied Plan

```python
report = simulator.simulate(
    plan="result = write_file(path=_injected_path, content=_body)",
    tools=["write_file"],
    policies=registry,
    preset_vars={
        "_injected_path": (
            CaMeLValueBuilder("/etc/cron.d/malware")
            .with_sources("read_email").with_inner_source("body").build()
        ),
        "_body": CaMeLValueBuilder("payload").with_sources("read_email").build(),
    },
)

assert "write_file" in report.denied_tools
for ev in report.evaluations:
    print(f"Tool: {ev.tool_name}  Outcome: {type(ev.result).__name__}")
    if ev.reason:
        print(f"  Reason: {ev.reason}")
```

### 4.4 `SimulationReport` Field Reference

| Field | Type | Meaning |
|---|---|---|
| `evaluations` | `list[SimulatedPolicyEvaluation]` | All policy evaluations during the dry run |
| `denied_tools` | `list[str]` | Tool names whose policy returned `Denied` |
| `allowed_tools` | `list[str]` | Tool names whose policy returned `Allowed` |

Each `SimulatedPolicyEvaluation`:

| Field | Type | Meaning |
|---|---|---|
| `tool_name` | `str` | Name of the evaluated tool |
| `args_snapshot` | `dict[str, Any]` | Snapshot of raw argument values |
| `result` | `SecurityPolicyResult` | `Allowed()` or `Denied(reason)` |
| `reason` | `str \| None` | Denial reason, or `None` if allowed |

---

## 5. Enterprise Deployment Patterns

### Pattern 1: Regulated Financial Services

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver
from camel.policy.interfaces import Allowed, Denied, is_trusted, get_all_sources
from camel.value import CaMeLValue
from collections.abc import Mapping


def build_financial_services_policies() -> PolicyConflictResolver:
    registry = TieredPolicyRegistry()

    # Platform — no PII in outbound email (GDPR Article 25, non-overridable)
    def no_pii_to_unclassified(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        body = kwargs.get("body")
        if body is not None:
            for source in get_all_sources(body):
                if source.startswith("pii:"):
                    return Denied(
                        f"Platform: body contains PII from '{source}'; blocked under GDPR Article 25"
                    )
        return Allowed()

    registry.register_platform("send_email", no_pii_to_unclassified, non_overridable=True)

    # Platform — both recipient and amount must be user-trusted (PSD2)
    def require_trusted_transfer_params(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        for field_name in ("recipient", "amount"):
            field = kwargs.get(field_name)
            if field is not None and not is_trusted(field):
                return Denied(
                    f"Platform: '{field_name}' must come from a trusted source (PSD2 compliance)"
                )
        return Allowed()

    registry.register_platform("send_money", require_trusted_transfer_params, non_overridable=True)

    # Tool-Provider — no BCC exfiltration
    def no_bcc_exfiltration(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        bcc = kwargs.get("bcc")
        if bcc is not None and not is_trusted(bcc):
            return Denied("send_email tool: BCC address must come from a trusted source")
        return Allowed()

    registry.register_tool_provider("send_email", no_bcc_exfiltration)

    # User — pre-approved contacts
    def user_approved_contacts(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        approved = {"trading-desk@corp.com", "risk-team@corp.com", "alice@corp.com"}
        to = kwargs.get("to")
        if to is not None and str(to.raw) not in approved:
            return Denied(f"User preference: '{to.raw}' is not in your approved contacts")
        return Allowed()

    registry.register_user("send_email", user_approved_contacts)

    return PolicyConflictResolver(registry)
```

### Pattern 2: Multi-Tenant SaaS Platform

```python
from camel.policy.governance import TieredPolicyRegistry, PolicyConflictResolver
from camel.policy.interfaces import Allowed, Denied, is_trusted, can_readers_read_value
from camel.value import CaMeLValue, Public
from collections.abc import Mapping


def build_saas_tenant_policies(
    tenant_workspace_prefix: str,
    tenant_users: frozenset[str],
) -> PolicyConflictResolver:
    registry = TieredPolicyRegistry()

    # Platform — cross-tenant write isolation (non-overridable)
    def enforce_tenant_workspace_isolation(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        path = kwargs.get("path")
        if path is not None:
            raw_path = str(path.raw)
            if not raw_path.startswith(tenant_workspace_prefix):
                return Denied(
                    f"Platform: write path '{raw_path}' is outside "
                    f"tenant workspace '{tenant_workspace_prefix}'"
                )
        return Allowed()

    registry.register_platform("write_file", enforce_tenant_workspace_isolation, non_overridable=True)

    # Tool-Provider — block path traversal sequences
    def no_path_traversal(
        tool_name: str, kwargs: Mapping[str, CaMeLValue]
    ) -> Allowed | Denied:
        path = kwargs.get("path")
        if path is not None and ".." in str(path.raw):
            return Denied("write_file tool: path must not contain '..' traversal sequences")
        return Allowed()

    registry.register_tool_provider("write_file", no_path_traversal)

    return PolicyConflictResolver(registry)
```

---

## 6. Migration Path from Flat `PolicyRegistry`

If you have existing flat `PolicyRegistry` policies from M4, migrate incrementally:

**Step 1 — No code changes required yet.** Flat `PolicyRegistry` still works.

**Step 2 — Opt in to tiered governance for new policies:**

```python
# Before (flat):
registry = PolicyRegistry()
registry.register("send_email", email_policy)
agent = CaMeLAgent(..., policies=registry)

# After (tiered, additive):
tiered = TieredPolicyRegistry()
tiered.register_platform("send_email", email_policy, non_overridable=True)
resolver = PolicyConflictResolver(tiered)
agent = CaMeLAgent(..., policies=resolver)
```

**Step 3 — Assign existing policies to the appropriate tier:**

| Old policy intent | Target tier |
|---|---|
| Regulatory / compliance rules | Platform, `non_overridable=True` |
| Organisation-wide security floor | Platform, `non_overridable=True` or `False` |
| Tool-specific security constraints | Tool-Provider |
| User preferences / defaults | User |

**Step 4 — Remove the flat registry** once all policies are migrated.

---

## 7. Reference Import Table

| Symbol | Import path |
|---|---|
| `CaMeLValueBuilder` | `camel_security.testing` |
| `PolicyTestCase` | `camel_security.testing` |
| `PolicyTestRunner` | `camel_security.testing` |
| `PolicyTestReport` | `camel_security.testing` |
| `PolicySimulator` | `camel_security.testing` |
| `SimulationReport` | `camel_security.testing` |
| `Allowed` / `Denied` | `camel.policy.interfaces` |
| `is_trusted` | `camel.policy.interfaces` |
| `can_readers_read_value` | `camel.policy.interfaces` |
| `get_all_sources` | `camel.policy.interfaces` |
| `PolicyRegistry` | `camel.policy` |
| `configure_reference_policies` | `camel.policy.reference_policies` |
| `TieredPolicyRegistry` | `camel.policy.governance` |
| `PolicyConflictResolver` | `camel.policy.governance` |
| `PolicyTier` | `camel.policy.governance` |
| `MergedPolicyResult` | `camel.policy.governance` |
| `Public` | `camel.value` |

---

## See Also

- [Policy Authoring Tutorial](policy-authoring-tutorial.md) — full step-by-step tutorial with detailed worked examples for `send_email` and `write_file`, including `PolicyTestRunner` output interpretation
- [Reference Policy Specification](policies/reference-policy-spec.md) — authoritative spec for the six built-in policies
- [Three-Tier Policy Authorship Guide](policies/three-tier-policy-authorship-guide.md) — additional enterprise patterns (in `docs/policies/`; distinct from the deprecated `policy_authorship_guide.md` in `docs/`)
- [Consent Handler Integration Guide](consent-handler-integration.md) — customising the user consent UX
- [Security Audit Log Reference](security-audit-log.md) — audit log schema
- [Architecture Reference §11](architecture.md#11-policy-engine) — Policy Engine design
- [Tool Onboarding Guide](tool-onboarding.md) — attaching per-tool policies to a `Tool` registration
- [Policy Authorship Guide (deprecated)](policy_authorship_guide.md) — retained for additional detail on compliance query patterns and `CAMEL_TIERED_POLICY_MODULE` environment-based deployment configuration
