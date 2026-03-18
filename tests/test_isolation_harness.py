"""Isolation verification test harness — 50-run parametrised suite.

Implements the three isolation invariants from ADR-008:

I-1  No tool return value content appears in any P-LLM ``generate()``
     call across 50 execution runs (10 scenarios × 5 repeats).
I-2  No raw Q-LLM free-form response text reaches P-LLM context.
I-3  All untrusted-origin exception messages are fully redacted before
     the P-LLM retry prompt (tested via the RETRY_SCENARIO path).

Architecture
------------
Each parametrised test case:

1. Creates a fresh :class:`~tests.harness.recording_backend.StubBackend`
   returning the scenario's pre-baked plan.
2. Wraps it in a :class:`~tests.harness.recording_backend.RecordingBackend`
   spy.
3. Constructs a :class:`~camel.llm.PLLMWrapper` using the recording backend.
4. Constructs a fresh :class:`~camel.CaMeLInterpreter` with the scenario's
   tool stubs.
5. Constructs a :class:`~camel.execution_loop.CaMeLOrchestrator` and calls
   :meth:`~camel.execution_loop.CaMeLOrchestrator.run`.
6. Applies :func:`~tests.harness.isolation_assertions.assert_no_tool_value_in_messages`
   (I-1) and
   :func:`~tests.harness.isolation_assertions.assert_no_qllm_freeform_in_messages`
   (I-2) to all recorded P-LLM calls.
7. Records the outcome in the session-scoped
   :class:`~tests.harness.results_reporter.HarnessResultsReporter`.

The retry-isolation test (I-3) uses
:data:`~tests.harness.scenarios.RETRY_SCENARIO`, which forces an exception
on the first attempt so that the orchestrator sends a retry prompt.
The sentinel embedded in the tool's return value must not appear in that
retry prompt.

All LLM backends are stubs — no real API calls are required.
"""

from __future__ import annotations

from typing import Any

import pytest

from camel import CaMeLInterpreter
from camel.execution_loop import CaMeLOrchestrator, ExecutionResult
from camel.llm.p_llm import PLLMWrapper, ToolSignature
from tests.harness.isolation_assertions import (
    assert_exception_message_redacted,
    assert_no_qllm_freeform_in_messages,
    assert_no_tool_value_in_messages,
)
from tests.harness.recording_backend import RecordingBackend, StubBackend
from tests.harness.results_reporter import HarnessResultsReporter
from tests.harness.scenarios import RETRY_SCENARIO, SCENARIOS, ScenarioSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_signatures(tools: dict[str, Any]) -> list[ToolSignature]:
    """Build minimal ToolSignature list from a tools dict.

    Parameters
    ----------
    tools:
        Dict of tool name → callable as used in the scenario.

    Returns
    -------
    list[ToolSignature]
        One :class:`~camel.llm.PLLMWrapper.ToolSignature` per tool with
        a placeholder signature, return type, and description.
    """
    return [
        ToolSignature(
            name=name,
            signature="*args",
            return_type="CaMeLValue",
            description=f"Stub tool: {name}",
        )
        for name in tools
    ]


def _build_orchestrator(
    scenario: ScenarioSpec,
    recording_backend: RecordingBackend,
) -> CaMeLOrchestrator:
    """Construct a fresh CaMeLOrchestrator for a single harness run.

    Parameters
    ----------
    scenario:
        The scenario whose tools and tool signatures to use.
    recording_backend:
        The spy backend wrapping the StubBackend for this run.

    Returns
    -------
    CaMeLOrchestrator
        Fully wired orchestrator ready for a single ``run()`` call.
    """
    p_llm = PLLMWrapper(recording_backend)
    interpreter = CaMeLInterpreter(tools=scenario.tools)
    sigs = _make_tool_signatures(scenario.tools)
    return CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interpreter,
        tool_signatures=sigs,
        max_loop_retries=5,
    )


