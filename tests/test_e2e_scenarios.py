"""End-to-end scenario tests for the CaMeL execution stack.

Executes all 10 representative task scenarios (S01–S10) defined in
``docs/e2e-scenario-specification.md`` against the full CaMeL stack:

    P-LLM (mocked) → CaMeLInterpreter → mock tools → ExecutionTrace

Security policies are disabled (policy_engine=None, NORMAL mode) so that
execution traces reflect correct data-flow without policy denials — this is a
Milestone 2 correctness concern, not a Milestone 3 policy concern.

Scenario inventory
------------------
| ID  | Description                          | Step type | Trace length |
|-----|--------------------------------------|-----------|--------------|
| S01 | Fetch and print email subject        | single    | 1            |
| S02 | Send a fixed email                   | single    | 1            |
| S03 | Read calendar, summarise next event  | multi     | 1            |
| S04 | Forward email to another recipient   | multi     | 2            |
| S05 | Create a calendar event              | single    | 1            |
| S06 | Search Drive and print file names    | multi     | 1            |
| S07 | Summarise last three emails          | multi     | ≥1           |
| S08 | Compose and send a reply             | multi     | 2            |
| S09 | Delete a specific Drive file         | single    | 1            |
| S10 | Multi-tool: read email, event, reply | multi     | 3            |

Assertions per scenario
-----------------------
- Correct tools called in correct order (trace_shape).
- Arguments match expected values.
- Memory snapshots contain correct variable bindings after each step.
- No exception raised; ExecutionResult returned.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from camel import CaMeLInterpreter
from camel.execution_loop import CaMeLOrchestrator, ExecutionResult
from camel.llm.p_llm import CodePlan, PLLMWrapper, ToolSignature
from camel.value import CaMeLValue, wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, return_raw: Any = None) -> Any:
    """Return a synchronous CaMeL-compatible tool callable."""

    def tool(*args: Any, **kwargs: Any) -> CaMeLValue:
        return wrap(return_raw, sources=frozenset({name}))

    tool.__name__ = name
    return tool


def _make_plllm(plan_source: str) -> PLLMWrapper:
    """Return a mock PLLMWrapper that always returns *plan_source*."""
    mock = MagicMock(spec=PLLMWrapper)
    mock.generate_plan = AsyncMock(return_value=CodePlan(source=plan_source))
    return mock


class _NullDisplayChannel:
    """Display channel that discards all output."""

    def write(self, value: CaMeLValue) -> None:
        """Discard *value*."""


def _run(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _build_orchestrator(
    plan_source: str,
    tools: dict[str, Any],
    tool_signatures: list[ToolSignature],
) -> CaMeLOrchestrator:
    """Construct a fully wired orchestrator with mock P-LLM and given tools."""
    interp = CaMeLInterpreter(tools=tools)
    p_llm = _make_plllm(plan_source)
    return CaMeLOrchestrator(
        p_llm=p_llm,
        interpreter=interp,
        tool_signatures=tool_signatures,
        display_channel=_NullDisplayChannel(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# S01 — Fetch and print email subject (single-step)
# ---------------------------------------------------------------------------

S01_SENTINEL = "__S01_EMAIL_RETURN_9A1B__"

S01_PLAN = """\
email = get_email()
print(email)
"""

S01_TOOLS: dict[str, Any] = {
    "get_email": _make_tool(
        "get_email",
        return_raw={"subject": f"Meeting notes {S01_SENTINEL}", "body": "..."},
    ),
}

S01_SIGS = [
    ToolSignature("get_email", "", "dict", "Retrieve the latest email."),
]


class TestS01FetchPrintEmailSubject:
    """S01 — single-step: get_email → print."""

    def test_trace_length_is_one(self) -> None:
        """Trace contains exactly one record."""
        orch = _build_orchestrator(S01_PLAN, S01_TOOLS, S01_SIGS)
        result = _run(orch.run("Retrieve the latest email and print its subject line."))
        assert isinstance(result, ExecutionResult)
        assert len(result.trace) == 1

    def test_trace_tool_name_is_get_email(self) -> None:
        """The single trace record names get_email."""
        orch = _build_orchestrator(S01_PLAN, S01_TOOLS, S01_SIGS)
        result = _run(orch.run("Retrieve the latest email and print its subject line."))
        assert result.trace[0].tool_name == "get_email"

    def test_final_store_contains_email(self) -> None:
        """final_store after execution contains 'email' binding."""
        orch = _build_orchestrator(S01_PLAN, S01_TOOLS, S01_SIGS)
        result = _run(orch.run("Retrieve the latest email and print its subject line."))
        # Memory snapshot is taken during the tool call (before the assignment
        # completes), so 'email' appears in final_store rather than the snapshot.
        assert "email" in result.final_store

    def test_loop_attempts_is_zero(self) -> None:
        """First attempt succeeds; no retries needed."""
        orch = _build_orchestrator(S01_PLAN, S01_TOOLS, S01_SIGS)
        result = _run(orch.run("Retrieve the latest email and print its subject line."))
        assert result.loop_attempts == 0


# ---------------------------------------------------------------------------
# S02 — Send a fixed email (single-step)
# ---------------------------------------------------------------------------

S02_SENTINEL = "__S02_SEND_RESULT_C3D4__"

S02_PLAN = """\
result = send_email(to="alice@example.com", subject="Hello", body="Hi Alice")
"""

S02_TOOLS: dict[str, Any] = {
    "send_email": _make_tool(
        "send_email",
        return_raw={"status": f"sent {S02_SENTINEL}"},
    ),
}

S02_SIGS = [
    ToolSignature(
        "send_email",
        "to: str, subject: str, body: str",
        "dict",
        "Send an email.",
    ),
]


class TestS02SendFixedEmail:
    """S02 — single-step: send_email with literal args."""

    def test_trace_length_is_one(self) -> None:
        """Trace contains exactly one record."""
        orch = _build_orchestrator(S02_PLAN, S02_TOOLS, S02_SIGS)
        result = _run(
            orch.run("Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'.")
        )
        assert len(result.trace) == 1

    def test_trace_to_argument_is_alice(self) -> None:
        """The send_email call received to='alice@example.com'."""
        orch = _build_orchestrator(S02_PLAN, S02_TOOLS, S02_SIGS)
        result = _run(
            orch.run("Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'.")
        )
        assert result.trace[0].args["to"] == "alice@example.com"

    def test_trace_subject_argument(self) -> None:
        """The send_email call received subject='Hello'."""
        orch = _build_orchestrator(S02_PLAN, S02_TOOLS, S02_SIGS)
        result = _run(
            orch.run("Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'.")
        )
        assert result.trace[0].args["subject"] == "Hello"

    def test_final_store_contains_result(self) -> None:
        """final_store after execution contains 'result' binding."""
        orch = _build_orchestrator(S02_PLAN, S02_TOOLS, S02_SIGS)
        result = _run(
            orch.run("Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'.")
        )
        # Memory snapshot is taken during the tool call (before assignment),
        # so use final_store to verify the binding.
        assert "result" in result.final_store


# ---------------------------------------------------------------------------
# S03 — Read calendar and summarise next event (multi-step)
# ---------------------------------------------------------------------------

S03_SENTINEL = "__S03_CAL_RETURN_E5F6__"

S03_PLAN = """\
event = get_calendar_event()
print(event)
"""

S03_TOOLS: dict[str, Any] = {
    "get_calendar_event": _make_tool(
        "get_calendar_event",
        return_raw={"title": f"Team standup {S03_SENTINEL}", "time": "09:00"},
    ),
}

S03_SIGS = [
    ToolSignature("get_calendar_event", "", "dict", "Get the next calendar event."),
]


class TestS03ReadCalendarEvent:
    """S03 — multi-step: get_calendar_event → print."""

    def test_trace_length_is_one(self) -> None:
        """Q-LLM is a builtin; only get_calendar_event appears in trace."""
        orch = _build_orchestrator(S03_PLAN, S03_TOOLS, S03_SIGS)
        result = _run(orch.run("Get my next calendar event and print a one-sentence summary."))
        assert len(result.trace) == 1

    def test_trace_tool_name_is_get_calendar_event(self) -> None:
        """Trace records get_calendar_event."""
        orch = _build_orchestrator(S03_PLAN, S03_TOOLS, S03_SIGS)
        result = _run(orch.run("Get my next calendar event and print a one-sentence summary."))
        assert result.trace[0].tool_name == "get_calendar_event"

    def test_final_store_contains_event(self) -> None:
        """final_store contains 'event' after execution."""
        orch = _build_orchestrator(S03_PLAN, S03_TOOLS, S03_SIGS)
        result = _run(orch.run("Get my next calendar event and print a one-sentence summary."))
        # Snapshot taken during tool call; check final_store for the binding.
        assert "event" in result.final_store

    def test_print_output_captured(self) -> None:
        """print(event) produces a print_output entry."""
        orch = _build_orchestrator(S03_PLAN, S03_TOOLS, S03_SIGS)
        result = _run(orch.run("Get my next calendar event and print a one-sentence summary."))
        assert len(result.print_outputs) >= 1


# ---------------------------------------------------------------------------
# S04 — Forward email to another recipient (multi-step)
# ---------------------------------------------------------------------------

S04_SENTINEL = "__S04_FWD_EMAIL_7G8H__"

S04_PLAN = """\
email = get_email()
result = send_email(to="bob@example.com", subject="Fwd: project", body="See attached.")
"""

S04_TOOLS: dict[str, Any] = {
    "get_email": _make_tool(
        "get_email",
        return_raw={
            "subject": f"Re: project {S04_SENTINEL}",
            "body": "See attached.",
        },
    ),
    "send_email": _make_tool("send_email", return_raw={"status": "sent"}),
}

S04_SIGS = [
    ToolSignature("get_email", "", "dict", "Retrieve the latest email."),
    ToolSignature("send_email", "to: str, subject: str, body: str", "dict", "Send an email."),
]


class TestS04ForwardEmail:
    """S04 — multi-step: get_email → send_email."""

    def test_trace_length_is_two(self) -> None:
        """Trace contains exactly two records."""
        orch = _build_orchestrator(S04_PLAN, S04_TOOLS, S04_SIGS)
        result = _run(orch.run("Get the latest email and forward it to bob@example.com."))
        assert len(result.trace) == 2

    def test_trace_order(self) -> None:
        """get_email appears before send_email in the trace."""
        orch = _build_orchestrator(S04_PLAN, S04_TOOLS, S04_SIGS)
        result = _run(orch.run("Get the latest email and forward it to bob@example.com."))
        assert result.trace[0].tool_name == "get_email"
        assert result.trace[1].tool_name == "send_email"

    def test_send_email_to_argument(self) -> None:
        """Forward destination is bob@example.com."""
        orch = _build_orchestrator(S04_PLAN, S04_TOOLS, S04_SIGS)
        result = _run(orch.run("Get the latest email and forward it to bob@example.com."))
        assert result.trace[1].args["to"] == "bob@example.com"

    def test_memory_snapshot_after_send_contains_prior_vars(self) -> None:
        """send_email snapshot has 'email' (assigned in prior statement).

        The snapshot is taken during the tool call, so the current assignment
        target ('result') is not yet bound; only variables from earlier
        statements appear.
        """
        orch = _build_orchestrator(S04_PLAN, S04_TOOLS, S04_SIGS)
        result = _run(orch.run("Get the latest email and forward it to bob@example.com."))
        snap = result.trace[1].memory_snapshot
        assert "email" in snap
        # Verify final_store has both bindings.
        assert "email" in result.final_store
        assert "result" in result.final_store


# ---------------------------------------------------------------------------
# S05 — Create a calendar event (single-step)
# ---------------------------------------------------------------------------

S05_SENTINEL = "__S05_CAL_CREATE_9I0J__"

S05_PLAN = """\
result = create_calendar_event(title="Sprint Review", date="2026-03-20", time="14:00")
"""

S05_TOOLS: dict[str, Any] = {
    "create_calendar_event": _make_tool(
        "create_calendar_event",
        return_raw={"id": f"evt_123 {S05_SENTINEL}"},
    ),
}

S05_SIGS = [
    ToolSignature(
        "create_calendar_event",
        "title: str, date: str, time: str",
        "dict",
        "Create a calendar event.",
    ),
]


class TestS05CreateCalendarEvent:
    """S05 — single-step: create_calendar_event with literal args."""

    def test_trace_length_is_one(self) -> None:
        """Trace has a single create_calendar_event record."""
        orch = _build_orchestrator(S05_PLAN, S05_TOOLS, S05_SIGS)
        result = _run(
            orch.run("Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00.")
        )
        assert len(result.trace) == 1

    def test_trace_title_argument(self) -> None:
        """title argument is 'Sprint Review'."""
        orch = _build_orchestrator(S05_PLAN, S05_TOOLS, S05_SIGS)
        result = _run(
            orch.run("Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00.")
        )
        assert result.trace[0].args["title"] == "Sprint Review"

    def test_trace_date_argument(self) -> None:
        """date argument is '2026-03-20'."""
        orch = _build_orchestrator(S05_PLAN, S05_TOOLS, S05_SIGS)
        result = _run(
            orch.run("Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00.")
        )
        assert result.trace[0].args["date"] == "2026-03-20"

    def test_final_store_contains_result(self) -> None:
        """final_store contains 'result' after create_calendar_event."""
        orch = _build_orchestrator(S05_PLAN, S05_TOOLS, S05_SIGS)
        result = _run(
            orch.run("Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00.")
        )
        # Snapshot taken before assignment; use final_store.
        assert "result" in result.final_store


# ---------------------------------------------------------------------------
# S06 — Search Drive and print file names (multi-step)
# ---------------------------------------------------------------------------

S06_SENTINEL = "__S06_DRIVE_RETURN_K1L2__"

S06_PLAN = """\
files = search_drive(query="budget")
print(files)
"""

S06_TOOLS: dict[str, Any] = {
    "search_drive": _make_tool(
        "search_drive",
        return_raw=[
            {"name": f"budget_2026.xlsx {S06_SENTINEL}", "id": "f1"},
            {"name": "budget_draft.docx", "id": "f2"},
        ],
    ),
}

S06_SIGS = [
    ToolSignature("search_drive", "query: str", "list", "Search Google Drive."),
]


class TestS06SearchDrive:
    """S06 — multi-step: search_drive → print."""

    def test_trace_length_is_one(self) -> None:
        """Trace has one record for search_drive."""
        orch = _build_orchestrator(S06_PLAN, S06_TOOLS, S06_SIGS)
        result = _run(
            orch.run("Search Google Drive for files named 'budget' and print the file names.")
        )
        assert len(result.trace) == 1

    def test_trace_query_argument(self) -> None:
        """search_drive called with query='budget'."""
        orch = _build_orchestrator(S06_PLAN, S06_TOOLS, S06_SIGS)
        result = _run(
            orch.run("Search Google Drive for files named 'budget' and print the file names.")
        )
        assert result.trace[0].args.get("query") == "budget"

    def test_final_store_contains_files(self) -> None:
        """final_store contains 'files' after search_drive."""
        orch = _build_orchestrator(S06_PLAN, S06_TOOLS, S06_SIGS)
        result = _run(
            orch.run("Search Google Drive for files named 'budget' and print the file names.")
        )
        # Snapshot taken before assignment; use final_store.
        assert "files" in result.final_store

    def test_print_output_non_empty(self) -> None:
        """print(files) produces output."""
        orch = _build_orchestrator(S06_PLAN, S06_TOOLS, S06_SIGS)
        result = _run(
            orch.run("Search Google Drive for files named 'budget' and print the file names.")
        )
        assert len(result.print_outputs) >= 1


# ---------------------------------------------------------------------------
# S07 — Summarise last three emails (multi-step)
# ---------------------------------------------------------------------------

S07_SENTINEL = "__S07_EMAIL_LIST_M3N4__"

S07_PLAN = """\
emails = list_emails(count=3)
print(emails)
"""

S07_TOOLS: dict[str, Any] = {
    "list_emails": _make_tool(
        "list_emails",
        return_raw=[
            {"subject": f"Email 1 {S07_SENTINEL}", "body": "..."},
            {"subject": "Email 2", "body": "..."},
            {"subject": "Email 3", "body": "..."},
        ],
    ),
}

S07_SIGS = [
    ToolSignature("list_emails", "count: int", "list", "List the last N emails."),
]


class TestS07SummariseLastThreeEmails:
    """S07 — multi-step: list_emails(count=3) → print."""

    def test_trace_non_empty(self) -> None:
        """Trace is non-empty (at least one tool was called)."""
        orch = _build_orchestrator(S07_PLAN, S07_TOOLS, S07_SIGS)
        result = _run(orch.run("Fetch the last three emails and print a one-line summary."))
        assert len(result.trace) >= 1

    def test_trace_contains_only_registered_tool_names(self) -> None:
        """All trace entries reference registered tool names."""
        registered = set(S07_TOOLS.keys())
        orch = _build_orchestrator(S07_PLAN, S07_TOOLS, S07_SIGS)
        result = _run(orch.run("Fetch the last three emails and print a one-line summary."))
        for record in result.trace:
            assert record.tool_name in registered

    def test_list_emails_count_argument(self) -> None:
        """list_emails is called with count=3."""
        orch = _build_orchestrator(S07_PLAN, S07_TOOLS, S07_SIGS)
        result = _run(orch.run("Fetch the last three emails and print a one-line summary."))
        assert result.trace[0].args.get("count") == 3

    def test_final_store_contains_emails(self) -> None:
        """final_store contains 'emails' after list_emails."""
        orch = _build_orchestrator(S07_PLAN, S07_TOOLS, S07_SIGS)
        result = _run(orch.run("Fetch the last three emails and print a one-line summary."))
        # Snapshot taken before assignment; use final_store.
        assert "emails" in result.final_store


# ---------------------------------------------------------------------------
# S08 — Compose and send a reply (multi-step)
# ---------------------------------------------------------------------------

S08_SENTINEL = "__S08_REPLY_O5P6__"

S08_PLAN = """\
email = get_email()
result = send_email(to="carol@example.com", subject="Re: FYI", body="Got it, thanks!")
"""

S08_TOOLS: dict[str, Any] = {
    "get_email": _make_tool(
        "get_email",
        return_raw={"from": f"carol@example.com {S08_SENTINEL}", "subject": "FYI"},
    ),
    "send_email": _make_tool("send_email", return_raw={"status": "sent"}),
}

S08_SIGS = [
    ToolSignature("get_email", "", "dict", "Retrieve the latest email."),
    ToolSignature("send_email", "to: str, subject: str, body: str", "dict", "Send an email."),
]


class TestS08ComposeAndSendReply:
    """S08 — multi-step: get_email → send_email (reply)."""

    def test_trace_length_is_two(self) -> None:
        """Trace has exactly two records."""
        orch = _build_orchestrator(S08_PLAN, S08_TOOLS, S08_SIGS)
        result = _run(
            orch.run("Get the latest email and reply to its sender saying 'Got it, thanks!'")
        )
        assert len(result.trace) == 2

    def test_second_tool_is_send_email(self) -> None:
        """Second trace record is send_email."""
        orch = _build_orchestrator(S08_PLAN, S08_TOOLS, S08_SIGS)
        result = _run(
            orch.run("Get the latest email and reply to its sender saying 'Got it, thanks!'")
        )
        assert result.trace[1].tool_name == "send_email"

    def test_first_tool_is_get_email(self) -> None:
        """First trace record is get_email."""
        orch = _build_orchestrator(S08_PLAN, S08_TOOLS, S08_SIGS)
        result = _run(
            orch.run("Get the latest email and reply to its sender saying 'Got it, thanks!'")
        )
        assert result.trace[0].tool_name == "get_email"

    def test_reply_body_in_args(self) -> None:
        """Reply body contains 'Got it, thanks!'."""
        orch = _build_orchestrator(S08_PLAN, S08_TOOLS, S08_SIGS)
        result = _run(
            orch.run("Get the latest email and reply to its sender saying 'Got it, thanks!'")
        )
        assert "Got it, thanks!" in result.trace[1].args.get("body", "")


# ---------------------------------------------------------------------------
# S09 — Delete a specific file from Drive (single-step)
# ---------------------------------------------------------------------------

S09_SENTINEL = "__S09_DELETE_RESULT_Q7R8__"

S09_PLAN = """\
result = delete_drive_file(file_id="doc_789")
"""

S09_TOOLS: dict[str, Any] = {
    "delete_drive_file": _make_tool(
        "delete_drive_file",
        return_raw={"deleted": True, "id": f"doc_789 {S09_SENTINEL}"},
    ),
}

S09_SIGS = [
    ToolSignature("delete_drive_file", "file_id: str", "dict", "Delete a Drive file."),
]


class TestS09DeleteDriveFile:
    """S09 — single-step: delete_drive_file with literal file_id."""

    def test_trace_length_is_one(self) -> None:
        """Trace has exactly one record."""
        orch = _build_orchestrator(S09_PLAN, S09_TOOLS, S09_SIGS)
        result = _run(orch.run("Delete the file with ID 'doc_789' from Google Drive."))
        assert len(result.trace) == 1

    def test_trace_file_id_argument(self) -> None:
        """file_id argument is 'doc_789'."""
        orch = _build_orchestrator(S09_PLAN, S09_TOOLS, S09_SIGS)
        result = _run(orch.run("Delete the file with ID 'doc_789' from Google Drive."))
        assert result.trace[0].args["file_id"] == "doc_789"

    def test_trace_tool_name(self) -> None:
        """Trace records delete_drive_file."""
        orch = _build_orchestrator(S09_PLAN, S09_TOOLS, S09_SIGS)
        result = _run(orch.run("Delete the file with ID 'doc_789' from Google Drive."))
        assert result.trace[0].tool_name == "delete_drive_file"

    def test_final_store_contains_result(self) -> None:
        """final_store contains 'result' after delete_drive_file."""
        orch = _build_orchestrator(S09_PLAN, S09_TOOLS, S09_SIGS)
        result = _run(orch.run("Delete the file with ID 'doc_789' from Google Drive."))
        # Snapshot taken before assignment; use final_store.
        assert "result" in result.final_store


# ---------------------------------------------------------------------------
# S10 — Multi-tool workflow: read email, create event, reply (multi-step)
# ---------------------------------------------------------------------------

S10_SENTINEL = "__S10_WORKFLOW_S9T0__"

S10_PLAN = """\
email = get_email()
event = create_calendar_event(title="Meeting", date="2026-03-25", time="10:00")
result = send_email(to="dave@example.com", subject="Booked", body="Meeting is booked!")
"""

S10_TOOLS: dict[str, Any] = {
    "get_email": _make_tool(
        "get_email",
        return_raw={
            "from": "dave@example.com",
            "body": f"Let's meet on 2026-03-25 {S10_SENTINEL}",
        },
    ),
    "create_calendar_event": _make_tool(
        "create_calendar_event",
        return_raw={"id": "evt_456"},
    ),
    "send_email": _make_tool("send_email", return_raw={"status": "sent"}),
}

S10_SIGS = [
    ToolSignature("get_email", "", "dict", "Retrieve the latest email."),
    ToolSignature(
        "create_calendar_event",
        "title: str, date: str, time: str",
        "dict",
        "Create a calendar event.",
    ),
    ToolSignature("send_email", "to: str, subject: str, body: str", "dict", "Send an email."),
]


class TestS10MultiToolWorkflow:
    """S10 — multi-step: get_email → create_calendar_event → send_email."""

    def test_trace_length_is_three(self) -> None:
        """Trace has exactly three records matching trace_shape."""
        orch = _build_orchestrator(S10_PLAN, S10_TOOLS, S10_SIGS)
        result = _run(
            orch.run(
                "Read the latest email, create a calendar event for the date mentioned "
                "in it, and reply confirming it's booked."
            )
        )
        assert len(result.trace) == 3

    def test_trace_order_matches_shape(self) -> None:
        """Tool call order: get_email, create_calendar_event, send_email."""
        orch = _build_orchestrator(S10_PLAN, S10_TOOLS, S10_SIGS)
        result = _run(
            orch.run(
                "Read the latest email, create a calendar event for the date mentioned "
                "in it, and reply confirming it's booked."
            )
        )
        assert result.trace[0].tool_name == "get_email"
        assert result.trace[1].tool_name == "create_calendar_event"
        assert result.trace[2].tool_name == "send_email"

    def test_send_email_to_argument(self) -> None:
        """reply destination is dave@example.com."""
        orch = _build_orchestrator(S10_PLAN, S10_TOOLS, S10_SIGS)
        result = _run(
            orch.run(
                "Read the latest email, create a calendar event for the date mentioned "
                "in it, and reply confirming it's booked."
            )
        )
        assert result.trace[2].args["to"] == "dave@example.com"

    def test_create_event_date_argument(self) -> None:
        """create_calendar_event called with date='2026-03-25'."""
        orch = _build_orchestrator(S10_PLAN, S10_TOOLS, S10_SIGS)
        result = _run(
            orch.run(
                "Read the latest email, create a calendar event for the date mentioned "
                "in it, and reply confirming it's booked."
            )
        )
        assert result.trace[1].args["date"] == "2026-03-25"

    def test_memory_snapshot_after_third_call(self) -> None:
        """send_email snapshot has prior bindings; final_store has all three.

        The snapshot for trace[2] (send_email) is taken before 'result' is
        assigned, but 'email' and 'event' (from earlier statements) are present.
        final_store has all three bindings.
        """
        orch = _build_orchestrator(S10_PLAN, S10_TOOLS, S10_SIGS)
        result = _run(
            orch.run(
                "Read the latest email, create a calendar event for the date mentioned "
                "in it, and reply confirming it's booked."
            )
        )
        snap = result.trace[2].memory_snapshot
        assert "email" in snap
        assert "event" in snap
        # 'result' is bound after send_email returns; visible in final_store.
        assert "result" in result.final_store


# ---------------------------------------------------------------------------
# Scenario summary parametrised smoke test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan,tools,sigs,query,expected_tool_order",
    [
        (
            S01_PLAN,
            S01_TOOLS,
            S01_SIGS,
            "Retrieve the latest email and print its subject line.",
            ["get_email"],
        ),
        (
            S02_PLAN,
            S02_TOOLS,
            S02_SIGS,
            "Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'.",
            ["send_email"],
        ),
        (
            S03_PLAN,
            S03_TOOLS,
            S03_SIGS,
            "Get my next calendar event and print a one-sentence summary.",
            ["get_calendar_event"],
        ),
        (
            S04_PLAN,
            S04_TOOLS,
            S04_SIGS,
            "Get the latest email and forward it to bob@example.com.",
            ["get_email", "send_email"],
        ),
        (
            S05_PLAN,
            S05_TOOLS,
            S05_SIGS,
            "Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00.",
            ["create_calendar_event"],
        ),
        (
            S06_PLAN,
            S06_TOOLS,
            S06_SIGS,
            "Search Google Drive for files named 'budget' and print the file names.",
            ["search_drive"],
        ),
        (
            S07_PLAN,
            S07_TOOLS,
            S07_SIGS,
            "Fetch the last three emails and print a one-line summary.",
            ["list_emails"],
        ),
        (
            S08_PLAN,
            S08_TOOLS,
            S08_SIGS,
            "Get the latest email and reply to its sender saying 'Got it, thanks!'",
            ["get_email", "send_email"],
        ),
        (
            S09_PLAN,
            S09_TOOLS,
            S09_SIGS,
            "Delete the file with ID 'doc_789' from Google Drive.",
            ["delete_drive_file"],
        ),
        (
            S10_PLAN,
            S10_TOOLS,
            S10_SIGS,
            "Read the latest email, create a calendar event and reply.",
            ["get_email", "create_calendar_event", "send_email"],
        ),
    ],
    ids=["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10"],
)
def test_scenario_trace_shape(
    plan: str,
    tools: dict[str, Any],
    sigs: list[ToolSignature],
    query: str,
    expected_tool_order: list[str],
) -> None:
    """Each scenario produces an execution trace matching its expected shape.

    This parametrised test is the primary per-scenario pass/fail status check
    suitable for the exit criteria report.
    """
    orch = _build_orchestrator(plan, tools, sigs)
    result = _run(orch.run(query))
    actual_order = [r.tool_name for r in result.trace]
    assert actual_order == expected_tool_order, (
        f"Expected tool order {expected_tool_order}, got {actual_order}"
    )
