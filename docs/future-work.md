# CaMeL — Open Questions & Future Work Tracker

**Document version:** 1.0
**Last updated:** 2026-03-18
**CaMeL version:** 0.6.0 (Milestone 5 — Final)
**Owner:** Software Architect

> **Related documents:**
> [PRD §12](PRD.md#12-open-questions--future-work) ·
> [Known Limitations & Risks Register](limitations.md) ·
> [Architecture Reference](architecture.md) ·
> [Security Hardening Design](design/security_hardening_design.md)

---

## Overview

This tracker is the authoritative source for all open questions and future work
items in the CaMeL project.  Each entry covers the research or implementation
question, its current status, relevant prior work, and recommended next steps.

**Status values:**

| Status | Meaning |
|---|---|
| **Open** | No work started; item is on the backlog |
| **In Progress** | Active investigation or partial implementation underway |
| **Resolved** | Design question answered or feature delivered; item closed |

---

## FW-1 — Alternative Execution Language

| Field | Value |
|---|---|
| **ID** | FW-1 |
| **Topic** | Alternative execution language |
| **Status** | **Open** |
| **Priority** | Research |
| **Related limitation** | [L3](limitations.md#l3--residual-exception-based-side-channel) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

Python's exception model creates structural side-channel risks: any exception
raised during interpreter execution carries information about the code path
that was taken, and by extension, information about the data that triggered
that path.

Milestone 4 exception hardening (M4-F6 through M4-F9) closes the primary
vectors (message redaction, NEIE handler, loop-body propagation), but the
residual risk of deeply nested exception chains (exceptions propagating through
multiple tool-call frames before reaching the interpreter handler) cannot be
fully eliminated within the Python exception model.

A Haskell-style execution language with explicit `Result`/`Either` error
handling would **eliminate the exception side-channel class entirely**: errors
would be values in the dependency graph, not stack-unwinding events that carry
implicit control-flow information.

A Rust-based DSL is also a candidate, offering:
- Explicit `Result<T, E>` types with no implicit exceptions
- Memory safety guarantees relevant to interpreter implementation security
- Strong type system for encoding capability constraints

### Prior Work & Context

- M4 exception hardening design:
  [design/milestone4-exception-hardening.md](design/milestone4-exception-hardening.md)
- The exception side-channel class is documented as a residual risk in
  [L3](limitations.md#l3--residual-exception-based-side-channel) and
  PRD §10.
- The PRD (original, Debenedetti et al.) identifies Python's exception model
  as a structural weakness (PRD §12, FW-1).

### Recommended Next Steps

1. Survey existing restricted-Python-subset DSL work (e.g., Starlark, Hy,
   other sandboxed Python variants) to assess whether Python can be extended
   with explicit error handling without abandoning the language.
2. Prototype a minimal Haskell or Rust DSL sufficient to express the P-LLM
   code plan grammar (assignments, conditionals, for-loops, function calls).
3. Evaluate LLM code generation quality on the new DSL vs. Python
   (a smaller subset = less code generation flexibility).
4. If feasible, design a migration path from the current Python-AST
   interpreter to the new DSL interpreter.

### Notes

- M4 exception hardening significantly reduces the practical risk of this
  channel; the language redesign is a research direction, not an urgent fix.
- Changing the execution language would be a major architectural revision
  (Milestone 6+ scope).

---

## FW-2 — Formal Verification

| Field | Value |
|---|---|
| **ID** | FW-2 |
| **Topic** | Formal verification of interpreter correctness |
| **Status** | **Open** |
| **Priority** | Research |
| **Related limitation** | [L8](limitations.md#l8--formal-verification-gap) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

CaMeL's security guarantees are currently justified by:
- Architectural argument (the PI-SEC formal security game, PRD §7.1)
- Comprehensive test coverage (30+ test files, ~291 tests as of M3 exit criteria)
- Code review

A machine-verified proof of the core interpreter properties would provide
a stronger, auditor-facing assurance that the implementation correctly enforces
the design.

The key properties to verify:

1. **Policy enforcement completeness:** Every tool call that proceeds has
   passed all registered policies; no tool call can bypass the pre-execution
   hook.
2. **Capability propagation soundness:** The capability of every derived value
   is a superset of the capabilities of all its upstream inputs.
3. **STRICT mode dependency completeness:** In STRICT mode, all required
   dependency propagation rules (M4-F1 through M4-F4) are correctly applied.
4. **Exception redaction completeness:** No exception message with any
   untrusted-data dependency reaches the P-LLM unredacted.

### Candidate Proof Assistants

| Tool | Approach | Notes |
|---|---|---|
| **Lean 4** | Functional proof assistant; growing ML/PL community | Active development; good interop with Python semantics research |
| **Coq** | Mature proof assistant with extraction support | Strong dependent type theory; higher learning curve |
| **Isabelle/HOL** | Higher-order logic; strong automation | Well-suited for data flow analysis proofs |

### Prior Work & Context

- The PRD (original, Debenedetti et al.) identifies formal verification as
  future work (PRD §12, FW-2).
- [L8](limitations.md#l8--formal-verification-gap) documents this as the
  formal verification gap limitation.
- The interpreter is implemented in Python using the `ast` library; a
  formal model would likely be written in the proof assistant's language
  with a proof of correspondence to the Python implementation.

### Recommended Next Steps

1. Define the formal specification of the four target properties above.
2. Evaluate Lean 4 vs. Coq vs. Isabelle for this use case.
3. Develop a small-scale proof of capability propagation soundness as a
   feasibility exercise.
4. If successful, expand to full interpreter correctness proof.

### Notes

- This is a multi-month research effort and is not appropriate for a
  sprint-based delivery cycle without dedicated resources.
- Current test coverage provides reasonable assurance for deployment;
  formal verification is a long-term quality initiative.

---

## FW-3 — Security Policy Automation

| Field | Value |
|---|---|
| **ID** | FW-3 |
| **Topic** | Auto-generation of security policies from tool documentation |
| **Status** | **Open** |
| **Priority** | Medium |
| **Related limitation** | [L7](limitations.md#l7--ecosystem-adoption-and-third-party-tool-annotations) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

Writing security policies and capability annotations manually is the primary
friction point for third-party tool adoption.  Integrating CaMeL with
contextual integrity frameworks (e.g., AirGap, Shi et al. 2025 DSL) would
allow policies to be auto-generated from tool documentation, type annotations,
and deployment context.

The vision is: a tool provider annotates their tool with standard metadata
(data classification, intended recipients, allowed data sources), and a
policy generation layer emits a correct CaMeL policy without requiring the
operator to write it by hand.

### Relevant Research

- **Contextual integrity** (Nissenbaum 2004): information flows are appropriate
  when they conform to the norms of the context in which the information was
  originally shared.  A policy DSL based on contextual integrity would allow
  tools to declare their information-flow norms declaratively.
- **AirGap** and **Shi et al. 2025 DSL:** formal DSLs for expressing contextual
  integrity constraints that can be compiled to policy checks.

### Prior Work & Context

- M5 shipped `PolicyTestRunner`, `CaMeLValueBuilder`, and `PolicySimulator` —
  tools that reduce the *testing* burden for manually-authored policies.
- The [Policy Authoring Tutorial](policy-authoring-tutorial.md) reduces the
  learning curve for writing policies.
- Automated policy generation would address the root cause: policies should
  not require manual authoring for well-understood tool categories.

### Recommended Next Steps

1. Identify the most common tool categories (email, cloud storage, calendar,
   HTTP fetch) and design a metadata schema for each.
2. Prototype a policy generator that takes tool metadata and emits a CaMeL
   Python policy function.
3. Evaluate correctness of generated policies against the reference policy
   library.
4. Explore integration with AirGap or Shi et al. DSL for formal correctness
   guarantees on generated policies.

### Notes

- This is a medium-priority item; current manual policy authoring tools
  (M5) provide a workable solution for production deployments.
- Policy automation is the key enabler for broad ecosystem adoption (L7).

---

## FW-4 — Nested P-LLM Tool

| Field | Value |
|---|---|
| **ID** | FW-4 |
| **Topic** | Nested P-LLM tool for data-requires-action scenarios |
| **Status** | **Open** |
| **Priority** | High |
| **Related limitation** | [L1](limitations.md#l1--data-requires-action-failure) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

The data-requires-action failure (L1) is the largest source of utility
degradation in the Travel domain.  The root cause is that the P-LLM cannot
observe tool return values, so it cannot plan actions that depend on the
content of prior tool outputs.

A **nested P-LLM tool** would allow the outer P-LLM to delegate a
sub-task to an inner P-LLM instance.  The inner P-LLM:
- Receives a *trusted* sub-query from the outer P-LLM (not from tool output)
- Is permitted to call a restricted subset of tools
- Operates under an even stricter policy set than the outer interpreter
- Returns a structured result to the outer interpreter as a `CaMeLValue`

This approach would enable tasks of the form:
```python
# Outer P-LLM plan:
itinerary = get_travel_itinerary(booking_id)
# Delegate to inner P-LLM with restricted tool access:
booking_actions = nested_plan(
    query="Book hotels for each leg of the itinerary",
    context=itinerary,
    allowed_tools=["book_hotel", "check_availability"],
)
```

The outer P-LLM trusts the *query* it sends to the inner instance (because
the outer P-LLM generated it from trusted user input), not the tool output
(`itinerary`) that the inner instance processes.

### Design Constraints

- The inner P-LLM must have its own policy set, independent of the outer
  instance.
- The inner P-LLM's tool access must be strictly limited to prevent
  privilege escalation via the nested instance.
- The capability annotations on the inner P-LLM's results must correctly
  propagate the untrusted provenance of the `context` parameter.
- The retry budget for the inner instance must be separate from the outer
  instance's budget.

### Prior Work & Context

- [L1](limitations.md#l1--data-requires-action-failure) documents this as
  the primary utility limitation.
- Travel domain performance (46–52% projected) vs. 76% native baseline
  represents the utility cost.
- The paper (Debenedetti et al.) identifies this as an open problem in §12.

### Recommended Next Steps

1. Design the nested P-LLM API surface and security invariants.
2. Define how capability annotations propagate from inner to outer instance.
3. Prototype the nested execution with a simplified tool set.
4. Evaluate utility improvement on Travel domain AgentDojo tasks.
5. Security review: confirm the nested architecture does not introduce new
   prompt injection vectors.

### Notes

- This is a **Milestone 6 candidate** — architecturally significant and
  requires careful security review before shipping.
- Even a partial implementation (supporting a limited class of
  data-requires-action scenarios) would meaningfully improve Travel utility.

---

## FW-5 — Multi-Party Policy Governance

| Field | Value |
|---|---|
| **ID** | FW-5 |
| **Topic** | Policy conflict resolution across platform, tool-provider, and user tiers |
| **Status** | **Resolved** |
| **Resolved in** | Milestone 5 (Sprint: Multi-Party Policy Governance) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

When multiple parties (platform operator, tool provider, end user) each
register security policies for the same tool, the system needs a deterministic
conflict resolution algorithm.  Without one, policies from different tiers
might contradict each other with no principled way to resolve the conflict.

### Resolution

Delivered in M5:

- **Three-tier `PolicyRegistry`:** Policies are organised into Platform,
  Tool-Provider, and User tiers with documented precedence rules.
- **`non-overridable` flag:** Platform policies can be marked as
  non-overridable, preventing lower tiers from weakening them.
- **`PolicyConflictResolver`:** Merges three tiers for a given tool and
  returns a single `SecurityPolicyResult` with an authoritative-tier audit
  trail.
- **Policy authorship guide:** Documents tier hierarchy, override semantics,
  and recommended patterns for enterprise deployments.

### References

- [ADR 011 — Three-Tier Policy Governance](adr/011-three-tier-policy-governance.md)
- [Three-Tier Policy Authorship Guide](policies/three-tier-policy-authorship-guide.md)
- [Policy Authoring Tutorial](policy-authoring-tutorial.md)

### Notes

- Item fully resolved; no further action required.
- The `PolicyConflictResolver` audit trail satisfies enterprise compliance
  requirements for auditable policy decisions.

---

## FW-6 — ROP-Analogue Attacks

| Field | Value |
|---|---|
| **ID** | FW-6 |
| **Topic** | Detection and prevention of action-chaining (ROP-analogue) attacks |
| **Status** | **In Progress** |
| **Related limitation** | [L6](limitations.md#l6--rop-analogous-attacks-action-chaining) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

An adversary may chain individually policy-allowed tool calls to produce a
collectively malicious outcome — analogous to return-oriented programming (ROP)
in traditional binary exploitation.

CaMeL's policy engine evaluates each tool call independently.  A sequence of
calls that individually pass all policies may collectively violate the spirit
of the user's security model.

### Progress to Date

- **`DataToControlFlowWarning` (M4-F15):** Detects when tool name resolution
  depends on untrusted data — the most direct form of data-to-control
  escalation.  Implemented in Milestone 4.
- **Elevated consent gate (M4-F16):** When `DataToControlFlowWarning` is
  emitted, execution is paused and elevated user consent is requested.
- **Audit log:** All tool calls with full capability dependency chains are
  written to the structured JSON audit log, providing raw data for
  post-execution anomaly analysis.

### Open Research Questions

1. **Formal model:** Can a formal model of "collectively prohibited action
   sequences" be defined in terms of the dependency graph?
2. **Sequence anomaly detection:** Can a lightweight sequence classifier
   detect unusual action chains at runtime without introducing unacceptable
   latency?
3. **Coverage boundary:** What is the class of ROP-analogue attacks that
   are not blocked by the current policy + `DataToControlFlowWarning`
   combination?  (Attack surface mapping required.)
4. **False positive rate:** Any sequence-level anomaly detector must have
   a low false positive rate to avoid triggering user fatigue (L4).

### Recommended Next Steps

1. Formalise the attack model: define what constitutes a "collectively
   prohibited sequence" in terms of PRD §7.1 (PI-SEC game).
2. Survey sequence anomaly detection approaches applicable to tool-call
   traces (e.g., n-gram models, LSTM-based sequence classifiers,
   rule-based pattern matchers).
3. Implement a prototype sequence monitor that operates over the audit log
   and flags suspicious action chains.
4. Evaluate on a synthetic set of ROP-analogue attack patterns.

### Notes

- Full prevention of ROP-analogue attacks is a research problem; detection
  is the near-term deliverable.
- Defence-in-depth remains the recommended deployment posture: combine CaMeL
  with model-level robustness training and a sequence monitor.

---

## FW-7 — Broader Safety Applications

| Field | Value |
|---|---|
| **ID** | FW-7 |
| **Topic** | Applying the CaMeL paradigm to broader LLM safety domains |
| **Status** | **Open** |
| **Priority** | Research / Exploratory |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

CaMeL's "security engineering" paradigm — wrapping an LLM in a principled
system architecture that enforces safety properties at the infrastructure
level rather than through model fine-tuning — may be applicable to other LLM
safety domains beyond prompt injection.

Candidate application areas:

| Domain | CaMeL analogue |
|---|---|
| **Harmful content generation** | Content policy enforcement via capability-tagged output categories |
| **Privacy-preserving agentic systems** | Data flow tracking for PII/PHI with policy-gated disclosure |
| **Multi-agent trust management** | Capability propagation across agent-to-agent communication channels |
| **Compliance enforcement** | Policy engine for regulatory constraints (GDPR, HIPAA, SOC 2) |
| **Output watermarking** | Provenance chain as a basis for tracing generated content to its inputs |

The common thread: instead of asking the LLM to *self-censor* (probabilistic,
bypassed by adversarial prompts), wrap the LLM in a system that enforces
the property *structurally* (deterministic, bypass-resistant).

### Prior Work & Context

- The original paper (Debenedetti et al.) focuses on prompt injection; this
  item explores whether the architectural pattern generalises.
- The CaMeL provenance chain (M5, `agent.get_provenance()`) is an early
  step toward output traceability.
- The three-tier policy governance model (M5, FW-5 resolved) provides a
  foundation for compliance enforcement use cases.

### Recommended Next Steps

1. Identify 2–3 safety domains with well-defined formal properties that map
   naturally to CaMeL's capability + policy model.
2. Prototype a CaMeL extension for one domain (e.g., PII data flow tracking)
   and evaluate its coverage vs. current LLM-level approaches.
3. Publish a research paper or technical report on the generalised
   "security engineering for LLM safety" paradigm.

### Notes

- This item is outside the scope of Milestone 5 and any near-term milestone.
- It is primarily a research and product vision item, not an engineering task.
- The CaMeL architecture provides a strong foundation for this direction;
  the investment in interpreter, dependency graph, capability system, and
  policy engine is directly reusable.

---

## Summary Table

| ID | Topic | Status | Priority | Related Limitation | Notes |
|---|---|---|---|---|---|
| [FW-1](#fw-1--alternative-execution-language) | Alternative execution language (Haskell/Rust DSL) | Open | Research | [L3](limitations.md#l3--residual-exception-based-side-channel) | M4 exception hardening mitigates primary vectors; language redesign is long-term |
| [FW-2](#fw-2--formal-verification) | Formal verification (Coq/Lean 4) | Open | Research | [L8](limitations.md#l8--formal-verification-gap) | No work started; strong test coverage provides interim assurance |
| [FW-3](#fw-3--security-policy-automation) | Security policy automation from tool docs | Open | Medium | [L7](limitations.md#l7--ecosystem-adoption-and-third-party-tool-annotations) | M5 policy authoring tools reduce manual burden; automation is the long-term goal |
| [FW-4](#fw-4--nested-p-llm-tool) | Nested P-LLM tool for data-requires-action | Open | High | [L1](limitations.md#l1--data-requires-action-failure) | Milestone 6 candidate; primary driver of Travel domain degradation |
| [FW-5](#fw-5--multi-party-policy-governance) | Multi-party policy governance | **Resolved** (M5) | — | [L4](limitations.md#l4--user-fatigue-from-policy-denials) | Three-tier PolicyRegistry + PolicyConflictResolver delivered |
| [FW-6](#fw-6--rop-analogue-attacks) | ROP-analogue action-chaining detection | In Progress | Medium-High | [L6](limitations.md#l6--rop-analogous-attacks-action-chaining) | DataToControlFlowWarning (M4-F15) delivered; full prevention is open research |
| [FW-7](#fw-7--broader-safety-applications) | Broader LLM safety applications | Open | Research / Exploratory | — | Exploratory; outside current milestone scope |

---

*See also:*
*[PRD §12 Open Questions & Future Work](PRD.md#12-open-questions--future-work) ·*
*[Known Limitations & Risks Register](limitations.md) ·*
*[Architecture Reference](architecture.md) ·*
*[Security Hardening Design](design/security_hardening_design.md)*

---

*Document maintained by: Software Architect Persona*
*Version: 1.0 — Milestone 5 Final*