# ---------------------------------------------------------------------------
# 50-run parametrised isolation harness (I-1 + I-2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,run_idx",
    [(s, i) for s in SCENARIOS for i in range(5)],
    ids=[f"{s.scenario_id}_run{i}" for s in SCENARIOS for i in range(5)],
)
async def test_isolation_harness(
    scenario: ScenarioSpec,
    run_idx: int,
    harness_reporter: HarnessResultsReporter,
) -> None:
    """Verify I-1 and I-2 for one scenario/run combination.

    This test is parametrised over 10 scenarios × 5 repetitions = 50 cases.
    Each case:

    1. Runs the scenario against a fresh recording backend.
    2. Asserts no tool return sentinel appears in any P-LLM message (I-1).
    3. Asserts no Q-LLM raw sentinel appears in any P-LLM message (I-2).
    4. Records pass/fail counts in the session reporter.

    Parameters
    ----------
    scenario:
        The :class:`~tests.harness.scenarios.ScenarioSpec` under test.
    run_idx:
        Repetition index (0–4); included in the test ID for traceability.
    harness_reporter:
        Session-scoped results reporter.
    """
    stub = StubBackend(responses=scenario.plans, cycle=True)
    recording = RecordingBackend(stub)
    orchestrator = _build_orchestrator(scenario, recording)

    result: ExecutionResult = await orchestrator.run(
        user_query=scenario.description,
    )

    # Verify I-1: no tool return value content in P-LLM messages.
    _i1_passed = True
    try:
        assert_no_tool_value_in_messages(
            recording.recorded_calls,
            scenario.tool_sentinels,
        )
    except AssertionError:
        _i1_passed = False
        harness_reporter.record("I1_no_tool_return_in_p_llm", False)
        raise
    harness_reporter.record("I1_no_tool_return_in_p_llm", True)

    # Verify I-2: no Q-LLM free-form sentinel in P-LLM messages.
    if scenario.qllm_sentinels:
        try:
            assert_no_qllm_freeform_in_messages(
                recording.recorded_calls,
                scenario.qllm_sentinels,
            )
            harness_reporter.record("I2_no_qllm_freeform_in_p_llm", True)
        except AssertionError:
            harness_reporter.record("I2_no_qllm_freeform_in_p_llm", False)
            raise

    # Sanity check: execution result is present.
    assert isinstance(result, ExecutionResult), f"Expected ExecutionResult; got {type(result)!r}"


# ---------------------------------------------------------------------------
# Retry isolation test — I-3 (untrusted exception redaction in retry prompt)
# ---------------------------------------------------------------------------


async def test_retry_isolation_i3(
    harness_reporter: HarnessResultsReporter,
) -> None:
    """Verify I-3: retry prompt never contains untrusted exception text.

    Uses :data:`~tests.harness.scenarios.RETRY_SCENARIO`, which has:

    - Plan 1: reads a tool (setting a variable with a sentinel) then calls
      a non-existent function — raising a ``NameError``.
    - Plan 2: simple successful plan.

    After the retry, the second ``generate()`` call's messages must not
    contain the sentinel (it lives in the interpreter store as a value,
    not in the retry prompt's accepted-state section).

    Parameters
    ----------
    harness_reporter:
        Session-scoped results reporter.
    """
    scenario = RETRY_SCENARIO
    stub = StubBackend(responses=scenario.plans, cycle=False)
    recording = RecordingBackend(stub)
    orchestrator = _build_orchestrator(scenario, recording)

    _result: ExecutionResult = await orchestrator.run(
        user_query=scenario.description,
    )

    # There must be at least 2 recorded generate() calls (initial + retry).
    generate_calls = [c for c in recording.recorded_calls if c.method == "generate"]
    assert len(generate_calls) >= 2, (
        f"Expected at least 2 generate() calls (initial + retry); got {len(generate_calls)}"
    )

    # I-3: retry prompt must not contain the tool return sentinel.
    try:
        assert_no_tool_value_in_messages(
            recording.recorded_calls,
            scenario.tool_sentinels,
        )
        harness_reporter.record("I3_redaction_completeness", True)
    except AssertionError:
        harness_reporter.record("I3_redaction_completeness", False)
        raise

    # Also assert that the exception message fragment from the NameError
    # is not present as-is in user-turn messages when the store has
    # untrusted data (the tool return value from get_secret_data).
    # The exception message typically includes the function name, not the
    # tool's sentinel value — but we verify no sentinel leaks regardless.
    assert_exception_message_redacted(
        recording.recorded_calls,
        scenario.tool_sentinels,
    )


# ---------------------------------------------------------------------------
# Protocol conformance check
# ---------------------------------------------------------------------------


def test_recording_backend_satisfies_llm_backend_protocol() -> None:
    """Verify RecordingBackend satisfies the LLMBackend structural protocol.

    ADR-008 §1 requires protocol-verified mocks.  This test confirms that
    :class:`~tests.harness.recording_backend.RecordingBackend` instances
    are ``isinstance``-compatible with ``LLMBackend``.
    """
    from camel.llm.backend import LLMBackend

    stub = StubBackend(responses=["x = 1"])
    recording = RecordingBackend(stub)

    assert isinstance(recording, LLMBackend), (
        "RecordingBackend must satisfy the LLMBackend protocol for "
        "dependency-injection harness tests."
    )
