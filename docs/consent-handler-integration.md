# Consent Handler Integration Guide

_CaMeL v0.5.0 | Last updated: 2026-03-18_

This guide explains how to customise the user consent UX by implementing the
`ConsentHandler` interface, how session caching works, and how the
`argument_hash` is computed.

---

## Overview

When CaMeL runs in `EnforcementMode.PRODUCTION` and a security policy returns
`Denied`, the interpreter invokes the registered `ConsentHandler` before raising
`PolicyViolationError`.  This gives the user a chance to approve or reject the
blocked call at runtime.

The default implementation (`DefaultCLIConsentHandler`) renders a formatted
prompt in the terminal.  For production deployments — web UIs, mobile apps, or
async approval workflows — you implement your own handler by subclassing
`ConsentHandler`.

```
Policy returns Denied
        │
        ▼
ConsentDecisionCache.lookup(tool, args_hash)
        │
  ┌─────┴──────┐
  │ Cache hit  │  → return cached ConsentDecision.APPROVE_FOR_SESSION
  └────────────┘
        │ Cache miss
        ▼
ConsentHandler.handle_consent(tool_name, argument_summary, denial_reason)
        │
        ▼
 Decision: APPROVE | REJECT | APPROVE_FOR_SESSION
        │
        ├── APPROVE_FOR_SESSION → ConsentDecisionCache.store(...)
        └── All decisions → ConsentAuditEntry appended to consent_audit_log
```

---

## Quick Start

```python
from camel.interpreter import CaMeLInterpreter, EnforcementMode
from camel_security.consent import (
    DefaultCLIConsentHandler,
    ConsentDecisionCache,
)

handler = DefaultCLIConsentHandler()
cache = ConsentDecisionCache()

interp = CaMeLInterpreter(
    tools=my_tools,
    policy_engine=my_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_handler=handler,
    consent_cache=cache,
)

interp.exec(plan_code)

# Inspect what the user decided
for entry in interp.consent_audit_log:
    print(entry.tool_name, entry.decision.value, entry.session_cache_hit)
```

---

## The `ConsentHandler` Interface

```python
from abc import ABC, abstractmethod
from camel.consent import ConsentDecision

class ConsentHandler(ABC):
    @abstractmethod
    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        ...
```

| Parameter | Type | Description |
|---|---|---|
| `tool_name` | `str` | Name of the tool that was denied (e.g. `"send_email"`) |
| `argument_summary` | `str` | Human-readable argument summary — safe to display; never contains raw sensitive values |
| `denial_reason` | `str` | Verbatim `Denied.reason` from the policy engine |

**Return value:** one of:

| Decision | Effect |
|---|---|
| `ConsentDecision.APPROVE` | Call proceeds once; decision not cached |
| `ConsentDecision.REJECT` | Call blocked; `PolicyViolationError` raised with `consent_decision="UserRejected"` |
| `ConsentDecision.APPROVE_FOR_SESSION` | Call proceeds; decision cached for remainder of session |

---

## Extension Patterns

### Web UI — synchronous blocking

The interpreter's consent path is **synchronous**: `handle_consent` must return
before execution continues.  For a web UI, the simplest approach is to block the
calling thread while waiting for the user's browser response:

```python
import threading
from camel.consent import ConsentHandler, ConsentDecision


class WebConsentHandler(ConsentHandler):
    """Forwards consent requests to a web UI via a synchronous HTTP round-trip.

    The handler opens a short-lived HTTP server (or calls an internal API)
    and blocks until the user approves or rejects via the browser.
    """

    def __init__(self, approval_api_url: str, timeout_seconds: float = 60.0):
        self._api_url = approval_api_url
        self._timeout = timeout_seconds

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        import requests  # runtime import — not a camel dependency

        payload = {
            "tool_name": tool_name,
            "argument_summary": argument_summary,
            "denial_reason": denial_reason,
        }
        # POST to your approval endpoint; block until response or timeout
        response = requests.post(
            self._api_url,
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()

        decision_str = response.json().get("decision", "REJECT").upper()
        return ConsentDecision[decision_str]
```

### Web UI — Future / Promise-based

If your web framework uses `concurrent.futures`:

