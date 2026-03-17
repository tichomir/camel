# CaMeL API Reference

This directory contains the API reference documentation for all public M2
components.

| Component | Module | Description |
|---|---|---|
| [LLM Backend](llm_backend.md) | `camel.llm.backend` | Provider-agnostic `LLMBackend` protocol, `LLMBackendError`, and `get_backend` factory. |
| [P-LLM Wrapper](p_llm.md) | `camel.llm.p_llm` | `PLLMWrapper`, `ToolSignature`, `CodePlan`, `CodeBlockParser`, and all P-LLM exceptions. |
| [Q-LLM Wrapper](q_llm.md) | `camel.llm.qllm`, `camel.llm.schemas`, `camel.llm.exceptions` | `QLLMWrapper`, `QResponse` base schema, `NotEnoughInformationError`, and `have_enough_information` semantics. |
| [Interpreter](interpreter.md) | `camel.interpreter` | `CaMeLInterpreter`, `ExecutionMode`, supported grammar, `UnsupportedSyntaxError`, `PolicyViolationError`. |
| [Execution Loop](execution_loop.md) | `camel.execution_loop` | `CaMeLOrchestrator`, `ExceptionRedactor`, `RetryPromptBuilder`, `TraceRecorder`, `ExecutionResult`, `TraceRecord`, `MaxRetriesExceededError`. |

---

## Related Documentation

- [System Architecture](../architecture.md)
- [ADR index](../adr/)
- [Developer Guide](../developer_guide.md)
- [Operator Guide](../manuals/operator-guide.md)
