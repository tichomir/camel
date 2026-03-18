# ADR-011: Three-Tier Policy Governance — Multi-Party Policy Authorship, Conflict Resolution, and Audit Trail

| Field         | Value                                                                       |
|---------------|-----------------------------------------------------------------------------|
| Status        | Accepted                                                                    |
| Date          | 2026-03-18                                                                  |
| Author        | Software Architect Persona                                                  |
| Supersedes    | Portions of ADR-009 §Decision 6 (single-module configuration loading)      |
| Superseded by | —                                                                           |
| Related       | ADR-009 (Policy Engine), ADR-010 (Enforcement Hook & Consent Flow)         |
| PRD refs      | FW-5 (Multi-Party Policy Governance), M5-F8 through M5-F11, Risk L4       |

---

## Context

CaMeL's existing `PolicyRegistry` (ADR-009) supports a single policy namespace with
flat registration order.  As deployments mature, three distinct groups of stakeholders
need to author policies with different authorities:

| Stakeholder    | Authority                               | Example policy                          |
|----------------|-----------------------------------------|-----------------------------------------|
| Platform        | Defines organisational security floor  | "Never send email to external domains"  |
| Tool-Provider   | Constrains its own tool's usage        | "fetch_url may only contact allow-listed hosts" |
| User            | Personalises within platform guardrails | "Only schedule meetings on Tuesdays"    |

The existing flat registry has no concept of authorship tiers, precedence, or
`non_overridable` rules.  This means:

1. A user can register a policy that partially contradicts a platform policy; the
   outcome depends on registration order, not explicit precedence.
2. There is no mechanism to mark a platform-level denial as immune to user consent
   (ADR-010 consent flow), which creates a tension between usability (G6, Risk L4)
   and absolute security guarantees (G7).
3. Post-execution auditors cannot determine which organisational tier was responsible
   for a denial, making compliance reporting difficult.

This ADR closes PRD open question **FW-5** and delivers milestone features
**M5-F8 through M5-F11**.

---

## Decisions

### Decision 1 — Three explicit policy tiers with strict precedence

**Decision:** Introduce a `PolicyTier` enum with three members evaluated in
descending precedence order:

```python
class PolicyTier(Enum):
    PLATFORM     = "Platform"      # highest authority
    TOOL_PROVIDER = "ToolProvider" # mid-tier
    USER         = "User"          # lowest authority
```

**Evaluation order:** Platform → Tool-Provider → User.  Within each tier,
policies are evaluated in registration order (first-Denied short-circuits, consistent
with ADR-009 Decision 3).  Evaluation halts at the first `Denied` encountered
across any tier.

**Rationale:**

Explicit precedence eliminates registration-order ambiguity between stakeholders.
Platform policies always run first because the deploying organisation has the highest
authority to set security guardrails.  Tool-Provider policies run next because tool
authors understand their tool's attack surface better than generic users.  User
policies run last and can add restrictions (increase security) but — without an
explicit override — cannot weaken decisions already made by higher tiers.

**Why not a priority integer:**

An integer priority creates ambiguity (is priority 10 higher than 1?), allows
accidental collisions, and requires documentation to understand.  A named enum is
self-documenting and exhaustively checkable by mypy.

---

### Decision 2 — `non_overridable` flag on Platform policies

**Decision:** Platform policies may carry a boolean `non_overridable` flag
(default `False`).  When `True`, a `Denied` outcome from that policy is **absolute**:

- No lower-tier policy (Tool-Provider or User) evaluation occurs after it.
- The consent callback (ADR-010 Decision 2) is **not invoked** in PRODUCTION mode;
  the denial is surfaced directly as a `PolicyViolationError` with a distinguishing
  flag.
- The `MergedPolicyResult` exposes `non_overridable_denial: bool = True` to
  downstream callers, signalling that no consent flow should be offered.

The flag is **only meaningful on Platform-tier policies** returning `Denied`.
A `non_overridable` Platform policy returning `Allowed` has no additional effect:
lower tiers can still add `Denied` results (they are making the decision more
restrictive, not weakening it).

**Semantics summary:**

| Platform result | `non_overridable` | Lower-tier evaluation | Consent override allowed? |
|-----------------|-------------------|-----------------------|---------------------------|
| `Denied`        | `True`            | Skipped               | **No** — absolute denial  |
| `Denied`        | `False`           | Skipped               | Yes (PRODUCTION mode)     |
| `Allowed`       | `True`            | Continues             | N/A                       |
| `Allowed`       | `False`           | Continues             | N/A                       |

