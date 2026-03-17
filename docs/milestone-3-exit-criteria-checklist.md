# Milestone 3 — Exit Criteria Checklist

_Generated: 2026-03-17 | Validated by: `tests/test_capability_assignment.py`, `tests/test_policy.py`, `tests/test_policy_harness.py`, `tests/test_e2e_enforcement.py`_

This document maps each Milestone 3 exit criterion to the test function(s) that provide
evidence of satisfaction, together with the expected evidence and current pass/fail status.

---

## EC-M3-1 — CaMeLValue Capability Assignment for All Tool Returns

| Field | Value |
|-------|-------|
| **Criterion** | Every value returned by a registered tool is wrapped in a `CaMeLValue` with `sources`, `inner_source`, and `readers` fields populated by the capability annotation function. |
| **Test file** | `tests/test_capability_assignment.py` |
| **Test classes** | `TestDefaultCapabilityAnnotation`, `TestToolRegistryDefaultAnnotation`, `TestToolRegistryCustomAnnotation` |
| **Key test functions** | `TestDefaultCapabilityAnnotation::test_sources_set_to_tool_name`, `TestDefaultCapabilityAnnotation::test_readers_defaults_to_public`, `TestToolRegistryCustomAnnotation::test_custom_annotation_is_invoked` |
| **Evidence** | `default_capability_annotation()` wraps tool returns with `sources={tool_id}` and `readers=Public`. Custom annotations override this. All 84 tests in `test_capability_assignment.py` pass. |
| **Status** | ✅ PASS |

---

## EC-M3-2 — Default Capability Annotation for Unannotated Tools

| Field | Value |
|-------|-------|
| **Criterion** | Tools registered without an explicit `capability_annotation` parameter automatically receive `sources={tool_id}` and `readers=Public` via the default annotation logic. |
| **Test file** | `tests/test_capability_assignment.py` |
| **Test class** | `TestToolRegistryDefaultAnnotation` |
| **Key test functions** | `TestToolRegistryDefaultAnnotation::test_default_annotation_sets_tool_id_as_source`, `TestToolRegistryDefaultAnnotation::test_default_annotation_readers_is_public` |
| **Evidence** | Confirmed: calling a tool registered with no annotation produces a `CaMeLValue` with `sources=frozenset({tool_id})` and `readers is Public`. |
| **Status** | ✅ PASS |

---

## EC-M3-3 — `read_email` Capability Annotation (Inner Source and Per-Field Tagging)

| Field | Value |
|-------|-------|
| **Criterion** | `annotate_read_email` tags the sender field as `inner_source`, wraps body and subject separately, and propagates correct capability metadata for each field. |
| **Test file** | `tests/test_capability_assignment.py` |
| **Test class** | `TestAnnotateReadEmail` |
| **Key test functions** | `TestAnnotateReadEmail::test_sender_tagged_as_inner_source`, `TestAnnotateReadEmail::test_body_wrapped_separately`, `TestAnnotateReadEmail::test_subject_wrapped_separately` |
| **Evidence** | All `annotate_read_email` scenarios pass, including sender `inner_source` tagging, per-field wrapping, and correct capability propagation. |
| **Status** | ✅ PASS |

---

## EC-M3-4 — Cloud Storage Capability Annotation (Permission-Based Readers)

| Field | Value |
|-------|-------|
| **Criterion** | `annotate_read_document` and `annotate_get_file` populate the `readers` field from document sharing permissions as a `Set[str]` or `Public`. |
| **Test file** | `tests/test_capability_assignment.py` |
| **Test class** | `TestAnnotateCloudStorage` |
| **Key test functions** | `TestAnnotateCloudStorage::test_readers_populated_from_permissions`, `TestAnnotateCloudStorage::test_public_document_readers_is_public`, `TestAnnotateCloudStorage::test_restricted_document_has_named_readers` |
| **Evidence** | Confirmed: cloud storage annotations correctly produce `readers=Public` for public documents and `readers=frozenset({permitted_emails})` for restricted documents. |
| **Status** | ✅ PASS |

---

## EC-M3-5 — `SecurityPolicyResult` Sealed Type (Allowed / Denied)

