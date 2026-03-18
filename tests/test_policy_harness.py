"""Tests for the CaMeL policy testing harness.

Validates that every harness primitive works correctly:

* Value factories produce :class:`~camel.value.CaMeLValue` instances with the
  expected capability tags and pass/fail :func:`~camel.policy.interfaces.is_trusted`.
* :func:`~tests.harness.policy_harness.assert_allowed` passes for
  :class:`~camel.policy.interfaces.Allowed` and fails for
  :class:`~camel.policy.interfaces.Denied`.
* :func:`~tests.harness.policy_harness.assert_denied` passes for
  :class:`~camel.policy.interfaces.Denied` (with optional reason match) and
  fails for :class:`~camel.policy.interfaces.Allowed`.
* :func:`~tests.harness.policy_harness.assert_policy_allowed` and
  :func:`~tests.harness.policy_harness.assert_policy_denied` accept a plain
  policy function and behave identically.
* :class:`~tests.harness.policy_harness.PolicyTestCase` provides an isolated
  registry and interpreter per test, with in-memory audit log capture.
* :func:`~tests.harness.policy_harness.replay_agentdojo_scenario_through_hook`
  executes at least four adversarial scenarios through the live interpreter
  enforcement hook, asserting
  :class:`~camel.interpreter.PolicyViolationError` is raised and the audit
  log captures denial entries with correct ``tool_name`` and ``outcome``.

NFR-9 compliance: all tests are synchronous, deterministic, and do not touch
production UI components or the file-system audit log.
"""

from __future__ import annotations

import pytest

from camel.interpreter import CaMeLInterpreter, EnforcementMode, PolicyViolationError
from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyRegistry,
    is_trusted,
)
from camel.policy.reference_policies import (
    configure_reference_policies,
    fetch_external_url_policy,
    send_email_policy,
    send_money_policy,
)
from camel.value import CaMeLValue, Public, wrap
from tests.harness.policy_harness import (
    AGENTDOJO_SCENARIOS,
    PolicyTestCase,
    assert_allowed,
    assert_denied,
    assert_policy_allowed,
    assert_policy_denied,
    make_mixed_value,
    make_trusted_value,
    make_untrusted_value,
    replay_agentdojo_scenario,
    replay_agentdojo_scenario_through_hook,
)

# ===========================================================================
# 1. Value factory helpers
# ===========================================================================


class TestMakeTrustedValue:
    """Tests for :func:`make_trusted_value`."""

    def test_default_source_is_user_literal(self) -> None:
        """make_trusted_value defaults to 'User literal' source."""
        cv = make_trusted_value("hello")
        assert cv.sources == frozenset({"User literal"})

    def test_custom_trusted_source(self) -> None:
        """make_trusted_value accepts a custom source label."""
        cv = make_trusted_value(42, source="CaMeL")
        assert cv.sources == frozenset({"CaMeL"})

    def test_is_trusted_returns_true(self) -> None:
        """is_trusted() returns True for make_trusted_value() output."""
        cv = make_trusted_value("alice@example.com")
        assert is_trusted(cv) is True

    def test_camel_source_is_trusted(self) -> None:
        """Values with source='CaMeL' are also trusted."""
        cv = make_trusted_value(99, source="CaMeL")
        assert is_trusted(cv) is True

    def test_default_readers_is_public(self) -> None:
        """Default readers is Public (unrestricted)."""
        cv = make_trusted_value("x")
        assert cv.readers is Public

    def test_custom_readers(self) -> None:
        """Custom readers frozenset is preserved."""
        cv = make_trusted_value("secret", readers=frozenset({"alice@example.com"}))
        assert cv.readers == frozenset({"alice@example.com"})

    def test_raw_value_preserved(self) -> None:
        """The raw Python value is accessible via .raw."""
        cv = make_trusted_value({"key": "val"})
        assert cv.raw == {"key": "val"}