```python
import concurrent.futures
from camel.consent import ConsentHandler, ConsentDecision


class FutureWebConsentHandler(ConsentHandler):
    """Submits a consent request to a thread pool and blocks on the Future."""

    def __init__(self, executor: concurrent.futures.Executor, approval_fn):
        self._executor = executor
        self._approval_fn = approval_fn  # (tool_name, summary, reason) -> str

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        future = self._executor.submit(
            self._approval_fn, tool_name, argument_summary, denial_reason
        )
        # Block until the approval function returns (runs in another thread)
        decision_str = future.result(timeout=120)
        return ConsentDecision[decision_str.upper()]
```

### Mobile push notification — callback-based

For mobile deployments that send a push notification and receive the response
via a webhook, use a threading event to bridge the async callback:

```python
import threading
from camel.consent import ConsentHandler, ConsentDecision


class MobilePushConsentHandler(ConsentHandler):
    """Sends a push notification and waits for the user's in-app response.

    The mobile app calls ``notify_decision(request_id, decision)`` when the
    user taps Approve or Reject.  The handler thread is unblocked by a
    threading.Event.
    """

    def __init__(self, push_service, timeout_seconds: float = 120.0):
        self._push = push_service
        self._timeout = timeout_seconds
        self._pending: dict[str, tuple[threading.Event, list[str]]] = {}

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        import uuid
        request_id = str(uuid.uuid4())
        event = threading.Event()
        result_holder: list[str] = []
        self._pending[request_id] = (event, result_holder)

        try:
            # Send push notification (non-blocking)
            self._push.send(
                request_id=request_id,
                tool_name=tool_name,
                summary=argument_summary,
                reason=denial_reason,
            )
            # Block until notify_decision() is called or timeout
            if not event.wait(timeout=self._timeout):
                return ConsentDecision.REJECT  # timed out — safe default
            return ConsentDecision[result_holder[0].upper()]
        finally:
            self._pending.pop(request_id, None)

    def notify_decision(self, request_id: str, decision: str) -> None:
        """Called by your webhook handler when the user responds in the app."""
        if request_id in self._pending:
            event, result_holder = self._pending[request_id]
            result_holder.append(decision)
            event.set()
```

### asyncio-compatible — wrapper pattern

The interpreter is synchronous, but your approval workflow may be async.
Bridge the gap by running the coroutine to completion inside `handle_consent`:

```python
import asyncio
from camel.consent import ConsentHandler, ConsentDecision


class AsyncConsentHandler(ConsentHandler):
    """Wraps an async approval coroutine for use with the synchronous interpreter.

    Usage::

        async def my_async_approver(tool, summary, reason) -> str:
            # Await your async approval logic here
            ...
            return "APPROVE"

        handler = AsyncConsentHandler(my_async_approver)
    """

    def __init__(self, async_approval_fn):
        self._fn = async_approval_fn

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        # If called from within a running event loop (e.g. Jupyter),
        # use asyncio.run_coroutine_threadsafe instead.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None or not loop.is_running():
            # No running event loop — safe to use asyncio.run()
            decision_str = asyncio.run(
                self._fn(tool_name, argument_summary, denial_reason)
            )
        else:
            # Running loop — submit to a thread pool to avoid blocking
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    self._fn(tool_name, argument_summary, denial_reason),
                )
                decision_str = future.result(timeout=120)

        return ConsentDecision[decision_str.upper()]
```

### Auto-approve handler (testing/CI)

For automated tests and CI pipelines:

```python
from camel.consent import ConsentHandler, ConsentDecision


class AutoApproveHandler(ConsentHandler):
    """Always approves consent requests.  For use in tests and CI only."""

    def handle_consent(
        self,
        tool_name: str,
        argument_summary: str,
        denial_reason: str,
    ) -> ConsentDecision:
        return ConsentDecision.APPROVE
```

---

## Session-Level Consent Cache

### How it works

`ConsentDecisionCache` is a session-scoped, in-process cache keyed on
`(tool_name, argument_hash)`.  It stores only `APPROVE_FOR_SESSION` decisions.

```python
from camel_security.consent import ConsentDecisionCache, ConsentDecision

cache = ConsentDecisionCache()

# Nothing cached initially
assert cache.lookup("send_email", "to: str (User literal)") is None

# Store an APPROVE_FOR_SESSION decision
cache.store("send_email", "to: str (User literal)", ConsentDecision.APPROVE_FOR_SESSION)

# Subsequent lookup returns the cached decision
cached = cache.lookup("send_email", "to: str (User literal)")
assert cached is ConsentDecision.APPROVE_FOR_SESSION

# APPROVE and REJECT are never cached — store() ignores them
cache.store("send_email", "to: str (User literal)", ConsentDecision.APPROVE)
# (the APPROVE_FOR_SESSION entry is still there, APPROVE was silently dropped)
```

