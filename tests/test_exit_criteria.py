"""Milestone 1 exit criteria validation suite.

Criteria validated
------------------
EC-1  100% unit test pass rate — subprocess pytest invocation over all Milestone 1 test files.
EC-2  ≥20 dependency-graph programs verified — collection count + full run of
      test_dependency_graph.py.
EC-3  CaMeLValue round-trip fidelity — wrap, propagate through all six operation classes, assert
      unwrapped result and capability union.
EC-4  ≥15 negative syntax tests — UnsupportedSyntaxError raised with correct node_type and lineno.
EC-5  Session persistence — 3 sequential exec() calls, variable state accumulates correctly.
EC-6  STRICT mode loop-dependency regression — loop-body variable's dependency set includes the
      iterable source variable.

See also: docs/exit_criteria_checklist.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from camel.dependency_graph import DependencyGraph
from camel.exceptions import ForbiddenImportError
from camel.interpreter import CaMeLInterpreter, ExecutionMode, UnsupportedSyntaxError
from camel.value import (
    CaMeLValue,
    Public,
    propagate_assignment,
    propagate_binary_op,
    propagate_dict_construction,
    propagate_list_construction,
    propagate_subscript,
    wrap,
)

# Project root — used as cwd for subprocess pytest calls so that paths resolve correctly.
_PROJECT_ROOT = Path(__file__).parent.parent


# ============================================================================
# EC-1 — 100% unit test pass rate
# ============================================================================


def test_ec1_full_milestone1_test_suite_passes():
    """All Milestone 1 unit tests pass with zero failures.

    Runs pytest over test_value.py, test_interpreter.py, and
    test_dependency_graph.py and asserts a zero exit-code.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-q",
            "tests/test_value.py",
            "tests/test_interpreter.py",
            "tests/test_dependency_graph.py",
        ],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"Milestone 1 test suite contains failures.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


# ============================================================================
# EC-2 — ≥20 dependency-graph programs verified
# ============================================================================


def test_ec2_dependency_graph_program_count_at_least_20():
    """test_dependency_graph.py contains ≥20 test items (programs).

    Uses pytest --collect-only to count collected test items without running
    them; asserts the total is ≥20.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_dependency_graph.py",
        ],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
    )
    # Each collected test item appears as a "path::test_name" line in -q output.
    test_lines = [
        line
        for line in result.stdout.splitlines()
        if "::" in line and not line.startswith("=") and not line.startswith(" ")
    ]
    assert len(test_lines) >= 20, (
        f"Expected ≥20 dependency-graph programs; found {len(test_lines)}.\n"
        f"Collected:\n" + "\n".join(test_lines)
    )


def test_ec2_dependency_graph_programs_all_pass():
    """All ≥20 dependency-graph programs pass with zero failures."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-q",
            "tests/test_dependency_graph.py",
        ],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"Dependency-graph tests contain failures.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


# ============================================================================
# EC-3 — CaMeLValue round-trip fidelity
# ============================================================================


