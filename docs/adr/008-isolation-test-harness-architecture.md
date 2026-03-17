# ADR-008: Isolation Verification Test Harness Architecture

| Field         | Value                                                                             |
|---------------|-----------------------------------------------------------------------------------|
| Status        | Accepted                                                                          |
| Date          | 2026-03-17                                                                        |
| Author        | Software Architect Persona                                                        |
| Supersedes    | —                                                                                 |
| Superseded by | —                                                                                 |
| Related       | ADR-005 (P-LLM Wrapper), ADR-006 (Q-LLM Schema Injection), ADR-007 (Execution Loop) |

---

## Context

Milestone 2 exit criteria require automated verification that three isolation
invariants hold across 50 execution runs and 10 adversarial redaction scenarios:

| ID  | Invariant                                                                       |
|-----|---------------------------------------------------------------------------------|
| I-1 | **No tool return value content** appears in any `LLMBackend.generate()` call made by the P-LLM |
| I-2 | **No raw Q-LLM free-form response** (unschematised text) reaches P-LLM context |
| I-3 | **All untrusted-origin exception messages** are fully redacted before reaching the P-LLM retry prompt |

Additionally, the harness must demonstrate multi-backend swappability (Claude ↔
Gemini without code changes) and execute 10 representative end-to-end task scenarios
producing correct execution traces.

The design must be compatible with the existing test infrastructure (pytest,
`unittest.mock`, no real API calls required).

---

## Decision

### 1. Interception Mechanism: Dependency Injection (not Monkey-Patching)

**Decision:** Use **dependency injection** — pass a `RecordingBackend` spy
instance at construction time — rather than monkey-patching live objects.

**Rationale:**

| Criterion            | DI (chosen)                                              | Monkey-patch (rejected)                             |
|----------------------|----------------------------------------------------------|-----------------------------------------------------|
| Test isolation       | Each test gets a fresh spy; no shared state              | Global patches risk inter-test contamination        |
| Coupling             | Tests depend only on the `LLMBackend` protocol           | Tests depend on import paths and internal structure |
| Parallelism          | Safe — no global mutation                                | Risky under `pytest-xdist` parallel execution       |
| Readability          | Explicit — spy passed in constructor                     | Hidden — side-effect at module scope                |
| Protocol compliance  | Spy can be verified with `isinstance(spy, LLMBackend)`   | No structural guarantees                            |

The existing codebase already structures `PLLMWrapper`, `QLLMWrapper`, and
`CaMeLOrchestrator` to accept their backends via constructor — DI is the
natural extension.

### 2. RecordingBackend Spy Design

The spy implements `LLMBackend` and wraps any underlying backend (real or stub):

```python
class RecordingBackend:
    """LLMBackend spy that records all generate() calls."""

    def __init__(self, delegate: LLMBackend) -> None:
        self._delegate = delegate
        self.recorded_calls: list[RecordedCall] = []

    async def generate(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        self.recorded_calls.append(
            RecordedCall(method="generate", messages=list(messages))
        )
        return await self._delegate.generate(messages, **kwargs)

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        self.recorded_calls.append(
            RecordedCall(method="generate_structured", messages=list(messages))
        )
        return await self._delegate.generate_structured(messages, schema)
```

`RecordedCall` is a frozen dataclass holding `method: str` and
`messages: list[Message]`.  After each run the test iterates
`spy.recorded_calls` and asserts none of the recorded message content strings
contain forbidden substrings.

### 3. Isolation Invariant Assertions

Each invariant is expressed as a reusable assertion helper so it can be applied
to all 50 runs uniformly:

#### I-1: No tool return value content in P-LLM context

```python
def assert_no_tool_value_in_messages(
    recorded_calls: list[RecordedCall],
    tool_return_sentinels: list[str],
) -> None:
    """Assert none of the P-LLM generate() calls contain tool return value text."""
    for call in recorded_calls:
        for msg in call.messages:
            content = msg.get("content", "")
            for sentinel in tool_return_sentinels:
                assert sentinel not in content, (
                    f"Tool return sentinel {sentinel!r} found in "
                    f"{msg['role']!r} message"
                )
```

**Mechanism:** Each E2E scenario registers tool stubs that embed a unique
`sentinel` string (e.g. `"__TOOL_RETURN_SENTINEL_42__"`) in their return
`CaMeLValue`.  The assertion checks all recorded P-LLM messages for that
sentinel.

#### I-2: No raw Q-LLM free-form text in P-LLM context

```python
def assert_no_qllm_freeform_in_messages(
    recorded_calls: list[RecordedCall],
    qllm_raw_sentinels: list[str],
) -> None:
    """Assert no unstructured Q-LLM response text appears in P-LLM messages."""
    for call in recorded_calls:
        for msg in call.messages:
            content = msg.get("content", "")
            for sentinel in qllm_raw_sentinels:
                assert sentinel not in content, (
                    f"Q-LLM free-form sentinel {sentinel!r} found in "
                    f"{msg['role']!r} message"
                )
```

