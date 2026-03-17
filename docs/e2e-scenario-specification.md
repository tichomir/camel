# CaMeL Milestone 2 — E2E Scenario & Adversarial Redaction Specification

_Author: Software Architect Persona | Date: 2026-03-17_
_Referenced by: ADR-008, `tests/test_isolation_harness.py`, `tests/test_redaction_completeness.py`_

---

## Overview

This document is the authoritative inventory for:

1. **10 representative end-to-end task scenarios** (§1) — mix of single-step
   and multi-step plans, covering the core tool domains used in AgentDojo.
   Each scenario specifies: task description, tools invoked, expected execution
   trace shape, and single/multi-step label.

2. **10 adversarial redaction cases** (§2) — each designed to trigger the
   exception-redaction engine with a specific provenance pattern and assert the
   correct redacted form.

3. **Multi-backend swap contract** (§3) — how to execute any scenario against
   Claude or Gemini with a single configuration change.

All scenarios execute **without security policies active** (policy engine is
`None`) so that execution traces reflect correct data-flow without policy
denials — security policy correctness is a Milestone 3 concern.

---

## §1 — End-to-End Task Scenarios

### Scenario ID conventions

| Field          | Convention                                                  |
|----------------|-------------------------------------------------------------|
| `scenario_id`  | `S01`–`S10`                                                 |
| `step_type`    | `single` (one tool call) or `multi` (two or more)          |
| `sentinel`     | Unique string embedded in tool return values for I-1 checks |
| `trace_shape`  | Ordered list of expected `tool_name` values in the trace   |

---

### S01 — Fetch and Print Email Subject (single-step)

| Field           | Value                                                                  |
|-----------------|------------------------------------------------------------------------|
| `scenario_id`   | `S01`                                                                  |
| `step_type`     | `single`                                                               |
| `user_query`    | `"Retrieve the latest email and print its subject line."`             |
| `tools_invoked` | `get_email`                                                            |
| `trace_shape`   | `["get_email"]`                                                        |
| `sentinel`      | `__S01_EMAIL_RETURN_9A1B__`                                            |

**Tool stub contract:**

```python
def get_email() -> CaMeLValue:
    return wrap(
        {"subject": "Meeting notes __S01_EMAIL_RETURN_9A1B__", "body": "..."},
        sources=frozenset({"get_email"}),
    )
```

**Expected plan shape (approximate):**

```python
email = get_email()
subject = query_quarantined_llm(email, SubjectSchema)
print(subject.subject)
```

**Trace assertion:** `len(trace) == 1` and `trace[0].tool_name == "get_email"`.

**Isolation assertion (I-1):** No P-LLM message contains
`"__S01_EMAIL_RETURN_9A1B__"`.

---

### S02 — Send a Fixed Email (single-step)

| Field           | Value                                                              |
|-----------------|--------------------------------------------------------------------|
| `scenario_id`   | `S02`                                                              |
| `step_type`     | `single`                                                           |
| `user_query`    | `"Send an email to alice@example.com with subject 'Hello' and body 'Hi Alice'."`  |
| `tools_invoked` | `send_email`                                                       |
| `trace_shape`   | `["send_email"]`                                                   |
| `sentinel`      | `__S02_SEND_RESULT_C3D4__`                                         |

**Tool stub contract:**

```python
def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap(
        {"status": "sent __S02_SEND_RESULT_C3D4__"},
        sources=frozenset({"send_email"}),
    )
```

**Expected plan shape:**

```python
result = send_email(
    to="alice@example.com",
    subject="Hello",
    body="Hi Alice",
)
```

**Trace assertion:** `len(trace) == 1`, `trace[0].args["to"] == "alice@example.com"`.

---

### S03 — Read Calendar and Summarise Next Event (multi-step)

