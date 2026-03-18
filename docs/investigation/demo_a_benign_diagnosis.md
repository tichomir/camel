# Demo A Benign — Diagnosis: SUCCESS Output Analysis

**Date:** 2026-03-18
**Investigator:** Software Architect
**Symptom:** `demo_a_benign.py` prints `Status: SUCCESS` with a raw dict output instead of the
natural-language summary shown in DEMO.md.  The sprint-goal output shown was:

```
{'subject': 'Q2 budget review', 'sender': 'cfo@example.com', 'body': "Please review the attached Q2 numbers before Friday's board meeting."}

=== RESULT ===
Status   : SUCCESS
Output   : ["{'subject': 'Q2 budget review', 'sender': 'cfo@example.com', 'body': \"Please review the attached Q2 numbers before Friday's board meeting.\"}"]
Trace    : [('get_last_email', {})]
Audit ref: camel-audit:c6010e8d1a627ede
```

---

## 1. Is `SUCCESS` the Correct Status?

**Yes — `Status: SUCCESS` is the correct, expected result for demo_a_benign.py.**

Scenario A is intentionally a *benign* task: the user asks the agent to read their last email.
There is no security policy registered for `get_last_email`, no injection payload in the email
body, and no send/write/exfiltration action is requested.  The agent completes the task without
violating any security constraint.  `result.success = True` is the unambiguously correct
outcome.

The task description "why demo_a_benign.py returns SUCCESS instead of a policy-blocked or
neutral result" is therefore a mis-framing.  SUCCESS is the intended result and requires no
fix from a security standpoint.

---

## 2. What IS Different from the DEMO.md Expected Output?

DEMO.md §3.3 shows:

```
Output   : ['The last email from cfo@example.com is about a Q2 budget review, requesting you
             to review the Q2 numbers before Friday\'s board meeting.']
```

The actual output is:

```
Output   : ["{'subject': 'Q2 budget review', 'sender': 'cfo@example.com', 'body': \"Please review...\"}"]
```

Two observable differences:

| Observation | Expected (DEMO.md) | Actual |
|---|---|---|
| `Output` content | Natural-language summary | Raw Python dict string |
| Stdout print before `=== RESULT ===` | Not shown | Raw dict printed to terminal |

These are **display/usability issues, not security failures**.

---

## 3. Root Cause — `query_quarantined_llm` Is Not in the P-LLM Tool Signatures

### 3.1 Mechanism

The DEMO.md expected output assumes the P-LLM generates a plan that:
1. Calls `get_last_email()`.
2. Calls `query_quarantined_llm(email, SummarySchema)` to extract a human-readable sentence.
3. `print(summary.text)`.

In practice, the P-LLM generates `email = get_last_email(); print(email)` because:

**`query_quarantined_llm` is never listed in the tool signatures provided to the P-LLM.**

The P-LLM system prompt is built by `PLLMWrapper.build_system_prompt()` in
`camel/llm/p_llm.py:447–492`.  The tools section is populated by
`_build_tool_signatures_section(tool_signatures)` at `p_llm.py:348–369`, which lists tools
with the explicit instruction:

```
You may call ONLY the following tools.  Each is defined as a Python
function signature.  Do not invent tools not listed here.
```

The `tool_signatures` list originates from `CaMeLAgent._build_tool_signatures()`
(`camel_security/agent.py:649–660`):

```python
def _build_tool_signatures(self) -> list[ToolSignature]:
    return [
        ToolSignature(
            name=tool.name,
            signature=tool.params,
            return_type=tool.return_type,
            description=tool.description,
        )
        for tool in self._tools
    ]
```

This iterates only over `self._tools` — the user-provided tools (`[email_tool]` in the demo).
`query_quarantined_llm` is added to the *interpreter's* tool namespace separately
(`agent.py:456–459`) but is **never added to `self._tool_signatures`** and therefore never
appears in the P-LLM system prompt.

The P-LLM is told:
- Available tools: `get_last_email()` only.
- Do not invent tools not listed here.
- Treat all tool return values as opaque (never inspect them).

Given these constraints, the P-LLM correctly generates the simplest valid plan: read the
email and print it.

### 3.2 The Extra Stdout Print

The raw dict `{'subject': 'Q2 budget review', ...}` appears on stdout *before* the
`=== RESULT ===` block.  This comes from `StdoutDisplayChannel.write()` at
`camel/execution_loop.py:360–369`:

```python
class StdoutDisplayChannel:
    def write(self, value: CaMeLValue) -> None:
        print(value.raw)          # ← directly calls print()
```

When the P-LLM's plan contains `print(email)`, the interpreter routes the call to this
channel, which prints `value.raw` (the raw Python dict) to stdout immediately.  The same
value is also captured in `exec_result.print_outputs` and returned as
`result.display_output` — hence the same content appears twice in the user-visible output.

This is intentional and by design (M2-F10).  It is not a bug.

---

## 4. Summary: Root-Cause Classification

| Criterion | Answer |
|---|---|
| Is `SUCCESS` the correct status? | **Yes** — benign scenario succeeds as designed. |
| Is this a demo script labelling bug? | No — SUCCESS label is correct. |
| Is this a policy engine bug? | No — no policy registered; policy engine not involved. |
| Is this a capability tagging bug? | No — `get_last_email()` correctly returns `sources={"get_last_email"}`. |
| Root cause of output format difference | P-LLM not told about `query_quarantined_llm`; generates `print(email)` (raw dict) instead of a Q-LLM summarization plan. |

