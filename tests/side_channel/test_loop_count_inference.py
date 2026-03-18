"""Side-channel test class 1: Indirect inference via loop count.

Attack vector
-------------
An observer who can see which tool calls are made, and with what arguments,
may be able to infer the *value* of a private variable by counting how many
loop iterations are executed.  For example, if a secret integer ``n`` controls
the number of times a tool is called inside a for-loop, the observer learns
``n`` simply by counting the calls — no exception required.

CaMeL mitigation (M4-F1)
------------------------
In STRICT mode the iterable's dependency set and capability sources are merged
into *every* variable assigned inside the loop body (including the loop-target
variable itself).  This means any subsequent tool call that uses a loop-body
variable will carry the iterable's untrusted provenance and will be flagged by
the policy engine before execution — preventing the observer from seeing
*how many times* the tool was called with different argument values derived
from the private iterable.

PRD Section 11 target: 100% pass rate for implemented mitigations.

Test structure
--------------
Each test documents its scenario, the expected STRICT mode behaviour, and
the corresponding NORMAL mode negative (showing that without STRICT the
mitigation is absent).
"""

from __future__ import annotations

from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.value import CaMeLValue, wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNTRUSTED = frozenset({"external_service"})
_TRUSTED = frozenset({"User literal"})


def _interp(mode: ExecutionMode = ExecutionMode.STRICT, **tools: object) -> CaMeLInterpreter:
    """Build a CaMeLInterpreter with a standard set of mock tools."""

    def _get_private_list() -> CaMeLValue:
        """Return an untrusted list whose length encodes a private value."""
        return wrap([1, 2, 3, 4, 5], sources=_UNTRUSTED)

    def _get_single_untrusted() -> CaMeLValue:
        """Return a single untrusted value."""
        return wrap(42, sources=_UNTRUSTED)

    def _get_nested_list() -> CaMeLValue:
        """Return an untrusted nested list."""
        return wrap([[1, 2], [3, 4], [5, 6]], sources=_UNTRUSTED)

    registered: dict[str, object] = {
        "get_private_list": _get_private_list,
        "get_single_untrusted": _get_single_untrusted,
        "get_nested_list": _get_nested_list,
    }
    registered.update(tools)
    return CaMeLInterpreter(tools=registered, mode=mode)  # type: ignore[arg-type]


