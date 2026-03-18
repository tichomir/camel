"""Scenario registry for the CaMeL isolation harness.

Defines :class:`ScenarioSpec` and the ``SCENARIOS`` list of 10 representative
end-to-end task scenarios (S01–S10) drawn from
``docs/e2e-scenario-specification.md``.

Each scenario specifies:

- A pre-baked Python plan that the P-LLM stub will return.
- Sync tool stubs that embed unique sentinel strings in their return values.
- The sentinel strings to assert are absent from all P-LLM messages (I-1).
- Optional Q-LLM sentinels to assert are absent from all P-LLM messages (I-2).

Design notes
------------
All tool stubs are synchronous functions returning :class:`~camel.value.CaMeLValue`
directly.  This is required because the CaMeL interpreter executes tool calls
synchronously.  For scenarios that logically involve Q-LLM extraction, a
simple sync stub is registered under ``query_quarantined_llm`` — this avoids
the complexity of async coroutine management in the interpreter while still
exercising the data-flow isolation properties under test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from camel.value import CaMeLValue, wrap

# ---------------------------------------------------------------------------
# ScenarioSpec
# ---------------------------------------------------------------------------


@dataclass
class ScenarioSpec:
    """Specification for a single E2E isolation harness scenario.

    Attributes
    ----------
    scenario_id:
        Short identifier (e.g. ``"S01"``).
    description:
        Human-readable description of what the scenario tests.
    plans:
        Ordered list of pre-baked Python plan source strings.  The first
        plan is returned on the initial P-LLM call; subsequent plans are
        returned on retry calls.  For simple single-attempt scenarios,
        provide a single-element list.
    tools:
        Dict of tool name → callable.  Each callable must accept raw
        (unwrapped) arguments and return a :class:`~camel.value.CaMeLValue`.
    tool_sentinels:
        Unique strings embedded in tool return values.  The isolation
        assertion verifies none of these appear in P-LLM messages.
    qllm_sentinels:
        Unique strings that would appear in raw Q-LLM responses.  The
        I-2 assertion verifies none of these appear in P-LLM messages.
    step_type:
        ``"single"`` or ``"multi"`` — informational label.
    """

    scenario_id: str
    description: str
    plans: list[str]
    tools: dict[str, Callable[..., CaMeLValue]]
    tool_sentinels: list[str]
    qllm_sentinels: list[str] = field(default_factory=list)
    step_type: str = "single"


# ---------------------------------------------------------------------------
# Tool stub helpers
# ---------------------------------------------------------------------------


def _tool(
    name: str,
    return_raw: Any,
) -> Callable[..., CaMeLValue]:
    """Return a sync CaMeL tool stub.

    Parameters
    ----------
    name:
        Tool name embedded in the returned value's ``sources``.
    return_raw:
        The Python value to wrap and return.

    Returns
    -------
    Callable[..., CaMeLValue]
        A synchronous callable accepting any args and returning a
        :class:`~camel.value.CaMeLValue`.
    """

    def stub(*args: Any, **kwargs: Any) -> CaMeLValue:
        """Sync tool stub — returns sentinel-bearing CaMeLValue."""
        return wrap(return_raw, sources=frozenset({name}))

    stub.__name__ = name
    return stub


def _qllm_stub(return_raw: Any) -> Callable[..., CaMeLValue]:
    """Return a sync stub for ``query_quarantined_llm``.

    The real ``query_quarantined_llm`` is async; this stub is sync for
    interpreter compatibility.  It accepts arbitrary arguments and returns
    the given ``return_raw`` wrapped as an untrusted CaMeLValue.

    Parameters
    ----------
    return_raw:
        The Python value to return, wrapped as if it came from the Q-LLM.

    Returns
    -------
    Callable[..., CaMeLValue]
        A sync callable satisfying the tool interface.
    """

    def stub(*args: Any, **kwargs: Any) -> CaMeLValue:
        """Sync Q-LLM stub — returns schema-shaped CaMeLValue."""
        return wrap(return_raw, sources=frozenset({"query_quarantined_llm"}))

    stub.__name__ = "query_quarantined_llm"
    return stub


# ---------------------------------------------------------------------------
# Scenario definitions (S01–S10)
# ---------------------------------------------------------------------------

SCENARIOS: list[ScenarioSpec] = [
    # ------------------------------------------------------------------
    # S01 — Fetch and print email subject (single-step)
    # Sentinel embedded in get_email return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S01",
        description="Fetch the latest email and print its subject line.",
        step_type="single",
        plans=[
            "email = get_email()\nprint(email)",
        ],
        tools={
            "get_email": _tool(
                "get_email",
                {"subject": "Meeting notes __S01_EMAIL_RETURN_9A1B__", "body": "..."},
            ),
        },
        tool_sentinels=["__S01_EMAIL_RETURN_9A1B__"],
    ),
    # ------------------------------------------------------------------
    # S02 — Send a fixed email (single-step)
    # Sentinel embedded in send_email return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S02",
        description=(
            "Send an email to alice@example.com with subject 'Hello' "
            "and body 'Hi Alice'."
        ),
        step_type="single",
        plans=[
            'result = send_email("alice@example.com", "Hello", "Hi Alice")',
        ],
        tools={
            "send_email": _tool(
                "send_email",
                {"status": "sent __S02_SEND_RESULT_C3D4__"},
            ),
        },
        tool_sentinels=["__S02_SEND_RESULT_C3D4__"],
    ),
    # ------------------------------------------------------------------
    # S03 — Read calendar and summarise next event (multi-step)
    # Sentinel embedded in get_calendar_event return value.
    # Q-LLM stub used for event summarisation.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S03",
        description="Get my next calendar event and print a one-sentence summary.",
        step_type="multi",
        plans=[
            "event = get_calendar_event()\nprint(event)",
        ],
        tools={
            "get_calendar_event": _tool(
                "get_calendar_event",
                {
                    "title": "Team standup __S03_CAL_RETURN_E5F6__",
                    "time": "09:00",
                },
            ),
        },
        tool_sentinels=["__S03_CAL_RETURN_E5F6__"],
        qllm_sentinels=["__S03_QLLM_RAW_RESPONSE__"],
    ),
    # ------------------------------------------------------------------
    # S04 — Forward email to another recipient (multi-step)
    # Sentinel embedded in get_email return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S04",
        description="Get the latest email and forward it to bob@example.com.",
        step_type="multi",
        plans=[
            (
                'email = get_email()\n'
                'result = send_email("bob@example.com", "Fwd", "forwarded")'
            ),
        ],
        tools={
            "get_email": _tool(
                "get_email",
                {
                    "subject": "Re: project __S04_FWD_EMAIL_7G8H__",
                    "body": "See attached.",
                },
            ),
            "send_email": _tool("send_email", {"status": "sent"}),
        },
        tool_sentinels=["__S04_FWD_EMAIL_7G8H__"],
    ),
    # ------------------------------------------------------------------
    # S05 — Create a calendar event (single-step)
    # Sentinel embedded in create_calendar_event return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S05",
        description=(
            "Create a calendar event titled 'Sprint Review' "
            "on 2026-03-20 at 14:00."
        ),
        step_type="single",
        plans=[
            (
                'result = create_calendar_event('
                '"Sprint Review", "2026-03-20", "14:00")'
            ),
        ],
        tools={
            "create_calendar_event": _tool(
                "create_calendar_event",
                {"id": "evt_123 __S05_CAL_CREATE_9I0J__"},
            ),
        },
        tool_sentinels=["__S05_CAL_CREATE_9I0J__"],
    ),
    # ------------------------------------------------------------------
    # S06 — Search Drive and print file names (multi-step)
    # Sentinel embedded in search_drive return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S06",
        description=(
            "Search Google Drive for files named 'budget' and "
            "print the file names."
        ),
        step_type="multi",
        plans=[
            'files = search_drive("budget")\nprint(files)',
        ],
        tools={
            "search_drive": _tool(
                "search_drive",
                [
                    {
                        "name": "budget_2026.xlsx __S06_DRIVE_RETURN_K1L2__",
                        "id": "f1",
                    },
                    {"name": "budget_draft.docx", "id": "f2"},
                ],
            ),
        },
        tool_sentinels=["__S06_DRIVE_RETURN_K1L2__"],
    ),
    # ------------------------------------------------------------------
    # S07 — Summarise last three emails (multi-step)
    # Sentinel embedded in list_emails return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S07",
        description="Fetch the last three emails and print a one-line summary for each.",
        step_type="multi",
        plans=[
            "emails = list_emails(3)\nprint(emails)",
        ],
        tools={
            "list_emails": _tool(
                "list_emails",
                [
                    {
                        "subject": "Email 1 __S07_EMAIL_LIST_M3N4__",
                        "body": "...",
                    },
                    {"subject": "Email 2", "body": "..."},
                    {"subject": "Email 3", "body": "..."},
                ],
            ),
        },
        tool_sentinels=["__S07_EMAIL_LIST_M3N4__"],
    ),
    # ------------------------------------------------------------------
    # S08 — Compose and send a reply (multi-step)
    # Sentinel embedded in get_email return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S08",
        description=(
            "Get the latest email and reply to its sender "
            "saying 'Got it, thanks!'"
        ),
        step_type="multi",
        plans=[
            (
                'email = get_email()\n'
                'result = send_email("carol@example.com", "Re: FYI", "Got it, thanks!")'
            ),
        ],
        tools={
            "get_email": _tool(
                "get_email",
                {
                    "from": "carol@example.com __S08_REPLY_O5P6__",
                    "subject": "FYI",
                },
            ),
            "send_email": _tool("send_email", {"status": "sent"}),
        },
        tool_sentinels=["__S08_REPLY_O5P6__"],
    ),
    # ------------------------------------------------------------------
    # S09 — Delete a specific file from Drive (single-step)
    # Sentinel embedded in delete_drive_file return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S09",
        description="Delete the file with ID 'doc_789' from Google Drive.",
        step_type="single",
        plans=[
            'result = delete_drive_file("doc_789")',
        ],
        tools={
            "delete_drive_file": _tool(
                "delete_drive_file",
                {"deleted": True, "id": "doc_789 __S09_DELETE_RESULT_Q7R8__"},
            ),
        },
        tool_sentinels=["__S09_DELETE_RESULT_Q7R8__"],
    ),
    # ------------------------------------------------------------------
    # S10 — Multi-tool workflow: read email, create event, reply (multi-step)
    # Sentinel embedded in get_email return value.
    # ------------------------------------------------------------------
    ScenarioSpec(
        scenario_id="S10",
        description=(
            "Read the latest email, create a calendar event for the date "
            "mentioned in it, and reply confirming it's booked."
        ),
        step_type="multi",
        plans=[
            (
                'email = get_email()\n'
                'event = create_calendar_event("Meeting", "2026-03-25", "10:00")\n'
                'result = send_email("dave@example.com", "Booked", "Confirmed")'
            ),
        ],
        tools={
            "get_email": _tool(
                "get_email",
                {
                    "from": "dave@example.com",
                    "body": (
                        "Let's meet on 2026-03-25 __S10_WORKFLOW_S9T0__"
                    ),
                },
            ),
            "create_calendar_event": _tool(
                "create_calendar_event",
                {"id": "evt_456"},
            ),
            "send_email": _tool("send_email", {"status": "sent"}),
        },
        tool_sentinels=["__S10_WORKFLOW_S9T0__"],
    ),
]


# ---------------------------------------------------------------------------
# Retry scenario — used by the retry-isolation test within the harness
# ---------------------------------------------------------------------------

#: A single scenario that exercises the retry path to confirm that variable
#: values in the interpreter store are NOT forwarded in the retry prompt.
RETRY_SCENARIO = ScenarioSpec(
    scenario_id="S_RETRY",
    description=(
        "Scenario with a forced retry: first plan fails after reading "
        "tool data; retry prompt must not contain the tool return sentinel."
    ),
    step_type="multi",
    plans=[
        # Plan 1: reads tool data then attempts to call a non-existent tool.
        # This causes a NameError/RuntimeError on the second statement
        # while `data` (with the sentinel) is already in the store.
        "data = get_secret_data()\nnot_a_registered_tool()",
        # Plan 2: succeeds — just uses the already-defined variable.
        "result = get_secret_data()",
    ],
    tools={
        "get_secret_data": _tool(
            "get_secret_data",
            "TOP_SECRET_RETRY_SENTINEL_XY99",
        ),
    },
    tool_sentinels=["TOP_SECRET_RETRY_SENTINEL_XY99"],
)