class TestMakeUntrustedValue:
    """Tests for :func:`make_untrusted_value`."""

    def test_default_source_label(self) -> None:
        """make_untrusted_value defaults to 'external_tool' source."""
        cv = make_untrusted_value("data")
        assert cv.sources == frozenset({"external_tool"})

    def test_custom_source_label(self) -> None:
        """make_untrusted_value accepts a custom source label."""
        cv = make_untrusted_value("evil@bad.com", source="read_email")
        assert cv.sources == frozenset({"read_email"})

    def test_is_trusted_returns_false(self) -> None:
        """is_trusted() returns False for make_untrusted_value() output."""
        cv = make_untrusted_value("evil@bad.com", source="read_email")
        assert is_trusted(cv) is False

    def test_external_tool_source_is_untrusted(self) -> None:
        """The default 'external_tool' source is not trusted."""
        cv = make_untrusted_value("data")
        assert is_trusted(cv) is False

    def test_raw_value_preserved(self) -> None:
        """The raw Python value is accessible via .raw."""
        cv = make_untrusted_value(123, source="some_tool")
        assert cv.raw == 123

    def test_default_readers_is_public(self) -> None:
        """Default readers is Public."""
        cv = make_untrusted_value("x")
        assert cv.readers is Public


class TestMakeMixedValue:
    """Tests for :func:`make_mixed_value`."""

    def test_sources_are_union_of_trusted_and_untrusted(self) -> None:
        """sources = trusted_sources | untrusted_sources."""
        cv = make_mixed_value(
            "data",
            trusted_sources=frozenset({"User literal"}),
            untrusted_sources=frozenset({"read_email"}),
        )
        assert cv.sources == frozenset({"User literal", "read_email"})

    def test_is_trusted_returns_false_for_mixed(self) -> None:
        """Mixed values are NOT trusted (any untrusted source taints the value)."""
        cv = make_mixed_value(
            "data",
            trusted_sources=frozenset({"User literal"}),
            untrusted_sources=frozenset({"read_email"}),
        )
        assert is_trusted(cv) is False


# ===========================================================================
# 2. Registry-based assertion helpers
# ===========================================================================


class TestAssertAllowed:
    """Tests for :func:`assert_allowed`."""

    def test_passes_when_policy_returns_allowed(self) -> None:
        """assert_allowed does not raise when the policy returns Allowed."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Allowed())
        # Should not raise
        assert_allowed(registry, "my_tool", {})

    def test_fails_when_policy_returns_denied(self) -> None:
        """assert_allowed raises AssertionError when policy returns Denied."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("blocked"))
        with pytest.raises(AssertionError, match="Expected Allowed"):
            assert_allowed(registry, "my_tool", {})

    def test_failure_message_includes_tool_name(self) -> None:
        """AssertionError message includes the tool name."""
        registry = PolicyRegistry()
        registry.register("send_email", lambda tn, kw: Denied("no"))
        with pytest.raises(AssertionError, match="send_email"):
            assert_allowed(registry, "send_email", {})

    def test_passes_for_tool_with_no_policies(self) -> None:
        """assert_allowed passes when no policies are registered (implicit allow)."""
        registry = PolicyRegistry()
        assert_allowed(registry, "unregistered_tool", {})

    def test_passes_with_trusted_kwargs(self) -> None:
        """assert_allowed works with real CaMeLValue kwargs."""
        registry = PolicyRegistry()
        registry.register("send_email", send_email_policy)
        kwargs = {"to": make_trusted_value("alice@example.com")}
        assert_allowed(registry, "send_email", kwargs)


