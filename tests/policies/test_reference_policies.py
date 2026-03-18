"""Comprehensive unit tests for CaMeL reference policy library.

Coverage targets
----------------
* Every policy: ≥3 test cases (allowed, denied-shallow, denied-deep-provenance)
* Every documented denial-reason string is asserted verbatim in at least one test
* Deep-provenance tests construct CaMeLValues with ≥2 hops of untrusted dependency
* All tests are deterministic and make zero LLM / HTTP calls (NFR-2)

Total: well above the 18-case minimum required by the sprint acceptance criteria.
"""

from __future__ import annotations

import pytest

from camel.policy.interfaces import Allowed, Denied, PolicyRegistry
from camel.policy.reference_policies import (
    configure_reference_policies,
    create_calendar_event_policy,
    fetch_external_url_policy,
    make_write_file_policy,
    post_message_policy,
    register_all,
    send_email_policy,
    send_money_policy,
)
from camel.value import Public, wrap

# ---------------------------------------------------------------------------
# Helpers — build multi-hop derived CaMeLValues without the interpreter
# ---------------------------------------------------------------------------

_TRUSTED = frozenset({"User literal"})
_CAMEL_TRUSTED = frozenset({"CaMeL"})
_EMAIL_TOOL = frozenset({"read_email"})
_DOC_TOOL = frozenset({"read_document"})
_DB_TOOL = frozenset({"read_db"})


def _hop1_email(raw: object, readers: object = Public) -> object:
    """Simulate a value returned directly from read_email (1 hop)."""
    return wrap(raw, sources=_EMAIL_TOOL, readers=readers)  # type: ignore[arg-type]


def _hop2_derived(raw: object, hop1_sources: frozenset[str], readers: object = Public) -> object:
    """Simulate a value derived from a 1-hop untrusted value (2 hops deep).

    In real interpreter execution the sources would be the union of all
    inputs.  Here we union manually to mirror propagate_binary_op logic.
    """
    derived_sources = hop1_sources | _CAMEL_TRUSTED
    return wrap(raw, sources=derived_sources, readers=readers)  # type: ignore[arg-type]


def _hop3_derived(raw: object, hop2_sources: frozenset[str], readers: object = Public) -> object:
    """Simulate a value derived from a 2-hop untrusted value (3 hops deep)."""
    derived_sources = hop2_sources | _TRUSTED
    return wrap(raw, sources=derived_sources, readers=readers)  # type: ignore[arg-type]


# ===========================================================================
# 1.  send_email_policy
# ===========================================================================


