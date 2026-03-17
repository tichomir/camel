"""Harness results reporter — accumulates invariant pass/fail counts.

Provides :class:`HarnessResultsReporter` which is instantiated as a
session-scoped pytest fixture.  Tests record individual invariant
outcomes via :meth:`~HarnessResultsReporter.record`.  At session end,
:meth:`~HarnessResultsReporter.write_json` writes a machine-readable
JSON summary file suitable for the exit criteria report.

JSON output shape
-----------------

.. code-block:: json

    {
      "I1_no_tool_return_in_p_llm": {
        "invariant": "I1_no_tool_return_in_p_llm",
        "description": "No tool return value content in P-LLM context",
        "passed": 50,
        "failed": 0,
        "total": 50
      },
      ...
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# InvariantResult
# ---------------------------------------------------------------------------


@dataclass
class InvariantResult:
    """Pass/fail counter for a single isolation invariant.

    Attributes
    ----------
    invariant:
        Short identifier string (e.g. ``"I1_no_tool_return_in_p_llm"``).
    description:
        Human-readable label for the invariant.
    passed:
        Number of test cases that passed this invariant.
    failed:
        Number of test cases that failed this invariant.
    """

    invariant: str
    description: str
    passed: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        """Total number of test cases evaluated for this invariant."""
        return self.passed + self.failed

    def record(self, success: bool) -> None:
        """Increment the pass or fail counter.

        Parameters
        ----------
        success:
            ``True`` to increment ``passed``; ``False`` to increment
            ``failed``.
        """
        if success:
            self.passed += 1
        else:
            self.failed += 1

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict.

        Returns
        -------
        dict[str, object]
            Keys: ``invariant``, ``description``, ``passed``, ``failed``,
            ``total``.
        """
        return {
            "invariant": self.invariant,
            "description": self.description,
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# HarnessResultsReporter
# ---------------------------------------------------------------------------


class HarnessResultsReporter:
    """Session-scoped reporter for isolation harness invariant results.

    Instantiated once per pytest session (via the ``harness_reporter``
    session fixture in ``conftest.py``).  Individual harness tests call
    :meth:`record` after each invariant assertion, then
    :meth:`write_json` is called in ``pytest_sessionfinish``.

    Attributes
    ----------
    invariants:
        Dict mapping invariant ID → :class:`InvariantResult`.
    """

    _INVARIANT_DEFS: list[tuple[str, str]] = [
        (
            "I1_no_tool_return_in_p_llm",
            "No tool return value content in P-LLM context",
        ),
        (
            "I2_no_qllm_freeform_in_p_llm",
            "No raw Q-LLM free-form response reaches P-LLM context",
        ),
        (
            "I3_redaction_completeness",
            "All untrusted-origin exception messages fully redacted before P-LLM",
        ),
    ]

    def __init__(self) -> None:
        """Initialise with zeroed counters for all three invariants."""
        self.invariants: dict[str, InvariantResult] = {
            inv_id: InvariantResult(invariant=inv_id, description=desc)
            for inv_id, desc in self._INVARIANT_DEFS
        }

    def record(self, invariant_id: str, passed: bool) -> None:
        """Record a test outcome for the given invariant.

        Parameters
        ----------
        invariant_id:
            One of the three invariant IDs:
            ``"I1_no_tool_return_in_p_llm"``,
            ``"I2_no_qllm_freeform_in_p_llm"``,
            ``"I3_redaction_completeness"``.
        passed:
            ``True`` if the test passed the invariant; ``False`` otherwise.

        Raises
        ------
        KeyError
            When *invariant_id* is not recognised.
        """
        self.invariants[invariant_id].record(passed)

    def write_json(
        self,
        path: str | Path = "tests/harness_results.json",
    ) -> None:
        """Serialise results to a JSON file.

        Parameters
        ----------
        path:
            Output file path (relative to the project root or absolute).
            Defaults to ``tests/harness_results.json``.
        """
        data = {k: v.to_dict() for k, v in self.invariants.items()}
        Path(path).write_text(json.dumps(data, indent=2))

    def summary(self) -> str:
        """Return a human-readable summary string.

        Returns
        -------
        str
            Multi-line summary of pass/fail counts per invariant.
        """
        lines = ["Isolation Harness Results", "=" * 40]
        for result in self.invariants.values():
            status = "PASS" if result.failed == 0 else "FAIL"
            lines.append(
                f"  [{status}] {result.invariant}: "
                f"{result.passed}/{result.total} passed"
            )
        return "\n".join(lines)