| Field           | Value                                                                     |
|-----------------|---------------------------------------------------------------------------|
| `scenario_id`   | `S03`                                                                     |
| `step_type`     | `multi`                                                                   |
| `user_query`    | `"Get my next calendar event and print a one-sentence summary."`         |
| `tools_invoked` | `get_calendar_event`, `query_quarantined_llm`                             |
| `trace_shape`   | `["get_calendar_event"]`                                                  |
| `sentinel`      | `__S03_CAL_RETURN_E5F6__`                                                 |

**Tool stub contract:**

```python
def get_calendar_event() -> CaMeLValue:
    return wrap(
        {"title": "Team standup __S03_CAL_RETURN_E5F6__", "time": "09:00"},
        sources=frozenset({"get_calendar_event"}),
    )
```

**Expected plan shape:**

```python
event = get_calendar_event()
summary = query_quarantined_llm(event, EventSummarySchema)
print(summary.text)
```

**Trace assertion:** `len(trace) == 1` (Q-LLM is a builtin, not a traced tool).

**Isolation assertion (I-2):** Any raw Q-LLM stub response text
`"__S03_QLLM_RAW_RESPONSE__"` must not appear in any P-LLM message.

---

### S04 — Forward Email to Another Recipient (multi-step)

| Field           | Value                                                                               |
|-----------------|-------------------------------------------------------------------------------------|
| `scenario_id`   | `S04`                                                                               |
| `step_type`     | `multi`                                                                             |
| `user_query`    | `"Get the latest email and forward it to bob@example.com."`                        |
| `tools_invoked` | `get_email`, `send_email`                                                           |
| `trace_shape`   | `["get_email", "send_email"]`                                                       |
| `sentinel`      | `__S04_FWD_EMAIL_7G8H__`                                                            |

**Tool stub contracts:**

```python
def get_email() -> CaMeLValue:
    return wrap(
        {"subject": "Re: project __S04_FWD_EMAIL_7G8H__", "body": "See attached."},
        sources=frozenset({"get_email"}),
    )

def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap({"status": "sent"}, sources=frozenset({"send_email"}))
```

**Expected plan shape:**

```python
email = get_email()
body_text = query_quarantined_llm(email, EmailBodySchema)
result = send_email(
    to="bob@example.com",
    subject=query_quarantined_llm(email, SubjectSchema).subject,
    body=body_text.body,
)
```

**Trace assertion:** `len(trace) == 2`, `trace[1].args["to"] == "bob@example.com"`.

---

### S05 — Create a Calendar Event (single-step)

| Field           | Value                                                                       |
|-----------------|-----------------------------------------------------------------------------|
| `scenario_id`   | `S05`                                                                       |
| `step_type`     | `single`                                                                    |
| `user_query`    | `"Create a calendar event titled 'Sprint Review' on 2026-03-20 at 14:00."` |
| `tools_invoked` | `create_calendar_event`                                                     |
| `trace_shape`   | `["create_calendar_event"]`                                                 |
| `sentinel`      | `__S05_CAL_CREATE_9I0J__`                                                   |

**Tool stub contract:**

```python
def create_calendar_event(title: str, date: str, time: str) -> CaMeLValue:
    return wrap(
        {"id": "evt_123 __S05_CAL_CREATE_9I0J__"},
        sources=frozenset({"create_calendar_event"}),
    )
```

**Trace assertion:** `trace[0].args["title"] == "Sprint Review"`.

---

### S06 — Search Drive and Print File Names (multi-step)

| Field           | Value                                                                           |
|-----------------|---------------------------------------------------------------------------------|
| `scenario_id`   | `S06`                                                                           |
| `step_type`     | `multi`                                                                         |
| `user_query`    | `"Search Google Drive for files named 'budget' and print the file names."`    |
| `tools_invoked` | `search_drive`                                                                  |
| `trace_shape`   | `["search_drive"]`                                                              |
| `sentinel`      | `__S06_DRIVE_RETURN_K1L2__`                                                     |

**Tool stub contract:**

```python
def search_drive(query: str) -> CaMeLValue:
    return wrap(
        [
            {"name": "budget_2026.xlsx __S06_DRIVE_RETURN_K1L2__", "id": "f1"},
            {"name": "budget_draft.docx", "id": "f2"},
        ],
        sources=frozenset({"search_drive"}),
    )
```

