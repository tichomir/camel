"""Session-level pytest configuration for CaMeL test suite.

Provides the ``harness_reporter`` session fixture used by the isolation
harness tests to accumulate invariant pass/fail counts and write the
JSON results summary at session end.
"""

from __future__ import annotations

import pytest

from tests.harness.results_reporter import HarnessResultsReporter

# Module-level singleton — shared across the entire pytest session.
_REPORTER = HarnessResultsReporter()


@pytest.fixture(scope="session")
def harness_reporter() -> HarnessResultsReporter:
    """Session-scoped isolation harness results reporter.

    Returns a :class:`~tests.harness.results_reporter.HarnessResultsReporter`
    instance that accumulates pass/fail counts across all harness test
    parametrisations.  After the session completes the reporter writes
    ``tests/harness_results.json``.

    Returns
    -------
    HarnessResultsReporter
        Shared reporter instance for the entire test session.
    """
    return _REPORTER


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: object,
) -> None:
    """Write the harness JSON results summary at session end.

    Called automatically by pytest after all tests have run.  Writes
    ``tests/harness_results.json`` with pass/fail counts per invariant.

    Parameters
    ----------
    session:
        The active pytest session.
    exitstatus:
        Session exit code (unused).
    """
    _REPORTER.write_json()
