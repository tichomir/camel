"""CaMeLValue — capability-tagged runtime value container.

Every runtime value managed by the CaMeL interpreter is wrapped in a
:class:`CaMeLValue`.  The wrapper carries the actual Python value alongside
capability metadata — *sources*, *inner_source*, and *readers* — that the
security policy engine inspects before every tool call.

Design principles
-----------------
* **Immutability.** All capability fields are ``frozenset`` or a singleton
  sentinel.  :class:`CaMeLValue` itself is a frozen dataclass; callers must
  construct a new instance to represent a derived value.
* **Union semantics.** When a derived value depends on multiple inputs, its
  capability fields are the *union* of all input capabilities.  :data:`Public`
  is absorbing under union: if any input reader set is :data:`Public`, the
  result reader set is :data:`Public`.
* **No implicit coercion.** There is no ``trusted`` boolean shorthand.  Every
  value carries its full provenance from construction.

Key types
---------
:class:`_PublicType` / :data:`Public`
    Singleton sentinel meaning *any* reader is permitted.  This is
    categorically different from an empty ``frozenset[str]`` (which means *no*
    reader is permitted).

:class:`CaMeLValue`
    Frozen dataclass wrapping a Python value with its capability fields.

Capability propagation functions
---------------------------------
:func:`propagate_assignment`
    Assign a new underlying value while preserving capability metadata.
:func:`propagate_binary_op`
    Result of a binary expression (``a + b``, ``a and b``, etc.).
:func:`propagate_list_construction`
    Result of a list literal ``[e0, e1, …, eN]``.
:func:`propagate_dict_construction`
    Result of a dict literal ``{k0: v0, …, kN: vN}``.
:func:`propagate_subscript`
    Result of a subscript or attribute access ``container[key]``.

Raw value accessor
------------------
:func:`raw_value` (module-level) and :attr:`CaMeLValue.raw` (property) strip
the capability wrapper and return the bare Python value.  These are the *only*
sanctioned ways to extract a value for tool execution; callers must not reach
into ``.value`` directly, because doing so bypasses the naming contract and
may break if the field is renamed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

# ---------------------------------------------------------------------------
# Public singleton — open-readers sentinel
# ---------------------------------------------------------------------------


class _PublicType:
    """Sentinel type meaning *any reader is permitted*.

    Use the module-level :data:`Public` constant; do not construct this class
    directly.

    Semantics
    ---------
    ``Public`` is the *top* element in the readers lattice:

    * ``Public`` ∪ {any set}  → ``Public``   (absorbing under union)
    * ``Public`` ≠ frozenset()``              (empty set = *no* readers allowed)
    * ``isinstance(readers, _PublicType)``    (preferred over ``is Public``)

    Singleton guarantee: ``_PublicType() is _PublicType()`` is ``True``.
    """

    _instance: _PublicType | None = None

    def __new__(cls) -> _PublicType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Public"

    def __reduce__(self) -> tuple[type[_PublicType], tuple[()]]:
        # Preserve singleton identity across pickle round-trips.
        return (_PublicType, ())


#: Singleton sentinel representing "open readers" — any consumer may hold this
#: value.  Assign this to :attr:`CaMeLValue.readers` for values that are not
#: restricted to a particular audience (e.g. values derived from trusted user
#: literals or from fully public tool outputs).
Public: Final[_PublicType] = _PublicType()

# ---------------------------------------------------------------------------
# Readers type alias
# ---------------------------------------------------------------------------

#: The type of the ``readers`` field on :class:`CaMeLValue`.
#:
#: * ``frozenset[str]`` — the value may only be forwarded to the listed
#:   principals (e.g. email addresses, user IDs).
#: * :class:`_PublicType` (:data:`Public`) — the value is unrestricted.
#:
#: An *empty* ``frozenset()`` means the value has no authorised readers and
#: must not be forwarded to any external principal.
Readers: TypeAlias = frozenset[str] | _PublicType


# ---------------------------------------------------------------------------
# CaMeLValue dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaMeLValue:
    """A capability-tagged runtime value.

    Every Python value managed by the CaMeL interpreter is stored as a
    ``CaMeLValue``.  The capability fields describe the value's provenance
    (``sources``, ``inner_source``) and authorised audience (``readers``).
    The security policy engine reads these fields before each tool call to
    decide whether the call is allowed.

    Attributes
    ----------
    value:
        The underlying Python value.  May be any type: ``str``, ``int``,
        ``list``, ``dict``, a Pydantic model instance, etc.
    sources:
        The set of *origin labels* for this value.  Each label is a string
        identifying how the value was produced — typically the name of the
        tool that returned it (e.g. ``"get_last_email"``) or a special
        label such as ``"User literal"`` (for constants the user typed
        directly) or ``"CaMeL"`` (for values synthesised by the interpreter
        itself with no external origin).

        For a *derived* value (produced by an operation over other
        ``CaMeLValue``s) ``sources`` is the *union* of all input ``sources``
        sets.  The field is a ``frozenset`` to enforce immutability.
    inner_source:
        An optional sub-source label within the originating tool.  Used to
        track which *field* of a structured tool response a value came from
        (e.g. ``"sender"`` when the value is the sender address of an email
        returned by ``get_last_email``).

        For derived values this is always ``None``; it is only meaningful for
        values extracted directly from a tool's return value.
    readers:
        The set of principals authorised to *receive* this value (e.g. as a
        tool-call argument).  Two forms are possible:

        * ``frozenset[str]`` — a finite set of principals, identified by
          strings (typically email addresses).  An *empty* frozenset means
          no principal is authorised; the value must not be forwarded.
        * :data:`Public` (:class:`_PublicType`) — any principal is
          authorised (open-reader semantics).

        The security policy engine checks ``readers`` before every tool call
        to enforce data-flow policies (see PRD §6.5).

    Notes
    -----
    * The class is frozen (``frozen=True``) — all fields are immutable after
      construction.  To represent a derived value, construct a new instance
      using one of the :func:`propagate_*` functions.
    * Do not access ``.value`` directly in tool-execution code; use
      :attr:`raw` or :func:`raw_value` instead to keep the access pattern
      uniform and to ease future refactoring.

    Examples
    --------
    Wrapping a trusted user literal::

        from camel.value import CaMeLValue, Public

        user_literal = CaMeLValue(
            value="alice@example.com",
            sources=frozenset({"User literal"}),
            inner_source=None,
            readers=Public,
        )

    Wrapping an untrusted tool return value::

        email_subject = CaMeLValue(
            value="Project update",
            sources=frozenset({"get_last_email"}),
            inner_source="subject",
            readers=frozenset({"alice@example.com"}),
        )
    """

    value: Any
    sources: frozenset[str]
    inner_source: str | None
    readers: Readers

    # ------------------------------------------------------------------
    # Raw value accessor
    # ------------------------------------------------------------------

    @property
    def raw(self) -> Any:
        """Return the bare Python value, stripping the capability wrapper.

        Use this (or :func:`raw_value`) in tool-execution code whenever you
        need to pass the underlying value to an external API.  Do **not**
        access ``.value`` directly.

        Returns
        -------
        Any
            The unwrapped Python value stored in :attr:`value`.
        """
        return self.value


# ---------------------------------------------------------------------------
# Module-level raw value accessor
# ---------------------------------------------------------------------------


def raw_value(v: CaMeLValue) -> Any:
    """Return the bare Python value from *v*, stripping capability metadata.

    Equivalent to ``v.raw``; provided as a standalone function for contexts
    where a callable is more ergonomic than a property (e.g. ``map``).

    Parameters
    ----------
    v:
        A :class:`CaMeLValue` instance.

    Returns
    -------
    Any
        The unwrapped Python value.

    Examples
    --------
    ::

        from camel.value import CaMeLValue, Public, raw_value

        cv = CaMeLValue(value=42, sources=frozenset(), inner_source=None, readers=Public)
        assert raw_value(cv) == 42
    """
    return v.value


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _union_sources(*args: frozenset[str]) -> frozenset[str]:
    """Return the union of all *sources* sets."""
    result: frozenset[str] = frozenset()
    for s in args:
        result = result | s
    return result


def _union_readers(*args: Readers) -> Readers:
    """Return the union of all *readers* values.

    :data:`Public` is absorbing: if any argument is :data:`Public`, the result
    is :data:`Public`.  Otherwise returns the union of all ``frozenset[str]``
    arguments.
    """
    combined: frozenset[str] = frozenset()
    for r in args:
        if isinstance(r, _PublicType):
            return Public
        combined = combined | r
    return combined


# ---------------------------------------------------------------------------
# Propagation functions
# ---------------------------------------------------------------------------


def propagate_assignment(source: CaMeLValue, new_value: Any) -> CaMeLValue:
    """Propagate capability metadata for a simple assignment.

    Use this when the interpreter evaluates a plain assignment statement such
    as ``x = expr`` where ``expr`` already resolved to a single
    :class:`CaMeLValue`.  The capability metadata is preserved unchanged; only
    :attr:`~CaMeLValue.value` is replaced.

    ``inner_source`` is cleared to ``None`` because the variable is now a
    derived binding, not a direct tool-output field.

    Parameters
    ----------
    source:
        The :class:`CaMeLValue` being assigned.
    new_value:
        The Python value to store in the result.  Typically equal to
        ``source.value``, but may differ when the interpreter has already
        performed type coercion.

    Returns
    -------
    CaMeLValue
        A new instance with ``sources`` and ``readers`` copied from *source*
        and ``inner_source`` set to ``None``.

    Examples
    --------
    ::

        result = propagate_assignment(email_subject, email_subject.raw)
        assert result.sources == email_subject.sources
        assert result.inner_source is None
    """
    return CaMeLValue(
        value=new_value,
        sources=source.sources,
        inner_source=None,
        readers=source.readers,
    )


def propagate_binary_op(
    left: CaMeLValue,
    right: CaMeLValue,
    result: Any,
) -> CaMeLValue:
    """Propagate capability metadata for a binary operation.

    Covers all binary AST nodes: arithmetic (``+``, ``-``, ``*``, ``/``,
    ``//``, ``%``, ``**``), bitwise (``&``, ``|``, ``^``, ``<<``, ``>>``),
    comparison (``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``), and boolean
    (``and``, ``or``).

    Capability union semantics:

    * ``sources`` = ``left.sources`` ∪ ``right.sources``
    * ``readers`` = ``left.readers`` ∪ ``right.readers``
      (with :data:`Public` absorbing)
    * ``inner_source`` = ``None`` (derived value)

    Parameters
    ----------
    left:
        Left operand.
    right:
        Right operand.
    result:
        The computed Python result of the binary operation.

    Returns
    -------
    CaMeLValue
        A new instance carrying the union of both operands' capabilities.

    Examples
    --------
    ::

        greeting = propagate_binary_op(salutation, name, salutation.raw + name.raw)
        assert "get_last_email" in greeting.sources
    """
    return CaMeLValue(
        value=result,
        sources=_union_sources(left.sources, right.sources),
        inner_source=None,
        readers=_union_readers(left.readers, right.readers),
    )


def propagate_list_construction(
    elements: Sequence[CaMeLValue],
    result: list[Any],
) -> CaMeLValue:
    """Propagate capability metadata for a list construction expression.

    Covers list literals ``[e0, e1, …, eN]`` and list comprehensions
    (once all element values have been resolved to :class:`CaMeLValue`
    instances by the interpreter).

    Capability union semantics:

    * ``sources`` = union of all element ``sources``
    * ``readers`` = union of all element ``readers``
      (with :data:`Public` absorbing)
    * ``inner_source`` = ``None``

    An empty list (``elements=[]``) produces a :class:`CaMeLValue` with
    ``sources=frozenset()``, ``readers=frozenset()``, and
    ``inner_source=None``.

    Parameters
    ----------
    elements:
        Ordered sequence of :class:`CaMeLValue` instances — the list elements.
    result:
        The constructed Python ``list`` (i.e. ``[e.raw for e in elements]``).

    Returns
    -------
    CaMeLValue
        A new instance with the union of all element capabilities.

    Examples
    --------
    ::

        combined = propagate_list_construction([a, b, c], [a.raw, b.raw, c.raw])
        assert combined.sources == a.sources | b.sources | c.sources
    """
    all_sources = _union_sources(*(e.sources for e in elements))
    all_readers = _union_readers(*(e.readers for e in elements))
    return CaMeLValue(
        value=result,
        sources=all_sources,
        inner_source=None,
        readers=all_readers,
    )


def propagate_dict_construction(
    keys: Sequence[CaMeLValue],
    values: Sequence[CaMeLValue],
    result: dict[Any, Any],
) -> CaMeLValue:
    """Propagate capability metadata for a dict construction expression.

    Covers dict literals ``{k0: v0, …, kN: vN}``.  Both key and value
    :class:`CaMeLValue`s contribute to the output capabilities, because a
    key that originates from untrusted content can encode sensitive
    information via the presence or absence of entries.

    Capability union semantics:

    * ``sources`` = union of all key and value ``sources``
    * ``readers`` = union of all key and value ``readers``
      (with :data:`Public` absorbing)
    * ``inner_source`` = ``None``

    *len(keys) must equal len(values).*

    Parameters
    ----------
    keys:
        Ordered sequence of :class:`CaMeLValue` instances for the dict keys.
    values:
        Ordered sequence of :class:`CaMeLValue` instances for the dict values.
    result:
        The constructed Python ``dict``.

    Returns
    -------
    CaMeLValue
        A new instance with the union of all key and value capabilities.

    Raises
    ------
    ValueError
        If ``len(keys) != len(values)``.

    Examples
    --------
    ::

        d = propagate_dict_construction(
            [k_cv], [v_cv], {k_cv.raw: v_cv.raw}
        )
        assert d.sources == k_cv.sources | v_cv.sources
    """
    if len(keys) != len(values):
        raise ValueError(
            f"propagate_dict_construction: keys and values must have the same "
            f"length, got {len(keys)} keys and {len(values)} values."
        )
    all_entries: list[CaMeLValue] = list(keys) + list(values)
    all_sources = _union_sources(*(e.sources for e in all_entries))
    all_readers = _union_readers(*(e.readers for e in all_entries))
    return CaMeLValue(
        value=result,
        sources=all_sources,
        inner_source=None,
        readers=all_readers,
    )


def wrap(
    value: Any,
    sources: frozenset[str] | None = None,
    inner_source: str | None = None,
    readers: Readers | None = None,
) -> CaMeLValue:
    """Convenience constructor for :class:`CaMeLValue`.

    Provides defaults for the capability fields so callers do not need to
    spell out ``frozenset()`` and ``Public`` for common cases.

    Parameters
    ----------
    value:
        The underlying Python value to wrap.
    sources:
        Origin labels.  Defaults to ``frozenset()`` (no known origin) when
        not provided.
    inner_source:
        Optional sub-field label.  Defaults to ``None``.
    readers:
        Authorised audience.  Defaults to :data:`Public` (unrestricted) when
        not provided.

    Returns
    -------
    CaMeLValue
        A new :class:`CaMeLValue` with the given fields.

    Examples
    --------
    ::

        from camel.value import wrap, Public

        # Trusted user literal — open readers, no specific source
        user_val = wrap("alice@example.com", sources=frozenset({"User literal"}))
        assert user_val.readers is Public

        # Tool output with restricted readers
        tool_val = wrap(
            "secret",
            sources=frozenset({"get_secret"}),
            readers=frozenset({"alice@example.com"}),
        )
    """
    return CaMeLValue(
        value=value,
        sources=sources if sources is not None else frozenset(),
        inner_source=inner_source,
        readers=readers if readers is not None else Public,
    )


def propagate_subscript(
    container: CaMeLValue,
    key: CaMeLValue,
    result: Any,
) -> CaMeLValue:
    """Propagate capability metadata for a subscript access.

    Covers ``container[key]`` expressions (list index, dict lookup, string
    slice, attribute access resolved via ``__getitem__``).

    The *key* contributes to the result's capabilities because the index or
    key value may itself carry sensitive provenance (e.g. an email-derived
    integer index used to pick an item from a trusted list).

    Capability union semantics:

    * ``sources`` = ``container.sources`` ∪ ``key.sources``
    * ``readers`` = ``container.readers`` ∪ ``key.readers``
      (with :data:`Public` absorbing)
    * ``inner_source`` = ``None``

    Parameters
    ----------
    container:
        The :class:`CaMeLValue` being subscripted.
    key:
        The :class:`CaMeLValue` used as the subscript key or index.
    result:
        The Python value extracted by the subscript operation.

    Returns
    -------
    CaMeLValue
        A new instance carrying the union of container and key capabilities.

    Examples
    --------
    ::

        element = propagate_subscript(email_list, index_cv, email_list.raw[index_cv.raw])
        assert email_list.sources <= element.sources
        assert index_cv.sources <= element.sources
    """
    return CaMeLValue(
        value=result,
        sources=_union_sources(container.sources, key.sources),
        inner_source=None,
        readers=_union_readers(container.readers, key.readers),
    )
