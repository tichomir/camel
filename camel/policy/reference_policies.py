"""CaMeL Reference Policy Library.

This module provides the six baseline security policies that ship with CaMeL.
Together they enforce Goals **G2** (prevent data-flow manipulation) and **G3**
(prevent private data exfiltration) for the most common side-effecting tool
categories found in AgentDojo and real-world LLM agent deployments.

Available policies
------------------
:func:`send_email_policy`
    Prevents injected recipients and private-content exfiltration via email.
:func:`send_money_policy`
    Ensures both transfer recipient and amount originate exclusively from
    trusted (user-controlled) sources.
:func:`create_calendar_event_policy`
    Prevents injected attendees and private-content exposure via calendar
    events.
:func:`make_write_file_policy`
    Factory that returns a policy parameterised by the file system owning
    user; prevents path injection and unauthorised content writes.
:func:`post_message_policy`
    Prevents channel injection and private-content exfiltration via messaging.
:func:`fetch_external_url_policy`
    Prevents SSRF and data exfiltration via URL, query parameters, and request
    body.

Convenience registration
------------------------
Use :func:`configure_reference_policies` to register all six policies against
a :class:`~camel.policy.interfaces.PolicyRegistry` in one call::

    from camel.policy import PolicyRegistry
    from camel.policy.reference_policies import configure_reference_policies

    registry = PolicyRegistry()
    configure_reference_policies(registry, file_owner="alice@example.com")

The ``register_all`` alias is also exported for compatibility::

    from camel.policy.reference_policies import register_all
    register_all(registry, file_owner="alice@example.com")

Configuration guidance
----------------------
To exclude a policy (e.g. no file writes in a deployment), register only the
policies you need by calling the individual ``registry.register(...)`` calls
manually instead of using :func:`configure_reference_policies`.

To extend a policy, register additional :data:`~camel.policy.interfaces.PolicyFn`
callables for the same tool name after calling
:func:`configure_reference_policies`; all registered policies must return
:class:`~camel.policy.interfaces.Allowed` for the tool call to proceed.
"""

from __future__ import annotations

from collections.abc import Mapping

from camel.policy.interfaces import (
    Allowed,
    Denied,
    PolicyFn,
    PolicyRegistry,
    SecurityPolicyResult,
    can_readers_read_value,
    is_trusted,
)
from camel.value import CaMeLValue, _PublicType

# ---------------------------------------------------------------------------
# 1. send_email
# ---------------------------------------------------------------------------


