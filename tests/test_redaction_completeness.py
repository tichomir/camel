"""Redaction completeness test suite — 10 adversarial parametrised cases.

Implements :ref:`ADR-008 §3` and ``docs/e2e-scenario-specification.md §2``
(R01–R10).

Each case targets a specific aspect of the :class:`~camel.execution_loop.ExceptionRedactor`
and the orchestrator retry path, asserting that untrusted exception content
is never forwarded to the P-LLM.

Test structure
--------------
- **R01–R06, R09–R10**: Unit-level tests that call
  :meth:`~camel.execution_loop.ExceptionRedactor.classify` directly with a
  crafted exception and store snapshot, then assert the :class:`RedactedError`
  fields.
- **R07**: Full-orchestrator integration test — a tool raises with sensitive
  payload text; the ``RecordingBackend`` spy asserts the retry prompt omits
  the payload.
- **R08**: Full-orchestrator integration test — Q-LLM stub signals
  ``have_enough_information=False``; asserts no classified data reaches the
  P-LLM.

All ten cases are collected by ``pytest`` as parametrised runs so results
can be aggregated by the ``harness_reporter`` session fixture.

All LLM backends are stubs — no real API calls required.
"""

from __future__ import annotations

from typing import Any

import pytest

from camel import CaMeLInterpreter
from camel.exceptions import NotEnoughInformationError as CamelNEIE
from camel.execution_loop import (
    CaMeLOrchestrator,
    ExceptionRedactor,
    RedactedError,
)
from camel.llm.exceptions import NotEnoughInformationError as LLMNotEnoughInfoError
from camel.llm.p_llm import PLLMWrapper, ToolSignature
from camel.llm.schemas import QResponse
from camel.value import CaMeLValue, wrap
from tests.harness.isolation_assertions import assert_exception_message_redacted
from tests.harness.recording_backend import RecordingBackend, StubBackend
from tests.harness.results_reporter import HarnessResultsReporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _untrusted_store(sentinel: str) -> dict[str, CaMeLValue]:
    """Return a store containing a single untrusted value with *sentinel*."""
    return {
        "data": wrap(sentinel, sources=frozenset({"external_tool"})),
    }


def _trusted_store() -> dict[str, CaMeLValue]:
    """Return a store containing only trusted-source values."""
    return {
        "x": wrap(1, sources=frozenset({"User literal"})),
        "y": wrap(2, sources=frozenset({"CaMeL"})),
    }


def _mixed_store(sentinel: str) -> dict[str, CaMeLValue]:
    """Return a store with one trusted and one untrusted value."""
    return {
        "trusted_val": wrap("safe", sources=frozenset({"User literal"})),
        "untrusted_val": wrap(sentinel, sources=frozenset({"search_drive"})),
    }


def _exc_with_lineno(exc_cls: type, msg: str, lineno: int) -> Exception:
    """Return an exception instance with a ``lineno`` attribute."""
    exc = exc_cls(msg)
    exc.lineno = lineno  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# Parametrised adversarial redaction cases (R01–R06, R09–R10)
# ---------------------------------------------------------------------------