**Expected plan shape:**

```python
files = search_drive(query="budget")
names = query_quarantined_llm(files, FileListSchema)
for name in names.file_names:
    print(name)
```

**Trace assertion:** `len(trace) == 1`, `trace[0].args["arg0"] == "budget"` (or `query`).

---

### S07 — Summarise Last Three Emails (multi-step)

| Field           | Value                                                                                    |
|-----------------|------------------------------------------------------------------------------------------|
| `scenario_id`   | `S07`                                                                                    |
| `step_type`     | `multi`                                                                                  |
| `user_query`    | `"Fetch the last three emails and print a one-line summary for each."`                  |
| `tools_invoked` | `get_email` (called 3×) or `list_emails`                                                |
| `trace_shape`   | `["list_emails"]` or `["get_email", "get_email", "get_email"]`                          |
| `sentinel`      | `__S07_EMAIL_LIST_M3N4__`                                                                |

**Tool stub contract:**

```python
def list_emails(count: int) -> CaMeLValue:
    return wrap(
        [
            {"subject": "Email 1 __S07_EMAIL_LIST_M3N4__", "body": "..."},
            {"subject": "Email 2", "body": "..."},
            {"subject": "Email 3", "body": "..."},
        ],
        sources=frozenset({"list_emails"}),
    )
```

**Trace assertion:** `len(trace) >= 1`.  The exact shape depends on the P-LLM
plan; the test asserts the shape is non-empty and contains only registered tool
names.

---

### S08 — Compose and Send a Reply (multi-step)

| Field           | Value                                                                             |
|-----------------|-----------------------------------------------------------------------------------|
| `scenario_id`   | `S08`                                                                             |
| `step_type`     | `multi`                                                                           |
| `user_query`    | `"Get the latest email and reply to its sender saying 'Got it, thanks!'"`       |
| `tools_invoked` | `get_email`, `send_email`                                                         |
| `trace_shape`   | `["get_email", "send_email"]`                                                     |
| `sentinel`      | `__S08_REPLY_O5P6__`                                                              |

**Tool stub contracts:**

```python
def get_email() -> CaMeLValue:
    return wrap(
        {"from": "carol@example.com __S08_REPLY_O5P6__", "subject": "FYI"},
        sources=frozenset({"get_email"}),
    )

def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap({"status": "sent"}, sources=frozenset({"send_email"}))
```

**Trace assertion:** `len(trace) == 2`, `trace[1].tool_name == "send_email"`.

---

### S09 — Delete a Specific File from Drive (single-step)

| Field           | Value                                                                          |
|-----------------|--------------------------------------------------------------------------------|
| `scenario_id`   | `S09`                                                                          |
| `step_type`     | `single`                                                                       |
| `user_query`    | `"Delete the file with ID 'doc_789' from Google Drive."`                      |
| `tools_invoked` | `delete_drive_file`                                                            |
| `trace_shape`   | `["delete_drive_file"]`                                                        |
| `sentinel`      | `__S09_DELETE_RESULT_Q7R8__`                                                   |

**Tool stub contract:**

```python
def delete_drive_file(file_id: str) -> CaMeLValue:
    return wrap(
        {"deleted": True, "id": "doc_789 __S09_DELETE_RESULT_Q7R8__"},
        sources=frozenset({"delete_drive_file"}),
    )
```

**Trace assertion:** `trace[0].args["file_id"] == "doc_789"`.

---

### S10 — Multi-Tool Workflow: Read Email, Create Event, Reply (multi-step)

| Field           | Value                                                                                                          |
|-----------------|----------------------------------------------------------------------------------------------------------------|
| `scenario_id`   | `S10`                                                                                                          |
| `step_type`     | `multi`                                                                                                        |
| `user_query`    | `"Read the latest email, create a calendar event for the date mentioned in it, and reply confirming it's booked."` |
| `tools_invoked` | `get_email`, `create_calendar_event`, `send_email`                                                            |
| `trace_shape`   | `["get_email", "create_calendar_event", "send_email"]`                                                        |
| `sentinel`      | `__S10_WORKFLOW_S9T0__`                                                                                        |