def send_email_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Prevent injected recipients and unauthorised content forwarding via email.

    **Check 1 — Recipient provenance:**
    If the ``to`` argument is not trusted (i.e. it did not originate exclusively
    from ``"User literal"`` or ``"CaMeL"`` sources), the policy verifies that
    the raw recipient address is already an authorised reader of every content
    field present (``body`` and ``subject``).  If any content field is present
    and the recipient is not an authorised reader of it, the call is denied.

    If ``to`` is trusted, the call is allowed unconditionally (both checks are
    skipped).

    Parameters
    ----------
    tool_name:
        The name of the tool being invoked (``"send_email"``).
    kwargs:
        Argument mapping including at minimum ``"to"``, and optionally
        ``"body"`` and ``"subject"``.

    Returns
    -------
    SecurityPolicyResult
        :class:`~camel.policy.interfaces.Allowed` if the call is safe to
        proceed; :class:`~camel.policy.interfaces.Denied` with a descriptive
        reason otherwise.

    Examples
    --------
    ::

        from camel.value import wrap, Public
        from camel.policy.reference_policies import send_email_policy

        # Trusted recipient — always allowed
        result = send_email_policy("send_email", {
            "to": wrap("alice@example.com", sources=frozenset({"User literal"})),
            "body": wrap("Hello", sources=frozenset({"User literal"})),
        })
        assert result == Allowed()

        # Untrusted recipient, public body — allowed
        result = send_email_policy("send_email", {
            "to": wrap("eve@example.com", sources=frozenset({"read_email"})),
            "body": wrap("Hello", sources=frozenset({"read_email"}), readers=Public),
        })
        assert result == Allowed()
    """
    to_cv = kwargs.get("to")
    if to_cv is None:
        return Allowed()

    if is_trusted(to_cv):
        return Allowed()

    # Recipient is untrusted — check each content field
    recipient_raw = to_cv.raw
    if isinstance(recipient_raw, list):
        recipients: list[str] = [str(r) for r in recipient_raw]
    elif isinstance(recipient_raw, str):
        # Handle comma-separated addresses in a single string
        recipients = [addr.strip() for addr in recipient_raw.split(",") if addr.strip()]
        if not recipients:
            recipients = [recipient_raw]
    else:
        recipients = [str(recipient_raw)]

    for field in ("body", "subject"):
        content_cv = kwargs.get(field)
        if content_cv is None:
            continue
        for recipient in recipients:
            if not can_readers_read_value(content_cv, recipient):
                return Denied(
                    f"recipient address from untrusted source is not an "
                    f"authorised reader of the email {field}"
                )

    return Allowed()


# ---------------------------------------------------------------------------
# 2. send_money
# ---------------------------------------------------------------------------


def send_money_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Prevent financial fraud by requiring recipient and amount to be fully trusted.

    This is the strictest reference policy.  Both the ``recipient`` and
    ``amount`` arguments must originate exclusively from trusted sources
    (``"User literal"`` or ``"CaMeL"``).  Any untrusted taint — even one hop
    removed — blocks the transfer.

    The ``memo`` and ``currency`` fields are **not** checked; they are
    lower-stakes fields.  Deployments may add additional policies for those
    fields if required.

    Parameters
    ----------
    tool_name:
        The name of the tool being invoked (``"send_money"``).
    kwargs:
        Argument mapping including at minimum ``"recipient"`` and/or
        ``"amount"``.

    Returns
    -------
    SecurityPolicyResult
        :class:`~camel.policy.interfaces.Allowed` if both checked fields are
        fully trusted; :class:`~camel.policy.interfaces.Denied` with a
        descriptive reason on the first untrusted field encountered.

    Examples
    --------
    ::

        from camel.value import wrap
        from camel.policy.reference_policies import send_money_policy

        result = send_money_policy("send_money", {
            "recipient": wrap("alice@bank.com", sources=frozenset({"User literal"})),
            "amount": wrap(100.0, sources=frozenset({"User literal"})),
        })
        assert result == Allowed()
    """
    recipient_cv = kwargs.get("recipient")
    amount_cv = kwargs.get("amount")

    if recipient_cv is not None and not is_trusted(recipient_cv):
        return Denied(
            "transfer recipient originates from untrusted source — possible data-flow manipulation"
        )

    if amount_cv is not None and not is_trusted(amount_cv):
        return Denied(
            "transfer amount originates from untrusted source — possible data-flow manipulation"
        )

    return Allowed()


# ---------------------------------------------------------------------------
# 3. create_calendar_event
# ---------------------------------------------------------------------------


