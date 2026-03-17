# CaMeL Reference Policy Specification

_Authoritative design document for the six reference security policies shipped with CaMeL._
_Status: Approved | Version: 1.0 | Date: 2026-03-17_
_Audience: Backend Developer, QA Engineer implementing Milestone 3 — Reference Policy Library._

---

## Table of Contents

1. [Overview](#overview)
2. [Common Conventions](#common-conventions)
3. [Policy Specifications](#policy-specifications)
   - [send_email](#1-send_email)
   - [send_money](#2-send_money)
   - [create_calendar_event](#3-create_calendar_event)
   - [write_file](#4-write_file)
   - [post_message](#5-post_message)
   - [fetch_external_url](#6-fetch_external_url)
4. [AgentDojo Attack-Scenario Mapping](#agentdojo-attack-scenario-mapping)
5. [Edge Case Catalogue](#edge-case-catalogue)
6. [Implementation Notes](#implementation-notes)

---

## Overview

The reference policy library is the baseline set of security policies that enforces
Goals **G2** (prevent data-flow manipulation) and **G3** (prevent private data
exfiltration) for the six most common side-effecting tool categories found in
AgentDojo and real-world LLM agent deployments.

Each policy is a `PolicyFn` conforming to the interface in
`camel/policy/interfaces.py`:

```python
def policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    ...
```

Policies use only the three authorised helper functions:

| Helper | Module | What it checks |
|--------|--------|---------------|
| `is_trusted(v)` | `camel.policy.interfaces` | `v.sources ⊆ {"User literal", "CaMeL"}` |
| `can_readers_read_value(v, reader)` | `camel.policy.interfaces` | `reader ∈ v.readers` or `v.readers is Public` |
| `get_all_sources(v)` | `camel.policy.interfaces` | Returns `v.sources` frozenset |

Policies are **synchronous**, **deterministic**, and contain **no LLM calls** (NFR-2).

---

## Common Conventions

### Trusted vs. Untrusted

A `CaMeLValue` is *trusted* if and only if `is_trusted(v)` returns `True`, meaning
`v.sources` is non-empty and is a subset of `{"User literal", "CaMeL"}`. Any value
with an empty `sources` set is treated as **untrusted** (conservative default).

### Readers semantics

- `readers = Public` → any recipient is authorised.
- `readers = frozenset({"alice@example.com"})` → only Alice is authorised.
- `readers = frozenset()` → **no** recipient is authorised; the value must not
  be forwarded.

### Missing arguments

If an expected argument is absent from `kwargs`, the policy returns `Allowed()`
rather than raising — the tool's own argument validation handles missing required
fields. Policies check what is present.

### Propagation reminder

Capability fields propagate under union: a value derived from trusted and untrusted
inputs inherits the untrusted sources. Therefore:

```
concat(User_literal, email_body)  →  sources = {"User literal", "read_email"}
                                  →  is_trusted(...) == False
```

This is the fundamental mechanism that closes multi-hop injection paths.

---

## Policy Specifications

---

### 1. `send_email`

#### Description

Prevents an injected recipient address from causing private data to be forwarded
to an attacker. Also prevents private content from being sent to a recipient who
is not an authorised reader of that content.

#### Tool signature (expected)

```python
def send_email(
    to: str,          # recipient email address
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> None: ...
```

#### Logic

Evaluate in order; return the first `Denied` encountered.

**Check 1 — Recipient provenance:**

```
recipient_cv = kwargs["to"]

if not is_trusted(recipient_cv):
    # Recipient is not a user literal. Verify the recipient is already
    # an authorised reader of every piece of content being sent.
    for field in ("body", "subject"):
        content_cv = kwargs.get(field)
        if content_cv is not None:
            recipient_raw = recipient_cv.raw  # str
            if not can_readers_read_value(content_cv, recipient_raw):
                return Denied(f"recipient address from untrusted source is not an "
                              f"authorised reader of the email {field}")

return Allowed()
```

**Summary of allow/deny:**

| Condition | Result |
|-----------|--------|
| `to` is trusted (User literal) | `Allowed()` |
| `to` is untrusted AND `to.raw` is an authorised reader of `body` and `subject` | `Allowed()` |
| `to` is untrusted AND `to.raw` is NOT an authorised reader of `body` or `subject` | `Denied(...)` |

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `to` | `sources` (via `is_trusted`) | Determine if recipient is trusted |
| `body` | `readers` (via `can_readers_read_value`) | Authorised audience of email body |
| `subject` | `readers` (via `can_readers_read_value`) | Authorised audience of email subject |

#### Denial reasons (exact strings)

1. `"recipient address from untrusted source is not an authorised reader of the email body"`
2. `"recipient address from untrusted source is not an authorised reader of the email subject"`

#### Edge cases

- **Multi-recipient `to`:** If `to` is a comma-separated string or the raw value is
  a list, each address must independently pass the readers check. Implementations
  must split and check each token.
- **Transitive untrusted recipient:** `to` is assembled as
  `email_sender + "@company.com"` where `email_sender` came from `read_email` →
  `sources` contains `"read_email"` → `is_trusted` returns `False` → readers check
  applies.
- **Missing `body` or `subject`:** If only `to` and `body` are present, only the
  `body` readers check is performed; `subject` is skipped.
- **`body` with `readers=Public`:** `can_readers_read_value` returns `True` for any
  reader → passes the check even when `to` is untrusted.
- **`to` with empty sources:** `is_trusted` returns `False` (conservative) → full
  readers check applies.

---

### 2. `send_money`

#### Description

Prevents financial fraud by ensuring that both the transfer recipient and the
transfer amount originate exclusively from trusted (user-controlled) sources.
Any untrusted taint — even one hop removed — blocks the transfer.

This is the **strictest** reference policy: it applies `is_trusted` to both
high-stakes arguments and returns `Denied` on the first untrusted value found.

#### Tool signature (expected)

```python
def send_money(
    recipient: str,   # payee identifier (email, account number, etc.)
    amount: float,    # transfer amount in account currency
    currency: str | None = None,
    memo: str | None = None,
) -> None: ...
```

#### Logic

```
recipient_cv = kwargs.get("recipient")
amount_cv    = kwargs.get("amount")

if recipient_cv is not None and not is_trusted(recipient_cv):
    return Denied("transfer recipient originates from untrusted source — "
                  "possible data-flow manipulation")

if amount_cv is not None and not is_trusted(amount_cv):
    return Denied("transfer amount originates from untrusted source — "
                  "possible data-flow manipulation")

return Allowed()
```

`memo` and `currency` are **not** checked by this policy — they are lower-stakes
fields. Deployments may add additional policies for those fields.

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `recipient` | `sources` (via `is_trusted`) | Must be user-controlled |
| `amount` | `sources` (via `is_trusted`) | Must be user-controlled |

#### Denial reasons (exact strings)

1. `"transfer recipient originates from untrusted source — possible data-flow manipulation"`
2. `"transfer amount originates from untrusted source — possible data-flow manipulation"`

#### Edge cases

- **Amount derived from arithmetic on trusted and untrusted values:**
  `amount = user_balance - email_fee` → `sources = {"User literal", "get_balance", "read_email"}` →
  `is_trusted` returns `False` → `Denied`.
- **Multi-hop provenance:** Recipient extracted from a document, reformatted by
  Q-LLM, then concatenated with a domain string → `sources` inherits `"read_document"` →
  `is_trusted` returns `False` → `Denied`.
- **Recipient as integer account number typed by user:** `sources = {"User literal"}` →
  `is_trusted` returns `True` → `Allowed`.
- **Zero amount from user:** `wrap(0.0, sources=frozenset({"User literal"}))` →
  `is_trusted` returns `True` → `Allowed` (zero-amount transfer is a valid user
  intent; business logic is out of scope for this policy).

---

### 3. `create_calendar_event`

#### Description

Prevents injection of unauthorized attendees into a calendar event and prevents
private event content from being exposed to participants who are not authorised
readers of that content.

#### Tool signature (expected)

```python
def create_calendar_event(
    title: str,
    start_time: str,           # ISO-8601 datetime
    end_time: str,             # ISO-8601 datetime
    participants: list[str],   # list of attendee email addresses
    location: str | None = None,
    description: str | None = None,
) -> None: ...
```

#### Logic

```
participants_cv  = kwargs.get("participants")
title_cv         = kwargs.get("title")
description_cv   = kwargs.get("description")
location_cv      = kwargs.get("location")

# Step 1: If participants list is NOT trusted, check whether adding these
# participants is safe (they must already be authorised readers of the
# event content fields).
if participants_cv is not None and not is_trusted(participants_cv):
    content_fields = {
        "title": title_cv,
        "description": description_cv,
        "location": location_cv,
    }
    participant_list: list[str] = participants_cv.raw  # raw list[str]
    if not isinstance(participant_list, list):
        participant_list = [str(participants_cv.raw)]

    for participant in participant_list:
        for field_name, field_cv in content_fields.items():
            if field_cv is None:
                continue
            if not can_readers_read_value(field_cv, participant):
                return Denied(
                    f"calendar event {field_name!r} is not authorised to be "
                    f"shared with participant {participant!r} — "
                    f"untrusted participant list may include injected attendees"
                )

return Allowed()
```

**Summary of allow/deny:**

| Condition | Result |
|-----------|--------|
| `participants` is trusted (User literal) | `Allowed()` |
| `participants` is untrusted AND every participant is an authorised reader of every content field | `Allowed()` |
| `participants` is untrusted AND any participant is NOT an authorised reader of any content field | `Denied(...)` |

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `participants` | `sources` (via `is_trusted`), `value` (raw list) | Trust check + enumerate attendees |
| `title` | `readers` (via `can_readers_read_value`) | Authorised audience |
| `description` | `readers` (via `can_readers_read_value`) | Authorised audience |
| `location` | `readers` (via `can_readers_read_value`) | Authorised audience |

#### Denial reasons (exact strings)

1. `"calendar event 'title' is not authorised to be shared with participant '<email>' — untrusted participant list may include injected attendees"`
2. `"calendar event 'description' is not authorised to be shared with participant '<email>' — untrusted participant list may include injected attendees"`
3. `"calendar event 'location' is not authorised to be shared with participant '<email>' — untrusted participant list may include injected attendees"`

_(The `<email>` placeholder is substituted with the actual participant address at
evaluation time.)_

#### Edge cases

- **Empty `participants` list:** `participant_list` is `[]` → inner loop never
  executes → `Allowed()`.
- **Participants partially from user, partially from email:** Union of sources
  contains `"read_email"` → `is_trusted` returns `False` → readers check applies to
  all participants including the trusted ones.
- **`description` with `readers=Public`:** Every participant passes the readers check
  for that field; policy does not block.
- **`participants` is a single string (not a list):** Wrap in `[str(...)]` and
  check that single address.
- **`start_time` / `end_time` from untrusted source:** These fields are not checked
  by this policy (they are scheduling metadata, not content). Deployments may add
  a separate policy if scheduling manipulation is a concern.

---

### 4. `write_file`

#### Description

Prevents an agent from writing restricted content to the file system on behalf of
a user who is not an authorised reader of that content. Also prevents path injection
attacks by requiring the file path to originate from a trusted source.

The policy is **parameterised** by the identity of the file system owning user
(e.g. `"alice@example.com"`). This identity is provided at deployment time via
the policy factory function and captured in a closure.

#### Tool signature (expected)

```python
def write_file(
    path: str,
    content: str,
) -> None: ...
```

#### Logic (factory pattern)

```python
def make_write_file_policy(owner: str) -> PolicyFn:
    """Return a write_file policy for a file system owned by `owner`."""

    def write_file_policy(
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        path_cv    = kwargs.get("path")
        content_cv = kwargs.get("content")

        # Check 1: path must be trusted (no injection).
        if path_cv is not None and not is_trusted(path_cv):
            return Denied(
                "file path originates from untrusted source — "
                "possible path injection"
            )

        # Check 2: content readers must include the file system owner.
        if content_cv is not None:
            if not can_readers_read_value(content_cv, owner):
                return Denied(
                    f"file content is not authorised to be written by the "
                    f"file system owner {owner!r}"
                )

        return Allowed()

    return write_file_policy
```

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `path` | `sources` (via `is_trusted`) | Prevent path injection |
| `content` | `readers` (via `can_readers_read_value`) | Owner must be authorised reader |

#### Denial reasons (exact strings)

1. `"file path originates from untrusted source — possible path injection"`
2. `"file content is not authorised to be written by the file system owner '<owner>'"` _(substituted at runtime)_

#### Edge cases

- **Content with `readers=Public`:** `can_readers_read_value` returns `True` for any
  reader including the owner → `Allowed`.
- **Content with `readers=frozenset()` (no readers):** `can_readers_read_value`
  returns `False` for any reader including the owner → `Denied`. This represents
  content that must not be written anywhere.
- **Path injected from a document:** `path_cv.sources` contains `"read_document"` →
  `is_trusted` returns `False` → `Denied` on path check.
- **Content derived from two sources, one restricted to `{alice, bob}`:** If `owner`
  is `"alice@example.com"`, `can_readers_read_value` returns `True` → `Allowed`.
  If `owner` is `"charlie@example.com"`, returns `False` → `Denied`.
- **No `owner` configured:** Deployments must provide an `owner` string.
  If the tool is registered without the factory, the policy cannot reason about
  ownership; deployers should use the factory exclusively.

---

### 5. `post_message`

#### Description

Prevents an agent from posting restricted content to a channel whose membership
does not include all authorised readers of that content. Also prevents channel
injection — an attacker hijacking the target channel via a malicious message.

#### Tool signature (expected)

```python
def post_message(
    channel: str,    # channel name or identifier
    message: str,    # message content to post
) -> None: ...
```

#### Logic

```
channel_cv = kwargs.get("channel")
message_cv = kwargs.get("message")

# Check 1: channel must be trusted (prevent channel injection).
if channel_cv is not None and not is_trusted(channel_cv):
    return Denied(
        "channel identifier originates from untrusted source — "
        "possible injection to unauthorized channel"
    )

# Check 2: message content must be trusted OR have Public readers.
# Restricted-readers content may only be posted if the content itself is
# trusted (i.e. the user authored it directly). If the content is untrusted
# and has a restricted reader set, it cannot safely be posted to an
# unverified audience.
if message_cv is not None:
    if not is_trusted(message_cv) and not isinstance(message_cv.readers, _PublicType):
        return Denied(
            "message content from untrusted source has restricted readers — "
            "posting would exfiltrate data to an unauthorised audience"
        )

return Allowed()
```

**Summary of allow/deny:**

| `channel` | `message` | Result |
|-----------|-----------|--------|
| Trusted | Any | `Allowed()` (assuming message passes check 2) |
| Untrusted | — | `Denied(...)` |
| Trusted | Trusted | `Allowed()` |
| Trusted | Untrusted + `readers=Public` | `Allowed()` (content already public) |
| Trusted | Untrusted + restricted readers | `Denied(...)` |

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `channel` | `sources` (via `is_trusted`) | Prevent channel injection |
| `message` | `sources` (via `is_trusted`), `readers` type check | Prevent content exfiltration |

#### Denial reasons (exact strings)

1. `"channel identifier originates from untrusted source — possible injection to unauthorized channel"`
2. `"message content from untrusted source has restricted readers — posting would exfiltrate data to an unauthorised audience"`

#### Edge cases

- **Channel constructed by concatenating trusted prefix with untrusted suffix:**
  `"#" + email_body_channel` → mixed sources → `is_trusted` returns `False` →
  `Denied` on channel check.
- **Message with `readers=Public` and untrusted source:** The content is already
  public — any recipient is authorised → `Allowed`. This covers the case where an
  agent reads a public FAQ document and reposts it.
- **Message is a user literal (`sources={"User literal"}`):** `is_trusted` returns
  `True` → content check passes → `Allowed`.
- **Channel from user, message from cloud document with `readers={alice, bob}`:**
  Channel is trusted; message is untrusted with restricted readers →
  `Denied` (data exfiltration via Slack).
- **`_PublicType` import:** Policies must import `_PublicType` from `camel.value`
  to perform `isinstance(message_cv.readers, _PublicType)` check. Alternatively,
  use `message_cv.readers is Public` (both are equivalent given the singleton).

---

### 6. `fetch_external_url`

#### Description

Prevents data exfiltration through externally fetched URLs (SSRF and URL-parameter
exfiltration attacks). Both the URL itself and any query parameters must originate
exclusively from trusted sources. Any untrusted taint in the URL or parameters
immediately blocks the request.

#### Tool signature (expected)

```python
def fetch_external_url(
    url: str,
    params: dict[str, str] | None = None,   # query string parameters
    method: str = "GET",
    body: str | None = None,
) -> str: ...
```

#### Logic

```
url_cv    = kwargs.get("url")
params_cv = kwargs.get("params")
body_cv   = kwargs.get("body")

# Check 1: URL must be trusted (no SSRF / URL injection).
if url_cv is not None and not is_trusted(url_cv):
    return Denied(
        "URL originates from untrusted source — "
        "possible SSRF or data exfiltration via URL"
    )

# Check 2: query parameters must be trusted (no exfiltration via params).
if params_cv is not None and not is_trusted(params_cv):
    return Denied(
        "query parameters contain data from untrusted source — "
        "possible data exfiltration via URL parameters"
    )

# Check 3: request body must be trusted (no exfiltration via POST body).
if body_cv is not None and not is_trusted(body_cv):
    return Denied(
        "request body contains data from untrusted source — "
        "possible data exfiltration via request body"
    )

return Allowed()
```

#### CaMeLValue fields inspected

| Argument | Fields read | Purpose |
|----------|-------------|---------|
| `url` | `sources` (via `is_trusted`) | Prevent URL injection / SSRF |
| `params` | `sources` (via `is_trusted`) | Prevent parameter exfiltration |
| `body` | `sources` (via `is_trusted`) | Prevent body exfiltration |

#### Denial reasons (exact strings)

1. `"URL originates from untrusted source — possible SSRF or data exfiltration via URL"`
2. `"query parameters contain data from untrusted source — possible data exfiltration via URL parameters"`
3. `"request body contains data from untrusted source — possible data exfiltration via request body"`

#### Edge cases

- **URL constructed by string concatenation:**
  `base_url + "/" + email_derived_path` → `sources = {"User literal", "read_email"}` →
  `is_trusted` returns `False` → `Denied`.
- **URL is a constant in the P-LLM plan:** `wrap("https://api.example.com/...", sources=frozenset({"User literal"}))` →
  `is_trusted` returns `True` → `Allowed`.
- **Params dict built from mixed sources:** If any key or value in `params` originates
  from an untrusted source, `propagate_dict_construction` will carry those sources
  onto the dict's `CaMeLValue` → `is_trusted` returns `False` → `Denied`.
- **`method` field from untrusted source:** Not checked by this policy; `GET`/`POST`
  method manipulation is lower risk than URL/body manipulation. Deployments may add
  a stricter policy for `method` if needed.
- **Fetching a user-specified URL with no params:** Only check 1 applies. If the URL
  was typed by the user, `Allowed`.

---

## AgentDojo Attack-Scenario Mapping

Each policy below is mapped to the concrete AgentDojo task categories it mitigates,
along with the Goal (G2 = data-flow manipulation, G3 = exfiltration) it enforces.

| Policy | AgentDojo Task Category | Attack Type | Goal |
|--------|------------------------|-------------|------|
| `send_email` | Workspace — Email exfiltration | Injected recipient in email body redirects forwarded mail to attacker | G2, G3 |
| `send_email` | Workspace — Email content manipulation | Email body content tainted by document; sent to unauthorised recipient | G3 |
| `send_money` | Banking — Financial fraud | Injected recipient/amount from document causes unauthorised transfer | G2 |
| `send_money` | Banking — Amount manipulation | Amount derived from malicious email overrides user-intended amount | G2 |
| `create_calendar_event` | Workspace — Calendar injection | Attacker email address injected into meeting participants list | G2, G3 |
| `create_calendar_event` | Workspace — Meeting data exposure | Private meeting description shared with untrusted participants | G3 |
| `write_file` | Workspace — Cloud storage exfiltration | Restricted document content written to public file location | G3 |
| `write_file` | Workspace — Path injection | Malicious path from email overwrites sensitive system file | G2 |
| `post_message` | Slack — Message exfiltration | Private document content posted to public Slack channel | G3 |
| `post_message` | Slack — Channel injection | Injected channel name redirects message to attacker-controlled channel | G2 |
| `fetch_external_url` | All categories — URL exfiltration | Injected URL leaks private data as URL query parameters | G3 |
| `fetch_external_url` | All categories — SSRF | Untrusted URL causes internal network request | G2 |
| `fetch_external_url` | All categories — Parameter exfiltration | Query params built from private data sent to external host | G3 |

---

## Edge Case Catalogue

This section consolidates all cross-cutting edge cases that implementations must
handle correctly.

### Transitive / Multi-Hop Provenance

The capability propagation rules in `camel/value.py` ensure that derived values
inherit the union of all input sources. Policies relying on `is_trusted` therefore
automatically handle multi-hop scenarios:

| Scenario | Sources | `is_trusted` result |
|----------|---------|---------------------|
| User types recipient | `{"User literal"}` | `True` |
| Recipient from email | `{"read_email"}` | `False` |
| Recipient = user prefix + email suffix | `{"User literal", "read_email"}` | `False` |
| Amount = user base − document fee | `{"User literal", "read_document"}` | `False` |
| Amount = trusted × 2 | `{"User literal", "CaMeL"}` | `True` |

### `Public` vs. Restricted Readers

| Scenario | `can_readers_read_value(v, reader)` |
|----------|--------------------------------------|
| `v.readers = Public` | Always `True` |
| `v.readers = frozenset({"alice"})`, reader = `"alice"` | `True` |
| `v.readers = frozenset({"alice"})`, reader = `"eve"` | `False` |
| `v.readers = frozenset()` (empty) | Always `False` |

### Empty / None Arguments

All six policies guard against missing arguments by checking `kwargs.get(field)`
and returning `Allowed()` if `None`. Missing arguments are validated by the tool
implementation, not the policy.

### Raw Value Type Coercion

Policies that access `.raw` on a `CaMeLValue` (e.g. `send_email` extracting the
recipient string, `create_calendar_event` extracting the participants list) must
perform a runtime type check before iterating or indexing:

```python
participants_raw = participants_cv.raw
if not isinstance(participants_raw, list):
    participants_raw = [str(participants_raw)]
```

---

## Implementation Notes

### Module location

Reference policies must be implemented in:

```
camel/policy/reference_policies.py
```

The module must expose a `configure_reference_policies(registry: PolicyRegistry) -> None`
function so that deployers can register the full baseline set in one call:

```python
from camel.policy.reference_policies import configure_reference_policies
from camel.policy import PolicyRegistry

registry = PolicyRegistry()
configure_reference_policies(registry, file_owner="alice@example.com")
```

### Parameterised policies (`write_file`)

`write_file` requires the `owner` string at registration time. The
`configure_reference_policies` function must accept `file_owner: str` as a
keyword argument and use `make_write_file_policy(file_owner)` internally.

### Test matrix

The QA engineer must produce, for every policy:

| Case type | Count minimum |
|-----------|---------------|
| Positive (`Allowed`) | ≥ 3 per policy |
| Negative (`Denied`) per denial reason | ≥ 1 per reason string |
| Multi-hop / transitive provenance | ≥ 1 per policy that uses `is_trusted` |
| Edge case (empty args, Public readers, etc.) | ≥ 2 per policy |

### AgentDojo validation

Each policy must be mapped in `tests/test_reference_policies.py` to at least one
adversarial scenario (fixture) that mirrors a concrete AgentDojo attack category
(see mapping table above). These scenarios must demonstrate that the policy returns
`Denied` given a `kwargs` dict constructed to represent the attack and `Allowed`
given the equivalent benign `kwargs`.

### Configuration guidance

Deployments should load the reference policies via the environment variable
mechanism:

```python
# In myapp/security/policies.py
from camel.policy.reference_policies import configure_reference_policies

def configure_policies(registry):
    configure_reference_policies(registry, file_owner="alice@company.com")
```

```bash
export CAMEL_POLICY_MODULE=myapp.security.policies
```

To **exclude** a policy (e.g., no file writes in this deployment), simply do not
call `registry.register("write_file", ...)` in `configure_policies`.

To **extend** a policy, register additional `PolicyFn` callables for the same tool
name — all registered policies must return `Allowed` for the call to proceed.
