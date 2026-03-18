"""Unit tests for CaMeLInterpreter.

Coverage
--------
- All supported statement types (Assign, AugAssign, If, For, Expr)
- All supported expression types (Constant, Name, BinOp, UnaryOp, BoolOp,
  Compare, Call, Attribute, Subscript, List, Tuple, Dict, JoinedStr)
- UnsupportedSyntaxError for ≥ 15 unsupported constructs
- Session-state persistence across ≥ 3 sequential exec() calls
- Capability propagation through operations
- Tool call dispatch and TypeError on bad return value
- Policy engine integration (allow and deny paths)
- STRICT mode capability merging
- seed() and store property
"""

from __future__ import annotations

import pytest

from camel.exceptions import ForbiddenImportError
from camel.interpreter import (
    CaMeLInterpreter,
    ExecutionMode,
    PolicyViolationError,
    UnsupportedSyntaxError,
)
from camel.value import CaMeLValue, Public, wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interp(**tools):
    return CaMeLInterpreter(tools=tools or None)


def _tool(raw_value, sources=None, readers=None):
    """Return a zero-arg tool that produces a CaMeLValue."""

    def _fn():
        return wrap(
            raw_value,
            sources=frozenset(sources) if sources else frozenset({"test_tool"}),
            readers=frozenset(readers) if readers else Public,
        )

    return _fn


def _tool1(raw_value, sources=None, readers=None):
    """Return a one-arg tool."""

    def _fn(x):
        return wrap(
            raw_value,
            sources=frozenset(sources) if sources else frozenset({"test_tool"}),
            readers=frozenset(readers) if readers else Public,
        )

    return _fn


# ---------------------------------------------------------------------------
# Constants and literals
# ---------------------------------------------------------------------------


class TestConstants:
    def test_int_literal(self):
        i = _interp()
        i.exec("x = 42")
        cv = i.get("x")
        assert cv.raw == 42
        assert isinstance(cv, CaMeLValue)
        assert cv.sources == frozenset({"User literal"})
        assert cv.readers is Public

    def test_float_literal(self):
        i = _interp()
        i.exec("x = 3.14")
        assert i.get("x").raw == pytest.approx(3.14)

    def test_string_literal(self):
        i = _interp()
        i.exec('x = "hello"')
        assert i.get("x").raw == "hello"

    def test_bool_literal(self):
        i = _interp()
        i.exec("x = True")
        assert i.get("x").raw is True

    def test_none_literal(self):
        i = _interp()
        i.exec("x = None")
        assert i.get("x").raw is None

    def test_negative_int(self):
        i = _interp()
        i.exec("x = -7")
        assert i.get("x").raw == -7


# ---------------------------------------------------------------------------
# Arithmetic BinOp
# ---------------------------------------------------------------------------


class TestBinOp:
    def test_add(self):
        i = _interp()
        i.exec("x = 3 + 4")
        assert i.get("x").raw == 7

    def test_sub(self):
        i = _interp()
        i.exec("x = 10 - 3")
        assert i.get("x").raw == 7

    def test_mul(self):
        i = _interp()
        i.exec("x = 3 * 4")
        assert i.get("x").raw == 12

    def test_div(self):
        i = _interp()
        i.exec("x = 10 / 4")
        assert i.get("x").raw == pytest.approx(2.5)

    def test_floordiv(self):
        i = _interp()
        i.exec("x = 10 // 3")
        assert i.get("x").raw == 3

    def test_mod(self):
        i = _interp()
        i.exec("x = 10 % 3")
        assert i.get("x").raw == 1

    def test_pow(self):
        i = _interp()
        i.exec("x = 2 ** 10")
        assert i.get("x").raw == 1024

    def test_string_concat(self):
        i = _interp()
        i.exec('x = "hello" + " " + "world"')
        assert i.get("x").raw == "hello world"

    def test_capability_union(self):
        i = _interp()
        i.exec("a = 1")
        i.exec("b = 2")
        i.exec("c = a + b")
        # constants → User literal on both sides; union is still User literal
        assert "User literal" in i.get("c").sources
        assert i.get("c").raw == 3


# ---------------------------------------------------------------------------
# UnaryOp
# ---------------------------------------------------------------------------


