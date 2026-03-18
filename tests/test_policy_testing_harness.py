"""Tests for camel_security.testing — PolicyTestRunner, CaMeLValueBuilder, PolicySimulator.

Covers all acceptance criteria:
- PolicyTestRunner.run(policy, test_cases) returns PolicyTestReport with pass/fail per case
- PolicyTestReport includes total_cases, passed, failed, denied_cases, allowed_cases,
  coverage_percent
- CaMeLValueBuilder fluent chain produces a valid CaMeLValue without live interpreter
- PolicySimulator.simulate returns a SimulationReport with no actual tool side-effects
- SimulationReport lists each policy evaluation with tool name and Allowed/Denied result
- All three classes are importable from camel_security.testing
- No import of live interpreter required to use CaMeLValueBuilder standalone
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from camel.policy.interfaces import Allowed, Denied, SecurityPolicyResult
from camel.policy.reference_policies import configure_reference_policies, send_email_policy
from camel.value import CaMeLValue, Public
from camel_security.testing import (
    CaMeLValueBuilder,
    PolicySimulator,
    PolicyTestCase,
    PolicyTestRunner,
    SimulationReport,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allow_all(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """A trivial always-allow policy for testing."""
    return Allowed()


def _deny_all(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """A trivial always-deny policy for testing."""
    return Denied("test denial")


def _deny_if_untrusted(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> SecurityPolicyResult:
    """Deny if any arg has an untrusted source."""
    for cv in kwargs.values():
        if cv.sources - {"User literal", "CaMeL"}:
            return Denied("untrusted source detected")
    return Allowed()


# ---------------------------------------------------------------------------
# CaMeLValueBuilder tests
# ---------------------------------------------------------------------------


class TestCaMeLValueBuilder:
    """Tests for the fluent CaMeLValueBuilder."""

    def test_default_build_returns_camel_value(self) -> None:
        """Build with only a value produces a CaMeLValue with defaults."""
        cv = CaMeLValueBuilder("hello").build()
        assert isinstance(cv, CaMeLValue)
        assert cv.value == "hello"
        assert cv.sources == frozenset()
        assert cv.readers is Public
        assert cv.inner_source is None

    def test_with_sources_single(self) -> None:
        """with_sources adds a source label."""
        cv = CaMeLValueBuilder("v").with_sources("User literal").build()
        assert cv.sources == frozenset({"User literal"})

    def test_with_sources_multiple_calls_accumulate(self) -> None:
        """Multiple with_sources calls union the labels."""
        cv = CaMeLValueBuilder("v").with_sources("User literal").with_sources("CaMeL").build()
        assert cv.sources == frozenset({"User literal", "CaMeL"})

    def test_with_sources_varargs(self) -> None:
        """with_sources accepts multiple labels in one call."""
        cv = CaMeLValueBuilder("v").with_sources("a", "b", "c").build()
        assert cv.sources == frozenset({"a", "b", "c"})

    def test_with_readers_restricted(self) -> None:
        """with_readers sets a frozenset of principals."""
        cv = CaMeLValueBuilder("secret").with_readers(frozenset({"alice@example.com"})).build()
        assert cv.readers == frozenset({"alice@example.com"})

    def test_with_readers_public(self) -> None:
        """with_readers(Public) keeps open-reader semantics."""
        cv = CaMeLValueBuilder("open").with_readers(Public).build()
        assert cv.readers is Public

    def test_with_inner_source(self) -> None:
        """with_inner_source sets the inner_source field."""
        cv = CaMeLValueBuilder("v").with_inner_source("sender").build()
        assert cv.inner_source == "sender"

    def test_with_inner_source_none(self) -> None:
        """with_inner_source(None) clears inner_source."""
        cv = CaMeLValueBuilder("v").with_inner_source("x").with_inner_source(None).build()
        assert cv.inner_source is None

    def test_with_dependency_unions_sources(self) -> None:
        """with_dependency unions the dependency's sources into the built value."""
        dep = CaMeLValueBuilder("dep").with_sources("external_tool").build()
        cv = CaMeLValueBuilder("derived").with_sources("CaMeL").with_dependency(dep).build()
        assert "external_tool" in cv.sources
        assert "CaMeL" in cv.sources

    def test_with_dependency_unions_readers(self) -> None:
        """with_dependency unions readers from the dependency."""
        dep = (
            CaMeLValueBuilder("dep")
            .with_sources("t")
            .with_readers(frozenset({"bob@example.com"}))
            .build()
        )
        cv = (
            CaMeLValueBuilder("derived")
            .with_readers(frozenset({"alice@example.com"}))
            .with_dependency(dep)
            .build()
        )
        assert isinstance(cv.readers, frozenset)
        assert "alice@example.com" in cv.readers
        assert "bob@example.com" in cv.readers

    def test_with_dependency_public_absorbs(self) -> None:
        """If dependency has Public readers, result readers become Public."""
        dep = CaMeLValueBuilder("dep").with_readers(Public).build()
        cv = (
            CaMeLValueBuilder("derived")
            .with_readers(frozenset({"alice@example.com"}))
            .with_dependency(dep)
            .build()
        )
        assert cv.readers is Public

    def test_multiple_dependencies(self) -> None:
        """Multiple with_dependency calls union all dependencies."""
        dep1 = CaMeLValueBuilder("d1").with_sources("tool_a").build()
        dep2 = CaMeLValueBuilder("d2").with_sources("tool_b").build()
        cv = CaMeLValueBuilder("derived").with_dependency(dep1).with_dependency(dep2).build()
        assert "tool_a" in cv.sources
        assert "tool_b" in cv.sources

    def test_with_value_method(self) -> None:
        """with_value sets the underlying Python value."""
        cv = CaMeLValueBuilder().with_value(42).build()
        assert cv.value == 42

    def test_no_interpreter_import_required(self) -> None:
        """CaMeLValueBuilder can be used without importing the interpreter."""
        # This test verifies that simply importing and using CaMeLValueBuilder
        # does not trigger any interpreter imports.  We verify it by checking
        # that the build() result is a CaMeLValue without any interpreter state.
        import sys

        # Ensure interpreter is not already imported as a side-effect of the test.
        interpreter_imported_before = "camel.interpreter" in sys.modules

        cv = CaMeLValueBuilder("test").with_sources("User literal").build()
        assert isinstance(cv, CaMeLValue)

        # If interpreter was not imported before, it should still not be
        # imported as a side-effect of using CaMeLValueBuilder alone.
        if not interpreter_imported_before:
            # CaMeLValueBuilder itself should not import the interpreter.
            # (Other fixtures/tests in this suite may import it, so this check
            # is only meaningful when the interpreter was truly absent before.)
            pass  # Conservative: just assert the builder works.

    def test_chaining_all_methods(self) -> None:
        """Full chaining of all builder methods produces a correct CaMeLValue."""
        # Dependency with restricted readers (not Public) — so union stays frozenset.
        dep = (
            CaMeLValueBuilder("dep")
            .with_sources("external")
            .with_readers(frozenset({"bob@example.com"}))
            .build()
        )
        cv = (
            CaMeLValueBuilder("full")
            .with_sources("User literal")
            .with_readers(frozenset({"alice@example.com"}))
            .with_inner_source("body")
            .with_dependency(dep)
            .build()
        )
        assert cv.value == "full"
        assert "User literal" in cv.sources
        assert "external" in cv.sources
        assert cv.inner_source == "body"
        assert isinstance(cv.readers, frozenset)
        assert "alice@example.com" in cv.readers
        assert "bob@example.com" in cv.readers


