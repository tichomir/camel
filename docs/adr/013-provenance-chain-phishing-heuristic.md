# ADR-013: Provenance Chain API and Phishing-Content Heuristic

| Field         | Value                                                                                         |
|---------------|-----------------------------------------------------------------------------------------------|
| Status        | Accepted                                                                                      |
| Date          | 2026-03-18                                                                                    |
| Author        | Software Architect Persona                                                                    |
| Supersedes    | —                                                                                             |
| Superseded by | —                                                                                             |
| Related       | ADR-002 (CaMeLValue Capability System), ADR-009 (Policy Engine), ADR-012 (Consent UX)        |
| PRD refs      | §6.4 (Capabilities), §7.2 (Trusted Boundary), Goals G3, NG2, M5-F20 through M5-F22          |

---

## Context

Milestone 5 Phase: Provenance Viewer & Chat UI Integration introduces three
closely related features:

| Feature | Purpose |
|---|---|
| `agent.get_provenance(variable_name, result)` | Expose structured origin lineage for any variable from a completed run |
| Chat UI annotation | Annotate response text derived from untrusted tools with `[Source: <tool>]` badges |
| Phishing-content detection | Surface a UI warning when text claims a trusted sender identity but comes from an untrusted tool |

The existing `CaMeLValue` type already carries `sources`, `inner_source`, and
`readers` — the raw provenance data.  However, callers need a higher-level API
that:

1. Wraps the flat `sources` frozenset into an ordered, serialisable chain.
2. Classifies each source as trusted or untrusted (using `TRUSTED_SOURCES`).
3. Provides a JSON representation suitable for audit logs and `AgentResult`.
4. Applies a pattern-based heuristic to detect phishing-content markers when
   untrusted data claims a trusted sender identity.

---

## Decisions

### Decision 1 — `ProvenanceHop` and `ProvenanceChain` data model

**Decision:** Introduce two frozen dataclasses in `camel/provenance.py`.

#### `ProvenanceHop`

```python
@dataclass(frozen=True)
class ProvenanceHop:
    tool_name: str          # origin label (tool ID or trusted label)
    inner_source: str | None   # sub-field within tool output, or None
    readers: list[str] | str   # sorted list of principals, or "Public"
    timestamp: str | None      # ISO 8601 (reserved; None in v0.6.0)
```

**Rationale:**
- `tool_name` maps directly to one element of `CaMeLValue.sources`.
- `inner_source` is propagated when the variable has exactly one source
  (direct extraction from a single tool output); set to `None` for derived
  values where `inner_source` is ambiguous.
- `readers` serialises `CaMeLValue.readers` — the `_PublicType` sentinel
  becomes the string `"Public"`, and frozensets are sorted for determinism.
- `timestamp` is reserved for future use; interpreter-level per-value
  timestamps are not yet tracked.

#### `ProvenanceChain`

```python
@dataclass(frozen=True)
class ProvenanceChain:
    variable_name: str
    hops: list[ProvenanceHop]   # trusted hops first, then untrusted, both sorted
    is_trusted: bool             # property: all hops in TRUSTED_SOURCES
```

**Rationale:**
- Hops are ordered: trusted sources first (alphabetic), then untrusted sources
  (alphabetic).  This makes the common case (fully trusted variable) easy to
  scan at a glance.
- `is_trusted` is a derived property — no separate storage.  Derived values
  from mixed sources are always untrusted.

#### JSON schema (`to_dict` / `to_json`)

```json
{
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
```

The schema is stable from v0.6.0.  New fields may be added to hop objects
in minor releases; existing fields will not be removed or renamed without a
major-version bump.

**Soundness invariant for the union approach:**

`ProvenanceChain.hops` is constructed by iterating over `CaMeLValue.sources`
and emitting one hop per label.  For this to correctly represent the full
provenance lineage, `CaMeLValue.sources` **must be the transitive closure** of
all upstream origin labels — not merely the labels of direct parents.

This invariant is maintained automatically by all `propagate_*` functions
(`propagate_binary_op`, `propagate_list_construction`, `propagate_dict_construction`,
`propagate_subscript`) and `CaMeLValue.merge`, which all union sources recursively.
Any `CaMeLValue` produced through the normal interpreter path therefore satisfies
the invariant.

**Risk — direct construction bypassing propagation helpers:**

If a `CaMeLValue` is constructed directly with a `sources` field containing
only *direct* parent labels (not the full transitive closure), the resulting
`ProvenanceChain` will silently under-report hops.  This is dangerous:

