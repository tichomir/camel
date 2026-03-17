"""Side-channel test class 3: Timing primitive exclusion.

Attack vector
-------------
If the interpreter allows the ``time`` module or any of its primitives
(``sleep``, ``perf_counter``, ``monotonic``, etc.), an adversary who
controls the P-LLM plan could:

1. Encode a private value into a series of time-delayed tool calls,
   allowing an external observer to infer the value from elapsed time.
2. Use ``time.sleep()`` inside a loop whose iteration count is secret,
   turning a loop-count inference attack into a covert timing channel.
3. Read ``time.perf_counter()`` before and after a computation to infer
   the complexity — and therefore the content — of a private value.

CaMeL mitigation (M4-F12)
--------------------------
The ``time`` module and all timing-related primitives are unconditionally
excluded from the interpreter's permitted namespace:

- Any ``import time`` / ``from time import ...`` / ``import datetime`` raises
  :class:`~camel.exceptions.ForbiddenImportError` before execution (M4-F10).
- Accessing the name ``time``, ``sleep``, ``perf_counter``, ``monotonic``,
  ``process_time``, ``clock``, ``time_ns``, or ``datetime`` inside the
  interpreter raises :class:`~camel.exceptions.ForbiddenNameError` (M4-F14).
- The allowlist configuration explicitly lists these names in the
  ``excluded_timing_names`` section (M4-F13).
- The permitted namespace returned by :func:`~camel.config.loader.build_permitted_namespace`
  contains none of these names (M4-F11 / M4-F12).

PRD Section 11 target: 100% pass rate for implemented mitigations.

Test structure
--------------
Each test class corresponds to a distinct sub-vector of the timing
side-channel attack surface.  Every test documents the PRD reference it
validates.
"""

from __future__ import annotations

import pytest

from camel.config.loader import (
    build_permitted_namespace,
    get_excluded_timing_names,
    get_permitted_names,
)
from camel.exceptions import ForbiddenImportError, ForbiddenNameError
from camel.interpreter import CaMeLInterpreter, ExecutionMode


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _interp() -> CaMeLInterpreter:
    """Build a CaMeLInterpreter with no extra tools."""
    return CaMeLInterpreter()


# ---------------------------------------------------------------------------
# Timing modules blocked via import (M4-F10 + M4-F12)
# ---------------------------------------------------------------------------


class TestTimingModuleImportsBlocked:
    """Verify that all timing-related import statements are blocked (M4-F10, M4-F12).

    PRD §11 timing side-channel target: import of timing modules raises
    ForbiddenImportError before any statement executes.
    """

    def test_import_time_raises_forbidden_import(self) -> None:
        """'import time' must raise ForbiddenImportError (M4-F10/M4-F12)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import time")
        assert exc_info.value.module_name == "time", (
            "M4-F10: module_name must be 'time'"
        )

    def test_from_time_import_sleep_raises_forbidden_import(self) -> None:
        """'from time import sleep' must raise ForbiddenImportError (M4-F10/M4-F12)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from time import sleep")
        assert exc_info.value.module_name == "time"

    def test_from_time_import_perf_counter_raises(self) -> None:
        """'from time import perf_counter' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from time import perf_counter")
        assert exc_info.value.module_name == "time"

    def test_from_time_import_monotonic_raises(self) -> None:
        """'from time import monotonic' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from time import monotonic")
        assert exc_info.value.module_name == "time"

    def test_import_datetime_raises_forbidden_import(self) -> None:
        """'import datetime' must raise ForbiddenImportError (M4-F10/M4-F12)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import datetime")
        assert exc_info.value.module_name == "datetime"

    def test_from_datetime_import_datetime_raises(self) -> None:
        """'from datetime import datetime' must raise ForbiddenImportError."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("from datetime import datetime")
        assert exc_info.value.module_name == "datetime"

    def test_import_timeit_raises_forbidden_import(self) -> None:
        """'import timeit' must raise ForbiddenImportError (M4-F10/M4-F12)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError):
            interp.exec("import timeit")

    def test_import_time_in_dead_code_still_blocked(self) -> None:
        """Import inside if-False branch must still be blocked (full AST pre-scan, M4-F10)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("if False:\n    import time")
        assert exc_info.value.module_name == "time"

    def test_import_time_blocked_before_side_effects(self) -> None:
        """Import check occurs before execution — prior assignments are not executed (M4-F10)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError):
            interp.exec("x = 1\nimport time\ny = 2")
        assert "x" not in interp.store, (
            "M4-F10: import scan must block execution before 'x' is assigned"
        )

    def test_forbidden_import_lineno_positive(self) -> None:
        """ForbiddenImportError.lineno must be a positive integer (M4-F10)."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import time\n")
        assert exc_info.value.lineno > 0