def _sources(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    return interp.get(name).sources


def _deps(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    return interp.get_dependency_graph(name).direct_deps


def _upstream(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    return interp.get_dependency_graph(name).all_upstream


# ---------------------------------------------------------------------------
# Scenario 1 — Loop body variable carries iterable's dependency (STRICT)
#
# Attack: adversary controls list length → observer counts loop iterations.
# Mitigation: every body variable inherits the iterable's dep in STRICT mode,
#   so the policy engine detects untrusted provenance before any tool call.
# ---------------------------------------------------------------------------


class TestLoopCountInferenceBasic:
    """Verify that STRICT mode merges iterable deps into loop body variables."""

    def test_strict_body_variable_carries_iterable_dep(self) -> None:
        """STRICT: loop body variable must carry iterable as direct dep (M4-F1).

        PRD §11 side-channel target: loop-count inference closed in STRICT mode.
        The iterable 'private_list' must appear in dep graph of 'item' and
        'body_result', preventing undetected derivation from loop count.
        """
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    body_result = item
""")
        assert "private_list" in _deps(interp, "body_result"), (
            "STRICT (M4-F1): loop body var must carry iterable dep — "
            "loop-count inference side-channel must be closed"
        )

    def test_strict_loop_target_carries_iterable_dep(self) -> None:
        """STRICT: loop target variable itself carries iterable as direct dep (M4-F1)."""
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    dummy = 0
""")
        assert "private_list" in _deps(interp, "item"), (
            "STRICT (M4-F1): loop target 'item' must carry 'private_list' as dep"
        )

    def test_strict_body_variable_carries_iterable_sources(self) -> None:
        """STRICT: loop body variable inherits iterable's capability sources (M4-F1)."""
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    body_result = item
""")
        assert "external_service" in _sources(interp, "body_result"), (
            "STRICT (M4-F1): iterable sources must propagate to body — "
            "enables policy engine to detect untrusted provenance"
        )

    def test_normal_body_variable_does_not_carry_iterable_dep(self) -> None:
        """NORMAL: loop body variable does NOT carry iterable dep (M4-F1 negative).

        This confirms that without STRICT mode, the loop-count side channel
        is NOT mitigated — demonstrating the value of STRICT mode.
        """
        interp = _interp(ExecutionMode.NORMAL)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    body_result = item
""")
        assert "private_list" not in _deps(interp, "body_result"), (
            "NORMAL (M4-F1 negative): iterable dep must NOT propagate to body "
            "in NORMAL mode — loop-count side channel is unmitigated here"
        )


# ---------------------------------------------------------------------------
# Scenario 2 — Variable computed inside loop carries iterable's upstream taint
#
# Attack: derived value inside loop used in a post-loop tool call; observer
#   infers loop count from how many times a downstream effect occurred.
# Mitigation: STRICT mode ensures the derived variable's all_upstream includes
#   the iterable, triggering policy checks on any tool call using it.
# ---------------------------------------------------------------------------


class TestLoopCountInferenceUpstreamTaint:
    """Verify that upstream taint from iterable reaches derived loop-body variables."""

    def test_strict_derived_body_variable_upstream_includes_iterable(self) -> None:
        """STRICT: upstream of a derived body variable includes the iterable (M4-F1).

        Even when the body variable is computed (not directly assigned from
        item), STRICT mode propagates the iterable to all body assignments.
        """
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    computed = item
""")
        upstream = _upstream(interp, "computed")
        assert "private_list" in upstream, (
            "STRICT: upstream of 'computed' must include 'private_list' (M4-F1)"
        )
        assert "external_service" in _sources(interp, "computed"), (
            "STRICT: 'computed' must carry external_service source (M4-F1)"
        )

    def test_strict_multiple_body_assignments_all_tainted(self) -> None:
        """STRICT: all variables assigned inside a loop body carry iterable taint (M4-F1)."""
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    first = item
    second = 0
    third = "static"
""")
        for var_name in ("first", "second", "third"):
            assert "private_list" in _deps(interp, var_name), (
                f"STRICT (M4-F1): '{var_name}' inside loop must carry "
                f"iterable dep regardless of its value origin"
            )

    def test_strict_nested_loop_inner_body_carries_outer_iterable(self) -> None:
        """STRICT: nested loop inner body carries outer iterable dep (M4-F1)."""

        def _get_inner_list() -> CaMeLValue:
            """Return a second untrusted list."""
            return wrap([10, 20], sources=frozenset({"inner_service"}))

        interp = _interp(ExecutionMode.STRICT, get_inner_list=_get_inner_list)
        interp.exec("outer = get_private_list()")
        interp.exec("inner = get_inner_list()")
        interp.exec("""
for o in outer:
    for i in inner:
        deep_result = i
""")
        deps_deep = _deps(interp, "deep_result")
        assert "outer" in deps_deep, (
            "STRICT nested (M4-F1): outer iterable must appear in inner body deps"
        )
        assert "inner" in deps_deep, (
            "STRICT nested (M4-F1): inner iterable must appear in inner body deps"
        )

    def test_strict_loop_over_trusted_iterable_not_tainted(self) -> None:
        """STRICT: loop over a trusted literal iterable does NOT add untrusted taint.

        The loop-count mitigation relies on capability propagation.  A loop
        over a User-literal list does not introduce external_service taint.
        """
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("""
for item in [1, 2, 3]:
    body = item
""")
        # Sources should only contain "User literal" — no external_service.
        sources_body = _sources(interp, "body")
        assert "external_service" not in sources_body, (
            "STRICT: loop over a trusted literal must not introduce "
            "external_service taint (M4-F1 does not over-taint trusted loops)"
        )

    def test_strict_iterable_taint_closed_before_post_loop_tool_would_run(self) -> None:
        """STRICT: post-loop variable derived from loop body carries iterable taint.

        This ensures that even *after* the loop, any variable that was
        last assigned inside the loop body still carries the iterable's
        sources — the policy engine would catch any tool call using it.
        """
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    last_seen = item
""")
        assert "external_service" in _sources(interp, "last_seen"), (
            "STRICT: 'last_seen' from loop body must carry iterable sources "
            "after loop exits — policy engine would flag downstream tool call"
        )


# ---------------------------------------------------------------------------
# Scenario 3 — PRD §11 pass/fail report helper
# ---------------------------------------------------------------------------


class TestSideChannelReportLoopCount:
    """Meta-tests that validate the PRD §11 pass-rate target for loop-count mitigation."""

    def test_prd_section11_loop_count_target_100_percent_strict(self) -> None:
        """PRD §11: all loop-count side-channel mitigations pass in STRICT mode.

        This test exercises a representative end-to-end scenario:
        private_list (untrusted) → for-loop → body variable.
        In STRICT mode, policy engine will detect the taint and block
        any unapproved downstream tool call.
        """
        interp = _interp(ExecutionMode.STRICT)
        interp.exec("private_list = get_private_list()")
        interp.exec("""
for item in private_list:
    processed = item
""")
        # All three properties must hold simultaneously.
        assert "private_list" in _deps(interp, "processed"), (
            "PRD §11 loop-count: dep-graph taint present (M4-F1)"
        )
        assert "external_service" in _sources(interp, "processed"), (
            "PRD §11 loop-count: capability sources tainted (M4-F1)"
        )
        assert "private_list" in _upstream(interp, "processed"), (
            "PRD §11 loop-count: upstream taint present (M4-F1)"
        )