* `detect_phishing_content` checks `cv.sources - TRUSTED_SOURCES` to decide
  whether to fire; an incomplete `sources` set can cause it to treat an
  untrusted value as trusted and suppress a warning it should have raised.
* `ProvenanceChain.is_trusted` will incorrectly return `True` for a value
  whose transitive ancestors include untrusted tool outputs.

There is no runtime mechanism to verify completeness of `cv.sources` without
a full `DependencyGraph` traversal (deferred — see Alternatives below).
Callers that construct `CaMeLValue` instances outside the interpreter are
responsible for ensuring `sources` is the full transitive union.  Code that
cannot guarantee this should raise `NotImplementedError` or document the
limitation explicitly rather than calling `build_provenance_chain` and
silently producing an incomplete chain.

**Alternatives considered:**
- *Full dependency-graph traversal* — walking the `DependencyGraph` to produce
  a true DAG of upstream variable assignments.  Rejected for v0.6.0: the
  `sources` union already records all contributing tools when the invariant
  above is respected; a DAG would add serialisation complexity without clear
  benefit for the primary use-case (provenance badge display).  Deferred to a
  future milestone.
- *Timestamp tracking per CaMeLValue* — adding a `created_at` field to
  `CaMeLValue`.  Rejected to avoid breaking the frozen dataclass contract and
  adding interpreter overhead.  Timestamps can be added at the interpreter
  level in a later milestone.

---

### Decision 2 — `agent.get_provenance(variable_name, result)` method signature

**Decision:**

```python
def get_provenance(
    self,
    variable_name: str,
    result: AgentResult,
) -> ProvenanceChain:
    ...
```

Raises `KeyError` with a message listing available variable names when
`variable_name` is not present in `result.provenance_chains`.

**Rationale:**
- `AgentResult` is a frozen snapshot of a completed run.  Thread safety
  requires that `CaMeLAgent` hold no mutable per-run state — the result must
  therefore be passed in explicitly.
- `KeyError` matches Python dict semantics; callers can guard with
  `variable_name in result.provenance_chains`.
- `provenance_chains` is also directly accessible on `AgentResult` for
  iteration; `get_provenance` provides the lookup-with-helpful-error ergonomic.

**Alternative considered:** Making `get_provenance` a method on `AgentResult`
directly.  Rejected: the sprint specification says `agent.get_provenance()` and
placing it on the agent keeps the provenance API co-located with the run API
in the primary entry-point class.

---

### Decision 3 — `AgentResult` provenance fields

**Decision:** Add two new optional fields to `AgentResult` (with `default_factory`
so they are backwards-compatible):

| Field | Type | Description |
|---|---|---|
| `provenance_chains` | `dict[str, ProvenanceChain]` | One chain per variable in `final_store` |
| `phishing_warnings` | `list[PhishingWarning]` | Warnings from phishing-content detector |

**Population:** `_build_provenance_data(final_store)` is called at the end of
a successful `run()` call.  Failed runs (`success=False`) leave both fields
empty.

**Rationale:** Embedding provenance in `AgentResult` allows callers to
serialise and log the full provenance context alongside the execution trace
without requiring a separate API call.

---

### Decision 4 — Phishing-content heuristic spec

**Decision:** Implement `detect_phishing_content(variable_name, cv) -> list[PhishingWarning]`
in `camel/provenance.py`.

#### Trigger condition

A `PhishingWarning` is emitted when **all** of the following hold:

1. `str(cv.value)` matches at least one of the heuristic patterns.
2. `cv.sources - TRUSTED_SOURCES` is non-empty (the value has at least one
   untrusted origin).

Values whose `sources ⊆ TRUSTED_SOURCES` never trigger a warning — they were
produced entirely from user-supplied constants or CaMeL internals and cannot
have been adversarially injected.

#### Heuristic patterns (v0.6.0)

| Pattern | Rationale |
|---|---|
| `From:\s*\S+@\S+` | Standard email `From:` header — common in injected email bodies |
| `Sender:\s*\S+` | RFC 5322 `Sender:` header variant |
| `Reply-To:\s*\S+@\S+` | Reply address impersonating a trusted sender |
| `\b(?:I am\|This is)\s+\w+` | First-person identity claims (social engineering) |
| `\bMessage\s+from\s+\w+` | Common phishing preamble |

All patterns use `re.IGNORECASE`.

#### `PhishingWarning` structure

```python
@dataclass(frozen=True)
class PhishingWarning:
    variable_name: str
    matched_pattern: str       # regex pattern string
    matched_text: str          # the matched substring
    untrusted_sources: frozenset[str]   # sources not in TRUSTED_SOURCES
    provenance_chain: ProvenanceChain   # full chain for display
```