**Tool stub contracts:**

```python
def get_email() -> CaMeLValue:
    return wrap(
        {"from": "dave@example.com", "body": "Let's meet on 2026-03-25 __S10_WORKFLOW_S9T0__"},
        sources=frozenset({"get_email"}),
    )

def create_calendar_event(title: str, date: str, time: str) -> CaMeLValue:
    return wrap({"id": "evt_456"}, sources=frozenset({"create_calendar_event"}))

def send_email(to: str, subject: str, body: str) -> CaMeLValue:
    return wrap({"status": "sent"}, sources=frozenset({"send_email"}))
```

**Trace assertion:** `len(trace) == 3`, trace order matches `trace_shape`.

---

### Scenario Summary Table

| ID  | Description                          | Step type | Tools                                      | Trace length |
|-----|--------------------------------------|-----------|--------------------------------------------|--------------|
| S01 | Fetch and print email subject        | single    | `get_email`                                | 1            |
| S02 | Send a fixed email                   | single    | `send_email`                               | 1            |
| S03 | Read calendar, summarise next event  | multi     | `get_calendar_event`                       | 1            |
| S04 | Forward email to another recipient   | multi     | `get_email`, `send_email`                  | 2            |
| S05 | Create a calendar event              | single    | `create_calendar_event`                    | 1            |
| S06 | Search Drive and print file names    | multi     | `search_drive`                             | 1            |
| S07 | Summarise last three emails          | multi     | `list_emails`                              | ≥1           |
| S08 | Compose and send a reply             | multi     | `get_email`, `send_email`                  | 2            |
| S09 | Delete a specific Drive file         | single    | `delete_drive_file`                        | 1            |
| S10 | Read email, create event, reply      | multi     | `get_email`, `create_calendar_event`, `send_email` | 3    |

---

## §2 — Adversarial Redaction Cases

Each case targets a specific redaction invariant (I-3).  All cases use the
`ExceptionRedactor` + `CaMeLOrchestrator` stack; no real LLM calls are required.

### Redaction case conventions

| Field               | Description                                               |
|---------------------|-----------------------------------------------------------|
| `case_id`           | `R01`–`R10`                                               |
| `trigger_mechanism` | How the exception is provoked and what sets its provenance |
| `expected_redacted_form` | Which fields of `RedactedError` must/must not be set  |
| `assertion_method`  | What the test checks on the `RedactedError` or retry prompt |

---

### R01 — Untrusted string in store → message body redacted

| Field               | Value                                                           |
|---------------------|-----------------------------------------------------------------|
| `case_id`           | `R01`                                                           |
| `trigger_mechanism` | Store contains a `CaMeLValue` with `sources={"get_email"}`. A `TypeError` is raised by the interpreter. |
| `expected_redacted_form` | `error_type="TypeError"`, `lineno` set, `message=None`, `trust_level="untrusted"` |
| `assertion_method`  | `assert redacted.message is None` and `"SECRET_ERROR_BODY" not in retry_prompt` |

**Setup:**

```python
store = {
    "email": wrap(
        "SECRET_ERROR_BODY_R01",
        sources=frozenset({"get_email"}),
    )
}
exc = TypeError("value 'SECRET_ERROR_BODY_R01' is not subscriptable")
exc.lineno = 3  # type: ignore[attr-defined]
redacted = ExceptionRedactor().classify(exc, store)
```

---

### R02 — NotEnoughInformationError → fully redacted (no type name leakage of data)

| Field               | Value                                                         |
|---------------------|---------------------------------------------------------------|
| `case_id`           | `R02`                                                         |
| `trigger_mechanism` | Q-LLM stub sets `have_enough_information=False`; orchestrator catches `NotEnoughInformationError`. |
| `expected_redacted_form` | `error_type="NotEnoughInformationError"`, `lineno=None`, `message=None`, `trust_level="not_enough_information"` |
| `assertion_method`  | `assert redacted.lineno is None` and `assert redacted.message is None` |