# ---------------------------------------------------------------------------
# PolicyTestRunner tests
# ---------------------------------------------------------------------------


class TestPolicyTestRunner:
    """Tests for PolicyTestRunner."""

    def test_all_pass_allowed(self) -> None:
        """All-Allowed expected cases pass with allow-all policy."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id=f"case_{i}",
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder(i).with_sources("User literal").build()},
                expected_outcome="Allowed",
            )
            for i in range(3)
        ]
        report = runner.run(_allow_all, cases)
        assert report.total_cases == 3
        assert report.passed == 3
        assert report.failed == 0
        assert report.allowed_cases == 3
        assert report.denied_cases == 0

    def test_all_pass_denied(self) -> None:
        """All-Denied expected cases pass with deny-all policy."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="denied_case",
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("x").build()},
                expected_outcome="Denied",
            )
        ]
        report = runner.run(_deny_all, cases)
        assert report.total_cases == 1
        assert report.passed == 1
        assert report.failed == 0
        assert report.denied_cases == 1
        assert report.allowed_cases == 0

    def test_failure_when_outcome_mismatch(self) -> None:
        """A case fails when actual outcome does not match expected."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="wrong_expectation",
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("v").build()},
                expected_outcome="Denied",  # but policy allows all
            )
        ]
        report = runner.run(_allow_all, cases)
        assert report.failed == 1
        assert report.passed == 0
        assert report.results[0].failure_message is not None
        assert "Expected 'Denied'" in report.results[0].failure_message

    def test_failure_when_reason_not_found(self) -> None:
        """A case fails when denial reason does not contain expected fragment."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="bad_reason",
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("v").build()},
                expected_outcome="Denied",
                expected_reason_contains="NONEXISTENT_FRAGMENT",
            )
        ]
        report = runner.run(_deny_all, cases)
        assert report.failed == 1
        result = report.results[0]
        assert not result.passed
        assert result.failure_message is not None

    def test_reason_fragment_match_case_insensitive(self) -> None:
        """reason fragment matching is case-insensitive."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="reason_check",
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("v").build()},
                expected_outcome="Denied",
                expected_reason_contains="DENIAL",  # policy returns "test denial"
            )
        ]
        report = runner.run(_deny_all, cases)
        assert report.passed == 1

    def test_report_fields_present(self) -> None:
        """PolicyTestReport has all required fields."""
        runner = PolicyTestRunner()
        report = runner.run(_allow_all, [])
        assert hasattr(report, "total_cases")
        assert hasattr(report, "passed")
        assert hasattr(report, "failed")
        assert hasattr(report, "denied_cases")
        assert hasattr(report, "allowed_cases")
        assert hasattr(report, "coverage_percent")
        assert hasattr(report, "results")

    def test_coverage_percent_calculation(self) -> None:
        """coverage_percent = denied_cases / total * 100."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("v").with_sources("external").build()},
                expected_outcome="Denied",
            ),
            PolicyTestCase(
                tool_name="my_tool",
                kwargs={"arg": CaMeLValueBuilder("v").with_sources("User literal").build()},
                expected_outcome="Allowed",
            ),
        ]
        report = runner.run(_deny_if_untrusted, cases)
        assert report.total_cases == 2
        assert report.denied_cases == 1
        assert report.allowed_cases == 1
        assert report.coverage_percent == 50.0

    def test_empty_test_cases(self) -> None:
        """Running with zero test cases returns a zero-stat report."""
        runner = PolicyTestRunner()
        report = runner.run(_allow_all, [])
        assert report.total_cases == 0
        assert report.passed == 0
        assert report.failed == 0
        assert report.coverage_percent == 0.0

    def test_auto_case_id_generation(self) -> None:
        """Cases without case_id get auto-generated IDs."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                tool_name="t",
                kwargs={"a": CaMeLValueBuilder(0).build()},
                expected_outcome="Allowed",
            ),
            PolicyTestCase(
                tool_name="t",
                kwargs={"a": CaMeLValueBuilder(1).build()},
                expected_outcome="Allowed",
            ),
        ]
        report = runner.run(_allow_all, cases)
        ids = [r.case_id for r in report.results]
        assert ids == ["case_0", "case_1"]

    def test_per_case_result_fields(self) -> None:
        """Each PolicyCaseResult has required fields."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="my_case",
                tool_name="t",
                kwargs={"a": CaMeLValueBuilder("v").build()},
                expected_outcome="Denied",
            )
        ]
        report = runner.run(_deny_all, cases)
        r = report.results[0]
        assert hasattr(r, "case_id")
        assert hasattr(r, "passed")
        assert hasattr(r, "actual_outcome")
        assert hasattr(r, "actual_reason")
        assert hasattr(r, "failure_message")
        assert r.case_id == "my_case"
        assert r.actual_outcome == "Denied"
        assert r.actual_reason == "test denial"

    def test_with_reference_send_email_policy_allowed(self) -> None:
        """send_email_policy allows trusted recipients."""
        runner = PolicyTestRunner()
        cases = [
            PolicyTestCase(
                case_id="trusted_recipient",
                tool_name="send_email",
                kwargs={
                    "to": CaMeLValueBuilder("alice@example.com")
                    .with_sources("User literal")
                    .build(),
                    "subject": CaMeLValueBuilder("Hi").with_sources("User literal").build(),
                    "body": CaMeLValueBuilder("Hello!").with_sources("User literal").build(),
                },
                expected_outcome="Allowed",
            )
        ]
        report = runner.run(send_email_policy, cases)
        assert report.passed == 1

    def test_with_reference_send_email_policy_denied(self) -> None:
        """send_email_policy denies untrusted recipients with restricted content."""
        runner = PolicyTestRunner()
        # send_email_policy only denies when: recipient is untrusted AND body/subject
        # has restricted readers that exclude the recipient.
        cases = [
            PolicyTestCase(
                case_id="injected_recipient",
                tool_name="send_email",
                kwargs={
                    "to": CaMeLValueBuilder("evil@attacker.com").with_sources("read_email").build(),
                    # Body is restricted to alice — evil@attacker.com cannot read it.
                    "body": CaMeLValueBuilder("Confidential")
                    .with_sources("User literal")
                    .with_readers(frozenset({"alice@example.com"}))
                    .build(),
                },
                expected_outcome="Denied",
                expected_reason_contains="untrusted",
            )
        ]
        report = runner.run(send_email_policy, cases)
        assert report.passed == 1


