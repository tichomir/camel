"""Unit test suite for M4 allowlist enforcement features.

Coverage
--------
M4-F10: ForbiddenImportError — raised for any import statement before execution.
M4-F11: Builtin allowlist — only the approved set of names is accessible.
M4-F12: Timing primitive exclusion — time and related names are absent from the
        interpreter namespace and raise ForbiddenNameError.
M4-F13: Allowlist loading — ConfigurationSecurityError on bad YAML; FileNotFoundError
        on missing file; clean startup on valid YAML.
M4-F14: ForbiddenNameError — raised with the offending name for any disallowed access.

Regression: existing interpreter functionality (assignment, list/dict ops,
for loops, if statements) is unaffected by the restricted namespace.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from camel.config.loader import (
    build_permitted_namespace,
    get_excluded_timing_names,
    get_permitted_names,
    load_allowlist,
)
from camel.exceptions import (
    ConfigurationSecurityError,
    ForbiddenImportError,
    ForbiddenNameError,
)
from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.value import CaMeLValue, Public, wrap

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_APPROVED_BUILTINS = [
    "len",
    "range",
    "list",
    "dict",
    "str",
    "int",
    "float",
    "bool",
    "set",
    "isinstance",
    "print",
    "enumerate",
    "zip",
    "sorted",
    "min",
    "max",
]

_TIMING_NAMES = [
    "time",
    "sleep",
    "perf_counter",
    "monotonic",
    "process_time",
    "clock",
    "time_ns",
]


def _interp(**tools: Any) -> CaMeLInterpreter:
    """Build a CaMeLInterpreter with no extra builtins beyond the allowlist."""
    return CaMeLInterpreter(tools=tools or None)


def _tool_returning(raw_value: Any) -> Any:
    """Return a zero-arg tool that produces a CaMeLValue."""

    def _fn() -> CaMeLValue:
        return wrap(raw_value, sources=frozenset({"test_tool"}), readers=Public)

    return _fn


def _exec(code: str, **tools: Any) -> CaMeLInterpreter:
    """Execute *code* in a fresh interpreter and return the interpreter."""
    interp = _interp(**tools)
    interp.exec(textwrap.dedent(code))
    return interp


# ---------------------------------------------------------------------------
# Section 1: ForbiddenImportError (M4-F10)
# ---------------------------------------------------------------------------


class TestForbiddenImportError:
    """Assert that any import statement raises ForbiddenImportError (M4-F10)."""

    def test_import_os(self) -> None:
        """'import os' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import os")
        assert exc_info.value.module_name == "os"

    def test_import_time(self) -> None:
        """'import time' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import time")
        assert exc_info.value.module_name == "time"

    def test_from_time_import_sleep(self) -> None:
        """'from time import sleep' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from time import sleep")
        assert exc_info.value.module_name == "time"

    def test_import_sys(self) -> None:
        """'import sys' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import sys")
        assert exc_info.value.module_name == "sys"

    def test_from_os_path_import_join(self) -> None:
        """'from os.path import join' must raise ForbiddenImportError.

        The interpreter extracts the top-level module name ('os'), not the
        dotted path ('os.path'), consistent with _extract_import_module_name.
        """
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from os.path import join")
        # Top-level module name is extracted per _extract_import_module_name.
        assert exc_info.value.module_name == "os"

    def test_import_subprocess(self) -> None:
        """'import subprocess' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import subprocess")
        assert exc_info.value.module_name == "subprocess"

    def test_from_datetime_import_datetime(self) -> None:
        """'from datetime import datetime' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from datetime import datetime")
        assert exc_info.value.module_name == "datetime"

    def test_import_json(self) -> None:
        """'import json' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError):
            interp.exec("import json")

    def test_import_blocked_before_execution(self) -> None:
        """Import scan occurs before any statement executes — side-effects are prevented."""
        interp = _interp()
        # The assignment before the import must NOT be executed.
        code = textwrap.dedent("""\
            x = 1
            import os
            y = 2
        """)
        with pytest.raises(ForbiddenImportError):
            interp.exec(code)
        # 'x' must not be in the store — execution was blocked before it ran.
        assert "x" not in interp.store

    def test_import_in_dead_code_branch_still_blocked(self) -> None:
        """Import inside an if-False branch is still blocked (full AST pre-scan)."""
        interp = _interp()
        code = textwrap.dedent("""\
            if False:
                import os
        """)
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec(code)
        assert exc_info.value.module_name == "os"

    def test_forbidden_import_error_carries_lineno(self) -> None:
        """ForbiddenImportError.lineno must be > 0 and point to the import."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import os\n")
        assert exc_info.value.lineno > 0

    def test_forbidden_import_error_str_contains_module(self) -> None:
        """str(ForbiddenImportError) must mention the module name."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import os")
        assert "os" in str(exc_info.value)

    def test_forbidden_import_logged_to_security_audit(self) -> None:
        """ForbiddenImportError events must be written to the security audit log."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError):
            interp.exec("import os")
        assert len(interp.security_audit_log) == 1

    def test_import_as_alias_blocked(self) -> None:
        """'import os as operating_system' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import os as operating_system")
        assert exc_info.value.module_name == "os"