class TestUnaryOp:
    def test_usub(self):
        i = _interp()
        i.exec("x = -5")
        assert i.get("x").raw == -5

    def test_not_true(self):
        i = _interp()
        i.exec("x = not True")
        assert i.get("x").raw is False

    def test_not_false(self):
        i = _interp()
        i.exec("x = not False")
        assert i.get("x").raw is True

    def test_invert(self):
        i = _interp()
        i.exec("x = ~3")
        assert i.get("x").raw == ~3

    def test_caps_preserved(self):
        def source_tool():
            return wrap("val", sources=frozenset({"src"}), readers=frozenset({"r"}))

        i = CaMeLInterpreter(tools={"source_tool": source_tool})
        i.exec("v = source_tool()")
        i.exec("n = not v")
        assert "src" in i.get("n").sources
        assert i.get("n").readers == frozenset({"r"})


# ---------------------------------------------------------------------------
# BoolOp
# ---------------------------------------------------------------------------


class TestBoolOp:
    def test_and_true(self):
        i = _interp()
        i.exec("x = True and True")
        assert i.get("x").raw is True

    def test_and_false(self):
        i = _interp()
        i.exec("x = True and False")
        assert i.get("x").raw is False

    def test_or_true(self):
        i = _interp()
        i.exec("x = False or True")
        assert i.get("x").raw is True

    def test_or_false(self):
        i = _interp()
        i.exec("x = False or False")
        assert i.get("x").raw is False

    def test_and_short_circuit_caps_still_union(self):
        """Even the short-circuited operand contributes capabilities."""

        def tool_a():
            return wrap(False, sources=frozenset({"tool_a"}))

        def tool_b():
            return wrap(True, sources=frozenset({"tool_b"}))

        i = CaMeLInterpreter(tools={"tool_a": tool_a, "tool_b": tool_b})
        i.exec("x = tool_a() and tool_b()")
        # tool_b() is short-circuited but its caps must still appear
        assert "tool_a" in i.get("x").sources
        assert "tool_b" in i.get("x").sources

    def test_or_three_operands(self):
        i = _interp()
        i.exec("x = False or False or 42")
        assert i.get("x").raw == 42


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


class TestCompare:
    def test_eq(self):
        i = _interp()
        i.exec("x = 1 == 1")
        assert i.get("x").raw is True

    def test_noteq(self):
        i = _interp()
        i.exec("x = 1 != 2")
        assert i.get("x").raw is True

    def test_lt(self):
        i = _interp()
        i.exec("x = 3 < 5")
        assert i.get("x").raw is True

    def test_gt(self):
        i = _interp()
        i.exec("x = 5 > 3")
        assert i.get("x").raw is True

    def test_lte(self):
        i = _interp()
        i.exec("x = 3 <= 3")
        assert i.get("x").raw is True

    def test_gte(self):
        i = _interp()
        i.exec("x = 4 >= 3")
        assert i.get("x").raw is True

    def test_in(self):
        i = _interp()
        i.exec("x = 2 in [1, 2, 3]")
        assert i.get("x").raw is True

    def test_not_in(self):
        i = _interp()
        i.exec("x = 9 not in [1, 2, 3]")
        assert i.get("x").raw is True

    def test_chained(self):
        i = _interp()
        i.exec("x = 1 < 2 < 3")
        assert i.get("x").raw is True

    def test_chained_false(self):
        i = _interp()
        i.exec("x = 1 < 2 < 1")
        assert i.get("x").raw is False


# ---------------------------------------------------------------------------
# List / Tuple / Dict literals
# ---------------------------------------------------------------------------


class TestCollectionLiterals:
    def test_list_literal(self):
        i = _interp()
        i.exec("x = [1, 2, 3]")
        assert i.get("x").raw == [1, 2, 3]

    def test_tuple_literal(self):
        i = _interp()
        i.exec("x = 1, 2, 3")
        assert list(i.get("x").raw) == [1, 2, 3]

    def test_dict_literal(self):
        i = _interp()
        i.exec('x = {"a": 1, "b": 2}')
        assert i.get("x").raw == {"a": 1, "b": 2}

    def test_empty_list(self):
        i = _interp()
        i.exec("x = []")
        assert i.get("x").raw == []

    def test_empty_dict(self):
        i = _interp()
        i.exec("x = {}")
        assert i.get("x").raw == {}

    def test_nested_list(self):
        i = _interp()
        i.exec("x = [[1, 2], [3, 4]]")
        assert i.get("x").raw == [[1, 2], [3, 4]]

    def test_dict_star_star_unsupported(self):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("d = {**{}}")