| Field | Value |
|-------|-------|
| **Criterion** | `SecurityPolicyResult` is a sealed type with only `Allowed()` and `Denied(reason)` as valid variants. `Allowed.is_allowed()` returns `True`; `Denied.is_allowed()` returns `False`. `Denied` carries a non-empty `reason` string. |
| **Test file** | `tests/test_policy.py` |
| **Test class** | `TestSecurityPolicyResultSealing` |
| **Key test functions** | `test_allowed_is_allowed`, `test_denied_is_not_allowed`, `test_denied_carries_reason`, `test_cannot_subclass_outside_module` |
| **Evidence** | All 14 `TestSecurityPolicyResultSealing` tests pass. Subclassing outside the module raises `TypeError`. |
| **Status** | ✅ PASS |

---

## EC-M3-6 — `PolicyRegistry` Registration and Evaluation Interface

| Field | Value |
|-------|-------|
| **Criterion** | `PolicyRegistry.register(tool_name, policy_fn)` stores the policy. `PolicyRegistry.evaluate(tool_name, kwargs)` invokes all registered policies and returns the first `Denied` or `Allowed` if all pass. |
| **Test file** | `tests/test_policy.py` |
| **Test classes** | `TestPolicyRegistryRegister`, `TestPolicyRegistryEvaluate` |
| **Key test functions** | `test_register_stores_policy`, `test_register_multiple_policies_accumulate`, `test_no_policies_returns_allowed`, `test_single_deny_returns_denied`, `test_first_deny_short_circuits` |
| **Evidence** | 8 registration tests and 11 evaluation tests pass. First-`Denied` short-circuit confirmed via `test_first_deny_short_circuits`. |
| **Status** | ✅ PASS |

---

## EC-M3-7 — Multi-Policy Composition (All-Must-Allow)

| Field | Value |
|-------|-------|
| **Criterion** | When multiple policies are registered for the same tool, every policy must return `Allowed` for execution to proceed. The first `Denied` short-circuits evaluation and blocks the tool call. |
| **Test file** | `tests/test_policy.py`, `tests/test_e2e_enforcement.py` |
| **Test classes** | `TestPolicyRegistryEvaluate`, `TestMultiPolicyComposition` |
| **Key test functions** | `TestPolicyRegistryEvaluate::test_all_allow_returns_allowed`, `TestPolicyRegistryEvaluate::test_first_deny_short_circuits`, `TestMultiPolicyComposition::test_both_policies_must_allow`, `TestMultiPolicyComposition::test_first_denial_short_circuits_evaluation` |
| **Evidence** | Confirmed across unit and integration tests: all policies must return `Allowed`; single `Denied` from any policy blocks execution. |
| **Status** | ✅ PASS |

---

## EC-M3-8 — Policy Helper Functions (`is_trusted`, `can_readers_read_value`, `get_all_sources`)

| Field | Value |
|-------|-------|
| **Criterion** | Three helper functions are implemented and exported: `is_trusted(value)` returns `True` iff all sources are in `{User literal, CaMeL}`; `can_readers_read_value(reader, value)` returns `True` iff reader is in `readers` or `readers is Public`; `get_all_sources(value)` returns the complete `frozenset[str]` of sources. |
| **Test file** | `tests/test_policy.py` |
| **Test classes** | `TestIsTrusted`, `TestCanReadersReadValue`, `TestGetAllSources` |
| **Key test functions** | `test_user_literal_is_trusted`, `test_tool_output_is_not_trusted`, `test_mixed_trusted_and_untrusted_is_not_trusted`, `test_public_readers_allows_any_reader`, `test_reader_not_in_set_is_denied`, `test_returns_sources_frozenset` |
| **Evidence** | All helper function tests pass. Confirmed: mixed trusted/untrusted source taints `is_trusted` to `False`; `Public` readers allows any reader string. |
| **Status** | ✅ PASS |

---

## EC-M3-9 — Six Reference Security Policies

All six reference policies are implemented, registered, and independently testable.

