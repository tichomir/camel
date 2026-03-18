# Policy Authoring Tutorial

_CaMeL v0.6.0 | Last updated: 2026-03-18_

This tutorial walks you through authoring and validating CaMeL security
policies using the developer tooling in `camel_security.testing`.  By the end
you will know how to:

1. Construct `CaMeLValue` test fixtures with `CaMeLValueBuilder`.
2. Write a policy and verify it with `PolicyTestRunner`.
3. Dry-run a full execution plan with `PolicySimulator`.
4. Interpret a `PolicyTestReport` to measure policy coverage.

All imports in this tutorial come from the public `camel_security` namespace —
no internal `camel.*` imports are required for test code.

---

## Prerequisites

```bash
pip install camel-security
```

Run the worked examples directly with Python 3.11+ or inside `pytest`.

---

## 1. Building `CaMeLValue` Test Fixtures

Every argument passed to a CaMeL tool is a `CaMeLValue` — a Python value
wrapped with provenance metadata (`sources`, `readers`, `inner_source`).
`CaMeLValueBuilder` constructs these fixtures without a live interpreter.

### 1.1 Trusted value (user literal)

```python
from camel_security.testing import CaMeLValueBuilder

# A value typed directly by the user in their query
trusted_email = (
    CaMeLValueBuilder("alice@example.com")
    .with_sources("User literal")
    .build()
)

assert trusted_email.sources == frozenset({"User literal"})
print(trusted_email.raw)  # alice@example.com
```

### 1.2 Untrusted value (from a tool)

```python
from camel_security.testing import CaMeLValueBuilder

# A value returned by an email tool — untrusted provenance
injected_email = (
    CaMeLValueBuilder("evil@attacker.com")
    .with_sources("read_email")
    .with_inner_source("body")
    .build()
)

assert "read_email" in injected_email.sources
assert injected_email.inner_source == "body"
```

### 1.3 Restricted readers

When data should only be readable by specific principals, set `readers`:

```python
from camel_security.testing import CaMeLValueBuilder

private_doc = (
    CaMeLValueBuilder("Q4 financials — confidential")
    .with_sources("read_document")
    .with_readers(frozenset({"alice@company.com", "bob@company.com"}))
    .build()
)

assert "alice@company.com" in private_doc.readers
```

### 1.4 Value with Public readers

```python
from camel_security.testing import CaMeLValueBuilder
from camel.value import Public

public_val = (
    CaMeLValueBuilder("public announcement")
    .with_sources("User literal")
    .with_readers(Public)
    .build()
)

from camel.value import _PublicType
assert isinstance(public_val.readers, _PublicType)
```

### 1.5 Derived value (dependency chain)

When a value is computed from another, capabilities propagate via union:

```python
from camel_security.testing import CaMeLValueBuilder

upstream = (
    CaMeLValueBuilder("upstream data")
    .with_sources("external_tool")
    .with_readers(frozenset({"alice@example.com"}))
    .build()
)

derived = (
    CaMeLValueBuilder("derived")
    .with_sources("CaMeL")
    .with_dependency(upstream)
    .build()
)

# The derived value inherits upstream's sources and readers
assert "external_tool" in derived.sources
assert "alice@example.com" in derived.readers
```

---

## 2. Writing a `send_email` Policy

### 2.1 The policy function

A CaMeL policy is a plain Python function with the signature:

```python
(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult
```

It returns `Allowed()` to permit the call or `Denied(reason)` to block it.
Here is a minimal `send_email` policy:

```python
from collections.abc import Mapping
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult
from camel.policy.interfaces import is_trusted, can_readers_read_value
from camel.value import CaMeLValue


def send_email_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Block emails to untrusted recipient addresses.

    Allows the call only when the recipient's email address is:
    (a) sourced exclusively from the user's literal input, or
    (b) already an authorised reader of all data being sent.
    """
    recipient = kwargs.get("to")
    if recipient is None:
        return Denied("Missing 'to' argument")

    # Path (a): recipient address was typed directly by the user
    if is_trusted(recipient):
        return Allowed()

    # Path (b): recipient is already a known reader of the body content
    body = kwargs.get("body")
    if body is not None and not can_readers_read_value(recipient, body):
        return Denied(
            "Recipient address originates from untrusted data and is not an "
            "authorised reader of the email body"
        )

    return Allowed()
```

### 2.2 Testing with `PolicyTestRunner`