# ---------------------------------------------------------------------------
# Subscript
# ---------------------------------------------------------------------------


class TestSubscript:
    def test_list_index(self):
        i = _interp()
        i.exec("lst = [10, 20, 30]")
        i.exec("x = lst[1]")
        assert i.get("x").raw == 20

    def test_dict_key(self):
        i = _interp()
        i.exec('d = {"key": 99}')
        i.exec('x = d["key"]')
        assert i.get("x").raw == 99

    def test_caps_union(self):
        def get_list():
            return wrap([1, 2, 3], sources=frozenset({"list_src"}))

        i = CaMeLInterpreter(tools={"get_list": get_list})
        i.exec("lst = get_list()")
        i.exec("x = lst[0]")
        assert "list_src" in i.get("x").sources

    def test_slice_unsupported(self):
        i = _interp()
        i.exec("lst = [1, 2, 3]")
        with pytest.raises(UnsupportedSyntaxError) as exc_info:
            i.exec("x = lst[0:2]")
        assert exc_info.value.node_type == "Slice"


# ---------------------------------------------------------------------------
# Attribute access
# ---------------------------------------------------------------------------


class TestAttribute:
    def test_string_upper(self):
        i = _interp()
        i.exec('x = "hello"')
        i.exec("y = x.upper()")
        assert i.get("y").raw == "HELLO"

    def test_method_call_via_attribute(self):
        i = _interp()
        i.exec('x = "hello world"')
        i.exec("y = x.split()")
        assert i.get("y").raw == ["hello", "world"]


# ---------------------------------------------------------------------------
# Assignment (simple and tuple unpacking)
# ---------------------------------------------------------------------------


class TestAssign:
    def test_simple(self):
        i = _interp()
        i.exec("x = 7")
        assert i.get("x").raw == 7

    def test_tuple_unpack_two(self):
        i = _interp()
        i.exec("a, b = 1, 2")
        assert i.get("a").raw == 1
        assert i.get("b").raw == 2

    def test_tuple_unpack_three(self):
        i = _interp()
        i.exec("a, b, c = [10, 20, 30]")
        assert i.get("a").raw == 10
        assert i.get("b").raw == 20
        assert i.get("c").raw == 30

    def test_multi_target(self):
        # a = b = expr
        i = _interp()
        i.exec("a = b = 5")
        assert i.get("a").raw == 5
        assert i.get("b").raw == 5

    def test_nested_unpack_unsupported(self):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("(a, (b, c)) = (1, (2, 3))")

    def test_subscript_target_unsupported(self):
        i = _interp()
        i.exec("d = {}")
        with pytest.raises(UnsupportedSyntaxError):
            i.exec('d["k"] = 1')

    def test_attribute_target_unsupported(self):
        # Use a tool that returns an object so we can attempt obj.attr = val
        # without triggering ForbiddenImportError first.
        import types

        def _obj_tool() -> CaMeLValue:
            return wrap(
                types.SimpleNamespace(name="original"),
                sources=frozenset({"CaMeL"}),
            )

        i = CaMeLInterpreter(tools={"get_obj": _obj_tool})
        i.exec("obj = get_obj()")
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("obj.name = 'x'")


# ---------------------------------------------------------------------------
# AugAssign
# ---------------------------------------------------------------------------


class TestAugAssign:
    def test_iadd(self):
        i = _interp()
        i.exec("x = 3")
        i.exec("x += 2")
        assert i.get("x").raw == 5

    def test_isub(self):
        i = _interp()
        i.exec("x = 10")
        i.exec("x -= 4")
        assert i.get("x").raw == 6

    def test_imul(self):
        i = _interp()
        i.exec("x = 3")
        i.exec("x *= 3")
        assert i.get("x").raw == 9

    def test_augassign_caps_propagate(self):
        def src_tool():
            return wrap(10, sources=frozenset({"src"}))

        i = CaMeLInterpreter(tools={"src_tool": src_tool})
        i.exec("x = src_tool()")
        i.exec("x += 5")
        assert "src" in i.get("x").sources
        assert i.get("x").raw == 15

    def test_augassign_undefined_raises(self):
        i = _interp()
        with pytest.raises(NameError):
            i.exec("x += 1")

    def test_augassign_non_name_target_unsupported(self):
        i = _interp()
        i.exec("lst = [1, 2]")
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("lst[0] += 1")


