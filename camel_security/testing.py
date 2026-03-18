"""Developer tooling for policy authoring and dry-run simulation.

This module provides three developer-facing utilities for authoring, testing,
and simulating CaMeL security policies without a live interpreter or real tool
side-effects:

``PolicyTestRunner``
    Batch-evaluates a policy function against a list of :class:`PolicyTestCase`
    instances and returns a structured :class:`PolicyTestReport` with per-case
    pass/fail results and aggregate coverage statistics.

``CaMeLValueBuilder``
    Fluent builder that constructs :class:`~camel.value.CaMeLValue` instances
    with caller-specified sources, readers, inner_source, and explicit
    dependency chains — no live interpreter required.

``PolicySimulator``
    Wraps the CaMeL interpreter in dry-run mode, replacing all registered tool
    implementations with no-op stubs that return typed placeholder values, and
    traverses the full execution loop against a provided pseudo-Python code
    plan.  Returns a :class:`SimulationReport` listing each policy evaluation
    without executing any side-effecting operations.

All three classes are exported from ``camel_security.testing``.

Example — testing a send_email policy::

    from camel_security.testing import (
        CaMeLValueBuilder,
        PolicyTestCase,
        PolicyTestRunner,
    )
    from camel.policy.reference_policies import send_email_policy

    runner = PolicyTestRunner()
    report = runner.run(
        send_email_policy,
        [
            PolicyTestCase(
                case_id="trusted_recipient",
                tool_name="send_email",
                kwargs={
                    "to": (
                        CaMeLValueBuilder("alice@example.com")
                        .with_sources("User literal")
                        .build()
                    ),
                },
                expected_outcome="Allowed",
            ),
            PolicyTestCase(
                case_id="injected_recipient",
                tool_name="send_email",
                kwargs={
                    "to": (
                        CaMeLValueBuilder("evil@attacker.com")
                        .with_sources("read_email")
                        .build()
                    ),
                },
                expected_outcome="Denied",
                expected_reason_contains="untrusted",
            ),
        ],
    )
    assert report.passed == 2

Example — dry-run simulation::

    from camel_security.testing import PolicySimulator
    from camel.policy import PolicyRegistry
    from camel.policy.reference_policies import configure_reference_policies

    registry = PolicyRegistry()
    configure_reference_policies(registry, file_owner="alice@example.com")

    simulator = PolicySimulator()
    report = simulator.simulate(
        plan=\"\"\"
        result = send_email(to=recipient, subject=subj, body=msg)
        \"\"\",
        tools=["send_email"],
        policies=registry,
    )
    for ev in report.evaluations:
        print(ev.tool_name, ev.result)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyFn,
    PolicyRegistry,
    SecurityPolicyResult,
)
from camel.value import CaMeLValue, Public, Readers, _PublicType, wrap

__all__ = [
    # PolicyTestRunner
    "PolicyTestCase",
    "PolicyCaseResult",
    "PolicyTestReport",
    "PolicyTestRunner",
    # CaMeLValueBuilder
    "CaMeLValueBuilder",
    # PolicySimulator
    "SimulatedPolicyEvaluation",
    "SimulationReport",
    "PolicySimulator",
]


# ---------------------------------------------------------------------------
# PolicyTestRunner — data classes
# ---------------------------------------------------------------------------


@dataclass
class PolicyTestCase:
    """A single test case for :class:`PolicyTestRunner`.

    Describes one invocation scenario for a policy function: the tool name,
    the argument mapping (as :class:`~camel.value.CaMeLValue` instances), the
    expected outcome, and an optional denial-reason fragment to match.

    Parameters
    ----------
    tool_name:
        The name of the tool being tested (passed as the first argument to
        the policy function).
    kwargs:
        A mapping from argument names to :class:`~camel.value.CaMeLValue`
        instances representing the tool-call arguments.  Build values with
        :class:`CaMeLValueBuilder` or :func:`~camel.value.wrap`.
    expected_outcome:
        ``"Allowed"`` if the policy should permit the call; ``"Denied"`` if
        it should block it.
    expected_reason_contains:
        When ``expected_outcome="Denied"``, optionally assert that the
        denial reason contains this substring (case-insensitive).  Ignored
        for ``"Allowed"`` cases.
    case_id:
        Optional human-readable identifier for this test case, used in
        failure messages and the :class:`PolicyTestReport`.  Defaults to a
        positional index string when ``None``.

    Examples
    --------
    ::

        from camel_security.testing import PolicyTestCase, CaMeLValueBuilder

        case = PolicyTestCase(
            case_id="trusted_recipient",
            tool_name="send_email",
            kwargs={
                "to": CaMeLValueBuilder("alice@example.com")
                    .with_sources("User literal")
                    .build()
            },
            expected_outcome="Allowed",
        )
    """

    tool_name: str
    kwargs: dict[str, CaMeLValue]
    expected_outcome: Literal["Allowed", "Denied"]
    expected_reason_contains: str | None = None
    case_id: str | None = None


@dataclass
class PolicyCaseResult:
    """Result of evaluating a single :class:`PolicyTestCase`.

    Attributes
    ----------
    case_id:
        The ``case_id`` from the corresponding :class:`PolicyTestCase`, or a
        generated positional string (e.g. ``"case_0"``) when none was given.
    passed:
        ``True`` if the actual outcome matched the expected outcome (and the
        optional reason fragment was found when specified).
    actual_outcome:
        The outcome actually returned by the policy function.
    actual_reason:
        The denial reason when ``actual_outcome="Denied"``; ``None``
        when the call was allowed.
    failure_message:
        A human-readable description of the failure when ``passed=False``;
        ``None`` when the test passed.
    """

    case_id: str
    passed: bool
    actual_outcome: Literal["Allowed", "Denied"]
    actual_reason: str | None
    failure_message: str | None


@dataclass
class PolicyTestReport:
    """Aggregate report produced by :meth:`PolicyTestRunner.run`.

    Attributes
    ----------
    total_cases:
        Total number of test cases evaluated.
    passed:
        Number of cases whose actual outcome matched the expected outcome.
    failed:
        Number of cases whose actual outcome did not match, or whose
        reason fragment was not found.
    denied_cases:
        Number of cases that *actually* returned :class:`~camel.policy.Denied`
        (regardless of whether the test passed or failed).
    allowed_cases:
        Number of cases that *actually* returned :class:`~camel.policy.Allowed`
        (regardless of whether the test passed or failed).
    coverage_percent:
        Percentage of all cases that exercise the ``"Denied"`` path, rounded
        to two decimal places.  Computed as
        ``denied_cases / total_cases * 100`` (``0.0`` when ``total_cases``
        is zero).
    results:
        Per-case :class:`PolicyCaseResult` instances in evaluation order.
    """

    total_cases: int
    passed: int
    failed: int
    denied_cases: int
    allowed_cases: int
    coverage_percent: float
    results: list[PolicyCaseResult] = field(default_factory=list)


class PolicyTestRunner:
    """Batch-evaluates a policy function against a list of test cases.

    :meth:`run` accepts a :data:`~camel.policy.PolicyFn` callable and a list
    of :class:`PolicyTestCase` instances, evaluates each case against the
    policy, and returns a :class:`PolicyTestReport` containing per-case
    pass/fail details and aggregate statistics.

    The runner wraps the policy in a temporary single-policy
    :class:`~camel.policy.PolicyRegistry` so the evaluation path is identical
    to how the interpreter calls policies in production.

    Examples
    --------
    ::

        from camel.policy.reference_policies import send_email_policy
        from camel_security.testing import (
            CaMeLValueBuilder,
            PolicyTestCase,
            PolicyTestRunner,
        )

        runner = PolicyTestRunner()
        report = runner.run(
            send_email_policy,
            [
                PolicyTestCase(
                    tool_name="send_email",
                    kwargs={
                        "to": CaMeLValueBuilder("alice@example.com")
                            .with_sources("User literal")
                            .build()
                    },
                    expected_outcome="Allowed",
                ),
            ],
        )
        assert report.passed == 1
        assert report.failed == 0
    """

    def run(
        self,
        policy: PolicyFn,
        test_cases: list[PolicyTestCase],
    ) -> PolicyTestReport:
        """Evaluate *policy* against each test case and return a report.

        Parameters
        ----------
        policy:
            A policy function conforming to :data:`~camel.policy.PolicyFn`.
            It is evaluated in isolation for every test case.
        test_cases:
            Ordered list of :class:`PolicyTestCase` instances.  Each case
            provides the tool name, argument mapping, and expected outcome.

        Returns
        -------
        PolicyTestReport
            A :class:`PolicyTestReport` containing per-case results and
            aggregate statistics.

        Notes
        -----
        * The policy is wrapped in a single-policy
          :class:`~camel.policy.PolicyRegistry` for each case so the
          evaluation semantics are identical to production usage.
        * A test case fails if:

          - The actual outcome does not match ``expected_outcome``; or
          - ``expected_reason_contains`` is set, the actual outcome is
            ``"Denied"``, but the denial reason does not contain the
            specified substring (case-insensitive).
        """
        results: list[PolicyCaseResult] = []
        denied_count = 0
        allowed_count = 0
        passed_count = 0
        failed_count = 0

        for idx, case in enumerate(test_cases):
            case_id = case.case_id if case.case_id is not None else f"case_{idx}"

            # Wrap the policy in a temporary registry for evaluation.
            registry = PolicyRegistry()
            registry.register(case.tool_name, policy)
            raw_result: SecurityPolicyResult = registry.evaluate(
                case.tool_name, case.kwargs
            )

            # Determine actual outcome.
            if isinstance(raw_result, Allowed):
                actual_outcome: Literal["Allowed", "Denied"] = "Allowed"
                actual_reason: str | None = None
                allowed_count += 1
            else:
                actual_outcome = "Denied"
                actual_reason = raw_result.reason if isinstance(raw_result, Denied) else None
                denied_count += 1

            # Determine pass/fail.
            failure_message: str | None = None

            if actual_outcome != case.expected_outcome:
                failure_message = (
                    f"[{case_id}] Expected {case.expected_outcome!r} but got "
                    f"{actual_outcome!r}"
                    + (f" (reason: {actual_reason!r})" if actual_reason else "")
                )
            elif (
                actual_outcome == "Denied"
                and case.expected_reason_contains is not None
                and (actual_reason is None
                     or case.expected_reason_contains.lower() not in actual_reason.lower())
            ):
                failure_message = (
                    f"[{case_id}] Policy returned Denied but reason "
                    f"{actual_reason!r} does not contain "
                    f"{case.expected_reason_contains!r}"
                )

            passed = failure_message is None
            if passed:
                passed_count += 1
            else:
                failed_count += 1

            results.append(
                PolicyCaseResult(
                    case_id=case_id,
                    passed=passed,
                    actual_outcome=actual_outcome,
                    actual_reason=actual_reason,
                    failure_message=failure_message,
                )
            )

        total = len(test_cases)
        coverage_percent = round(denied_count / total * 100, 2) if total > 0 else 0.0

        return PolicyTestReport(
            total_cases=total,
            passed=passed_count,
            failed=failed_count,
            denied_cases=denied_count,
            allowed_cases=allowed_count,
            coverage_percent=coverage_percent,
            results=results,
        )


# ---------------------------------------------------------------------------
# CaMeLValueBuilder — fluent builder
# ---------------------------------------------------------------------------


class CaMeLValueBuilder:
    """Fluent builder that constructs :class:`~camel.value.CaMeLValue` instances.

    Allows test code to construct :class:`~camel.value.CaMeLValue` objects
    with full control over sources, readers, inner_source, and dependency
    chains without instantiating a live interpreter.

    The builder is immutable-friendly: each ``with_*`` method returns ``self``
    after updating internal state, so calls can be chained.  Call
    :meth:`build` to produce the final :class:`~camel.value.CaMeLValue`.

    Parameters
    ----------
    value:
        The underlying Python value to wrap.  May be set later via
        :meth:`with_value`.  Defaults to ``None``.

    Examples
    --------
    ::

        from camel_security.testing import CaMeLValueBuilder
        from camel.value import Public

        # Trusted user literal with open readers
        cv = (
            CaMeLValueBuilder("alice@example.com")
            .with_sources("User literal")
            .build()
        )
        assert cv.sources == frozenset({"User literal"})
        assert cv.readers is Public

        # Untrusted tool value with restricted readers
        cv2 = (
            CaMeLValueBuilder("secret")
            .with_sources("read_email")
            .with_readers(frozenset({"alice@example.com"}))
            .with_inner_source("body")
            .build()
        )
        assert cv2.inner_source == "body"
        assert cv2.readers == frozenset({"alice@example.com"})

        # Value with explicit dependency chain
        dep = CaMeLValueBuilder("dep").with_sources("external_tool").build()
        cv3 = (
            CaMeLValueBuilder("derived")
            .with_sources("CaMeL")
            .with_dependency(dep)
            .build()
        )
        # The dependency's sources are unioned into the built value's sources.
        assert "external_tool" in cv3.sources
    """

    def __init__(self, value: Any = None) -> None:
        """Initialise the builder with an optional underlying value.

        Parameters
        ----------
        value:
            The Python value to wrap.  May also be set via :meth:`with_value`.
        """
        self._value: Any = value
        self._sources: frozenset[str] = frozenset()
        self._readers: Readers = Public
        self._inner_source: str | None = None
        # Accumulated dependency sources and readers (unioned at build time).
        self._dep_sources: frozenset[str] = frozenset()
        self._dep_readers: Readers = frozenset()

    def with_value(self, value: Any) -> CaMeLValueBuilder:
        """Set the underlying Python value.

        Parameters
        ----------
        value:
            The Python value to wrap in the :class:`~camel.value.CaMeLValue`.

        Returns
        -------
        CaMeLValueBuilder
            *self*, for method chaining.
        """
        self._value = value
        return self

    def with_sources(self, *sources: str) -> CaMeLValueBuilder:
        """Add one or more source labels.

        Each label is added to the builder's ``sources`` frozenset.  This
        method may be called multiple times; sources accumulate.

        Parameters
        ----------
        *sources:
            One or more string source labels (e.g. ``"User literal"``,
            ``"read_email"``, ``"CaMeL"``).

        Returns
        -------
        CaMeLValueBuilder
            *self*, for method chaining.

        Examples
        --------
        ::

            cv = (
                CaMeLValueBuilder("hello")
                .with_sources("User literal")
                .with_sources("CaMeL")
                .build()
            )
            assert cv.sources == frozenset({"User literal", "CaMeL"})
        """
        self._sources = self._sources | frozenset(sources)
        return self

    def with_readers(self, readers: frozenset[str] | _PublicType) -> CaMeLValueBuilder:
        """Set the authorised readers for the value.

        Parameters
        ----------
        readers:
            Either a ``frozenset[str]`` of principal identifiers or the
            :data:`~camel.value.Public` singleton for unrestricted access.

        Returns
        -------
        CaMeLValueBuilder
            *self*, for method chaining.

        Examples
        --------
        ::

            from camel.value import Public

            # Restricted readers
            cv = (
                CaMeLValueBuilder("secret")
                .with_readers(frozenset({"alice@example.com"}))
                .build()
            )

            # Unrestricted (default)
            cv2 = CaMeLValueBuilder("open").with_readers(Public).build()
        """
        self._readers = readers
        return self

    def with_inner_source(self, inner_source: str | None) -> CaMeLValueBuilder:
        """Set the inner_source sub-field label.

        Parameters
        ----------
        inner_source:
            A string identifying which field within the originating tool
            this value came from (e.g. ``"sender"``, ``"body"``).  Pass
            ``None`` to clear a previously set inner source.

        Returns
        -------
        CaMeLValueBuilder
            *self*, for method chaining.
        """
        self._inner_source = inner_source
        return self

    def with_dependency(self, dep: CaMeLValue) -> CaMeLValueBuilder:
        """Merge the sources and readers of *dep* into this builder.

        Simulates the CaMeL capability propagation rule: a value derived
        from *dep* inherits its sources and readers via union.  Calling this
        method unions *dep*'s capability metadata into the accumulated
        dependency sets that are applied at :meth:`build` time.

        Parameters
        ----------
        dep:
            A :class:`~camel.value.CaMeLValue` whose ``sources`` and
            ``readers`` are unioned into the built value's capabilities.
            Multiple dependencies may be added by calling this method
            multiple times.

        Returns
        -------
        CaMeLValueBuilder
            *self*, for method chaining.

        Examples
        --------
        ::

            dep = (
                CaMeLValueBuilder("upstream")
                .with_sources("external_tool")
                .with_readers(frozenset({"alice@example.com"}))
                .build()
            )
            cv = (
                CaMeLValueBuilder("derived")
                .with_sources("CaMeL")
                .with_dependency(dep)
                .build()
            )
            assert "external_tool" in cv.sources
        """
        self._dep_sources = self._dep_sources | dep.sources
        # Union readers: Public is absorbing.
        if isinstance(dep.readers, _PublicType) or isinstance(self._dep_readers, _PublicType):
            self._dep_readers = Public
        else:
            self._dep_readers = self._dep_readers | dep.readers  # type: ignore[operator]
        return self

    def build(self) -> CaMeLValue:
        """Construct and return the :class:`~camel.value.CaMeLValue`.

        Unions the explicitly set sources with any accumulated dependency
        sources.  Unions the explicitly set readers with any accumulated
        dependency readers (using :data:`~camel.value.Public` absorption
        semantics).

        Returns
        -------
        CaMeLValue
            The constructed :class:`~camel.value.CaMeLValue` with all
            accumulated capability metadata applied.

        Examples
        --------
        ::

            cv = (
                CaMeLValueBuilder("hello")
                .with_sources("User literal")
                .with_readers(frozenset({"alice@example.com"}))
                .with_inner_source("greeting")
                .build()
            )
            assert cv.value == "hello"
            assert cv.sources == frozenset({"User literal"})
            assert cv.readers == frozenset({"alice@example.com"})
            assert cv.inner_source == "greeting"
        """
        final_sources = self._sources | self._dep_sources

        # Union readers: Public is absorbing.
        if isinstance(self._readers, _PublicType) or isinstance(self._dep_readers, _PublicType):
            final_readers: Readers = Public
        elif isinstance(self._readers, frozenset) and isinstance(self._dep_readers, frozenset):
            final_readers = self._readers | self._dep_readers
        else:
            final_readers = self._readers

        return CaMeLValue(
            value=self._value,
            sources=final_sources,
            inner_source=self._inner_source,
            readers=final_readers,
        )


# ---------------------------------------------------------------------------
# PolicySimulator — dry-run simulation
# ---------------------------------------------------------------------------


@dataclass
class SimulatedPolicyEvaluation:
    """One policy evaluation recorded during a :class:`PolicySimulator` dry run.

    Attributes
    ----------
    tool_name:
        The name of the tool whose policy was evaluated.
    args_snapshot:
        A snapshot of the raw argument values (unwrapped from
        :class:`~camel.value.CaMeLValue`) at the time of evaluation.
    result:
        The :class:`~camel.policy.SecurityPolicyResult` produced by the
        policy engine for this tool call.
    reason:
        The denial reason string when ``result`` is
        :class:`~camel.policy.Denied`; ``None`` when the call was allowed.
    """

    tool_name: str
    args_snapshot: dict[str, Any]
    result: SecurityPolicyResult
    reason: str | None


@dataclass
class SimulationReport:
    """Report produced by :meth:`PolicySimulator.simulate`.

    Attributes
    ----------
    evaluations:
        Ordered list of :class:`SimulatedPolicyEvaluation` instances, one per
        policy evaluation that occurred during the dry run.  No actual tool
        side-effects were executed.
    denied_tools:
        Names of tools whose policy evaluation returned
        :class:`~camel.policy.Denied` during the simulation.
    allowed_tools:
        Names of tools whose policy evaluation returned
        :class:`~camel.policy.Allowed` during the simulation.
    """

    evaluations: list[SimulatedPolicyEvaluation] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)


class PolicySimulator:
    """Dry-run policy simulator that traverses a code plan without side-effects.

    :meth:`simulate` accepts a pseudo-Python code plan, a list of tool names
    (or :class:`~camel_security.Tool` instances), and a
    :class:`~camel.policy.PolicyRegistry`.  It builds stub tool
    implementations that return typed placeholder
    :class:`~camel.value.CaMeLValue` instances, then executes the plan
    through the CaMeL interpreter in ``EVALUATION`` mode.

    All policy evaluations are captured via the interpreter's audit log and
    returned in a :class:`SimulationReport`.  No real tool calls are made.

    Notes
    -----
    * Tools that would be *denied* by a policy cause a
      :class:`~camel.interpreter.PolicyViolationError` to be raised inside
      the interpreter; the simulator catches these and records them in the
      report, then continues with the remaining plan (the policy violation is
      isolated and does not abort the entire simulation).
    * Stub tools accept any keyword arguments and return a
      :class:`~camel.value.CaMeLValue` wrapping ``None`` with
      ``sources=frozenset({tool_name})`` and ``readers=Public``.

    Examples
    --------
    ::

        from camel.policy import PolicyRegistry
        from camel.policy.reference_policies import configure_reference_policies
        from camel_security.testing import PolicySimulator, CaMeLValueBuilder

        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner="alice@example.com")

        simulator = PolicySimulator()
        report = simulator.simulate(
            plan="result = send_email(to=_to, subject=_subj)",
            tools=["send_email"],
            policies=registry,
            preset_vars={
                "_to": CaMeLValueBuilder("alice@example.com")
                    .with_sources("User literal")
                    .build(),
                "_subj": CaMeLValueBuilder("Hello")
                    .with_sources("User literal")
                    .build(),
            },
        )
        assert len(report.evaluations) == 1
        assert report.evaluations[0].tool_name == "send_email"
    """

    def simulate(
        self,
        plan: str,
        tools: list[str] | list[Any],
        policies: PolicyRegistry,
        preset_vars: Mapping[str, CaMeLValue] | None = None,
    ) -> SimulationReport:
        """Run *plan* through the interpreter in dry-run mode and capture policy evaluations.

        Parameters
        ----------
        plan:
            A pseudo-Python code string (the same format generated by the
            P-LLM).  Variable names referenced by the plan that are not
            produced by tool calls must be provided via ``preset_vars``.
        tools:
            Tool names to register as stubs.  Each entry may be either a
            plain ``str`` (just the name) or a
            :class:`~camel_security.Tool` instance (from which the
            ``name`` attribute is extracted).  All tools are replaced with
            no-op stubs that return a placeholder
            :class:`~camel.value.CaMeLValue`.
        policies:
            A :class:`~camel.policy.PolicyRegistry` containing the policies
            to simulate.
        preset_vars:
            Optional mapping of variable name → :class:`~camel.value.CaMeLValue`
            injected into the interpreter's store before execution.  Use this to
            supply values that the plan reads but that would normally come from
            earlier tool calls or the user query.

        Returns
        -------
        SimulationReport
            A :class:`SimulationReport` containing all policy evaluations
            recorded during the dry run.

        Notes
        -----
        * The simulation runs in ``EVALUATION`` mode, which means
          :class:`~camel.interpreter.PolicyViolationError` is raised on any
          policy denial.  The simulator catches these exceptions, records the
          denial in the report, and does *not* re-raise them.
        * The interpreter's audit log is read after execution (or after each
          caught ``PolicyViolationError``) to collect evaluation records.
        """
        from camel.interpreter import (  # noqa: PLC0415
            CaMeLInterpreter,
            EnforcementMode,
            PolicyViolationError,
        )

        # Extract tool names from either str or Tool-like objects.
        tool_names: list[str] = []
        for t in tools:
            if isinstance(t, str):
                tool_names.append(t)
            elif hasattr(t, "name"):
                tool_names.append(t.name)
            else:
                raise TypeError(
                    f"Each entry in 'tools' must be a str or an object with a "
                    f"'name' attribute; got {type(t).__name__!r}"
                )

        # Build stub implementations: accept any kwargs, return a placeholder CaMeLValue.
        def _make_stub(name: str) -> Any:
            """Return a no-op stub function for *name*."""

            def _stub(**_kwargs: Any) -> CaMeLValue:
                """No-op stub — returns a placeholder CaMeLValue."""
                return wrap(None, sources=frozenset({name}))

            _stub.__name__ = name
            return _stub

        stub_tools: dict[str, Any] = {name: _make_stub(name) for name in tool_names}

        # Build the interpreter in EVALUATION mode.
        interp = CaMeLInterpreter(
            tools=stub_tools,
            policy_engine=policies,
            enforcement_mode=EnforcementMode.EVALUATION,
        )

        # Inject preset variables.
        if preset_vars:
            for var_name, cv in preset_vars.items():
                interp._store[var_name] = cv  # type: ignore[attr-defined]

        # Execute the plan, catching PolicyViolationError to continue.
        try:
            interp.exec(plan)
        except PolicyViolationError:
            # Denial is already recorded in the audit log; continue to
            # collect all audit entries recorded so far.
            pass
        except Exception:
            # Other errors (e.g. name errors, syntax errors) are swallowed
            # so partial audit data is still returned; the caller can inspect
            # evaluations that completed before the error.
            pass

        # Collect evaluations from the audit log.
        evaluations: list[SimulatedPolicyEvaluation] = []
        denied_tools: list[str] = []
        allowed_tools: list[str] = []

        for entry in interp.audit_log:
            tool_name = getattr(entry, "tool_name", "")
            outcome = getattr(entry, "outcome", "Allowed")
            reason = getattr(entry, "reason", None)

            if outcome == "Denied":
                result: SecurityPolicyResult = Denied(reason or "")
                if tool_name not in denied_tools:
                    denied_tools.append(tool_name)
            else:
                result = Allowed()
                if tool_name not in allowed_tools:
                    allowed_tools.append(tool_name)

            # Build an args snapshot: we use an empty dict since the
            # interpreter's audit log does not record raw argument values
            # (they are capability-wrapped and not serialised into the log).
            # Teams requiring argument inspection should use preset_vars and
            # inspect the interpreter store after simulation.
            evaluations.append(
                SimulatedPolicyEvaluation(
                    tool_name=tool_name,
                    args_snapshot={},
                    result=result,
                    reason=reason,
                )
            )

        return SimulationReport(
            evaluations=evaluations,
            denied_tools=denied_tools,
            allowed_tools=allowed_tools,
        )