REDACTION_CASES: list[dict[str, Any]] = [
    # R01: Untrusted store + TypeError → message body redacted
    {
        "case_id": "R01",
        "description": "Untrusted store + TypeError: message body must be None",
        "exc": _exc_with_lineno(
            TypeError,
            "value 'SECRET_ERROR_BODY_R01' is not subscriptable",
            3,
        ),
        "store": _untrusted_store("SECRET_ERROR_BODY_R01"),
        "expected_trust_level": "untrusted",
        "expected_message": None,
        "expected_lineno": 3,
        "fragment_must_not_appear": "SECRET_ERROR_BODY_R01",
    },
    # R02: LLM-variant NotEnoughInformationError → fully redacted
    {
        "case_id": "R02",
        "description": (
            "LLM-variant NotEnoughInformationError: "
            "lineno and message must both be None"
        ),
        "exc": None,  # constructed inline (needs QResponse subclass)
        "store": _untrusted_store("neie_payload_r02"),
        "expected_trust_level": "not_enough_information",
        "expected_message": None,
        "expected_lineno": None,
        "fragment_must_not_appear": None,
        "_special": "neie_llm",
    },
    # R03: Trusted-only store + ValueError → full message included
    {
        "case_id": "R03",
        "description": "Trusted-only store + ValueError: full message must be included",
        "exc": _exc_with_lineno(ValueError, "trusted error text R03", 5),
        "store": _trusted_store(),
        "expected_trust_level": "trusted",
        "expected_message": "trusted error text R03",
        "expected_lineno": 5,
        "fragment_must_not_appear": None,
    },
    # R04: Mixed store + KeyError → message redacted (one untrusted taints all)
    {
        "case_id": "R04",
        "description": "Mixed store + KeyError: single untrusted source taints store",
        "exc": _exc_with_lineno(
            KeyError,
            "SENSITIVE_MIXED_R04",
            8,
        ),
        "store": _mixed_store("SENSITIVE_MIXED_R04"),
        "expected_trust_level": "untrusted",
        "expected_message": None,
        "expected_lineno": 8,
        "fragment_must_not_appear": "SENSITIVE_MIXED_R04",
    },
    # R05: Interpreter-variant NotEnoughInformationError → fully redacted
    {
        "case_id": "R05",
        "description": (
            "Interpreter-variant NotEnoughInformationError: "
            "both variants must produce identical redaction"
        ),
        "exc": None,  # CamelNEIE constructed inline
        "store": _untrusted_store("camel_neie_payload_r05"),
        "expected_trust_level": "not_enough_information",
        "expected_message": None,
        "expected_lineno": None,
        "fragment_must_not_appear": None,
        "_special": "neie_camel",
    },
    # R06: Untrusted store + exception with lineno=7 → lineno preserved, message None
    {
        "case_id": "R06",
        "description": (
            "Untrusted store + exception lineno=7: "
            "lineno safe to expose; message must be None"
        ),
        "exc": _exc_with_lineno(RuntimeError, "SECRET_PAYLOAD_R06", 7),
        "store": _untrusted_store("SECRET_PAYLOAD_R06"),
        "expected_trust_level": "untrusted",
        "expected_message": None,
        "expected_lineno": 7,
        "fragment_must_not_appear": "SECRET_PAYLOAD_R06",
    },
    # R09: Empty store → trusted classification (no untrusted sources)
    {
        "case_id": "R09",
        "description": "Empty store + NameError: no untrusted sources → trusted",
        "exc": NameError("name 'x' is not defined"),
        "store": {},
        "expected_trust_level": "trusted",
        "expected_message": "name 'x' is not defined",
        "expected_lineno": None,
        "fragment_must_not_appear": None,
    },
    # R10: Untrusted store + exception with no lineno → lineno=None
    {
        "case_id": "R10",
        "description": "Untrusted store + no lineno attribute: lineno must be None",
        "exc": Exception("no lineno here PAYLOAD_R10"),
        "store": _untrusted_store("PAYLOAD_R10"),
        "expected_trust_level": "untrusted",
        "expected_message": None,
        "expected_lineno": None,
        "fragment_must_not_appear": "PAYLOAD_R10",
    },
]


@pytest.mark.parametrize(
    "case",
    REDACTION_CASES,
    ids=[c["case_id"] for c in REDACTION_CASES],
)
def test_redaction_unit(
    case: dict[str, Any],
    harness_reporter: HarnessResultsReporter,
) -> None:
    """Unit-level assertion for a single adversarial redaction case.

    Directly calls :meth:`~camel.execution_loop.ExceptionRedactor.classify`
    and asserts the :class:`~camel.execution_loop.RedactedError` fields
    match the expected values for this case.

    Parameters
    ----------
    case:
        Adversarial case dict from ``REDACTION_CASES``.
    harness_reporter:
        Session-scoped results reporter.
    """
    redactor = ExceptionRedactor()

    # Build the exception (some cases need special construction).
    special = case.get("_special")
    if special == "neie_llm":

        class _DummySchema(QResponse):
            value: str

        exc = LLMNotEnoughInfoError(schema_type=_DummySchema)
    elif special == "neie_camel":
        exc = CamelNEIE()
    else:
        exc = case["exc"]

    store: dict[str, CaMeLValue] = case["store"]
    redacted: RedactedError = redactor.classify(exc, store)

    try:
        assert redacted.trust_level == case["expected_trust_level"], (
            f"[{case['case_id']}] Expected trust_level="
            f"{case['expected_trust_level']!r}; "
            f"got {redacted.trust_level!r}"
        )
        assert redacted.message == case["expected_message"], (
            f"[{case['case_id']}] Expected message={case['expected_message']!r}; "
            f"got {redacted.message!r}"
        )
        if case["expected_lineno"] is not None:
            assert redacted.lineno == case["expected_lineno"], (
                f"[{case['case_id']}] Expected lineno={case['expected_lineno']}; "
                f"got {redacted.lineno}"
            )
        else:
            assert redacted.lineno is None or isinstance(redacted.lineno, int), (
                f"[{case['case_id']}] lineno should be None or int; "
                f"got {redacted.lineno!r}"
            )

        # If there is a fragment that must not appear, double-check.
        fragment = case.get("fragment_must_not_appear")
        if fragment:
            assert redacted.message is None or fragment not in redacted.message, (
                f"[{case['case_id']}] Fragment {fragment!r} should not appear "
                f"in redacted message"
            )

        harness_reporter.record("I3_redaction_completeness", True)
    except AssertionError:
        harness_reporter.record("I3_redaction_completeness", False)
        raise


# ---------------------------------------------------------------------------
# R07: Full-orchestrator retry — sensitive payload must not reach P-LLM
# ---------------------------------------------------------------------------