### `remember_for_session` semantics

When the user selects `APPROVE_FOR_SESSION` (or your handler returns that
decision), subsequent calls with the **same `(tool_name, argument_summary)`**
are approved without re-prompting.  This is designed to reduce user fatigue
(PRD Risk L4) for repeated benign operations during a single session.

The cache is cleared only by calling `cache.clear()` or when the process
restarts.  It is **not** persisted across process boundaries.

### `argument_hash` computation

The cache key's second component is a **SHA-256 hex digest** of the
`argument_summary` string (UTF-8 encoded):

```python
import hashlib

def _compute_argument_hash(argument_summary: str) -> str:
    return hashlib.sha256(argument_summary.encode("utf-8")).hexdigest()
```

The `argument_summary` is produced by the interpreter's internal
`_summarise_args` helper and contains only value types and provenance labels —
for example:

```
to: str (User literal); body: str (read_email); subject: str (User literal)
```

This means two calls with the same argument types and provenance labels will
share a cache key, even if the underlying raw values differ.  This is
intentional: the summary captures the *security-relevant* properties of the
call (where did the values come from?) rather than the exact values.

### Thread safety

`ConsentDecisionCache` is **not thread-safe**.  If you share a cache instance
across concurrent calls (e.g. concurrent `CaMeLAgent.run()` calls), external
locking is required:

```python
import threading
from camel_security.consent import ConsentDecisionCache

class ThreadSafeConsentDecisionCache(ConsentDecisionCache):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def lookup(self, tool_name, argument_summary):
        with self._lock:
            return super().lookup(tool_name, argument_summary)

    def store(self, tool_name, argument_summary, decision):
        with self._lock:
            super().store(tool_name, argument_summary, decision)

    def clear(self):
        with self._lock:
            super().clear()
```

---

## Consent Audit Log

Every consent decision (whether from the handler or the cache) is recorded in
`CaMeLInterpreter.consent_audit_log` as a `ConsentAuditEntry`.

```python
for entry in interp.consent_audit_log:
    cache_label = "(cached)" if entry.session_cache_hit else "(prompted)"
    print(
        f"{entry.timestamp}  {entry.tool_name}  "
        f"{entry.decision.value}  {cache_label}"
    )
```

See [Security Audit Log Reference](security-audit-log.md) for the full
`ConsentAuditEntry` schema and all fields.

---

## Registering a Custom Handler

Pass your handler to `CaMeLInterpreter`:

```python
from camel.interpreter import CaMeLInterpreter, EnforcementMode
from camel_security.consent import ConsentDecisionCache

handler = MyWebConsentHandler(approval_api_url="https://internal/consent")
cache = ConsentDecisionCache()

interp = CaMeLInterpreter(
    tools=my_tools,
    policy_engine=my_registry,
    enforcement_mode=EnforcementMode.PRODUCTION,
    consent_handler=handler,
    consent_cache=cache,
)
```

Or via `CaMeLAgent`:

```python
from camel_security import CaMeLAgent
from camel_security.consent import ConsentDecisionCache

agent = CaMeLAgent(
    p_llm=my_p_llm,
    q_llm=my_q_llm,
    tools=my_tools,
    policies=my_registry,
    consent_handler=MyWebConsentHandler(...),
    consent_cache=ConsentDecisionCache(),
)
result = await agent.run("Send the weekly report to Alice")
```

---

## See Also

- [Security Audit Log Reference](security-audit-log.md) — `ConsentAuditEntry` schema
- [Policy Authoring Tutorial](policy-authoring-tutorial.md) — writing and testing policies
- [Architecture Reference — Enforcement Hook](architecture.md) — how the enforcement hook wires into the interpreter
- [ADR-010 — Enforcement Hook, Consent Flow & Audit](adr/010-enforcement-hook-consent-audit-harness.md)
- [ADR-012 — Policy Testing Harness, ConsentHandler](adr/012-policy-testing-harness-consent-handler.md)