# ---------------------------------------------------------------------------
# Timing names blocked as Name lookups (M4-F12, M4-F14)
# ---------------------------------------------------------------------------

_TIMING_PRIMITIVES = [
    "time",
    "sleep",
    "perf_counter",
    "monotonic",
    "process_time",
    "clock",
    "time_ns",
]

_ADDITIONAL_TIMING_NAMES = [
    "datetime",
    "timedelta",
    "timezone",
]


class TestTimingNameAccessBlocked:
    """Verify that direct name access to timing primitives raises ForbiddenNameError (M4-F14)."""

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_name_raises_forbidden_name_error(
        self, timing_name: str
    ) -> None:
        """M4-F12/M4-F14: accessing any timing primitive name raises ForbiddenNameError."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"x = {timing_name}")
        assert exc_info.value.offending_name == timing_name, (
            f"M4-F14: offending_name must be '{timing_name}'"
        )

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_offending_name_matches(self, timing_name: str) -> None:
        """M4-F14: ForbiddenNameError.offending_name must match the accessed name."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"t = {timing_name}")
        assert exc_info.value.offending_name == timing_name

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_error_str_contains_name(self, timing_name: str) -> None:
        """M4-F14: str(ForbiddenNameError) must mention the offending name."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"x = {timing_name}")
        assert timing_name in str(exc_info.value), (
            f"M4-F14: error message must contain '{timing_name}'"
        )

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_lineno_positive(self, timing_name: str) -> None:
        """M4-F14: ForbiddenNameError.lineno must be a positive integer."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec(f"x = {timing_name}")
        assert exc_info.value.lineno > 0


# ---------------------------------------------------------------------------
# Timing names absent from permitted namespace (M4-F11, M4-F12)
# ---------------------------------------------------------------------------


class TestTimingNamesAbsentFromNamespace:
    """Verify timing names are absent from the interpreter's permitted namespace (M4-F12)."""

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_absent_from_permitted_namespace(
        self, timing_name: str
    ) -> None:
        """M4-F12: timing primitive must not appear in build_permitted_namespace()."""
        ns = build_permitted_namespace()
        assert timing_name not in ns, (
            f"M4-F12: '{timing_name}' must not appear in the permitted namespace"
        )

    @pytest.mark.parametrize("timing_name", _TIMING_PRIMITIVES)
    def test_timing_primitive_in_excluded_set(self, timing_name: str) -> None:
        """M4-F12/M4-F13: timing primitive must appear in get_excluded_timing_names()."""
        excluded = get_excluded_timing_names()
        assert timing_name in excluded, (
            f"M4-F12: '{timing_name}' must appear in excluded_timing_names config"
        )

    def test_permitted_and_excluded_sets_are_disjoint(self) -> None:
        """M4-F12/M4-F13: permitted names and excluded timing names must not overlap."""
        permitted = get_permitted_names()
        excluded = get_excluded_timing_names()
        overlap = permitted & excluded
        assert not overlap, (
            f"M4-F12: names appear in both permitted and excluded sets: "
            f"{sorted(overlap)}"
        )

    def test_datetime_absent_from_permitted_namespace(self) -> None:
        """M4-F12: 'datetime' must not appear in build_permitted_namespace()."""
        ns = build_permitted_namespace()
        assert "datetime" not in ns, (
            "M4-F12: 'datetime' must not appear in the permitted namespace"
        )


