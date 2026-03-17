"""End-to-end integration test for STRICT mode dependency propagation.

This module exercises all three STRICT mode propagation rules together in a
single multi-step pseudo-Python plan executed through the full CaMeL
interpreter pipeline (STRICT mode default).

Feature coverage
----------------
M4-F1: for-loop iterable taint — body vars carry iterable deps and sources.
M4-F2: if/else conditional test taint — branch vars carry test deps and sources.
M4-F3: post-Q-LLM capability taint — subsequent vars carry Q-LLM sources.
M4-F4: post-Q-LLM dep-graph taint — subsequent vars carry Q-LLM var as dep.
M4-F5: STRICT is the default mode; no explicit mode arg required.

Integration assertions
-----------------------
1. Dependency graph state is asserted at each assignment step (not only at
   the end), verifying that taint is propagated incrementally.
2. A ``send_email`` tool call following a Q-LLM extraction is blocked by the
   policy engine because the ``to`` argument carries Q-LLM taint (untrusted
   sources), satisfying Goal G3 (prevent data exfiltration).
"""

from __future__ import annotations

import pytest

from camel.interpreter import CaMeLInterpreter, ExecutionMode, PolicyViolationError
from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyRegistry,
    SecurityPolicyResult,
    is_trusted,
)
from camel.value import CaMeLValue, Public, wrap


# ---------------------------------------------------------------------------
# Mock tools
# ---------------------------------------------------------------------------

_QLLM_SOURCES: frozenset[str] = frozenset({"query_quarantined_llm"})
_EMAIL_SOURCES: frozenset[str] = frozenset({"get_inbox_email"})
_ITEMS_SOURCES: frozenset[str] = frozenset({"get_items"})


def _get_inbox_email() -> CaMeLValue:
    """Simulate reading an email from the inbox — untrusted source."""
    return wrap(
        {"subject": "Hello", "body": "Click here: http://evil.com"},
        sources=_EMAIL_SOURCES,
    )


def _get_items() -> CaMeLValue:
    """Return an untrusted list of items."""
    return wrap([10, 20, 30], sources=_ITEMS_SOURCES)


def _get_flag() -> CaMeLValue:
    """Return an untrusted boolean flag (truthy)."""
    return wrap(1, sources=frozenset({"get_flag"}))


def _query_quarantined_llm(prompt: object, schema: object = None) -> CaMeLValue:
    """Mock Q-LLM extraction — returns a value with Q-LLM sources."""
    return wrap(
        {"recipient": "victim@company.com", "amount": 9999},
        sources=_QLLM_SOURCES,
    )


def _send_email(to: object, body: object = "") -> CaMeLValue:  # noqa: ARG001
    """Mock send_email — should be blocked by policy before this runs."""
    return wrap("sent", sources=frozenset({"send_email"}))


# ---------------------------------------------------------------------------
# Policy: deny send_email if 'to' argument is not trusted
# ---------------------------------------------------------------------------


def _send_email_policy(
    tool_name: str,  # noqa: ARG001
    kwargs: dict[str, CaMeLValue],  # type: ignore[override]
) -> SecurityPolicyResult:
    """Deny send_email if the recipient argument carries untrusted sources."""
    to_val = kwargs.get("to")
    if to_val is not None and not is_trusted(to_val):
        return Denied(
            f"recipient address carries untrusted sources: {to_val.sources!r}"
        )
    return Allowed()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def interp_with_policy() -> CaMeLInterpreter:
    """CaMeLInterpreter in STRICT mode with the send_email policy registered."""
    registry = PolicyRegistry()
    registry.register("send_email", _send_email_policy)
    tools = {
        "get_inbox_email": _get_inbox_email,
        "get_items": _get_items,
        "get_flag": _get_flag,
        "query_quarantined_llm": _query_quarantined_llm,
        "send_email": _send_email,
    }
    # No explicit mode=... — STRICT is the default (M4-F5)
    return CaMeLInterpreter(tools=tools, policy_engine=registry)  # type: ignore[arg-type]