class TestEC3CaMeLValueRoundTrip:
    """Round-trip: wrap a value, propagate through an operation, assert result + caps."""

    # -- EC-3a: arithmetic ---------------------------------------------------

    def test_ec3a_arithmetic(self):
        """Arithmetic binary op: value and capability union both correct."""
        a = wrap(10, sources=frozenset({"src_a"}), readers=frozenset({"alice@x.com"}))
        b = wrap(3, sources=frozenset({"src_b"}), readers=frozenset({"bob@x.com"}))
        result = propagate_binary_op(a, b, a.raw + b.raw)

        assert result.raw == 13
        assert result.sources == frozenset({"src_a", "src_b"})
        assert isinstance(result.readers, frozenset)
        assert "alice@x.com" in result.readers  # type: ignore[operator]
        assert "bob@x.com" in result.readers  # type: ignore[operator]
        assert result.inner_source is None

    # -- EC-3b: string formatting (f-string via interpreter) -----------------

    def test_ec3b_string_format(self):
        """F-string formatting: value correct, capabilities union from parts."""
        interp = CaMeLInterpreter()
        interp.exec('first = "hello"')
        interp.exec('second = "world"')
        interp.exec('msg = f"{first} {second}"')

        cv = interp.get("msg")
        assert cv.raw == "hello world"
        assert "User literal" in cv.sources
        assert cv.readers is Public

    # -- EC-3c: list access --------------------------------------------------

    def test_ec3c_list_access(self):
        """Subscript on a list: correct element and union of container + key caps."""
        container = wrap(
            [10, 20, 30],
            sources=frozenset({"list_tool"}),
            readers=frozenset({"alice@x.com"}),
        )
        key = wrap(1, sources=frozenset({"User literal"}), readers=Public)

        result = propagate_subscript(container, key, container.raw[key.raw])

        assert result.raw == 20
        assert "list_tool" in result.sources
        assert "User literal" in result.sources
        # Public is absorbing — readers should be Public because key.readers is Public
        assert result.readers is Public
        assert result.inner_source is None

    # -- EC-3d: dict access --------------------------------------------------

    def test_ec3d_dict_access(self):
        """Subscript on a dict: correct value and capability union."""
        container = wrap(
            {"greeting": "hi"},
            sources=frozenset({"dict_tool"}),
            readers=frozenset({"user@x.com"}),
        )
        key = wrap("greeting", sources=frozenset({"User literal"}), readers=Public)

        result = propagate_subscript(container, key, container.raw[key.raw])

        assert result.raw == "hi"
        assert "dict_tool" in result.sources
        assert "User literal" in result.sources
        assert result.readers is Public

    # -- EC-3e: conditional branch (via interpreter) -------------------------

    def test_ec3e_conditional_branch(self):
        """If-branch: correct value selected, capabilities from branch RHS."""
        interp = CaMeLInterpreter()
        interp.exec("flag = 1")
        interp.exec("""
if flag:
    result = 100
else:
    result = 0
""")
        cv = interp.get("result")
        assert cv.raw == 100
        assert "User literal" in cv.sources

    def test_ec3e_conditional_branch_else_taken(self):
        """Else-branch: correct alternative value selected."""
        interp = CaMeLInterpreter()
        interp.exec("flag = 0")
        interp.exec("""
if flag:
    result = 100
else:
    result = 0
""")
        cv = interp.get("result")
        assert cv.raw == 0
        assert "User literal" in cv.sources

    # -- EC-3f: for-loop (via interpreter) -----------------------------------

    def test_ec3f_for_loop(self):
        """For-loop accumulation: final value correct, sources propagated."""
        interp = CaMeLInterpreter()
        interp.exec("total = 0")
        interp.exec("nums = [1, 2, 3, 4]")
        interp.exec("""
for n in nums:
    total += n
""")
        cv = interp.get("total")
        assert cv.raw == 10
        assert "User literal" in cv.sources

    # -- EC-3g: list construction propagation --------------------------------

    def test_ec3g_list_construction(self):
        """List literal: capability union across all element CaMeLValues."""
        e0 = wrap(1, sources=frozenset({"s0"}), readers=frozenset({"a@x.com"}))
        e1 = wrap(2, sources=frozenset({"s1"}), readers=frozenset({"b@x.com"}))
        e2 = wrap(3, sources=frozenset({"s2"}), readers=Public)

        result = propagate_list_construction([e0, e1, e2], [e0.raw, e1.raw, e2.raw])

        assert result.raw == [1, 2, 3]
        assert result.sources == frozenset({"s0", "s1", "s2"})
        assert result.readers is Public  # Public absorbs

    # -- EC-3h: dict construction propagation --------------------------------

    def test_ec3h_dict_construction(self):
        """Dict literal: capability union across all key and value CaMeLValues."""
        k0 = wrap("k", sources=frozenset({"ksrc"}), readers=frozenset({"k@x.com"}))
        v0 = wrap(42, sources=frozenset({"vsrc"}), readers=frozenset({"v@x.com"}))

        result = propagate_dict_construction([k0], [v0], {k0.raw: v0.raw})

        assert result.raw == {"k": 42}
        assert result.sources == frozenset({"ksrc", "vsrc"})
        assert isinstance(result.readers, frozenset)
        assert "k@x.com" in result.readers  # type: ignore[operator]
        assert "v@x.com" in result.readers  # type: ignore[operator]

    # -- EC-3i: assignment propagation ---------------------------------------

    def test_ec3i_assignment_propagation(self):
        """propagate_assignment: sources/readers preserved, inner_source cleared."""
        original = CaMeLValue(
            value="secret",
            sources=frozenset({"email_tool"}),
            inner_source="body",
            readers=frozenset({"alice@x.com"}),
        )
        derived = propagate_assignment(original, original.raw)

        assert derived.raw == "secret"
        assert derived.sources == frozenset({"email_tool"})
        assert derived.inner_source is None  # cleared by propagate_assignment
        assert derived.readers == frozenset({"alice@x.com"})


