# CaMeL Tool Onboarding Guide

_CaMeL v0.6.0 (camel-security) — Registering a new tool with capability annotations and policies_

This guide walks you through the complete lifecycle of adding a new tool to a CaMeL
deployment: typing the function signature, returning `CaMeLValue`, providing a
capability annotation, and optionally attaching a per-tool security policy.

---

## Overview

Adding a tool to CaMeL requires three things:

| Step | Required | Description |
|---|---|---|
| 1. **Function** | Yes | A Python callable that returns a `CaMeLValue` |
| 2. **Registration** | Yes | A `Tool` dataclass wiring the function to a name, description, and signature |
| 3. **Capability annotation** | Optional (has default) | A function that sets `sources`, `inner_source`, and `readers` on the return value |
| 4. **Security policy** | Optional (all-allow default) | A policy function evaluated before the tool is called |

If you skip the capability annotation, CaMeL applies the **default**: `sources={tool_name}`,
`readers=Public`, no `inner_source`.  If you skip the security policy, the tool is
always allowed.

---

## 1. Write a Tool Function

Every tool must return a `CaMeLValue`.  Use `wrap()` for the common case:

```python
from camel.value import wrap, Public, CaMeLValue


def get_weather(city: str) -> CaMeLValue:
    """Fetch the current weather for a city."""
    # Real implementation calls an external weather API
    temperature = 22  # °C
    conditions = "Partly cloudy"
    return wrap(
        value={"temperature": temperature, "conditions": conditions},
        sources=frozenset({"get_weather"}),
        readers=Public,
    )
```

**Rules for tool functions:**

- The return value must be a `CaMeLValue` instance.
- Arguments are passed as raw Python values (the interpreter unwraps `CaMeLValue`
  arguments before calling your function).
- Functions may be synchronous or `async`.

---

## 2. Register the Tool

Use the `Tool` dataclass from `camel_security`:

```python
from camel_security import Tool

weather_tool = Tool(
    name="get_weather",
    fn=get_weather,
    description=(
        "Returns the current temperature (°C) and weather conditions for the "
        "given city name."
    ),
    params="city: str",
    return_type="dict",
)
```

### `Tool` field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Unique tool identifier used in P-LLM plans |
| `fn` | `Callable` | Yes | The Python callable to invoke |
| `description` | `str` | Yes | Human-readable description shown to the P-LLM |
| `params` | `str` | Yes | Parameter signature string (e.g. `"city: str"`, `"a: int, b: int"`) |
| `return_type` | `str` | Yes | Return type hint string used in P-LLM system prompt |
| `capability_annotation` | `Callable \| None` | No | Custom capability annotator (see §3) |
| `policies` | `list[Callable] \| None` | No | Per-tool security policies (see §4) |

Pass the tool to `CaMeLAgent`:

```python
from camel_security import CaMeLAgent
from camel.llm.adapters import ClaudeBackend
import asyncio

backend = ClaudeBackend(model="claude-sonnet-4-6", api_key="YOUR_KEY")

agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[weather_tool],
)

result = asyncio.run(agent.run("What's the weather in London?"))
print(result.display_output)
```

---

## 3. Write a Capability Annotation (recommended)

The default annotation (`sources={tool_name}`, `readers=Public`) is a safe starting
point, but providing a custom annotation lets you:

- Tag sub-fields with `inner_source` (e.g., mark the `sender` field of an email
  as `inner_source="sender"` so policies can reason about it separately).
- Restrict `readers` to a specific set of principals (e.g., for documents shared
  with a known audience).
- Use metadata returned by the tool itself (e.g., document sharing permissions
  from a cloud storage response) to populate `readers`.

### Annotation function contract

```python
from camel.value import CaMeLValue
from collections.abc import Mapping
from typing import Any


def annotate_get_weather(
    return_value: Any,
    tool_kwargs: Mapping[str, Any],
) -> CaMeLValue:
    """Custom capability annotation for get_weather.

    Args:
        return_value: The raw value returned by the tool function.
        tool_kwargs: The raw keyword arguments the tool was called with.

    Returns:
        A CaMeLValue wrapping return_value with provenance metadata.
    """
    from camel.value import wrap, Public

    # Weather data is public — no readers restriction needed.
    # Tag with the tool name and the city as inner_source.
    return wrap(
        value=return_value,
        sources=frozenset({"get_weather"}),
        inner_source=f"weather_for:{tool_kwargs.get('city', 'unknown')}",
        readers=Public,
    )
```