def create_calendar_event_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Prevent injected attendees and private-content exposure via calendar events.

    If the ``participants`` argument is trusted (user-controlled), the call is
    allowed unconditionally.  If ``participants`` is untrusted, every participant
    in the raw list must be an authorised reader of every content field present
    (``title``, ``description``, ``location``).  The first participant/field
    combination that fails the readers check triggers a ``Denied``.

    The ``start_time`` and ``end_time`` fields are **not** checked by this
    policy; they are scheduling metadata, not content.  Deployments may add a
    separate policy for scheduling manipulation if required.

    Parameters
    ----------
    tool_name:
        The name of the tool being invoked (``"create_calendar_event"``).
    kwargs:
        Argument mapping containing ``"participants"`` and optionally
        ``"title"``, ``"description"``, ``"location"``.

    Returns
    -------
    SecurityPolicyResult
        :class:`~camel.policy.interfaces.Allowed` or
        :class:`~camel.policy.interfaces.Denied` with a descriptive reason.

    Examples
    --------
    ::

        from camel.value import wrap
        from camel.policy.reference_policies import create_calendar_event_policy

        result = create_calendar_event_policy("create_calendar_event", {
            "participants": wrap(
                ["alice@example.com"],
                sources=frozenset({"User literal"}),
            ),
            "title": wrap("Sync", sources=frozenset({"User literal"})),
        })
        assert result == Allowed()
    """
    participants_cv = kwargs.get("participants")
    if participants_cv is None:
        return Allowed()

    if is_trusted(participants_cv):
        return Allowed()

    # Participants are untrusted — check content field readers
    participants_raw = participants_cv.raw
    if isinstance(participants_raw, list):
        participant_list: list[str] = [str(p) for p in participants_raw]
    else:
        participant_list = [str(participants_raw)]

    content_fields: dict[str, CaMeLValue | None] = {
        "title": kwargs.get("title"),
        "description": kwargs.get("description"),
        "location": kwargs.get("location"),
    }

    for participant in participant_list:
        for field_name, field_cv in content_fields.items():
            if field_cv is None:
                continue
            if not can_readers_read_value(field_cv, participant):
                return Denied(
                    f"calendar event {field_name!r} is not authorised to be "
                    f"shared with participant {participant!r} — "
                    f"untrusted participant list may include injected attendees"
                )

    return Allowed()


# ---------------------------------------------------------------------------
# 4. write_file (factory)
# ---------------------------------------------------------------------------


def make_write_file_policy(owner: str) -> PolicyFn:
    """Return a ``write_file`` policy parameterised by the file system owner.

    The policy enforces two checks:

    1. **Path provenance:** the ``path`` argument must originate exclusively
       from trusted sources (``"User literal"`` or ``"CaMeL"``); untrusted
       paths are rejected to prevent path injection attacks.
    2. **Content readers:** the ``content`` argument's ``readers`` set must
       include ``owner``; content that is not authorised for the file system
       owner must not be written.

    Parameters
    ----------
    owner:
        The string identity of the file system owning user (e.g.
        ``"alice@example.com"``).  This value is captured in the returned
        policy's closure and used for every call to ``write_file``.

    Returns
    -------
    PolicyFn
        A :data:`~camel.policy.interfaces.PolicyFn` that can be registered
        against ``"write_file"`` in a :class:`~camel.policy.interfaces.PolicyRegistry`.

    Examples
    --------
    ::

        from camel.value import wrap, Public
        from camel.policy.reference_policies import make_write_file_policy

        policy = make_write_file_policy("alice@example.com")

        # Allowed — path is trusted, content readers include owner
        result = policy("write_file", {
            "path": wrap("/tmp/report.txt", sources=frozenset({"User literal"})),
            "content": wrap("data", readers=frozenset({"alice@example.com"})),
        })
        assert result == Allowed()
    """

    def write_file_policy(
        tool_name: str,
        kwargs: Mapping[str, CaMeLValue],
    ) -> SecurityPolicyResult:
        """Enforce path-injection and owner-readers checks for write_file.

        Parameters
        ----------
        tool_name:
            The name of the tool being invoked (``"write_file"``).
        kwargs:
            Argument mapping containing ``"path"`` and/or ``"content"``.

        Returns
        -------
        SecurityPolicyResult
            :class:`~camel.policy.interfaces.Allowed` or
            :class:`~camel.policy.interfaces.Denied` with a descriptive
            reason.
        """
        path_cv = kwargs.get("path")
        content_cv = kwargs.get("content")

        if path_cv is not None and not is_trusted(path_cv):
            return Denied("file path originates from untrusted source — possible path injection")

        if content_cv is not None:
            if not can_readers_read_value(content_cv, owner):
                return Denied(
                    f"file content is not authorised to be written by the "
                    f"file system owner {owner!r}"
                )

        return Allowed()

    return write_file_policy


# ---------------------------------------------------------------------------
# 5. post_message
# ---------------------------------------------------------------------------


def post_message_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Prevent channel injection and private-content exfiltration via messaging.

    Two checks are applied:

    1. **Channel provenance:** the ``channel`` argument must be trusted; an
       untrusted channel identifier indicates a possible injection to an
       attacker-controlled channel.
    2. **Message content:** if the ``message`` is untrusted and its
       ``readers`` field is not :data:`~camel.value.Public`, the call is
       denied.  Untrusted content with a restricted reader set must not be
       broadcast to an unverified audience.

    If the ``message`` is trusted (user-authored), or its readers are
    :data:`~camel.value.Public` (the content is already public), the content
    check passes.

    Parameters
    ----------
    tool_name:
        The name of the tool being invoked (``"post_message"``).
    kwargs:
        Argument mapping containing ``"channel"`` and/or ``"message"``.

    Returns
    -------
    SecurityPolicyResult
        :class:`~camel.policy.interfaces.Allowed` or
        :class:`~camel.policy.interfaces.Denied` with a descriptive reason.

    Examples
    --------
    ::

        from camel.value import wrap, Public
        from camel.policy.reference_policies import post_message_policy

        # Trusted channel, trusted message — allowed
        result = post_message_policy("post_message", {
            "channel": wrap("#general", sources=frozenset({"User literal"})),
            "message": wrap("Hello!", sources=frozenset({"User literal"})),
        })
        assert result == Allowed()
    """
    channel_cv = kwargs.get("channel")
    message_cv = kwargs.get("message")

    if channel_cv is not None and not is_trusted(channel_cv):
        return Denied(
            "channel identifier originates from untrusted source — "
            "possible injection to unauthorized channel"
        )

    if message_cv is not None:
        if not is_trusted(message_cv) and not isinstance(message_cv.readers, _PublicType):
            return Denied(
                "message content from untrusted source has restricted readers — "
                "posting would exfiltrate data to an unauthorised audience"
            )

    return Allowed()


