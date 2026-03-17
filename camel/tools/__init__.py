"""CaMeL tool registration and execution utilities.

This sub-package provides the :class:`~camel.tools.registry.ToolRegistry`,
the central component for registering CaMeL-aware tools alongside their
optional capability annotators.

Sub-modules
-----------
:mod:`camel.tools.registry`
    :class:`~camel.tools.registry.ToolRegistry` — registers tools and their
    :data:`~camel.capabilities.CapabilityAnnotationFn` callbacks, and exposes
    wrapped callables that return :class:`~camel.value.CaMeLValue` instances.

Public re-exports
-----------------
::

    from camel.tools import ToolRegistry

"""

from camel.tools.registry import ToolRegistry

__all__ = ["ToolRegistry"]