```python
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicyTestCase,
    PolicyTestRunner,
)

runner = PolicyTestRunner()

report = runner.run(
    send_email_policy,
    [
        # ── Allowed cases ─────────────────────────────────────────────────
        PolicyTestCase(
            case_id="trusted_recipient",
            tool_name="send_email",
            kwargs={
                "to": (
                    CaMeLValueBuilder("alice@example.com")
                    .with_sources("User literal")
                    .build()
                ),
                "body": (
                    CaMeLValueBuilder("Hello Alice!")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Allowed",
        ),
        # ── Denied cases ──────────────────────────────────────────────────
        PolicyTestCase(
            case_id="injected_recipient",
            tool_name="send_email",
            kwargs={
                "to": (
                    CaMeLValueBuilder("evil@attacker.com")
                    .with_sources("read_email")   # untrusted tool output
                    .with_inner_source("body")
                    .build()
                ),
                "body": (
                    CaMeLValueBuilder("Confidential report")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Denied",
            expected_reason_contains="untrusted",
        ),
    ],
)

assert report.passed == 2, report.results
assert report.failed == 0
```

### 2.3 Interpreting `PolicyTestReport`

```python
print(f"Total: {report.total_cases}")
print(f"Passed: {report.passed}  Failed: {report.failed}")
print(f"Denied-path coverage: {report.coverage_percent}%")

for result in report.results:
    status = "PASS" if result.passed else "FAIL"
    print(f"  [{status}] {result.case_id}: {result.actual_outcome}", end="")
    if result.actual_reason:
        print(f" — {result.actual_reason}", end="")
    if result.failure_message:
        print(f"\n         ↳ {result.failure_message}", end="")
    print()
```

Sample output:

```
Total: 2
Passed: 2  Failed: 0
Denied-path coverage: 50.0%

  [PASS] trusted_recipient: Allowed
  [PASS] injected_recipient: Denied — Recipient address originates from untrusted data ...
```

**Field reference:**

| Field | Type | Meaning |
|---|---|---|
| `total_cases` | `int` | Total test cases evaluated |
| `passed` | `int` | Cases where actual == expected |
| `failed` | `int` | Cases where actual ≠ expected, or reason substring not found |
| `denied_cases` | `int` | Cases that actually returned `Denied` (pass or fail) |
| `allowed_cases` | `int` | Cases that actually returned `Allowed` (pass or fail) |
| `coverage_percent` | `float` | `denied_cases / total_cases * 100` — measures negative-path coverage |
| `results` | `list[PolicyCaseResult]` | Per-case result objects |

---

## 3. Writing a `write_file` Policy

### 3.1 The policy function

```python
from collections.abc import Mapping
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult, is_trusted
from camel.policy.interfaces import can_readers_read_value
from camel.value import CaMeLValue


def make_write_file_policy(owner: str):
    """Return a write_file policy that protects *owner*'s files.

    The policy allows writes only when:
    - The file path is user-literal (no path injection), and
    - The file content's readers include the owner (no exfiltration).
    """

    def write_file_policy(
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        path = kwargs.get("path")
        content = kwargs.get("content")

        if path is None:
            return Denied("Missing 'path' argument")

        # Guard 1: path must come from the user directly
        if not is_trusted(path):
            return Denied(
                "File path originates from untrusted data — possible path injection"
            )

        # Guard 2: content readers must include the file owner
        if content is not None:
            owner_cv = CaMeLValueBuilder(owner).with_sources("User literal").build()
            if not can_readers_read_value(owner_cv, content):
                return Denied(
                    f"File content readers do not include the owner ({owner!r})"
                )

        return Allowed()

    return write_file_policy
```

### 3.2 Testing with `PolicyTestRunner`

```python
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicyTestCase,
    PolicyTestRunner,
)

write_file_policy = make_write_file_policy("alice@example.com")

runner = PolicyTestRunner()
report = runner.run(
    write_file_policy,
    [
        # Allowed: path is trusted, content has public readers
        PolicyTestCase(
            case_id="user_path_public_content",
            tool_name="write_file",
            kwargs={
                "path": (
                    CaMeLValueBuilder("/home/alice/report.txt")
                    .with_sources("User literal")
                    .build()
                ),
                "content": (
                    CaMeLValueBuilder("Monthly summary")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Allowed",
        ),
        # Denied: path comes from an untrusted tool
        PolicyTestCase(
            case_id="injected_path",
            tool_name="write_file",
            kwargs={
                "path": (
                    CaMeLValueBuilder("/etc/cron.d/malware")
                    .with_sources("read_email")   # injected
                    .with_inner_source("body")
                    .build()
                ),
                "content": (
                    CaMeLValueBuilder("bad payload")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Denied",
            expected_reason_contains="path injection",
        ),
        # Denied: content is restricted to a set that excludes the owner
        PolicyTestCase(
            case_id="content_excludes_owner",
            tool_name="write_file",
            kwargs={
                "path": (
                    CaMeLValueBuilder("/home/alice/out.txt")
                    .with_sources("User literal")
                    .build()
                ),
                "content": (
                    CaMeLValueBuilder("restricted document")
                    .with_sources("read_document")
                    .with_readers(frozenset({"eve@example.com"}))  # excludes alice
                    .build()
                ),
            },
            expected_outcome="Denied",
            expected_reason_contains="owner",
        ),
    ],
)

assert report.passed == 3
assert report.failed == 0
print(f"Denied-path coverage: {report.coverage_percent}%")  # 66.67%
```