# ---------------------------------------------------------------------------
# Timing channel: sleep-based loop covert channel blocked
# ---------------------------------------------------------------------------


class TestTimingSleepLoopChannelBlocked:
    """Verify that sleep-based loop covert channel is blocked end-to-end.

    Attack scenario: adversary generates a plan like:
        for _ in private_list:
            sleep(0.1)   ← encodes list length in elapsed time
    Both `import time` and `sleep` as a bare name must be blocked.
    """

    def test_sleep_name_blocked_in_loop(self) -> None:
        """'sleep' used as a bare name in a loop body must raise ForbiddenNameError."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec("""
for i in [1, 2, 3]:
    sleep(0.1)
""")
        assert exc_info.value.offending_name == "sleep"

    def test_time_sleep_import_then_call_blocked_at_import(self) -> None:
        """'import time; time.sleep(0.1)' must be blocked at the import step."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError) as exc_info:
            interp.exec("import time\ntime.sleep(0.1)")
        assert exc_info.value.module_name == "time"

    def test_perf_counter_diff_pattern_blocked(self) -> None:
        """'t0 = perf_counter()' must raise ForbiddenNameError (M4-F12)."""
        interp = _interp()
        with pytest.raises(ForbiddenNameError) as exc_info:
            interp.exec("t0 = perf_counter()")
        assert exc_info.value.offending_name == "perf_counter"


# ---------------------------------------------------------------------------
# PRD §11 pass/fail summary for timing side-channel class
# ---------------------------------------------------------------------------


class TestPRDSection11TimingSideChannel:
    """Meta-tests validating PRD §11 100% pass-rate target for timing side-channel."""

    def test_prd_section11_all_timing_imports_blocked(self) -> None:
        """PRD §11: all timing-related import attempts must be blocked (M4-F10/M4-F12)."""
        interp = _interp()
        timing_modules = ["time", "datetime", "timeit"]
        for module in timing_modules:
            with pytest.raises(ForbiddenImportError):
                interp.exec(f"import {module}")

    def test_prd_section11_all_timing_names_blocked(self) -> None:
        """PRD §11: all timing primitive names must raise ForbiddenNameError (M4-F14)."""
        for name in _TIMING_PRIMITIVES:
            interp = _interp()
            with pytest.raises(ForbiddenNameError):
                interp.exec(f"x = {name}")

    def test_prd_section11_timing_exclusion_does_not_break_approved_builtins(
        self,
    ) -> None:
        """PRD §11: timing exclusion must not affect any of the 15 approved builtins."""
        approved = [
            "len", "range", "list", "dict", "str", "int", "float", "bool",
            "set", "isinstance", "enumerate", "zip", "sorted", "min", "max",
        ]
        ns = build_permitted_namespace()
        for name in approved:
            assert name in ns, (
                f"PRD §11: '{name}' must remain in permitted namespace "
                f"after timing exclusion"
            )

    def test_prd_section11_no_timing_primitive_in_permitted_namespace(self) -> None:
        """PRD §11: no timing primitive must appear in the permitted namespace (M4-F12)."""
        ns = build_permitted_namespace()
        for name in _TIMING_PRIMITIVES:
            assert name not in ns, (
                f"PRD §11: '{name}' must not appear in permitted namespace"
            )

    def test_prd_section11_security_audit_log_records_timing_block(self) -> None:
        """PRD §11/NFR-6: blocked timing primitive access must be recorded in audit log."""
        interp = _interp()
        with pytest.raises(ForbiddenImportError):
            interp.exec("import time")
        assert len(interp.security_audit_log) >= 1, (
            "PRD §11/NFR-6: blocked timing import must appear in security audit log"
        )
