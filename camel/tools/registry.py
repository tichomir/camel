"""CaMeL Tool Registry — registration and capability-annotation dispatch.

Every tool available to the CaMeL interpreter must be registered here.
Registration associates:

1. A **tool callable** — any Python callable that accepts raw Python values and
   returns a raw Python value (or, for backwards compatibility, a
   :class:`~camel.value.CaMeLValue` directly).
2. An optional **capability annotator** — a
   :data:`~camel.capabilities.CapabilityAnnotationFn` that maps
   ``(return_value, tool_kwargs) -> CaMeLValue``.  When omitted, the
   :func:`~camel.capabilities.default_capability_annotation` is applied,
   tagging the return value with ``sources={tool_id}`` and ``readers=Public``.

The registry exposes :meth:`~ToolRegistry.as_interpreter_tools`, which
returns a ``dict[str, Callable[..., CaMeLValue]]`` ready to be passed to
:class:`~camel.interpreter.CaMeLInterpreter` as the ``tools`` argument.  Each
wrapped callable:

* Calls the underlying tool with raw values.
* Passes the return value through the registered (or default) capability
  annotator.
* Returns the resulting :class:`~camel.value.CaMeLValue`.

This design preserves the interpreter contract (tools must return
:class:`~camel.value.CaMeLValue`) while decoupling tool authors from the
capability system — a tool author only needs to return a plain Python value;
the registry handles the wrapping.

Architecture note
-----------------
The registry intentionally does **not** manage policy functions — those live
in a separate policy engine injected into the interpreter.  The registry is
concerned only with capability *annotation* (provenance tagging), not
capability *enforcement*.

Usage
-----
::

    from camel.tools import ToolRegistry

    registry = ToolRegistry()

    def my_tool(arg: str) -> str:
        return f"result for {arg}"

    registry.register("my_tool", my_tool)

    # Pass to interpreter
    from camel.interpreter import CaMeLInterpreter
    interp = CaMeLInterpreter(tools=registry.as_interpreter_tools())

With a custom annotator::

    from camel.value import CaMeLValue, Public

    def annotate_my_tool(return_value, tool_kwargs):
        return CaMeLValue(
            value=return_value,
            sources=frozenset({"my_tool"}),
            inner_source=None,
            readers=frozenset({"alice@example.com"}),
        )

    registry.register("my_tool", my_tool, capability_annotation=annotate_my_tool)

"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from camel.capabilities.types import CapabilityAnnotationFn, default_capability_annotation
from camel.value import CaMeLValue

__all__ = ["ToolRegistry"]


class ToolRegistry:
    """Registry of CaMeL tools and their capability annotators.

    Each entry maps a tool name to its underlying callable and an optional
    :data:`~camel.capabilities.CapabilityAnnotationFn`.  The registry wraps
    each tool so that the interpreter always receives callables that return
    :class:`~camel.value.CaMeLValue`.

    Attributes are intentionally private; interact with the registry only
    through :meth:`register`, :meth:`unregister`, :meth:`get_tool`,
    :meth:`names`, and :meth:`as_interpreter_tools`.

    Examples
    --------
    ::

        registry = ToolRegistry()
        registry.register("get_email", get_email_fn)
        interp = CaMeLInterpreter(tools=registry.as_interpreter_tools())
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._entries: dict[str, _ToolEntry] = {}

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        capability_annotation: CapabilityAnnotationFn | None = None,
    ) -> None:
        """Register a tool under *name*.

        Parameters
        ----------
        name:
            The tool identifier used in P-LLM generated code (e.g.
            ``"read_email"``).  Must be a valid Python identifier.
        fn:
            The underlying callable.  It is called with raw Python values
            (unwrapped from :class:`~camel.value.CaMeLValue`).  It may
            return either a raw Python value **or** a
            :class:`~camel.value.CaMeLValue` — if a ``CaMeLValue`` is
            returned directly, the capability annotation step is skipped
            (the value is used as-is, preserving any fine-grained annotation
            the tool itself applied).
        capability_annotation:
            Optional annotator with signature
            ``(return_value: Any, tool_kwargs: Mapping[str, Any]) -> CaMeLValue``.
            When ``None``, :func:`~camel.capabilities.default_capability_annotation`
            is applied (``sources={name}``, ``readers=Public``).

        Raises
        ------
        ValueError
            If *name* is already registered.  Call :meth:`unregister` first
            to replace an entry.
        TypeError
            If *fn* is not callable.

        Examples
        --------
        ::

            registry.register("send_email", send_email_fn)
            registry.register(
                "read_cloud_doc",
                read_doc_fn,
                capability_annotation=annotate_cloud_doc,
            )
        """
        if not callable(fn):
            raise TypeError(f"Tool {name!r}: fn must be callable, got {type(fn).__name__!r}")
        if name in self._entries:
            raise ValueError(
                f"Tool {name!r} is already registered.  "
                f"Call unregister({name!r}) first to replace it."
            )
        self._entries[name] = _ToolEntry(
            name=name,
            fn=fn,
            capability_annotation=capability_annotation,
        )

    def unregister(self, name: str) -> None:
        """Remove the tool registered under *name*.

        Parameters
        ----------
        name:
            The tool identifier to remove.

        Raises
        ------
        KeyError
            If *name* is not registered.
        """
        if name not in self._entries:
            raise KeyError(f"Tool {name!r} is not registered")
        del self._entries[name]

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get_tool(self, name: str) -> _ToolEntry:
        """Return the :class:`_ToolEntry` for *name*.

        Parameters
        ----------
        name:
            Registered tool identifier.

        Returns
        -------
        _ToolEntry
            The entry containing the callable and optional annotator.

        Raises
        ------
        KeyError
            If *name* is not registered.
        """
        if name not in self._entries:
            raise KeyError(f"Tool {name!r} is not registered")
        return self._entries[name]

    @property
    def names(self) -> frozenset[str]:
        """Return the set of registered tool names.

        Returns
        -------
        frozenset[str]
            All tool identifiers currently in the registry.
        """
        return frozenset(self._entries)

    # ------------------------------------------------------------------
    # Interpreter integration
    # ------------------------------------------------------------------

    def as_interpreter_tools(self) -> dict[str, Callable[..., CaMeLValue]]:
        """Return wrapped tool callables for use with :class:`~camel.interpreter.CaMeLInterpreter`.

        Each returned callable wraps the underlying tool function so that:

        1. The tool is called with raw Python argument values.
        2. The return value is passed through the registered capability
           annotator (or :func:`~camel.capabilities.default_capability_annotation`
           if none is set).
        3. A :class:`~camel.value.CaMeLValue` is returned to the interpreter.

        If the underlying tool already returns a :class:`~camel.value.CaMeLValue`,
        the annotation step is skipped — the pre-annotated value is returned
        directly, preserving fine-grained per-field annotation.

        Returns
        -------
        dict[str, Callable[..., CaMeLValue]]
            Mapping of tool name → wrapped callable, suitable for the
            ``tools`` argument of :class:`~camel.interpreter.CaMeLInterpreter`.

        Examples
        --------
        ::

            interp = CaMeLInterpreter(tools=registry.as_interpreter_tools())
        """
        return {name: entry.as_wrapped() for name, entry in self._entries.items()}

    def __len__(self) -> int:
        """Return the number of registered tools."""
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        """Return ``True`` if *name* is a registered tool identifier."""
        return name in self._entries

    def __repr__(self) -> str:
        """Return a concise string representation of the registry."""
        names = sorted(self._entries)
        return f"ToolRegistry({names!r})"