class TestAssertDenied:
    """Tests for :func:`assert_denied`."""

    def test_passes_when_policy_returns_denied(self) -> None:
        """assert_denied does not raise when the policy returns Denied."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("blocked"))
        result = assert_denied(registry, "my_tool", {})
        assert isinstance(result, Denied)

    def test_fails_when_policy_returns_allowed(self) -> None:
        """assert_denied raises AssertionError when policy returns Allowed."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Allowed())
        with pytest.raises(AssertionError, match="Expected Denied"):
            assert_denied(registry, "my_tool", {})

    def test_failure_message_includes_tool_name(self) -> None:
        """AssertionError message includes the tool name."""
        registry = PolicyRegistry()
        registry.register("send_email", lambda tn, kw: Allowed())
        with pytest.raises(AssertionError, match="send_email"):
            assert_denied(registry, "send_email", {})

    def test_reason_contains_check_passes(self) -> None:
        """reason_contains check passes when substring is present."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("recipient untrusted"))
        result = assert_denied(
            registry, "my_tool", {}, reason_contains="untrusted"
        )
        assert "untrusted" in result.reason

    def test_reason_contains_check_case_insensitive(self) -> None:
        """reason_contains check is case-insensitive."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("Recipient UNTRUSTED"))
        result = assert_denied(
            registry, "my_tool", {}, reason_contains="untrusted"
        )
        assert result.reason == "Recipient UNTRUSTED"

    def test_reason_contains_check_fails_when_missing(self) -> None:
        """reason_contains check raises AssertionError when fragment is absent."""
        registry = PolicyRegistry()
        registry.register("my_tool", lambda tn, kw: Denied("policy violation"))
        with pytest.raises(AssertionError, match="untrusted"):
            assert_denied(registry, "my_tool", {}, reason_contains="untrusted")

    def test_returns_denied_instance(self) -> None:
        """assert_denied returns the Denied result for further assertions."""
        registry = PolicyRegistry()
        registry.register("send_email", send_email_policy)
        # Untrusted recipient + body the recipient can't read → Denied
        kwargs = {
            "to": make_untrusted_value("evil@bad.com", source="read_email"),
            "body": wrap(
                "secret report",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        }
        result = assert_denied(registry, "send_email", kwargs)
        assert isinstance(result, Denied)
        assert result.reason != ""


# ===========================================================================
# 3. Policy-function-based assertion helpers
# ===========================================================================


class TestAssertPolicyAllowed:
    """Tests for :func:`assert_policy_allowed`."""

    def test_passes_when_policy_returns_allowed(self) -> None:
        """assert_policy_allowed passes when the policy function returns Allowed."""
        def always_allow(tn: str, kw: object) -> Allowed:
            return Allowed()

        assert_policy_allowed(always_allow, "my_tool", {})

    def test_fails_when_policy_returns_denied(self) -> None:
        """assert_policy_allowed raises AssertionError when policy returns Denied."""
        def always_deny(tn: str, kw: object) -> Denied:
            return Denied("blocked")

        with pytest.raises(AssertionError, match="Expected Allowed"):
            assert_policy_allowed(always_deny, "my_tool", {})

    def test_passes_for_send_email_trusted_recipient(self) -> None:
        """assert_policy_allowed passes for send_email with a trusted recipient."""
        kwargs = {
            "to": make_trusted_value("alice@example.com"),
            "body": make_trusted_value("Hello!"),
        }
        assert_policy_allowed(send_email_policy, "send_email", kwargs)

    def test_passes_for_fetch_url_trusted(self) -> None:
        """assert_policy_allowed passes for fetch_external_url with trusted URL."""
        kwargs = {"url": make_trusted_value("https://api.example.com/data")}
        assert_policy_allowed(fetch_external_url_policy, "fetch_external_url", kwargs)

    def test_invokes_policy_with_correct_tool_name(self) -> None:
        """The tool_name argument is forwarded to the policy function."""
        received: list[str] = []

        def capture_tool_name(tn: str, kw: object) -> Allowed:
            received.append(tn)
            return Allowed()

        assert_policy_allowed(capture_tool_name, "my_specific_tool", {})
        assert received == ["my_specific_tool"]


class TestAssertPolicyDenied:
    """Tests for :func:`assert_policy_denied`."""

    def test_passes_when_policy_returns_denied(self) -> None:
        """assert_policy_denied passes when the policy function returns Denied."""
        def always_deny(tn: str, kw: object) -> Denied:
            return Denied("blocked")

        result = assert_policy_denied(always_deny, "my_tool", {})
        assert isinstance(result, Denied)

    def test_fails_when_policy_returns_allowed(self) -> None:
        """assert_policy_denied raises AssertionError when policy returns Allowed."""
        def always_allow(tn: str, kw: object) -> Allowed:
            return Allowed()

        with pytest.raises(AssertionError, match="Expected Denied"):
            assert_policy_denied(always_allow, "my_tool", {})

    def test_expected_reason_passes(self) -> None:
        """expected_reason check passes when substring is present."""
        def deny_with_reason(tn: str, kw: object) -> Denied:
            return Denied("untrusted recipient detected")

        result = assert_policy_denied(
            deny_with_reason, "my_tool", {}, expected_reason="untrusted"
        )
        assert "untrusted" in result.reason

    def test_expected_reason_fails_when_missing(self) -> None:
        """expected_reason check raises when fragment is absent from reason."""
        def deny_vague(tn: str, kw: object) -> Denied:
            return Denied("policy violation")

        with pytest.raises(AssertionError):
            assert_policy_denied(
                deny_vague, "my_tool", {}, expected_reason="untrusted"
            )

    def test_passes_for_send_email_injected_recipient(self) -> None:
        """assert_policy_denied passes when untrusted recipient is injected."""
        # Need a body with restricted readers so the policy can deny
        kwargs = {
            "to": make_untrusted_value("evil@bad.com", source="read_email"),
            "body": wrap(
                "secret report",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        }
        result = assert_policy_denied(
            send_email_policy,
            "send_email",
            kwargs,
            expected_reason="untrusted",
        )
        assert isinstance(result, Denied)

    def test_passes_for_send_money_injected_amount(self) -> None:
        """assert_policy_denied passes when untrusted amount is injected."""
        kwargs = {
            "recipient": make_trusted_value("bob@example.com"),
            "amount": make_untrusted_value(99999.99, source="read_document"),
        }
        result = assert_policy_denied(
            send_money_policy,
            "send_money",
            kwargs,
            expected_reason="untrusted",
        )
        assert isinstance(result, Denied)

    def test_passes_for_fetch_external_url_injected_url(self) -> None:
        """assert_policy_denied passes for untrusted URL in fetch_external_url."""
        kwargs = {
            "url": make_untrusted_value(
                "https://attacker.com/exfil", source="read_email"
            )
        }
        result = assert_policy_denied(
            fetch_external_url_policy,
            "fetch_external_url",
            kwargs,
            expected_reason="untrusted",
        )
        assert isinstance(result, Denied)


# ===========================================================================
# 4. PolicyTestCase base class
# ===========================================================================


class TestPolicyTestCaseBase(PolicyTestCase):
    """Verify PolicyTestCase provides an isolated registry and interpreter."""

    def test_registry_is_policy_registry_instance(self) -> None:
        """self.registry is a PolicyRegistry instance."""
        assert isinstance(self.registry, PolicyRegistry)

    def test_registry_has_all_six_reference_policies(self) -> None:
        """The registry contains all six reference policy tool names."""
        expected_tools = {
            "send_email",
            "send_money",
            "create_calendar_event",
            "write_file",
            "post_message",
            "fetch_external_url",
        }
        assert expected_tools <= self.registry.registered_tools()

    def test_interp_is_camel_interpreter(self) -> None:
        """self.interp is a CaMeLInterpreter instance."""
        assert isinstance(self.interp, CaMeLInterpreter)

    def test_audit_log_starts_empty(self) -> None:
        """audit_log is empty before any tool calls."""
        assert self.audit_log == []

    def test_registry_is_isolated_per_test(self) -> None:
        """Each test gets a fresh registry — mutations don't bleed over."""
        # Register an extra policy in this test; it must not appear elsewhere.
        from camel.policy.interfaces import Allowed

        self.registry.register("extra_tool", lambda tn, kw: Allowed())
        assert "extra_tool" in self.registry.registered_tools()

    def test_evaluation_mode_raises_policy_violation_error(self) -> None:
        """Interpreter in EVALUATION mode raises PolicyViolationError on denial."""
        def _blocked_tool(**_kw: object) -> CaMeLValue:
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = CaMeLInterpreter(
            tools={"send_email": _blocked_tool},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )
        # Untrusted recipient + restricted body → policy denies.
        interp._store["_to"] = make_untrusted_value(  # type: ignore[attr-defined]
            "evil@bad.com", source="read_email"
        )
        interp._store["_body"] = wrap(  # type: ignore[attr-defined]
            "secret",
            sources=frozenset({"User literal"}),
            readers=frozenset({"alice@example.com"}),
        )
        with pytest.raises(PolicyViolationError):
            interp.exec("r = send_email(to=_to, body=_body)")

    def test_audit_log_captures_denied_entry(self) -> None:
        """audit_log captures a Denied entry when policy blocks a tool call."""
        def _blocked_tool(**_kw: object) -> CaMeLValue:
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = CaMeLInterpreter(
            tools={"send_email": _blocked_tool},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )
        # Untrusted recipient + restricted body → denial.
        interp._store["_to"] = make_untrusted_value(  # type: ignore[attr-defined]
            "evil@bad.com", source="read_email"
        )
        interp._store["_body"] = wrap(  # type: ignore[attr-defined]
            "secret",
            sources=frozenset({"User literal"}),
            readers=frozenset({"alice@example.com"}),
        )
        try:
            interp.exec("r = send_email(to=_to, body=_body)")
        except PolicyViolationError:
            pass

        log = interp.audit_log
        assert any(
            getattr(e, "tool_name", None) == "send_email"
            and getattr(e, "outcome", None) == "Denied"
            for e in log
        ), f"Expected Denied entry in audit log, got: {log!r}"


# ===========================================================================
# 5. Pytest fixtures (smoke test)
# ===========================================================================


def test_reference_policy_registry_fixture(
    reference_policy_registry: PolicyRegistry,
) -> None:
    """reference_policy_registry fixture provides a loaded PolicyRegistry."""
    assert isinstance(reference_policy_registry, PolicyRegistry)
    assert "send_email" in reference_policy_registry.registered_tools()
    assert "send_money" in reference_policy_registry.registered_tools()
    assert "fetch_external_url" in reference_policy_registry.registered_tools()


def test_policy_test_interpreter_fixture(
    policy_test_interpreter: CaMeLInterpreter,
) -> None:
    """policy_test_interpreter fixture provides a CaMeLInterpreter in EVALUATION mode."""
    assert isinstance(policy_test_interpreter, CaMeLInterpreter)
    # Audit log starts empty.
    assert policy_test_interpreter.audit_log == []


def test_fixture_audit_log_captures_allowed_entry(
    policy_test_interpreter: CaMeLInterpreter,
    reference_policy_registry: PolicyRegistry,
) -> None:
    """Audit log captures an Allowed entry via the fixture-provided interpreter."""
    def _send_email_tool(**_kw: object) -> CaMeLValue:
        return wrap("sent", sources=frozenset({"CaMeL"}))

    # Build a fresh interpreter with the tool registered.
    interp = CaMeLInterpreter(
        tools={"send_email": _send_email_tool},
        policy_engine=reference_policy_registry,
        enforcement_mode=EnforcementMode.EVALUATION,
    )
    interp._store["_to"] = make_trusted_value(  # type: ignore[attr-defined]
        "alice@example.com"
    )
    interp.exec("r = send_email(to=_to)")

    log = interp.audit_log
    assert any(
        getattr(e, "tool_name", None) == "send_email"
        and getattr(e, "outcome", None) == "Allowed"
        for e in log
    ), f"Expected Allowed entry in audit log, got: {log!r}"


# ===========================================================================
# 6. AgentDojo scenario replay — registry level
# ===========================================================================


class TestReplayAgentdojoScenario:
    """Tests for :func:`replay_agentdojo_scenario` (registry-level)."""

    @pytest.fixture(autouse=True)
    def _setup_registry(self) -> None:
        """Build a fresh registry for each test."""
        self.registry = PolicyRegistry()
        configure_reference_policies(self.registry, file_owner="alice@example.com")

    def test_send_email_injected_recipient_is_denied(self) -> None:
        """send_email_injected_recipient scenario results in Denied at registry level."""
        scenario = next(
            s for s in AGENTDOJO_SCENARIOS
            if s.scenario_id == "send_email_injected_recipient"
        )
        replay_agentdojo_scenario(self.registry, scenario)  # must not raise

    def test_send_email_trusted_recipient_is_allowed(self) -> None:
        """send_email_trusted_recipient scenario results in Allowed."""
        scenario = next(
            s for s in AGENTDOJO_SCENARIOS
            if s.scenario_id == "send_email_trusted_recipient"
        )
        replay_agentdojo_scenario(self.registry, scenario)  # must not raise

    def test_send_money_injected_amount_is_denied(self) -> None:
        """send_money_injected_amount scenario results in Denied."""
        scenario = next(
            s for s in AGENTDOJO_SCENARIOS
            if s.scenario_id == "send_money_injected_amount"
        )
        replay_agentdojo_scenario(self.registry, scenario)

    def test_fetch_external_url_injected_url_is_denied(self) -> None:
        """fetch_external_url_injected_url scenario results in Denied."""
        scenario = next(
            s for s in AGENTDOJO_SCENARIOS
            if s.scenario_id == "fetch_external_url_injected_url"
        )
        replay_agentdojo_scenario(self.registry, scenario)

    def test_entire_catalogue_runs_without_assertion_errors(self) -> None:
        """All entries in AGENTDOJO_SCENARIOS replay without AssertionError."""
        for scenario in AGENTDOJO_SCENARIOS:
            replay_agentdojo_scenario(self.registry, scenario)


# ===========================================================================
# 7. AgentDojo scenario replay — live enforcement hook
# ===========================================================================


@pytest.fixture()
def full_registry() -> PolicyRegistry:
    """Fresh registry with all six reference policies (file_owner=alice)."""
    registry = PolicyRegistry()
    configure_reference_policies(registry, file_owner="alice@example.com")
    return registry


class TestReplayThroughHook:
    """Tests for :func:`replay_agentdojo_scenario_through_hook`.

    Each test verifies that the named adversarial scenario:
    1. Causes :class:`~camel.interpreter.PolicyViolationError` to be raised
       by the live interpreter enforcement hook.
    2. Results in a ``Denied`` entry in the audit log with the correct
       ``tool_name``.
    """

    @pytest.fixture(autouse=True)
    def _setup_registry(self) -> None:
        """Build a fresh registry for each test."""
        self.registry = PolicyRegistry()
        configure_reference_policies(self.registry, file_owner="alice@example.com")

    # --- Required minimum four adversarial scenarios ---

    def test_send_email_injected_recipient_raises_policy_violation(self) -> None:
        """send_email recipient injection raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "send_email_injected_recipient", self.registry
        )

    def test_send_money_injected_amount_raises_policy_violation(self) -> None:
        """send_money amount manipulation raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "send_money_injected_amount", self.registry
        )

    def test_fetch_external_url_injected_url_raises_policy_violation(self) -> None:
        """fetch_external_url with untrusted URL raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "fetch_external_url_injected_url", self.registry
        )

    def test_post_message_data_to_control_escalation_raises_policy_violation(
        self,
    ) -> None:
        """Data-to-control escalation via post_message raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "post_message_data_to_control_escalation", self.registry
        )

    # --- Additional scenarios ---

    def test_send_money_injected_recipient_raises_policy_violation(self) -> None:
        """send_money recipient injection raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "send_money_injected_recipient", self.registry
        )

    def test_write_file_injected_path_raises_policy_violation(self) -> None:
        """write_file path injection raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "write_file_injected_path", self.registry
        )

    def test_post_message_injected_channel_raises_policy_violation(self) -> None:
        """post_message channel injection raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "post_message_injected_channel", self.registry
        )

    def test_send_money_data_to_control_escalation_raises_policy_violation(
        self,
    ) -> None:
        """Full data-to-control escalation via send_money raises PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "send_money_data_to_control_escalation", self.registry
        )

    def test_benign_send_email_does_not_raise(self) -> None:
        """Trusted send_email does NOT raise PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "send_email_trusted_recipient", self.registry
        )

    def test_benign_fetch_url_does_not_raise(self) -> None:
        """Trusted fetch_external_url does NOT raise PolicyViolationError."""
        replay_agentdojo_scenario_through_hook(
            "fetch_external_url_trusted_url", self.registry
        )

    def test_unknown_scenario_id_raises_key_error(self) -> None:
        """Passing an unknown scenario_id raises KeyError."""
        with pytest.raises(KeyError, match="no_such_scenario"):
            replay_agentdojo_scenario_through_hook(
                "no_such_scenario", self.registry
            )

    def test_audit_log_has_denied_entry_for_injected_recipient(self) -> None:
        """Verifying audit log directly: send_email injection leaves a Denied entry."""
        # Manually run the enforcement hook and verify the audit log.
        def _dummy_send_email(**_kw: object) -> CaMeLValue:
            return wrap("sent", sources=frozenset({"CaMeL"}))

        interp = CaMeLInterpreter(
            tools={"send_email": _dummy_send_email},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )
        to_cv = make_untrusted_value("attacker@evil.com", source="read_email")
        subj_cv = make_trusted_value("Q4 Report")
        # Body has restricted readers — attacker can't read it → policy denies.
        body_cv = wrap(
            "Confidential financials",
            sources=frozenset({"User literal"}),
            readers=frozenset({"alice@example.com"}),
        )
        interp._store["_to"] = to_cv  # type: ignore[attr-defined]
        interp._store["_subject"] = subj_cv  # type: ignore[attr-defined]
        interp._store["_body"] = body_cv  # type: ignore[attr-defined]

        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec("r = send_email(to=_to, subject=_subject, body=_body)")

        # Verify audit log
        log = interp.audit_log
        denied_entries = [
            e for e in log
            if getattr(e, "tool_name", None) == "send_email"
            and getattr(e, "outcome", None) == "Denied"
        ]
        assert denied_entries, (
            f"Expected 'Denied' audit entry for send_email, got: {log!r}"
        )

        # Verify policy violation error reason
        assert "untrusted" in exc_info.value.reason.lower(), (
            f"Expected 'untrusted' in reason, got: {exc_info.value.reason!r}"
        )


# ===========================================================================
# 8. Audit log entries — content verification
# ===========================================================================


class TestAuditLogEntryContent:
    """Verify that AuditLogEntry fields are populated correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        """Build a fresh registry for each test."""
        self.registry = PolicyRegistry()
        configure_reference_policies(self.registry, file_owner="alice@example.com")

    def _make_interp(self, tools: dict) -> CaMeLInterpreter:
        return CaMeLInterpreter(
            tools=tools,
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )

    def test_allowed_entry_fields(self) -> None:
        """Allowed audit entry has correct tool_name, outcome=Allowed, reason=None."""
        def _tool(**_kw: object) -> CaMeLValue:
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = self._make_interp({"send_email": _tool})
        interp._store["_to"] = make_trusted_value("alice@example.com")  # type: ignore[attr-defined]
        interp.exec("r = send_email(to=_to)")

        log = interp.audit_log
        allowed = next(
            (e for e in log if getattr(e, "outcome", None) == "Allowed"), None
        )
        assert allowed is not None
        assert getattr(allowed, "tool_name") == "send_email"
        assert getattr(allowed, "reason") is None
        assert getattr(allowed, "timestamp") is not None

    def test_denied_entry_fields(self) -> None:
        """Denied audit entry has correct tool_name, outcome=Denied, reason string."""
        def _tool(**_kw: object) -> CaMeLValue:
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = self._make_interp({"send_email": _tool})
        # Untrusted recipient + restricted body → denial.
        interp._store["_to"] = make_untrusted_value(  # type: ignore[attr-defined]
            "evil@bad.com", source="read_email"
        )
        interp._store["_body"] = wrap(  # type: ignore[attr-defined]
            "private content",
            sources=frozenset({"User literal"}),
            readers=frozenset({"alice@example.com"}),
        )
        try:
            interp.exec("r = send_email(to=_to, body=_body)")
        except PolicyViolationError:
            pass

        log = interp.audit_log
        denied = next(
            (e for e in log if getattr(e, "outcome", None) == "Denied"), None
        )
        assert denied is not None
        assert getattr(denied, "tool_name") == "send_email"
        assert getattr(denied, "reason") is not None
        assert getattr(denied, "reason") != ""
        assert getattr(denied, "consent_decision") is None  # EVALUATION mode