| Policy | Test class | Key positive test | Key negative test | Status |
|--------|-----------|-------------------|-------------------|--------|
| `send_email_policy` | `TestE2EPipelineSendEmail` | `test_happy_path_trusted_recipient_executes_tool` | `test_attack_path_injected_recipient_raises_policy_violation` | ✅ PASS |
| `send_money_policy` | `TestE2EPipelineSendMoney` | `test_happy_path_trusted_recipient_and_amount` | `test_attack_path_injected_recipient`, `test_attack_path_injected_amount` | ✅ PASS |
| `create_calendar_event_policy` | `TestE2EPipelineCreateCalendarEvent` | `test_happy_path_trusted_participants` | `test_attack_path_injected_participants_private_title` | ✅ PASS |
| `make_write_file_policy` (write_file) | `TestE2EPipelineWriteFile` | `test_happy_path_trusted_path_owner_readable_content`, `test_happy_path_public_content` | `test_attack_path_injected_path`, `test_attack_path_content_not_readable_by_owner` | ✅ PASS |
| `post_message_policy` | `TestE2EPipelinePostMessage` | `test_happy_path_trusted_channel_trusted_message` | `test_attack_path_injected_channel`, `test_attack_path_private_untrusted_message` | ✅ PASS |
| `fetch_external_url_policy` | `TestE2EPipelineFetchExternalUrl` | `test_happy_path_trusted_url` | `test_attack_path_injected_url`, `test_attack_path_injected_params`, `test_attack_path_injected_body` | ✅ PASS |

**All six reference policies: ✅ PASS**

---

## EC-M3-10 — NFR-2 Compliance: Synchronous, Deterministic Policy Evaluation with No LLM Calls

| Field | Value |
|-------|-------|
| **Criterion** | Policy evaluation must be synchronous, deterministic, and contain zero LLM calls. `evaluate()` must not return a coroutine. Results must be identical across 100 successive invocations with the same inputs. |
| **Test files** | `tests/test_policy.py`, `tests/test_e2e_enforcement.py` |
| **Test classes** | `TestNFR2Compliance` (in `test_policy.py`), `TestNFR2Determinism` (in `test_e2e_enforcement.py`) |
| **Key test functions** | `TestNFR2Compliance::test_evaluate_returns_non_coroutine`, `TestNFR2Compliance::test_registry_uses_no_llm_infrastructure`, `TestNFR2Determinism::test_allowed_result_is_deterministic_across_100_runs`, `TestNFR2Determinism::test_zero_llm_calls_during_policy_evaluation`, `TestNFR2Determinism::test_denial_reasons_are_identical_across_100_runs` |
| **Evidence** | `evaluate()` returns a concrete `SecurityPolicyResult` (not a coroutine). No LLM imports or async calls detected in policy evaluation path. Results identical across 100 runs. |
| **Status** | ✅ PASS |

---

## EC-M3-11 — NFR-4 Compliance: Interpreter Overhead ≤100ms per Tool Call

| Field | Value |
|-------|-------|
| **Criterion** | Median (p50) and 95th-percentile (p95) interpreter overhead (including capability assignment and policy evaluation) must be ≤100ms per tool call. |
| **Test file** | `tests/test_e2e_enforcement.py` |
| **Test class** | `TestNFR4Performance` |
| **Key test functions** | `test_p50_interpreter_overhead_within_budget`, `test_p95_interpreter_overhead_within_budget`, `test_benchmark_report_emitted` |
| **Evidence** | p50 and p95 measured over 1,000 synthetic tool calls including policy evaluation. Both values confirmed ≤100ms. Benchmark report emitted to stdout. |
| **Status** | ✅ PASS |

---

## EC-M3-12 — NFR-6 Compliance: Security Audit Log