# ---------------------------------------------------------------------------
# 6. fetch_external_url
# ---------------------------------------------------------------------------


def fetch_external_url_policy(
    tool_name: str,
    kwargs: Mapping[str, CaMeLValue],
) -> SecurityPolicyResult:
    """Prevent SSRF and data exfiltration via externally fetched URLs.

    Three checks are applied in order:

    1. **URL provenance:** the ``url`` must originate exclusively from trusted
       sources; any untrusted taint suggests URL injection or SSRF.
    2. **Query parameter provenance:** the ``params`` dict must originate
       exclusively from trusted sources; untrusted parameters may leak private
       data as query string values.
    3. **Request body provenance:** the ``body`` must originate exclusively
       from trusted sources; untrusted body content may exfiltrate private
       data via POST requests.

    The ``method`` field is **not** checked; ``GET``/``POST`` method
    manipulation is lower risk than URL/body manipulation.

    Parameters
    ----------
    tool_name:
        The name of the tool being invoked (``"fetch_external_url"``).
    kwargs:
        Argument mapping containing ``"url"`` and optionally ``"params"``
        and ``"body"``.

    Returns
    -------
    SecurityPolicyResult
        :class:`~camel.policy.interfaces.Allowed` or
        :class:`~camel.policy.interfaces.Denied` with a descriptive reason.

    Examples
    --------
    ::

        from camel.value import wrap
        from camel.policy.reference_policies import fetch_external_url_policy

        result = fetch_external_url_policy("fetch_external_url", {
            "url": wrap(
                "https://api.example.com/data",
                sources=frozenset({"User literal"}),
            ),
        })
        assert result == Allowed()
    """
    url_cv = kwargs.get("url")
    params_cv = kwargs.get("params")
    body_cv = kwargs.get("body")

    if url_cv is not None and not is_trusted(url_cv):
        return Denied(
            "URL originates from untrusted source — possible SSRF or data exfiltration via URL"
        )

    if params_cv is not None and not is_trusted(params_cv):
        return Denied(
            "query parameters contain data from untrusted source — "
            "possible data exfiltration via URL parameters"
        )

    if body_cv is not None and not is_trusted(body_cv):
        return Denied(
            "request body contains data from untrusted source — "
            "possible data exfiltration via request body"
        )

    return Allowed()


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def configure_reference_policies(
    registry: PolicyRegistry,
    *,
    file_owner: str,
) -> None:
    """Register all six reference policies into ``registry``.

    This is the primary entry point for deployers who want the full baseline
    policy set.  Pass the registry that will be injected into the CaMeL
    interpreter, and supply the ``file_owner`` identity used by the
    ``write_file`` policy.

    Parameters
    ----------
    registry:
        The :class:`~camel.policy.interfaces.PolicyRegistry` instance to
        populate.
    file_owner:
        The string identity of the file system owning user (e.g.
        ``"alice@example.com"``).  Required by :func:`make_write_file_policy`.

    Examples
    --------
    ::

        from camel.policy import PolicyRegistry
        from camel.policy.reference_policies import configure_reference_policies

        registry = PolicyRegistry()
        configure_reference_policies(registry, file_owner="alice@example.com")
        # registry now enforces all six reference policies
    """
    registry.register("send_email", send_email_policy)
    registry.register("send_money", send_money_policy)
    registry.register("create_calendar_event", create_calendar_event_policy)
    registry.register("write_file", make_write_file_policy(file_owner))
    registry.register("post_message", post_message_policy)
    registry.register("fetch_external_url", fetch_external_url_policy)


def register_all(
    registry: PolicyRegistry,
    *,
    file_owner: str,
) -> None:
    """Alias for :func:`configure_reference_policies`.

    Provided for compatibility with the sprint acceptance criteria which
    requires a ``register_all(registry)`` convenience function.

    Parameters
    ----------
    registry:
        The :class:`~camel.policy.interfaces.PolicyRegistry` instance to
        populate.
    file_owner:
        The string identity of the file system owning user.  Forwarded to
        :func:`configure_reference_policies`.
    """
    configure_reference_policies(registry, file_owner=file_owner)


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    "send_email_policy",
    "send_money_policy",
    "create_calendar_event_policy",
    "make_write_file_policy",
    "post_message_policy",
    "fetch_external_url_policy",
    "configure_reference_policies",
    "register_all",
]