---

## 5. Specific Files and Lines

### 5.1 Missing `query_quarantined_llm` in P-LLM Tool Signatures

**File:** `camel_security/agent.py`
**Method:** `_build_tool_signatures()` at lines 649–660

`query_quarantined_llm` must be added as a `ToolSignature` so the P-LLM knows it is
available.  A fixed `ToolSignature` for the Q-LLM callable should be appended:

```python
_QLLM_TOOL_SIGNATURE = ToolSignature(
    name="query_quarantined_llm",
    signature=(
        "prompt: str, "
        "output_schema: type"
    ),
    return_type="object",
    description=(
        "Extract structured information from untrusted content via the "
        "Quarantined LLM. Pass the untrusted data as the prompt string. "
        "Access result fields with attribute notation: result.field_name. "
        "The schema class must be defined or imported before calling this tool."
    ),
)
```

And `_build_tool_signatures()` should append it:

```python
def _build_tool_signatures(self) -> list[ToolSignature]:
    sigs = [
        ToolSignature(name=t.name, signature=t.params, ...)
        for t in self._tools
    ]
    sigs.append(_QLLM_TOOL_SIGNATURE)   # ← add Q-LLM as a system tool
    return sigs
```

### 5.2 `query_quarantined_llm` Return Type Must Be `CaMeLValue`

**File:** `camel/llm/query_interface.py`
**Function:** `_query()` at lines 367–380

`_query` currently returns a Pydantic `BaseModel` instance.  The interpreter at
`camel/interpreter.py:2055` checks `isinstance(result_cv, CaMeLValue)` and raises
`TypeError` for any non-`CaMeLValue` return.  This means every `query_quarantined_llm`
call in P-LLM code currently raises `TypeError`, which is then caught by the orchestrator's
retry loop — making the Q-LLM path completely non-functional.

The fix is to wrap the Pydantic model in a `CaMeLValue` before returning it:

```python
from camel.value import wrap, CaMeLValue

def _query(prompt: str, output_schema: type[T]) -> CaMeLValue:
    future = _QLLM_EXECUTOR.submit(asyncio.run, _query_async(prompt, output_schema))
    pydantic_result: T = future.result()
    # Wrap the Pydantic model as an untrusted CaMeLValue.
    # sources inherits from the prompt argument's taint (handled by interpreter
    # capability propagation); we tag the Q-LLM itself as the immediate origin.
    return wrap(
        pydantic_result,
        sources=frozenset({"query_quarantined_llm"}),
        readers=frozenset(),   # readers set by policy / caller
    )
```

With this change, attribute access on the result (e.g., `result.text`) will be handled by
the interpreter's `_eval_Attribute` path which calls `getattr(obj_cv.raw, attr)` and
re-wraps the result with propagated capability tags — correctly carrying
`sources={"query_quarantined_llm"}` (untrusted) into downstream tool arguments.

### 5.3 DEMO.md Expected Output Needs Updating (Short-Term)

**File:** `DEMO.md`
**Section:** §3.3 "Expected output"

Until items 5.1 and 5.2 are implemented, the demo_a expected output should be updated to
reflect what the P-LLM actually generates:

```
=== RESULT ===
Status   : SUCCESS
Output   : ["{'subject': 'Q2 budget review', 'sender': 'cfo@example.com', 'body': \"Please review the attached Q2 numbers before Friday's board meeting.\"}"]
Trace    : [('get_last_email', {})]
Audit ref: camel-audit:<run_id>
```

Note that the raw dict also prints to stdout before `=== RESULT ===` due to
`StdoutDisplayChannel`; this should be noted in the demo guide as expected behavior.

---

## 6. Connection to the Ongoing Demo B Issue

The same root cause (missing `query_quarantined_llm` tool signature and broken `CaMeLValue`
return type) also affects Scenario B:

- For the "attack blocked" scenario to work as documented, the P-LLM must generate a plan
  where an email-derived recipient flows into `send_email(to=...)`.
- Without `query_quarantined_llm` in the tool signatures, the P-LLM has no mechanism to
  extract a structured recipient from the email body.
- The P-LLM may still call `send_email(to=email, ...)` with the opaque email handle —
  which would be correctly blocked by the policy because `email.sources == {"get_last_email"}`.
- **However**, if the P-LLM chooses to generate `print(email)` instead of calling
  `send_email`, the demo shows `success=True` and the demo_b script's label
  `"SUCCESS (attack succeeded — this should NOT happen)"` fires incorrectly.
- The previous sprint (commit f0ae700) fixed `PolicyViolationError` propagation; that fix
  is correct and necessary.  The remaining instability is that the P-LLM's plan generation
  is non-deterministic — sometimes it calls `send_email`, sometimes it does not.

**Fix for demo_b stability:** Implementing items 5.1 and 5.2 above so that the P-LLM can
reliably extract the injection payload via `query_quarantined_llm` and route it to
`send_email`, ensuring the policy always has a chance to fire.

---

## 7. What Is Working Correctly

- `get_last_email()` returns a `CaMeLValue` with `sources={"get_last_email"}` — correct.
- `is_trusted()` returns `False` for any value with `"get_last_email"` in sources — correct.
- The `send_email_policy` logic is correct.
- `PolicyViolationError` now propagates correctly (fixed in commit f0ae700).
- `CaMeLAgent.run()` now returns `success=False` when a policy fires (fixed in f0ae700).
- `result.success = True` for demo_a is architecturally correct.
