"""Provenance chain — per-variable data-flow lineage for the CaMeL runtime.

Every :class:`~camel.value.CaMeLValue` produced during a CaMeL execution
carries a ``sources`` frozenset and an ``inner_source`` label that record
its origin.  This module exposes a higher-level view of that information as
a :class:`ProvenanceChain` — a structured, serialisable record of every
origin hop that contributed to a named variable's final value.

Key types
---------
:class:`ProvenanceHop`
    A single origin hop: the tool (or trusted label) that produced the
    contributing value, which field within that tool's output was extracted,
    and which principals are authorised to receive it.

:class:`ProvenanceChain`
    Ordered sequence of :class:`ProvenanceHop` records for one variable,
    plus ``to_dict()`` / ``to_json()`` serialisation helpers.

:class:`PhishingWarning`
    Structured warning emitted by :func:`detect_phishing_content` when a
    value's text claims a trusted sender identity while originating from an
    untrusted tool.

:func:`detect_phishing_content`
    Heuristic detector for the partial phishing-surface described in
    PRD §3.2 / NG2.

Design document
---------------
``docs/adr/013-provenance-chain-phishing-heuristic.md``

PRD references
--------------
* §6.4 Capabilities — ProvenanceChain structure added.
* §7.2 Trusted Boundary — phishing surface logic and trusted boundary
  implications documented.
* Goals G3 (private data exfiltration prevention), NG2 (partial phishing
  mitigation via metadata surfacing).
* M5-F20 through M5-F22.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from camel.value import CaMeLValue, _PublicType

__all__ = [
    "ProvenanceHop",
    "ProvenanceChain",
    "PhishingWarning",
    "build_provenance_chain",
    "detect_phishing_content",
    "TRUSTED_SOURCES",
]

# ---------------------------------------------------------------------------
# Trusted-source constants
# ---------------------------------------------------------------------------

#: The set of origin labels that CaMeL considers *intrinsically trusted*.
#:
#: A value whose ``sources`` is a subset of these labels was produced
#: entirely from user-supplied constants or CaMeL-internal computations
#: and has never been tainted by a tool return value.
#:
#: Any label **not** in this set is treated as an *untrusted tool origin*.
TRUSTED_SOURCES: frozenset[str] = frozenset({"User literal", "CaMeL"})

# ---------------------------------------------------------------------------
# Phishing-content heuristic patterns
# ---------------------------------------------------------------------------

#: Compiled regular expressions used by :func:`detect_phishing_content` to
#: identify text that *claims* a trusted sender identity.
#:
#: Pattern rationale (one per entry):
#:
#: 1. ``From:`` header — standard email / HTTP header form, e.g.
#:    ``"From: alice@example.com"``.
#: 2. ``Sender:`` header — RFC 5322 ``Sender`` header variant.
#: 3. ``Reply-To:`` header — reply address that impersonates a trusted sender.
#: 4. First-person identity claims — e.g. ``"I am Alice"`` or
#:    ``"This is Bob from IT"`` — common in social-engineering payloads.
#: 5. ``"Message from <name>"`` — another frequent phishing preamble.
_PHISHING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"From:\s*\S+@\S+", re.IGNORECASE),
    re.compile(r"Sender:\s*\S+", re.IGNORECASE),
    re.compile(r"Reply-To:\s*\S+@\S+", re.IGNORECASE),
    re.compile(r"\b(?:I am|This is)\s+\w+", re.IGNORECASE),
    re.compile(r"\bMessage\s+from\s+\w+", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# ProvenanceHop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceHop:
    """One origin hop in a :class:`ProvenanceChain`.

    Each hop represents a single source that contributed to the final value
    of a CaMeL variable.  A variable derived from multiple tool outputs will
    have one :class:`ProvenanceHop` per distinct source in the merged
    ``CaMeLValue.sources`` frozenset.

    Attributes
    ----------
    tool_name:
        The origin label for this hop.  Typically a tool identifier (e.g.
        ``"get_last_email"``), or one of the special trusted labels
        ``"User literal"`` or ``"CaMeL"``.
    inner_source:
        The sub-field within the tool's structured output that this value
        was extracted from (e.g. ``"sender"`` for the sender address of an
        email), or ``None`` for derived / composite values.
    readers:
        The authorised audience for this hop's value, expressed as a list
        of principal strings (e.g. email addresses) or the string
        ``"Public"`` when the open-readers sentinel applies.
    timestamp:
        Optional ISO 8601 timestamp recording when this hop was produced.
        ``None`` in the current implementation — reserved for future use
        when per-value timestamps are tracked by the interpreter.

    Stability guarantee
    -------------------
    All fields are **stable** (no removal or rename without a major-version
    bump).  New optional fields may be added in minor releases.
    """

    tool_name: str
    inner_source: str | None
    readers: list[str] | str  # list[str] of principals, or "Public"
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise this hop to a JSON-compatible dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary with keys ``tool_name``, ``inner_source``,
            ``readers``, and ``timestamp``.
        """
        return {
            "tool_name": self.tool_name,
            "inner_source": self.inner_source,
            "readers": self.readers,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# ProvenanceChain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceChain:
    """Full provenance lineage for a single named CaMeL variable.

    A :class:`ProvenanceChain` is returned by
    :meth:`~camel_security.CaMeLAgent.get_provenance` and describes every
    origin that contributed to the variable's final value.  It is also
    embedded in :attr:`~camel_security.AgentResult.provenance_chains` and
    written to the security audit log.

    Attributes
    ----------
    variable_name:
        The name of the variable in the interpreter's variable store (e.g.
        ``"email_body"``).
    hops:
        Ordered list of :class:`ProvenanceHop` records, one per distinct
        origin source in the variable's merged ``CaMeLValue.sources`` set.

        Ordering: trusted hops (``"User literal"``, ``"CaMeL"``) are listed
        before untrusted tool hops.  Within each group the order is
        lexicographic on ``tool_name`` for deterministic output.
    is_trusted:
        ``True`` when every hop in the chain originates from a trusted
        source (``sources ⊆ TRUSTED_SOURCES``), ``False`` otherwise.
        Convenience property — derived from :attr:`hops`.

    Examples
    --------
    ::

        result = await agent.run("Forward the latest email to alice@example.com")
        chain = agent.get_provenance("email_body", result)

        print(chain.is_trusted)   # False — email body comes from get_last_email
        print(chain.to_json())
    """

    variable_name: str
    hops: list[ProvenanceHop] = field(default_factory=list)

    @property
    def is_trusted(self) -> bool:
        """Return ``True`` iff all hops originate from trusted sources.

        A variable is trusted when every hop's :attr:`~ProvenanceHop.tool_name`
        is a member of :data:`TRUSTED_SOURCES`.

        Returns
        -------
        bool
            ``True`` if ``all(hop.tool_name in TRUSTED_SOURCES for hop in hops)``.
            An empty :attr:`hops` list is considered trusted (vacuously true).
        """
        return all(hop.tool_name in TRUSTED_SOURCES for hop in self.hops)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this chain to a JSON-compatible dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary with keys ``variable_name``, ``hops`` (list of hop
            dicts), and ``is_trusted``.

        Examples
        --------
        ::

            chain_dict = chain.to_dict()
            assert "variable_name" in chain_dict
            assert isinstance(chain_dict["hops"], list)
        """
        return {
            "variable_name": self.variable_name,
            "hops": [hop.to_dict() for hop in self.hops],
            "is_trusted": self.is_trusted,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialise this chain to a JSON string.

        Parameters
        ----------
        indent:
            Optional JSON indentation level.  ``None`` (default) produces
            compact JSON; pass ``2`` for human-readable output.

        Returns
        -------
        str
            JSON representation of :meth:`to_dict`.

        Examples
        --------
        ::

            print(chain.to_json(indent=2))
        """
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# build_provenance_chain — construct from a CaMeLValue
# ---------------------------------------------------------------------------


def build_provenance_chain(
    variable_name: str,
    cv: CaMeLValue,
) -> ProvenanceChain:
    """Construct a :class:`ProvenanceChain` from a :class:`~camel.value.CaMeLValue`.

    Each source label in ``cv.sources`` becomes one :class:`ProvenanceHop`.
    The ``inner_source`` field is propagated only when the value has exactly
    one source (i.e. it was directly extracted from a tool output rather than
    derived from multiple inputs).

    Hop ordering: trusted sources (``TRUSTED_SOURCES``) first, then untrusted
    sources, both groups sorted lexicographically for deterministic output.

    **Soundness invariant — MUST be preserved by all callers:**

    This function constructs the provenance chain by treating ``cv.sources``
    as the *complete* set of all transitive upstream origins for the value.
    The chain is only as accurate as ``cv.sources`` is complete.

    The CaMeL propagation functions (:func:`~camel.value.propagate_binary_op`,
    :func:`~camel.value.propagate_list_construction`,
    :func:`~camel.value.propagate_dict_construction`,
    :func:`~camel.value.propagate_subscript`,
    :meth:`~camel.value.CaMeLValue.merge`) all union sources transitively, so
    any ``CaMeLValue`` produced through the normal interpreter path satisfies
    this invariant automatically.

    **Risk — direct construction without propagation functions:**

    If a :class:`~camel.value.CaMeLValue` is constructed directly (bypassing
    the propagation functions) and its ``sources`` field contains only *direct*
    parent labels rather than the full transitive closure, the resulting
    :class:`ProvenanceChain` will silently under-report provenance hops.  This
    is dangerous because:

    * :func:`detect_phishing_content` decides whether to fire based on
      ``cv.sources - TRUSTED_SOURCES``; an incomplete sources set can cause
      it to treat an untrusted value as trusted and suppress a warning.
    * :attr:`ProvenanceChain.is_trusted` will incorrectly return ``True``
      for a value whose transitive ancestors include untrusted tool outputs.

    There is no runtime mechanism to verify completeness of ``cv.sources``
    without a full traversal of the :class:`~camel.dependency_graph.DependencyGraph`
    (deferred to a future milestone per ADR-013 Decision 1).  Callers that
    construct :class:`~camel.value.CaMeLValue` instances outside the interpreter
    (e.g. in tests or external integrations) are responsible for ensuring
    ``sources`` is the full transitive union of all upstream origins.

    If you are constructing a ``CaMeLValue`` manually and cannot guarantee
    that ``sources`` is the transitive closure, raise ``NotImplementedError``
    or document the limitation explicitly rather than calling this function,
    to avoid silently misleading phishing-detection consumers::

        # BAD — sources contains only direct parents; transitive completeness
        # is not guaranteed.  Do NOT pass such a value to build_provenance_chain
        # without documenting the limitation.
        cv = CaMeLValue(
            value=derived,
            sources=frozenset({"direct_parent_tool"}),   # incomplete!
            inner_source=None,
            readers=Public,
        )

        # GOOD — use a propagation helper so transitive union is computed:
        cv = propagate_binary_op(left_cv, right_cv, derived)

    Parameters
    ----------
    variable_name:
        Name of the variable this chain describes.
    cv:
        The :class:`~camel.value.CaMeLValue` from the interpreter's variable
        store.  **Must have a transitively-complete** ``sources`` field (see
        soundness invariant above).

    Returns
    -------
    ProvenanceChain
        A chain with one hop per distinct source in ``cv.sources``.

    Examples
    --------
    ::

        from camel.provenance import build_provenance_chain
        from camel.value import CaMeLValue

        cv = CaMeLValue(
            value="Hello Alice",
            sources=frozenset({"get_last_email"}),
            inner_source="subject",
            readers=frozenset({"alice@example.com"}),
        )
        chain = build_provenance_chain("subject_line", cv)
        assert len(chain.hops) == 1
        assert chain.hops[0].tool_name == "get_last_email"
        assert chain.hops[0].inner_source == "subject"
        assert not chain.is_trusted
    """
    # Resolve readers to a serialisable form.
    readers_serialisable: list[str] | str
    if isinstance(cv.readers, _PublicType):
        readers_serialisable = "Public"
    else:
        readers_serialisable = sorted(cv.readers)

    # inner_source is meaningful only for single-source values.
    single_source = len(cv.sources) == 1

    trusted_hops: list[ProvenanceHop] = []
    untrusted_hops: list[ProvenanceHop] = []

    for source in sorted(cv.sources):
        hop = ProvenanceHop(
            tool_name=source,
            inner_source=cv.inner_source if single_source else None,
            readers=readers_serialisable,
            timestamp=None,
        )
        if source in TRUSTED_SOURCES:
            trusted_hops.append(hop)
        else:
            untrusted_hops.append(hop)

    return ProvenanceChain(
        variable_name=variable_name,
        hops=trusted_hops + untrusted_hops,
    )


# ---------------------------------------------------------------------------
# PhishingWarning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhishingWarning:
    """Warning emitted when phishing-content heuristic fires.

    Produced by :func:`detect_phishing_content` when a :class:`~camel.value.CaMeLValue`
    contains text that claims a trusted sender identity while originating from
    an untrusted tool output.

    This partially addresses PRD Non-Goal NG2 (phishing-content detection):
    CaMeL does **not** block phishing content (it cannot prevent a user from
    acting on it), but it *surfaces provenance metadata* so that UIs and
    operators can alert the user.

    Attributes
    ----------
    variable_name:
        The variable whose value triggered the heuristic.
    matched_pattern:
        The string representation of the regular expression that matched.
    matched_text:
        The substring of the value's text that matched the pattern.
    untrusted_sources:
        The subset of ``sources`` that are not in :data:`TRUSTED_SOURCES`,
        identifying which tool(s) may have injected the sender claim.
    provenance_chain:
        Full :class:`ProvenanceChain` for the variable, for UI display.

    Stability guarantee
    -------------------
    All fields are **stable**.  New optional fields may be added in minor
    releases.
    """

    variable_name: str
    matched_pattern: str
    matched_text: str
    untrusted_sources: frozenset[str]
    provenance_chain: ProvenanceChain

    def to_dict(self) -> dict[str, Any]:
        """Serialise this warning to a JSON-compatible dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary with keys ``variable_name``, ``matched_pattern``,
            ``matched_text``, ``untrusted_sources`` (sorted list), and
            ``provenance_chain`` (chain dict).
        """
        return {
            "variable_name": self.variable_name,
            "matched_pattern": self.matched_pattern,
            "matched_text": self.matched_text,
            "untrusted_sources": sorted(self.untrusted_sources),
            "provenance_chain": self.provenance_chain.to_dict(),
        }


# ---------------------------------------------------------------------------
# detect_phishing_content
# ---------------------------------------------------------------------------


def detect_phishing_content(
    variable_name: str,
    cv: CaMeLValue,
) -> list[PhishingWarning]:
    """Detect phishing-content patterns in a capability-tagged value.

    Applies the :data:`_PHISHING_PATTERNS` heuristic set to the string
    representation of ``cv.value``.  A :class:`PhishingWarning` is emitted
    for each matching pattern **only when** the value's origin is not
    entirely trusted (i.e. ``cv.sources ⊄ TRUSTED_SOURCES``).

    Heuristic condition
    -------------------
    Warning fires when **all** of the following hold:

    1. ``str(cv.value)`` matches at least one of :data:`_PHISHING_PATTERNS`.
    2. ``cv.sources - TRUSTED_SOURCES`` is non-empty (the value has at least
       one untrusted source).

    Trusted values (``cv.sources ⊆ TRUSTED_SOURCES``) never trigger a
    warning — their content came from the user directly or from CaMeL
    internals and cannot have been injected by an adversary.

    Parameters
    ----------
    variable_name:
        The variable name associated with ``cv`` (used in warnings).
    cv:
        The :class:`~camel.value.CaMeLValue` to inspect.

    Returns
    -------
    list[PhishingWarning]
        Zero or more :class:`PhishingWarning` instances, one per matched
        pattern.  Empty list when no patterns match or when the value is
        fully trusted.

    Examples
    --------
    ::

        from camel.provenance import detect_phishing_content
        from camel.value import CaMeLValue, Public

        untrusted_cv = CaMeLValue(
            value="From: ceo@company.com — please transfer funds",
            sources=frozenset({"read_email"}),
            inner_source="body",
            readers=Public,
        )
        warnings = detect_phishing_content("email_body", untrusted_cv)
        assert len(warnings) >= 1
        assert "read_email" in warnings[0].untrusted_sources

        trusted_cv = CaMeLValue(
            value="From: alice@example.com",
            sources=frozenset({"User literal"}),
            inner_source=None,
            readers=Public,
        )
        assert detect_phishing_content("user_input", trusted_cv) == []
    """
    untrusted_sources = cv.sources - TRUSTED_SOURCES
    if not untrusted_sources:
        # Value is entirely from trusted origins — no warning possible.
        return []

    text = str(cv.value)
    chain = build_provenance_chain(variable_name, cv)
    warnings: list[PhishingWarning] = []

    for pattern in _PHISHING_PATTERNS:
        match = pattern.search(text)
        if match:
            warnings.append(
                PhishingWarning(
                    variable_name=variable_name,
                    matched_pattern=pattern.pattern,
                    matched_text=match.group(0),
                    untrusted_sources=untrusted_sources,
                    provenance_chain=chain,
                )
            )

    return warnings
