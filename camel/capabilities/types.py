"""Capability Assignment Engine — core type definitions and protocol stubs.

This module is the canonical home for the *types* that drive the CaMeL
Capability Assignment Engine.  It re-exports the foundational
:class:`~camel.value.CaMeLValue` and :data:`~camel.value.Public` symbols
from :mod:`camel.value` and adds the :data:`CapabilityAnnotationFn` protocol
that every tool-specific capability annotator must satisfy.

Overview
--------
Every value produced by a tool call is wrapped in a
:class:`~camel.value.CaMeLValue`.  The wrapper carries three capability
fields:

``sources``
    A ``frozenset[str]`` of *origin labels* — typically the tool ID
    (e.g. ``"read_email"``) or the special labels ``"User literal"`` and
    ``"CaMeL"``.

``inner_source``
    An optional ``str`` naming the *field* within a structured tool response
    from which this value was directly extracted (e.g. ``"sender"`` for the
    sender address of an email).  ``None`` for derived / composite values.

``readers``
    Either a ``frozenset[str]`` of authorised principals (e.g. email
    addresses) or the :data:`~camel.value.Public` singleton, which means
    *any* reader is permitted.  An empty ``frozenset()`` means *no external
    reader is permitted*.

Capability propagation rules
----------------------------
When a *derived* value is computed from one or more ``CaMeLValue`` inputs
(e.g. string concatenation, arithmetic, list construction), its capabilities
are determined by the following **union rules**:

1. **sources** — union of all input ``sources`` sets.
2. **inner_source** — always ``None`` for derived values; only meaningful on
   values extracted directly from a tool's structured return value.
3. **readers** — union of all input ``readers`` sets, where
   :data:`~camel.value.Public` is the *top* (absorbing) element:

   * ``frozenset_A ∪ frozenset_B``  →  ``frozenset_A | frozenset_B``
   * ``Public ∪ anything``          →  ``Public``
   * ``frozenset() ∪ frozenset()``  →  ``frozenset()``  (no readers)

These rules are implemented in :mod:`camel.value` (see
:func:`~camel.value.propagate_binary_op`,
:func:`~camel.value.propagate_list_construction`, etc.) and are *enforced*
by the interpreter at every expression evaluation step.

CapabilityAnnotationFn contract
---------------------------------
A *capability annotator* is a callable with the signature::

    def annotate(
        return_value: Any,
        tool_kwargs: Mapping[str, Any],
    ) -> CaMeLValue:
        ...

It receives the *raw* Python value returned by a tool and the keyword
arguments that were passed to that tool, and returns a ``CaMeLValue`` with
the appropriate capability metadata attached.

**Default annotation** (applied when no annotator is registered for a tool):

* ``sources = frozenset({tool_id})``
* ``inner_source = None``
* ``readers = Public``

**Tool-specific annotations** override the defaults to express finer
provenance, e.g.:

* ``read_email`` — attaches ``inner_source="sender"`` to the sender address,
  ``inner_source="subject"`` to the subject line, and so on for each field.
* Cloud storage tools — populate ``readers`` from the document's sharing
  permissions (``frozenset[str]`` of authorised email addresses, or
  :data:`~camel.value.Public` for publicly shared documents).

Annotators **must not** raise exceptions on valid tool output; they may
return a ``CaMeLValue`` with :data:`~camel.value.Public` readers as a safe
fallback if permissions cannot be determined.

Imports / re-exports
--------------------
This module re-exports the canonical types from :mod:`camel.value` so that
downstream code can import everything capability-related from a single
location::

    from camel.capabilities.types import (
        CaMeLValue,
        Public,
        Readers,
        _PublicType,
        CapabilityAnnotationFn,
    )

"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from camel.value import CaMeLValue, Public, Readers, _PublicType

# ---------------------------------------------------------------------------
# CapabilityAnnotationFn
# ---------------------------------------------------------------------------

#: Type alias for a *capability annotator* function.
#:
#: A capability annotator is a pure function registered alongside a tool that
#: maps the tool's raw return value and the kwargs it received to a fully
#: capability-tagged :class:`~camel.value.CaMeLValue`.
#:
#: Signature
#: ---------
#: ``(return_value: Any, tool_kwargs: Mapping[str, Any]) -> CaMeLValue``
#:
#: Parameters
#: ----------
#: return_value:
#:     The raw Python object returned by the tool (before capability tagging).
#: tool_kwargs:
#:     The keyword arguments that were passed to the tool.  These may carry
#:     information useful for computing the ``readers`` field, e.g. a
#:     ``document_id`` parameter whose ACL the annotator resolves.
#:
#: Returns
#: -------
#: CaMeLValue
#:     A capability-tagged wrapper around ``return_value``.  The annotator is
#:     responsible for setting ``sources``, ``inner_source``, and ``readers``
#:     fields correctly.
#:
#: Example
#: -------
#: ::
#:
#:     def annotate_read_email(
#:         return_value: Any,
#:         tool_kwargs: Mapping[str, Any],
#:     ) -> CaMeLValue:
#:         \"\"\"Tag email fields with appropriate inner_source labels.\"\"\"
#:         return CaMeLValue(
#:             value=return_value,
#:             sources=frozenset({"read_email"}),
#:             inner_source=None,   # set per-field in a richer annotator
#:             readers=Public,
#:         )
#:
CapabilityAnnotationFn: type = Callable[[Any, Mapping[str, Any]], CaMeLValue]


# ---------------------------------------------------------------------------
# default_capability_annotation
# ---------------------------------------------------------------------------


def default_capability_annotation(
    return_value: Any,
    tool_kwargs: Mapping[str, Any],
    tool_id: str,
) -> CaMeLValue:
    """Apply the default capability annotation to a tool's return value.

    When no tool-specific :data:`CapabilityAnnotationFn` is registered, this
    function is used to wrap the raw return value in a :class:`~camel.value.CaMeLValue`
    with conservative provenance metadata.

    Default annotation contract
    ---------------------------
    * ``sources = frozenset({tool_id})`` — the only known origin is the tool itself.
    * ``inner_source = None`` — no sub-field extraction at this level.
    * ``readers = Public`` — the value is not restricted to a particular audience
      by default; tool-specific annotators narrow this when needed.

    Parameters
    ----------
    return_value:
        The raw Python object returned by the tool (before capability tagging).
    tool_kwargs:
        The keyword arguments that were passed to the tool.  Not used in the
        default implementation, but provided so that overrides can access call
        context without changing the signature.
    tool_id:
        The registered name / identifier of the tool (e.g. ``"read_email"``).

    Returns
    -------
    CaMeLValue
        A capability-tagged wrapper around *return_value* with
        ``sources={tool_id}`` and ``readers=Public``.

    Examples
    --------
    ::

        from camel.capabilities.types import default_capability_annotation

        raw = {"subject": "Hello", "sender": "bob@example.com"}
        cv = default_capability_annotation(raw, {}, "read_email")
        assert cv.sources == frozenset({"read_email"})
        assert cv.readers is Public
        assert cv.value == raw
    """
    return CaMeLValue(
        value=return_value,
        sources=frozenset({tool_id}),
        inner_source=None,
        readers=Public,
    )


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------

__all__ = [
    # Re-exported from camel.value
    "CaMeLValue",
    "Public",
    "Readers",
    "_PublicType",
    # Defined here
    "CapabilityAnnotationFn",
    "default_capability_annotation",
]
