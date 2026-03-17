"""Session-level pytest configuration for CaMeL test suite.

Provides the ``harness_reporter`` session fixture used by the isolation
harness tests to accumulate invariant pass/fail counts and write the
JSON results summary at session end.

Also provides policy-engine fixtures for policy unit tests:

* ``reference_policy_registry`` — function-scoped
  :class:`~camel.policy.interfaces.PolicyRegistry` pre-loaded with all six
  reference policies.
* ``policy_test_interpreter`` — function-scoped
  :class:`~camel.interpreter.CaMeLInterpreter` in ``EVALUATION`` mode wired
  to ``reference_policy_registry``, with the in-memory audit log accessible
  via ``interpreter.audit_log``.
"""

from __future__ import annotations

import pytest

from camel.interpreter import CaMeLInterpreter, EnforcementMode
from camel.policy.interfaces import PolicyRegistry
from camel.policy.reference_policies import configure_reference_policies
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


@pytest.fixture()
def reference_policy_registry() -> PolicyRegistry:
    """Return a fresh PolicyRegistry loaded with all six reference policies.

    The ``write_file`` policy is configured with ``file_owner="alice@example.com"``.
    Each test receives an isolated instance (function scope).

    Returns
    -------
    PolicyRegistry
        Pre-loaded with :func:`~camel.policy.reference_policies.configure_reference_policies`.
    """
    registry = PolicyRegistry()
    configure_reference_policies(registry, file_owner="alice@example.com")
    return registry


@pytest.fixture()
def policy_test_interpreter(
    reference_policy_registry: PolicyRegistry,
) -> CaMeLInterpreter:
    """Return a CaMeLInterpreter in EVALUATION mode with the reference policy registry.

    The interpreter has no tools registered by default; register tools via
    the ``tools`` constructor or use :meth:`~camel.interpreter.CaMeLInterpreter.exec`
    to call builtins.  The in-memory audit log is accessible via
    ``interpreter.audit_log``.

    Parameters
    ----------
    reference_policy_registry:
        Injected by the ``reference_policy_registry`` fixture.

    Returns
    -------
    CaMeLInterpreter
        Fresh interpreter instance in ``EVALUATION`` mode.
    """
    return CaMeLInterpreter(
        tools={},
        policy_engine=reference_policy_registry,
        enforcement_mode=EnforcementMode.EVALUATION,
    )


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
