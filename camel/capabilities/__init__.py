"""CaMeL Capability Assignment Engine.

This sub-package defines the types, protocols, and annotation helpers that
form the *Capability Assignment Engine* — the foundational data layer on
which the CaMeL security policy engine operates.

Sub-modules
-----------
:mod:`camel.capabilities.types`
    Core type definitions: :class:`~camel.value.CaMeLValue`,
    :data:`~camel.value.Public`, :class:`CapabilityAnnotationFn` protocol,
    and the :data:`Readers` type alias.

:mod:`camel.capabilities.annotations`
    Tool-specific capability annotators for ``read_email`` and cloud storage
    tools (``read_document``, ``get_file``), plus the
    :func:`~camel.capabilities.annotations.register_built_in_tools` helper.

Public re-exports
-----------------
The most commonly used symbols are re-exported here for convenience::

    from camel.capabilities import (
        CaMeLValue, Public, CapabilityAnnotationFn,
        annotate_read_email, annotate_read_document, annotate_get_file,
        register_built_in_tools,
    )

"""

from camel.capabilities.annotations import (
    annotate_get_file,
    annotate_read_document,
    annotate_read_email,
    register_built_in_tools,
)
from camel.capabilities.types import CapabilityAnnotationFn, default_capability_annotation
from camel.value import CaMeLValue, Public, Readers, _PublicType

__all__ = [
    "CapabilityAnnotationFn",
    "default_capability_annotation",
    "CaMeLValue",
    "Public",
    "Readers",
    "_PublicType",
    # Tool-specific annotators
    "annotate_read_email",
    "annotate_read_document",
    "annotate_get_file",
    "register_built_in_tools",
]