# ---------------------------------------------------------------------------
# Section 2: ForbiddenNameError — disallowed names (M4-F14)
# ---------------------------------------------------------------------------


class TestForbiddenNameErrorDisallowed:
    """Assert that disallowed names raise ForbiddenNameError (M4-F14)."""

    @pytest.mark.parametrize(
        "name",
        [
            "open",
            "exec",
            "eval",
            "compile",
            "__import__",
            "breakpoint",
            "globals",
            "locals",
            "vars",
            "getattr",
            "setattr",
            "delattr",
            "hasattr",
            "__builtins__",
            "input",
        ],
    )
    def test_disallowed_name_raises(self, name: str) -> None:
        """Each disallowed name must raise ForbiddenNameError."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"x = {name}")
        assert exc_info.value.offending_name == name

    @pytest.mark.parametrize("name", ["open", "exec", "eval", "compile", "__import__"])
    def test_forbidden_name_error_carries_name(self, name: str) -> None:
        """ForbiddenNameError.offending_name must equal the accessed name."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"result = {name}")
        assert exc_info.value.offending_name == name

    def test_forbidden_name_error_carries_lineno(self) -> None:
        """ForbiddenNameError.lineno must be > 0."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec("x = open")
        assert exc_info.value.lineno > 0

    def test_forbidden_name_error_str_contains_name(self) -> None:
        """str(ForbiddenNameError) must mention the offending name."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec("x = eval")
        assert "eval" in str(exc_info.value)

    def test_forbidden_name_logged_to_security_audit(self) -> None:
        """ForbiddenNameError events must be written to the security audit log."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError):
            interp.exec("x = open")
        assert len(interp.security_audit_log) == 1

    def test_undefined_variable_raises_forbidden_name_error(self) -> None:
        """Accessing an undefined variable also raises ForbiddenNameError."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec("y = completely_undefined_name_xyz")
        assert exc_info.value.offending_name == "completely_undefined_name_xyz"


# ---------------------------------------------------------------------------
# Section 3: Approved builtins are accessible (M4-F11)
# ---------------------------------------------------------------------------


class TestApprovedBuiltinsAccessible:
    """Assert that each of the 15 approved builtins works without error (M4-F11)."""

    def test_len_accessible(self) -> None:
        interp = _exec("x = len([1, 2, 3])")
        assert interp.store["x"].raw == 3

    def test_range_accessible(self) -> None:
        interp = _exec("x = list(range(3))")
        assert interp.store["x"].raw == [0, 1, 2]

    def test_list_accessible(self) -> None:
        interp = _exec("x = list()")
        assert interp.store["x"].raw == []

    def test_dict_accessible(self) -> None:
        interp = _exec("x = dict()")
        assert interp.store["x"].raw == {}

    def test_str_accessible(self) -> None:
        interp = _exec("x = str(42)")
        assert interp.store["x"].raw == "42"

    def test_int_accessible(self) -> None:
        interp = _exec('x = int("7")')
        assert interp.store["x"].raw == 7

    def test_float_accessible(self) -> None:
        interp = _exec('x = float("3.14")')
        assert abs(interp.store["x"].raw - 3.14) < 1e-9

    def test_bool_accessible(self) -> None:
        interp = _exec("x = bool(0)")
        assert interp.store["x"].raw is False

    def test_set_accessible(self) -> None:
        interp = _exec("x = set()")
        assert interp.store["x"].raw == set()

    def test_isinstance_accessible(self) -> None:
        interp = _exec("x = isinstance(1, int)")
        assert interp.store["x"].raw is True

    def test_print_accessible(self, capsys: pytest.CaptureFixture[str]) -> None:
        # print() is allowed; it routes to the display channel.
        interp = _exec('print("hello")')
        # No ForbiddenNameError should have been raised.
        assert not any(
            getattr(e, "offending_name", None) == "print" for e in interp.security_audit_log
        )

    def test_enumerate_accessible(self) -> None:
        interp = _exec("""\
            items = list(enumerate([10, 20]))
        """)
        assert interp.store["items"].raw == [(0, 10), (1, 20)]

    def test_zip_accessible(self) -> None:
        interp = _exec("""\
            pairs = list(zip([1, 2], [3, 4]))
        """)
        assert interp.store["pairs"].raw == [(1, 3), (2, 4)]

    def test_sorted_accessible(self) -> None:
        interp = _exec("x = sorted([3, 1, 2])")
        assert interp.store["x"].raw == [1, 2, 3]

    def test_min_accessible(self) -> None:
        interp = _exec("x = min([5, 2, 8])")
        assert interp.store["x"].raw == 2

    def test_max_accessible(self) -> None:
        interp = _exec("x = max([5, 2, 8])")
        assert interp.store["x"].raw == 8

    @pytest.mark.parametrize("name", _APPROVED_BUILTINS)
    def test_approved_builtin_not_in_security_log(self, name: str) -> None:
        """None of the 15 approved builtins should appear in the security audit log."""
        permitted_ns = build_permitted_namespace()
        assert name in permitted_ns, f"'{name}' must be in the permitted namespace"