# ---------------------------------------------------------------------------
# Internal _ToolEntry
# ---------------------------------------------------------------------------


class _ToolEntry:
    """Internal record associating a tool name with its callable and annotator.

    Parameters
    ----------
    name:
        The registered tool identifier.
    fn:
        The underlying tool callable (accepts raw values, returns raw value or
        :class:`~camel.value.CaMeLValue`).
    capability_annotation:
        Optional annotator; ``None`` means use the default.
    """

    def __init__(
        self,
        name: str,
        fn: Callable[..., Any],
        capability_annotation: CapabilityAnnotationFn | None,
    ) -> None:
        """Initialise the entry."""
        self.name = name
        self.fn = fn
        self.capability_annotation = capability_annotation

    def as_wrapped(self) -> Callable[..., CaMeLValue]:
        """Return a callable that applies capability annotation after invoking *fn*.

        The wrapped callable:

        1. Calls ``self.fn`` with the provided positional and keyword arguments
           (raw Python values — not :class:`~camel.value.CaMeLValue` wrappers).
        2. If the result is already a :class:`~camel.value.CaMeLValue`, returns
           it unchanged (the tool performed its own annotation).
        3. Otherwise, reconstructs the ``tool_kwargs`` mapping from the
           call arguments (using :func:`inspect.signature` when possible),
           then passes ``(result, tool_kwargs)`` to the registered annotator
           (or to :func:`~camel.capabilities.default_capability_annotation`
           with ``tool_id=self.name``).

        Returns
        -------
        Callable[..., CaMeLValue]
            The wrapped callable ready for the interpreter's ``_tools`` dict.
        """
        entry = self  # capture for closure

        def _wrapped(*args: Any, **kwargs: Any) -> CaMeLValue:
            """Capability-annotating wrapper around the underlying tool fn."""
            result = entry.fn(*args, **kwargs)

            # If the tool already returned a CaMeLValue, use it as-is.
            if isinstance(result, CaMeLValue):
                return result

            # Reconstruct tool_kwargs for the annotator.
            tool_kwargs: Mapping[str, Any] = _reconstruct_kwargs(entry.fn, args, kwargs)

            if entry.capability_annotation is not None:
                return entry.capability_annotation(result, tool_kwargs)
            return default_capability_annotation(result, tool_kwargs, entry.name)

        # Preserve function metadata for introspection / debugging.
        _wrapped.__name__ = entry.name
        _wrapped.__qualname__ = f"ToolRegistry._wrapped[{entry.name}]"
        _wrapped.__doc__ = getattr(entry.fn, "__doc__", None)

        return _wrapped

    def __repr__(self) -> str:
        """Return a concise string representation of the entry."""
        ann = "default" if self.capability_annotation is None else "custom"
        return f"_ToolEntry(name={self.name!r}, annotation={ann})"


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _reconstruct_kwargs(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Mapping[str, Any]:
    """Build a ``{param_name: value}`` mapping from positional *args* and *kwargs*.

    Used by :meth:`_ToolEntry.as_wrapped` so the capability annotator receives
    a complete keyword-argument mapping regardless of whether arguments were
    passed positionally or by name.

    If :func:`inspect.signature` cannot introspect *fn* (e.g. for built-in C
    functions), positional arguments are mapped to ``"arg0"``, ``"arg1"``, etc.

    Parameters
    ----------
    fn:
        The callable whose parameter names we want to resolve.
    args:
        Positional arguments that were passed to *fn*.
    kwargs:
        Keyword arguments that were passed to *fn*.

    Returns
    -------
    Mapping[str, Any]
        Combined ``{name: value}`` mapping for all arguments.
    """
    combined: dict[str, Any] = {}
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        for i, arg_val in enumerate(args):
            key = params[i] if i < len(params) else f"arg{i}"
            combined[key] = arg_val
    except (ValueError, TypeError):
        for i, arg_val in enumerate(args):
            combined[f"arg{i}"] = arg_val
    combined.update(kwargs)
    return combined
