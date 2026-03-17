"""CaMeL AST interpreter — capability-tracking execution engine.

Architecture
------------
The interpreter parses pseudo-Python code produced by the P-LLM and executes it
over a restricted grammar subset.  Every runtime value is stored as a
:class:`~camel.value.CaMeLValue`, carrying full capability metadata
(sources, readers) that the security policy engine inspects before each tool
call.

Design document: ``docs/adr/003-ast-interpreter-architecture.md``

Supported grammar
-----------------
**Statements**

- ``ast.Assign``    — simple assignment, including tuple unpacking
- ``ast.AugAssign`` — augmented assignment (``x += expr``)
- ``ast.If``        — conditional with optional else
- ``ast.For``       — for loop (``else`` clause not supported)
- ``ast.Expr``      — bare expression (function call for side effects)

All other statement types raise :class:`UnsupportedSyntaxError`.

**Expressions**

- ``ast.Constant``    — integer, float, string, boolean, ``None`` literals
- ``ast.Name``        — variable reference (load only)
- ``ast.BinOp``       — arithmetic, bitwise, and matrix-multiply operators
- ``ast.UnaryOp``     — ``-``, ``+``, ``not``, ``~``
- ``ast.BoolOp``      — ``and`` / ``or`` (all operands evaluated for caps)
- ``ast.Compare``     — ``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``, ``in``, ``not in``, ``is``, ``is not``
- ``ast.Call``        — function / tool calls
- ``ast.Attribute``   — attribute access (``obj.field``)
- ``ast.Subscript``   — subscript access (``container[key]``)
- ``ast.List``        — list literal
- ``ast.Tuple``       — tuple literal
- ``ast.Dict``        — dict literal
- ``ast.JoinedStr``   — f-string (``f"..."``), including ``ast.FormattedValue``

All other expression types raise :class:`UnsupportedSyntaxError`.

CaMeLValue wrapping strategy
-----------------------------
Every expression evaluation site returns a :class:`~camel.value.CaMeLValue`.
The wrapping rules are:

- **Constants**: ``sources=frozenset({"User literal"})``, ``readers=Public``.
- **Variable loads**: return the stored :class:`~camel.value.CaMeLValue` unchanged.
- **Binary / augmented operations**: :func:`~camel.value.propagate_binary_op`.
- **Unary operations**: :func:`~camel.value.propagate_assignment` (preserve operand caps, new raw value).
- **BoolOp / Compare**: fold operands left-to-right with :func:`~camel.value.propagate_binary_op`.
- **List / Tuple**: :func:`~camel.value.propagate_list_construction`.
- **Dict**: :func:`~camel.value.propagate_dict_construction` (both keys and values contribute).
- **Subscript / Attribute**: :func:`~camel.value.propagate_subscript`.
- **f-string**: fold all parts left-to-right with :func:`~camel.value.propagate_binary_op`.
- **Tool calls**: the tool returns a :class:`~camel.value.CaMeLValue` directly.
- **Builtin calls**: return value is wrapped using the union of all argument capabilities.

Session state
-------------
The variable store (``_store: dict[str, CaMeLValue]``) persists across
sequential :meth:`CaMeLInterpreter.exec` calls on the same instance.  This
enables multi-step plans where later code blocks reference variables set by
earlier ones.  The store is initialised empty at construction and is never
reset automatically.

Execution modes
---------------
``ExecutionMode.NORMAL`` (default):
    Capabilities propagate only via data assignments and operations.

``ExecutionMode.STRICT``:
    Additionally, whenever an ``if`` or ``for`` block is entered, the test /
    iterable expression's capabilities are merged into every assignment within
    the block (closing timing side-channel vectors).  See the ``ctx_caps``
    parameter threading through ``_exec_*`` and ``_eval_*`` methods.
"""

from __future__ import annotations

import ast
import inspect
import operator as _operator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from camel.dependency_graph import DependencyGraph, _InternalGraph
from camel.value import (
    CaMeLValue,
    Public,
    propagate_assignment,
    propagate_binary_op,
    propagate_dict_construction,
    propagate_list_construction,
    propagate_subscript,
    wrap,
)

# ---------------------------------------------------------------------------
# UnsupportedSyntaxError
# ---------------------------------------------------------------------------


@dataclass
class UnsupportedSyntaxError(Exception):
    """Raised when the interpreter encounters an AST node outside the supported grammar.

    Attributes
    ----------
    node_type:
        ``type(node).__name__`` of the offending AST node.
        Examples: ``"While"``, ``"Lambda"``, ``"ListComp"``.
    lineno:
        1-based source line number, taken from ``getattr(node, "lineno", 0)``.
        Zero when the node carries no line information.
    message:
        Human-readable description of why the node is unsupported.

    Examples
    --------
    ::

        try:
            interp.exec("while True: pass")
        except UnsupportedSyntaxError as exc:
            assert exc.node_type == "While"
            assert exc.lineno == 1
    """

    node_type: str
    lineno: int
    message: str

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"Unsupported AST node {self.node_type!r} at line {self.lineno}: "
            f"{self.message}"
        )


# ---------------------------------------------------------------------------
# PolicyViolationError
# ---------------------------------------------------------------------------


@dataclass
class PolicyViolationError(Exception):
    """Raised when the policy engine denies a tool call.

    Attributes
    ----------
    tool_name:
        Name of the tool whose call was denied.
    reason:
        Human-readable reason string from the policy engine.

    Notes
    -----
    This exception is defined here for completeness of the interpreter API.
    The policy engine itself is injected at construction time and is not
    defined in this module.
    """

    tool_name: str
    reason: str

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"Policy denied call to {self.tool_name!r}: {self.reason}"


# ---------------------------------------------------------------------------
# ExecutionMode
# ---------------------------------------------------------------------------


class ExecutionMode(Enum):
    """Controls how the interpreter propagates capabilities through control flow.

    Members
    -------
    NORMAL:
        Capabilities flow only via data assignments and expressions.
        Control-flow conditions and loop iterables do not contribute
        capabilities to variables assigned within their bodies.
    STRICT:
        In addition to data-flow tracking, the capability metadata of every
        ``if`` test and every ``for`` iterable is merged into all assignments
        that occur within the respective block.  This closes timing
        side-channel vectors described in the CaMeL paper §6.3.

    Usage
    -----
    ::

        interp_normal = CaMeLInterpreter(mode=ExecutionMode.NORMAL)
        interp_strict = CaMeLInterpreter(mode=ExecutionMode.STRICT)
    """

    NORMAL = "normal"
    STRICT = "strict"


# ---------------------------------------------------------------------------
# Operator maps
# ---------------------------------------------------------------------------

# Maps ast.operator subclass → two-argument callable applied to raw values.
_BINOP_MAP: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: _operator.add,
    ast.Sub: _operator.sub,
    ast.Mult: _operator.mul,
    ast.Div: _operator.truediv,
    ast.FloorDiv: _operator.floordiv,
    ast.Mod: _operator.mod,
    ast.Pow: _operator.pow,
    ast.BitAnd: _operator.and_,
    ast.BitOr: _operator.or_,
    ast.BitXor: _operator.xor,
    ast.LShift: _operator.lshift,
    ast.RShift: _operator.rshift,
    ast.MatMult: _operator.matmul,
}