Register it via the `capability_annotation` field on `Tool`:

```python
weather_tool = Tool(
    name="get_weather",
    fn=get_weather,
    description="Returns current temperature and conditions for a given city.",
    params="city: str",
    return_type="dict",
    capability_annotation=annotate_get_weather,
)
```

### Worked example: email tool with restricted readers

```python
from camel.value import wrap, Public, CaMeLValue
from collections.abc import Mapping
from typing import Any


def read_email(email_id: str) -> CaMeLValue:
    """Fetch an email by ID."""
    # Real implementation calls email API
    email = {
        "from": "alice@example.com",
        "to": ["bob@example.com"],
        "subject": "Project update",
        "body": "Here is the project status...",
    }
    return wrap(
        value=email,
        sources=frozenset({"read_email"}),
        readers=frozenset({"bob@example.com", "alice@example.com"}),
    )


def annotate_read_email(
    return_value: Any,
    tool_kwargs: Mapping[str, Any],
) -> CaMeLValue:
    """Annotate read_email output with sender as inner_source."""
    from camel.value import wrap

    raw = return_value if isinstance(return_value, dict) else {}
    sender = raw.get("from", "unknown")
    recipients = set(raw.get("to", []))
    recipients.add(sender)

    return wrap(
        value=return_value,
        sources=frozenset({"read_email"}),
        inner_source=sender,            # the sender field drives data-flow policy
        readers=frozenset(recipients),  # only email participants may receive this data
    )


email_tool = Tool(
    name="read_email",
    fn=read_email,
    description="Fetches an email by its ID.",
    params="email_id: str",
    return_type="dict",
    capability_annotation=annotate_read_email,
)
```

---

## 4. Attach a Per-Tool Security Policy (optional)

Per-tool policies are registered directly on the `Tool` object and are evaluated
as **Tool-Provider tier** policies in the three-tier governance model (see
[Policy Authoring Guide](policy-authoring-guide.md)).

A policy function has the signature:

```python
(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult
```

### Example: restrict `get_weather` to known cities

```python
from collections.abc import Mapping
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult, is_trusted
from camel.value import CaMeLValue

ALLOWED_CITIES = frozenset({"London", "Paris", "Berlin", "Tokyo", "New York"})


def weather_city_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Only fetch weather for cities in the approved list."""
    city = kwargs.get("city")
    if city is None:
        return Denied("Missing 'city' argument")

    # The city must be user-supplied (trusted) and in the allowed list
    if not is_trusted(city):
        return Denied(
            "City name must come from a trusted source (user input), "
            "not from an untrusted tool output"
        )

    if city.raw not in ALLOWED_CITIES:
        return Denied(
            f"City '{city.raw}' is not in the approved city list"
        )

    return Allowed()
```

Register it on the tool:

```python
weather_tool = Tool(
    name="get_weather",
    fn=get_weather,
    description="Returns current temperature and conditions for a given city.",
    params="city: str",
    return_type="dict",
    capability_annotation=annotate_get_weather,
    policies=[weather_city_policy],
)
```

Multiple policies may be registered; all must return `Allowed` for the tool
call to proceed.

---

## 5. End-to-End Worked Example

The following is a complete, self-contained example for a `fetch_stock_price`
tool with a custom capability annotation and a security policy.