class TestSendEmailAllowed:
    """Positive (Allowed) cases for send_email_policy."""

    def test_trusted_to_trusted_body_allowed(self) -> None:
        """Trusted recipient with trusted body is always allowed."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("alice@example.com", sources=_TRUSTED),
                "body": wrap("Hello Alice", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_trusted_to_no_body_allowed(self) -> None:
        """Trusted recipient with no body/subject kwargs is allowed."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("alice@example.com", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_untrusted_to_public_body_allowed(self) -> None:
        """Untrusted recipient is OK when body has Public readers."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("eve@external.com", sources=_EMAIL_TOOL),
                "body": wrap("Public announcement", sources=_EMAIL_TOOL, readers=Public),
            },
        )
        assert result == Allowed()

    def test_missing_to_allowed(self) -> None:
        """No 'to' argument → policy has nothing to check → Allowed."""
        result = send_email_policy("send_email", {})
        assert result == Allowed()

    def test_untrusted_to_is_authorised_reader_allowed(self) -> None:
        """Untrusted 'to' whose raw address is in body.readers is allowed."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("alice@example.com", sources=_EMAIL_TOOL),
                "body": wrap(
                    "Private content",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
            },
        )
        assert result == Allowed()

    def test_untrusted_to_authorised_for_both_fields_allowed(self) -> None:
        """Untrusted 'to' authorised for both body and subject → Allowed."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("alice@example.com", sources=_EMAIL_TOOL),
                "subject": wrap(
                    "Re: meeting",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
                "body": wrap(
                    "See attached",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
            },
        )
        assert result == Allowed()


class TestSendEmailEdgeCases:
    """Edge-case coverage: list 'to', empty/non-string forms."""

    def test_to_as_list_untrusted_one_recipient_not_reader_denied(self) -> None:
        """'to' is a list of addresses (untrusted); one not in body.readers → Denied."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap(
                    ["alice@example.com", "attacker@evil.com"],
                    sources=_EMAIL_TOOL,
                ),
                "body": wrap(
                    "Private content",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "email body" in result.reason

    def test_to_as_list_all_authorised_allowed(self) -> None:
        """'to' is a list; all recipients are authorised readers → Allowed."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap(
                    ["alice@example.com", "bob@example.com"],
                    sources=_EMAIL_TOOL,
                ),
                "body": wrap(
                    "Shared report",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com", "bob@example.com"}),
                ),
            },
        )
        assert result == Allowed()

    def test_to_as_non_string_non_list_denied(self) -> None:
        """'to' as an integer (non-string, non-list) triggers else branch → Denied."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap(12345, sources=_EMAIL_TOOL),  # unusual but exercised
                "body": wrap(
                    "Private",
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)

    def test_to_as_comma_only_string_uses_raw_fallback(self) -> None:
        """'to' is a comma-only string that splits into empty tokens → raw value used."""
        # When split(",") produces only empty/whitespace tokens, the policy
        # falls back to using the raw string itself as the sole recipient.
        # The body has restricted readers, so the empty-string recipient
        # should not be in those readers → Denied.
        result = send_email_policy(
            "send_email",
            {
                "to": wrap(",,,", sources=_EMAIL_TOOL),
                "body": wrap(
                    "Private",
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        # The comma-only string becomes ['',] after strip-filter fallback
        # or [',,,'] via raw — either way the recipient is not an authorised reader.
        assert isinstance(result, Denied)


class TestSendEmailDenied:
    """Negative (Denied) cases for send_email_policy — shallow untrusted."""

    def test_untrusted_to_body_restricted_denied(self) -> None:
        """Untrusted recipient, body restricted to different reader → Denied."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("attacker@evil.com", sources=_EMAIL_TOOL),
                "body": wrap(
                    "Private data",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "recipient address from untrusted source is not an authorised reader of the email body"
        )

    def test_untrusted_to_subject_restricted_denied(self) -> None:
        """Untrusted recipient, subject restricted to different reader → Denied."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("attacker@evil.com", sources=_EMAIL_TOOL),
                "subject": wrap(
                    "Confidential subject",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "recipient address from untrusted source is not an "
            "authorised reader of the email subject"
        )

    def test_empty_to_sources_treated_as_untrusted(self) -> None:
        """'to' with empty sources is not trusted (conservative) → readers check fires."""
        result = send_email_policy(
            "send_email",
            {
                "to": wrap("anyone@example.com"),  # sources defaults to frozenset()
                "body": wrap(
                    "Secret",
                    readers=frozenset({"owner@example.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)


class TestSendEmailDeniedDeepProvenance:
    """Denied cases where untrusted source is ≥2 hops away in the dependency chain."""

    def test_two_hop_untrusted_to_body_restricted_denied(self) -> None:
        """to = user_prefix + email_part  (2-hop sources) → still denied."""
        # Hop 1: email_sender came from read_email
        _email_sender = _hop1_email("injected_part")
        # Hop 2: formatted_to = "prefix-" + email_sender  (union of User literal + read_email)
        two_hop_to = _hop2_derived(
            "prefix-injected_part",
            _EMAIL_TOOL,
        )
        result = send_email_policy(
            "send_email",
            {
                "to": two_hop_to,  # type: ignore[arg-type]
                "body": wrap(
                    "Confidential report",
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "email body" in result.reason

    def test_three_hop_untrusted_to_denied(self) -> None:
        """3-hop derived 'to' still carries untrusted sources → Denied."""
        hop1 = _hop1_email("attacker@evil.com")
        hop2 = _hop2_derived("attacker@evil.com", _EMAIL_TOOL)
        three_hop_to = _hop3_derived("attacker@evil.com", _EMAIL_TOOL | _CAMEL_TRUSTED)
        result = send_email_policy(
            "send_email",
            {
                "to": three_hop_to,  # type: ignore[arg-type]
                "body": wrap("Private", readers=frozenset({"owner@company.com"})),
            },
        )
        assert isinstance(result, Denied)
        _ = hop1, hop2  # suppress unused warnings


# ===========================================================================
# 2.  send_money_policy
# ===========================================================================


class TestSendMoneyAllowed:
    """Positive cases for send_money_policy."""

    def test_trusted_recipient_and_amount_allowed(self) -> None:
        """Both recipient and amount from user literal → Allowed."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_TRUSTED),
                "amount": wrap(500.00, sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_trusted_recipient_only_allowed(self) -> None:
        """Only recipient present and trusted → Allowed."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_no_kwargs_allowed(self) -> None:
        """No kwargs at all → nothing to deny → Allowed."""
        result = send_money_policy("send_money", {})
        assert result == Allowed()

    def test_zero_amount_trusted_allowed(self) -> None:
        """Zero amount typed by user is a valid intent → Allowed."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_TRUSTED),
                "amount": wrap(0.0, sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_camel_trusted_source_allowed(self) -> None:
        """CaMeL-synthesised values are trusted."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_CAMEL_TRUSTED),
                "amount": wrap(100.0, sources=_CAMEL_TRUSTED),
            },
        )
        assert result == Allowed()


class TestSendMoneyDenied:
    """Negative cases for send_money_policy — shallow untrusted."""

    def test_untrusted_recipient_denied(self) -> None:
        """Recipient from email → Denied with exact reason."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("injected@attacker.com", sources=_EMAIL_TOOL),
                "amount": wrap(9999.0, sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "transfer recipient originates from untrusted source — possible data-flow manipulation"
        )

    def test_untrusted_amount_denied(self) -> None:
        """Amount from email → Denied with exact reason."""
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_TRUSTED),
                "amount": wrap(9999.0, sources=_EMAIL_TOOL),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "transfer amount originates from untrusted source — possible data-flow manipulation"
        )


class TestSendMoneyDeniedDeepProvenance:
    """Denied cases with ≥2 hops of untrusted provenance."""

    def test_two_hop_recipient_denied(self) -> None:
        """recipient = Q-LLM-extracted name + domain (2-hop chain) → Denied."""
        # Hop 1: raw name extracted from email via Q-LLM (still untrusted)
        hop1 = wrap("Evil Corp", sources=_EMAIL_TOOL)
        # Hop 2: formatted recipient address (union sources)
        two_hop_recipient = _hop2_derived("evilcorp@attacker.com", _EMAIL_TOOL)
        result = send_money_policy(
            "send_money",
            {
                "recipient": two_hop_recipient,  # type: ignore[arg-type]
                "amount": wrap(500.0, sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)
        assert "recipient" in result.reason
        _ = hop1

    def test_two_hop_amount_derived_from_email_fee_denied(self) -> None:
        """amount = user_balance − email_fee: mixed sources → Denied."""
        # Hop 1: fee value from email
        email_fee = wrap(99.0, sources=_EMAIL_TOOL)
        # Hop 2: amount = balance - fee  (User literal sources ∪ read_email)
        mixed_amount = wrap(
            401.0,
            sources=_TRUSTED | _EMAIL_TOOL,
        )
        result = send_money_policy(
            "send_money",
            {
                "recipient": wrap("alice@bank.com", sources=_TRUSTED),
                "amount": mixed_amount,
            },
        )
        assert isinstance(result, Denied)
        assert "amount" in result.reason
        _ = email_fee

    def test_three_hop_recipient_denied(self) -> None:
        """3-hop chain: doc → transform → string concat → Denied."""
        hop1 = wrap("doc_recipient", sources=_DOC_TOOL)
        hop2 = _hop2_derived("doc_recipient_formatted", _DOC_TOOL)
        three_hop = _hop3_derived("doc_recipient_formatted@bank.com", _DOC_TOOL | _CAMEL_TRUSTED)
        result = send_money_policy(
            "send_money",
            {
                "recipient": three_hop,  # type: ignore[arg-type]
                "amount": wrap(100.0, sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)
        _ = hop1, hop2


# ===========================================================================
# 3.  create_calendar_event_policy
# ===========================================================================


class TestCreateCalendarEventAllowed:
    """Positive cases for create_calendar_event_policy."""

    def test_trusted_participants_allowed(self) -> None:
        """Participants typed by user → Allowed regardless of content."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["alice@example.com"], sources=_TRUSTED),
                "title": wrap(
                    "Team sync", sources=_EMAIL_TOOL, readers=frozenset({"owner@example.com"})
                ),
            },
        )
        assert result == Allowed()

    def test_untrusted_participants_public_title_allowed(self) -> None:
        """Untrusted participants, title is Public → every participant passes."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["alice@example.com"], sources=_EMAIL_TOOL),
                "title": wrap("Public event", sources=_EMAIL_TOOL, readers=Public),
            },
        )
        assert result == Allowed()

    def test_untrusted_participants_authorised_reader_allowed(self) -> None:
        """Untrusted participant is an authorised reader of all content → Allowed."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["alice@example.com"], sources=_EMAIL_TOOL),
                "title": wrap(
                    "Private meeting",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
                "description": wrap(
                    "Confidential agenda",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@example.com"}),
                ),
            },
        )
        assert result == Allowed()

    def test_no_participants_allowed(self) -> None:
        """No participants kwarg → Allowed."""
        result = create_calendar_event_policy("create_calendar_event", {})
        assert result == Allowed()

    def test_empty_untrusted_participant_list_allowed(self) -> None:
        """Empty participant list → inner loop never executes → Allowed."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap([], sources=_EMAIL_TOOL),
                "title": wrap(
                    "Event", sources=_EMAIL_TOOL, readers=frozenset({"owner@example.com"})
                ),
            },
        )
        assert result == Allowed()


class TestCreateCalendarEventEdgeCases:
    """Edge-case coverage: non-list participants."""

    def test_participants_as_single_string_untrusted_denied(self) -> None:
        """'participants' is a plain string (not a list) → wraps to single-item list → Denied."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap("attacker@evil.com", sources=_EMAIL_TOOL),
                "title": wrap(
                    "Secret meeting",
                    sources=_TRUSTED,
                    readers=frozenset({"owner@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "attacker@evil.com" in result.reason


class TestCreateCalendarEventDenied:
    """Negative cases for create_calendar_event_policy — shallow untrusted."""

    def test_untrusted_participant_not_reader_of_title_denied(self) -> None:
        """Injected participant not authorised for event title → Denied."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["attacker@evil.com"], sources=_EMAIL_TOOL),
                "title": wrap(
                    "Secret board meeting",
                    sources=_TRUSTED,
                    readers=frozenset({"ceo@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "title" in result.reason
        assert "attacker@evil.com" in result.reason
        assert "untrusted participant list may include injected attendees" in result.reason

    def test_untrusted_participant_not_reader_of_description_denied(self) -> None:
        """Injected participant not authorised for description → Denied."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["attacker@evil.com"], sources=_EMAIL_TOOL),
                "title": wrap("Meeting", sources=_EMAIL_TOOL, readers=Public),
                "description": wrap(
                    "Confidential agenda",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"alice@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "description" in result.reason

    def test_untrusted_participant_not_reader_of_location_denied(self) -> None:
        """Injected participant not authorised for location → Denied."""
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": wrap(["spy@evil.com"], sources=_EMAIL_TOOL),
                "location": wrap(
                    "Secret bunker, Level B2",
                    sources=_TRUSTED,
                    readers=frozenset({"alice@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert "location" in result.reason


class TestCreateCalendarEventDeniedDeepProvenance:
    """Denied cases with ≥2 hops of untrusted provenance."""

    def test_two_hop_participants_denied(self) -> None:
        """Participants from 2-hop chain (email → format → wrap) → Denied."""
        hop1 = wrap(["raw@attacker.com"], sources=_EMAIL_TOOL)
        two_hop_participants = _hop2_derived(["formatted@attacker.com"], _EMAIL_TOOL)
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": two_hop_participants,  # type: ignore[arg-type]
                "title": wrap(
                    "Board meeting",
                    sources=_TRUSTED,
                    readers=frozenset({"ceo@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        _ = hop1

    def test_three_hop_participants_denied(self) -> None:
        """3-hop participants chain still carries untrusted sources → Denied."""
        hop2_participants = _hop2_derived(["attacker@evil.com"], _EMAIL_TOOL)
        three_hop = _hop3_derived(
            ["attacker@evil.com"],
            _EMAIL_TOOL | _CAMEL_TRUSTED,
        )
        result = create_calendar_event_policy(
            "create_calendar_event",
            {
                "participants": three_hop,  # type: ignore[arg-type]
                "description": wrap(
                    "Confidential",
                    sources=_TRUSTED,
                    readers=frozenset({"alice@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        _ = hop2_participants


# ===========================================================================
# 4.  make_write_file_policy (factory)
# ===========================================================================

FILE_OWNER = "alice@example.com"


@pytest.fixture()
def write_file_policy():  # type: ignore[return]
    """Return a write_file policy for FILE_OWNER."""
    return make_write_file_policy(FILE_OWNER)


class TestWriteFileAllowed:
    """Positive cases for make_write_file_policy."""

    def test_trusted_path_owner_in_readers_allowed(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Trusted path, content readable by owner → Allowed."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/home/alice/report.txt", sources=_TRUSTED),
                "content": wrap(
                    "Report content",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({FILE_OWNER}),
                ),
            },
        )
        assert result == Allowed()

    def test_trusted_path_public_content_allowed(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Trusted path, content with Public readers → owner is authorised → Allowed."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/tmp/public.txt", sources=_TRUSTED),
                "content": wrap("Public data", sources=_EMAIL_TOOL, readers=Public),
            },
        )
        assert result == Allowed()

    def test_trusted_path_no_content_allowed(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Trusted path, no content kwarg → Allowed (tool validates content)."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/home/alice/empty.txt", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_no_kwargs_allowed(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """No kwargs at all → nothing to check → Allowed."""
        result = write_file_policy("write_file", {})
        assert result == Allowed()


class TestWriteFileDenied:
    """Negative cases for make_write_file_policy — shallow untrusted."""

    def test_untrusted_path_denied(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Path from email → Denied with exact path-injection reason."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/etc/passwd", sources=_EMAIL_TOOL),
                "content": wrap("harmless", sources=_TRUSTED, readers=Public),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "file path originates from untrusted source — possible path injection"
        )

    def test_owner_not_in_content_readers_denied(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Content restricted to other users, owner excluded → Denied."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/home/alice/doc.txt", sources=_TRUSTED),
                "content": wrap(
                    "Restricted document",
                    sources=_EMAIL_TOOL,
                    readers=frozenset({"bob@example.com"}),  # alice NOT included
                ),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            f"file content is not authorised to be written by the file system owner {FILE_OWNER!r}"
        )

    def test_empty_readers_content_denied(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Content with empty readers set (no one authorised) → Denied."""
        result = write_file_policy(
            "write_file",
            {
                "path": wrap("/home/alice/secret.txt", sources=_TRUSTED),
                "content": wrap(
                    "Must not be written",
                    sources=_EMAIL_TOOL,
                    readers=frozenset(),
                ),
            },
        )
        assert isinstance(result, Denied)


class TestWriteFileDeniedDeepProvenance:
    """Denied cases with ≥2 hops of untrusted provenance."""

    def test_two_hop_path_from_document_denied(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """Path derived from document content (2-hop) → Denied."""
        hop1 = wrap("/injected/path", sources=_DOC_TOOL)
        two_hop_path = _hop2_derived("/injected/path/formatted", _DOC_TOOL)
        result = write_file_policy(
            "write_file",
            {
                "path": two_hop_path,  # type: ignore[arg-type]
                "content": wrap("Safe content", sources=_TRUSTED, readers=Public),
            },
        )
        assert isinstance(result, Denied)
        assert "path injection" in result.reason
        _ = hop1

    def test_three_hop_path_denied(self, write_file_policy) -> None:  # type: ignore[no-untyped-def]
        """3-hop path chain still untrusted → Denied."""
        hop2 = _hop2_derived("/some/path", _EMAIL_TOOL)
        three_hop_path = _hop3_derived(
            "/some/path/final",
            _EMAIL_TOOL | _CAMEL_TRUSTED,
        )
        result = write_file_policy(
            "write_file",
            {
                "path": three_hop_path,  # type: ignore[arg-type]
                "content": wrap("Safe content", sources=_TRUSTED, readers=Public),
            },
        )
        assert isinstance(result, Denied)
        _ = hop2


# ===========================================================================
# 5.  post_message_policy
# ===========================================================================


class TestPostMessageAllowed:
    """Positive cases for post_message_policy."""

    def test_trusted_channel_trusted_message_allowed(self) -> None:
        """Both channel and message from user literal → Allowed."""
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#general", sources=_TRUSTED),
                "message": wrap("Hello team!", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_trusted_channel_untrusted_public_message_allowed(self) -> None:
        """Trusted channel, untrusted message with Public readers → Allowed."""
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#general", sources=_TRUSTED),
                "message": wrap(
                    "Public FAQ answer",
                    sources=_DOC_TOOL,
                    readers=Public,
                ),
            },
        )
        assert result == Allowed()

    def test_trusted_channel_no_message_allowed(self) -> None:
        """Trusted channel, no message kwarg → Allowed."""
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#general", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_no_kwargs_allowed(self) -> None:
        """No kwargs → Allowed."""
        result = post_message_policy("post_message", {})
        assert result == Allowed()


class TestPostMessageDenied:
    """Negative cases for post_message_policy — shallow untrusted."""

    def test_untrusted_channel_denied(self) -> None:
        """Channel from email → Denied with exact channel-injection reason."""
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#attacker-controlled", sources=_EMAIL_TOOL),
                "message": wrap("Hello", sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "channel identifier originates from untrusted source — "
            "possible injection to unauthorized channel"
        )

    def test_untrusted_message_restricted_readers_denied(self) -> None:
        """Trusted channel, untrusted message with restricted readers → Denied."""
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#general", sources=_TRUSTED),
                "message": wrap(
                    "Private document content",
                    sources=_DOC_TOOL,
                    readers=frozenset({"alice@company.com"}),
                ),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "message content from untrusted source has restricted readers — "
            "posting would exfiltrate data to an unauthorised audience"
        )


class TestPostMessageDeniedDeepProvenance:
    """Denied cases with ≥2 hops of untrusted provenance."""

    def test_two_hop_channel_denied(self) -> None:
        """Channel constructed via: '#' + email_body_channel (2-hop) → Denied."""
        hop1 = wrap("attacker-secret", sources=_EMAIL_TOOL)
        two_hop_channel = _hop2_derived("#attacker-secret", _EMAIL_TOOL)
        result = post_message_policy(
            "post_message",
            {
                "channel": two_hop_channel,  # type: ignore[arg-type]
                "message": wrap("Content", sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)
        assert "channel" in result.reason
        _ = hop1

    def test_two_hop_message_restricted_readers_denied(self) -> None:
        """Message derived from document (2-hop) with restricted readers → Denied."""
        hop1 = wrap(
            "Confidential info", sources=_DOC_TOOL, readers=frozenset({"alice@company.com"})
        )
        two_hop_message = wrap(
            "Summary of Confidential info",
            sources=_DOC_TOOL | _CAMEL_TRUSTED,
            readers=frozenset({"alice@company.com"}),
        )
        result = post_message_policy(
            "post_message",
            {
                "channel": wrap("#general", sources=_TRUSTED),
                "message": two_hop_message,
            },
        )
        assert isinstance(result, Denied)
        assert "exfiltrate" in result.reason
        _ = hop1


# ===========================================================================
# 6.  fetch_external_url_policy
# ===========================================================================


class TestFetchExternalUrlAllowed:
    """Positive cases for fetch_external_url_policy."""

    def test_trusted_url_only_allowed(self) -> None:
        """Only url, trusted → Allowed."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/data", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_trusted_url_and_params_allowed(self) -> None:
        """Trusted url and params → Allowed."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/search", sources=_TRUSTED),
                "params": wrap({"q": "hello"}, sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_trusted_url_and_body_allowed(self) -> None:
        """Trusted url and body → Allowed."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/post", sources=_TRUSTED),
                "body": wrap('{"key": "value"}', sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_no_kwargs_allowed(self) -> None:
        """No kwargs → Allowed."""
        result = fetch_external_url_policy("fetch_external_url", {})
        assert result == Allowed()


class TestFetchExternalUrlDenied:
    """Negative cases for fetch_external_url_policy — shallow untrusted."""

    def test_untrusted_url_denied(self) -> None:
        """URL from email → Denied with exact SSRF reason."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://attacker.com/exfil", sources=_EMAIL_TOOL),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "URL originates from untrusted source — possible SSRF or data exfiltration via URL"
        )

    def test_untrusted_params_denied(self) -> None:
        """Trusted URL, params from email → Denied with exact params reason."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/search", sources=_TRUSTED),
                "params": wrap({"secret": "private_data"}, sources=_EMAIL_TOOL),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "query parameters contain data from untrusted source — "
            "possible data exfiltration via URL parameters"
        )

    def test_untrusted_body_denied(self) -> None:
        """Trusted URL and params, body from document → Denied with exact body reason."""
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/post", sources=_TRUSTED),
                "params": wrap({}, sources=_TRUSTED),
                "body": wrap("Confidential data", sources=_DOC_TOOL),
            },
        )
        assert isinstance(result, Denied)
        assert result.reason == (
            "request body contains data from untrusted source — "
            "possible data exfiltration via request body"
        )


class TestFetchExternalUrlDeniedDeepProvenance:
    """Denied cases with ≥2 hops of untrusted provenance."""

    def test_two_hop_url_denied(self) -> None:
        """URL = base + email_derived_path (2-hop) → still denied."""
        hop1 = wrap("/exfil?data=secret", sources=_EMAIL_TOOL)
        two_hop_url = _hop2_derived(
            "https://api.example.com/exfil?data=secret",
            _EMAIL_TOOL,
        )
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": two_hop_url,  # type: ignore[arg-type]
            },
        )
        assert isinstance(result, Denied)
        assert "SSRF" in result.reason
        _ = hop1

    def test_two_hop_params_denied(self) -> None:
        """Params dict assembled from db-derived values (2-hop) → Denied."""
        hop1 = wrap("sensitive_token", sources=_DB_TOOL)
        two_hop_params = _hop2_derived({"token": "sensitive_token"}, _DB_TOOL)
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": wrap("https://api.example.com/query", sources=_TRUSTED),
                "params": two_hop_params,  # type: ignore[arg-type]
            },
        )
        assert isinstance(result, Denied)
        assert "URL parameters" in result.reason
        _ = hop1

    def test_three_hop_url_denied(self) -> None:
        """3-hop URL chain → still untrusted → Denied."""
        hop2 = _hop2_derived("https://evil.com/step2", _EMAIL_TOOL)
        three_hop_url = _hop3_derived(
            "https://evil.com/step3",
            _EMAIL_TOOL | _CAMEL_TRUSTED,
        )
        result = fetch_external_url_policy(
            "fetch_external_url",
            {
                "url": three_hop_url,  # type: ignore[arg-type]
            },
        )
        assert isinstance(result, Denied)
        _ = hop2


# ===========================================================================
# configure_reference_policies / register_all integration smoke tests
# ===========================================================================


class TestConfigureReferencePolicies:
    """Verify that the convenience registration helpers work correctly."""

    def test_configure_registers_all_six_tools(self) -> None:
        """configure_reference_policies registers exactly the six reference tools."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        expected = {
            "send_email",
            "send_money",
            "create_calendar_event",
            "write_file",
            "post_message",
            "fetch_external_url",
        }
        assert registry.registered_tools() == expected

    def test_register_all_alias_works(self) -> None:
        """register_all is an alias for configure_reference_policies."""
        registry = PolicyRegistry()
        register_all(registry, file_owner=FILE_OWNER)
        assert "send_email" in registry.registered_tools()
        assert registry.policy_count("write_file") == 1

    def test_registry_evaluate_send_email_trusted_allowed(self) -> None:
        """Registry.evaluate returns Allowed for a trusted send_email call."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        result = registry.evaluate(
            "send_email",
            {
                "to": wrap("alice@example.com", sources=_TRUSTED),
                "body": wrap("Hi", sources=_TRUSTED),
            },
        )
        assert result == Allowed()

    def test_registry_evaluate_send_money_untrusted_denied(self) -> None:
        """Registry.evaluate returns Denied for an untrusted send_money call."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        result = registry.evaluate(
            "send_money",
            {
                "recipient": wrap("attacker@evil.com", sources=_EMAIL_TOOL),
                "amount": wrap(9999.0, sources=_TRUSTED),
            },
        )
        assert isinstance(result, Denied)

    def test_unregistered_tool_implicitly_allowed(self) -> None:
        """No policies for a tool name → implicit Allowed."""
        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner=FILE_OWNER)
        result = registry.evaluate(
            "some_other_tool",
            {"arg": wrap("value", sources=_TRUSTED)},
        )
        assert result == Allowed()


# ===========================================================================
# NFR-2 guard — no external network calls during test execution
# ===========================================================================


def test_no_llm_calls_in_any_policy() -> None:
    """Smoke test: all six policies execute without making any HTTP calls.

    This test constructs adversarial kwargs for every policy and runs them
    directly.  If any policy tried to make an LLM / HTTP call (which would
    violate NFR-2), it would raise a connection error in the test environment,
    causing this test to fail.
    """
    # send_email
    send_email_policy(
        "send_email",
        {
            "to": wrap("a@example.com", sources=_EMAIL_TOOL),
            "body": wrap("data", readers=frozenset({"b@example.com"})),
        },
    )
    # send_money
    send_money_policy(
        "send_money",
        {
            "recipient": wrap("x", sources=_EMAIL_TOOL),
            "amount": wrap(1.0, sources=_EMAIL_TOOL),
        },
    )
    # create_calendar_event
    create_calendar_event_policy(
        "create_calendar_event",
        {
            "participants": wrap(["x@x.com"], sources=_EMAIL_TOOL),
            "title": wrap("T", readers=frozenset({"other@x.com"})),
        },
    )
    # write_file
    policy = make_write_file_policy("owner@example.com")
    policy(
        "write_file",
        {
            "path": wrap("/tmp/f", sources=_EMAIL_TOOL),
        },
    )
    # post_message
    post_message_policy(
        "post_message",
        {
            "channel": wrap("#c", sources=_EMAIL_TOOL),
        },
    )
    # fetch_external_url
    fetch_external_url_policy(
        "fetch_external_url",
        {
            "url": wrap("https://evil.com", sources=_EMAIL_TOOL),
        },
    )
