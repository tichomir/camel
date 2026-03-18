"""CaMeL — Capabilities for Machine Learning runtime.

Public API for CaMeL v0.6.0 (Milestone 5):

Value & capability system
--------------------------
:class:`CaMeLValue`         Capability-tagged runtime value container.
:data:`Public`              Singleton sentinel — open-readers (unrestricted).
:class:`_PublicType`        Type of the :data:`Public` sentinel.
:type:`Readers`             ``frozenset[str] | _PublicType`` type alias.
:func:`wrap`                Convenience constructor for :class:`CaMeLValue`.
:func:`raw_value`           Strip capability wrapper; return bare Python value.
:func:`propagate_assignment`        Propagate caps for a simple assignment.
:func:`propagate_binary_op`         Propagate caps for a binary operation.
:func:`propagate_list_construction` Propagate caps for a list literal.
:func:`propagate_dict_construction` Propagate caps for a dict literal.
:func:`propagate_subscript`         Propagate caps for a subscript access.

Interpreter
-----------
:class:`CaMeLInterpreter`   AST-walking interpreter for the CaMeL pseudo-Python subset.
:class:`ExecutionMode`      ``NORMAL`` / ``STRICT`` tracking-mode flag.
:class:`UnsupportedSyntaxError`  Raised for grammar outside the supported subset.
:class:`PolicyViolationError`    Raised when a security policy blocks a tool call.
:class:`StrictDependencyAdditionEvent`  Per-statement STRICT mode dep addition (M4-F18).

Escalation detection (M4-F15/F16)
----------------------------------
:class:`DataToControlFlowWarning`       Warning emitted on escalation detection.
:class:`DataToControlFlowEscalationError`  Raised when escalation gate fires.

Dependency graph
----------------
:class:`DependencyGraph`    Immutable snapshot of upstream variable dependencies.
:func:`get_dependency_graph` Query the dependency graph for a variable (module-level helper).
"""

from camel.dependency_graph import DependencyGraph
from camel.exceptions import (
    DataToControlFlowEscalationError,
    DataToControlFlowWarning,
)
from camel.interpreter import (
    CaMeLInterpreter,
    ExecutionMode,
    PolicyViolationError,
    StrictDependencyAdditionEvent,
    UnsupportedSyntaxError,
)
from camel.value import (
    CaMeLValue,
    Public,
    Readers,
    _PublicType,
    propagate_assignment,
    propagate_binary_op,
    propagate_dict_construction,
    propagate_list_construction,
    propagate_subscript,
    raw_value,
    wrap,
)

__version__ = "0.6.0"

__all__ = [
    # Version
    "__version__",
    # Value & capability system
    "CaMeLValue",
    "Public",
    "Readers",
    "_PublicType",
    "wrap",
    "raw_value",
    "propagate_assignment",
    "propagate_binary_op",
    "propagate_list_construction",
    "propagate_dict_construction",
    "propagate_subscript",
    # Interpreter
    "CaMeLInterpreter",
    "ExecutionMode",
    "UnsupportedSyntaxError",
    "PolicyViolationError",
    "StrictDependencyAdditionEvent",
    # Escalation detection
    "DataToControlFlowWarning",
    "DataToControlFlowEscalationError",
    # Dependency graph
    "DependencyGraph",
    "get_dependency_graph",
]


def get_dependency_graph(interpreter: "CaMeLInterpreter", variable: str) -> DependencyGraph:
    """Query the dependency graph for *variable* on the given interpreter instance.

    Convenience module-level wrapper around
    :meth:`CaMeLInterpreter.get_dependency_graph`.

    Parameters
    ----------
    interpreter:
        A :class:`CaMeLInterpreter` instance whose session you want to query.
    variable:
        The variable name to look up.

    Returns
    -------
    DependencyGraph
        Frozen snapshot of the upstream dependency subgraph for *variable*.
        All fields are empty if *variable* has never been assigned.

    Examples
    --------
    ::

        from camel import CaMeLInterpreter, get_dependency_graph

        interp = CaMeLInterpreter()
        interp.exec("a = 1\\nb = a + 2")
        dg = get_dependency_graph(interp, "b")
        assert "a" in dg.all_upstream
    """
    return interpreter.get_dependency_graph(variable)