```python
import asyncio
from collections.abc import Mapping
from typing import Any

from camel.value import wrap, Public, CaMeLValue
from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult, is_trusted
from camel_security import CaMeLAgent, Tool
from camel.llm.adapters import ClaudeBackend

# ── Tool function ──────────────────────────────────────────────────────────────

def fetch_stock_price(ticker: str) -> CaMeLValue:
    """Return the latest stock price for the given ticker symbol."""
    # Real implementation would call a market data API
    price = 150.75  # USD
    return wrap(
        value={"ticker": ticker, "price": price, "currency": "USD"},
        sources=frozenset({"fetch_stock_price"}),
        readers=Public,
    )


# ── Capability annotation ───────────────────────────────────────────────────────

def annotate_stock_price(
    return_value: Any,
    tool_kwargs: Mapping[str, Any],
) -> CaMeLValue:
    """Tag stock price data with the ticker as inner_source."""
    ticker = tool_kwargs.get("ticker", "unknown")
    return wrap(
        value=return_value,
        sources=frozenset({"fetch_stock_price"}),
        inner_source=f"ticker:{ticker}",
        readers=Public,
    )


# ── Per-tool security policy ────────────────────────────────────────────────────

APPROVED_TICKERS = frozenset({"AAPL", "GOOG", "MSFT", "AMZN"})


def stock_ticker_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Ticker must be user-supplied and in the approved list."""
    ticker = kwargs.get("ticker")
    if ticker is None:
        return Denied("Missing 'ticker' argument")
    if not is_trusted(ticker):
        return Denied("Ticker symbol must originate from a trusted source")
    if ticker.raw not in APPROVED_TICKERS:
        return Denied(f"Ticker '{ticker.raw}' is not in the approved list")
    return Allowed()


# ── Tool registration ───────────────────────────────────────────────────────────

stock_tool = Tool(
    name="fetch_stock_price",
    fn=fetch_stock_price,
    description=(
        "Returns the latest stock price (USD) for a ticker symbol. "
        "Supported tickers: AAPL, GOOG, MSFT, AMZN."
    ),
    params="ticker: str",
    return_type="dict",
    capability_annotation=annotate_stock_price,
    policies=[stock_ticker_policy],
)


# ── Agent construction and run ─────────────────────────────────────────────────

backend = ClaudeBackend(model="claude-sonnet-4-6", api_key="YOUR_KEY")

agent = CaMeLAgent(
    p_llm=backend,
    q_llm=backend,
    tools=[stock_tool],
)

result = asyncio.run(agent.run("What is the current price of Apple stock?"))
print(result.display_output)
for record in result.execution_trace:
    print(f"  Tool: {record.tool_name} → args: {record.args}")
```

---

## 6. Testing Your Tool

Use `CaMeLValueBuilder` and `PolicyTestRunner` to unit-test your policy without
a live interpreter:

```python
from camel_security.testing import CaMeLValueBuilder, PolicyTestCase, PolicyTestRunner

runner = PolicyTestRunner()

report = runner.run(
    stock_ticker_policy,
    [
        # ── Allowed: trusted ticker in approved list ───────────────────────────
        PolicyTestCase(
            case_id="approved_ticker",
            tool_name="fetch_stock_price",
            kwargs={
                "ticker": (
                    CaMeLValueBuilder("AAPL")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Allowed",
        ),
        # ── Denied: untrusted ticker (injected from tool output) ───────────────
        PolicyTestCase(
            case_id="injected_ticker",
            tool_name="fetch_stock_price",
            kwargs={
                "ticker": (
                    CaMeLValueBuilder("EVIL")
                    .with_sources("read_email")   # untrusted
                    .with_inner_source("body")
                    .build()
                ),
            },
            expected_outcome="Denied",
            expected_reason_contains="trusted source",
        ),
        # ── Denied: trusted but not in approved list ───────────────────────────
        PolicyTestCase(
            case_id="unapproved_ticker",
            tool_name="fetch_stock_price",
            kwargs={
                "ticker": (
                    CaMeLValueBuilder("TSLA")
                    .with_sources("User literal")
                    .build()
                ),
            },
            expected_outcome="Denied",
            expected_reason_contains="approved list",
        ),
    ],
)

assert report.passed == 3, report.results
print(f"All {report.total_cases} policy tests passed.")
```

---

## 7. Checklist

Before deploying a new tool, verify:

- [ ] Tool function returns a `CaMeLValue` in all code paths
- [ ] `Tool` fields `name`, `description`, `params`, `return_type` are complete
- [ ] Capability annotation correctly sets `readers` (not all tools should be `Public`)
- [ ] Per-tool policy covers at least one `Denied` path (for tools that accept
      parameters from the plan)
- [ ] Policy unit tests pass with `PolicyTestRunner` (both `Allowed` and `Denied` cases)
- [ ] Tool has been smoke-tested end-to-end with `agent.run()` on a representative query

---

## Next Steps

| Guide | What you'll learn |
|---|---|
| [Policy Authoring Guide](policy-authoring-guide.md) | Three-tier governance model, Platform / Tool-Provider / User tiers, `PolicyConflictResolver` |
| [Developer Quickstart](quickstart.md) | Full quickstart: SDK install, backend configuration, first run |
| [Architecture Reference](architecture.md) | How capability propagation works internally |
| [Reference Policy Specification](policies/reference-policy-spec.md) | Specification for the six built-in reference policies |
