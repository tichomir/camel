"""STRICT mode dependency propagation test suite — unit tests.

Feature mapping
---------------
Each test method documents its corresponding feature ID in its docstring.

M4-F1: for-loop iterable taint — every variable assigned in the body of a
       ``for`` loop over an untrusted iterable must carry:
       (a) the iterable variable as a direct dep in the dependency graph, AND
       (b) the iterable's capability sources merged into the assigned value.

M4-F2: if/else conditional test taint — variables assigned in either branch
       of an ``if`` statement carry:
       (a) the test variable as a direct dep in the dependency graph, AND
       (b) the test expression's capability sources merged into the result.

M4-F3: post-Q-LLM-call capability taint — all assignments following a
       ``query_quarantined_llm()`` call in the same block carry the Q-LLM
       result's capability sources merged into their context.

M4-F4: post-Q-LLM-call dependency graph recording — subsequent assignments
       in the same block record the Q-LLM variable as a direct dep.

M4-F5: STRICT mode is the default — NORMAL mode requires explicit opt-in.
"""

from __future__ import annotations

import pytest

from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.value import CaMeLValue, Public, wrap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QLLM_SOURCES: frozenset[str] = frozenset({"query_quarantined_llm"})
_UNTRUSTED_SOURCES: frozenset[str] = frozenset({"get_email"})
_TOOL_SOURCES: frozenset[str] = frozenset({"get_items"})


def _make_interp(mode: ExecutionMode = ExecutionMode.STRICT, **extra_tools: object) -> CaMeLInterpreter:
    """Build a CaMeLInterpreter with a standard set of mock tools."""

    def _qllm_tool(prompt: object, schema: object = None) -> CaMeLValue:
        """Mock query_quarantined_llm returning a value with Q-LLM sources."""
        return wrap({"recipient": "attacker@evil.com"}, sources=_QLLM_SOURCES)

    def _get_email() -> CaMeLValue:
        """Return a mock email value with untrusted sources."""
        return wrap("email body from attacker", sources=_UNTRUSTED_SOURCES)

    def _get_items() -> CaMeLValue:
        """Return an untrusted list."""
        return wrap([1, 2, 3], sources=_TOOL_SOURCES)

    tools: dict[str, object] = {
        "query_quarantined_llm": _qllm_tool,
        "get_email": _get_email,
        "get_items": _get_items,
    }
    tools.update(extra_tools)
    return CaMeLInterpreter(tools=tools, mode=mode)  # type: ignore[arg-type]