---

## 4. Dry-Running with `PolicySimulator`

`PolicySimulator` runs a pseudo-Python code plan through the CaMeL interpreter
**without executing any real tool side-effects**.  All tools are replaced by
no-op stubs that return placeholder `CaMeLValue` instances.  Policy evaluations
are captured and returned in a `SimulationReport`.

### 4.1 Setting up the simulator

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicySimulator,
)

# Build a registry with the reference policies
registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")

simulator = PolicySimulator()
```

### 4.2 Simulating a `send_email` plan

```python
plan = """
result = send_email(to=_to, subject=_subj, body=_body)
"""

report = simulator.simulate(
    plan=plan,
    tools=["send_email"],
    policies=registry,
    preset_vars={
        "_to": (
            CaMeLValueBuilder("alice@example.com")
            .with_sources("User literal")
            .build()
        ),
        "_subj": (
            CaMeLValueBuilder("Weekly update")
            .with_sources("User literal")
            .build()
        ),
        "_body": (
            CaMeLValueBuilder("Here is the update.")
            .with_sources("User literal")
            .build()
        ),
    },
)

# The policy should allow this call
assert len(report.evaluations) == 1
assert report.evaluations[0].tool_name == "send_email"
assert report.allowed_tools == ["send_email"]
assert report.denied_tools == []
```

### 4.3 Simulating a plan that should be denied

```python
plan = """
result = write_file(path=_injected_path, content=_body)
"""

report = simulator.simulate(
    plan=plan,
    tools=["write_file"],
    policies=registry,
    preset_vars={
        "_injected_path": (
            CaMeLValueBuilder("/etc/cron.d/malware")
            .with_sources("read_email")
            .with_inner_source("body")
            .build()
        ),
        "_body": (
            CaMeLValueBuilder("injected payload")
            .with_sources("read_email")
            .build()
        ),
    },
)

# The policy should deny this call
assert "write_file" in report.denied_tools
for ev in report.evaluations:
    print(f"Tool: {ev.tool_name}  Outcome: {type(ev.result).__name__}")
    if ev.reason:
        print(f"  Reason: {ev.reason}")
```

### 4.4 `SimulationReport` field reference

| Field | Type | Meaning |
|---|---|---|
| `evaluations` | `list[SimulatedPolicyEvaluation]` | All policy evaluations recorded during the dry run |
| `denied_tools` | `list[str]` | Tool names whose policy returned `Denied` |
| `allowed_tools` | `list[str]` | Tool names whose policy returned `Allowed` |

Each `SimulatedPolicyEvaluation` has:

| Field | Type | Meaning |
|---|---|---|
| `tool_name` | `str` | Name of the tool evaluated |
| `args_snapshot` | `dict[str, Any]` | Snapshot of raw argument values (may be empty for stub calls) |
| `result` | `SecurityPolicyResult` | `Allowed()` or `Denied(reason)` |
| `reason` | `str \| None` | Denial reason, or `None` if allowed |

---

## 5. End-to-End Worked Example: `send_email`

This section combines all three tools into a single end-to-end test:

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicyTestCase,
    PolicyTestRunner,
    PolicySimulator,
)
from camel.policy.reference_policies import send_email_policy as _default_send_email

# 1. Test the policy in isolation
runner = PolicyTestRunner()
report = runner.run(
    _default_send_email,
    [
        PolicyTestCase(
            case_id="allowed_user_literal",
            tool_name="send_email",
            kwargs={
                "to": CaMeLValueBuilder("alice@example.com")
                    .with_sources("User literal").build(),
                "body": CaMeLValueBuilder("Hi Alice")
                    .with_sources("User literal").build(),
            },
            expected_outcome="Allowed",
        ),
        PolicyTestCase(
            case_id="denied_injected_recipient",
            tool_name="send_email",
            kwargs={
                "to": CaMeLValueBuilder("evil@attacker.com")
                    .with_sources("read_email")
                    .with_inner_source("body").build(),
                "body": CaMeLValueBuilder("Secret data")
                    .with_sources("read_email").build(),
            },
            expected_outcome="Denied",
        ),
    ],
)
assert report.passed == 2

# 2. Dry-run a plan that uses send_email
registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")
simulator = PolicySimulator()

report = simulator.simulate(
    plan="result = send_email(to=_to, subject=_subj, body=_body)",
    tools=["send_email"],
    policies=registry,
    preset_vars={
        "_to": CaMeLValueBuilder("alice@example.com")
            .with_sources("User literal").build(),
        "_subj": CaMeLValueBuilder("Report").with_sources("User literal").build(),
        "_body": CaMeLValueBuilder("See attached.").with_sources("User literal").build(),
    },
)
assert report.allowed_tools == ["send_email"]

print("All checks passed — send_email policy is correctly configured.")
```