| Field | Value |
|-------|-------|
| **Criterion** | All policy evaluation outcomes (tool name, policy name, result, reason) and user consent decisions (approved/rejected) are written to the security audit log. |
| **Test files** | `tests/test_policy.py`, `tests/test_e2e_enforcement.py` |
| **Test classes** | `TestInterpreterAuditLog` (in `test_policy.py`), `TestAuditLogCompleteness`, `TestAuditLogTimestamps` (in `test_e2e_enforcement.py`) |
| **Key test functions** | `TestInterpreterAuditLog::test_allowed_call_appends_audit_entry`, `TestInterpreterAuditLog::test_denied_call_appends_audit_entry`, `TestAuditLogCompleteness::test_ten_calls_produce_ten_audit_entries`, `TestAuditLogCompleteness::test_all_entries_have_required_fields`, `TestAuditLogCompleteness::test_five_allowed_five_denied_entries`, `TestAuditLogTimestamps::test_timestamps_are_chronologically_ordered`, `TestAuditLogTimestamps::test_timestamp_is_iso8601_string` |
| **Evidence** | Confirmed: every tool call produces exactly one audit log entry. Entries include `tool_name`, `outcome`, `reason`, `timestamp` (ISO-8601), and `consent_decision`. Allowed and denied entries both logged correctly. |
| **Status** | ✅ PASS |

---

## EC-M3-13 — NFR-9 Compliance: Independent Testability of Each Component

| Field | Value |
|-------|-------|
| **Criterion** | The policy engine, capability system, and enforcement hook must each be independently unit-testable without requiring the other components. |
| **Test file** | `tests/test_e2e_enforcement.py` |
| **Test class** | `TestNFR9Independence` |
| **Key test functions** | `test_policy_engine_instantiates_without_interpreter`, `test_policy_engine_denies_without_interpreter`, `test_all_six_policies_evaluate_in_isolation`, `test_capability_system_without_interpreter_or_policy_engine`, `test_enforcement_hook_in_isolation_evaluation_mode`, `test_policy_function_itself_requires_no_other_components` |
| **Evidence** | All three components (`PolicyRegistry`, `CaMeLValue`/capability system, enforcement hook) instantiate and evaluate correctly without the other two. Six reference policies each evaluated standalone with no interpreter dependency. |
| **Status** | ✅ PASS |

---

## EC-M3-14 — Production Mode Consent Flow

| Field | Value |
|-------|-------|
| **Criterion** | In production mode, a policy denial triggers a user consent prompt (displaying tool name, argument summary, and denial reason). Tool executes on approval; raises `PolicyViolationError` on rejection. Consent decision is recorded in the audit log. |
| **Test file** | `tests/test_e2e_enforcement.py` |
| **Test class** | `TestProductionModeConsentFlow` |
| **Key test functions** | `test_approval_path_tool_executes_after_consent`, `test_approval_path_audit_entry_has_user_approved_decision`, `test_rejection_path_raises_policy_violation_error`, `test_rejection_path_audit_entry_has_user_rejected_decision`, `test_rejection_path_tool_is_not_called`, `test_consent_callback_receives_correct_arguments`, `test_production_mode_requires_consent_callback`, `test_allowed_calls_do_not_invoke_consent_callback` |
| **Evidence** | Full approval and rejection paths verified. Consent callback receives correct `tool_name`, `kwargs_summary`, and `denial_reason`. Audit log records `user_approved` / `user_rejected` correctly. Tool not called on rejection. Allowed calls bypass consent callback entirely. |
| **Status** | ✅ PASS |

---

## EC-M3-15 — Policy Testing Harness (AgentDojo Scenario Replay)

| Field | Value |
|-------|-------|
| **Criterion** | A policy testing harness is delivered with utilities for: constructing trusted/untrusted/mixed `CaMeLValue` inputs; asserting `Allowed`/`Denied` outcomes with optional reason checks; replaying AgentDojo adversarial scenarios through the interpreter enforcement hook; and recording audit log entries during replay. |
| **Test file** | `tests/test_policy_harness.py` |
| **Test classes** | `TestMakeTrustedValue`, `TestMakeUntrustedValue`, `TestMakeMixedValue`, `TestAssertAllowed`, `TestAssertDenied`, `TestAssertPolicyAllowed`, `TestAssertPolicyDenied`, `TestReplayAgentdojoScenario`, `TestReplayThroughHook`, `TestAuditLogEntryContent` |
| **Key test functions** | `TestMakeTrustedValue::test_is_trusted_returns_true`, `TestAssertDenied::test_passes_when_policy_returns_denied`, `TestAssertDenied::test_expected_reason_passes`, `TestReplayAgentdojoScenario::test_passes_for_send_email_injected_recipient`, `TestReplayAgentdojoScenario::test_passes_for_send_money_injected_amount`, `TestReplayAgentdojoScenario::test_passes_for_fetch_external_url_injected_url` |
| **Evidence** | 68 harness tests pass. AgentDojo replay scenarios confirm injected-recipient, injected-amount, and injected-URL attacks are all denied through the interpreter hook. Audit log entry content verified per scenario. |
| **Status** | ✅ PASS |