# ---------------------------------------------------------------------------
# If / elif / else
# ---------------------------------------------------------------------------


class TestIf:
    def test_if_true_branch(self):
        i = _interp()
        i.exec("""
if True:
    x = 1
else:
    x = 2
""")
        assert i.get("x").raw == 1

    def test_if_false_branch(self):
        i = _interp()
        i.exec("""
if False:
    x = 1
else:
    x = 2
""")
        assert i.get("x").raw == 2

    def test_elif(self):
        i = _interp()
        i.exec("val = 5")
        i.exec("""
if val > 10:
    result = "big"
elif val > 3:
    result = "medium"
else:
    result = "small"
""")
        assert i.get("result").raw == "medium"

    def test_no_else(self):
        i = _interp()
        i.exec("x = 0")
        i.exec("""
if True:
    x = 99
""")
        assert i.get("x").raw == 99

    def test_nested_if(self):
        i = _interp()
        i.exec("""
x = 5
if x > 0:
    if x > 3:
        result = "large"
    else:
        result = "small"
else:
    result = "negative"
""")
        assert i.get("result").raw == "large"


# ---------------------------------------------------------------------------
# For loop
# ---------------------------------------------------------------------------


class TestFor:
    def test_basic_iteration(self):
        i = _interp()
        i.exec("""
total = 0
for n in [1, 2, 3, 4, 5]:
    total += n
""")
        assert i.get("total").raw == 15

    def test_loop_target_persists(self):
        i = _interp()
        i.exec("""
last = 0
for x in [10, 20, 30]:
    last = x
""")
        assert i.get("x").raw == 30
        assert i.get("last").raw == 30

    def test_tuple_unpack_in_for(self):
        i = _interp()
        i.exec("""
result = []
for a, b in [[1, 2], [3, 4]]:
    result += [a + b]
""")
        assert i.get("result").raw == [3, 7]

    def test_for_range(self):
        i = _interp()
        i.exec("""
s = 0
for i in range(5):
    s += i
""")
        assert i.get("s").raw == 10

    def test_for_else_unsupported(self):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("""
for x in [1]:
    x = x
else:
    done = True
""")

    def test_caps_from_iterable_propagate(self):
        def get_items():
            return wrap([1, 2, 3], sources=frozenset({"items_src"}))

        i = CaMeLInterpreter(tools={"get_items": get_items})
        i.exec("items = get_items()")
        i.exec("""
for item in items:
    last = item
""")
        assert "items_src" in i.get("last").sources


# ---------------------------------------------------------------------------
# Bare expression (Expr statement)
# ---------------------------------------------------------------------------


class TestExprStatement:
    def test_tool_call_side_effect_no_assignment(self):
        called = []

        def side_effect_tool():
            called.append(True)
            return wrap(None)

        i = CaMeLInterpreter(tools={"side_effect_tool": side_effect_tool})
        i.exec("side_effect_tool()")
        assert called == [True]


# ---------------------------------------------------------------------------
# JoinedStr (f-string)
# ---------------------------------------------------------------------------


class TestJoinedStr:
    def test_simple_fstring(self):
        i = _interp()
        i.exec('name = "Alice"')
        i.exec('msg = f"Hello, {name}!"')
        assert i.get("msg").raw == "Hello, Alice!"

    def test_fstring_with_expression(self):
        i = _interp()
        i.exec("n = 5")
        i.exec('msg = f"n squared is {n * n}"')
        assert i.get("msg").raw == "n squared is 25"

    def test_fstring_str_conversion(self):
        i = _interp()
        i.exec("x = 42")
        i.exec('msg = f"{x!s}"')
        assert i.get("msg").raw == "42"

    def test_fstring_repr_conversion(self):
        i = _interp()
        i.exec('x = "hello"')
        i.exec('msg = f"{x!r}"')
        assert i.get("msg").raw == repr("hello")

    def test_empty_fstring(self):
        i = _interp()
        i.exec('msg = f""')
        assert i.get("msg").raw == ""

    def test_fstring_caps_from_variable(self):
        def src():
            return wrap("Bob", sources=frozenset({"name_src"}))

        i = CaMeLInterpreter(tools={"src": src})
        i.exec("name = src()")
        i.exec('msg = f"Hello, {name}"')
        assert "name_src" in i.get("msg").sources

    def test_fstring_format_spec(self):
        i = _interp()
        i.exec("x = 3.14159")
        i.exec('msg = f"{x:.2f}"')
        assert i.get("msg").raw == "3.14"


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