def _sources(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return the sources of the variable *name* in *interp*."""
    return interp.get(name).sources


def _deps(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return the direct dep-graph deps of *name* in *interp*."""
    return interp.get_dependency_graph(name).direct_deps


def _upstream(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    """Return the transitive dep-graph upstream of *name* in *interp*."""
    return interp.get_dependency_graph(name).all_upstream


# ---------------------------------------------------------------------------
# Test 1 — M4-F1: for-loop body variable carries iterable dep (dep graph)
# ---------------------------------------------------------------------------


def test_m4f1_for_loop_body_dep_graph():
    """M4-F1: In STRICT mode, body vars carry iterable var in dep graph.

    Feature: M4-F1
    Given:  items = get_items()  (untrusted tool)
            for item in items: body = item
    Expect: 'items' appears in direct_deps of 'body' in STRICT mode.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("items = get_items()")
    interp.exec("""
for item in items:
    body = item
""")
    assert "items" in _deps(interp, "body"), (
        "STRICT mode: 'items' must be a direct dep of 'body' (M4-F1)"
    )


# ---------------------------------------------------------------------------
# Test 2 — M4-F1: for-loop body variable carries iterable capability sources
# ---------------------------------------------------------------------------


def test_m4f1_for_loop_body_capability_sources():
    """M4-F1: In STRICT mode, body vars inherit iterable capability sources.

    Feature: M4-F1
    Given:  items = get_items()  (sources={"get_items"})
            for item in items: body = item
    Expect: 'body'.sources includes "get_items" in STRICT mode.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("items = get_items()")
    interp.exec("""
for item in items:
    body = item
""")
    assert "get_items" in _sources(interp, "body"), (
        "STRICT mode: iterable sources must propagate to body vars (M4-F1)"
    )


# ---------------------------------------------------------------------------
# Test 3 — M4-F2: if-condition dep propagates to body variable (dep graph)
# ---------------------------------------------------------------------------


def test_m4f2_if_condition_dep_graph():
    """M4-F2: In STRICT mode, if-test variable appears in body dep graph.

    Feature: M4-F2
    Given:  flag = get_email()  (untrusted test value)
            val = 1
            if flag: result = val
    Expect: 'flag' in direct_deps of 'result'.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("flag = get_email()")
    interp.exec("val = 1")
    interp.exec("""
if flag:
    result = val
""")
    assert "flag" in _deps(interp, "result"), (
        "STRICT mode: if-test var must be a direct dep of body vars (M4-F2)"
    )


# ---------------------------------------------------------------------------
# Test 4 — M4-F2: if-condition capability sources propagate to body variable
# ---------------------------------------------------------------------------


def test_m4f2_if_condition_capability_sources():
    """M4-F2: In STRICT mode, if-test sources merge into body variable.

    Feature: M4-F2
    Given:  flag = get_email()  (sources={"get_email"})
            val = 1
            if flag: result = val
    Expect: 'result'.sources includes "get_email".
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("flag = get_email()")
    interp.exec("val = 1")
    interp.exec("""
if flag:
    result = val
""")
    assert "get_email" in _sources(interp, "result"), (
        "STRICT mode: if-test sources must propagate to body vars (M4-F2)"
    )


# ---------------------------------------------------------------------------
# Test 5 — M4-F2: else-branch variable also carries test dep
# ---------------------------------------------------------------------------


def test_m4f2_else_branch_also_carries_test_dep():
    """M4-F2: In STRICT mode, else-branch vars carry if-test deps.

    Feature: M4-F2
    Given:  flag = 0  (falsy literal, but STRICT merges the flag dep)
            a = 1; b = 2
            if flag: result = a
            else: result = b
    Expect: 'flag' in direct_deps of 'result' (else branch executed).
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("flag = get_email()")
    interp.exec("b = 2")
    # Patch store: make flag falsy but untrusted
    interp._store["flag"] = wrap(0, sources=frozenset({"get_email"}))
    interp.exec("""
if flag:
    result = 1
else:
    result = b
""")
    assert "flag" in _deps(interp, "result"), (
        "STRICT mode: else-branch vars must carry the if-test dep (M4-F2)"
    )


# ---------------------------------------------------------------------------
# Test 6 — M4-F3: post-Q-LLM capability taint on subsequent assignment
# ---------------------------------------------------------------------------


def test_m4f3_post_qllm_capability_taint():
    """M4-F3: In STRICT mode, assignments after Q-LLM carry Q-LLM sources.

    Feature: M4-F3
    Given:  extraction = query_quarantined_llm(...)  (Q-LLM call)
            note = "static string"  (constant — no direct Q-LLM usage)
    Expect: 'note'.sources includes "query_quarantined_llm".
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("""
extraction = query_quarantined_llm("extract", "schema")
note = "static string"
""")
    assert "query_quarantined_llm" in _sources(interp, "note"), (
        "STRICT mode: post-Q-LLM assignment must carry Q-LLM sources (M4-F3)"
    )


# ---------------------------------------------------------------------------
# Test 7 — M4-F4: post-Q-LLM dep-graph taint on subsequent assignment
# ---------------------------------------------------------------------------


def test_m4f4_post_qllm_dep_graph_taint():
    """M4-F4: In STRICT mode, assignments after Q-LLM carry Q-LLM var as dep.

    Feature: M4-F4
    Given:  extraction = query_quarantined_llm(...)
            note = "static string"
    Expect: 'extraction' in direct_deps of 'note'.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("""
extraction = query_quarantined_llm("extract", "schema")
note = "static string"
""")
    assert "extraction" in _deps(interp, "note"), (
        "STRICT mode: Q-LLM variable must appear in dep graph of subsequent "
        "assignments (M4-F4)"
    )


# ---------------------------------------------------------------------------
# Test 8 — M4-F3/F4: Q-LLM taint scoped to current block (scope-exit cleanup)
# ---------------------------------------------------------------------------


def test_m4f3_f4_qllm_taint_does_not_leak_outside_block():
    """M4-F3/F4: Q-LLM taint is scoped to the block where Q-LLM was called.

    Feature: M4-F3, M4-F4
    Given:  if True:
                extraction = query_quarantined_llm(...)
                tainted = "a"  ← should carry Q-LLM taint
            outside = "b"  ← Q-LLM taint must NOT appear here
    Expect: 'extraction' in deps of 'tainted' but NOT in deps of 'outside'.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("""
if 1:
    extraction = query_quarantined_llm("extract", "schema")
    tainted = "inside"
outside = "outside"
""")
    # Taint inside the block
    assert "extraction" in _deps(interp, "tainted"), (
        "STRICT mode: Q-LLM taint must appear inside its block (M4-F4)"
    )
    # No taint outside the block
    assert "extraction" not in _deps(interp, "outside"), (
        "STRICT mode: Q-LLM taint must not leak outside its block (M4-F4 scope)"
    )


# ---------------------------------------------------------------------------
# Test 9 — M4-F1 NORMAL mode negative: iterable does NOT taint body vars
# ---------------------------------------------------------------------------


def test_m4f1_normal_mode_iterable_does_not_taint_body():
    """M4-F1 negative: In NORMAL mode, for-loop iterable does NOT taint body.

    Feature: M4-F1 (negative / NORMAL mode)
    Given:  items = get_items()
            for item in items: body = item
    Expect: 'items' is NOT in direct_deps of 'body' (NORMAL mode).
    """
    interp = _make_interp(ExecutionMode.NORMAL)
    interp.exec("items = get_items()")
    interp.exec("""
for item in items:
    body = item
""")
    assert "items" not in _deps(interp, "body"), (
        "NORMAL mode: iterable must NOT be added to body dep graph (M4-F1 negative)"
    )


# ---------------------------------------------------------------------------
# Test 10 — M4-F2 NORMAL mode negative: if-condition does NOT taint body
# ---------------------------------------------------------------------------


def test_m4f2_normal_mode_condition_does_not_taint_body():
    """M4-F2 negative: In NORMAL mode, if-test does NOT taint body var.

    Feature: M4-F2 (negative / NORMAL mode)
    Given:  flag = get_email()  (untrusted)
            val = 1
            if flag: result = val
    Expect: 'flag' NOT in direct_deps of 'result' (NORMAL mode).
    """
    interp = _make_interp(ExecutionMode.NORMAL)
    interp.exec("flag = get_email()")
    interp.exec("val = 1")
    interp.exec("""
if flag:
    result = val
""")
    assert "flag" not in _deps(interp, "result"), (
        "NORMAL mode: if-test must NOT taint body dep graph (M4-F2 negative)"
    )


# ---------------------------------------------------------------------------
# Test 11 — M4-F3/F4 NORMAL mode negative: Q-LLM does NOT taint subsequent
# ---------------------------------------------------------------------------


def test_m4f3_f4_normal_mode_qllm_does_not_taint():
    """M4-F3/F4 negative: In NORMAL mode, Q-LLM call does NOT taint subsequent.

    Feature: M4-F3, M4-F4 (negative / NORMAL mode)
    Given:  extraction = query_quarantined_llm(...)
            note = "static string"
    Expect: 'extraction' NOT in deps of 'note' (NORMAL mode).
    """
    interp = _make_interp(ExecutionMode.NORMAL)
    interp.exec("""
extraction = query_quarantined_llm("extract", "schema")
note = "static string"
""")
    assert "extraction" not in _deps(interp, "note"), (
        "NORMAL mode: Q-LLM must NOT taint subsequent dep graph (M4-F4 negative)"
    )
    # Capability sources: in NORMAL mode, note comes from a constant, so it
    # must NOT carry Q-LLM sources via context.
    assert "query_quarantined_llm" not in _sources(interp, "note"), (
        "NORMAL mode: Q-LLM sources must NOT propagate to subsequent vars (M4-F3 negative)"
    )


# ---------------------------------------------------------------------------
# Test 12 — M4-F5: default mode is STRICT
# ---------------------------------------------------------------------------


def test_m4f5_default_mode_is_strict():
    """M4-F5: CaMeLInterpreter defaults to STRICT mode.

    Feature: M4-F5
    STRICT is the recommended production default.  NORMAL mode must be
    passed explicitly via ``mode=ExecutionMode.NORMAL``.
    """
    interp = CaMeLInterpreter()
    assert interp._mode is ExecutionMode.STRICT, (
        "Default mode must be ExecutionMode.STRICT (M4-F5)"
    )


# ---------------------------------------------------------------------------
# Test 13 — M4-F5: NORMAL mode requires explicit opt-in
# ---------------------------------------------------------------------------


def test_m4f5_normal_mode_requires_explicit_opt_in():
    """M4-F5: NORMAL mode is not active unless explicitly requested.

    Feature: M4-F5
    Demonstrate that without mode=ExecutionMode.NORMAL, the if-condition
    propagates taint (STRICT behaviour), whereas with NORMAL it does not.
    """
    interp_strict = CaMeLInterpreter(tools={"get_email": lambda: wrap(1, sources=frozenset({"get_email"}))})
    interp_strict.exec("flag = get_email()")
    interp_strict.exec("v = 42")
    interp_strict.exec("""
if flag:
    result = v
""")
    # STRICT by default: flag should be in deps
    assert "flag" in _deps(interp_strict, "result"), (
        "Default (STRICT) mode must propagate if-test deps (M4-F5)"
    )

    interp_normal = CaMeLInterpreter(
        tools={"get_email": lambda: wrap(1, sources=frozenset({"get_email"}))},
        mode=ExecutionMode.NORMAL,
    )
    interp_normal.exec("flag = get_email()")
    interp_normal.exec("v = 42")
    interp_normal.exec("""
if flag:
    result = v
""")
    # NORMAL explicit: flag should NOT be in deps
    assert "flag" not in _deps(interp_normal, "result"), (
        "Explicit NORMAL mode must not propagate if-test deps (M4-F5)"
    )


# ---------------------------------------------------------------------------
# Test 14 — M4-F3/F4: multiple Q-LLM calls accumulate deps
# ---------------------------------------------------------------------------


def test_m4f3_f4_multiple_qllm_calls_accumulate():
    """M4-F3/F4: Multiple Q-LLM calls in the same block accumulate taint.

    Feature: M4-F3, M4-F4
    Given:  a = query_quarantined_llm(...)
            b = query_quarantined_llm(...)
            note = "static"
    Expect: both 'a' and 'b' appear in deps of 'note'.
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("""
a = query_quarantined_llm("first", "schema")
b = query_quarantined_llm("second", "schema")
note = "static"
""")
    deps_note = _deps(interp, "note")
    assert "a" in deps_note, (
        "STRICT mode: first Q-LLM var 'a' must be in 'note' deps (M4-F4)"
    )
    assert "b" in deps_note, (
        "STRICT mode: second Q-LLM var 'b' must be in 'note' deps (M4-F4)"
    )


# ---------------------------------------------------------------------------
# Test 15 — M4-F1: loop target itself carries iterable dep (both modes)
# ---------------------------------------------------------------------------


def test_m4f1_loop_target_carries_iterable_dep():
    """M4-F1: Loop target variable carries the iterable as a direct dep.

    Feature: M4-F1
    The loop target assignment (item = items[i]) must record 'items' as a
    direct dep of 'item' in both STRICT and NORMAL modes.
    """
    for mode in (ExecutionMode.STRICT, ExecutionMode.NORMAL):
        interp = _make_interp(mode)
        interp.exec("items = get_items()")
        interp.exec("""
for item in items:
    dummy = 0
""")
        assert "items" in _deps(interp, "item"), (
            f"Loop target 'item' must carry 'items' as dep in {mode.value} mode (M4-F1)"
        )


# ---------------------------------------------------------------------------
# Test 16 — M4-F1: nested loops — inner body carries both iterables
# ---------------------------------------------------------------------------


def test_m4f1_nested_loops_inner_body_carries_both_iterables():
    """M4-F1: In STRICT mode, nested loop body vars carry deps from both iterables.

    Feature: M4-F1
    Given:  outer_list = get_items()   (sources={"get_items"})
            inner_list = get_email()   (sources={"get_email"})
            for outer in outer_list:
                for inner in inner_list:
                    body = inner
    Expect: 'outer_list' in deps of 'body' AND 'inner_list' in deps of 'body'.
    """
    def _get_inner() -> CaMeLValue:
        """Return a second untrusted list."""
        return wrap([4, 5], sources=frozenset({"get_email"}))

    interp = _make_interp(
        ExecutionMode.STRICT,
        get_inner=_get_inner,
    )
    interp.exec("outer_list = get_items()")
    interp.exec("inner_list = get_inner()")
    interp.exec("""
for outer in outer_list:
    for inner in inner_list:
        body = inner
""")
    assert "outer_list" in _deps(interp, "body"), (
        "STRICT nested loops: outer iterable must be dep of inner body (M4-F1)"
    )
    assert "inner_list" in _deps(interp, "body"), (
        "STRICT nested loops: inner iterable must be dep of inner body (M4-F1)"
    )
    assert "get_items" in _sources(interp, "body"), (
        "STRICT nested loops: outer iterable sources must propagate to body (M4-F1)"
    )
    assert "get_email" in _sources(interp, "body"), (
        "STRICT nested loops: inner iterable sources must propagate to body (M4-F1)"
    )


# ---------------------------------------------------------------------------
# Test 17 — M4-F2: if inside for-loop body carries both test and iterable dep
# ---------------------------------------------------------------------------


def test_m4f2_if_inside_for_loop_carries_both_deps():
    """M4-F2 + M4-F1: if inside a loop body carries test dep AND iterable dep.

    Feature: M4-F1, M4-F2
    Given:  items = get_items()   (untrusted iterable)
            flag = get_email()    (untrusted if-test)
            for item in items:
                if flag:
                    result = item
    Expect (STRICT): 'result' deps include both 'items' and 'flag';
                     sources include both "get_items" and "get_email".
    """

    def _get_flag() -> CaMeLValue:
        """Return a truthy untrusted flag."""
        return wrap(1, sources=frozenset({"get_email"}))

    interp = _make_interp(ExecutionMode.STRICT, get_flag=_get_flag)
    interp.exec("items = get_items()")
    interp.exec("flag = get_flag()")
    interp.exec("""
for item in items:
    if flag:
        result = item
""")
    deps_result = _deps(interp, "result")
    assert "items" in deps_result, (
        "STRICT (M4-F1): loop iterable 'items' must be dep of 'result'"
    )
    assert "flag" in deps_result, (
        "STRICT (M4-F2): if-test 'flag' must be dep of 'result' inside loop"
    )
    assert "get_items" in _sources(interp, "result"), (
        "STRICT (M4-F1): loop iterable sources must propagate to 'result'"
    )
    assert "get_email" in _sources(interp, "result"), (
        "STRICT (M4-F2): if-test sources must propagate to 'result'"
    )


# ---------------------------------------------------------------------------
# Test 18 — M4-F3/F4: Q-LLM call inside for-loop body taints subsequent vars
# ---------------------------------------------------------------------------


def test_m4f3_f4_qllm_inside_loop_body_taints_subsequent():
    """M4-F3/F4 + M4-F1: Q-LLM call inside a loop body taints subsequent vars.

    Feature: M4-F3, M4-F4, M4-F1
    Given:  items = get_items()
            for item in items:
                extracted = query_quarantined_llm(item, "schema")
                post = "after"
    Expect (STRICT):
        - 'extracted' dep includes 'item' (direct arg)
        - 'post' dep includes 'extracted' (M4-F4 Q-LLM dep ctx)
        - 'post' sources include "query_quarantined_llm" (M4-F3)
        - 'post' sources include "get_items" (M4-F1 iterable propagation)
    """
    interp = _make_interp(ExecutionMode.STRICT)
    interp.exec("items = get_items()")
    interp.exec("""
for item in items:
    extracted = query_quarantined_llm(item, "schema")
    post = "after"
""")
    # Direct arg dep: extracted depends on item
    assert "item" in _deps(interp, "extracted"), (
        "STRICT (M4-F4): 'extracted' must depend on 'item' (direct Q-LLM arg)"
    )
    # Q-LLM dep ctx: post carries extracted as dep (M4-F4)
    assert "extracted" in _deps(interp, "post"), (
        "STRICT (M4-F4): 'post' must carry 'extracted' dep (Q-LLM inside loop)"
    )
    # Q-LLM capability taint: post carries Q-LLM sources (M4-F3)
    assert "query_quarantined_llm" in _sources(interp, "post"), (
        "STRICT (M4-F3): 'post' must carry Q-LLM sources (Q-LLM inside loop)"
    )
    # Iterable taint: post is inside the loop so must carry get_items sources (M4-F1)
    assert "get_items" in _sources(interp, "post"), (
        "STRICT (M4-F1): 'post' inside loop must carry iterable sources"
    )