# ============================================================================
# EC-4 — ≥15 negative syntax tests (UnsupportedSyntaxError)
# ============================================================================


@pytest.mark.parametrize(
    "code, expected_node_type",
    [
        # ---------- unsupported statements ----------
        ("while True: pass", "While"),
        ("class Foo: pass", "ClassDef"),
        ("def foo(): pass", "FunctionDef"),
        ("del x", "Delete"),
        ("raise ValueError()", "Raise"),
        ("assert True", "Assert"),
        ("try:\n    pass\nexcept Exception:\n    pass", "Try"),
        ("with open('f') as fh:\n    pass", "With"),
        # ---------- unsupported expressions ----------
        ("x = lambda: 0", "Lambda"),
        ("x = [i for i in []]", "ListComp"),
        ("x = {k: v for k, v in []}", "DictComp"),
        ("x = {i for i in []}", "SetComp"),
        ("x = (i for i in [])", "GeneratorExp"),
        ("x = {1, 2}", "Set"),
        ("x = 1 if True else 2", "IfExp"),
    ],
    ids=[
        "while",
        "class_def",
        "function_def",
        "delete",
        "raise",
        "assert",
        "try_except",
        "with",
        "lambda",
        "list_comp",
        "dict_comp",
        "set_comp",
        "generator_exp",
        "set_literal",
        "if_expr",
    ],
)
def test_ec4_unsupported_syntax_raises_correct_error(
    code: str, expected_node_type: str
) -> None:
    """UnsupportedSyntaxError is raised with correct node_type and non-zero lineno."""
    interp = CaMeLInterpreter()
    with pytest.raises(UnsupportedSyntaxError) as exc_info:
        interp.exec(code)
    exc = exc_info.value
    assert exc.node_type == expected_node_type, (
        f"Expected node_type={expected_node_type!r}, got {exc.node_type!r}"
    )
    assert exc.lineno >= 1, f"Expected lineno ≥ 1, got {exc.lineno}"


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "from os import path",
    ],
    ids=["import", "import_from"],
)
def test_ec4_import_raises_forbidden_import_error(code: str) -> None:
    """Import statements raise ForbiddenImportError (M4-F10), not UnsupportedSyntaxError."""
    interp = CaMeLInterpreter()
    with pytest.raises(ForbiddenImportError):
        interp.exec(code)


# ============================================================================
# EC-5 — Session persistence across 3 sequential exec() calls
# ============================================================================


def test_ec5_session_persistence_three_sequential_execs():
    """Variable state accumulates correctly across exactly 3 sequential exec() calls.

    Exec 1: assigns ``a``.
    Exec 2: references ``a`` to compute ``b`` — confirms ``a`` is still in store.
    Exec 3: references both ``a`` and ``b`` to compute ``c`` — confirms both persist.
    """
    interp = CaMeLInterpreter()

    # --- exec 1 ---
    interp.exec("a = 10")
    assert interp.get("a").raw == 10, "After exec 1: a should be 10"

    # --- exec 2 ---
    interp.exec("b = a + 5")
    assert interp.get("a").raw == 10, "After exec 2: a must still be 10"
    assert interp.get("b").raw == 15, "After exec 2: b = a + 5 = 15"

    # --- exec 3 ---
    interp.exec("c = a + b")
    assert interp.get("a").raw == 10, "After exec 3: a must still be 10"
    assert interp.get("b").raw == 15, "After exec 3: b must still be 15"
    assert interp.get("c").raw == 25, "After exec 3: c = a + b = 25"