**Rationale:**

Risk L4 (user fatigue) is addressed by allowing Platform to mark only truly
non-negotiable rules as `non_overridable`.  Rules about sensitive data residency or
regulatory compliance should be `non_overridable`; rules about preferred naming
conventions may safely allow user consent override.  This gives operators fine-grained
control without forcing a binary "all policies are absolute" or "all policies can be
overridden" choice.

**Why not `non_overridable` on Tool-Provider or User tiers:**

Lower tiers have lower authority.  A Tool-Provider policy that denies an action can
already be consented in production mode, which is the appropriate level of override
flexibility for that tier.  Allowing lower tiers to declare themselves `non_overridable`
would create a mechanism for a tool author to permanently block user consent, which
conflicts with the trust model.

---

### Decision 3 — New data types for three-tier storage and audit trail

**Decision:** Introduce four new data types in `camel/policy/governance.py`:

#### 3.1 `TieredPolicyEntry`

Wraps a `PolicyFn` with its tier metadata:

```python
@dataclass(frozen=True)
class TieredPolicyEntry:
    policy_fn: PolicyFn
    tier: PolicyTier
    non_overridable: bool
    name: str  # human-readable; defaults to policy_fn.__name__
```

`non_overridable` is stored on the entry so the resolver can inspect it at
evaluation time without re-checking the tier.

#### 3.2 `TierEvaluationRecord`

Records the result of evaluating a single policy, used in the audit trail:

```python
@dataclass(frozen=True)
class TierEvaluationRecord:
    tier: PolicyTier
    policy_name: str
    result: SecurityPolicyResult
    non_overridable: bool
```

#### 3.3 `MergedPolicyResult`

The output of `PolicyConflictResolver.evaluate()`:

```python
@dataclass(frozen=True)
class MergedPolicyResult:
    outcome: SecurityPolicyResult
    authoritative_tier: PolicyTier | None  # None when all tiers return Allowed
    non_overridable_denial: bool           # True only for Platform non-overridable Denied
    audit_trail: tuple[TierEvaluationRecord, ...]  # ordered evaluation records

    @property
    def is_allowed(self) -> bool:
        """Return True when the merged outcome is Allowed."""

    @property
    def can_be_consented(self) -> bool:
        """Return False when the denial was from a non_overridable Platform policy.

        The consent callback (ADR-010) MUST NOT be invoked when this is False.
        """
```

**Audit trail coverage:** The `audit_trail` contains a `TierEvaluationRecord` for
every policy that was evaluated, in evaluation order.  Policies in tiers skipped
due to a higher-tier short-circuit are **not** included (consistent with the
short-circuit semantics inherited from ADR-009 Decision 3).

**Why `outcome: SecurityPolicyResult` rather than `Allowed | Denied` union:**

`SecurityPolicyResult` is the sealed base type defined in `camel/policy/interfaces.py`.
Referencing it directly keeps `MergedPolicyResult` compatible with all existing
code that already handles `SecurityPolicyResult` via isinstance.

#### 3.4 `TieredPolicyRegistry`

Stores `TieredPolicyEntry` objects partitioned by tier and tool name:

```python
class TieredPolicyRegistry:
    """Storage layer for three-tier policy entries.

    Stores TieredPolicyEntry objects keyed by (tier, tool_name).
    Does not evaluate policies — evaluation is delegated to PolicyConflictResolver.
    """

    def register(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        tier: PolicyTier,
        *,
        non_overridable: bool = False,
        name: str = "",
    ) -> PolicyFn:
        """Register a policy function for a tool in the given tier.

        Returns policy_fn unchanged (enables decorator usage).

        Raises
        ------
        ValueError
            If non_overridable is True and tier is not PolicyTier.PLATFORM.
        """

    # Convenience methods (delegate to register()):
    def register_platform(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        non_overridable: bool = False,
        name: str = "",
    ) -> PolicyFn: ...

    def register_tool_provider(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        name: str = "",
    ) -> PolicyFn: ...

    def register_user(
        self,
        tool_name: str,
        policy_fn: PolicyFn,
        *,
        name: str = "",
    ) -> PolicyFn: ...

    def get_entries(
        self,
        tool_name: str,
        tier: PolicyTier,
    ) -> list[TieredPolicyEntry]:
        """Return all entries for the given tool name and tier, in registration order."""

    def registered_tools(
        self,
        tier: PolicyTier | None = None,
    ) -> frozenset[str]:
        """Return tool names with at least one registered policy.

        If tier is None, return the union across all tiers.
        """
```