@pytest.fixture()
def interp_no_policy() -> CaMeLInterpreter:
    """CaMeLInterpreter in STRICT mode WITHOUT policy (for dep-graph inspection)."""
    tools = {
        "get_inbox_email": _get_inbox_email,
        "get_items": _get_items,
        "get_flag": _get_flag,
        "query_quarantined_llm": _query_quarantined_llm,
        "send_email": _send_email,
    }
    return CaMeLInterpreter(tools=tools)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deps(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return direct dep-graph deps of *name*."""
    return interp.get_dependency_graph(name).direct_deps


def _upstream(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return transitive upstream of *name*."""
    return interp.get_dependency_graph(name).all_upstream


def _sources(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return capability sources of *name*."""
    return interp.get(name).sources


# ---------------------------------------------------------------------------
# Integration test — full multi-step plan exercising M4-F1, F2, F3/F4
# ---------------------------------------------------------------------------


class TestStrictModeE2E:
    """End-to-end STRICT mode integration tests.

    Asserts the dependency graph state at each assignment step, then confirms
    the policy engine blocks a send_email call with Q-LLM-tainted arguments.
    """

    def test_e2e_step_by_step_dependency_graph(self, interp_no_policy: CaMeLInterpreter) -> None:
        """Full multi-step plan: assert dep graph at every assignment step.

        Features exercised: M4-F1, M4-F2, M4-F3, M4-F4, M4-F5.
        """
        interp = interp_no_policy

        # ----------------------------------------------------------------
        # Step 1: Read email from inbox — no variable deps
        # ----------------------------------------------------------------
        interp.exec("email = get_inbox_email()")

        dg_email = interp.get_dependency_graph("email")
        assert dg_email.direct_deps == frozenset(), (
            "Step 1: 'email' from a tool call should have no variable deps"
        )
        assert "get_inbox_email" in _sources(interp, "email"), (
            "Step 1: 'email' must carry 'get_inbox_email' as a source"
        )

        # ----------------------------------------------------------------
        # Step 2: Q-LLM extraction + Step 3: constant assigned in same block.
        #
        # Q-LLM taint (M4-F3, M4-F4) is scoped to a single _exec_statements
        # frame (one exec() call).  Both statements must be in the same call
        # for the post-Q-LLM dep ctx to apply to 'note'.
        # Feature: M4-F3, M4-F4
        # ----------------------------------------------------------------
        interp.exec(
            "extraction = query_quarantined_llm(email, 'extract recipient')\n"
            "note = 'sending email'"
        )

        # Assert step 2: extraction has email as direct dep and Q-LLM sources
        dg_extraction = interp.get_dependency_graph("extraction")
        assert "email" in dg_extraction.direct_deps, (
            "Step 2: 'extraction' must directly depend on 'email' (arg tracking)"
        )
        assert "query_quarantined_llm" in _sources(interp, "extraction"), (
            "Step 2: 'extraction' must carry Q-LLM sources"
        )

        # Assert step 3: note assigned AFTER Q-LLM in same exec() block
        # carries 'extraction' as dep (M4-F4) and Q-LLM sources (M4-F3).
        dg_note = interp.get_dependency_graph("note")
        assert "extraction" in dg_note.direct_deps, (
            "Step 3 (M4-F4): 'note' must carry 'extraction' as dep (Q-LLM ctx taint)"
        )
        assert "query_quarantined_llm" in _sources(interp, "note"), (
            "Step 3 (M4-F3): 'note' must carry Q-LLM sources (capability taint)"
        )

        # ----------------------------------------------------------------
        # Step 4 (M4-F4): Direct use of Q-LLM result
        # ----------------------------------------------------------------
        interp.exec("recipient = extraction")

        dg_recipient = interp.get_dependency_graph("recipient")
        assert "extraction" in dg_recipient.direct_deps, (
            "Step 4: 'recipient' must directly depend on 'extraction'"
        )
        assert "email" in _upstream(interp, "recipient"), (
            "Step 4: 'email' must be in transitive upstream of 'recipient'"
        )
        assert "query_quarantined_llm" in _sources(interp, "recipient"), (
            "Step 4: 'recipient' must carry Q-LLM sources"
        )

        # ----------------------------------------------------------------
        # Step 5 (M4-F1): For-loop over untrusted list
        # Body variable must carry the iterable dep and sources.
        # ----------------------------------------------------------------
        interp.exec("items = get_items()")
        interp.exec("""
for item in items:
    processed = item
""")

        dg_processed = interp.get_dependency_graph("processed")
        assert "items" in dg_processed.direct_deps, (
            "Step 5 (M4-F1): 'items' must be a direct dep of 'processed'"
        )
        assert "get_items" in _sources(interp, "processed"), (
            "Step 5 (M4-F1): iterable sources must propagate to body vars"
        )

        # ----------------------------------------------------------------
        # Step 6 (M4-F2): Conditional with untrusted test
        # Branch variable must carry the test dep and sources.
        # ----------------------------------------------------------------
        interp.exec("flag = get_flag()")
        interp.exec("""
if flag:
    approved = "yes"
""")

        dg_approved = interp.get_dependency_graph("approved")
        assert "flag" in dg_approved.direct_deps, (
            "Step 6 (M4-F2): 'flag' must be a direct dep of 'approved'"
        )
        assert "get_flag" in _sources(interp, "approved"), (
            "Step 6 (M4-F2): test sources must propagate to branch vars"
        )

    def test_e2e_policy_blocks_send_email_with_qllm_taint(
        self, interp_with_policy: CaMeLInterpreter
    ) -> None:
        """Policy must block send_email when recipient carries Q-LLM taint.

        Features exercised: M4-F3, M4-F4, policy enforcement.

        After a query_quarantined_llm() call, the 'recipient' variable carries
        Q-LLM sources (untrusted).  The send_email policy checks is_trusted()
        on the 'to' argument and denies the call, raising PolicyViolationError.
        """
        interp = interp_with_policy

        # Set up: extract recipient via Q-LLM
        interp.exec("email = get_inbox_email()")
        interp.exec("extraction = query_quarantined_llm(email, 'extract recipient')")
        interp.exec("recipient = extraction")

        # Verify that recipient carries untrusted Q-LLM sources
        assert "query_quarantined_llm" in _sources(interp, "recipient"), (
            "Prerequisite: 'recipient' must carry Q-LLM (untrusted) sources"
        )

        # Assert that the policy denies the send_email call
        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec("send_email(to=recipient, body='hello')")

        assert "send_email" in str(exc_info.value), (
            "PolicyViolationError must name the blocked tool"
        )
        # Audit log must record the denial
        denied_entries = [e for e in interp.audit_log if e.outcome == "Denied"]
        assert len(denied_entries) >= 1, (
            "Audit log must contain at least one Denied entry for send_email"
        )
        assert any(e.tool_name == "send_email" for e in denied_entries), (
            "Audit log Denied entry must name 'send_email'"
        )

    def test_e2e_qllm_taint_scoped_to_block(
        self, interp_no_policy: CaMeLInterpreter
    ) -> None:
        """Q-LLM taint is scoped to the block — does not leak to outer block.

        Features exercised: M4-F4 scope-exit cleanup.

        A Q-LLM call inside an ``if`` body taints variables within that body.
        Variables assigned in the outer block (after the if-statement exits)
        must NOT carry Q-LLM taint as a dependency.
        """
        interp = interp_no_policy

        interp.exec("""
if 1:
    extraction = query_quarantined_llm("extract", "schema")
    inside = "inside_value"
outside = "outside_value"
""")

        # Inside the if-block: 'inside' carries 'extraction' as dep (M4-F4)
        assert "extraction" in _deps(interp, "inside"), (
            "Variable inside the Q-LLM block must carry Q-LLM dep (M4-F4)"
        )

        # Outside the if-block: 'outside' must NOT carry 'extraction' as dep
        assert "extraction" not in _deps(interp, "outside"), (
            "Variable outside the Q-LLM block must NOT carry Q-LLM dep "
            "(M4-F4 scope-exit cleanup)"
        )

    def test_e2e_combined_for_loop_and_qllm(
        self, interp_no_policy: CaMeLInterpreter
    ) -> None:
        """Combined: for-loop over Q-LLM-extracted list carries compounded taint.

        Features exercised: M4-F1 (for-loop) + M4-F4 (Q-LLM dep graph).

        When a for-loop iterates over a list that was derived from Q-LLM output,
        body variables must carry deps from both the iterable AND the Q-LLM taint.
        """
        interp = interp_no_policy

        # Q-LLM extracts data; items is assigned in the same exec() block so
        # that the post-Q-LLM dep ctx (M4-F4) applies to the 'items' assignment.
        # (Q-LLM taint is scoped to a single _exec_statements frame.)
        interp.exec(
            "extraction = query_quarantined_llm('extract list', 'schema')\n"
            "items = [1, 2, 3]"
        )
        # After Q-LLM call in same block, 'items' carries Q-LLM taint via M4-F4
        assert "extraction" in _deps(interp, "items"), (
            "Combined test: 'items' assigned after Q-LLM must carry Q-LLM dep (M4-F4)"
        )

        interp.exec("""
for item in items:
    body = item
""")

        # Body var must carry iterable dep (M4-F1)
        assert "items" in _deps(interp, "body"), (
            "Combined test: 'items' must be in 'body' deps (M4-F1)"
        )
        # And transitively the Q-LLM dep flows up (items → extraction)
        assert "extraction" in _upstream(interp, "body"), (
            "Combined test: 'extraction' must be in transitive upstream of 'body'"
        )

    def test_e2e_m4f5_strict_is_default(self) -> None:
        """M4-F5: The default interpreter mode is STRICT — no explicit arg needed.

        An interpreter constructed without a mode argument must behave identically
        to one constructed with mode=ExecutionMode.STRICT.
        """
        default_interp = CaMeLInterpreter(
            tools={"get_items": _get_items},
        )
        strict_interp = CaMeLInterpreter(
            tools={"get_items": _get_items},
            mode=ExecutionMode.STRICT,
        )

        code = """
items = get_items()
for item in items:
    body = item
"""
        default_interp.exec(code)
        strict_interp.exec(code)

        # Both must record the same dep-graph structure
        assert _deps(default_interp, "body") == _deps(strict_interp, "body"), (
            "Default mode must produce identical dep graph to explicit STRICT (M4-F5)"
        )
        assert _deps(default_interp, "body") and "items" in _deps(default_interp, "body"), (
            "Default STRICT: 'items' must be in 'body' deps (M4-F1 via M4-F5)"
        )