---

## 6. End-to-End Worked Example: `write_file`

```python
from camel.policy import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicyTestCase,
    PolicyTestRunner,
    PolicySimulator,
)
from camel.policy.reference_policies import make_write_file_policy

# 1. Isolate-test the policy
policy_fn = make_write_file_policy("alice@example.com")
runner = PolicyTestRunner()
report = runner.run(
    policy_fn,
    [
        PolicyTestCase(
            case_id="safe_write",
            tool_name="write_file",
            kwargs={
                "path": CaMeLValueBuilder("/home/alice/out.txt")
                    .with_sources("User literal").build(),
                "content": CaMeLValueBuilder("output data")
                    .with_sources("User literal").build(),
            },
            expected_outcome="Allowed",
        ),
        PolicyTestCase(
            case_id="path_injection",
            tool_name="write_file",
            kwargs={
                "path": CaMeLValueBuilder("/etc/passwd")
                    .with_sources("read_email").with_inner_source("subject").build(),
                "content": CaMeLValueBuilder("malicious")
                    .with_sources("User literal").build(),
            },
            expected_outcome="Denied",
            expected_reason_contains="path injection",
        ),
    ],
)
assert report.passed == 2

# 2. Dry-run a plan that writes a file
registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")
simulator = PolicySimulator()

report = simulator.simulate(
    plan="result = write_file(path=_path, content=_content)",
    tools=["write_file"],
    policies=registry,
    preset_vars={
        "_path": CaMeLValueBuilder("/home/alice/report.txt")
            .with_sources("User literal").build(),
        "_content": CaMeLValueBuilder("Monthly summary.")
            .with_sources("User literal").build(),
    },
)
assert report.allowed_tools == ["write_file"]
assert report.denied_tools == []

print("All checks passed — write_file policy is correctly configured.")
```

---

## 7. Running as pytest tests

All of the above examples work inside pytest without any special fixtures:

```python
# tests/test_my_policies.py
from camel_security.testing import CaMeLValueBuilder, PolicyTestCase, PolicyTestRunner


def test_send_email_policy_allows_trusted_recipient():
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


def test_send_email_policy_denies_injected_recipient():
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
                    "body": CaMeLValueBuilder("secret")
                        .with_sources("User literal").build(),
                },
                expected_outcome="Denied",
                expected_reason_contains="untrusted",
            )
        ],
    )
    assert report.passed == 1
```

Run with:

```bash
pytest tests/test_my_policies.py -v
```

---

## 8. Reference Import Table

| Symbol | Import path |
|---|---|
| `CaMeLValueBuilder` | `camel_security.testing` |
| `PolicyTestCase` | `camel_security.testing` |
| `PolicyCaseResult` | `camel_security.testing` |
| `PolicyTestReport` | `camel_security.testing` |
| `PolicyTestRunner` | `camel_security.testing` |
| `PolicySimulator` | `camel_security.testing` |
| `SimulationReport` | `camel_security.testing` |
| `SimulatedPolicyEvaluation` | `camel_security.testing` |
| `Allowed` / `Denied` | `camel.policy.interfaces` |
| `is_trusted` | `camel.policy.interfaces` |
| `can_readers_read_value` | `camel.policy.interfaces` |
| `get_all_sources` | `camel.policy.interfaces` |
| `PolicyRegistry` | `camel.policy` |
| `configure_reference_policies` | `camel.policy.reference_policies` |
| `Public` | `camel.value` |

---

## See Also

- [Policy Authoring Guide](policy-authoring-guide.md) — **primary combined reference** covering policy basics, three-tier governance model, test harness, simulator, enterprise deployment patterns, and migration path in a single document (v0.6.0)
- [Reference Policy Specification](policies/reference-policy-spec.md) — authoritative spec for the six built-in policies
- [Three-Tier Policy Authorship Guide](policies/three-tier-policy-authorship-guide.md) — Platform / Tool-Provider / User tier model with additional enterprise patterns
- [Consent Handler Integration Guide](consent-handler-integration.md) — customising the user consent UX
- [Security Audit Log Reference](security-audit-log.md) — audit log schema and `ConsentAuditEntry` fields
- [Architecture Reference](architecture.md) — full system design including the policy enforcement hook
