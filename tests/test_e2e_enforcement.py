"""End-to-end enforcement integration and NFR compliance test suite.

Covers the five test categories required by the sprint acceptance criteria:

1. **E2E pipeline tests** — happy-path (Allowed, tool executes, audit log entry
   written) and attack-path (Denied, PolicyViolationError raised, audit log
   entry written) across all six reference policies.
2. **Audit log completeness** — 10 mixed-outcome tool calls produce exactly 10
   audit entries, each with the required fields (timestamp, tool_name,
   policy_name placeholder, result, reason).
3. **NFR-4 performance** — 50 consecutive tool calls in EVALUATION mode; median
   (p50) and 95th-percentile (p95) interpreter overhead ≤ 100 ms.
4. **NFR-9 independence** — the policy engine, capability system, and
   enforcement hook can each be instantiated and exercised in complete
   isolation with no interpreter, no LLM, and no production UI.
5. **NFR-2 determinism** — 100 identical runs of the same policy evaluation
   produce identical results; zero real LLM calls are made (verified via
   ``unittest.mock``).
6. **Production-mode consent flow** — approval path resumes the tool call;
   rejection path raises :class:`~camel.interpreter.PolicyViolationError` with
   ``consent_decision="UserRejected"``.

All tests are synchronous and deterministic; no external service dependencies.
Run the full suite with::

    pytest tests/test_e2e_enforcement.py -v
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from camel.interpreter import (
    AuditLogEntry,
    CaMeLInterpreter,
    EnforcementMode,
    PolicyViolationError,
)
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
from tests.harness.policy_harness import make_trusted_value, make_untrusted_value

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

FILE_OWNER = "alice@example.com"


def _make_registry() -> PolicyRegistry:
    """Return a fresh registry with all six reference policies registered."""
    registry = PolicyRegistry()
    configure_reference_policies(registry, file_owner=FILE_OWNER)
    return registry


def _ok_tool(**_kw: object) -> CaMeLValue:
    """Dummy tool that always succeeds and returns a trusted CaMeLValue."""
    return wrap("ok", sources=frozenset({"CaMeL"}), readers=Public)


def _make_interp(
    tools: dict[str, Callable[..., CaMeLValue]],
    registry: PolicyRegistry,
    enforcement_mode: EnforcementMode = EnforcementMode.EVALUATION,
    consent_callback: Callable[..., bool] | None = None,
) -> CaMeLInterpreter:
    """Build an interpreter wired to the given registry and enforcement mode."""
    return CaMeLInterpreter(
        tools=tools,
        policy_engine=registry,
        enforcement_mode=enforcement_mode,
        consent_callback=consent_callback,
    )


def _seed(interp: CaMeLInterpreter, **kwargs: CaMeLValue) -> None:
    """Inject pre-built CaMeLValue objects directly into the interpreter store."""
    for name, value in kwargs.items():
        interp._store[name] = value  # type: ignore[attr-defined]


# ===========================================================================
# 1. End-to-end pipeline tests — all six reference policies
# ===========================================================================


class TestE2EPipelineSendEmail:
    """E2E tests for the ``send_email`` policy via the interpreter hook."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"send_email": _ok_tool}, self.registry)

    # --- happy path ---

    def test_happy_path_trusted_recipient_executes_tool(self) -> None:
        """Trusted recipient: tool executes and Allowed entry is logged."""
        _seed(
            self.interp,
            _to=make_trusted_value("alice@example.com"),
            _body=make_trusted_value("Hello Alice"),
        )
        self.interp.exec("result = send_email(to=_to, body=_body)")

        assert self.interp.get("result").raw == "ok"
        log = self.interp.audit_log
        assert any(e.tool_name == "send_email" and e.outcome == "Allowed" for e in log), (
            f"No Allowed entry found. Log: {log!r}"
        )

    def test_happy_path_audit_entry_has_required_fields(self) -> None:
        """Allowed audit entry carries tool_name, outcome, reason=None, timestamp."""
        _seed(self.interp, _to=make_trusted_value("alice@example.com"))
        self.interp.exec("r = send_email(to=_to)")

        entry = next(
            e
            for e in self.interp.audit_log
            if e.tool_name == "send_email" and e.outcome == "Allowed"
        )
        assert entry.tool_name == "send_email"
        assert entry.outcome == "Allowed"
        assert entry.reason is None
        assert entry.timestamp is not None
        assert entry.timestamp != ""

    # --- attack path ---

    def test_attack_path_injected_recipient_raises_policy_violation(self) -> None:
        """Injected recipient: PolicyViolationError is raised."""
        _seed(
            self.interp,
            _to=make_untrusted_value("attacker@evil.com", source="read_email"),
            _body=wrap(
                "Confidential data",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = send_email(to=_to, body=_body)")

        assert exc_info.value.tool_name == "send_email"
        assert "untrusted" in exc_info.value.reason.lower()

    def test_attack_path_denied_entry_logged_with_reason(self) -> None:
        """Injected recipient: Denied audit entry written with non-empty reason."""
        _seed(
            self.interp,
            _to=make_untrusted_value("attacker@evil.com", source="read_email"),
            _body=wrap(
                "Confidential data",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = send_email(to=_to, body=_body)")

        denied = next((e for e in self.interp.audit_log if e.outcome == "Denied"), None)
        assert denied is not None
        assert denied.tool_name == "send_email"
        assert denied.reason is not None
        assert denied.reason != ""

    def test_attack_path_tool_not_called_on_denial(self) -> None:
        """The tool callable must NOT be invoked when the policy denies the call."""
        call_count: list[int] = [0]

        def _counting_tool(**_kw: object) -> CaMeLValue:
            call_count[0] += 1
            return wrap("ok", sources=frozenset({"CaMeL"}))

        interp = _make_interp({"send_email": _counting_tool}, self.registry)
        _seed(
            interp,
            _to=make_untrusted_value("attacker@evil.com", source="read_email"),
            _body=wrap(
                "Secret",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )
        with pytest.raises(PolicyViolationError):
            interp.exec("r = send_email(to=_to, body=_body)")

        assert call_count[0] == 0, "Tool was called despite policy denial"


class TestE2EPipelineSendMoney:
    """E2E tests for the ``send_money`` policy via the interpreter hook."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"send_money": _ok_tool}, self.registry)

    def test_happy_path_trusted_recipient_and_amount(self) -> None:
        """Fully trusted recipient + amount: tool executes."""
        _seed(
            self.interp,
            _recipient=make_trusted_value("bob@example.com"),
            _amount=make_trusted_value(100.0),
        )
        self.interp.exec("r = send_money(recipient=_recipient, amount=_amount)")
        assert self.interp.get("r").raw == "ok"

        log = self.interp.audit_log
        assert any(e.tool_name == "send_money" and e.outcome == "Allowed" for e in log)

    def test_attack_path_injected_recipient(self) -> None:
        """Untrusted recipient: PolicyViolationError raised."""
        _seed(
            self.interp,
            _recipient=make_untrusted_value("attacker@evil.com", source="read_email"),
            _amount=make_trusted_value(500.0),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = send_money(recipient=_recipient, amount=_amount)")

        assert exc_info.value.tool_name == "send_money"
        log = self.interp.audit_log
        assert any(e.tool_name == "send_money" and e.outcome == "Denied" for e in log)

    def test_attack_path_injected_amount(self) -> None:
        """Untrusted amount: PolicyViolationError raised and Denied logged."""
        _seed(
            self.interp,
            _recipient=make_trusted_value("bob@example.com"),
            _amount=make_untrusted_value(99999.99, source="read_document"),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = send_money(recipient=_recipient, amount=_amount)")

        assert any(
            e.outcome == "Denied" and e.tool_name == "send_money" for e in self.interp.audit_log
        )


class TestE2EPipelineCreateCalendarEvent:
    """E2E tests for the ``create_calendar_event`` policy."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"create_calendar_event": _ok_tool}, self.registry)

    def test_happy_path_trusted_participants(self) -> None:
        """Trusted participants: event created, Allowed logged."""
        _seed(
            self.interp,
            _participants=make_trusted_value(["alice@example.com", "bob@example.com"]),
            _title=make_trusted_value("Quarterly Planning"),
        )
        self.interp.exec("r = create_calendar_event(participants=_participants, title=_title)")
        assert self.interp.get("r").raw == "ok"
        assert any(
            e.tool_name == "create_calendar_event" and e.outcome == "Allowed"
            for e in self.interp.audit_log
        )

    def test_attack_path_injected_participants_private_title(self) -> None:
        """Injected participants, private title: PolicyViolationError raised."""
        _seed(
            self.interp,
            _participants=make_untrusted_value(["attacker@evil.com"], source="read_email"),
            _title=wrap(
                "Confidential Q4 Strategy",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = create_calendar_event(participants=_participants, title=_title)")

        assert exc_info.value.tool_name == "create_calendar_event"
        assert any(e.outcome == "Denied" for e in self.interp.audit_log)


class TestE2EPipelineWriteFile:
    """E2E tests for the ``write_file`` policy."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"write_file": _ok_tool}, self.registry)

    def test_happy_path_trusted_path_owner_readable_content(self) -> None:
        """Trusted path + owner-readable content: write proceeds."""
        _seed(
            self.interp,
            _path=make_trusted_value("/tmp/report.txt"),
            _content=wrap(
                "Safe report data",
                sources=frozenset({"User literal"}),
                readers=frozenset({FILE_OWNER}),
            ),
        )
        self.interp.exec("r = write_file(path=_path, content=_content)")
        assert self.interp.get("r").raw == "ok"
        assert any(
            e.tool_name == "write_file" and e.outcome == "Allowed" for e in self.interp.audit_log
        )

    def test_happy_path_public_content(self) -> None:
        """Trusted path + public content: write proceeds."""
        _seed(
            self.interp,
            _path=make_trusted_value("/tmp/public.txt"),
            _content=make_trusted_value("public data"),
        )
        self.interp.exec("r = write_file(path=_path, content=_content)")
        assert any(e.outcome == "Allowed" for e in self.interp.audit_log)

    def test_attack_path_injected_path(self) -> None:
        """Untrusted path: PolicyViolationError raised and Denied logged."""
        _seed(
            self.interp,
            _path=make_untrusted_value("/etc/passwd", source="read_email"),
            _content=make_trusted_value("harmless"),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = write_file(path=_path, content=_content)")

        assert exc_info.value.tool_name == "write_file"
        assert any(
            e.tool_name == "write_file" and e.outcome == "Denied" for e in self.interp.audit_log
        )

    def test_attack_path_content_not_readable_by_owner(self) -> None:
        """Content with owner excluded from readers: write blocked."""
        _seed(
            self.interp,
            _path=make_trusted_value("/tmp/secret.txt"),
            _content=wrap(
                "Top secret",
                sources=frozenset({"read_email"}),
                readers=frozenset({"eve@evil.com"}),  # owner not included
            ),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = write_file(path=_path, content=_content)")

        assert any(e.outcome == "Denied" for e in self.interp.audit_log)


class TestE2EPipelinePostMessage:
    """E2E tests for the ``post_message`` policy."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"post_message": _ok_tool}, self.registry)

    def test_happy_path_trusted_channel_trusted_message(self) -> None:
        """Trusted channel + trusted message: post succeeds."""
        _seed(
            self.interp,
            _channel=make_trusted_value("#general"),
            _message=make_trusted_value("Hello team!"),
        )
        self.interp.exec("r = post_message(channel=_channel, message=_message)")
        assert self.interp.get("r").raw == "ok"
        assert any(
            e.tool_name == "post_message" and e.outcome == "Allowed" for e in self.interp.audit_log
        )

    def test_attack_path_injected_channel(self) -> None:
        """Injected channel identifier: PolicyViolationError raised."""
        _seed(
            self.interp,
            _channel=make_untrusted_value("#attacker-channel", source="read_email"),
            _message=make_trusted_value("Weekly update"),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = post_message(channel=_channel, message=_message)")

        assert exc_info.value.tool_name == "post_message"
        assert any(e.outcome == "Denied" for e in self.interp.audit_log)

    def test_attack_path_private_untrusted_message(self) -> None:
        """Untrusted message with restricted readers: exfiltration blocked."""
        _seed(
            self.interp,
            _channel=make_trusted_value("#general"),
            _message=wrap(
                "private internal data",
                sources=frozenset({"read_document"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = post_message(channel=_channel, message=_message)")

        assert any(e.outcome == "Denied" for e in self.interp.audit_log)


class TestE2EPipelineFetchExternalUrl:
    """E2E tests for the ``fetch_external_url`` policy."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()
        self.interp = _make_interp({"fetch_external_url": _ok_tool}, self.registry)

    def test_happy_path_trusted_url(self) -> None:
        """Trusted URL: fetch succeeds and Allowed logged."""
        _seed(
            self.interp,
            _url=make_trusted_value("https://api.example.com/data"),
        )
        self.interp.exec("r = fetch_external_url(url=_url)")
        assert self.interp.get("r").raw == "ok"
        assert any(
            e.tool_name == "fetch_external_url" and e.outcome == "Allowed"
            for e in self.interp.audit_log
        )

    def test_attack_path_injected_url(self) -> None:
        """Injected URL: SSRF blocked via PolicyViolationError."""
        _seed(
            self.interp,
            _url=make_untrusted_value(
                "https://attacker.example.com/steal?secret=1",
                source="read_email",
            ),
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            self.interp.exec("r = fetch_external_url(url=_url)")

        assert exc_info.value.tool_name == "fetch_external_url"
        assert "untrusted" in exc_info.value.reason.lower()
        assert any(e.outcome == "Denied" for e in self.interp.audit_log)

    def test_attack_path_injected_params(self) -> None:
        """Untrusted query params: data exfiltration blocked."""
        _seed(
            self.interp,
            _url=make_trusted_value("https://api.example.com/q"),
            _params=make_untrusted_value({"secret": "private_data"}, source="read_document"),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = fetch_external_url(url=_url, params=_params)")

        assert any(e.outcome == "Denied" for e in self.interp.audit_log)

    def test_attack_path_injected_body(self) -> None:
        """Untrusted request body: data exfiltration via POST blocked."""
        _seed(
            self.interp,
            _url=make_trusted_value("https://api.example.com/upload"),
            _body=make_untrusted_value("confidential_payload", source="read_file"),
        )
        with pytest.raises(PolicyViolationError):
            self.interp.exec("r = fetch_external_url(url=_url, body=_body)")

        assert any(e.outcome == "Denied" for e in self.interp.audit_log)


# ===========================================================================
# 2. Audit log completeness test — 10 mixed-outcome calls
# ===========================================================================


class TestAuditLogCompleteness:
    """Audit log must record every evaluation with all required fields.

    Runs 10 tool calls (5 Allowed + 5 Denied) through a single interpreter
    and verifies that the audit log contains exactly 10 entries, each carrying:
    - ``tool_name`` (non-empty string)
    - ``outcome`` (``"Allowed"`` or ``"Denied"``)
    - ``reason`` (``None`` for Allowed; non-empty string for Denied)
    - ``timestamp`` (non-empty ISO-8601 string)
    - ``consent_decision`` (``None`` in EVALUATION mode)
    """

    def _build_scenario(
        self,
    ) -> tuple[CaMeLInterpreter, list[tuple[str, bool]]]:
        """Return interpreter and list of (code_snippet, expected_allowed) pairs."""
        registry = _make_registry()
        tools: dict[str, Callable[..., CaMeLValue]] = {
            "send_email": _ok_tool,
            "send_money": _ok_tool,
            "write_file": _ok_tool,
            "post_message": _ok_tool,
            "fetch_external_url": _ok_tool,
        }
        interp = _make_interp(tools, registry)

        # Pre-seed variables.
        _seed(
            interp,
            # Trusted values → 5 Allowed calls
            t_email=make_trusted_value("alice@example.com"),
            t_amount=make_trusted_value(50.0),
            t_recipient=make_trusted_value("bob@example.com"),
            t_path=make_trusted_value("/tmp/out.txt"),
            t_content=wrap(
                "data", sources=frozenset({"User literal"}), readers=frozenset({FILE_OWNER})
            ),
            t_channel=make_trusted_value("#general"),
            t_msg=make_trusted_value("Hi"),
            t_url=make_trusted_value("https://api.example.com"),
            # Untrusted values → 5 Denied calls
            u_email=make_untrusted_value("attacker@evil.com", source="read_email"),
            u_body=wrap(
                "private",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
            u_amount=make_untrusted_value(99999.0, source="read_document"),
            u_path=make_untrusted_value("/etc/cron.d/evil", source="read_email"),
            u_channel=make_untrusted_value("#data-leak", source="read_document"),
            u_url=make_untrusted_value("https://evil.com/steal", source="read_email"),
        )

        snippets_and_expected: list[tuple[str, bool]] = [
            # 5 Allowed calls
            ("r1 = send_email(to=t_email)", True),
            ("r2 = send_money(recipient=t_recipient, amount=t_amount)", True),
            ("r3 = write_file(path=t_path, content=t_content)", True),
            ("r4 = post_message(channel=t_channel, message=t_msg)", True),
            ("r5 = fetch_external_url(url=t_url)", True),
            # 5 Denied calls
            ("r6 = send_email(to=u_email, body=u_body)", False),
            ("r7 = send_money(recipient=t_recipient, amount=u_amount)", False),
            ("r8 = write_file(path=u_path, content=t_content)", False),
            ("r9 = post_message(channel=u_channel, message=t_msg)", False),
            ("r10 = fetch_external_url(url=u_url)", False),
        ]
        return interp, snippets_and_expected

    def test_ten_calls_produce_ten_audit_entries(self) -> None:
        """Exactly 10 audit log entries are produced for 10 tool calls."""
        interp, snippets = self._build_scenario()
        for code, expected_allowed in snippets:
            if expected_allowed:
                interp.exec(code)
            else:
                with pytest.raises(PolicyViolationError):
                    interp.exec(code)

        log = interp.audit_log
        assert len(log) == 10, f"Expected 10 audit entries, got {len(log)}: {log!r}"

    def test_all_entries_have_required_fields(self) -> None:
        """Every audit entry carries tool_name, outcome, reason, timestamp."""
        interp, snippets = self._build_scenario()
        for code, expected_allowed in snippets:
            if expected_allowed:
                interp.exec(code)
            else:
                with pytest.raises(PolicyViolationError):
                    interp.exec(code)

        for i, entry in enumerate(interp.audit_log):
            assert isinstance(entry, AuditLogEntry), f"Entry {i} is not an AuditLogEntry: {entry!r}"
            assert entry.tool_name and isinstance(entry.tool_name, str), (
                f"Entry {i} has empty/missing tool_name"
            )
            assert entry.outcome in ("Allowed", "Denied"), (
                f"Entry {i} has invalid outcome: {entry.outcome!r}"
            )
            assert isinstance(entry.timestamp, str) and entry.timestamp, (
                f"Entry {i} has empty/missing timestamp"
            )
            # reason is None for Allowed, non-empty str for Denied
            if entry.outcome == "Allowed":
                assert entry.reason is None, (
                    f"Entry {i} (Allowed) has non-None reason: {entry.reason!r}"
                )
            else:
                assert entry.reason and isinstance(entry.reason, str), (
                    f"Entry {i} (Denied) has empty/missing reason"
                )

    def test_five_allowed_five_denied_entries(self) -> None:
        """Exactly 5 Allowed and 5 Denied entries are recorded."""
        interp, snippets = self._build_scenario()
        for code, expected_allowed in snippets:
            if expected_allowed:
                interp.exec(code)
            else:
                with pytest.raises(PolicyViolationError):
                    interp.exec(code)

        log = interp.audit_log
        allowed_count = sum(1 for e in log if e.outcome == "Allowed")
        denied_count = sum(1 for e in log if e.outcome == "Denied")
        assert allowed_count == 5, f"Expected 5 Allowed, got {allowed_count}"
        assert denied_count == 5, f"Expected 5 Denied, got {denied_count}"

    def test_consent_decision_is_none_in_evaluation_mode(self) -> None:
        """In EVALUATION mode all audit entries have consent_decision=None."""
        interp, snippets = self._build_scenario()
        for code, expected_allowed in snippets:
            if expected_allowed:
                interp.exec(code)
            else:
                with pytest.raises(PolicyViolationError):
                    interp.exec(code)

        for entry in interp.audit_log:
            assert entry.consent_decision is None, (
                f"Expected consent_decision=None in EVALUATION mode, "
                f"got {entry.consent_decision!r} for {entry!r}"
            )

    def test_denied_entries_have_non_empty_reason(self) -> None:
        """All Denied audit entries have a non-empty human-readable reason."""
        interp, snippets = self._build_scenario()
        for code, expected_allowed in snippets:
            if expected_allowed:
                interp.exec(code)
            else:
                with pytest.raises(PolicyViolationError):
                    interp.exec(code)

        denied_entries = [e for e in interp.audit_log if e.outcome == "Denied"]
        assert len(denied_entries) == 5
        for entry in denied_entries:
            assert entry.reason is not None
            assert len(entry.reason) > 0


# ===========================================================================
# 3. NFR-4 performance test — p50 and p95 ≤ 100 ms per tool call
# ===========================================================================


class TestNFR4Performance:
    """NFR-4: interpreter overhead (including policy evaluation) ≤ 100 ms per call.

    Uses a real :class:`CaMeLInterpreter` in EVALUATION mode with all six
    reference policies active.  Measures wall-clock time for 50 sequential
    tool calls (alternating Allowed to avoid skewing from exception paths)
    and asserts both p50 and p95 are within the 100 ms budget.
    """

    _NUM_CALLS = 50
    _THRESHOLD_MS = 100.0  # NFR-4 budget per tool call

    def _run_benchmark(self) -> list[float]:
        """Execute _NUM_CALLS Allowed tool calls and return per-call ms timings."""
        registry = _make_registry()
        interp = _make_interp({"send_email": _ok_tool}, registry)
        _seed(interp, _to=make_trusted_value("alice@example.com"))

        timings: list[float] = []
        for _ in range(self._NUM_CALLS):
            # Reset the store result slot to avoid stale NameError from prior loops.
            start = time.perf_counter()
            interp.exec("r_perf = send_email(to=_to)")
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            timings.append(elapsed_ms)
        return timings

    def test_p50_interpreter_overhead_within_budget(self) -> None:
        """Median (p50) interpreter overhead ≤ 100 ms per tool call."""
        timings = self._run_benchmark()
        p50 = statistics.median(timings)
        assert p50 <= self._THRESHOLD_MS, (
            f"NFR-4 FAIL: p50={p50:.2f} ms exceeds budget of "
            f"{self._THRESHOLD_MS} ms.  All timings: {timings!r}"
        )

    def test_p95_interpreter_overhead_within_budget(self) -> None:
        """95th-percentile interpreter overhead ≤ 100 ms per tool call."""
        timings = self._run_benchmark()
        sorted_timings = sorted(timings)
        # p95 index: 95th percentile position
        idx = int(len(sorted_timings) * 0.95)
        p95 = sorted_timings[min(idx, len(sorted_timings) - 1)]
        assert p95 <= self._THRESHOLD_MS, (
            f"NFR-4 FAIL: p95={p95:.2f} ms exceeds budget of "
            f"{self._THRESHOLD_MS} ms.  Sorted timings: {sorted_timings!r}"
        )

    def test_benchmark_report_emitted(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Benchmark statistics are printed for observability (not a pass/fail gate)."""
        timings = self._run_benchmark()
        p50 = statistics.median(timings)
        sorted_t = sorted(timings)
        idx = int(len(sorted_t) * 0.95)
        p95 = sorted_t[min(idx, len(sorted_t) - 1)]
        print(
            f"\n[NFR-4 Benchmark] {self._NUM_CALLS} calls — "
            f"p50={p50:.3f} ms, p95={p95:.3f} ms, "
            f"min={min(timings):.3f} ms, max={max(timings):.3f} ms"
        )
        captured = capsys.readouterr()
        assert "NFR-4 Benchmark" in captured.out


# ===========================================================================
# 4. NFR-9 independence test — isolated component instantiation
# ===========================================================================


class TestNFR9Independence:
    """NFR-9: each component passes unit tests when instantiated in isolation.

    Tests that the policy engine, capability system, and enforcement hook can
    each be exercised without the others (no interpreter, no LLM).
    """

    # --- Policy engine in isolation ---

    def test_policy_engine_instantiates_without_interpreter(self) -> None:
        """PolicyRegistry can be constructed and used with no interpreter."""
        registry = PolicyRegistry()
        registry.register("send_email", send_email_policy)

        # Evaluate with trusted kwargs — should return Allowed.
        kwargs = {"to": make_trusted_value("alice@example.com")}
        result = registry.evaluate("send_email", kwargs)
        assert isinstance(result, Allowed)

    def test_policy_engine_denies_without_interpreter(self) -> None:
        """PolicyRegistry.evaluate returns Denied with no interpreter present."""
        registry = PolicyRegistry()
        registry.register("send_email", send_email_policy)

        kwargs = {
            "to": make_untrusted_value("attacker@evil.com", source="read_email"),
            "body": wrap(
                "private",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        }
        result = registry.evaluate("send_email", kwargs)
        assert isinstance(result, Denied)

    def test_all_six_policies_evaluate_in_isolation(self) -> None:
        """All six reference policies evaluate correctly without an interpreter."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)

        # send_email
        assert (
            registry.evaluate("send_email", {"to": make_trusted_value("alice@example.com")})
            == Allowed()
        )

        # send_money
        assert (
            registry.evaluate(
                "send_money",
                {
                    "recipient": make_trusted_value("bob@example.com"),
                    "amount": make_trusted_value(10.0),
                },
            )
            == Allowed()
        )

        # create_calendar_event
        assert (
            registry.evaluate(
                "create_calendar_event",
                {"participants": make_trusted_value(["alice@example.com"])},
            )
            == Allowed()
        )

        # write_file
        assert (
            registry.evaluate(
                "write_file",
                {
                    "path": make_trusted_value("/tmp/x.txt"),
                    "content": wrap(
                        "data",
                        sources=frozenset({"User literal"}),
                        readers=frozenset({FILE_OWNER}),
                    ),
                },
            )
            == Allowed()
        )

        # post_message
        assert (
            registry.evaluate(
                "post_message",
                {
                    "channel": make_trusted_value("#general"),
                    "message": make_trusted_value("Hello"),
                },
            )
            == Allowed()
        )

        # fetch_external_url
        assert (
            registry.evaluate(
                "fetch_external_url",
                {"url": make_trusted_value("https://api.example.com")},
            )
            == Allowed()
        )

    # --- Capability system in isolation ---

    def test_capability_system_without_interpreter_or_policy_engine(self) -> None:
        """CaMeLValue construction and is_trusted work with no other components."""
        trusted_cv = make_trusted_value("hello")
        assert trusted_cv.sources == frozenset({"User literal"})
        assert trusted_cv.readers is Public
        assert is_trusted(trusted_cv) is True

        untrusted_cv = make_untrusted_value("evil", source="read_email")
        assert is_trusted(untrusted_cv) is False

    def test_wrap_produces_correct_capability_tags(self) -> None:
        """wrap() produces CaMeLValue with correct sources and readers."""
        cv = wrap(
            "data",
            sources=frozenset({"read_email"}),
            readers=frozenset({"alice@example.com"}),
        )
        assert cv.sources == frozenset({"read_email"})
        assert cv.readers == frozenset({"alice@example.com"})
        assert not is_trusted(cv)

    # --- Enforcement hook in isolation ---

    def test_enforcement_hook_in_isolation_evaluation_mode(self) -> None:
        """CaMeLInterpreter enforcement hook works with no LLM present."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)

        # Build interpreter with only the enforcement hook; no LLM backend.
        interp = CaMeLInterpreter(
            tools={"send_email": _ok_tool},
            policy_engine=registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )
        _seed(interp, _to=make_trusted_value("alice@example.com"))
        interp.exec("r = send_email(to=_to)")

        log = interp.audit_log
        assert len(log) == 1
        assert log[0].outcome == "Allowed"

    def test_enforcement_hook_denial_in_isolation(self) -> None:
        """Enforcement hook raises PolicyViolationError with no LLM present."""
        registry = PolicyRegistry()
        registry.register("send_money", send_money_policy)

        interp = CaMeLInterpreter(
            tools={"send_money": _ok_tool},
            policy_engine=registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )
        _seed(
            interp,
            _recipient=make_untrusted_value("attacker@evil.com", source="read_email"),
            _amount=make_trusted_value(100.0),
        )
        with pytest.raises(PolicyViolationError):
            interp.exec("r = send_money(recipient=_recipient, amount=_amount)")

        log = interp.audit_log
        assert log[0].outcome == "Denied"

    def test_policy_function_itself_requires_no_other_components(self) -> None:
        """Individual policy functions run with no registry, interpreter, or LLM."""
        # send_email_policy standalone
        result = send_email_policy("send_email", {"to": make_trusted_value("alice@example.com")})
        assert result == Allowed()

        result_denied = send_email_policy(
            "send_email",
            {
                "to": make_untrusted_value("evil@bad.com", source="read_email"),
                "body": wrap(
                    "private",
                    sources=frozenset({"User literal"}),
                    readers=frozenset({"alice@example.com"}),
                ),
            },
        )
        assert isinstance(result_denied, Denied)

        # send_money_policy standalone
        result_money = send_money_policy(
            "send_money",
            {
                "recipient": make_trusted_value("bob@example.com"),
                "amount": make_trusted_value(42.0),
            },
        )
        assert result_money == Allowed()

        # fetch_external_url_policy standalone
        result_url = fetch_external_url_policy(
            "fetch_external_url",
            {"url": make_untrusted_value("https://evil.com", source="read_email")},
        )
        assert isinstance(result_url, Denied)


# ===========================================================================
# 5. NFR-2 determinism test — 100 identical runs, zero LLM invocations
# ===========================================================================


class TestNFR2Determinism:
    """NFR-2: policy evaluation is synchronous, deterministic, and LLM-free.

    Runs the same policy evaluation 100 times and asserts:
    - All 100 results are identical (either all Allowed or all Denied).
    - No LLM backend method is called during policy evaluation.
    """

    _NUM_RUNS = 100

    def _run_policy_100_times(
        self,
        tool_name: str,
        kwargs: dict[str, CaMeLValue],
        *,
        use_registry: bool = True,
    ) -> list[type]:
        """Return list of result types from 100 evaluations."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        results: list[type] = []
        for _ in range(self._NUM_RUNS):
            result = registry.evaluate(tool_name, kwargs)
            results.append(type(result))
        return results

    def test_allowed_result_is_deterministic_across_100_runs(self) -> None:
        """Allowed result is identical across 100 consecutive evaluations."""
        kwargs = {"to": make_trusted_value("alice@example.com")}
        results = self._run_policy_100_times("send_email", kwargs)
        assert all(r is Allowed for r in results), (
            f"Non-deterministic Allowed result: {set(r.__name__ for r in results)}"
        )

    def test_denied_result_is_deterministic_across_100_runs(self) -> None:
        """Denied result is identical across 100 consecutive evaluations."""
        kwargs = {
            "to": make_untrusted_value("attacker@evil.com", source="read_email"),
            "body": wrap(
                "private",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        }
        results = self._run_policy_100_times("send_email", kwargs)
        assert all(r is Denied for r in results), (
            f"Non-deterministic Denied result: {set(r.__name__ for r in results)}"
        )

    def test_zero_llm_calls_during_policy_evaluation(self) -> None:
        """Policy evaluation makes zero real LLM calls (verified via mock)."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)

        kwargs = {
            "to": make_untrusted_value("attacker@evil.com", source="read_email"),
            "body": wrap(
                "private",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        }

        # Patch the anthropic module's main client to detect any instantiation.
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            for _ in range(self._NUM_RUNS):
                registry.evaluate("send_email", kwargs)

        # The anthropic module should not have been used at all.
        mock_anthropic.Anthropic.assert_not_called()

    def test_zero_llm_calls_in_interpreter_enforcement_hook(self) -> None:
        """The interpreter enforcement hook never invokes an LLM (via mock)."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        interp = _make_interp({"send_email": _ok_tool}, registry)
        _seed(interp, _to=make_trusted_value("alice@example.com"))

        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            for _ in range(self._NUM_RUNS):
                # Reset result each iteration.
                interp.exec("r_det = send_email(to=_to)")

        mock_anthropic.Anthropic.assert_not_called()

    def test_denial_reasons_are_identical_across_100_runs(self) -> None:
        """Identical inputs produce bit-for-bit identical denial reasons."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)

        kwargs = {
            "recipient": make_untrusted_value("attacker@evil.com", source="read_email"),
            "amount": make_trusted_value(100.0),
        }
        reasons: list[str] = []
        for _ in range(self._NUM_RUNS):
            result = registry.evaluate("send_money", kwargs)
            assert isinstance(result, Denied)
            reasons.append(result.reason)

        # All reasons must be identical.
        assert len(set(reasons)) == 1, (
            f"Expected identical reasons across {self._NUM_RUNS} runs, "
            f"got {len(set(reasons))} distinct values: {set(reasons)!r}"
        )


# ===========================================================================
# 6. Production-mode consent flow tests
# ===========================================================================


class TestProductionModeConsentFlow:
    """Production-mode enforcement: approval resumes the call; rejection raises.

    Uses a :class:`ConsentCallback` mock to control the user decision.
    """

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = _make_registry()

    def _make_production_interp(
        self,
        approve: bool,
    ) -> tuple[CaMeLInterpreter, MagicMock]:
        """Build a PRODUCTION-mode interpreter with a mock consent callback."""
        callback = MagicMock(return_value=approve)
        interp = CaMeLInterpreter(
            tools={"send_email": _ok_tool},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=callback,
        )
        return interp, callback

    def _inject_denied_scenario(self, interp: CaMeLInterpreter) -> None:
        """Inject variables that will trigger a policy denial for send_email."""
        _seed(
            interp,
            _to=make_untrusted_value("attacker@evil.com", source="read_email"),
            _body=wrap(
                "Confidential report",
                sources=frozenset({"User literal"}),
                readers=frozenset({"alice@example.com"}),
            ),
        )

    # --- Approval path ---

    def test_approval_path_tool_executes_after_consent(self) -> None:
        """User approval in PRODUCTION mode allows the tool call to proceed."""
        interp, callback = self._make_production_interp(approve=True)
        self._inject_denied_scenario(interp)

        # Should NOT raise — user approved.
        interp.exec("r = send_email(to=_to, body=_body)")

        assert interp.get("r").raw == "ok"
        callback.assert_called_once()

    def test_approval_path_audit_entry_has_user_approved_decision(self) -> None:
        """UserApproved consent_decision is recorded in the audit log."""
        interp, _ = self._make_production_interp(approve=True)
        self._inject_denied_scenario(interp)
        interp.exec("r = send_email(to=_to, body=_body)")

        log = interp.audit_log
        approved_entries = [e for e in log if e.consent_decision == "UserApproved"]
        assert approved_entries, f"Expected UserApproved entry in audit log, got: {log!r}"
        entry = approved_entries[0]
        assert entry.tool_name == "send_email"
        assert entry.outcome == "Denied"  # policy still denied; user overrode

    # --- Rejection path ---

    def test_rejection_path_raises_policy_violation_error(self) -> None:
        """User rejection in PRODUCTION mode raises PolicyViolationError."""
        interp, callback = self._make_production_interp(approve=False)
        self._inject_denied_scenario(interp)

        with pytest.raises(PolicyViolationError) as exc_info:
            interp.exec("r = send_email(to=_to, body=_body)")

        callback.assert_called_once()
        assert exc_info.value.consent_decision == "UserRejected"
        assert exc_info.value.tool_name == "send_email"

    def test_rejection_path_audit_entry_has_user_rejected_decision(self) -> None:
        """UserRejected consent_decision is recorded in the audit log."""
        interp, _ = self._make_production_interp(approve=False)
        self._inject_denied_scenario(interp)

        with pytest.raises(PolicyViolationError):
            interp.exec("r = send_email(to=_to, body=_body)")

        log = interp.audit_log
        rejected_entries = [e for e in log if e.consent_decision == "UserRejected"]
        assert rejected_entries, f"Expected UserRejected entry in audit log, got: {log!r}"
        assert rejected_entries[0].outcome == "Denied"

    def test_rejection_path_tool_is_not_called(self) -> None:
        """When user rejects, the tool callable is not invoked."""
        call_count: list[int] = [0]

        def _counting_tool(**_kw: object) -> CaMeLValue:
            call_count[0] += 1
            return wrap("ok", sources=frozenset({"CaMeL"}))

        callback = MagicMock(return_value=False)
        interp = CaMeLInterpreter(
            tools={"send_email": _counting_tool},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=callback,
        )
        self._inject_denied_scenario(interp)

        with pytest.raises(PolicyViolationError):
            interp.exec("r = send_email(to=_to, body=_body)")

        assert call_count[0] == 0, "Tool was called despite user rejection"

    def test_consent_callback_receives_correct_arguments(self) -> None:
        """Consent callback is called with tool_name, argument_summary, denial_reason."""
        interp, callback = self._make_production_interp(approve=True)
        self._inject_denied_scenario(interp)
        interp.exec("r = send_email(to=_to, body=_body)")

        callback.assert_called_once()
        call_args = callback.call_args
        tool_name_arg, arg_summary_arg, denial_reason_arg = call_args.args

        assert tool_name_arg == "send_email"
        assert isinstance(arg_summary_arg, str) and len(arg_summary_arg) > 0
        assert isinstance(denial_reason_arg, str) and len(denial_reason_arg) > 0
        assert "untrusted" in denial_reason_arg.lower()

    def test_production_mode_requires_consent_callback(self) -> None:
        """ValueError is raised if PRODUCTION mode is selected without a callback."""
        with pytest.raises(ValueError, match="consent_callback"):
            CaMeLInterpreter(
                tools={},
                policy_engine=self.registry,
                enforcement_mode=EnforcementMode.PRODUCTION,
                consent_callback=None,  # intentionally omitted
            )

    def test_allowed_calls_do_not_invoke_consent_callback(self) -> None:
        """Consent callback is NOT called when the policy allows the tool call."""
        callback = MagicMock(return_value=True)
        interp = CaMeLInterpreter(
            tools={"send_email": _ok_tool},
            policy_engine=self.registry,
            enforcement_mode=EnforcementMode.PRODUCTION,
            consent_callback=callback,
        )
        _seed(interp, _to=make_trusted_value("alice@example.com"))
        interp.exec("r = send_email(to=_to)")

        callback.assert_not_called()


# ===========================================================================
# 7. Additional cross-cutting tests
# ===========================================================================


class TestMultiPolicyComposition:
    """Verify all-must-allow composition: multiple policies for the same tool."""

    def test_both_policies_must_allow(self) -> None:
        """When two policies are registered, both must return Allowed."""
        registry = PolicyRegistry()

        # First policy always allows.
        registry.register("send_email", lambda tn, kw: Allowed())
        # Second policy always denies.
        registry.register("send_email", lambda tn, kw: Denied("second policy blocks"))

        result = registry.evaluate("send_email", {})
        assert isinstance(result, Denied)
        assert "second policy blocks" in result.reason

    def test_first_denial_short_circuits_evaluation(self) -> None:
        """Once the first Denied is returned, remaining policies are not called."""
        call_counts: list[int] = [0, 0]

        def policy_a(tn: str, kw: object) -> Denied:
            call_counts[0] += 1
            return Denied("first")

        def policy_b(tn: str, kw: object) -> Allowed:
            call_counts[1] += 1
            return Allowed()

        registry = PolicyRegistry()
        registry.register("some_tool", policy_a)
        registry.register("some_tool", policy_b)

        registry.evaluate("some_tool", {})
        assert call_counts[0] == 1
        assert call_counts[1] == 0, "Second policy should not be called after first Denied"


class TestAuditLogTimestamps:
    """Verify audit log timestamp ordering and ISO-8601 format."""

    def test_timestamps_are_chronologically_ordered(self) -> None:
        """Audit log entries have non-decreasing timestamps."""
        registry = _make_registry()
        interp = _make_interp({"send_email": _ok_tool}, registry)
        _seed(interp, _to=make_trusted_value("alice@example.com"))

        for _ in range(5):
            interp.exec("r_ts = send_email(to=_to)")

        log = interp.audit_log
        assert len(log) == 5
        timestamps = [e.timestamp for e in log]
        assert timestamps == sorted(timestamps), (
            f"Audit log timestamps are not ordered: {timestamps!r}"
        )

    def test_timestamp_is_iso8601_string(self) -> None:
        """Audit log timestamp is a valid ISO-8601 datetime string."""
        from datetime import datetime

        registry = _make_registry()
        interp = _make_interp({"send_email": _ok_tool}, registry)
        _seed(interp, _to=make_trusted_value("alice@example.com"))
        interp.exec("r_iso = send_email(to=_to)")

        ts = interp.audit_log[-1].timestamp
        # datetime.fromisoformat raises ValueError if parsing fails.
        parsed = datetime.fromisoformat(ts)
        assert parsed is not None