**Why validate `non_overridable` at registration, not evaluation:**

A `ValueError` at registration time surfaces a misconfiguration immediately,
before any tool call is made.  A silent no-op at evaluation time would be hard
to detect and could give a false sense of security.

---

### Decision 4 — `PolicyConflictResolver` — the merging engine

**Decision:** Introduce `PolicyConflictResolver` as a standalone class that wraps
a `TieredPolicyRegistry` and implements the deterministic conflict resolution
algorithm:

```python
class PolicyConflictResolver:
    """Merges three-tier policies into a single authoritative SecurityPolicyResult.

    Evaluation order: Platform → Tool-Provider → User.
    Within each tier: registration order, first-Denied short-circuits.
    Non-overridable Platform Denied: halts all evaluation immediately.
    """

    def __init__(self, registry: TieredPolicyRegistry) -> None: ...

    def evaluate(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> MergedPolicyResult:
        """Run all registered policies for tool_name across all three tiers.

        Returns a MergedPolicyResult with the authoritative outcome and full
        audit trail.  Never raises; all exceptions from policy functions
        propagate to the caller unchanged.
        """

    def evaluate_flat(
        self,
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        """Convenience wrapper returning only SecurityPolicyResult.

        Equivalent to self.evaluate(...).outcome.
        Allows PolicyConflictResolver to be used as a drop-in for the
        existing PolicyRegistry.evaluate() call sites.
        """
```

#### 4.1 Conflict Resolution Algorithm

```
INPUT: tool_name, kwargs
audit_trail = []

--- Phase 1: Platform tier ---
for entry in registry.get_entries(tool_name, PolicyTier.PLATFORM):
    result = entry.policy_fn(tool_name, kwargs)
    audit_trail.append(TierEvaluationRecord(
        tier=PolicyTier.PLATFORM,
        policy_name=entry.name,
        result=result,
        non_overridable=entry.non_overridable,
    ))
    if isinstance(result, Denied):
        RETURN MergedPolicyResult(
            outcome=result,
            authoritative_tier=PolicyTier.PLATFORM,
            non_overridable_denial=entry.non_overridable,
            audit_trail=tuple(audit_trail),
        )

--- Phase 2: Tool-Provider tier ---
for entry in registry.get_entries(tool_name, PolicyTier.TOOL_PROVIDER):
    result = entry.policy_fn(tool_name, kwargs)
    audit_trail.append(TierEvaluationRecord(
        tier=PolicyTier.TOOL_PROVIDER,
        policy_name=entry.name,
        result=result,
        non_overridable=False,
    ))
    if isinstance(result, Denied):
        RETURN MergedPolicyResult(
            outcome=result,
            authoritative_tier=PolicyTier.TOOL_PROVIDER,
            non_overridable_denial=False,
            audit_trail=tuple(audit_trail),
        )

--- Phase 3: User tier ---
for entry in registry.get_entries(tool_name, PolicyTier.USER):
    result = entry.policy_fn(tool_name, kwargs)
    audit_trail.append(TierEvaluationRecord(
        tier=PolicyTier.USER,
        policy_name=entry.name,
        result=result,
        non_overridable=False,
    ))
    if isinstance(result, Denied):
        RETURN MergedPolicyResult(
            outcome=result,
            authoritative_tier=PolicyTier.USER,
            non_overridable_denial=False,
            audit_trail=tuple(audit_trail),
        )

--- Phase 4: All allowed ---
RETURN MergedPolicyResult(
    outcome=Allowed(),
    authoritative_tier=None,
    non_overridable_denial=False,
    audit_trail=tuple(audit_trail),
)
```

**Scenario table (exhaustive):**

| Scenario                              | Platform    | ToolProvider | User        | `authoritative_tier` | `non_overridable_denial` |
|---------------------------------------|-------------|--------------|-------------|----------------------|--------------------------|
| Platform-only, Denied (NO)            | Denied      | —            | —           | Platform             | False                    |
| Platform-only, Denied (NOO)           | Denied NO   | —            | —           | Platform             | **True**                 |
| Tool-Provider-only, Denied            | Allowed     | Denied       | —           | ToolProvider         | False                    |
| User-only, Denied                     | Allowed     | Allowed      | Denied      | User                 | False                    |
| All Allowed                           | Allowed     | Allowed      | Allowed     | None                 | False                    |
| All Denied                            | Denied      | —            | —           | Platform             | False                    |
| Mixed (Platform Allowed, TP Denied)   | Allowed     | Denied       | —           | ToolProvider         | False                    |
| Mixed (Platform NOO Denied)           | Denied NO   | —            | —           | Platform             | **True**                 |
| No policies registered for any tier   | —           | —            | —           | None                 | False                    |