# ---------------------------------------------------------------------------
# Section 4: Timing primitive exclusion (M4-F12)
# ---------------------------------------------------------------------------


class TestTimingPrimitiveExclusion:
    """Assert that timing-related names are excluded from the interpreter (M4-F12)."""

    @pytest.mark.parametrize("timing_name", _TIMING_NAMES)
    def test_timing_name_raises_forbidden_name_error(self, timing_name: str) -> None:
        """Each timing primitive must raise ForbiddenNameError when accessed."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"x = {timing_name}")
        assert exc_info.value.offending_name == timing_name

    @pytest.mark.parametrize("timing_name", _TIMING_NAMES)
    def test_timing_name_absent_from_permitted_namespace(self, timing_name: str) -> None:
        """Each timing primitive must be absent from build_permitted_namespace()."""
        ns = build_permitted_namespace()
        assert timing_name not in ns, (
            f"Timing primitive '{timing_name}' must not appear in the permitted namespace"
        )

    @pytest.mark.parametrize("timing_name", _TIMING_NAMES)
    def test_timing_name_in_excluded_set(self, timing_name: str) -> None:
        """Each timing primitive must appear in get_excluded_timing_names()."""
        excluded = get_excluded_timing_names()
        assert timing_name in excluded, f"'{timing_name}' must be listed in excluded_timing_names"

    def test_time_import_raises_forbidden_import(self) -> None:
        """'import time' raises ForbiddenImportError, not ForbiddenNameError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import time")
        assert exc_info.value.module_name == "time"

    def test_permitted_namespace_has_no_overlap_with_excluded(self) -> None:
        """Permitted names and excluded timing names must be disjoint."""
        permitted = get_permitted_names()
        excluded = get_excluded_timing_names()
        overlap = permitted & excluded
        assert not overlap, f"Names appear in both permitted and excluded sets: {sorted(overlap)}"


# ---------------------------------------------------------------------------
# Section 5: Allowlist loading (M4-F13)
# ---------------------------------------------------------------------------


class TestAllowlistLoading:
    """Assert correct behaviour when allowlist.yaml is missing or malformed."""

    def test_missing_allowlist_raises_file_not_found(self, tmp_path: Path) -> None:
        """load_allowlist() raises FileNotFoundError for a non-existent path."""
        missing = str(tmp_path / "nonexistent_allowlist.yaml")
        # lru_cache is on the function; bypass it by calling with a fresh path.
        with pytest.raises(FileNotFoundError):
            load_allowlist(missing)

    def test_malformed_yaml_raises_configuration_security_error(self, tmp_path: Path) -> None:
        """load_allowlist() raises ConfigurationSecurityError for invalid YAML."""
        bad_yaml = tmp_path / "bad_allowlist.yaml"
        bad_yaml.write_text(":::invalid:::yaml:::", encoding="utf-8")
        with pytest.raises(ConfigurationSecurityError):
            load_allowlist(str(bad_yaml))

    def test_yaml_missing_required_fields_raises_configuration_security_error(
        self, tmp_path: Path
    ) -> None:
        """load_allowlist() raises ConfigurationSecurityError when required sections absent."""
        incomplete_yaml = tmp_path / "incomplete_allowlist.yaml"
        # Missing permitted_builtins and excluded_timing_names.
        incomplete_yaml.write_text(
            "review_gate:\n  last_reviewed: '2026-01-01'\n"
            "  reviewers: []\n  review_required: false\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationSecurityError):
            load_allowlist(str(incomplete_yaml))

    def test_empty_reviewers_with_review_required_raises(self, tmp_path: Path) -> None:
        """review_gate.reviewers must not be empty when review_required=true."""
        config_yaml = tmp_path / "allowlist.yaml"
        data: dict[str, Any] = {
            "review_gate": {
                "last_reviewed": "2026-03-17",
                "reviewers": [],
                "review_required": True,
            },
            "permitted_builtins": [{"name": "len", "risk_level": "low", "justification": "ok"}],
            "excluded_timing_names": [
                {"name": "time", "category": "stdlib_module", "rationale": "timing"}
            ],
        }
        config_yaml.write_text(yaml.dump(data), encoding="utf-8")
        with pytest.raises(ConfigurationSecurityError):
            load_allowlist(str(config_yaml))

    def test_valid_allowlist_initialises_interpreter(self) -> None:
        """A valid allowlist.yaml produces a working CaMeLInterpreter."""
        # Simply constructing and running a trivial exec confirms the default
        # allowlist loads without error.
        interp = _interp()
        interp.exec("x = 1 + 1")
        assert interp.store["x"].raw == 2

    def test_valid_allowlist_returns_correct_permitted_names(self) -> None:
        """get_permitted_names() must return all 15 approved builtin names."""
        permitted = get_permitted_names()
        for name in _APPROVED_BUILTINS:
            assert name in permitted, f"'{name}' missing from permitted_names"

    def test_non_mapping_yaml_raises_configuration_security_error(self, tmp_path: Path) -> None:
        """load_allowlist() raises ConfigurationSecurityError when YAML root is not a mapping."""
        list_yaml = tmp_path / "list_allowlist.yaml"
        list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ConfigurationSecurityError):
            load_allowlist(str(list_yaml))