#### Design rationale

- Heuristic-based (not semantic) — intentionally conservative.  False
  positives are acceptable; the warning is advisory, not blocking.
- The detector does **not** block execution.  Warnings surface in
  `AgentResult.phishing_warnings` for UI display only.  This is consistent
  with PRD NG2 (CaMeL does not prevent phishing; it surfaces provenance
  metadata to aid the user).
- Multiple warnings per variable are possible (one per matched pattern).

**Alternative considered:** Semantic phishing detection using the Q-LLM.
Rejected: would introduce LLM latency into every run and violates NFR-2
(policy evaluation must be synchronous and deterministic, no LLM involvement
in security decisions).

---

### Decision 5 — `TRUSTED_SOURCES` constant

**Decision:**

```python
TRUSTED_SOURCES: frozenset[str] = frozenset({"User literal", "CaMeL"})
```

Exported from `camel.provenance` and `camel_security`.

**Rationale:**
- These two labels are assigned exclusively by the interpreter and the P-LLM
  wrapper to values that have no external (adversary-reachable) origin.
- Any label not in this set must have come from a tool call — and tool outputs
  are untrusted by definition (PRD §7.2).
- Exporting the constant allows operators to build custom heuristics that
  reference the same trust boundary without hardcoding strings.

---

### Decision 6 — Chat UI annotation contract

**Decision:** Chat UI integration is a *consumer-side* responsibility.  The
`camel_security` SDK provides the data model (`ProvenanceChain`, `PhishingWarning`);
the annotation rendering (badge text, colour, warning display) is left to the
caller.

**Recommended annotation format:** `[Source: <tool_name>]` appended inline, or
a tooltip/popover in rich UIs.

**PhishingWarning display guidance:**

> ⚠ **Provenance warning** — This response contains text that claims a sender
> identity (e.g. `From: alice@corp.com`) but originates from an untrusted
> source (`get_last_email`).  Verify the claim independently before acting.

**Rationale:** CaMeL is a backend library; it must not impose a UI framework
dependency.  Providing structured data enables any frontend to render the
annotation appropriately.

---

## Consequences

### Positive
- `agent.get_provenance(variable_name, result)` provides a clean, tested API
  for provenance lookup with `KeyError` semantics.
- `AgentResult.provenance_chains` enables full provenance serialisation to JSON
  for audit logs without additional API calls.
- `detect_phishing_content` partially addresses PRD NG2 (phishing surface) in a
  way that satisfies NFR-2 (no LLM in security decisions).
- `TRUSTED_SOURCES` gives operators a stable, named constant for building their
  own provenance heuristics.

### Negative / Accepted Trade-offs
- `ProvenanceChain` represents the *merged* sources frozenset, not a true DAG
  of upstream variable derivations.  For variables derived from two tool
  outputs, both sources appear as siblings, not in causal order.  Mitigated by
  the planned future milestone to integrate `DependencyGraph` traversal.
  **The union approach is only sound if `CaMeLValue.sources` is the full
  transitive closure of all upstream origins** (see Decision 1 soundness
  invariant above).  Code that constructs `CaMeLValue` directly must uphold
  this invariant or the provenance chain and phishing detection will
  silently produce incorrect results.
- `timestamp` is always `None` in v0.6.0.  This is documented as a reserved
  field; callers must not rely on it being populated.
- Heuristic phishing detection has inherent false-positive and false-negative
  rates.  This is acceptable given the advisory-only semantics.

---

## Acceptance Criteria Traceability

| Criterion | Satisfied by |
|---|---|
| `ProvenanceChain` dataclass with `variable_name`, `hops`, `to_dict/to_json` | `camel/provenance.py` — `ProvenanceChain`, `ProvenanceHop` |
| `agent.get_provenance(variable_name)` signature, `KeyError` for unknown | `camel_security/agent.py` — `CaMeLAgent.get_provenance` |
| JSON serialisation schema validated against fixture | `ProvenanceChain.to_dict()` / `to_json()`, schema documented in §Decision 1 |
| Phishing heuristic spec: patterns + trust condition | `camel/provenance.py` — `_PHISHING_PATTERNS`, `TRUSTED_SOURCES`, `detect_phishing_content` |
| PRD §6.4 updated | `docs/architecture.md` §12 updated |
| PRD §7.2 updated | `docs/architecture.md` §10 updated |
| ADR recorded | This document |