def test_ec5_session_persistence_capability_metadata_carries_forward():
    """Capability metadata set in one exec() is accessible in later exec() calls."""
    from camel.value import wrap

    tool_value = wrap(
        "data",
        sources=frozenset({"external_tool"}),
        readers=frozenset({"admin@x.com"}),
    )

    def external_tool() -> CaMeLValue:
        return tool_value

    interp = CaMeLInterpreter(tools={"external_tool": external_tool})

    interp.exec("result = external_tool()")
    interp.exec("copy = result")
    interp.exec("processed = copy")

    cv = interp.get("processed")
    assert cv.raw == "data"
    assert "external_tool" in cv.sources
    assert isinstance(cv.readers, frozenset)
    assert "admin@x.com" in cv.readers  # type: ignore[operator]


# ============================================================================
# EC-6 — STRICT mode loop-dependency regression
# ============================================================================


def test_ec6_strict_loop_body_var_depends_on_iterable():
    """STRICT mode: loop-body variable's dependency set includes the iterable source.

    This is the primary regression guard for the STRICT mode side-channel
    mitigation: an adversary must not be able to infer the contents of an
    iterable by observing which downstream tool calls are made, because the
    policy engine sees the iterable as an upstream dependency of every
    loop-body assignment.
    """
    interp = CaMeLInterpreter(mode=ExecutionMode.STRICT)
    interp.exec("items = [1, 2, 3]")
    interp.exec("""
for item in items:
    result = item
""")

    dg = interp.get_dependency_graph("result")
    assert isinstance(dg, DependencyGraph)
    assert "items" in dg.direct_deps, (
        "STRICT regression: 'items' (iterable) must be in result's direct_deps"
    )
    assert "items" in dg.all_upstream, (
        "STRICT regression: 'items' must also appear in all_upstream"
    )


def test_ec6_strict_loop_body_var_depends_on_iterable_with_accumulator():
    """STRICT mode: accumulator inside a loop carries the iterable as a dependency."""
    interp = CaMeLInterpreter(mode=ExecutionMode.STRICT)
    interp.exec("nums = [10, 20, 30]")
    interp.exec("acc = 0")
    interp.exec("""
for n in nums:
    acc += n
""")

    dg = interp.get_dependency_graph("acc")
    direct = dg.direct_deps
    assert "nums" in direct, (
        "STRICT regression: accumulator 'acc' must carry iterable 'nums' in direct_deps"
    )
    assert "n" in direct, "acc depends on loop variable 'n'"
    assert "acc" in direct, "augmented-assignment creates a self-dep on 'acc'"


def test_ec6_strict_loop_body_carries_iterable_not_just_loop_var():
    """STRICT mode: the iterable variable itself (not just the loop target) is a dep.

    In NORMAL mode, loop-body assignments do NOT carry the iterable as a direct dep
    (only the loop target does).  This test confirms STRICT mode adds the iterable
    to every body assignment.
    """
    interp_normal = CaMeLInterpreter(mode=ExecutionMode.NORMAL)
    interp_normal.exec("data = [5, 6, 7]")
    interp_normal.exec("""
for val in data:
    output = val
""")
    normal_direct = interp_normal.get_dependency_graph("output").direct_deps
    # NORMAL: output depends on val, but NOT directly on data
    assert "val" in normal_direct
    assert "data" not in normal_direct, (
        "NORMAL mode: 'data' should NOT be a direct dep of 'output'"
    )

    interp_strict = CaMeLInterpreter(mode=ExecutionMode.STRICT)
    interp_strict.exec("data = [5, 6, 7]")
    interp_strict.exec("""
for val in data:
    output = val
""")
    strict_direct = interp_strict.get_dependency_graph("output").direct_deps
    # STRICT: output must also depend on data
    assert "val" in strict_direct
    assert "data" in strict_direct, (
        "STRICT mode: 'data' (iterable) MUST be a direct dep of 'output'"
    )
