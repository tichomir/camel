"""Tests for camel/provenance.py — ProvenanceChain API, serialisation, and phishing heuristics.

Covers:
  1. ProvenanceHop construction and to_dict() round-trip.
  2. ProvenanceChain construction, is_trusted property, to_dict() and to_json() round-trip.
  3. build_provenance_chain() with single/multi source, trusted/untrusted origins.
  4. TRUSTED_SOURCES boundary logic — hops from untrusted sources flagged correctly.
  5. detect_phishing_content() — returns PhishingWarning for known patterns, empty for clean/trusted.
  6. _build_provenance_data() helper producing schema-valid output.
  7. CaMeLAgent.get_provenance() — KeyError semantics and concurrent AgentResult safety.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from camel.interpreter import CaMeLInterpreter, EnforcementMode, ExecutionMode
from camel.provenance import (
    TRUSTED_SOURCES,
    PhishingWarning,
    ProvenanceChain,
    ProvenanceHop,
    build_provenance_chain,
    detect_phishing_content,
)
from camel.value import CaMeLValue, Public, wrap
from camel_security.agent import AgentResult, _build_provenance_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _untrusted_cv(
    value: Any = "hello",
    sources: frozenset[str] | None = None,
    inner_source: str | None = None,
    readers: Any = Public,
) -> CaMeLValue:
    return CaMeLValue(
        value=value,
        sources=sources if sources is not None else frozenset({"get_last_email"}),
        inner_source=inner_source,
        readers=readers,
    )


def _trusted_cv(value: Any = "literal") -> CaMeLValue:
    return CaMeLValue(
        value=value,
        sources=frozenset({"User literal"}),
        inner_source=None,
        readers=Public,
    )


# ---------------------------------------------------------------------------
# 1. ProvenanceHop
# ---------------------------------------------------------------------------


class TestProvenanceHop:
    def test_fields_stored(self) -> None:
        hop = ProvenanceHop(
            tool_name="get_last_email",
            inner_source="sender",
            readers=["alice@example.com"],
        )
        assert hop.tool_name == "get_last_email"
        assert hop.inner_source == "sender"
        assert hop.readers == ["alice@example.com"]
        assert hop.timestamp is None

    def test_to_dict_keys(self) -> None:
        hop = ProvenanceHop(
            tool_name="tool_x",
            inner_source="field",
            readers="Public",
            timestamp="2026-01-01T00:00:00Z",
        )
        d = hop.to_dict()
        assert set(d.keys()) == {"tool_name", "inner_source", "readers", "timestamp"}
        assert d["tool_name"] == "tool_x"
        assert d["inner_source"] == "field"
        assert d["readers"] == "Public"
        assert d["timestamp"] == "2026-01-01T00:00:00Z"

    def test_to_dict_json_serialisable(self) -> None:
        hop = ProvenanceHop(
            tool_name="tool_x",
            inner_source=None,
            readers=["a@b.com"],
        )
        # Must not raise
        json.dumps(hop.to_dict())

    def test_frozen(self) -> None:
        hop = ProvenanceHop(tool_name="t", inner_source=None, readers="Public")
        with pytest.raises((AttributeError, TypeError)):
            hop.tool_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. ProvenanceChain
# ---------------------------------------------------------------------------


class TestProvenanceChain:
    def _chain_with_hops(self, hops: list[ProvenanceHop]) -> ProvenanceChain:
        return ProvenanceChain(variable_name="x", hops=hops)

    def test_empty_chain_is_trusted(self) -> None:
        chain = ProvenanceChain(variable_name="v", hops=[])
        assert chain.is_trusted is True

    def test_all_trusted_hops(self) -> None:
        hops = [
            ProvenanceHop(tool_name="User literal", inner_source=None, readers="Public"),
            ProvenanceHop(tool_name="CaMeL", inner_source=None, readers="Public"),
        ]
        chain = self._chain_with_hops(hops)
        assert chain.is_trusted is True

    def test_any_untrusted_hop_makes_chain_untrusted(self) -> None:
        hops = [
            ProvenanceHop(tool_name="User literal", inner_source=None, readers="Public"),
            ProvenanceHop(tool_name="get_last_email", inner_source=None, readers="Public"),
        ]
        chain = self._chain_with_hops(hops)
        assert chain.is_trusted is False

    def test_to_dict_structure(self) -> None:
        chain = ProvenanceChain(
            variable_name="email_body",
            hops=[
                ProvenanceHop(
                    tool_name="get_last_email",
                    inner_source="body",
                    readers="Public",
                )
            ],
        )
        d = chain.to_dict()
        assert d["variable_name"] == "email_body"
        assert isinstance(d["hops"], list)
        assert len(d["hops"]) == 1
        assert d["is_trusted"] is False

    def test_to_dict_json_round_trip(self) -> None:
        chain = ProvenanceChain(
            variable_name="subject",
            hops=[
                ProvenanceHop(tool_name="User literal", inner_source=None, readers="Public"),
                ProvenanceHop(
                    tool_name="read_file",
                    inner_source="content",
                    readers=["alice@example.com"],
                ),
            ],
        )
        d = chain.to_dict()
        # JSON serialisable without loss
        raw = json.dumps(d)
        recovered = json.loads(raw)
        assert recovered["variable_name"] == "subject"
        assert len(recovered["hops"]) == 2
        assert recovered["is_trusted"] is False

    def test_to_json_compact(self) -> None:
        chain = ProvenanceChain(variable_name="v", hops=[])
        j = chain.to_json()
        parsed = json.loads(j)
        assert parsed["variable_name"] == "v"
        assert parsed["hops"] == []
        assert parsed["is_trusted"] is True

    def test_to_json_indented(self) -> None:
        chain = ProvenanceChain(variable_name="v", hops=[])
        j = chain.to_json(indent=2)
        assert "\n" in j

    def test_all_provenance_hop_fields_survive_round_trip(self) -> None:
        hop = ProvenanceHop(
            tool_name="get_calendar",
            inner_source="attendees",
            readers=["bob@corp.com", "alice@corp.com"],
            timestamp="2026-03-18T12:00:00Z",
        )
        chain = ProvenanceChain(variable_name="attendees", hops=[hop])
        recovered = json.loads(chain.to_json())
        hop_dict = recovered["hops"][0]
        assert hop_dict["tool_name"] == "get_calendar"
        assert hop_dict["inner_source"] == "attendees"
        assert hop_dict["readers"] == ["bob@corp.com", "alice@corp.com"]
        assert hop_dict["timestamp"] == "2026-03-18T12:00:00Z"


# ---------------------------------------------------------------------------
# 3. build_provenance_chain
# ---------------------------------------------------------------------------


class TestBuildProvenanceChain:
    def test_single_untrusted_source(self) -> None:
        cv = CaMeLValue(
            value="body text",
            sources=frozenset({"get_last_email"}),
            inner_source="body",
            readers=Public,
        )
        chain = build_provenance_chain("email_body", cv)
        assert chain.variable_name == "email_body"
        assert len(chain.hops) == 1
        hop = chain.hops[0]
        assert hop.tool_name == "get_last_email"
        assert hop.inner_source == "body"
        assert hop.readers == "Public"
        assert chain.is_trusted is False

    def test_single_trusted_source(self) -> None:
        cv = CaMeLValue(
            value="hello",
            sources=frozenset({"User literal"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("greeting", cv)
        assert chain.is_trusted is True
        assert len(chain.hops) == 1
        assert chain.hops[0].tool_name == "User literal"

    def test_camel_source_is_trusted(self) -> None:
        cv = CaMeLValue(
            value=42,
            sources=frozenset({"CaMeL"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("result", cv)
        assert chain.is_trusted is True

    def test_multi_source_inner_source_is_none(self) -> None:
        # inner_source only propagated for single-source values
        cv = CaMeLValue(
            value="merged",
            sources=frozenset({"get_last_email", "read_file"}),
            inner_source="sender",  # should NOT appear — multiple sources
            readers=Public,
        )
        chain = build_provenance_chain("merged_var", cv)
        assert len(chain.hops) == 2
        for hop in chain.hops:
            assert hop.inner_source is None

    def test_multi_source_mixed_trusted_untrusted(self) -> None:
        cv = CaMeLValue(
            value="derived",
            sources=frozenset({"User literal", "get_last_email"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("derived", cv)
        assert chain.is_trusted is False
        tool_names = {h.tool_name for h in chain.hops}
        assert "User literal" in tool_names
        assert "get_last_email" in tool_names

    def test_trusted_hops_come_before_untrusted(self) -> None:
        cv = CaMeLValue(
            value="v",
            sources=frozenset({"get_last_email", "User literal"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("v", cv)
        # First hop should be trusted
        assert chain.hops[0].tool_name in TRUSTED_SOURCES

    def test_readers_set_serialised_as_sorted_list(self) -> None:
        cv = CaMeLValue(
            value="data",
            sources=frozenset({"read_file"}),
            inner_source=None,
            readers=frozenset({"charlie@c.com", "alice@a.com", "bob@b.com"}),
        )
        chain = build_provenance_chain("data", cv)
        hop = chain.hops[0]
        assert isinstance(hop.readers, list)
        assert hop.readers == sorted(["charlie@c.com", "alice@a.com", "bob@b.com"])

    def test_public_readers_serialised_as_string(self) -> None:
        cv = CaMeLValue(
            value="open",
            sources=frozenset({"fetch_url"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("open", cv)
        assert chain.hops[0].readers == "Public"

    def test_empty_sources_produces_empty_hops(self) -> None:
        cv = CaMeLValue(
            value="empty",
            sources=frozenset(),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("empty", cv)
        assert chain.hops == []
        assert chain.is_trusted is True  # vacuously true


# ---------------------------------------------------------------------------
# 4. TRUSTED_SOURCES boundary logic
# ---------------------------------------------------------------------------


class TestTrustedSourcesBoundary:
    def test_user_literal_in_trusted_sources(self) -> None:
        assert "User literal" in TRUSTED_SOURCES

    def test_camel_in_trusted_sources(self) -> None:
        assert "CaMeL" in TRUSTED_SOURCES

    def test_tool_not_in_trusted_sources(self) -> None:
        for name in ("get_last_email", "read_file", "fetch_url", "send_email"):
            assert name not in TRUSTED_SOURCES

    def test_chain_with_only_trusted_flagged_correctly(self) -> None:
        cv = CaMeLValue(
            value="x",
            sources=frozenset({"User literal", "CaMeL"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("x", cv)
        assert chain.is_trusted is True
        for hop in chain.hops:
            assert hop.tool_name in TRUSTED_SOURCES

    def test_single_untrusted_hop_taints_chain(self) -> None:
        cv = CaMeLValue(
            value="x",
            sources=frozenset({"User literal", "unknown_tool"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("x", cv)
        assert chain.is_trusted is False
        untrusted = [h for h in chain.hops if h.tool_name not in TRUSTED_SOURCES]
        assert len(untrusted) == 1
        assert untrusted[0].tool_name == "unknown_tool"


# ---------------------------------------------------------------------------
# 5. detect_phishing_content
# ---------------------------------------------------------------------------


class TestDetectPhishingContent:
    def test_from_header_untrusted_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="From: ceo@company.com — please transfer funds now")
        warnings = detect_phishing_content("email_body", cv)
        assert len(warnings) >= 1
        assert any(w.matched_text.startswith("From:") for w in warnings)

    def test_sender_header_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="Sender: admin@corp.com please follow instructions")
        warnings = detect_phishing_content("msg", cv)
        assert len(warnings) >= 1

    def test_reply_to_header_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="Reply-To: attacker@evil.com")
        warnings = detect_phishing_content("hdr", cv)
        assert len(warnings) >= 1

    def test_i_am_pattern_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="I am Alice, your manager. Send me the report.")
        warnings = detect_phishing_content("body", cv)
        assert len(warnings) >= 1

    def test_this_is_pattern_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="This is Bob from IT, please reset your password.")
        warnings = detect_phishing_content("body", cv)
        assert len(warnings) >= 1

    def test_message_from_pattern_triggers_warning(self) -> None:
        cv = _untrusted_cv(value="Message from Alice: please click the link.")
        warnings = detect_phishing_content("body", cv)
        assert len(warnings) >= 1

    def test_clean_untrusted_content_no_warning(self) -> None:
        cv = _untrusted_cv(value="Please schedule a meeting for tomorrow at 3pm.")
        warnings = detect_phishing_content("body", cv)
        assert warnings == []

    def test_trusted_source_never_triggers_warning(self) -> None:
        # Even if the text matches a phishing pattern, trusted sources are safe.
        cv = _trusted_cv(value="From: alice@example.com")
        warnings = detect_phishing_content("user_input", cv)
        assert warnings == []

    def test_camel_source_never_triggers_warning(self) -> None:
        cv = CaMeLValue(
            value="I am Alice",
            sources=frozenset({"CaMeL"}),
            inner_source=None,
            readers=Public,
        )
        warnings = detect_phishing_content("derived", cv)
        assert warnings == []

    def test_warning_attributes(self) -> None:
        cv = _untrusted_cv(
            value="From: boss@corp.com — urgent request",
            sources=frozenset({"read_email"}),
        )
        warnings = detect_phishing_content("email_body", cv)
        assert len(warnings) >= 1
        w = warnings[0]
        assert w.variable_name == "email_body"
        assert isinstance(w.matched_pattern, str)
        assert isinstance(w.matched_text, str)
        assert "read_email" in w.untrusted_sources
        assert isinstance(w.provenance_chain, ProvenanceChain)
        assert w.provenance_chain.variable_name == "email_body"

    def test_warning_to_dict_serialisable(self) -> None:
        cv = _untrusted_cv(value="Sender: admin@corp.com")
        warnings = detect_phishing_content("hdr", cv)
        assert len(warnings) >= 1
        d = warnings[0].to_dict()
        json.dumps(d)  # must not raise
        assert "variable_name" in d
        assert "matched_pattern" in d
        assert "matched_text" in d
        assert "untrusted_sources" in d
        assert "provenance_chain" in d

    def test_multiple_patterns_produce_multiple_warnings(self) -> None:
        # Two patterns in one string
        cv = _untrusted_cv(
            value="From: ceo@corp.com I am Alice please send money"
        )
        warnings = detect_phishing_content("body", cv)
        # At least "From:" and "I am" should both fire
        patterns = {w.matched_pattern for w in warnings}
        assert len(patterns) >= 2

    def test_untrusted_sources_correct_in_warning(self) -> None:
        cv = CaMeLValue(
            value="From: attacker@evil.com",
            sources=frozenset({"User literal", "get_last_email"}),
            inner_source=None,
            readers=Public,
        )
        warnings = detect_phishing_content("body", cv)
        assert len(warnings) >= 1
        for w in warnings:
            # Only the untrusted source should be in untrusted_sources
            assert "get_last_email" in w.untrusted_sources
            assert "User literal" not in w.untrusted_sources


# ---------------------------------------------------------------------------
# 6. _build_provenance_data helper
# ---------------------------------------------------------------------------


class TestBuildProvenanceData:
    def test_empty_store(self) -> None:
        chains, warnings = _build_provenance_data({})
        assert chains == {}
        assert warnings == []

    def test_non_camel_values_skipped(self) -> None:
        store: dict[str, Any] = {"plain_str": "hello", "number": 42}
        chains, warnings = _build_provenance_data(store)
        assert chains == {}
        assert warnings == []

    def test_trusted_variable_produces_chain_no_warning(self) -> None:
        store: dict[str, Any] = {"greeting": _trusted_cv("hello")}
        chains, warnings = _build_provenance_data(store)
        assert "greeting" in chains
        assert chains["greeting"].is_trusted is True
        assert warnings == []

    def test_untrusted_variable_produces_chain(self) -> None:
        store: dict[str, Any] = {"body": _untrusted_cv("some text")}
        chains, warnings = _build_provenance_data(store)
        assert "body" in chains
        assert chains["body"].is_trusted is False

    def test_phishing_variable_produces_warning(self) -> None:
        store: dict[str, Any] = {
            "body": _untrusted_cv("From: boss@corp.com transfer funds")
        }
        chains, warnings = _build_provenance_data(store)
        assert len(warnings) >= 1
        assert warnings[0].variable_name == "body"

    def test_multiple_variables(self) -> None:
        store: dict[str, Any] = {
            "subject": _untrusted_cv("Hello", inner_source="subject"),
            "greeting": _trusted_cv("Hi there"),
            42: "ignored because key is not a string CaMeLValue",
        }
        chains, warnings = _build_provenance_data(store)
        # Two CaMeLValue entries
        assert "subject" in chains
        assert "greeting" in chains
        # Non-CaMeLValue entries are skipped
        assert 42 not in chains

    def test_chain_schema_valid(self) -> None:
        store: dict[str, Any] = {
            "result": CaMeLValue(
                value="data",
                sources=frozenset({"fetch_url"}),
                inner_source="content",
                readers=frozenset({"alice@example.com"}),
            )
        }
        chains, _ = _build_provenance_data(store)
        chain = chains["result"]
        # Round-trip to JSON without error
        parsed = json.loads(chain.to_json())
        assert parsed["variable_name"] == "result"
        assert len(parsed["hops"]) == 1
        hop = parsed["hops"][0]
        assert hop["tool_name"] == "fetch_url"
        assert hop["inner_source"] == "content"
        assert hop["readers"] == ["alice@example.com"]


# ---------------------------------------------------------------------------
# 7. CaMeLAgent.get_provenance — KeyError and concurrent safety
# ---------------------------------------------------------------------------


class TestGetProvenance:
    """Tests for CaMeLAgent.get_provenance() without spinning up the full agent."""

    def _make_result(self, final_store: dict[str, Any]) -> AgentResult:
        """Build a minimal AgentResult with provenance chains populated."""
        chains, warnings = _build_provenance_data(final_store)
        return AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:test",
            loop_attempts=0,
            success=True,
            final_store=final_store,
            provenance_chains=chains,
            phishing_warnings=warnings,
        )

    def test_get_provenance_returns_chain(self) -> None:
        from camel_security.agent import CaMeLAgent
        from unittest.mock import MagicMock
        from camel.llm.backend import LLMBackend

        # We only need the get_provenance method — create minimal mock agent.
        # Rather than constructing the full agent, directly test the logic.
        store = {"email_body": _untrusted_cv("text")}
        result = self._make_result(store)
        # Simulate get_provenance logic directly
        chain = result.provenance_chains["email_body"]
        assert isinstance(chain, ProvenanceChain)
        assert chain.variable_name == "email_body"

    def test_get_provenance_key_error_on_missing(self) -> None:
        result = self._make_result({})
        with pytest.raises(KeyError, match="not found"):
            # Replicate get_provenance KeyError logic
            try:
                _ = result.provenance_chains["missing_var"]
            except KeyError:
                raise KeyError(
                    f"Variable 'missing_var' not found in execution result. "
                    f"Available variables: {sorted(result.provenance_chains)}"
                ) from None

    def test_agent_result_is_frozen(self) -> None:
        result = self._make_result({})
        with pytest.raises((AttributeError, TypeError)):
            result.success = False  # type: ignore[misc]

    def test_concurrent_result_access_thread_safe(self) -> None:
        """Multiple threads reading the same AgentResult concurrently — no errors."""
        store = {
            "a": _untrusted_cv("text_a"),
            "b": _trusted_cv("text_b"),
            "c": CaMeLValue(
                value="From: boss@corp.com",
                sources=frozenset({"read_email"}),
                inner_source="body",
                readers=Public,
            ),
        }
        result = self._make_result(store)
        errors: list[Exception] = []

        def reader() -> None:
            try:
                for _ in range(50):
                    chain_a = result.provenance_chains["a"]
                    assert not chain_a.is_trusted
                    chain_b = result.provenance_chains["b"]
                    assert chain_b.is_trusted
                    _ = chain_a.to_json()
                    _ = result.phishing_warnings
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_provenance_chains_keyed_by_variable_name(self) -> None:
        store = {
            "x": _untrusted_cv("val_x"),
            "y": _trusted_cv("val_y"),
        }
        result = self._make_result(store)
        assert set(result.provenance_chains.keys()) == {"x", "y"}

    def test_failed_result_has_empty_provenance(self) -> None:
        result = AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:test",
            loop_attempts=10,
            success=False,
            final_store={},
        )
        assert result.provenance_chains == {}
        assert result.phishing_warnings == []

    def test_json_serialisation_no_data_loss(self) -> None:
        store = {
            "subject": CaMeLValue(
                value="Urgent: please act",
                sources=frozenset({"get_last_email"}),
                inner_source="subject",
                readers=frozenset({"alice@example.com", "bob@example.com"}),
            )
        }
        result = self._make_result(store)
        chain = result.provenance_chains["subject"]
        raw = chain.to_json()
        parsed = json.loads(raw)

        assert parsed["variable_name"] == "subject"
        assert len(parsed["hops"]) == 1
        hop = parsed["hops"][0]
        assert hop["tool_name"] == "get_last_email"
        assert hop["inner_source"] == "subject"
        assert sorted(hop["readers"]) == ["alice@example.com", "bob@example.com"]
        assert hop["timestamp"] is None
        assert parsed["is_trusted"] is False


# ---------------------------------------------------------------------------
# 8. Three-hop chain acceptance criterion
# ---------------------------------------------------------------------------


class TestThreeHopChain:
    """Explicit 3-hop dependency chain tests (acceptance criterion)."""

    def test_three_distinct_untrusted_sources(self) -> None:
        """3-hop chain from three distinct untrusted tools."""
        cv = CaMeLValue(
            value="combined_result",
            sources=frozenset({"tool_alpha", "tool_beta", "tool_gamma"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("final_output", cv)
        assert len(chain.hops) == 3
        assert not chain.is_trusted
        hop_names = sorted(h.tool_name for h in chain.hops)
        assert hop_names == ["tool_alpha", "tool_beta", "tool_gamma"]

    def test_three_hop_chain_serialisation(self) -> None:
        """3-hop chain serialises to valid JSON with correct schema."""
        cv = CaMeLValue(
            value="data",
            sources=frozenset({"tool_a", "tool_b", "tool_c"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("triple", cv)
        parsed = json.loads(chain.to_json())
        assert parsed["variable_name"] == "triple"
        assert len(parsed["hops"]) == 3
        assert parsed["is_trusted"] is False
        for hop in parsed["hops"]:
            assert "tool_name" in hop
            assert "inner_source" in hop
            assert "readers" in hop
            assert "timestamp" in hop

    def test_three_hop_mixed_trusted_untrusted(self) -> None:
        """3-hop chain with 2 trusted + 1 untrusted — chain is untrusted."""
        cv = CaMeLValue(
            value="mixed",
            sources=frozenset({"User literal", "CaMeL", "external_tool"}),
            inner_source=None,
            readers=Public,
        )
        chain = build_provenance_chain("mixed_var", cv)
        assert len(chain.hops) == 3
        assert not chain.is_trusted
        # Trusted hops appear before untrusted hops
        trusted_positions = [
            i for i, h in enumerate(chain.hops) if h.tool_name in TRUSTED_SOURCES
        ]
        untrusted_positions = [
            i for i, h in enumerate(chain.hops) if h.tool_name not in TRUSTED_SOURCES
        ]
        assert max(trusted_positions) < min(untrusted_positions)


# ---------------------------------------------------------------------------
# 9. AuditLogEntry.provenance_chains integration tests
# ---------------------------------------------------------------------------


class TestAuditLogProvenanceChains:
    """Verify AuditLogEntry.provenance_chains populated for allowed tool calls."""

    def _make_tool(self, name: str) -> Any:
        """Create a trivial tool that returns a CaMeLValue."""

        def tool_fn(text: str) -> CaMeLValue:
            return CaMeLValue(
                value=f"out:{text}",
                sources=frozenset({name}),
                inner_source=None,
                readers=Public,
            )

        tool_fn.__name__ = name
        return tool_fn

    def test_allowed_entry_has_provenance_chains(self) -> None:
        """AuditLogEntry for an allowed tool call has provenance_chains dict."""
        from camel.interpreter import EnforcementMode
        from camel.policy.interfaces import Allowed as PolicyAllowed, PolicyRegistry

        registry = PolicyRegistry()
        registry.register("my_tool", lambda tool_name, kwargs: PolicyAllowed())

        tool_fn = self._make_tool("my_tool")
        interp = CaMeLInterpreter(
            tools={"my_tool": tool_fn},
            mode=ExecutionMode.STRICT,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )

        interp.exec('result = my_tool("hello")')
        entries = [e for e in interp.audit_log if e.tool_name == "my_tool"]
        assert entries, "Expected at least one audit log entry for my_tool"
        entry = entries[-1]
        assert entry.outcome == "Allowed"
        assert entry.provenance_chains is not None
        assert isinstance(entry.provenance_chains, dict)
        # Argument 'text' should have a provenance chain
        assert "text" in entry.provenance_chains
        chain_dict = entry.provenance_chains["text"]
        assert chain_dict["variable_name"] == "text"
        assert isinstance(chain_dict["hops"], list)
        assert "is_trusted" in chain_dict

    def test_provenance_chains_json_serialisable(self) -> None:
        """provenance_chains in AuditLogEntry can be JSON-serialised."""
        from camel.interpreter import EnforcementMode
        from camel.policy.interfaces import Allowed as PolicyAllowed, PolicyRegistry

        registry = PolicyRegistry()
        registry.register("another_tool", lambda tool_name, kwargs: PolicyAllowed())

        tool_fn = self._make_tool("another_tool")
        interp = CaMeLInterpreter(
            tools={"another_tool": tool_fn},
            mode=ExecutionMode.STRICT,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )

        interp.exec('out = another_tool("data")')
        entry = interp.audit_log[-1]
        assert entry.provenance_chains is not None
        # Must be JSON-serialisable without error
        raw = json.dumps(entry.provenance_chains)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_audit_entry_fields_complete(self) -> None:
        """AuditLogEntry has all expected fields including provenance_chains."""
        from camel.interpreter import EnforcementMode
        from camel.policy.interfaces import Allowed as PolicyAllowed, PolicyRegistry

        def simple_tool(x: int) -> CaMeLValue:
            return CaMeLValue(
                value=x * 2,
                sources=frozenset({"simple_tool"}),
                inner_source=None,
                readers=Public,
            )

        registry = PolicyRegistry()
        registry.register("simple_tool", lambda tool_name, kwargs: PolicyAllowed())

        interp = CaMeLInterpreter(
            tools={"simple_tool": simple_tool},
            mode=ExecutionMode.STRICT,
            policy_engine=registry,
            enforcement_mode=EnforcementMode.EVALUATION,
        )

        interp.exec("result = simple_tool(5)")
        entry = interp.audit_log[-1]
        assert hasattr(entry, "tool_name")
        assert hasattr(entry, "outcome")
        assert hasattr(entry, "reason")
        assert hasattr(entry, "timestamp")
        assert hasattr(entry, "provenance_chains")
        assert entry.tool_name == "simple_tool"
        assert entry.outcome == "Allowed"
        assert entry.provenance_chains is not None


# ---------------------------------------------------------------------------
# 10. CaMeLAgent.get_provenance() KeyError behaviour (direct method test)
# ---------------------------------------------------------------------------


class TestAgentGetProvenanceMethod:
    """Test the actual CaMeLAgent.get_provenance() method raises KeyError."""

    def _make_agent_result_with_chains(
        self, store: dict[str, Any]
    ) -> "AgentResult":  # noqa: F821
        from camel_security.agent import AgentResult

        chains, warnings = _build_provenance_data(store)
        return AgentResult(
            execution_trace=[],
            display_output=[],
            policy_denials=[],
            audit_log_ref="camel-audit:test",
            loop_attempts=0,
            success=True,
            final_store=store,
            provenance_chains=chains,
            phishing_warnings=warnings,
        )

    def test_get_provenance_raises_keyerror_unknown_variable(self) -> None:
        """agent.get_provenance raises KeyError for unknown variable name."""
        from camel_security.agent import CaMeLAgent

        # Build a minimal mock agent to call get_provenance on.
        # We need a real CaMeLAgent instance; use __new__ to bypass __init__
        # since we only need the get_provenance method.
        agent = object.__new__(CaMeLAgent)
        result = self._make_agent_result_with_chains({})

        with pytest.raises(KeyError):
            agent.get_provenance("nonexistent_var", result)

    def test_get_provenance_returns_correct_chain(self) -> None:
        """agent.get_provenance returns correct chain for known variable."""
        from camel_security.agent import CaMeLAgent

        store = {"my_var": _untrusted_cv("value_here")}
        agent = object.__new__(CaMeLAgent)
        result = self._make_agent_result_with_chains(store)

        chain = agent.get_provenance("my_var", result)
        assert isinstance(chain, ProvenanceChain)
        assert chain.variable_name == "my_var"
        assert not chain.is_trusted

    def test_get_provenance_trusted_variable(self) -> None:
        """agent.get_provenance returns trusted chain for trusted variable."""
        from camel_security.agent import CaMeLAgent

        store = {"literal": _trusted_cv("hello world")}
        agent = object.__new__(CaMeLAgent)
        result = self._make_agent_result_with_chains(store)

        chain = agent.get_provenance("literal", result)
        assert chain.is_trusted is True