**Mechanism:** The Q-LLM stub is configured to embed a unique sentinel in its
raw `generate()` response text.  That text is consumed internally by
`QLLMWrapper` and must never propagate to the P-LLM.

#### I-3: Untrusted-origin exceptions are fully redacted

```python
def assert_exception_message_redacted(
    recorded_calls: list[RecordedCall],
    exception_message_fragments: list[str],
) -> None:
    """Assert exception message body fragments never appear in P-LLM retry prompts."""
    for call in recorded_calls:
        for msg in call.messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            for fragment in exception_message_fragments:
                assert fragment not in content, (
                    f"Redacted exception fragment {fragment!r} leaked into "
                    "P-LLM retry prompt"
                )
```

**Mechanism:** Tools are configured to raise exceptions containing a known
sensitive message fragment (e.g. `"SECRET_ERROR_BODY_7G3K"`).  After the
orchestrator classifies and redacts the error, the retry prompt is inspected
via the recording spy.

### 4. 50-Run Orchestration

The harness executes the 10 E2E scenarios 5 times each (= 50 total runs) in a
single parameterised pytest session.  This repetition validates that:

- No residual state leaks between runs (interpreter store is re-created each
  run).
- Non-deterministic mock ordering does not create spurious failures.
- The assertions hold under all scenario × attempt combinations.

Parameterisation is implemented via `@pytest.mark.parametrize` over the
scenario registry:

```python
SCENARIOS = load_scenario_registry()  # list of ScenarioSpec

@pytest.mark.parametrize("scenario,run_idx", [
    (s, i) for s in SCENARIOS for i in range(5)
])
async def test_isolation_harness(scenario: ScenarioSpec, run_idx: int) -> None:
    ...
```

Each run creates a fresh `RecordingBackend`, `CaMeLInterpreter`, and
`CaMeLOrchestrator`, runs the scenario, then applies all three invariant
assertions.

### 5. Multi-Backend Swap Approach

Backend selection is driven by a single configuration key:

```python
provider: str = os.environ.get("CAMEL_TEST_BACKEND", "mock")
```

| Value    | Backend constructed                  | Use case                           |
|----------|--------------------------------------|------------------------------------|
| `"mock"` | `StubBackend` (default, no API key)  | CI, unit tests, isolation harness  |
| `"claude"` | `ClaudeBackend` via `get_backend("claude", api_key=...)` | Integration tests against real Claude |
| `"gemini"` | `GeminiBackend` via `get_backend("gemini", api_key=...)` | Integration tests against real Gemini |

Tests that verify swappability assert:

```python
assert isinstance(backend, LLMBackend), (
    "Backend must satisfy LLMBackend protocol regardless of provider"
)
```

No test imports `ClaudeBackend` or `GeminiBackend` directly — only
`get_backend(provider)` is called, enforcing that configuration is the only
change required to switch providers.

### 6. Q-LLM Backend Separation

The Q-LLM uses a **separate** `RecordingBackend` spy instance from the P-LLM.
This allows:

- Independent assertion that the Q-LLM's `generate_structured()` calls contain
  no tool-calling capability (no `tools` parameter forwarded).
- Independent assertion that the Q-LLM's response is consumed only by
  `QLLMWrapper.extract()`, never forwarded raw to P-LLM context.

---

## Consequences

### Positive

- **No production-code changes required** — the spy slots in via existing DI
  entry points.
- **Tests are deterministic** — all LLM responses are controlled via stubs.
- **Protocol-verified mocks** — `isinstance(spy, LLMBackend)` is checked in
  each test, guarding against spy drift.
- **Extensible** — adding new isolation invariants means adding new assertion
  helpers, not restructuring the harness.

### Negative / Risks

- **Sentinel uniqueness** must be maintained across scenarios; a helper
  `make_sentinel(scenario_id, run_idx)` generates collision-free strings.
- **Q-LLM structured output** requires the stub to return valid Pydantic
  instances; scenarios must define their output schemas explicitly.
- **Async test infrastructure** (`pytest-asyncio`, `asyncio_mode = "auto"`)
  must remain in `pyproject.toml` — this is already the case per existing CI.

---

## Implementation References

| Component              | Target file                                    |
|------------------------|------------------------------------------------|
| `RecordingBackend` spy | `tests/harness/recording_backend.py`           |
| Isolation invariant helpers | `tests/harness/isolation_assertions.py`   |
| Scenario registry      | `tests/harness/scenarios.py`                   |
| 50-run parameterised test | `tests/test_isolation_harness.py`           |
| Multi-backend swap test | `tests/test_backend_swap.py`                  |
| Adversarial redaction suite | `tests/test_redaction_completeness.py`    |

See also: `docs/e2e-scenario-specification.md` for the full scenario and
adversarial case inventory.