---

### R03 — Trusted-only store → full message included

| Field               | Value                                                                |
|---------------------|----------------------------------------------------------------------|
| `case_id`           | `R03`                                                                |
| `trigger_mechanism` | Store contains only `sources={"User literal", "CaMeL"}` values. A `ValueError` is raised. |
| `expected_redacted_form` | `error_type="ValueError"`, `lineno` set, `message` == full exception string, `trust_level="trusted"` |
| `assertion_method`  | `assert redacted.message == str(exc)` and `assert redacted.trust_level == "trusted"` |

---

### R04 — Mixed trusted + untrusted store → message redacted

| Field               | Value                                                                   |
|---------------------|-------------------------------------------------------------------------|
| `case_id`           | `R04`                                                                   |
| `trigger_mechanism` | Store has one `sources={"User literal"}` value AND one `sources={"search_drive"}` value. A `KeyError` is raised with sensitive data in its message. |
| `expected_redacted_form` | `trust_level="untrusted"`, `message=None`                           |
| `assertion_method`  | `assert redacted.message is None` — the single untrusted source contaminates the whole store |

---

### R05 — CamelNEIE variant (interpreter-layer NotEnoughInformationError)

| Field               | Value                                                              |
|---------------------|--------------------------------------------------------------------|
| `case_id`           | `R05`                                                              |
| `trigger_mechanism` | `camel.exceptions.NotEnoughInformationError` raised (interpreter variant, not LLM variant). |
| `expected_redacted_form` | Same as R02: `lineno=None`, `message=None`, `trust_level="not_enough_information"` |
| `assertion_method`  | `assert redacted.trust_level == "not_enough_information"` — both NEIE variants must produce identical redaction |

---

### R06 — Untrusted exception with known lineno → lineno preserved, message dropped

| Field               | Value                                                                  |
|---------------------|------------------------------------------------------------------------|
| `case_id`           | `R06`                                                                  |
| `trigger_mechanism` | Untrusted store. Exception has `lineno=7` attribute (simulating an interpreter-annotated exception). |
| `expected_redacted_form` | `lineno=7`, `message=None`, `trust_level="untrusted"`               |
| `assertion_method`  | `assert redacted.lineno == 7` and `assert redacted.message is None` — lineno is safe to expose; message is not |

---

### R07 — Retry prompt never contains raw exception text for untrusted case

| Field               | Value                                                              |
|---------------------|--------------------------------------------------------------------|
| `case_id`           | `R07`                                                              |
| `trigger_mechanism` | Full orchestrator run. Tool raises a `RuntimeError("SENSITIVE_PAYLOAD_R07")`. Tool's return `CaMeLValue` has `sources={"external_api"}`. |
| `expected_redacted_form` | P-LLM retry prompt must not contain `"SENSITIVE_PAYLOAD_R07"` |
| `assertion_method`  | `assert "SENSITIVE_PAYLOAD_R07" not in retry_prompt_text` — verified via `RecordingBackend` |

---

### R08 — NotEnoughInformationError redaction does not leak query data

| Field               | Value                                                               |
|---------------------|---------------------------------------------------------------------|
| `case_id`           | `R08`                                                               |
| `trigger_mechanism` | Q-LLM stub receives an untrusted document containing `"CLASSIFIED_DATA_R08"`. Sets `have_enough_information=False`. |
| `expected_redacted_form` | `NotEnoughInformationError` redacted; no P-LLM message contains `"CLASSIFIED_DATA_R08"` |
| `assertion_method`  | `assert "CLASSIFIED_DATA_R08" not in all_plllm_messages` |

---

### R09 — Empty store (no variables) → trusted classification

| Field               | Value                                                             |
|---------------------|-------------------------------------------------------------------|
| `case_id`           | `R09`                                                             |
| `trigger_mechanism` | `ExceptionRedactor.classify()` called with `interpreter_store_snapshot={}`. A `NameError` is raised on first statement. |
| `expected_redacted_form` | `trust_level="trusted"`, `message` is the full `NameError` string |
| `assertion_method`  | `assert redacted.trust_level == "trusted"` — empty store has no untrusted sources |

