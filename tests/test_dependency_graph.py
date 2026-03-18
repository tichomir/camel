"""Tests for dependency graph tracking in CaMeLInterpreter.

Coverage
--------
- DependencyGraph / _InternalGraph standalone unit tests
- NORMAL mode: direct assignment chains
- NORMAL mode: augmented assignment self-deps
- NORMAL mode: tuple-unpacking deps
- NORMAL mode: tool call with no var-ref RHS (no deps)
- NORMAL mode: tool call whose return is then used in expressions
- M4-F3/F4 STRICT mode: post-Q-LLM-call statement dependency propagation
- M4-F3/F4 STRICT mode: Q-LLM flag scoped to current block only
- M4-F3/F4 STRICT mode: multiple Q-LLM calls accumulate deps
- M4-F3/F4 NORMAL mode: Q-LLM calls do NOT taint subsequent statements
- STRICT mode: if-condition variables propagate to body assignments
- STRICT mode: else-branch also carries test deps
- STRICT mode: for-iterable variables propagate to body assignments
- STRICT mode: loop target carries iterable deps
- STRICT mode: nested if-inside-for
- STRICT mode: nested for-inside-for
- STRICT mode: nested if-inside-if
- STRICT mode: regression — loop-body vars carry iterable dep
- NORMAL mode: control flow does NOT add deps (negative STRICT test)
- get_dependency_graph() returns correct DependencyGraph snapshots
- set_mode() switches mode mid-session
- Mode defaults to STRICT (M4-F5)
- Transitive all_upstream computation
- Variables unknown to graph → empty DependencyGraph
- Multiple assignments to same var accumulate deps (union semantics)
- AugAssign self-dep in STRICT mode
"""

from __future__ import annotations

from camel.dependency_graph import DependencyGraph, _InternalGraph
from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.value import wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interp(mode: ExecutionMode = ExecutionMode.NORMAL, **tools):
    return CaMeLInterpreter(tools=tools or None, mode=mode)


def _tool(raw_value, sources=None):
    def _fn():
        return wrap(
            raw_value,
            sources=frozenset(sources) if sources else frozenset({"tool"}),
        )

    return _fn


def _tool1(raw_value, sources=None):
    """One-argument tool."""

    def _fn(arg):
        return wrap(
            raw_value,
            sources=frozenset(sources) if sources else frozenset({"tool"}),
        )

    return _fn