# ---------------------------------------------------------------------------
# Section 6: Regression tests — existing interpreter functionality
# ---------------------------------------------------------------------------


class TestRegressionExistingFunctionality:
    """Confirm that allowlist enforcement does not break existing interpreter features."""

    def test_simple_variable_assignment(self) -> None:
        interp = _exec("x = 42")
        assert interp.store["x"].raw == 42

    def test_string_variable_assignment(self) -> None:
        interp = _exec('name = "alice"')
        assert interp.store["name"].raw == "alice"

    def test_arithmetic_expression(self) -> None:
        interp = _exec("result = 3 * 4 + 2")
        assert interp.store["result"].raw == 14

    def test_augmented_assignment(self) -> None:
        interp = _exec("x = 10\nx += 5")
        assert interp.store["x"].raw == 15

    def test_list_literal(self) -> None:
        interp = _exec("items = [1, 2, 3]")
        assert interp.store["items"].raw == [1, 2, 3]

    def test_dict_literal(self) -> None:
        interp = _exec('d = {"a": 1, "b": 2}')
        assert interp.store["d"].raw == {"a": 1, "b": 2}

    def test_subscript_access(self) -> None:
        interp = _exec("d = [10, 20, 30]\nx = d[1]")
        assert interp.store["x"].raw == 20

    def test_for_loop(self) -> None:
        interp = _exec("""\
            total = 0
            for i in [1, 2, 3]:
                total = total + i
        """)
        assert interp.store["total"].raw == 6

    def test_if_else(self) -> None:
        interp = _exec("""\
            x = 5
            if x > 3:
                result = 1
            else:
                result = 0
        """)
        assert interp.store["result"].raw == 1

    def test_nested_if_in_for(self) -> None:
        interp = _exec("""\
            count = 0
            for v in [1, 2, 3, 4]:
                if v > 2:
                    count = count + 1
        """)
        assert interp.store["count"].raw == 2

    def test_f_string(self) -> None:
        interp = _exec('greeting = f"hello world"')
        assert interp.store["greeting"].raw == "hello world"

    def test_tool_call_dispatch(self) -> None:
        interp = _exec(
            "result = my_tool()",
            my_tool=_tool_returning(99),
        )
        assert interp.store["result"].raw == 99

    def test_len_builtin_in_code(self) -> None:
        interp = _exec("n = len([10, 20, 30])")
        assert interp.store["n"].raw == 3

    def test_sorted_builtin_in_code(self) -> None:
        interp = _exec("s = sorted([3, 1, 2])")
        assert interp.store["s"].raw == [1, 2, 3]

    def test_range_in_for_loop(self) -> None:
        interp = _exec("""\
            acc = 0
            for i in range(4):
                acc = acc + i
        """)
        assert interp.store["acc"].raw == 6

    def test_isinstance_builtin_in_code(self) -> None:
        interp = _exec("flag = isinstance(42, int)")
        assert interp.store["flag"].raw is True

    def test_session_state_persists_across_exec_calls(self) -> None:
        interp = _interp()
        interp.exec("x = 10")
        interp.exec("y = x + 5")
        interp.exec("z = x + y")
        assert interp.store["z"].raw == 25

    def test_strict_mode_is_default(self) -> None:
        """STRICT mode must be the default execution mode (M4-F5)."""
        interp = _interp()
        assert interp._mode == ExecutionMode.STRICT  # noqa: SLF001

    def test_normal_mode_explicit_opt_in(self) -> None:
        """NORMAL mode requires explicit opt-in."""
        interp = CaMeLInterpreter(mode=ExecutionMode.NORMAL)
        assert interp._mode == ExecutionMode.NORMAL  # noqa: SLF001
