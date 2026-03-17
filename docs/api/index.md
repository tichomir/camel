# CaMeL API Reference

This directory contains the API reference documentation for all public components
through Milestone 3 (v0.3.0).

## Milestone 1 — Foundation

| Component | Module | Description |
|---|---|---|
| [Interpreter](interpreter.md) | `camel.interpreter` | `CaMeLInterpreter`, `ExecutionMode`, `EnforcementMode`, `ConsentCallback`, `AuditLogEntry`, supported grammar, `UnsupportedSyntaxError`, `PolicyViolationError`. |

## Milestone 2 — Dual LLM & Interpreter

| Component | Module | Description |
|---|---|---|
| [LLM Backend](llm_backend.md) | `camel.llm.backend` | Provider-agnostic `LLMBackend` protocol, `LLMBackendError`, and `get_backend` factory. |
| [P-LLM Wrapper](p_llm.md) | `camel.llm.p_llm` | `PLLMWrapper`, `ToolSignature`, `CodePlan`, `CodeBlockParser`, and all P-LLM exceptions. |
| [Q-LLM Wrapper](q_llm.md) | `camel.llm.qllm`, `camel.llm.schemas`, `camel.llm.exceptions` | `QLLMWrapper`, `QResponse` base schema, `NotEnoughInformationError`, and `have_enough_information` semantics. |
| [Execution Loop](execution_loop.md) | `camel.execution_loop` | `CaMeLOrchestrator`, `ExceptionRedactor`, `RetryPromptBuilder`, `TraceRecorder`, `ExecutionResult`, `TraceRecord`, `MaxRetriesExceededError`. |

## Milestone 3 — Capabilities & Policies

| Component | Module | Description |
|---|---|---|
| Capability Assignment Engine | `camel.capabilities` | `CapabilityAnnotationFn`, `default_capability_annotation`, `annotate_read_email`, `annotate_read_document`, `annotate_get_file`, `register_built_in_tools`. |
| Policy Engine & Registry | `camel.policy` | `SecurityPolicyResult`, `Allowed`, `Denied`, `PolicyFn`, `PolicyRegistry`, helper functions `is_trusted`, `can_readers_read_value`, `get_all_sources`. |
| Reference Policy Library | `camel.policy.reference_policies` | Six reference policies: `send_email`, `send_money`, `create_calendar_event`, `write_file`, `post_message`, `fetch_external_url`. `configure_reference_policies()` convenience loader. |
| Tool Registry | `camel.tools.registry` | `ToolRegistry` with `capability_annotation` support. |

---

## Related Documentation

- [System Architecture](../architecture.md)
- [ADR index](../adr/)
- [Developer Guide](../developer_guide.md)
- [Operator Guide](../manuals/operator-guide.md)
- [Reference Policy Specification](../policies/reference-policy-spec.md)