def _deps(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    return interp.get_dependency_graph(name).direct_deps


def _upstream(interp: CaMeLInterpreter, name: str) -> frozenset[str]:
    return interp.get_dependency_graph(name).all_upstream


# ---------------------------------------------------------------------------
# _InternalGraph standalone tests
# ---------------------------------------------------------------------------


class TestInternalGraph:
    def test_empty_graph_direct_deps(self):
        g = _InternalGraph()
        assert g.direct_deps("x") == frozenset()

    def test_empty_graph_all_upstream(self):
        g = _InternalGraph()
        assert g.all_upstream("x") == frozenset()

    def test_simple_chain(self):
        g = _InternalGraph()
        g.record("b", frozenset({"a"}))
        g.record("c", frozenset({"b"}))
        assert g.direct_deps("c") == frozenset({"b"})
        assert g.all_upstream("c") == frozenset({"a", "b"})

    def test_subgraph_edges(self):
        g = _InternalGraph()
        g.record("b", frozenset({"a"}))
        g.record("c", frozenset({"b"}))
        dg = g.subgraph("c")
        assert dg.variable == "c"
        assert dg.direct_deps == frozenset({"b"})
        assert dg.all_upstream == frozenset({"a", "b"})
        assert ("c", "b") in dg.edges
        assert ("b", "a") in dg.edges

    def test_union_semantics_on_repeated_record(self):
        g = _InternalGraph()
        g.record("x", frozenset({"a"}))
        g.record("x", frozenset({"b"}))
        assert g.direct_deps("x") == frozenset({"a", "b"})

    def test_self_edge_cycle_no_infinite_loop(self):
        g = _InternalGraph()
        g.record("x", frozenset({"x", "y"}))
        upstream = g.all_upstream("x")
        assert "y" in upstream

    def test_empty_deps_records_variable(self):
        g = _InternalGraph()
        g.record("x", frozenset())
        assert g.direct_deps("x") == frozenset()
        assert g.all_upstream("x") == frozenset()


# ---------------------------------------------------------------------------
# Test 1 — NORMAL mode: simple assignment chain
# ---------------------------------------------------------------------------


def test_normal_simple_chain():
    """a = tool(); b = a; c = b  →  c deps={b}, upstream={a,b}."""
    interp = _interp(tool=_tool(42))
    interp.exec("a = tool()")
    interp.exec("b = a")
    interp.exec("c = b")

    assert _deps(interp, "a") == frozenset()
    assert _deps(interp, "b") == frozenset({"a"})
    assert _deps(interp, "c") == frozenset({"b"})
    assert _upstream(interp, "c") == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# Test 2 — NORMAL mode: tool call returns value, used in expression
# ---------------------------------------------------------------------------


def test_normal_tool_then_expression():
    """x = tool(); y = x + 1  →  y deps={x}, x deps={}."""
    interp = _interp(tool=_tool(10))
    interp.exec("x = tool()")
    interp.exec("y = x + 1")

    assert _deps(interp, "x") == frozenset()
    assert _deps(interp, "y") == frozenset({"x"})
    assert _upstream(interp, "y") == frozenset({"x"})


# ---------------------------------------------------------------------------
# Test 3 — NORMAL mode: constant-only RHS has no var deps
# ---------------------------------------------------------------------------


def test_normal_constant_rhs():
    """z = 5  →  z deps={}."""
    interp = _interp()
    interp.exec("z = 5")
    assert _deps(interp, "z") == frozenset()


# ---------------------------------------------------------------------------
# Test 4 — NORMAL mode: augmented assignment records self-dep
# ---------------------------------------------------------------------------


def test_normal_augassign_self_dep():
    """x = 0; y = tool(); x += y  →  x deps={x, y}."""
    interp = _interp(tool=_tool(5))
    interp.exec("x = 0")
    interp.exec("y = tool()")
    interp.exec("x += y")

    assert _deps(interp, "x") == frozenset({"x", "y"})
    assert "y" in _upstream(interp, "x")


# ---------------------------------------------------------------------------
# Test 5 — NORMAL mode: tuple unpacking propagates same deps to both vars
# ---------------------------------------------------------------------------


def test_normal_tuple_unpack():
    """a = tool(); b, c = a  — both b and c depend on a."""
    # tool returns a 2-tuple
    interp = _interp(tool=_tool([10, 20]))
    interp.exec("a = tool()")
    interp.exec("b, c = a")

    assert _deps(interp, "b") == frozenset({"a"})
    assert _deps(interp, "c") == frozenset({"a"})


# ---------------------------------------------------------------------------
# Test 6 — NORMAL mode: multiple variables in RHS
# ---------------------------------------------------------------------------


def test_normal_multi_var_rhs():
    """a = 1; b = 2; c = a + b  →  c deps={a, b}."""
    interp = _interp()
    interp.exec("a = 1")
    interp.exec("b = 2")
    interp.exec("c = a + b")

    assert _deps(interp, "c") == frozenset({"a", "b"})
    assert _upstream(interp, "c") == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# Test 7 — NORMAL mode: if-condition does NOT add deps to body vars
# ---------------------------------------------------------------------------


def test_normal_if_no_control_flow_deps():
    """In NORMAL mode, if flag: x = a  should NOT add flag to x's deps."""
    interp = _interp()
    interp.exec("flag = 1")
    interp.exec("a = 2")
    interp.exec("""
if flag:
    x = a
""")
    # NORMAL mode: x depends only on a, not on flag
    assert _deps(interp, "x") == frozenset({"a"})
    assert "flag" not in _upstream(interp, "x")


# ---------------------------------------------------------------------------
# Test 8 — NORMAL mode: for-iterable DOES add direct dep for loop target
# ---------------------------------------------------------------------------


def test_normal_for_loop_target_dep():
    """In NORMAL mode, the loop variable depends on the iterable variable."""
    interp = _interp()
    interp.exec("items = [1, 2, 3]")
    interp.exec("""
for item in items:
    dummy = item
""")
    # Loop target 'item' depends on iterable 'items'
    assert _deps(interp, "item") == frozenset({"items"})


# ---------------------------------------------------------------------------
# Test 9 — NORMAL mode: for body assignments use their own RHS vars only
# ---------------------------------------------------------------------------


def test_normal_for_body_no_iterable_dep():
    """In NORMAL mode, body var depends on what's in the RHS, not the iterable."""
    interp = _interp()
    interp.exec("items = [1, 2, 3]")
    interp.exec("base = 10")
    interp.exec("""
for item in items:
    result = item + base
""")
    # result directly depends on item and base; upstream includes items (via item)
    assert _deps(interp, "result") == frozenset({"item", "base"})
    assert _upstream(interp, "result") == frozenset({"item", "base", "items"})
    # items not in direct_deps of result in NORMAL mode
    assert "items" not in _deps(interp, "result")


# ---------------------------------------------------------------------------
# Test 10 — STRICT mode: if-condition adds test vars to body assignments
# ---------------------------------------------------------------------------


def test_strict_if_condition_propagates():
    """In STRICT mode, if flag: x = a  makes x depend on both a and flag."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("flag = 1")
    interp.exec("a = 2")
    interp.exec("""
if flag:
    x = a
""")
    assert _deps(interp, "x") == frozenset({"a", "flag"})
    assert "flag" in _upstream(interp, "x")


# ---------------------------------------------------------------------------
# Test 11 — STRICT mode: else branch also carries test deps
# ---------------------------------------------------------------------------


def test_strict_else_also_carries_test_deps():
    """In STRICT mode, the else-branch assignments also carry the test deps."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("flag = 0")
    interp.exec("a = 5")
    interp.exec("b = 6")
    interp.exec("""
if flag:
    x = a
else:
    x = b
""")
    # else branch executed; x = b; but test dep (flag) must still be on x
    assert "flag" in _deps(interp, "x")
    assert "b" in _deps(interp, "x")


# ---------------------------------------------------------------------------
# Test 12 — STRICT mode: for-iterable propagates to body assignments
# ---------------------------------------------------------------------------


def test_strict_for_body_carries_iterable_dep():
    """STRICT regression: body vars must carry the iterable variable as dep."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("items = [1, 2, 3]")
    interp.exec("base = 10")
    interp.exec("""
for item in items:
    result = item + base
""")
    # In STRICT mode, result depends on item + base AND items (from iterable ctx)
    direct = _deps(interp, "result")
    assert "item" in direct
    assert "base" in direct
    assert "items" in direct


# ---------------------------------------------------------------------------
# Test 13 — STRICT mode: loop target carries iterable dep
# ---------------------------------------------------------------------------


def test_strict_loop_target_dep():
    """In STRICT mode, loop target depends on iterable variable."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("items = [10, 20]")
    interp.exec("""
for item in items:
    dummy = item
""")
    assert "items" in _deps(interp, "item")


# ---------------------------------------------------------------------------
# Test 14 — STRICT mode: nested if-inside-for
# ---------------------------------------------------------------------------


def test_strict_nested_if_inside_for():
    """Nested STRICT: body var gets both iterable dep and if-condition dep."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("items = [1, 2, 3]")
    interp.exec("flag = 1")
    interp.exec("val = 99")
    interp.exec("""
for item in items:
    if flag:
        inner = val
""")
    direct = _deps(interp, "inner")
    assert "val" in direct
    assert "flag" in direct  # from the if-test
    assert "items" in direct  # from the for-iterable (STRICT ctx propagates)


# ---------------------------------------------------------------------------
# Test 15 — STRICT mode: nested for-inside-for
# ---------------------------------------------------------------------------


def test_strict_nested_for_inside_for():
    """Nested STRICT: inner loop body carries deps from both iterables."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("outer_list = [1, 2]")
    interp.exec("inner_list = [3, 4]")
    interp.exec("base = 0")
    interp.exec("""
for o in outer_list:
    for i in inner_list:
        result = base + i
""")
    direct = _deps(interp, "result")
    assert "base" in direct
    assert "i" in direct
    # Both iterables should appear in deps (transitively via STRICT ctx)
    assert "outer_list" in direct
    assert "inner_list" in direct


# ---------------------------------------------------------------------------
# Test 16 — STRICT mode: nested if-inside-if
# ---------------------------------------------------------------------------


def test_strict_nested_if_inside_if():
    """Deeply nested STRICT: both outer and inner conditions propagate."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("outer_flag = 1")
    interp.exec("inner_flag = 1")
    interp.exec("val = 42")
    interp.exec("""
if outer_flag:
    if inner_flag:
        x = val
""")
    direct = _deps(interp, "x")
    assert "val" in direct
    assert "outer_flag" in direct
    assert "inner_flag" in direct


# ---------------------------------------------------------------------------
# Test 17 — get_dependency_graph returns correct DependencyGraph object
# ---------------------------------------------------------------------------


def test_get_dependency_graph_structure():
    """get_dependency_graph returns a frozen DependencyGraph with correct fields."""
    interp = _interp()
    interp.exec("a = 1")
    interp.exec("b = a")
    interp.exec("c = b")

    dg = interp.get_dependency_graph("c")
    assert isinstance(dg, DependencyGraph)
    assert dg.variable == "c"
    assert dg.direct_deps == frozenset({"b"})
    assert dg.all_upstream == frozenset({"a", "b"})
    assert ("c", "b") in dg.edges
    assert ("b", "a") in dg.edges


# ---------------------------------------------------------------------------
# Test 18 — get_dependency_graph for unknown variable
# ---------------------------------------------------------------------------


def test_get_dependency_graph_unknown_variable():
    """Querying an unassigned variable returns an empty DependencyGraph."""
    interp = _interp()
    dg = interp.get_dependency_graph("nonexistent")
    assert dg.variable == "nonexistent"
    assert dg.direct_deps == frozenset()
    assert dg.all_upstream == frozenset()
    assert dg.edges == frozenset()


# ---------------------------------------------------------------------------
# Test 19 — set_mode switches mode mid-session
# ---------------------------------------------------------------------------


def test_set_mode_switches_mode():
    """set_mode('strict') enables STRICT dep tracking for subsequent execs."""
    interp = _interp(mode=ExecutionMode.NORMAL)
    interp.exec("flag = 1")
    interp.exec("a = 2")
    # In NORMAL mode, this if-body should NOT add flag to x's deps
    interp.exec("""
if flag:
    x = a
""")
    assert "flag" not in _deps(interp, "x")

    # Switch to STRICT
    interp.set_mode("strict")
    interp.exec("b = 3")
    interp.exec("""
if flag:
    y = b
""")
    # In STRICT mode, y should depend on both b and flag
    assert "flag" in _deps(interp, "y")
    assert "b" in _deps(interp, "y")


# ---------------------------------------------------------------------------
# Test 20 — mode defaults to STRICT (M4-F5)
# ---------------------------------------------------------------------------


def test_default_mode_is_strict():
    """CaMeLInterpreter defaults to STRICT mode (M4-F5).

    STRICT is the recommended production default.  NORMAL mode must be passed
    explicitly via ``mode=ExecutionMode.NORMAL``.
    """
    interp = CaMeLInterpreter()
    assert interp._mode == ExecutionMode.STRICT


# ---------------------------------------------------------------------------
# Test 21 — set_mode accepts ExecutionMode enum directly
# ---------------------------------------------------------------------------


def test_set_mode_accepts_enum():
    """set_mode accepts ExecutionMode enum values directly."""
    interp = _interp()
    interp.set_mode(ExecutionMode.STRICT)
    assert interp._mode == ExecutionMode.STRICT
    interp.set_mode(ExecutionMode.NORMAL)
    assert interp._mode == ExecutionMode.NORMAL


# ---------------------------------------------------------------------------
# Test 22 — multiple assignments to same variable accumulate deps
# ---------------------------------------------------------------------------


def test_multiple_assignments_union_deps():
    """Reassigning a variable unions the new deps with the old ones."""
    interp = _interp()
    interp.exec("a = 1")
    interp.exec("b = 2")
    interp.exec("x = a")
    interp.exec("x = b")  # reassign
    # Union: x depends on both a and b
    assert _deps(interp, "x") == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# Test 23 — transitive upstream across a long chain
# ---------------------------------------------------------------------------


def test_long_transitive_chain():
    """Verify transitive closure across a 5-variable chain."""
    interp = _interp(tool=_tool(0))
    interp.exec("a = tool()")
    interp.exec("b = a")
    interp.exec("c = b")
    interp.exec("d = c")
    interp.exec("e = d")

    dg = interp.get_dependency_graph("e")
    assert dg.direct_deps == frozenset({"d"})
    assert dg.all_upstream == frozenset({"a", "b", "c", "d"})


# ---------------------------------------------------------------------------
# Test 24 — STRICT mode: augmented assignment inside for-loop
# ---------------------------------------------------------------------------


def test_strict_augassign_inside_for():
    """STRICT mode augassign inside a for loop carries both self and iter deps."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("acc = 0")
    interp.exec("nums = [1, 2, 3]")
    interp.exec("""
for n in nums:
    acc += n
""")
    # acc += n  →  acc depends on {acc, n} | ctx (nums from for-iterable)
    direct = _deps(interp, "acc")
    assert "acc" in direct  # self-dep from augassign
    assert "n" in direct
    assert "nums" in direct  # from STRICT for-iterable ctx


# ---------------------------------------------------------------------------
# Test 25 — STRICT mode: for-inside-if (for loop nested inside if block)
# ---------------------------------------------------------------------------


def test_strict_for_inside_if():
    """STRICT mode: for loop inside an if block — body vars carry if-cond dep."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("flag = 1")
    interp.exec("items = [1, 2]")
    interp.exec("val = 99")
    interp.exec("""
if flag:
    for item in items:
        inner = val
""")
    direct = _deps(interp, "inner")
    assert "val" in direct
    assert "flag" in direct  # from the outer if-test in STRICT mode
    assert "items" in direct  # from the for-iterable in STRICT mode


# ---------------------------------------------------------------------------
# Test 26 — session persistence: deps accumulate across multiple exec() calls
# ---------------------------------------------------------------------------


def test_session_persistence_across_exec_calls():
    """Dep graph grows monotonically across separate exec() invocations."""
    interp = _interp()
    interp.exec("a = 1")
    interp.exec("b = a")
    # Session must retain 'a' dep after a fresh exec() call
    assert _deps(interp, "b") == frozenset({"a"})
    interp.exec("c = b")
    # Transitive: c → b → a, all from three separate exec() calls
    assert _deps(interp, "c") == frozenset({"b"})
    assert _upstream(interp, "c") == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# Test 27 — list construction: list literal captures all element var deps
# ---------------------------------------------------------------------------


def test_list_construction_deps():
    """lst = [a, b, c]  →  lst depends on {a, b, c}."""
    interp = _interp()
    interp.exec("a = 1")
    interp.exec("b = 2")
    interp.exec("c = 3")
    interp.exec("lst = [a, b, c]")

    assert _deps(interp, "lst") == frozenset({"a", "b", "c"})
    assert _upstream(interp, "lst") == frozenset({"a", "b", "c"})


# ---------------------------------------------------------------------------
# Test 28 — dict construction: dict literal captures all key/value var deps
# ---------------------------------------------------------------------------


def test_dict_construction_deps():
    """d = {k: v}  →  d depends on {k, v}."""
    interp = _interp()
    interp.exec("k = 1")
    interp.exec("v = 2")
    interp.exec("d = {k: v}")

    assert "k" in _deps(interp, "d")
    assert "v" in _deps(interp, "d")


# ---------------------------------------------------------------------------
# Test 29 — STRICT mode: realistic multi-step program
#           assignments + conditionals + loops combined
# ---------------------------------------------------------------------------


def test_strict_realistic_multi_step():
    """Realistic STRICT program: conditional flag controls loop that builds a result."""
    interp = _interp(mode=ExecutionMode.STRICT, get_items=_tool([10, 20, 30]))
    interp.exec("items = get_items()")
    interp.exec("threshold = 15")
    interp.exec("enabled = 1")
    interp.exec("total = 0")
    interp.exec("""
if enabled:
    for item in items:
        total += item
""")
    # 'total' inside the for body (via augassign) should carry:
    # - self dep (total)
    # - item (loop var)
    # - items (for-iterable ctx, STRICT)
    # - enabled (if-condition ctx, STRICT)
    direct = _deps(interp, "total")
    assert "total" in direct  # self-dep from augassign
    assert "item" in direct
    assert "items" in direct  # STRICT for-iterable
    assert "enabled" in direct  # STRICT if-condition


# ---------------------------------------------------------------------------
# Test 30 — STRICT mode: elif body also carries test variable deps
# ---------------------------------------------------------------------------


def test_strict_elif_body_carries_test_deps():
    """In STRICT mode, elif-branch assignment carries the test dep."""
    interp = _interp(mode=ExecutionMode.STRICT)
    interp.exec("flag = 0")
    interp.exec("other = 1")
    interp.exec("a = 5")
    interp.exec("b = 6")
    interp.exec("c = 7")
    interp.exec("""
if flag:
    x = a
elif other:
    x = b
else:
    x = c
""")
    # elif branch executes; x = b; but both flag and other are test deps
    direct = _deps(interp, "x")
    assert "flag" in direct or "other" in direct  # at least one test dep
    assert "b" in direct or "c" in direct  # at least one branch RHS dep


# ---------------------------------------------------------------------------
# Test 31 — get_dependency_graph on a variable with no variable dependencies
# ---------------------------------------------------------------------------


def test_get_dependency_graph_no_var_deps():
    """Variable assigned from a literal has an empty DependencyGraph."""
    interp = _interp()
    interp.exec("x = 42")

    dg = interp.get_dependency_graph("x")
    assert isinstance(dg, DependencyGraph)
    assert dg.variable == "x"
    assert dg.direct_deps == frozenset()
    assert dg.all_upstream == frozenset()
    assert dg.edges == frozenset()


# ---------------------------------------------------------------------------
# M4-F3/F4 Tests — post-Q-LLM-call STRICT mode tainting
# ---------------------------------------------------------------------------


def _qllm_tool(raw_value, sources=None):
    """Tool that registers as a query_quarantined_llm entry point."""

    def _fn(*args, **kwargs):
        return wrap(
            raw_value,
            sources=frozenset(sources) if sources else frozenset({"qllm"}),
        )

    return _fn


# ---------------------------------------------------------------------------
# Test 32 — M4-F3/F4 STRICT mode: post-Q-LLM-call propagates to next stmts
# ---------------------------------------------------------------------------


def test_strict_post_qllm_taints_subsequent_statements():
    """STRICT mode: statements after query_quarantined_llm() carry its deps.

    After a Q-LLM call, all subsequent statements in the same block must
    inherit the Q-LLM result's capability envelope (M4-F3).  The dependency
    graph must also record the Q-LLM variable as a dep on those statements
    (M4-F4).
    """
    interp = CaMeLInterpreter(
        tools={"query_quarantined_llm": _qllm_tool("extracted", sources=["qllm"])},
        mode=ExecutionMode.STRICT,
    )
    interp.exec("raw_data = 1")
    interp.exec("""
result = query_quarantined_llm(raw_data)
after = 42
""")
    # 'after' is a constant, but it was assigned AFTER the Q-LLM call in the
    # same block, so its dependency graph must include 'result' (the Q-LLM var).
    direct = _deps(interp, "after")
    assert "result" in direct


# ---------------------------------------------------------------------------
# Test 33 — M4-F3/F4 STRICT mode: Q-LLM tainting scoped to current block
# ---------------------------------------------------------------------------


def test_strict_post_qllm_taint_scoped_to_block():
    """STRICT mode: Q-LLM taint does NOT leak into sibling or parent blocks.

    Statements in a separate exec() call (a different top-level block) must
    NOT inherit the Q-LLM taint from a previous block.
    """
    interp = CaMeLInterpreter(
        tools={"query_quarantined_llm": _qllm_tool("data", sources=["qllm"])},
        mode=ExecutionMode.STRICT,
    )
    interp.exec("""
result = query_quarantined_llm()
""")
    # Fresh exec() call: a separate block; 'other' must not depend on 'result'
    interp.exec("other = 99")
    direct = _deps(interp, "other")
    assert "result" not in direct


# ---------------------------------------------------------------------------
# Test 34 — M4-F3/F4 STRICT mode: multiple Q-LLM calls accumulate deps
# ---------------------------------------------------------------------------


def test_strict_multiple_qllm_calls_accumulate():
    """STRICT mode: multiple Q-LLM calls in same block accumulate dep context.

    After two Q-LLM calls, a subsequent statement must depend on both Q-LLM
    result variables.
    """
    interp = CaMeLInterpreter(
        tools={"query_quarantined_llm": _qllm_tool("x", sources=["qllm"])},
        mode=ExecutionMode.STRICT,
    )
    interp.exec("""
first = query_quarantined_llm()
second = query_quarantined_llm()
after = 0
""")
    direct = _deps(interp, "after")
    # 'after' must depend on both 'first' and 'second' (accumulated Q-LLM ctx)
    assert "first" in direct
    assert "second" in direct


# ---------------------------------------------------------------------------
# Test 35 — M4-F3/F4 NORMAL mode: Q-LLM calls do NOT taint subsequent stmts
# ---------------------------------------------------------------------------


def test_normal_qllm_does_not_taint_subsequent_statements():
    """NORMAL mode: query_quarantined_llm() does NOT taint later statements.

    The M4-F3/F4 tainting mechanism is STRICT-mode-only.  In NORMAL mode,
    statements after a Q-LLM call depend only on their own RHS variables.
    """
    interp = CaMeLInterpreter(
        tools={"query_quarantined_llm": _qllm_tool("x", sources=["qllm"])},
        mode=ExecutionMode.NORMAL,
    )
    interp.exec("""
result = query_quarantined_llm()
after = 42
""")
    # NORMAL mode: 'after' is a constant with no variable deps.
    direct = _deps(interp, "after")
    assert "result" not in direct
    assert direct == frozenset()
