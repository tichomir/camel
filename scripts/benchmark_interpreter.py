"""Benchmark: CaMeL interpreter per-step overhead (NFR-4).

Simulates 50 tool-call steps through the CaMeLInterpreter and asserts
that the median per-step wall-clock time is ≤ 100 ms.

Usage
-----
    python scripts/benchmark_interpreter.py          # exits 0 if pass, 1 if fail
    python scripts/benchmark_interpreter.py --verbose
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from typing import Any

# Ensure the package root is importable when run directly.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from camel.interpreter import CaMeLInterpreter
from camel.value import Public, wrap

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_STEPS = 50
THRESHOLD_MS = 100.0  # NFR-4: median overhead ≤ 100 ms per tool-call step

# ---------------------------------------------------------------------------
# Simulated tool suite
# ---------------------------------------------------------------------------

_TOOL_PROGRAMS = [
    # Arithmetic assignment
    "result = 1 + 2",
    # String concatenation
    'greeting = "hello" + " " + "world"',
    # List construction
    "items = [1, 2, 3]",
    # Dict construction
    'config = {"key": "value", "count": 10}',
    # Conditional
    'x = 5\nif x > 3:\n    label = "big"\nelse:\n    label = "small"',
    # For loop with accumulator
    "total = 0\nfor i in [1, 2, 3, 4, 5]:\n    total = total + i",
    # Nested arithmetic
    "a = 10\nb = 20\nc = a * b + a - b",
    # f-string
    'name = "CaMeL"\nmsg = f"Hello, {name}!"',
    # Subscript access
    'words = ["alpha", "beta", "gamma"]\nfirst = words[0]',
    # Multiple assignments
    "x = 1\ny = 2\nz = x + y\nw = z * 3",
]


def _make_tools() -> dict[str, Any]:
    """Return a minimal tool registry for benchmark steps."""

    def get_value() -> Any:
        return wrap(42, sources=frozenset({"benchmark_tool"}), readers=Public)

    def get_string() -> Any:
        return wrap("bench_data", sources=frozenset({"benchmark_tool"}), readers=Public)

    def get_list() -> Any:
        return wrap([1, 2, 3], sources=frozenset({"benchmark_tool"}), readers=Public)

    return {
        "get_value": get_value,
        "get_string": get_string,
        "get_list": get_list,
    }


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def _run_step(interp: CaMeLInterpreter, step_index: int) -> float:
    """Execute one simulated tool-call step and return wall-clock time in ms."""
    program = _TOOL_PROGRAMS[step_index % len(_TOOL_PROGRAMS)]
    # Include a tool call in every other step to exercise the full path.
    if step_index % 2 == 0:
        program = f"tool_out = get_value()\n{program}"

    t0 = time.perf_counter()
    interp.exec(program)
    t1 = time.perf_counter()
    return (t1 - t0) * 1000.0  # convert to ms


def run_benchmark(verbose: bool = False) -> tuple[float, list[float]]:
    """Run the full benchmark and return (median_ms, all_times_ms)."""
    tools = _make_tools()
    # Use a fresh interpreter per step to isolate each measurement.
    times: list[float] = []
    for i in range(NUM_STEPS):
        interp = CaMeLInterpreter(tools=tools)
        elapsed = _run_step(interp, i)
        times.append(elapsed)
        if verbose:
            print(f"  step {i + 1:>3}/{NUM_STEPS}  {elapsed:7.2f} ms")

    median = statistics.median(times)
    return median, times


def main() -> int:
    parser = argparse.ArgumentParser(description="CaMeL interpreter benchmark (NFR-4)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-step times")
    args = parser.parse_args()

    print(f"Running {NUM_STEPS} interpreter steps …")
    median_ms, times = run_benchmark(verbose=args.verbose)

    p95 = sorted(times)[int(0.95 * len(times))]
    print("\nResults:")
    print(f"  Steps    : {NUM_STEPS}")
    print(f"  Median   : {median_ms:.2f} ms  (threshold: {THRESHOLD_MS:.0f} ms)")
    print(f"  Min      : {min(times):.2f} ms")
    print(f"  Max      : {max(times):.2f} ms")
    print(f"  p95      : {p95:.2f} ms")

    if median_ms <= THRESHOLD_MS:
        print(f"\nPASS — median {median_ms:.2f} ms ≤ {THRESHOLD_MS:.0f} ms (NFR-4)")
        return 0
    else:
        print(
            f"\nFAIL — median {median_ms:.2f} ms exceeds {THRESHOLD_MS:.0f} ms threshold (NFR-4)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
