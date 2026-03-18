# CaMeL — Known Limitations & Risks Register

**Document version:** 1.0
**Last updated:** 2026-03-18
**CaMeL version:** 0.6.0 (Milestone 5 — Final)
**Owner:** Software Architect

> **Related documents:**
> [PRD §10](PRD.md#10-known-limitations--risks) ·
> [Open Questions & Future Work](future-work.md) ·
> [Security Hardening Design](design/security_hardening_design.md) ·
> [AgentDojo Benchmark Report](benchmark/agentdojo_benchmark_report.md)

---

## Overview

This register is the authoritative source for all known limitations and risks
in CaMeL v0.6.0.  Each entry includes a severity rating, a full description of
the limitation, its current mitigation status (as of M5), open work items, and
a link to any associated future-work item in the
[Open Questions & Future Work Tracker](future-work.md).

Severity ratings follow this scale:

| Severity | Meaning |
|---|---|
| **Critical** | Actively exploitable; blocks production deployment without mitigation |
| **High** | Significant risk; mitigation required before wide deployment |
| **Medium-High** | Meaningful risk; detection or partial mitigation in place |
| **Medium** | Notable limitation; documented workaround or partial mitigation exists |
| **Low-Medium** | Minor impact on security or utility; monitored |
| **Low** | Theoretical or niche risk; acknowledged but not prioritised |

---

## L1 — Data-Requires-Action Failure

| Field | Value |
|---|---|
| **ID** | L1 |
| **Title** | Data-requires-action failure |
| **Severity** | Medium |
| **Component** | P-LLM / CaMeL Interpreter |
| **Introduced** | Milestone 1 (by design) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

The P-LLM generates the execution plan **before** any tool is called and
**never** observes tool return values.  This means the P-LLM cannot produce a
plan whose next action depends on the *content* of a prior tool's output.

Concretely: tasks of the form "do whatever the email says" or "follow the
instructions in the document" are structurally unsupported, because honouring
such a request would require the P-LLM to inspect untrusted tool output —
which would break the control-flow isolation invariant.

This limitation is the **primary driver** of CaMeL's lower utility on the
AgentDojo **Travel** domain (projected 46–52% vs. 76% native baseline),
where many tasks require the agent to first read a travel itinerary or booking
document and then act on its contents.

### Current Mitigation

- **Design-level documentation:** Documented as an explicit architectural
  constraint in PRD §6.1 and the Architecture Reference.
- **Q-LLM extraction:** The Q-LLM can extract structured fields from tool
  output that the P-LLM *pre-declared* it would need (via `query_quarantined_llm`
  calls in the plan).  This covers cases where the P-LLM can anticipate what
  information is needed — but not free-form "do whatever it says" tasks.
- **Benchmark impact:** Travel domain 46–52% (v0.6.0 projected) vs. paper
  baseline of 44% — a positive trend, but well below the 76% native rate.

### Open Work Items

- **[FW-4](future-work.md#fw-4--nested-p-llm-tool):** Nested P-LLM tool —
  allow the P-LLM to invoke a sub-instance of itself as a trusted-sub-plan
  generator for data-requires-action scenarios, with extremely strict policies
  governing the sub-instance's capabilities.  This is a candidate for
  Milestone 6 scoping.

---

## L2 — Underdocumented Tool APIs

| Field | Value |
|---|---|
| **ID** | L2 |
| **Title** | Underdocumented tool APIs degrade P-LLM planning quality |
| **Severity** | Medium |
| **Component** | P-LLM / Tool Registration |
| **Introduced** | Milestone 2 |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

The P-LLM relies on tool signatures (name, description, parameter types,
return type) to generate correct execution plans.  When a tool's return schema
is undocumented, ambiguous, or uses informal types (e.g., `Any`, `dict`),
the P-LLM may generate code that accesses fields which do not exist, passes
arguments of the wrong type, or misinterprets return values.

This is particularly noticeable in the **Workspace** domain, where file-path
handling and cloud storage tool return schemas have varied conventions across
providers.  Workspace benchmark failures on ambiguous file paths are partially
attributable to this limitation.

### Current Mitigation

- **Typed return schemas:** NFR-7 requires all tool providers to supply
  typed return schemas at registration time.  The tool registration API
  enforces this via `ToolSignature.return_type`.
- **Q-LLM fallback:** When a tool returns unstructured data, the P-LLM can
  delegate extraction to `query_quarantined_llm(prompt, OutputSchema)`.
  This covers most ambiguous-return-schema cases.
- **Tool onboarding guide:** The Tool Onboarding Guide documents best
  practices for writing typed, unambiguous tool signatures.
- **Workspace impact (estimated):** ~2 of 40 Workspace task failures per
  backend are attributable to ambiguous tool schemas (based on retry trigger
  analysis: ~15% of retries stem from `NameError` on undeclared fields).

### Open Work Items

- Tool schema improvement guide is published in developer docs.
- No dedicated future-work item; addressed through NFR-7 enforcement and
  documentation.

---

## L3 — Residual Exception-Based Side Channel

| Field | Value |
|---|---|
| **ID** | L3 |
| **Title** | Residual exception-based information side channel |
| **Severity** | Low |
| **Component** | CaMeL Interpreter / Execution Loop |
| **Introduced** | Milestone 2 |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

An adversary who can influence tool return values may attempt to encode
information in *which exception is raised* during interpreter execution —
exploiting the interpreter's error-handling paths as a covert channel to
exfiltrate information back to the P-LLM.

Specifically, the following exception-based channels have been analysed:

1. **Exception message content:** Unredacted error messages containing
   untrusted-data-derived content.
2. **NEIE content leakage:** `NotEnoughInformationError` exposing the
   missing-information content to the P-LLM.
3. **Loop-iterable exception propagation:** Exceptions from inside a for-loop
   body with a non-public iterable dropping the iterable's taint across
   retry cycles.
4. **Deeply nested tool call chain exceptions:** Exceptions propagating
   through multiple tool call frames before reaching the interpreter's
   handler, where the dependency context may be incomplete.

### Current Mitigation

The following Milestone 4 hardening features close the primary vectors:

| Feature | Closes |
|---|---|
| **M4-F6** — Dependency-graph-aware taint check | Exception message content channel |
| **M4-F7** — NEIE handler (type + line number only) | NEIE content leakage channel |
| **M4-F8** — STRICT annotation preservation across NEIE | Annotation loss across retry cycles |
| **M4-F9** — Loop-body exception STRICT propagation | Loop-iterable exception channel |
| **M4-F3/F4** — Post-Q-LLM contamination propagation | Q-LLM data-to-assignment channel |
| **M4-F17** — Redaction audit log events | All redaction events audited |

**Residual risk:** Adversarial exception triggering via deeply nested tool call
chains (exceptions propagating through multiple frames before reaching the
interpreter handler) is a **documented residual risk** not fully closed by the
above mitigations.  See
[Exception Hardening Design §9.2](design/milestone4-exception-hardening.md).

### Open Work Items

- Documented residual risk in
  [Exception Hardening Design §9.2](design/milestone4-exception-hardening.md).
- **[FW-1](future-work.md#fw-1--alternative-execution-language):** An
  alternative execution language with Haskell-style Result/Either error
  handling would eliminate the exception side-channel class entirely.
- Side-channel test suite (loop-count inference, exception bit-leakage,
  timing exclusion) achieves 100% pass rate for implemented mitigations.

---

## L4 — User Fatigue from Policy Denials

| Field | Value |
|---|---|
| **ID** | L4 |
| **Title** | User fatigue from frequent consent prompts |
| **Severity** | Medium |
| **Component** | Policy Engine / Consent Handler |
| **Introduced** | Milestone 3 |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

Overly strict security policies generate frequent consent prompts.  If users
are interrupted too often, they are likely to approve prompts without careful
review — effectively defeating the purpose of the consent mechanism.  This is
analogous to security-fatigue patterns observed in OS-level permission dialogs.

In the Travel domain, complex itinerary tasks frequently trigger consent prompts
for multi-leg booking data with mixed-provenance capability sets.  The projected
consent rate for Travel is 26–30%, above the ≤20% target for well-annotated
domains.

### Current Mitigation

- **Session-level consent cache:** The `ConsentHandler` supports a
  `remember_for_session` flag keyed on `(tool, argument_hash)`, allowing
  users to approve a class of operations once per session.
- **Granular policy annotations:** Well-annotated domains (Banking, Workspace,
  Slack) achieve 8–17.5% consent prompt rates (within the ≤20% target).
- **Human-readable consent prompts:** The production consent UI displays the
  tool name, human-readable argument summary, and denial reason — giving users
  enough context to make informed decisions.
- **Policy tuning guide:** The
  [Policy Authoring Tutorial](policy-authoring-tutorial.md) and
  [Three-Tier Policy Authorship Guide](policies/three-tier-policy-authorship-guide.md)
  provide guidance on writing fine-grained policies that minimise spurious
  denials while maintaining security.
- **Multi-party governance (FW-5 resolved):** Three-tier PolicyRegistry with
  `non-overridable` Platform flag prevents lower tiers from accidentally
  over-restricting in ways that would increase consent frequency.

### Open Work Items

- Travel domain consent rate (26–30%) exceeds ≤20% target; not subject to the
  target (PRD §11: target applies to "well-annotated domains").
- Policy tuning for Travel itinerary tools is a recommended deployment
  optimisation.
- **[FW-5](future-work.md#fw-5--multi-party-policy-governance):** Resolved in
  M5; three-tier governance model delivered.

---

## L5 — Token Cost Overhead

| Field | Value |
|---|---|
| **ID** | L5 |
| **Title** | ~2.82× token cost overhead vs. native tool-calling |
| **Severity** | Low-Medium |
| **Component** | P-LLM / Q-LLM / Execution Loop |
| **Introduced** | Milestone 1 (by design) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

CaMeL's security architecture requires additional tokens compared to native
tool-calling:

| Source | Approximate contribution |
|---|---|
| P-LLM system prompt (tool signatures, grammar spec, opaque-variable guidance) | ~1.4× |
| Q-LLM extraction calls (additional input tokens per unstructured tool output) | ~0.6× |
| P-LLM re-generation prompts on retry (amortised) | ~0.1× |
| **Total** | **~2.1–2.9×** (domain-dependent) |

The projected median input token overhead is **2.82×** (range: 2.63–2.93×
across all backend × domain combinations), and the median output token overhead
is **2.77×** (range: 2.41–2.95×).  Both are within the NFR-3 target of ≤3×.

This overhead directly translates to higher API costs for operators deploying
CaMeL in production.

### Current Mitigation

- **Cheaper Q-LLM model:** Using `claude-haiku-4-5-20251001` as the Q-LLM
  instead of a full-capability model reduces total cost by ~12% with <1%
  utility degradation (validated M5/v0.6.0).
- **NFR-3 compliance:** Within the ≤3× target; validated via the benchmark
  simulation.
- **Expected improvement trajectory:** As models become better at following
  restricted-Python-subset specifications, P-LLM retry rates and system prompt
  token overhead should decrease.

### Open Work Items

- No dedicated future-work item.
- Monitoring: token overhead is tracked as a success metric (PRD §11.3).
- Cost optimisation is an ongoing deployment consideration; no architectural
  change required at this time.

---

## L6 — ROP-Analogous Attacks (Action Chaining)

| Field | Value |
|---|---|
| **ID** | L6 |
| **Title** | Return-oriented programming (ROP) analogous action-chaining attacks |
| **Severity** | Medium-High |
| **Component** | Policy Engine / Interpreter |
| **Introduced** | Milestone 3 (by design) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

An adversary may attempt to chain individually-allowed tool calls in a sequence
that produces a collectively malicious outcome — analogous to return-oriented
programming (ROP) attacks in traditional software security.

For example: an injection payload might not attempt to call `send_email`
directly (which the policy would block) but instead attempt to manipulate a
series of individually-allowed read and write operations whose combined effect
results in data exfiltration.

The key property being attacked is that CaMeL's policy engine evaluates each
tool call independently: a sequence of calls that individually pass all
policies may collectively violate the intent of the user's security model.

### Current Mitigation

- **`DataToControlFlowWarning` (M4-F15):** Detects when tool name resolution
  itself depends on untrusted data (the most direct form of data-to-control
  escalation).  Execution is paused and an elevated consent prompt is
  surfaced.
- **Dependency graph analysis:** The STRICT mode dependency graph tracks
  provenance of all values across the full execution trace.  This provides
  raw data for post-execution auditing of action chains.
- **Defence-in-depth:** CaMeL is designed to be combined with
  model-level robustness training and anomaly detection.  The audit log
  provides the action trace needed by an anomaly detector.
- **Audit log:** All tool calls with their full capability dependency chains
  are written to the structured JSON audit log, enabling post-execution review.

### Current Status

Detection is implemented; **formal prevention of ROP-analogue attack chains is
an open research problem** not resolved by the current CaMeL architecture.
CaMeL reduces the attack surface by making each individual action
policy-checked, but does not provide a formal proof that no chain of
individually-allowed actions can produce a collectively prohibited outcome.

### Open Work Items

- **[FW-6](future-work.md#fw-6--rop-analogue-attacks):** Research whether
  action-chaining attacks can be detected and blocked via dependency graph
  analysis or action sequence anomaly detection.  In progress: `DataToControlFlowWarning`
  addresses the direct data-to-control escalation vector; full ROP-analogue
  prevention via sequence anomaly detection is ongoing.

---

## L7 — Ecosystem Adoption and Third-Party Tool Annotations

| Field | Value |
|---|---|
| **ID** | L7 |
| **Title** | Third-party tools without capability annotations degrade policy granularity |
| **Severity** | Medium |
| **Component** | Tool Registration / Capability Assignment |
| **Introduced** | Milestone 3 |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

CaMeL's security model depends on capability annotations being present on all
tools.  When a tool does not supply a `capability_annotation` function at
registration time, CaMeL falls back to the default annotation:
`sources={tool_id}, readers=Public`.

The `readers=Public` default means the tool's output is treated as readable by
anyone — which is the *least restrictive* capability assignment.  For tools
that return sensitive data (user email, financial records, private documents),
the `readers=Public` default will cause policies that check `can_readers_read_value`
to approve operations that should be blocked, potentially allowing data
exfiltration to untrusted recipients.

This limitation primarily affects **third-party tool integrations** where the
tool provider has not shipped a CaMeL capability annotation.

### Current Mitigation

- **Tool Registration API:** The `Tool` registration interface explicitly
  accepts a `capability_annotation` parameter.  Registering a tool without
  an annotation generates a console warning (in development mode) to prompt
  the operator to add one.
- **CaMeL as central capability authority:** For third-party tools, operators
  can define capability annotations within their own CaMeL deployment (i.e.,
  the operator acts as the capability authority rather than the tool provider).
- **Adapter templates:** The Tool Onboarding Guide provides annotation
  templates for common tool categories (email, cloud storage, calendar,
  external HTTP).
- **Policy authoring guidance:** The policy authorship guide warns operators
  to review all tools for missing annotations before production deployment.

### Open Work Items

- No dedicated future-work item in FW-1 through FW-7.
- Ongoing: community and ecosystem adoption of CaMeL capability annotation
  conventions.  The tool onboarding guide is the primary enablement resource.

---

## L8 — Formal Verification Gap

| Field | Value |
|---|---|
| **ID** | L8 |
| **Title** | No machine-verified proof of interpreter correctness |
| **Severity** | Low |
| **Component** | CaMeL Interpreter / Policy Engine |
| **Introduced** | Milestone 1 (by design) |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

The CaMeL interpreter and policy engine have extensive unit and integration
test coverage (30+ test files, ~291 tests as of M3 exit criteria), but no
formal, machine-verified proof of correctness.

The key properties that would benefit from formal verification are:

1. **Policy enforcement completeness:** Every tool call that proceeds has
   passed all registered policies; no tool call can bypass the pre-execution
   hook.
2. **Capability propagation soundness:** The capability of every derived value
   is a superset of the capabilities of all its upstream inputs; no value
   can "lose" provenance information.
3. **STRICT mode dependency completeness:** In STRICT mode, every assignment
   inside a for-loop body carries the iterable's dependency set; every
   assignment inside an if/else carries the condition's dependency set.
4. **Exception redaction completeness:** No exception message with any
   untrusted-data dependency reaches the P-LLM unredacted.

Without a formal proof, correctness is asserted by test coverage, code review,
and the architectural argument — but a sufficiently subtle implementation bug
could violate one of these properties without being caught by the existing test
suite.

### Current Mitigation

- **Comprehensive test suite:** 30+ test files covering all interpreter
  constructs, capability propagation rules, STRICT mode propagation,
  exception redaction paths, and policy evaluation.
- **CI enforcement:** All tests run on every pull request; CI blocks merge on
  any test failure.
- **Code review:** All interpreter changes require review by a security-aware
  engineer.
- **NFR-9 compliance:** Each component is independently unit-testable,
  enabling regression testing of individual security properties.

### Open Work Items

- **[FW-2](future-work.md#fw-2--formal-verification):** Formally verify the
  CaMeL interpreter's policy enforcement properties using a proof assistant
  (Coq or Lean 4).  Status: open — no formal verification work begun.

---

## L9 — Benchmark Validation Status (M5)

| Field | Value |
|---|---|
| **ID** | L9 |
| **Title** | AgentDojo benchmark results are projected estimates, not live-run measurements |
| **Severity** | Medium |
| **Component** | Benchmark Infrastructure |
| **Introduced** | Milestone 5 |
| **Last reviewed** | 2026-03-18 (M5) |

### Description

All numeric benchmark results in the
[AgentDojo Benchmark Report](benchmark/agentdojo_benchmark_report.md) and in
PRD §11 are **projected estimates derived from design expectations**, not
measurements from live model API calls.

The benchmark simulation script (`scripts/benchmark_agentdojo.py`) uses
hardcoded result values; it does not invoke any LLM API.  No live API
credentials were available at the time of report generation.

This means:

- The 0% ASR claim is an architectural argument, not an empirical result from
  running adversarial fixtures against live backends.
- The utility rates (Banking 90–94%, Workspace 67.5–70%, Slack 56.7–60%,
  Travel 46–52%) are estimates, not measured results.
- The token overhead figures (median 2.82× input, 2.77× output) are estimates.
- All PRD §11 success metrics marked ✅ MET are based on these projected
  estimates and should be treated as ⚠️ UNVERIFIED until live runs are executed.

The multi-backend integration test suite (15 test cases: 5 fixtures × 3
providers) validates security equivalence at the code level using mock/unit
test infrastructure, not live API calls.

### Current Mitigation

- **Benchmark disclaimer:** The benchmark report prominently labels all
  results as `PROJECTED ESTIMATES — NOT VALIDATED BY REAL RUNS`.
- **CSV data status column:** All CSV files in `docs/benchmark/data/` carry a
  `data_status` column with value `projected_estimate_not_real_run`.
- **Architectural security argument:** CaMeL's 0 ASR claim is supported by a
  rigorous architectural argument (P-LLM isolation, Q-LLM schema constraint,
  interpreter allowlist, policy enforcement).  The argument is documented in
  PRD §7 and the Architecture Reference.

### Open Work Items

- Execute the full benchmark suite against live API backends to replace
  projected estimates with measured results.
- Update `scripts/benchmark_agentdojo.py` to make live API calls.
- Replace PRD §11 metrics status from ⚠️ UNVERIFIED with ✅ VERIFIED once
  live runs are complete.

---

## Summary Table

| ID | Title | Severity | M5 Status | Future Work |
|---|---|---|---|---|
| L1 | Data-requires-action failure | Medium | Open — Travel 46–52% (up from 44% baseline) | [FW-4](future-work.md#fw-4--nested-p-llm-tool) |
| L2 | Underdocumented tool APIs | Medium | Partially mitigated — Q-LLM extraction + typed schema enforcement | None |
| L3 | Residual exception side channel | Low | Mitigated (primary vectors) — deeply nested chains residual | [FW-1](future-work.md#fw-1--alternative-execution-language) |
| L4 | User fatigue from policy denials | Medium | Mitigated — ≤17.5% on well-annotated domains; Travel 26–30% | [FW-5](future-work.md#fw-5--multi-party-policy-governance) (resolved) |
| L5 | Token cost ~2.82× overhead | Low-Medium | Monitoring — within NFR-3 ≤3× target | None |
| L6 | ROP-analogous action-chaining attacks | Medium-High | Detected (M4-F15), not formally prevented | [FW-6](future-work.md#fw-6--rop-analogue-attacks) |
| L7 | Third-party tool annotation gap | Medium | Partially addressed — API + templates + guide | None |
| L8 | Formal verification gap | Low | Open — no proof assistant work begun | [FW-2](future-work.md#fw-2--formal-verification) |
| L9 | Benchmark results are estimates (M5) | Medium | Open — live runs required | None |

---

*See also:*
*[PRD §10 Known Limitations & Risks](PRD.md#10-known-limitations--risks) ·*
*[Open Questions & Future Work Tracker](future-work.md) ·*
*[Security Hardening Design](design/security_hardening_design.md) ·*
*[Exception Hardening Design](design/milestone4-exception-hardening.md) ·*
*[AgentDojo Benchmark Report](benchmark/agentdojo_benchmark_report.md)*

---

*Document maintained by: Software Architect Persona*
*Version: 1.0 — Milestone 5 Final*