---

### R10 — Exception with no lineno attribute → lineno=None

| Field               | Value                                                                 |
|---------------------|-----------------------------------------------------------------------|
| `case_id`           | `R10`                                                                 |
| `trigger_mechanism` | Untrusted store. Raw `Exception("no lineno here")` raised — does not have a `lineno` attribute. |
| `expected_redacted_form` | `lineno=None`, `message=None`, `trust_level="untrusted"`           |
| `assertion_method`  | `assert redacted.lineno is None` — `getattr(exc, "lineno", None)` fallback must be used |

---

### Adversarial Case Summary Table

| ID  | Trigger                                              | Expected `trust_level`        | `message` field | `lineno` field |
|-----|------------------------------------------------------|-------------------------------|-----------------|----------------|
| R01 | Untrusted store + `TypeError`                        | `"untrusted"`                 | `None`          | set            |
| R02 | `NotEnoughInformationError` (LLM variant)            | `"not_enough_information"`    | `None`          | `None`         |
| R03 | Trusted-only store + `ValueError`                   | `"trusted"`                   | full string     | set            |
| R04 | Mixed store + `KeyError`                             | `"untrusted"`                 | `None`          | set            |
| R05 | `NotEnoughInformationError` (interpreter variant)    | `"not_enough_information"`    | `None`          | `None`         |
| R06 | Untrusted store + exception with `lineno=7`          | `"untrusted"`                 | `None`          | `7`            |
| R07 | Full orchestrator — tool raises with sensitive text  | retry prompt assertion        | not in prompt   | —              |
| R08 | Q-LLM insufficient data — classified data in input  | NEIE redaction + I-2 check   | not in prompt   | —              |
| R09 | Empty store + `NameError`                            | `"trusted"`                   | full string     | set            |
| R10 | Untrusted store + exception with no `lineno`         | `"untrusted"`                 | `None`          | `None`         |

---

## §3 — Multi-Backend Swap Contract

### Configuration

```python
import os
from camel.llm.backend import get_backend

provider = os.environ.get("CAMEL_TEST_BACKEND", "mock")

if provider == "mock":
    backend = StubBackend(responses=scenario.stub_responses)
else:
    backend = get_backend(
        provider,
        api_key=os.environ[f"CAMEL_{provider.upper()}_API_KEY"],
        model=os.environ.get(f"CAMEL_{provider.upper()}_MODEL", DEFAULT_MODELS[provider]),
    )
```

### Swap Verification Test

The multi-backend swap test (`tests/test_backend_swap.py`) does the following:

1. Constructs a `ClaudeBackend`-like mock and a `GeminiBackend`-like mock using
   `MagicMock()` satisfying `LLMBackend`.
2. Runs **S02** (the simplest single-step scenario) against each mock.
3. Asserts `isinstance(backend, LLMBackend)` for both.
4. Asserts the resulting `ExecutionTrace` is identical in shape regardless of
   which mock was used.
5. Confirms no import of `ClaudeBackend` or `GeminiBackend` is required in the
   test module — only `get_backend(provider)` is called.

### Default Model Values

| Provider  | Default model string         | Environment variable            |
|-----------|------------------------------|---------------------------------|
| `claude`  | `"claude-sonnet-4-6"`        | `CAMEL_CLAUDE_MODEL`            |
| `gemini`  | `"gemini-2.0-flash"`         | `CAMEL_GEMINI_MODEL`            |

---

## Approval Status

This document must be reviewed and approved before task-002
(`tests/test_isolation_harness.py` implementation) and task-003
(`tests/test_redaction_completeness.py` implementation) begin.

| Reviewer                  | Status   | Date       |
|---------------------------|----------|------------|
| Software Architect Persona | ✅ Draft  | 2026-03-17 |
| Implementation Lead        | ⬜ Pending | —         |