Legend: `NO` = `non_overridable=True`; `—` = not evaluated (skipped by short-circuit or no policies registered).

**Determinism guarantee:** The algorithm is deterministic for a fixed registry state.
All operations are pure Python with no I/O, no LLM calls, and no randomness (NFR-2).

---

### Decision 5 — Backward compatibility with existing `PolicyRegistry`

**Decision:** The existing `PolicyRegistry` (ADR-009) is **not modified** and remains
the canonical API for flat (non-tiered) deployments.  `PolicyConflictResolver` is an
additive, opt-in component.

Integration with `CaMeLInterpreter`:

- The interpreter's `policy_engine` constructor parameter continues to accept a
  `PolicyRegistry` instance (existing behaviour).
- A new optional `conflict_resolver` constructor parameter accepts a
  `PolicyConflictResolver`.  When present, it takes precedence over `policy_engine`
  for all tool-call pre-execution evaluations.
- Both parameters are never active simultaneously; `ValueError` is raised at
  construction time if both are supplied.

```python
CaMeLInterpreter(
    tools=...,
    # Legacy flat registry (ADR-009)
    policy_engine: PolicyRegistry | None = None,
    # New three-tier resolver (this ADR)
    conflict_resolver: PolicyConflictResolver | None = None,
    ...
)
```

When `conflict_resolver` is active, the enforcement hook uses
`conflict_resolver.evaluate()` instead of `policy_engine.evaluate()`.  The
`MergedPolicyResult` is used to set `can_be_consented` before invoking the consent
callback: if `can_be_consented` is `False`, the consent callback is bypassed and
`PolicyViolationError` is raised directly with `consent_decision=None`.

**Why not replace `PolicyRegistry`:**

Replacing `PolicyRegistry` would break all existing code that constructs and injects
a registry.  An additive API preserves backward compatibility (NFR-7, NFR-8) and
allows incremental adoption.

---

### Decision 6 — Configuration-driven three-tier loading

**Decision:** Extend the environment-variable loading pattern (ADR-009 Decision 6)
to support tier-aware configuration modules:

```python
# CAMEL_TIERED_POLICY_MODULE=myapp.security.tiered_policies
def configure_tiered_policies(registry: TieredPolicyRegistry) -> None:
    registry.register_platform("send_email", platform_email_policy, non_overridable=True)
    registry.register_tool_provider("fetch_url", url_allowlist_policy)
    registry.register_user("create_calendar_event", user_calendar_policy)
```

`PolicyConflictResolver.load_from_env()` reads `CAMEL_TIERED_POLICY_MODULE` and
returns a fully constructed resolver.

The existing `CAMEL_POLICY_MODULE` variable and `PolicyRegistry.load_from_env()` are
unchanged.

---

### Decision 7 — File location and module exports

**Decision:** All new types are defined in `camel/policy/governance.py`.
`camel/policy/__init__.py` is updated to re-export them.

**New `camel/policy/governance.py` public API:**

```python
__all__ = [
    "PolicyTier",
    "TieredPolicyEntry",
    "TierEvaluationRecord",
    "MergedPolicyResult",
    "TieredPolicyRegistry",
    "PolicyConflictResolver",
]
```

**Rationale for a new module rather than extending `interfaces.py`:**

`interfaces.py` already serves as the authoritative source for the flat policy API.
Adding ~300 lines of tier-aware code to it would make the module unwieldy and could
break downstream packages that import from it directly.  A separate `governance.py`
module maintains a clean separation of concerns.

---

### Decision 8 — `non_overridable` enforcement and the consent hook interaction

**Decision:** The enforcement hook in `CaMeLInterpreter._eval_Call` (ADR-010
Decision 4) is extended with a `can_be_consented` check when a `PolicyConflictResolver`
is active:

```
# Extended enforcement hook sequence (conflict_resolver active):
1.  Evaluate arguments → kwargs_mapping
2.  Call conflict_resolver.evaluate(tool_name, kwargs_mapping) → merged
3a. merged.is_allowed → True:
      append AuditLogEntry(outcome="Allowed", consent_decision=None, authoritative_tier=None)
      proceed to tool call
3b. merged.is_allowed → False AND merged.can_be_consented → False:
      # non_overridable Platform Denied
      append AuditLogEntry(outcome="Denied", consent_decision=None,
                           authoritative_tier="Platform",
                           non_overridable_denial=True)
      raise PolicyViolationError(tool_name, reason, consent_decision=None)
3c. merged.is_allowed → False AND merged.can_be_consented → True:
      if enforcement_mode == EVALUATION:
          append AuditLogEntry(outcome="Denied", ...)
          raise PolicyViolationError(...)
      if enforcement_mode == PRODUCTION:
          invoke consent_callback → bool
          proceed as per ADR-010 Decision 4c
```

**Audit log extension:** `AuditLogEntry` gains two optional fields:

```python
authoritative_tier: str | None = None   # "Platform", "ToolProvider", "User", or None
non_overridable_denial: bool = False
```

These default to `None`/`False` when the flat `PolicyRegistry` is in use, preserving
full backward compatibility with existing tests that do not check these fields.

---

## Summary of Precedence Rules

| Rule | Description |
|------|-------------|
| P1 | Platform policies are always evaluated first. |
| P2 | Within a tier, policies are evaluated in registration order. |
| P3 | The first `Denied` in any tier terminates evaluation of that tier and all lower tiers. |
| P4 | A Platform `Denied` with `non_overridable=True` skips lower tiers AND suppresses the consent callback. |
| P5 | A Platform `Denied` with `non_overridable=False` skips lower tiers but ALLOWS the consent callback in PRODUCTION mode. |
| P6 | If all three tiers return `Allowed` (or have no policies), the outcome is `Allowed`. |
| P7 | `non_overridable` may only be set on Platform-tier policies; `ValueError` is raised at registration otherwise. |

---

## Consequences

### Positive

- PRD open question **FW-5** (Multi-party policy governance) is formally addressed.
- Risk **L4** (user fatigue) is mitigated: only policies that truly require it are
  marked `non_overridable`; all others remain consent-eligible in PRODUCTION mode.
- The `MergedPolicyResult.audit_trail` gives compliance teams an authoritative,
  tier-attributed record of every policy decision.
- Full backward compatibility: existing `PolicyRegistry`-based deployments require
  zero code changes.
- `PolicyConflictResolver.evaluate_flat()` allows gradual migration: call sites can
  switch to the resolver without changing how they consume the result.

### Negative / Trade-offs

- **Two registry classes:** Operators must choose between `PolicyRegistry` (flat) and
  `TieredPolicyRegistry` (tiered).  The coexistence adds cognitive surface area.
  Mitigation: documentation clearly states that `TieredPolicyRegistry` is the
  recommended choice for all new deployments.
- **`AuditLogEntry` extension:** Adding `authoritative_tier` and `non_overridable_denial`
  fields with default values is backward-compatible but lengthens the dataclass.
  Serialised JSON audit logs will grow slightly.
- **Consent callback bypass:** The `can_be_consented` logic in the enforcement hook
  adds one conditional branch to the already-complex enforcement sequence.  This is
  unavoidable given the `non_overridable` design requirement.
- **No cross-tier relaxation:** Once Platform Denies (even without `non_overridable`),
  lower tiers cannot Allow.  This is intentional (Platform is the highest authority)
  but means Tool-Provider and User policies cannot grant exceptions to platform rules
  without the platform policy being removed or made non-`non_overridable`.

---

## Policy Authorship Guide Location

A full enterprise deployment guide for the three-tier model (tier hierarchy, override
semantics, recommended patterns) is published at:

```
docs/policies/three-tier-policy-authorship-guide.md
```

It covers:

- When to use each tier
- Non-overridable rule selection criteria
- Migration path from flat `PolicyRegistry` to `TieredPolicyRegistry`
- Example platform, tool-provider, and user policy modules for common enterprise scenarios
- Audit log interpretation for compliance reporting

---

## Related ADRs

| ADR                                              | Topic                                      |
|--------------------------------------------------|--------------------------------------------|
| [009](009-policy-engine-architecture.md)         | Flat policy engine (superseded in part)    |
| [010](010-enforcement-hook-consent-audit-harness.md) | Enforcement hook and consent callback  |
| [002](002-camelvalue-capability-system.md)       | CaMeLValue capability propagation          |
| [003](003-ast-interpreter-architecture.md)       | Interpreter tool dispatch path             |