class TestToolCalls:
    def test_tool_returns_camelvalue(self):
        def my_tool():
            return wrap("result", sources=frozenset({"my_tool"}))

        i = CaMeLInterpreter(tools={"my_tool": my_tool})
        i.exec("r = my_tool()")
        assert i.get("r").raw == "result"
        assert "my_tool" in i.get("r").sources

    def test_tool_with_positional_args(self):
        def add_tool(a, b):
            return wrap(a + b, sources=frozenset({"add_tool"}))

        i = CaMeLInterpreter(tools={"add_tool": add_tool})
        i.exec("r = add_tool(3, 4)")
        assert i.get("r").raw == 7

    def test_tool_with_keyword_args(self):
        def greet(name, greeting="Hello"):
            return wrap(f"{greeting}, {name}!", sources=frozenset({"greet"}))

        i = CaMeLInterpreter(tools={"greet": greet})
        i.exec('r = greet(name="Alice", greeting="Hi")')
        assert i.get("r").raw == "Hi, Alice!"

    def test_tool_wrong_return_type_raises(self):
        def bad_tool():
            return "raw string, not CaMeLValue"

        i = CaMeLInterpreter(tools={"bad_tool": bad_tool})
        with pytest.raises(TypeError, match="CaMeLValue"):
            i.exec("r = bad_tool()")

    def test_tool_starred_arg_unsupported(self):
        def my_tool(*args):
            return wrap(None)

        i = CaMeLInterpreter(tools={"my_tool": my_tool})
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("args = [1, 2]; my_tool(*args)")

    def test_tool_double_starred_kwarg_unsupported(self):
        def my_tool(**kwargs):
            return wrap(None)

        i = CaMeLInterpreter(tools={"my_tool": my_tool})
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("kw = {}; my_tool(**kw)")

    def test_builtin_call_len(self):
        i = _interp()
        i.exec("lst = [1, 2, 3]")
        i.exec("n = len(lst)")
        assert i.get("n").raw == 3

    def test_builtin_call_str(self):
        i = _interp()
        i.exec("n = 42")
        i.exec('s = str(n)')
        assert i.get("s").raw == "42"

    def test_builtin_caps_inherit_from_args(self):
        def num_src():
            return wrap(5, sources=frozenset({"num_src"}))

        i = CaMeLInterpreter(tools={"num_src": num_src})
        i.exec("x = num_src()")
        i.exec("y = str(x)")
        assert "num_src" in i.get("y").sources

    def test_builtin_no_args_public_readers(self):
        # A custom builtin called with no arguments → wrap(result) → readers=Public
        def zero_arg_fn():
            return 99

        i = CaMeLInterpreter(builtins={"zero_arg": zero_arg_fn})
        i.exec("x = zero_arg()")
        assert i.get("x").raw == 99
        assert i.get("x").readers is Public


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class _Allowed:
    reason = None

    def is_allowed(self) -> bool:
        return True


class _Denied:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def is_allowed(self) -> bool:
        return False


class _PolicyAllow:
    def evaluate(self, tool_name: str, kwargs: object) -> _Allowed:
        return _Allowed()


class _PolicyDeny:
    def __init__(self, reason: str = "not allowed") -> None:
        self._reason = reason

    def evaluate(self, tool_name: str, kwargs: object) -> _Denied:
        return _Denied(self._reason)