# Maps ast.unaryop subclass → one-argument callable applied to the raw value.
_UNARYOP_MAP: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: _operator.pos,
    ast.USub: _operator.neg,
    ast.Not: _operator.not_,
    ast.Invert: _operator.invert,
}

# Maps ast.cmpop subclass → two-argument callable applied to raw values.
# Used when evaluating chained comparisons element-by-element.
_CMPOP_MAP: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: _operator.eq,
    ast.NotEq: _operator.ne,
    ast.Lt: _operator.lt,
    ast.LtE: _operator.le,
    ast.Gt: _operator.gt,
    ast.GtE: _operator.ge,
    ast.Is: _operator.is_,
    ast.IsNot: _operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# Default builtin callables available to all CaMeLInterpreter instances.
# All of these receive raw Python values and return raw Python values;
# the interpreter wraps the return value with the union of argument capabilities.
_DEFAULT_BUILTINS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "range": range,
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "isinstance": isinstance,
    "type": type,
    "repr": repr,
}


# ---------------------------------------------------------------------------
# CaMeLInterpreter
# ---------------------------------------------------------------------------


class CaMeLInterpreter:
    """CaMeL custom AST-walking interpreter for the restricted Python subset.

    The interpreter executes pseudo-Python plans generated by the P-LLM.  It
    maintains a session-scoped variable store that persists across multiple
    :meth:`exec` calls, enabling multi-step plans expressed as sequential code
    blocks.

    Every value produced during execution is wrapped in a
    :class:`~camel.value.CaMeLValue` carrying full capability metadata
    (sources, readers).  The interpreter propagates these capabilities through
    every expression evaluation and assignment according to the rules in
    ``docs/adr/003-ast-interpreter-architecture.md``.

    Parameters
    ----------
    tools:
        CaMeL-aware callables keyed by name.  Each callable MUST return a
        :class:`~camel.value.CaMeLValue` directly; the interpreter raises
        ``TypeError`` if the return value is not a ``CaMeLValue``.  Tool calls
        go through the policy engine (if set) before execution.
        Example: ``{"get_email": get_email_tool, "send_email": send_email_tool}``
    builtins:
        Plain Python callables keyed by name.  These are called with raw
        (unwrapped) argument values; the interpreter wraps the return value
        with the union of all argument capabilities.  If ``builtins`` is
        provided, it is **merged with** (and can override) the default builtin
        set.  Pass ``{}`` to suppress all defaults.
    mode:
        Execution mode controlling side-channel capability propagation.
        Defaults to ``ExecutionMode.NORMAL``.
    policy_engine:
        Optional policy engine instance.  Must implement::

            check(tool_name: str, kwargs: Mapping[str, CaMeLValue]) -> PolicyResult

        where ``PolicyResult`` is either ``Allowed()`` or ``Denied(reason)``.
        If ``None``, all tool calls are permitted.

    Examples
    --------
    Basic usage::

        from camel.interpreter import CaMeLInterpreter
        from camel.value import wrap, Public

        def get_subject(email_id: int) -> CaMeLValue:
            return wrap("Re: Q4 results", sources=frozenset({"get_subject"}))

        interp = CaMeLInterpreter(tools={"get_subject": get_subject})
        interp.exec('subject = get_subject(42)')
        result = interp.get("subject")
        assert result.raw == "Re: Q4 results"

    Multi-step session::

        interp.exec('a = 1')
        interp.exec('b = a + 2')   # sees 'a' from the previous exec() call
        assert interp.get("b").raw == 3
    """

    def __init__(
        self,
        tools: Mapping[str, Callable[..., CaMeLValue]] | None = None,
        builtins: Mapping[str, Callable[..., Any]] | None = None,
        mode: ExecutionMode = ExecutionMode.NORMAL,
        policy_engine: Any | None = None,
    ) -> None:
        """Initialise the interpreter.

        Implementation notes
        --------------------
        - ``_store`` is initialised to ``{}``.
        - ``_tools`` is a copy of the ``tools`` argument (or ``{}`` if None).
        - ``_builtins`` is the merge of ``_DEFAULT_BUILTINS`` with the
          ``builtins`` argument.  Values in ``builtins`` override defaults of
          the same name.
        - ``_mode`` and ``_policy_engine`` are stored as given.
        """
        self._store: dict[str, CaMeLValue] = {}
        self._tools: dict[str, Callable[..., CaMeLValue]] = dict(tools) if tools else {}
        merged_builtins: dict[str, Callable[..., Any]] = dict(_DEFAULT_BUILTINS)
        if builtins is not None:
            merged_builtins.update(builtins)
        self._builtins = merged_builtins
        self._mode = mode
        self._policy_engine = policy_engine
        # Dependency graph tracking state.
        self._dep_graph: _InternalGraph = _InternalGraph()
        # Set to a live set while evaluating an assignment RHS; None otherwise.
        self._tracking: set[str] | None = None
        # Stack of accumulated context-flow dependency sets (variable names).
        # The bottom element is always frozenset() (top-level, no enclosing block).
        # In STRICT mode, entering an if/for block pushes a new frozenset that
        # merges the test/iterable variable names with the enclosing entry.
        self._dep_ctx_stack: list[frozenset[str]] = [frozenset()]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def store(self) -> dict[str, CaMeLValue]:
        """Return a shallow copy of the current variable store.

        The returned dict is a snapshot; mutations do not affect the
        interpreter's internal state.

        Returns
        -------
        dict[str, CaMeLValue]
            All variables currently defined in the session.
        """
        return dict(self._store)

    def get(self, name: str) -> CaMeLValue:
        """Return the :class:`~camel.value.CaMeLValue` bound to *name*.

        Parameters
        ----------
        name:
            Variable name to look up.

        Returns
        -------
        CaMeLValue
            The value currently stored under *name*.

        Raises
        ------
        KeyError
            If *name* is not defined in the store.
        """
        return self._store[name]

    def seed(self, name: str, value: CaMeLValue) -> None:
        """Inject a :class:`~camel.value.CaMeLValue` into the store.

        Use this to pre-populate variables from outside the interpreter (e.g.
        to pass a tool result from a prior orchestration step into a new code
        block without re-executing).

        Parameters
        ----------
        name:
            Variable name to bind.
        value:
            The :class:`~camel.value.CaMeLValue` to store.

        Raises
        ------
        TypeError
            If *value* is not a :class:`~camel.value.CaMeLValue` instance.
        """
        if not isinstance(value, CaMeLValue):
            raise TypeError(
                f"seed: expected CaMeLValue, got {type(value).__name__!r}"
            )
        self._store[name] = value

    def set_mode(self, mode: ExecutionMode | str) -> None:
        """Set the execution mode for this session.

        Parameters
        ----------
        mode:
            Either an :class:`ExecutionMode` enum member or its string value
            (``"normal"`` or ``"strict"``).  The change takes effect
            immediately for subsequent :meth:`exec` calls.

        Raises
        ------
        ValueError
            If *mode* is a string that does not match any :class:`ExecutionMode`
            member.
        """
        if isinstance(mode, str):
            mode = ExecutionMode(mode)
        self._mode = mode

    def get_dependency_graph(self, variable: str) -> DependencyGraph:
        """Return the dependency graph snapshot for *variable*.

        Delegates to :meth:`_InternalGraph.subgraph`; returns an immutable
        :class:`DependencyGraph` with all upstream variable dependencies
        computed transitively.

        Parameters
        ----------
        variable:
            The variable name to query.

        Returns
        -------
        DependencyGraph
            Frozen snapshot.  All fields are empty if *variable* has never
            been assigned in this session.
        """
        return self._dep_graph.subgraph(variable)

    def exec(self, code: str) -> None:
        """Parse and execute a restricted-Python code string.

        The code is parsed with ``ast.parse(code, mode="exec")`` and each
        top-level statement is executed in order.  The variable store is
        updated in place; it is NOT cleared before execution.

        Parameters
        ----------
        code:
            A string of pseudo-Python source code in the supported grammar
            subset.

        Raises
        ------
        UnsupportedSyntaxError
            If the code contains any AST node outside the supported grammar.
        PolicyViolationError
            If a policy engine is set and denies a tool call.
        NameError
            If a ``Name`` node references a variable not in the store, tools,
            or builtins.
        SyntaxError
            If the code is not valid Python (propagated from ``ast.parse``).
        TypeError
            If a registered tool returns a value that is not a
            :class:`~camel.value.CaMeLValue`.

        Implementation notes
        --------------------
        1. Call ``ast.parse(code, mode="exec")`` to obtain a ``ast.Module``.
        2. Call ``self._exec_statements(module.body, ctx_caps=None)``.
        """
        module = ast.parse(code, mode="exec")
        self._exec_statements(module.body, ctx_caps=None)

    # ------------------------------------------------------------------
    # Statement dispatchers
    # ------------------------------------------------------------------

    def _exec_statements(
        self,
        stmts: list[ast.stmt],
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute a list of statements in order.

        Iterates over *stmts* and dispatches each to the appropriate
        ``_exec_*`` method.  Raises :class:`UnsupportedSyntaxError` for any
        statement type not in the supported grammar.

        Parameters
        ----------
        stmts:
            List of ``ast.stmt`` nodes to execute.
        ctx_caps:
            Context capabilities accumulated from enclosing control-flow
            constructs.  ``None`` in NORMAL mode or at the top level in STRICT
            mode.
        """
        for stmt in stmts:
            self._exec_statement(stmt, ctx_caps)

    def _exec_statement(
        self,
        node: ast.stmt,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Dispatch a single statement node to the appropriate ``_exec_*`` method.

        Supported statement types:
        - ``ast.Assign``    → :meth:`_exec_Assign`
        - ``ast.AugAssign`` → :meth:`_exec_AugAssign`
        - ``ast.If``        → :meth:`_exec_If`
        - ``ast.For``       → :meth:`_exec_For`
        - ``ast.Expr``      → :meth:`_exec_Expr`

        All other types raise :class:`UnsupportedSyntaxError`.

        Parameters
        ----------
        node:
            The statement node to execute.
        ctx_caps:
            Forwarded to the delegated method unchanged.
        """
        if isinstance(node, ast.Assign):
            self._exec_Assign(node, ctx_caps)
        elif isinstance(node, ast.AugAssign):
            self._exec_AugAssign(node, ctx_caps)
        elif isinstance(node, ast.If):
            self._exec_If(node, ctx_caps)
        elif isinstance(node, ast.For):
            self._exec_For(node, ctx_caps)
        elif isinstance(node, ast.Expr):
            self._exec_Expr(node, ctx_caps)
        else:
            raise self._unsupported(node)

    def _exec_Assign(
        self,
        node: ast.Assign,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute an assignment statement (``x = expr`` or ``a, b = expr``).

        Algorithm
        ---------
        1. Evaluate ``node.value`` via :meth:`_eval` → ``rhs_cv``.
        2. In STRICT mode: ``rhs_cv = _merge_ctx_caps(rhs_cv, ctx_caps)``.
        3. For each target in ``node.targets``:
           call :meth:`_store_target`\(target, rhs_cv)`.

        Supported target shapes
        -----------------------
        - ``ast.Name`` (Store): store ``rhs_cv`` under the variable name.
        - ``ast.Tuple`` or ``ast.List`` of ``ast.Name`` nodes: tuple-unpack.
          For each ``(i, name_node)`` pair, extract element ``i`` using
          :func:`~camel.value.propagate_subscript`\(rhs_cv,
          wrap(i, sources=frozenset({"CaMeL"})), rhs_cv.raw[i])`.
          In STRICT mode merge ``ctx_caps`` into each element before storing.
        - Nested unpacking (e.g. ``(a, (b, c)) = …``): raise
          :class:`UnsupportedSyntaxError`.
        - ``ast.Subscript`` / ``ast.Attribute`` targets: raise
          :class:`UnsupportedSyntaxError` (in-place mutation out of scope).

        Multiple targets (``a = b = expr``) are each stored with the same
        ``rhs_cv``.
        """
        self._tracking = set()
        rhs_cv = self._eval(node.value, ctx_caps)
        direct_deps = frozenset(self._tracking)
        self._tracking = None
        if self._mode == ExecutionMode.STRICT:
            rhs_cv = self._merge_ctx_caps(rhs_cv, ctx_caps)
        combined_deps = direct_deps | self._dep_ctx_stack[-1]
        for target in node.targets:
            self._store_target(target, rhs_cv, combined_deps)

    def _exec_AugAssign(
        self,
        node: ast.AugAssign,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute an augmented assignment (``x += expr``, ``x -= expr``, …).

        Algorithm
        ---------
        1. The target MUST be ``ast.Name``; otherwise raise
           :class:`UnsupportedSyntaxError`.
        2. Load the current value from ``_store``; raise ``NameError`` if
           not found.
        3. Evaluate the RHS via :meth:`_eval` → ``rhs_cv``.
        4. Apply the operator: look up ``type(node.op)`` in ``_BINOP_MAP``.
           ``raw_result = _BINOP_MAP[type(node.op)](current.raw, rhs_cv.raw)``
        5. Propagate caps: ``result_cv = propagate_binary_op(current, rhs_cv, raw_result)``.
        6. In STRICT mode: ``result_cv = _merge_ctx_caps(result_cv, ctx_caps)``.
        7. Store ``result_cv`` under the variable name.
        """
        if not isinstance(node.target, ast.Name):
            raise self._unsupported(node.target)
        name = node.target.id
        if name not in self._store:
            raise NameError(f"name {name!r} is not defined")
        current = self._store[name]
        self._tracking = set()
        rhs_cv = self._eval(node.value, ctx_caps)
        direct_deps = frozenset(self._tracking)
        self._tracking = None
        op_type = type(node.op)
        if op_type not in _BINOP_MAP:
            raise self._unsupported(node)
        raw_result = _BINOP_MAP[op_type](current.raw, rhs_cv.raw)
        result_cv = propagate_binary_op(current, rhs_cv, raw_result)
        if self._mode == ExecutionMode.STRICT:
            result_cv = self._merge_ctx_caps(result_cv, ctx_caps)
        self._store[name] = result_cv
        # x += y means x depends on its previous self plus y.
        combined_deps = frozenset({name}) | direct_deps | self._dep_ctx_stack[-1]
        self._dep_graph.record(name, combined_deps)

    def _exec_If(
        self,
        node: ast.If,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute an ``if`` / ``else`` statement.

        Algorithm
        ---------
        1. Evaluate ``node.test`` via :meth:`_eval` → ``test_cv``.
        2. Build ``inner_ctx``:
           - STRICT mode: ``inner_ctx = _merge_ctx_caps(test_cv, ctx_caps)``
             (i.e. union of outer ctx and test capabilities).
           - NORMAL mode: ``inner_ctx = ctx_caps`` (unchanged).
        3. If ``test_cv.raw`` is truthy: execute ``node.body`` with ``inner_ctx``.
        4. Else: execute ``node.orelse`` with ``inner_ctx``.
           (``node.orelse`` is an empty list when there is no ``else`` clause.)
        """
        if self._mode == ExecutionMode.STRICT:
            self._tracking = set()
        test_cv = self._eval(node.test, ctx_caps)
        inner_ctx: CaMeLValue | None
        if self._mode == ExecutionMode.STRICT:
            test_deps = frozenset(self._tracking or ())
            self._tracking = None
            inner_ctx = self._merge_ctx_caps(test_cv, ctx_caps)
            self._dep_ctx_stack.append(self._dep_ctx_stack[-1] | test_deps)
        else:
            inner_ctx = ctx_caps
        try:
            if test_cv.raw:
                self._exec_statements(node.body, inner_ctx)
            else:
                self._exec_statements(node.orelse, inner_ctx)
        finally:
            if self._mode == ExecutionMode.STRICT:
                self._dep_ctx_stack.pop()

    def _exec_For(
        self,
        node: ast.For,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute a ``for`` loop.

        ``for … else`` is NOT supported; a ``node.orelse`` that is non-empty
        raises :class:`UnsupportedSyntaxError`.

        The loop target MUST be a single ``ast.Name`` node or a flat
        ``ast.Tuple`` / ``ast.List`` of ``ast.Name`` nodes (tuple unpacking).
        Nested targets raise :class:`UnsupportedSyntaxError`.

        Algorithm
        ---------
        1. Evaluate ``node.iter`` via :meth:`_eval` → ``iter_cv``.
        2. Build ``inner_ctx``:
           - STRICT mode: ``inner_ctx = _merge_ctx_caps(iter_cv, ctx_caps)``.
           - NORMAL mode: ``inner_ctx = ctx_caps``.
        3. For each element ``element_raw`` in ``iter_cv.raw``:
           a. Wrap the element: ``element_cv = propagate_subscript(iter_cv,
              wrap(idx, sources=frozenset({"CaMeL"})), element_raw)``
              where ``idx`` is the current iteration index.
           b. In STRICT mode: ``element_cv = _merge_ctx_caps(element_cv, inner_ctx)``.
           c. Store the element via :meth:`_store_target`\(node.target, element_cv)`.
           d. Execute ``node.body`` with ``inner_ctx``.
        4. After the loop the loop-target variable remains in ``_store``
           with the value from the last iteration (Python semantics).
        """
        if node.orelse:
            raise self._unsupported(node)
        # Always track iterable variables so the loop target's direct dep is
        # recorded correctly in both NORMAL and STRICT mode.
        self._tracking = set()
        iter_cv = self._eval(node.iter, ctx_caps)
        iter_deps = frozenset(self._tracking)
        self._tracking = None
        outer_ctx_deps = self._dep_ctx_stack[-1]
        # The loop target depends on iter_deps plus any enclosing ctx deps.
        target_deps = iter_deps | outer_ctx_deps
        inner_ctx2: CaMeLValue | None
        if self._mode == ExecutionMode.STRICT:
            inner_ctx2 = self._merge_ctx_caps(iter_cv, ctx_caps)
            self._dep_ctx_stack.append(outer_ctx_deps | iter_deps)
        else:
            inner_ctx2 = ctx_caps
        try:
            for idx, element_raw in enumerate(iter_cv.raw):
                idx_cv = wrap(idx, sources=frozenset({"CaMeL"}))
                element_cv = propagate_subscript(iter_cv, idx_cv, element_raw)
                if self._mode == ExecutionMode.STRICT:
                    element_cv = self._merge_ctx_caps(element_cv, inner_ctx2)
                self._store_target(node.target, element_cv, target_deps)
                self._exec_statements(node.body, inner_ctx2)
        finally:
            if self._mode == ExecutionMode.STRICT:
                self._dep_ctx_stack.pop()

    def _exec_Expr(
        self,
        node: ast.Expr,
        ctx_caps: CaMeLValue | None,
    ) -> None:
        """Execute a bare expression statement (typically a function call).

        Evaluates ``node.value`` via :meth:`_eval` and discards the result.
        Used for tool calls whose return value is intentionally not captured.

        Parameters
        ----------
        node:
            The ``ast.Expr`` statement node.
        ctx_caps:
            Forwarded to :meth:`_eval` unchanged.
        """
        self._eval(node.value, ctx_caps)

    # ------------------------------------------------------------------
    # Expression evaluators
    # ------------------------------------------------------------------

    def _eval(
        self,
        node: ast.expr,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Dispatch an expression node to the appropriate ``_eval_*`` method.

        All expression evaluators return a :class:`~camel.value.CaMeLValue`.
        Unsupported expression types raise :class:`UnsupportedSyntaxError`.

        The ``ctx_caps`` argument is NOT applied inside ``_eval`` itself; it is
        only applied at **assignment** sites via :meth:`_merge_ctx_caps`.
        ``_eval`` threads ``ctx_caps`` down only so that ``_exec_If`` and
        ``_exec_For`` have access to the accumulated context when they need to
        build ``inner_ctx``.

        Supported dispatch targets
        --------------------------
        ``Constant``, ``Name``, ``BinOp``, ``UnaryOp``, ``BoolOp``,
        ``Compare``, ``Call``, ``Attribute``, ``Subscript``,
        ``List``, ``Tuple``, ``Dict``, ``JoinedStr``

        Special cases
        -------------
        - ``ast.Index`` (Python ≤ 3.8 compatibility): unwrap and recurse into
          ``node.value``.
        - All other node types: raise :class:`UnsupportedSyntaxError`.
        """
        # Python ≤ 3.8 compatibility: ast.Index wraps the actual key node.
        if hasattr(ast, "Index") and isinstance(node, ast.Index):  # type: ignore[attr-defined]
            return self._eval(node.value, ctx_caps)  # type: ignore[attr-defined]

        if isinstance(node, ast.Constant):
            return self._eval_Constant(node, ctx_caps)
        elif isinstance(node, ast.Name):
            return self._eval_Name(node, ctx_caps)
        elif isinstance(node, ast.BinOp):
            return self._eval_BinOp(node, ctx_caps)
        elif isinstance(node, ast.UnaryOp):
            return self._eval_UnaryOp(node, ctx_caps)
        elif isinstance(node, ast.BoolOp):
            return self._eval_BoolOp(node, ctx_caps)
        elif isinstance(node, ast.Compare):
            return self._eval_Compare(node, ctx_caps)
        elif isinstance(node, ast.Call):
            return self._eval_Call(node, ctx_caps)
        elif isinstance(node, ast.Attribute):
            return self._eval_Attribute(node, ctx_caps)
        elif isinstance(node, ast.Subscript):
            return self._eval_Subscript(node, ctx_caps)
        elif isinstance(node, ast.List):
            return self._eval_List(node, ctx_caps)
        elif isinstance(node, ast.Tuple):
            return self._eval_Tuple(node, ctx_caps)
        elif isinstance(node, ast.Dict):
            return self._eval_Dict(node, ctx_caps)
        elif isinstance(node, ast.JoinedStr):
            return self._eval_JoinedStr(node, ctx_caps)
        else:
            raise self._unsupported(node)

    def _eval_Constant(
        self,
        node: ast.Constant,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a constant literal.

        All constants — integers, floats, strings, booleans, ``None``,
        byte strings, ellipsis — are wrapped with:

        - ``sources = frozenset({"User literal"})``
        - ``inner_source = None``
        - ``readers = Public``

        Rationale: constants in P-LLM-generated code originate from the
        trusted user query; they are not derived from untrusted tool output.

        Returns
        -------
        CaMeLValue
            ``wrap(node.value, sources=frozenset({"User literal"}), readers=Public)``
        """
        return wrap(node.value, sources=frozenset({"User literal"}), readers=Public)

    def _eval_Name(
        self,
        node: ast.Name,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a name reference (variable load).

        Lookup order
        ------------
        1. ``_tools`` — if the name is a registered tool, return a synthetic
           callable ``CaMeLValue``.  (This path is rarely triggered directly
           via ``Name`` load; ``_eval_Call`` handles tool dispatch.  However,
           a Name node whose context is ``ast.Load`` and whose id is a tool
           name may appear in edge cases — treat it as a callable reference.)
        2. ``_builtins`` — same treatment.
        3. ``_store`` — return the stored :class:`~camel.value.CaMeLValue`.

        Raises ``NameError(node.id)`` if the name is not found in any of the
        three namespaces.

        Note: ``ast.Name`` nodes in ``ast.Store`` context are handled by
        :meth:`_store_target`, not here.
        """
        name = node.id
        if name in self._tools:
            return wrap(self._tools[name], sources=frozenset({"CaMeL"}), readers=Public)
        if name in self._builtins:
            return wrap(self._builtins[name], sources=frozenset({"CaMeL"}), readers=Public)
        if name in self._store:
            # Record this variable reference for dependency tracking.
            if self._tracking is not None:
                self._tracking.add(name)
            return self._store[name]
        raise NameError(f"name {name!r} is not defined")

    def _eval_BinOp(
        self,
        node: ast.BinOp,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a binary operation (``a + b``, ``a * b``, etc.).

        Algorithm
        ---------
        1. Evaluate ``node.left`` → ``left_cv``.
        2. Evaluate ``node.right`` → ``right_cv``.
        3. Look up ``type(node.op)`` in ``_BINOP_MAP``; raise
           :class:`UnsupportedSyntaxError` if not found (should not happen for
           standard operators).
        4. ``raw_result = op_fn(left_cv.raw, right_cv.raw)``
        5. Return ``propagate_binary_op(left_cv, right_cv, raw_result)``.
        """
        left_cv = self._eval(node.left, ctx_caps)
        right_cv = self._eval(node.right, ctx_caps)
        op_type = type(node.op)
        if op_type not in _BINOP_MAP:
            raise self._unsupported(node)
        raw_result = _BINOP_MAP[op_type](left_cv.raw, right_cv.raw)
        return propagate_binary_op(left_cv, right_cv, raw_result)

    def _eval_UnaryOp(
        self,
        node: ast.UnaryOp,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a unary operation (``-x``, ``not x``, ``~x``, ``+x``).

        Algorithm
        ---------
        1. Evaluate ``node.operand`` → ``operand_cv``.
        2. Look up ``type(node.op)`` in ``_UNARYOP_MAP``; raise
           :class:`UnsupportedSyntaxError` if not found.
        3. ``raw_result = op_fn(operand_cv.raw)``
        4. Return ``propagate_assignment(operand_cv, raw_result)``
           (same capability envelope, new raw value).
        """
        operand_cv = self._eval(node.operand, ctx_caps)
        op_type = type(node.op)
        if op_type not in _UNARYOP_MAP:
            raise self._unsupported(node)
        raw_result = _UNARYOP_MAP[op_type](operand_cv.raw)
        return propagate_assignment(operand_cv, raw_result)

    def _eval_BoolOp(
        self,
        node: ast.BoolOp,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a boolean expression (``a and b and c``, ``a or b``).

        Important: **all operands are evaluated** even if Python's normal
        short-circuit semantics would skip some.  This is necessary to
        compute the full capability union — an attacker could plant untrusted
        data in a skipped operand and its capabilities would otherwise be lost.

        Algorithm
        ---------
        1. Evaluate all operands in ``node.values`` → list of ``CaMeLValue``.
        2. Compute the raw Python result using **short-circuit semantics**:
           - ``and``: fold with short-circuit ``and`` over raw values.
           - ``or``:  fold with short-circuit ``or``  over raw values.
        3. Fold ALL operand ``CaMeLValue``s left-to-right with
           ``propagate_binary_op``, using the short-circuit raw result as the
           final value.

        Implementation hint: compute the raw result first (standard short-
        circuit), then fold capabilities across all operands regardless.
        Use ``propagate_binary_op(acc, operand_cv, raw_result)`` on the last
        fold to set the correct raw value.
        """
        # Evaluate ALL operands for capability tracking.
        operand_cvs = [self._eval(v, ctx_caps) for v in node.values]
        raw_values = [cv.raw for cv in operand_cvs]

        # Compute raw result with proper short-circuit semantics.
        if isinstance(node.op, ast.And):
            raw_result: Any = raw_values[0]
            for rv in raw_values[1:]:
                if not raw_result:
                    break
                raw_result = rv
        else:  # ast.Or
            raw_result = raw_values[0]
            for rv in raw_values[1:]:
                if raw_result:
                    break
                raw_result = rv

        # Fold all capabilities left-to-right; set correct raw on each step.
        if len(operand_cvs) == 1:
            return propagate_assignment(operand_cvs[0], raw_result)
        acc = operand_cvs[0]
        for cv in operand_cvs[1:]:
            acc = propagate_binary_op(acc, cv, raw_result)
        return acc

    def _eval_Compare(
        self,
        node: ast.Compare,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a comparison expression (``a == b``, ``a < b < c``, ``x in lst``).

        ``ast.Compare`` supports chained comparisons: ``left op1 comp1 op2 comp2 …``.
        Python evaluates these with short-circuit semantics:
        ``left op1 comp1 AND comp1 op2 comp2 AND …``

        Algorithm
        ---------
        1. Evaluate ``node.left`` → ``left_cv``.
        2. Initialise ``acc_cv = left_cv``.
        3. For each ``(op, comparator_node)`` in ``zip(node.ops, node.comparators)``:
           a. Evaluate ``comparator_node`` → ``comp_cv``.
           b. Compute pairwise raw bool: ``pair_bool = _CMPOP_MAP[type(op)](prev_raw, comp_cv.raw)``
              where ``prev_raw`` is the raw value of the previous comparator.
           c. ``acc_cv = propagate_binary_op(acc_cv, comp_cv, pair_bool)``
              Note: the raw value is the pairwise boolean, not the final chain result.
        4. Compute the full chain boolean using Python's built-in chained
           comparison (i.e. re-evaluate over raw values), and set it as the
           final raw value of the accumulated ``CaMeLValue``.

        Alternative simpler algorithm: evaluate all operands, fold capabilities
        with ``propagate_binary_op`` using a dummy value, then replace the raw
        value with the Python chained-comparison result.
        """
        left_cv = self._eval(node.left, ctx_caps)
        comp_cvs = [self._eval(c, ctx_caps) for c in node.comparators]

        # Compute the full chained comparison result over raw values.
        raw_values = [left_cv.raw] + [cv.raw for cv in comp_cvs]
        raw_result: bool = True
        for i, op in enumerate(node.ops):
            op_type = type(op)
            if op_type not in _CMPOP_MAP:
                raise self._unsupported(node)
            if not _CMPOP_MAP[op_type](raw_values[i], raw_values[i + 1]):
                raw_result = False
                break

        # Fold all capabilities left-to-right; final value is raw_result.
        acc = left_cv
        for cv in comp_cvs:
            acc = propagate_binary_op(acc, cv, raw_result)
        return acc

    def _eval_Call(
        self,
        node: ast.Call,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a function / tool call.

        Callee resolution
        -----------------
        The callee (``node.func``) must be one of:

        - ``ast.Name``: look up in ``_tools`` first, then ``_builtins``, then
          ``_store`` (if the stored value's raw is callable).
        - ``ast.Attribute``: evaluate ``node.func.value`` → ``obj_cv``;
          ``callable = getattr(obj_cv.raw, node.func.attr)``.  Treat as a
          builtin (wraps return value; no policy check).
        - Any other shape: raise :class:`UnsupportedSyntaxError`.

        Argument evaluation
        -------------------
        1. Evaluate each positional arg (``node.args``) → ``list[CaMeLValue]``.
        2. Evaluate each keyword arg (``node.keywords``) → ``dict[str, CaMeLValue]``.
        3. ``ast.Starred`` and double-starred kwargs raise
           :class:`UnsupportedSyntaxError`.

        Tool call path (callee in ``_tools``)
        --------------------------------------
        1. **Policy check** (if ``_policy_engine`` is not ``None``):
           build a ``kwargs_mapping: dict[str, CaMeLValue]`` that includes both
           positional args (mapped to parameter names via ``inspect.signature``)
           and keyword args.
           Call ``_policy_engine.check(tool_name, kwargs_mapping)``.
           If denied: raise :class:`PolicyViolationError`.
        2. **Call** the tool with raw values:
           ``result_cv = tool(*[a.raw for a in pos_args], **{k: v.raw for k, v in kw_args.items()})``
        3. **Type assertion**: ``isinstance(result_cv, CaMeLValue)`` → raise
           ``TypeError`` if not.
        4. Return ``result_cv``.

        Builtin call path (callee in ``_builtins`` or attribute)
        ---------------------------------------------------------
        1. Call with raw values:
           ``result_raw = builtin(*[a.raw for a in pos_args], **{k: v.raw ...})``
        2. Collect all arg ``CaMeLValue``s (positional + keyword values).
        3. Wrap the result:
           - If no args: ``wrap(result_raw)`` (sources=frozenset(), readers=Public).
           - Otherwise: use :func:`~camel.value.propagate_list_construction` on
             all arg ``CaMeLValue``s to obtain the union capability envelope,
             then construct:
             ``CaMeLValue(value=result_raw, sources=unioned.sources,
             inner_source=None, readers=unioned.readers)``.
        4. Return the wrapped ``CaMeLValue``.
        """
        # Reject starred / double-starred arguments.
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise self._unsupported(arg)
        for kw in node.keywords:
            if kw.arg is None:  # **kwargs unpacking
                raise self._unsupported(node)

        # Evaluate arguments.
        pos_arg_cvs: list[CaMeLValue] = [self._eval(a, ctx_caps) for a in node.args]
        kw_arg_cvs: dict[str, CaMeLValue] = {
            kw.arg: self._eval(kw.value, ctx_caps)  # type: ignore[misc]
            for kw in node.keywords
        }

        func_node = node.func

        if isinstance(func_node, ast.Name):
            name = func_node.id

            if name in self._tools:
                tool_fn = self._tools[name]

                # Policy check (if engine is set).
                if self._policy_engine is not None:
                    kwargs_mapping: dict[str, CaMeLValue] = {}
                    try:
                        sig = inspect.signature(tool_fn)
                        params = list(sig.parameters.keys())
                        for i, arg_cv in enumerate(pos_arg_cvs):
                            if i < len(params):
                                kwargs_mapping[params[i]] = arg_cv
                    except (ValueError, TypeError):
                        for i, arg_cv in enumerate(pos_arg_cvs):
                            kwargs_mapping[f"arg{i}"] = arg_cv
                    kwargs_mapping.update(kw_arg_cvs)
                    policy_result = self._policy_engine.check(name, kwargs_mapping)
                    if hasattr(policy_result, "reason") and policy_result.reason is not None:
                        raise PolicyViolationError(
                            tool_name=name, reason=str(policy_result.reason)
                        )

                # Call the tool with raw values.
                result_cv = tool_fn(
                    *[a.raw for a in pos_arg_cvs],
                    **{k: v.raw for k, v in kw_arg_cvs.items()},
                )
                if not isinstance(result_cv, CaMeLValue):
                    raise TypeError(
                        f"Tool {name!r} returned {type(result_cv).__name__!r}; "
                        f"expected CaMeLValue"
                    )
                return result_cv

            elif name in self._builtins:
                builtin_fn = self._builtins[name]
                result_raw = builtin_fn(
                    *[a.raw for a in pos_arg_cvs],
                    **{k: v.raw for k, v in kw_arg_cvs.items()},
                )
                all_arg_cvs: list[CaMeLValue] = pos_arg_cvs + list(kw_arg_cvs.values())
                return self._wrap_builtin_result(all_arg_cvs, result_raw)

            elif name in self._store:
                stored_cv = self._store[name]
                if not callable(stored_cv.raw):
                    raise TypeError(f"{name!r} is not callable")
                result_raw = stored_cv.raw(
                    *[a.raw for a in pos_arg_cvs],
                    **{k: v.raw for k, v in kw_arg_cvs.items()},
                )
                all_arg_cvs = [stored_cv] + pos_arg_cvs + list(kw_arg_cvs.values())
                return self._wrap_builtin_result(all_arg_cvs, result_raw)

            else:
                raise NameError(f"name {name!r} is not defined")

        elif isinstance(func_node, ast.Attribute):
            obj_cv = self._eval(func_node.value, ctx_caps)
            callable_fn = getattr(obj_cv.raw, func_node.attr)
            result_raw = callable_fn(
                *[a.raw for a in pos_arg_cvs],
                **{k: v.raw for k, v in kw_arg_cvs.items()},
            )
            all_arg_cvs = [obj_cv] + pos_arg_cvs + list(kw_arg_cvs.values())
            return self._wrap_builtin_result(all_arg_cvs, result_raw)

        else:
            raise self._unsupported(func_node)

    def _eval_Attribute(
        self,
        node: ast.Attribute,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate an attribute access (``obj.attr``).

        Algorithm
        ---------
        1. Evaluate ``node.value`` → ``obj_cv``.
        2. Synthesise a key ``CaMeLValue`` for the attribute name:
           ``key_cv = wrap(node.attr, sources=frozenset({"CaMeL"}), readers=Public)``
           (The attribute name itself is a trusted structural reference, not data.)
        3. ``raw_result = getattr(obj_cv.raw, node.attr)``
        4. Return ``propagate_subscript(obj_cv, key_cv, raw_result)``.

        Notes
        -----
        - ``ast.Attribute`` in ``ast.Store`` context (assignment target) is not
          supported and raises :class:`UnsupportedSyntaxError`.
        - Only ``ast.Load`` context is handled here.
        - If the object's raw value does not have the attribute, the ``getattr``
          call raises ``AttributeError`` which propagates as-is.
        """
        obj_cv = self._eval(node.value, ctx_caps)
        key_cv = wrap(node.attr, sources=frozenset({"CaMeL"}), readers=Public)
        raw_result = getattr(obj_cv.raw, node.attr)
        return propagate_subscript(obj_cv, key_cv, raw_result)

    def _eval_Subscript(
        self,
        node: ast.Subscript,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a subscript access (``container[key]``, ``lst[i]``, ``dct["k"]``).

        Algorithm
        ---------
        1. Evaluate ``node.value`` → ``container_cv``.
        2. Evaluate the slice:
           - Python 3.9+: ``node.slice`` is the key expression directly.
           - Python 3.8: ``node.slice`` may be ``ast.Index``; unwrap its
             ``.value`` before evaluating.
           - ``ast.Slice`` (slicing with ``:``): raise
             :class:`UnsupportedSyntaxError` (slice syntax not supported).
        3. ``key_cv = _eval(slice_node, ctx_caps)``
        4. ``raw_result = container_cv.raw[key_cv.raw]``
        5. Return ``propagate_subscript(container_cv, key_cv, raw_result)``.
        """
        container_cv = self._eval(node.value, ctx_caps)
        slice_node = node.slice
        # Python 3.8: ast.Index wraps the actual expression.
        if hasattr(ast, "Index") and isinstance(slice_node, ast.Index):  # type: ignore[attr-defined]
            slice_node = slice_node.value  # type: ignore[attr-defined]
        if isinstance(slice_node, ast.Slice):
            raise self._unsupported(slice_node)
        key_cv = self._eval(slice_node, ctx_caps)
        raw_result = container_cv.raw[key_cv.raw]
        return propagate_subscript(container_cv, key_cv, raw_result)

    def _eval_List(
        self,
        node: ast.List,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a list literal (``[a, b, c]``).

        Algorithm
        ---------
        1. Evaluate each element in ``node.elts`` → ``elem_cvs: list[CaMeLValue]``.
        2. ``raw_list = [cv.raw for cv in elem_cvs]``
        3. Return ``propagate_list_construction(elem_cvs, raw_list)``.
        """
        elem_cvs = [self._eval(e, ctx_caps) for e in node.elts]
        raw_list = [cv.raw for cv in elem_cvs]
        return propagate_list_construction(elem_cvs, raw_list)

    def _eval_Tuple(
        self,
        node: ast.Tuple,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a tuple literal (``(a, b)`` or ``a, b``).

        Identical to :meth:`_eval_List` except the raw value is a ``tuple``.

        Algorithm
        ---------
        1. Evaluate each element in ``node.elts`` → ``elem_cvs: list[CaMeLValue]``.
        2. ``raw_tuple = tuple(cv.raw for cv in elem_cvs)``
        3. Return ``propagate_list_construction(elem_cvs, list(raw_tuple))``.

        Note: ``propagate_list_construction`` accepts any sequence for the
        ``result`` parameter; passing a list of raw values is fine even when
        the final container is a tuple.
        """
        elem_cvs = [self._eval(e, ctx_caps) for e in node.elts]
        raw_tuple = tuple(cv.raw for cv in elem_cvs)
        return propagate_list_construction(elem_cvs, list(raw_tuple))

    def _eval_Dict(
        self,
        node: ast.Dict,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate a dict literal (``{"k": v, …}``).

        ``None`` keys (``**unpacking`` syntax in dict literals) raise
        :class:`UnsupportedSyntaxError`.

        Algorithm
        ---------
        1. For each ``(key_node, value_node)`` in ``zip(node.keys, node.values)``:
           - If ``key_node is None``: raise :class:`UnsupportedSyntaxError`
             (``{**other_dict}`` unpacking is not supported).
           - Evaluate ``key_node`` → ``key_cv``.
           - Evaluate ``value_node`` → ``val_cv``.
        2. ``raw_dict = {kc.raw: vc.raw for kc, vc in zip(key_cvs, val_cvs)}``
        3. Return ``propagate_dict_construction(key_cvs, val_cvs, raw_dict)``.
        """
        key_cvs: list[CaMeLValue] = []
        val_cvs: list[CaMeLValue] = []
        for key_node, val_node in zip(node.keys, node.values):
            if key_node is None:
                raise self._unsupported(node)
            key_cvs.append(self._eval(key_node, ctx_caps))
            val_cvs.append(self._eval(val_node, ctx_caps))
        raw_dict = {kc.raw: vc.raw for kc, vc in zip(key_cvs, val_cvs)}
        return propagate_dict_construction(key_cvs, val_cvs, raw_dict)

    def _eval_JoinedStr(
        self,
        node: ast.JoinedStr,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Evaluate an f-string (``f"Hello {name}!"``).

        ``ast.JoinedStr`` contains a ``values`` list of either ``ast.Constant``
        (the literal text segments) or ``ast.FormattedValue`` (the
        ``{expr}`` parts).

        Algorithm
        ---------
        1. Initialise ``parts: list[CaMeLValue] = []``.
        2. Accumulate the formatted string in ``raw_parts: list[str] = []``.
        3. For each part in ``node.values``:
           a. ``ast.Constant``: evaluate via :meth:`_eval_Constant` → ``cv``;
              append ``str(cv.raw)`` to ``raw_parts``; append ``cv`` to ``parts``.
           b. ``ast.FormattedValue``:
              - Evaluate ``part.value`` → ``val_cv``.
              - Apply conversion if set:
                ``ord('s')`` → ``str(val_cv.raw)``
                ``ord('r')`` → ``repr(val_cv.raw)``
                ``ord('a')`` → ``ascii(val_cv.raw)``
                ``-1``       → no conversion (use raw value directly)
              - Apply ``format_spec`` if set: recursively evaluate the nested
                ``ast.JoinedStr`` format_spec → ``spec_cv``; apply
                ``format(converted_val, spec_cv.raw)``; merge ``spec_cv`` into
                ``val_cv`` via ``propagate_binary_op``.
              - Append the formatted string to ``raw_parts``.
              - Append (possibly spec-merged) ``val_cv`` to ``parts``.
        4. ``raw_result = "".join(raw_parts)``
        5. Fold all ``parts`` left-to-right with ``propagate_binary_op``
           (initialise accumulator with the first part; use ``raw_result`` as
           the final raw value on the last fold).
        6. If ``parts`` is empty (empty f-string ``f""``), return
           ``wrap("", sources=frozenset({"User literal"}), readers=Public)``.
        """
        parts: list[CaMeLValue] = []
        raw_parts: list[str] = []

        for part in node.values:
            if isinstance(part, ast.Constant):
                cv = self._eval_Constant(part, ctx_caps)
                raw_parts.append(str(cv.raw))
                parts.append(cv)
            elif isinstance(part, ast.FormattedValue):
                val_cv = self._eval(part.value, ctx_caps)
                raw_val = val_cv.raw

                # Apply conversion.
                if part.conversion == ord("s"):
                    converted: Any = str(raw_val)
                elif part.conversion == ord("r"):
                    converted = repr(raw_val)
                elif part.conversion == ord("a"):
                    converted = ascii(raw_val)
                else:  # -1: no conversion
                    converted = raw_val

                # Apply format_spec if present.
                if part.format_spec is not None:
                    assert isinstance(part.format_spec, ast.JoinedStr)
                    spec_cv = self._eval_JoinedStr(part.format_spec, ctx_caps)
                    formatted = format(converted, spec_cv.raw)
                    val_cv = propagate_binary_op(val_cv, spec_cv, formatted)
                else:
                    formatted = format(converted, "")
                    val_cv = propagate_assignment(val_cv, formatted)

                raw_parts.append(formatted)
                parts.append(val_cv)
            else:
                raise self._unsupported(part)

        if not parts:
            return wrap("", sources=frozenset({"User literal"}), readers=Public)

        raw_result = "".join(raw_parts)

        if len(parts) == 1:
            return propagate_assignment(parts[0], raw_result)

        acc = parts[0]
        for cv in parts[1:]:
            acc = propagate_binary_op(acc, cv, raw_result)
        return acc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _merge_ctx_caps(
        self,
        value: CaMeLValue,
        ctx_caps: CaMeLValue | None,
    ) -> CaMeLValue:
        """Merge context capabilities into *value* when in STRICT mode.

        In NORMAL mode or when ``ctx_caps`` is ``None``, returns *value*
        unchanged.

        In STRICT mode with a non-``None`` ``ctx_caps``, returns:
        ``propagate_binary_op(value, ctx_caps, value.raw)``

        This preserves the raw value while unioning the capability metadata,
        so the result's ``sources`` and ``readers`` include both the value's
        own provenance and the enclosing control-flow condition's provenance.

        Parameters
        ----------
        value:
            The value to potentially enrich with context capabilities.
        ctx_caps:
            Accumulated context capabilities from enclosing ``if`` / ``for``
            constructs.  ``None`` means no context (top-level or NORMAL mode).

        Returns
        -------
        CaMeLValue
            The original *value* (NORMAL mode / no ctx) or a new instance
            with merged capabilities (STRICT mode with ctx).
        """
        if self._mode != ExecutionMode.STRICT or ctx_caps is None:
            return value
        return propagate_binary_op(value, ctx_caps, value.raw)

    def _store_target(
        self,
        target: ast.expr,
        value: CaMeLValue,
        deps: frozenset[str] = frozenset(),
    ) -> None:
        """Store a :class:`~camel.value.CaMeLValue` into the variable store for an assignment target.

        Supported target types
        ----------------------
        ``ast.Name`` (Store context):
            ``self._store[target.id] = value``

        ``ast.Tuple`` or ``ast.List`` (flat, all elements are ``ast.Name``):
            Tuple-unpack ``value.raw`` into the named variables.
            For each ``(i, name_node)`` pair:
            ``elem_cv = propagate_subscript(value, wrap(i, sources=frozenset({"CaMeL"})), value.raw[i])``
            ``self._store[name_node.id] = elem_cv``

        Raises :class:`UnsupportedSyntaxError` for:
        - Nested unpacking targets.
        - ``ast.Subscript`` or ``ast.Attribute`` targets.
        - Any other target type.

        Parameters
        ----------
        target:
            The ``ast.expr`` node representing the assignment target.
        value:
            The :class:`~camel.value.CaMeLValue` to store.
        deps:
            Dependency variable names to record for each assigned variable.
            Defaults to ``frozenset()`` (no variable dependencies).
        """
        if isinstance(target, ast.Name):
            self._store[target.id] = value
            self._dep_graph.record(target.id, deps)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, elem_target in enumerate(target.elts):
                if not isinstance(elem_target, ast.Name):
                    raise self._unsupported(elem_target)
                idx_cv = wrap(i, sources=frozenset({"CaMeL"}))
                elem_cv = propagate_subscript(value, idx_cv, value.raw[i])
                self._store[elem_target.id] = elem_cv
                self._dep_graph.record(elem_target.id, deps)
        elif isinstance(target, (ast.Subscript, ast.Attribute)):
            raise self._unsupported(target)
        else:
            raise self._unsupported(target)

    def _wrap_builtin_result(
        self,
        all_arg_cvs: Sequence[CaMeLValue],
        result: Any,
    ) -> CaMeLValue:
        """Wrap a builtin call's return value with the union of argument capabilities.

        If *all_arg_cvs* is empty, returns ``wrap(result)`` — i.e. a
        ``CaMeLValue`` with ``sources=frozenset()``, ``readers=Public``.

        Otherwise, uses :func:`~camel.value.propagate_list_construction` on
        *all_arg_cvs* to obtain the union capability envelope, then constructs
        a new ``CaMeLValue`` with *result* as the underlying value.

        Parameters
        ----------
        all_arg_cvs:
            All argument ``CaMeLValue``s (positional + keyword values).
        result:
            The raw Python return value from the builtin.

        Returns
        -------
        CaMeLValue
            Capability-tagged result.
        """
        if not all_arg_cvs:
            return wrap(result)
        union_cv = propagate_list_construction(list(all_arg_cvs), [])
        return CaMeLValue(
            value=result,
            sources=union_cv.sources,
            inner_source=None,
            readers=union_cv.readers,
        )

    def _unsupported(self, node: ast.AST) -> UnsupportedSyntaxError:
        """Construct an :class:`UnsupportedSyntaxError` for *node*.

        Convenience factory used throughout ``_exec_*`` and ``_eval_*``
        methods.  Extracts ``node_type`` and ``lineno`` automatically.

        Parameters
        ----------
        node:
            The unsupported AST node.

        Returns
        -------
        UnsupportedSyntaxError
            Ready to raise.

        Example
        -------
        ::

            raise self._unsupported(node)
        """
        return UnsupportedSyntaxError(
            node_type=type(node).__name__,
            lineno=getattr(node, "lineno", 0),
            message=(
                f"{type(node).__name__!r} is not part of the CaMeL "
                f"restricted grammar; see docs/adr/003-ast-interpreter-architecture.md"
            ),
        )