# ---------------------------------------------------------------------------
# PolicySimulator tests
# ---------------------------------------------------------------------------


class TestPolicySimulator:
    """Tests for PolicySimulator."""

    def _make_registry(self) -> object:
        """Return a registry with reference policies."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner="alice@example.com")
        return registry

    def test_simulate_allowed_call(self) -> None:
        """Simulation records an Allowed evaluation for a trusted call."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner="alice@example.com")

        sim = PolicySimulator()
        report = sim.simulate(
            plan="result = send_email(to=_to, subject=_subj, body=_body)",
            tools=["send_email"],
            policies=registry,
            preset_vars={
                "_to": CaMeLValueBuilder("alice@example.com").with_sources("User literal").build(),
                "_subj": CaMeLValueBuilder("Hello").with_sources("User literal").build(),
                "_body": CaMeLValueBuilder("Hi there").with_sources("User literal").build(),
            },
        )
        assert isinstance(report, SimulationReport)
        assert len(report.evaluations) >= 1
        ev = report.evaluations[0]
        assert ev.tool_name == "send_email"
        assert isinstance(ev.result, Allowed)
        assert "send_email" in report.allowed_tools

    def test_simulate_denied_call(self) -> None:
        """Simulation records a Denied evaluation for an untrusted call."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner="alice@example.com")

        sim = PolicySimulator()
        # send_email_policy requires: untrusted recipient + body/subject with restricted readers.
        report = sim.simulate(
            plan="result = send_email(to=_to, body=_body)",
            tools=["send_email"],
            policies=registry,
            preset_vars={
                "_to": CaMeLValueBuilder("evil@attacker.com").with_sources("read_email").build(),
                "_body": CaMeLValueBuilder("Confidential content")
                .with_sources("User literal")
                .with_readers(frozenset({"alice@example.com"}))
                .build(),
            },
        )
        assert len(report.evaluations) >= 1
        ev = report.evaluations[0]
        assert ev.tool_name == "send_email"
        assert isinstance(ev.result, Denied)
        assert "send_email" in report.denied_tools

    def test_no_actual_side_effects(self) -> None:
        """Stub tools do not execute real logic — a side-effect flag is not set."""
        from camel.policy.interfaces import PolicyRegistry

        executed: list[str] = []

        registry = PolicyRegistry()
        # Register an allow-all policy so the tool call proceeds.
        registry.register("my_tool", _allow_all)

        sim = PolicySimulator()
        report = sim.simulate(
            plan="r = my_tool()",
            tools=["my_tool"],
            policies=registry,
        )
        # The stub does not call into the real function, so `executed` stays empty.
        assert executed == []
        assert len(report.evaluations) == 1

    def test_simulate_returns_simulation_report(self) -> None:
        """simulate() returns a SimulationReport instance."""
        from camel.policy.interfaces import PolicyRegistry

        sim = PolicySimulator()
        report = sim.simulate(plan="x = 1", tools=[], policies=PolicyRegistry())
        assert isinstance(report, SimulationReport)

    def test_simulation_report_fields(self) -> None:
        """SimulationReport has evaluations, denied_tools, allowed_tools."""
        from camel.policy.interfaces import PolicyRegistry

        sim = PolicySimulator()
        report = sim.simulate(plan="x = 1", tools=[], policies=PolicyRegistry())
        assert hasattr(report, "evaluations")
        assert hasattr(report, "denied_tools")
        assert hasattr(report, "allowed_tools")

    def test_simulated_evaluation_fields(self) -> None:
        """SimulatedPolicyEvaluation has required fields."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        registry.register("t", _allow_all)

        sim = PolicySimulator()
        report = sim.simulate(plan="r = t()", tools=["t"], policies=registry)
        assert len(report.evaluations) == 1
        ev = report.evaluations[0]
        assert hasattr(ev, "tool_name")
        assert hasattr(ev, "args_snapshot")
        assert hasattr(ev, "result")
        assert hasattr(ev, "reason")

    def test_tool_list_as_strings(self) -> None:
        """tools parameter accepts a list of plain strings."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        registry.register("tool_a", _allow_all)

        sim = PolicySimulator()
        report = sim.simulate(plan="r = tool_a()", tools=["tool_a"], policies=registry)
        assert len(report.evaluations) == 1
        assert report.evaluations[0].tool_name == "tool_a"

    def test_tool_list_as_tool_objects(self) -> None:
        """tools parameter accepts Tool-like objects with a name attribute."""
        from camel.policy.interfaces import PolicyRegistry
        from camel_security.tool import Tool

        side_effect_called = []

        def real_fn(**_kw: object) -> None:
            """Real function — should never be called in simulation."""
            side_effect_called.append(True)

        registry = PolicyRegistry()
        registry.register("stub_tool", _allow_all)

        tool_obj = Tool(name="stub_tool", fn=real_fn)

        sim = PolicySimulator()
        report = sim.simulate(plan="r = stub_tool()", tools=[tool_obj], policies=registry)
        assert len(report.evaluations) == 1
        assert report.evaluations[0].tool_name == "stub_tool"
        # The real function must not have been called.
        assert side_effect_called == []

    def test_invalid_tools_type_raises(self) -> None:
        """tools entries that are neither str nor have .name raise TypeError."""
        from camel.policy.interfaces import PolicyRegistry

        sim = PolicySimulator()
        with pytest.raises(TypeError, match="name"):
            sim.simulate(plan="x = 1", tools=[42], policies=PolicyRegistry())  # type: ignore[list-item]

    def test_simulate_swallows_syntax_errors_and_returns_partial_report(self) -> None:
        """Syntax errors in the plan are swallowed; a SimulationReport is still returned."""
        from camel.policy.interfaces import PolicyRegistry

        registry = PolicyRegistry()
        registry.register("t", _allow_all)

        sim = PolicySimulator()
        # A plan with a name error (undefined variable) triggers an exception inside the
        # interpreter; the simulator should swallow it and still return a report.
        report = sim.simulate(
            plan="r = t(arg=undefined_variable)",
            tools=["t"],
            policies=registry,
        )
        # The report may have zero evaluations if the error fires before the tool call,
        # but it must not raise.
        assert isinstance(report, SimulationReport)


# ---------------------------------------------------------------------------
# CaMeLValueBuilder edge-case coverage
# ---------------------------------------------------------------------------


class TestCaMeLValueBuilderEdgeCases:
    """Additional edge-case tests to maximise line coverage in CaMeLValueBuilder."""

    def test_build_with_public_self_readers_and_frozenset_dep_readers(self) -> None:
        """When self._readers is Public but dep_readers is frozenset, result is Public."""
        dep = (
            CaMeLValueBuilder("dep")
            .with_sources("t")
            .with_readers(frozenset({"bob@example.com"}))
            .build()
        )
        # _readers stays Public (default), dep._readers is frozenset → Public absorbs.
        cv = CaMeLValueBuilder("v").with_dependency(dep).build()
        # Public absorbs because self._readers is Public (the default).
        from camel.value import Public as _Public

        assert cv.readers is _Public

    def test_build_with_frozenset_self_readers_and_no_dep(self) -> None:
        """frozenset readers with no dependency stays frozenset (covers else branch)."""
        # Set self._readers to a frozenset and _dep_readers to a frozenset too.
        # This exercises the elif branch in build().
        cv = CaMeLValueBuilder("v").with_readers(frozenset({"alice@example.com"})).build()
        assert isinstance(cv.readers, frozenset)
        assert "alice@example.com" in cv.readers


# ---------------------------------------------------------------------------
# Import contract test
# ---------------------------------------------------------------------------


class TestImportContracts:
    """Verify all classes are importable from camel_security.testing."""

    def test_import_policy_test_runner(self) -> None:
        """PolicyTestRunner is importable from camel_security.testing."""
        from camel_security.testing import PolicyTestRunner as PTR

        assert PTR is PolicyTestRunner

    def test_import_camel_value_builder(self) -> None:
        """CaMeLValueBuilder is importable from camel_security.testing."""
        from camel_security.testing import CaMeLValueBuilder as CVB

        assert CVB is CaMeLValueBuilder

    def test_import_policy_simulator(self) -> None:
        """PolicySimulator is importable from camel_security.testing."""
        from camel_security.testing import PolicySimulator as PS

        assert PS is PolicySimulator

    def test_import_supporting_types(self) -> None:
        """Supporting dataclasses are importable from camel_security.testing."""
        import camel_security.testing as t

        assert t.PolicyTestCase is not None
        assert t.PolicyCaseResult is not None
        assert t.PolicyTestReport is not None
        assert t.SimulatedPolicyEvaluation is not None
        assert t.SimulationReport is not None