class TestPolicyEngine:
    def test_allowed_tool_executes(self):
        def my_tool():
            return wrap("ok", sources=frozenset({"my_tool"}))

        i = CaMeLInterpreter(tools={"my_tool": my_tool}, policy_engine=_PolicyAllow())
        i.exec("r = my_tool()")
        assert i.get("r").raw == "ok"

    def test_denied_tool_raises_policy_violation(self):
        def my_tool():
            return wrap("ok")

        i = CaMeLInterpreter(
            tools={"my_tool": my_tool}, policy_engine=_PolicyDeny("blocked!")
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            i.exec("r = my_tool()")
        assert exc_info.value.tool_name == "my_tool"
        assert "blocked!" in exc_info.value.reason

    def test_no_policy_engine_allows_all(self):
        def my_tool():
            return wrap("free")

        i = CaMeLInterpreter(tools={"my_tool": my_tool}, policy_engine=None)
        i.exec("r = my_tool()")
        assert i.get("r").raw == "free"


# ---------------------------------------------------------------------------
# Session state persistence
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    def test_variable_visible_across_exec_calls(self):
        i = _interp()
        i.exec("x = 10")
        i.exec("y = x + 5")
        assert i.get("y").raw == 15

    def test_three_sequential_calls(self):
        i = _interp()
        i.exec("a = 1")
        i.exec("b = a + 2")
        i.exec("c = a + b")
        assert i.get("a").raw == 1
        assert i.get("b").raw == 3
        assert i.get("c").raw == 4

    def test_four_sequential_calls_with_loop(self):
        i = _interp()
        i.exec("total = 0")
        i.exec("items = [1, 2, 3]")
        i.exec("""
for item in items:
    total += item
""")
        i.exec("doubled = total * 2")
        assert i.get("doubled").raw == 12

    def test_variable_overwritten_across_exec(self):
        i = _interp()
        i.exec("x = 1")
        i.exec("x = 99")
        assert i.get("x").raw == 99

    def test_store_property_is_snapshot(self):
        i = _interp()
        i.exec("x = 5")
        snap = i.store
        i.exec("x = 99")
        assert snap["x"].raw == 5  # snapshot not affected
        assert i.get("x").raw == 99

    def test_get_undefined_raises_key_error(self):
        i = _interp()
        with pytest.raises(KeyError):
            i.get("nonexistent")

    def test_seed_and_get(self):
        i = _interp()
        cv = wrap(42, sources=frozenset({"external"}))
        i.seed("injected", cv)
        i.exec("result = injected + 1")
        assert i.get("result").raw == 43
        assert "external" in i.get("result").sources

    def test_seed_wrong_type_raises(self):
        i = _interp()
        with pytest.raises(TypeError):
            i.seed("x", 42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# STRICT mode
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_if_caps_taint_body_assignments(self):
        def secret():
            return wrap(True, sources=frozenset({"secret_src"}), readers=frozenset({"r"}))

        i = CaMeLInterpreter(
            tools={"secret": secret}, mode=ExecutionMode.STRICT
        )
        i.exec("cond = secret()")
        i.exec("""
if cond:
    x = 1
else:
    x = 0
""")
        # x is assigned inside if — its caps must include secret_src
        assert "secret_src" in i.get("x").sources

    def test_for_iterable_caps_taint_body(self):
        def get_items():
            return wrap([1, 2, 3], sources=frozenset({"iter_src"}))

        i = CaMeLInterpreter(
            tools={"get_items": get_items}, mode=ExecutionMode.STRICT
        )
        i.exec("items = get_items()")
        i.exec("""
result = 0
for n in items:
    result += n
""")
        assert "iter_src" in i.get("result").sources

    def test_normal_mode_no_taint(self):
        def secret():
            return wrap(True, sources=frozenset({"secret_src"}))

        i = CaMeLInterpreter(
            tools={"secret": secret}, mode=ExecutionMode.NORMAL
        )
        i.exec("cond = secret()")
        i.exec("""
if cond:
    x = 1
""")
        # In NORMAL mode, x is a user literal — no taint from cond
        assert "secret_src" not in i.get("x").sources


# ---------------------------------------------------------------------------
# Unsupported syntax — ≥ 15 cases
# ---------------------------------------------------------------------------


class TestUnsupportedSyntax:
    def _assert_unsupported(self, code, expected_node_type=None):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError) as exc_info:
            i.exec(code)
        if expected_node_type:
            assert exc_info.value.node_type == expected_node_type, (
                f"Expected node_type={expected_node_type!r}, "
                f"got {exc_info.value.node_type!r}"
            )

    def test_while(self):
        self._assert_unsupported("while True: pass", "While")

    def test_import(self):
        # Import statements raise ForbiddenImportError (M4-F10), not
        # UnsupportedSyntaxError, because the allowlist check fires first.
        i = _interp()
        with pytest.raises(ForbiddenImportError):
            i.exec("import os")

    def test_import_from(self):
        # Same as test_import: ForbiddenImportError is raised before the
        # generic unsupported-syntax path is reached.
        i = _interp()
        with pytest.raises(ForbiddenImportError):
            i.exec("from os import path")

    def test_class_def(self):
        self._assert_unsupported("class Foo: pass", "ClassDef")

    def test_function_def(self):
        self._assert_unsupported("def foo(): pass", "FunctionDef")

    def test_lambda(self):
        self._assert_unsupported("f = lambda x: x", "Lambda")

    def test_list_comp(self):
        self._assert_unsupported("x = [i for i in range(3)]", "ListComp")

    def test_dict_comp(self):
        self._assert_unsupported("x = {k: v for k, v in {}}", "DictComp")

    def test_set_comp(self):
        self._assert_unsupported("x = {i for i in range(3)}", "SetComp")

    def test_generator_exp(self):
        self._assert_unsupported("x = sum(i for i in range(3))", "GeneratorExp")

    def test_pass(self):
        self._assert_unsupported("pass", "Pass")

    def test_with(self):
        self._assert_unsupported("with open('f') as f: x = 1", "With")

    def test_try(self):
        self._assert_unsupported("try:\n  pass\nexcept:\n  pass", "Try")

    def test_delete(self):
        self._assert_unsupported("x = 1\ndel x", "Delete")

    def test_assert(self):
        self._assert_unsupported("assert True", "Assert")

    def test_raise(self):
        self._assert_unsupported("raise ValueError()", "Raise")

    def test_yield(self):
        self._assert_unsupported("def f(): yield 1", "FunctionDef")

    def test_slice_subscript(self):
        i = _interp()
        i.exec("lst = [1, 2, 3]")
        with pytest.raises(UnsupportedSyntaxError) as exc_info:
            i.exec("x = lst[1:2]")
        assert exc_info.value.node_type == "Slice"

    def test_lineno_is_set(self):
        i = _interp()
        try:
            i.exec("\n\nwhile True: pass")
        except UnsupportedSyntaxError as e:
            assert e.lineno == 3

    def test_nested_unpack_in_assign(self):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("(a, (b, c)) = (1, (2, 3))")

    def test_for_else(self):
        i = _interp()
        with pytest.raises(UnsupportedSyntaxError):
            i.exec("for x in [1]:\n  x = x\nelse:\n  done = 1")


# ---------------------------------------------------------------------------
# Name resolution errors
# ---------------------------------------------------------------------------


class TestNameResolution:
    def test_undefined_name_raises(self):
        i = _interp()
        with pytest.raises(NameError):
            i.exec("x = undefined_var")

    def test_tool_name_resolves(self):
        def my_tool():
            return wrap(1)

        i = CaMeLInterpreter(tools={"my_tool": my_tool})
        i.exec("r = my_tool()")
        assert i.get("r").raw == 1


# ---------------------------------------------------------------------------
# Capability propagation integration
# ---------------------------------------------------------------------------


class TestCapabilityPropagation:
    def test_tool_result_caps_survive_arithmetic(self):
        def price_tool():
            return wrap(
                100,
                sources=frozenset({"price_db"}),
                readers=frozenset({"finance@corp.com"}),
            )

        i = CaMeLInterpreter(tools={"price_tool": price_tool})
        i.exec("price = price_tool()")
        i.exec("tax = price * 2")
        cv = i.get("tax")
        assert cv.raw == 200
        assert "price_db" in cv.sources
        # Public (from User literal 2) absorbs restricted readers
        assert cv.readers is Public

    def test_two_tool_caps_merge(self):
        def tool_a():
            return wrap(1, sources=frozenset({"src_a"}), readers=frozenset({"a@x.com"}))

        def tool_b():
            return wrap(2, sources=frozenset({"src_b"}), readers=frozenset({"b@x.com"}))

        i = CaMeLInterpreter(tools={"tool_a": tool_a, "tool_b": tool_b})
        i.exec("a = tool_a()")
        i.exec("b = tool_b()")
        i.exec("c = a + b")
        cv = i.get("c")
        assert "src_a" in cv.sources
        assert "src_b" in cv.sources
        assert "a@x.com" in cv.readers
        assert "b@x.com" in cv.readers

    def test_list_construction_merges_caps(self):
        def tool_a():
            return wrap("x", sources=frozenset({"src_a"}))

        def tool_b():
            return wrap("y", sources=frozenset({"src_b"}))

        i = CaMeLInterpreter(tools={"tool_a": tool_a, "tool_b": tool_b})
        i.exec("a = tool_a()")
        i.exec("b = tool_b()")
        i.exec("lst = [a, b]")
        cv = i.get("lst")
        assert "src_a" in cv.sources
        assert "src_b" in cv.sources