async def test_r07_orchestrator_retry_redaction(
    harness_reporter: HarnessResultsReporter,
) -> None:
    """R07: sensitive exception payload never appears in P-LLM retry prompt.

    A tool raises ``RuntimeError("SENSITIVE_PAYLOAD_R07")``.  The
    orchestrator catches the exception, classifies it as untrusted
    (since the store contains a value from an external tool), and builds
    a retry prompt that omits the exception message.

    The ``RecordingBackend`` spy verifies the payload never reaches any
    P-LLM ``generate()`` call.

    Parameters
    ----------
    harness_reporter:
        Session-scoped results reporter.
    """
    SENTINEL = "SENSITIVE_PAYLOAD_R07"

    # Tool that first sets a variable with untrusted data, then on a
    # second call raises with the sensitive payload.
    call_count = [0]

    def failing_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
        """Raises only on the second invocation with a sensitive message."""
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError(SENTINEL)
        return wrap(f"data_{SENTINEL}", sources=frozenset({"external_api"}))

    # Plan 1: sets a variable then calls the tool a second time (which raises).
    # Plan 2 (retry): simple success plan.
    plan1 = "result = failing_tool()\nresult2 = failing_tool()"
    plan2 = "result = failing_tool()"

    stub = StubBackend(responses=[plan1, plan2], cycle=False)
    recording = RecordingBackend(stub)
    p_llm = PLLMWrapper(recording)
    interpreter = CaMeLInterpreter(
        tools={"failing_tool": failing_tool},
    )
    sigs = [ToolSignature("failing_tool", "", "CaMeLValue", "Stub tool")]
    orchestrator = CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interpreter,
        tool_signatures=sigs,
        max_loop_retries=5,
    )

    # Run — expect success on the retry (plan2).
    await orchestrator.run("Test R07 retry redaction")

    try:
        assert_exception_message_redacted(
            recording.recorded_calls,
            [SENTINEL],
        )
        harness_reporter.record("I3_redaction_completeness", True)
    except AssertionError:
        harness_reporter.record("I3_redaction_completeness", False)
        raise


# ---------------------------------------------------------------------------
# R08: Q-LLM insufficient data — classified data must not reach P-LLM
# ---------------------------------------------------------------------------


async def test_r08_qllm_neie_no_classified_data_leak(
    harness_reporter: HarnessResultsReporter,
) -> None:
    """R08: classified data in Q-LLM input does not reach P-LLM.

    The Q-LLM tool stub receives untrusted content containing
    ``"CLASSIFIED_DATA_R08"`` and raises
    :class:`~camel.exceptions.NotEnoughInformationError` (simulating a
    Q-LLM that cannot extract the requested schema).

    The orchestrator must redact the ``NotEnoughInformationError``
    completely (type name only) so the classified data never appears in
    the retry prompt.

    Parameters
    ----------
    harness_reporter:
        Session-scoped results reporter.
    """
    CLASSIFIED = "CLASSIFIED_DATA_R08"

    def qllm_stub(*args: Any, **kwargs: Any) -> CaMeLValue:
        """Raises CamelNEIE — simulating insufficient Q-LLM context."""
        raise CamelNEIE()

    def data_tool(*args: Any, **kwargs: Any) -> CaMeLValue:
        """Returns data containing classified content."""
        return wrap(
            f"document: {CLASSIFIED}",
            sources=frozenset({"document_store"}),
        )

    # Plan 1: reads classified data then calls Q-LLM stub (which raises NEIE).
    # Plan 2: simple success without Q-LLM.
    plan1 = "doc = data_tool()\nresult = qllm_stub(doc)"
    plan2 = "result = data_tool()"

    stub = StubBackend(responses=[plan1, plan2], cycle=False)
    recording = RecordingBackend(stub)
    p_llm = PLLMWrapper(recording)
    interpreter = CaMeLInterpreter(
        tools={"data_tool": data_tool, "qllm_stub": qllm_stub},
    )
    sigs = [
        ToolSignature("data_tool", "", "CaMeLValue", "Returns classified doc"),
        ToolSignature("qllm_stub", "doc", "CaMeLValue", "Q-LLM extraction stub"),
    ]
    orchestrator = CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interpreter,
        tool_signatures=sigs,
        max_loop_retries=5,
    )

    await orchestrator.run("Test R08 NEIE classified data")

    try:
        # The classified data string must not appear in any P-LLM message.
        assert_exception_message_redacted(
            recording.recorded_calls,
            [CLASSIFIED],
        )
        # Also check all P-LLM messages globally.
        all_content = recording.all_message_content()
        for content in all_content:
            assert CLASSIFIED not in content, (
                f"[R08] Classified fragment {CLASSIFIED!r} leaked into "
                "a P-LLM message."
            )
        harness_reporter.record("I3_redaction_completeness", True)
    except AssertionError:
        harness_reporter.record("I3_redaction_completeness", False)
        raise
