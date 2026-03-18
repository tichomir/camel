"""Specific capability annotation functions for CaMeL built-in tools.

This module provides :data:`CapabilityAnnotationFn`-compatible callables for
tools that require fine-grained provenance tagging beyond the
:func:`~camel.capabilities.types.default_capability_annotation` defaults.

Available annotators
--------------------
:func:`annotate_read_email`
    Tags the top-level :class:`~camel.value.CaMeLValue` with
    ``inner_source=<sender_email>`` and individually wraps the ``body`` and
    ``subject`` fields as nested :class:`~camel.value.CaMeLValue` instances.

:func:`annotate_read_document`
    Cloud storage annotator for a ``read_document`` tool.  Extracts sharing
    permissions from the ``"permissions"`` key of the return value dict and
    sets ``readers`` accordingly (``frozenset[str]`` or :data:`~camel.value.Public`).

:func:`annotate_get_file`
    Cloud storage annotator for a ``get_file`` tool — same logic as
    :func:`annotate_read_document` but with ``sources={"get_file"}``.

Registration helpers
--------------------
:func:`register_built_in_tools`
    Convenience function that registers read_email and cloud storage tools
    in a :class:`~camel.tools.ToolRegistry` with their correct annotators in
    a single call.

Expected tool return shapes
---------------------------
``read_email`` tool must return a :class:`dict` with at least::

    {
        "sender":  str,   # e.g. "alice@example.com"
        "subject": str,
        "body":    str,
        # …any additional fields are left unwrapped
    }

Cloud storage tools (``read_document``, ``get_file``) must return a
:class:`dict` with a ``"permissions"`` key::

    {
        "content": str,
        "permissions": {
            "type": "public" | "restricted",
            "readers": ["alice@example.com", ...]   # only for "restricted"
        },
    }

If the ``"permissions"`` key is absent, :data:`~camel.value.Public` is used
as a safe fallback so that annotation never raises an exception on valid tool
output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from camel.capabilities.types import CapabilityAnnotationFn
from camel.value import CaMeLValue, Public, Readers, _PublicType

__all__ = [
    "annotate_read_email",
    "annotate_read_document",
    "annotate_get_file",
    "register_built_in_tools",
]

# ---------------------------------------------------------------------------
# read_email annotator
# ---------------------------------------------------------------------------

_TOOL_ID_READ_EMAIL: str = "read_email"


def annotate_read_email(
    return_value: Any,
    tool_kwargs: Mapping[str, Any],
) -> CaMeLValue:
    """Capability annotator for the ``read_email`` tool.

    Applies fine-grained provenance tagging to structured email objects:

    * **Top-level** :class:`~camel.value.CaMeLValue`:

      - ``sources = frozenset({"read_email"})``
      - ``inner_source`` = the sender's email address string (e.g.
        ``"alice@example.com"``), or ``None`` if the ``"sender"`` key is
        absent from the return value.
      - ``readers = Public``
      - ``value`` = the (possibly modified) return dict with ``body``,
        ``subject``, and ``sender`` fields individually wrapped as nested
        :class:`~camel.value.CaMeLValue` instances.

    * **Nested field** :class:`~camel.value.CaMeLValue` (``body``, ``subject``,
      ``sender``):

      - ``sources = frozenset({"read_email"})``
      - ``inner_source`` = the field name (``"body"``, ``"subject"``, or
        ``"sender"``)
      - ``readers = Public``

    Parameters
    ----------
    return_value:
        Raw value returned by the ``read_email`` tool.  Expected to be a
        :class:`dict` with ``"sender"``, ``"subject"``, and ``"body"`` keys.
        Non-dict values are wrapped without field-level annotation.
    tool_kwargs:
        Keyword arguments passed to the tool (not used in this annotator but
        required by the :data:`CapabilityAnnotationFn` protocol).

    Returns
    -------
    CaMeLValue
        Top-level capability-tagged wrapper with ``inner_source`` set to the
        sender's email address.

    Examples
    --------
    ::

        raw = {"sender": "alice@example.com", "subject": "Hi", "body": "Hello!"}
        cv = annotate_read_email(raw, {})
        assert cv.inner_source == "alice@example.com"
        assert cv.sources == frozenset({"read_email"})
        subject_cv = cv.value["subject"]
        assert isinstance(subject_cv, CaMeLValue)
        assert subject_cv.inner_source == "subject"
    """
    sources: frozenset[str] = frozenset({_TOOL_ID_READ_EMAIL})

    # Extract sender for the top-level inner_source.
    sender: str | None = None
    if isinstance(return_value, dict):
        raw_sender = return_value.get("sender")
        if isinstance(raw_sender, str):
            sender = raw_sender

    # Wrap individual fields for fine-grained capability tracking.
    annotated_value: Any = return_value
    if isinstance(return_value, dict):
        annotated_dict: dict[str, Any] = dict(return_value)
        for field in ("body", "subject", "sender"):
            if field in annotated_dict and not isinstance(annotated_dict[field], CaMeLValue):
                annotated_dict[field] = CaMeLValue(
                    value=annotated_dict[field],
                    sources=sources,
                    inner_source=field,
                    readers=Public,
                )
        annotated_value = annotated_dict

    return CaMeLValue(
        value=annotated_value,
        sources=sources,
        inner_source=sender,
        readers=Public,
    )


# ---------------------------------------------------------------------------
# Cloud storage annotators
# ---------------------------------------------------------------------------


def _extract_readers(permissions: Any) -> Readers:
    """Derive a :data:`~camel.value.Readers` value from a permissions object.

    Parameters
    ----------
    permissions:
        The value stored under the ``"permissions"`` key of a cloud storage
        tool's return dict.  Supported shapes:

        * ``None`` / missing → :data:`~camel.value.Public` (safe fallback).
        * :class:`~camel.value._PublicType` → :data:`~camel.value.Public`.
        * ``{"type": "public"}`` → :data:`~camel.value.Public`.
        * ``{"type": "restricted", "readers": [...]}`` →
          ``frozenset[str]`` of the listed email addresses.
        * Any unrecognised structure → :data:`~camel.value.Public` (safe
          fallback; annotators must never raise on valid tool output).

    Returns
    -------
    Readers
        Either :data:`~camel.value.Public` or a ``frozenset[str]``.
    """
    if permissions is None:
        return Public
    if isinstance(permissions, _PublicType):
        return Public
    if isinstance(permissions, dict):
        perm_type = permissions.get("type", "restricted")
        if perm_type == "public":
            return Public
        readers_list = permissions.get("readers", [])
        if isinstance(readers_list, (list, tuple, frozenset, set)):
            return frozenset(str(r) for r in readers_list)
        return frozenset()
    # Unrecognised structure — safe fallback.
    return Public


def _make_cloud_storage_annotator(tool_id: str) -> CapabilityAnnotationFn:
    """Return a :data:`CapabilityAnnotationFn` for a cloud storage tool.

    The generated annotator:

    * Sets ``sources = frozenset({tool_id})``.
    * Sets ``inner_source = None``.
    * Derives ``readers`` from the ``"permissions"`` key of the return dict:

      - ``{"type": "public"}`` → :data:`~camel.value.Public`
      - ``{"type": "restricted", "readers": [...]}`` → ``frozenset[str]``
      - absent / unrecognised → :data:`~camel.value.Public` (safe fallback)

    Parameters
    ----------
    tool_id:
        The registered name of the cloud storage tool (e.g.
        ``"read_document"`` or ``"get_file"``).

    Returns
    -------
    CapabilityAnnotationFn
        A callable with signature
        ``(return_value, tool_kwargs) -> CaMeLValue``.
    """
    _tool_id = tool_id  # capture for closure

    def _annotate(
        return_value: Any,
        tool_kwargs: Mapping[str, Any],
    ) -> CaMeLValue:
        """Cloud storage capability annotator for ``{tool_id}``."""
        sources: frozenset[str] = frozenset({_tool_id})

        permissions: Any = None
        if isinstance(return_value, dict):
            permissions = return_value.get("permissions")

        readers: Readers = _extract_readers(permissions)

        return CaMeLValue(
            value=return_value,
            sources=sources,
            inner_source=None,
            readers=readers,
        )

    # Give the closure a meaningful name for debugging.
    _annotate.__name__ = f"annotate_{tool_id}"
    _annotate.__qualname__ = f"_make_cloud_storage_annotator.<locals>.annotate_{tool_id}"
    _annotate.__doc__ = (
        f"Capability annotator for the ``{tool_id}`` cloud storage tool.\n\n"
        'Extracts ``readers`` from the ``"permissions"`` key of the return\n'
        "dict; falls back to :data:`~camel.value.Public` when absent."
    )
    return _annotate


#: Pre-built annotator for ``read_document`` cloud storage tool.
annotate_read_document: CapabilityAnnotationFn = _make_cloud_storage_annotator("read_document")

#: Pre-built annotator for ``get_file`` cloud storage tool.
annotate_get_file: CapabilityAnnotationFn = _make_cloud_storage_annotator("get_file")


# ---------------------------------------------------------------------------
# Tool registry registration helper
# ---------------------------------------------------------------------------


def register_built_in_tools(
    registry: Any,
    *,
    read_email_fn: Callable[..., Any] | None = None,
    read_document_fn: Callable[..., Any] | None = None,
    get_file_fn: Callable[..., Any] | None = None,
) -> None:
    """Register built-in tools with their capability annotators.

    Convenience wrapper that registers any subset of the CaMeL built-in tools
    in *registry* together with the correct :data:`CapabilityAnnotationFn`.

    Parameters
    ----------
    registry:
        A :class:`~camel.tools.ToolRegistry` instance.
    read_email_fn:
        Callable implementing the ``read_email`` tool.  If ``None``, the tool
        is not registered.
    read_document_fn:
        Callable implementing the ``read_document`` cloud storage tool.  If
        ``None``, the tool is not registered.
    get_file_fn:
        Callable implementing the ``get_file`` cloud storage tool.  If
        ``None``, the tool is not registered.

    Examples
    --------
    ::

        from camel.tools import ToolRegistry
        from camel.capabilities.annotations import register_built_in_tools

        registry = ToolRegistry()
        register_built_in_tools(
            registry,
            read_email_fn=real_read_email,
            read_document_fn=real_read_document,
        )
        interp = CaMeLInterpreter(tools=registry.as_interpreter_tools())
    """
    if read_email_fn is not None:
        registry.register(
            "read_email",
            read_email_fn,
            capability_annotation=annotate_read_email,
        )
    if read_document_fn is not None:
        registry.register(
            "read_document",
            read_document_fn,
            capability_annotation=annotate_read_document,
        )
    if get_file_fn is not None:
        registry.register(
            "get_file",
            get_file_fn,
            capability_annotation=annotate_get_file,
        )