---

## EC-M3-16 — Configuration-Driven Policy Loading (`PolicyRegistry.load_from_env`)

| Field | Value |
|-------|-------|
| **Criterion** | Policy definitions must be externalisable per-deployment via environment variable configuration without modifying core code. `PolicyRegistry.load_from_env()` must discover and register policies from an externally specified module. |
| **Test file** | `tests/test_policy.py` |
| **Test class** | `TestLoadFromEnv` |
| **Key test functions** | `test_empty_env_var_returns_empty_registry`, `test_unset_env_var_returns_empty_registry`, `test_load_from_env_calls_configure_policies`, `test_load_from_env_raises_import_error_for_missing_module`, `test_policies_loaded_from_env_are_evaluated` |
| **Evidence** | Confirmed: `load_from_env()` discovers the `configure_policies` callable from the module specified by the environment variable, calls it with the registry, and the resulting policies are evaluated correctly on subsequent tool calls. Missing or empty env var yields an empty registry (no error). |
| **Status** | ✅ PASS |

---

## Summary

| Criterion | Test file(s) | Approx. test count | Status |
|-----------|-------------|-------------------|--------|
| EC-M3-1 — Capability assignment for all tool returns | `test_capability_assignment.py` | 84 | ✅ PASS |
| EC-M3-2 — Default annotation for unannotated tools | `test_capability_assignment.py` | subset | ✅ PASS |
| EC-M3-3 — `read_email` annotation (inner source, per-field) | `test_capability_assignment.py` | subset | ✅ PASS |
| EC-M3-4 — Cloud storage annotation (permission-based readers) | `test_capability_assignment.py` | subset | ✅ PASS |
| EC-M3-5 — `SecurityPolicyResult` sealed type | `test_policy.py` | 14 | ✅ PASS |
| EC-M3-6 — `PolicyRegistry` register and evaluate | `test_policy.py` | 19 | ✅ PASS |
| EC-M3-7 — Multi-policy composition (all-must-allow) | `test_policy.py`, `test_e2e_enforcement.py` | 6 | ✅ PASS |
| EC-M3-8 — Helper functions (`is_trusted`, `can_readers_read_value`, `get_all_sources`) | `test_policy.py` | 17 | ✅ PASS |
| EC-M3-9 — Six reference security policies | `test_e2e_enforcement.py` | 20 | ✅ PASS |
| EC-M3-10 — NFR-2: Synchronous, deterministic, no LLM calls | `test_policy.py`, `test_e2e_enforcement.py` | 9 | ✅ PASS |
| EC-M3-11 — NFR-4: Interpreter overhead ≤100ms | `test_e2e_enforcement.py` | 3 | ✅ PASS |
| EC-M3-12 — NFR-6: Security audit log completeness | `test_policy.py`, `test_e2e_enforcement.py` | 12 | ✅ PASS |
| EC-M3-13 — NFR-9: Independent component testability | `test_e2e_enforcement.py` | 9 | ✅ PASS |
| EC-M3-14 — Production mode consent flow | `test_e2e_enforcement.py` | 8 | ✅ PASS |
| EC-M3-15 — Policy testing harness (AgentDojo replay) | `test_policy_harness.py` | 68 | ✅ PASS |
| EC-M3-16 — Config-driven policy loading (`load_from_env`) | `test_policy.py` | 5 | ✅ PASS |
| **Total** | 4 test files | **~291 tests** | **✅ ALL PASS** |

---

_Note: Test counts are based on `def test_` function counts across the four M3 test files:
`test_capability_assignment.py` (84), `test_policy.py` (85), `test_policy_harness.py` (68),
`test_e2e_enforcement.py` (54). Individual criterion rows show the primary test class or subset
responsible for that criterion; totals include all tests in the respective files._
