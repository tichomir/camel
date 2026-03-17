"""Reusable isolation invariant assertion helpers.

Each helper corresponds to one of the three isolation invariants defined
in ADR-008:

I-1  No tool return value content in P-LLM context.
I-2  No raw Q-LLM free-form response reaches P-LLM context.
I-3  All untrusted-origin exception messages are fully redacted before
     reaching the P-LLM retry prompt.

Usage
-----
Pass the spy's ``recorded_calls`` plus the relevant sentinel strings.
Each helper raises ``AssertionError`` with a descriptive message if the
invariant is violated.
"""

from __future__ import annotations

from tests.harness.recording_backend import RecordedCall


# ---------------------------------------------------------------------------
# I-1: No tool return value content in P-LLM context
# ---------------------------------------------------------------------------


def assert_no_tool_value_in_messages(
    recorded_calls: list[RecordedCall],
    tool_return_sentinels: list[str],
) -> None:
    """Assert none of the P-LLM generate() calls contain tool return value text.

    Each E2E scenario registers tool stubs that embed a unique sentinel
    string in their return :class:`~camel.value.CaMeLValue`.  This helper
    checks all recorded P-LLM messages for that sentinel to confirm the
    interpreter's isolation contract holds.

    Parameters
    ----------
    recorded_calls:
        All calls recorded by a :class:`~tests.harness.recording_backend.RecordingBackend`
        spy on the P-LLM backend.
    tool_return_sentinels:
        Strings that MUST NOT appear in any P-LLM message.  Each sentinel
        is unique per scenario to prevent false negatives from substring
        collisions.

    Raises
    ------
    AssertionError
        When any sentinel is found in any recorded message.
    """
    for call in recorded_calls:
        for msg in call.messages:
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for sentinel in tool_return_sentinels:
                assert sentinel not in content, (
                    f"[I-1] Tool return sentinel {sentinel!r} found in "
                    f"{msg.get('role')!r} message (method={call.method!r}). "
                    "Tool return values must never reach the P-LLM context."
                )


# ---------------------------------------------------------------------------
# I-2: No raw Q-LLM free-form response in P-LLM context
# ---------------------------------------------------------------------------


def assert_no_qllm_freeform_in_messages(
    recorded_calls: list[RecordedCall],
    qllm_raw_sentinels: list[str],
) -> None:
    """Assert no unstructured Q-LLM response text appears in P-LLM messages.

    The Q-LLM stub is configured to embed a unique sentinel in its raw
    ``generate()`` response text.  That text is consumed internally by
    :class:`~camel.llm.qllm.QLLMWrapper` and must never propagate to
    the P-LLM context.

    Parameters
    ----------
    recorded_calls:
        Recorded P-LLM backend calls.
    qllm_raw_sentinels:
        Strings present only in raw Q-LLM response text — must not appear
        in any P-LLM message.

    Raises
    ------
    AssertionError
        When any Q-LLM sentinel is found in any P-LLM message.
    """
    for call in recorded_calls:
        for msg in call.messages:
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for sentinel in qllm_raw_sentinels:
                assert sentinel not in content, (
                    f"[I-2] Q-LLM free-form sentinel {sentinel!r} found in "
                    f"{msg.get('role')!r} message (method={call.method!r}). "
                    "Unstructured Q-LLM output must never reach P-LLM context."
                )


# ---------------------------------------------------------------------------
# I-3: Untrusted-origin exceptions are fully redacted in retry prompts
# ---------------------------------------------------------------------------


def assert_exception_message_redacted(
    recorded_calls: list[RecordedCall],
    exception_message_fragments: list[str],
) -> None:
    """Assert exception message body fragments never appear in P-LLM retry prompts.

    When the interpreter raises an exception whose provenance includes any
    untrusted source, the orchestrator must redact the message body from the
    retry prompt forwarded to the P-LLM.  Only the exception type name and
    line number are permitted.

    Parameters
    ----------
    recorded_calls:
        Recorded P-LLM backend calls.  Retry prompts appear as ``user``-role
        messages in calls after the first.
    exception_message_fragments:
        Substrings from the exception's ``str()`` representation that must
        not appear in any P-LLM message.

    Raises
    ------
    AssertionError
        When any exception fragment is found in any P-LLM user-role message.
    """
    for call in recorded_calls:
        for msg in call.messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for fragment in exception_message_fragments:
                assert fragment not in content, (
                    f"[I-3] Redacted exception fragment {fragment!r} leaked "
                    "into P-LLM retry prompt. Untrusted exception messages "
                    "must be fully redacted."
                )
