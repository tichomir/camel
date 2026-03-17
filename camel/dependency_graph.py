"""Dependency graph — per-session data-flow tracking for the CaMeL interpreter.

Overview
--------
Every assignment in the CaMeL interpreter records which *variables* the
right-hand side referenced.  These records are stored in an
:class:`_InternalGraph` that grows monotonically throughout the session.
Callers query it through :class:`DependencyGraph` snapshots, obtained via
:meth:`CaMeLInterpreter.get_dependency_graph`.

Two tracking modes (controlled by the per-session
:class:`~camel.interpreter.ExecutionMode` flag):

``NORMAL`` mode
    Only data-assignment dependencies are recorded.  Assigning
    ``x = f(a, b)`` records that *x* directly depends on *a* and *b*.
    Control-flow conditions and loop iterables contribute no edges.

``STRICT`` mode
    In addition to data-assignment edges, the variables accessed in an
    ``if``-test or ``for``-iterable are recorded as dependencies on **every
    variable assigned within that block** (transitively, including nested
    blocks).  This closes timing-side-channel vectors: an adversary cannot
    infer whether a private boolean was ``True`` or ``False`` by observing
    which downstream tool calls are made, because the policy engine will see
    that those calls carry the private boolean as an upstream dependency.

Design document: ``docs/adr/004-dependency-graph-architecture.md``

Public surface
--------------
:class:`DependencyGraph`
    Frozen snapshot of the subgraph rooted at a queried variable.
    Fields: ``variable``, ``direct_deps``, ``all_upstream``, ``edges``.

:class:`_InternalGraph`
    Mutable graph that lives on the interpreter; not part of the public API
    but documented here so the backend developer understands the lifecycle.

Integration with the interpreter
---------------------------------
The :class:`~camel.interpreter.CaMeLInterpreter` must:

1. Hold an ``_dep_graph: _InternalGraph`` instance (created in ``__init__``).

2. Track variable accesses during expression evaluation via an optional
   ``_tracking: set[str] | None`` field (``None`` means "not tracking").

   - Set ``_tracking = set()`` before evaluating any RHS that will be
     assigned to one or more variables.
   - In ``_eval_name``: if ``self._tracking is not None``, add the name to
     ``self._tracking``.
   - After the RHS is evaluated, ``frozenset(self._tracking)`` is the set of
     directly referenced variables.
   - Reset ``self._tracking = None`` after capturing.

3. In ``_exec_assign`` and ``_exec_augassign``: call
   ``self._dep_graph.record(target_name, direct_deps | ctx_deps)`` where
   ``ctx_deps`` is the (possibly empty) frozenset of context dependencies
   threaded in from enclosing control-flow blocks.

4. In STRICT mode, before recursing into an ``if`` body or ``for`` body:
   evaluate the test/iterable expression with tracking enabled, capture the
   resulting ``frozenset[str]`` as ``block_ctx_deps``, and pass it down
   (merged with any outer ``ctx_deps``) to ``_exec_statements``.

   In NORMAL mode: pass ``frozenset()`` as ``ctx_deps`` to all sub-blocks
   (control flow contributes no dependency edges).

5. Expose ``get_dependency_graph(variable: str) -> DependencyGraph`` as a
   public method that delegates to ``_dep_graph.subgraph(variable)``.

Variable-access tracking — thread-safety note
---------------------------------------------
The ``_tracking`` field is intentionally simple (a bare ``set[str] | None``).
CaMeL's interpreter is synchronous (single-threaded); re-entrancy through
recursive ``_eval_expr`` calls is safe because each call **appends** to the
same set — all transitively accessed variables across an entire expression tree
are accumulated in a single flat set.

What is NOT tracked
--------------------
- Tool call internals: the tool function itself is not walked; only the
  argument variables (accessed via ``ast.Name`` nodes before the call) are
  recorded.
- Builtins: ``len``, ``str``, ``int``, etc. are not tracked as variable
  dependencies.
- Literal constants: ``ast.Constant`` nodes carry no variable origin.
- ``_tracking`` is reset to ``None`` immediately after capturing; nested
  ``_exec_statements`` sub-calls (e.g. inside a ``for`` body) will set their
  own tracking context when they evaluate individual assignment RHSes.

Interaction with CaMeLValue capabilities
-----------------------------------------
The dependency graph and the capability system (:mod:`camel.value`) are
**orthogonal** but complementary:

* Capabilities (``sources``, ``readers``) answer: *where did this data come
  from?* (tool identity, authorization).
* The dependency graph answers: *which variables influenced this variable?*
  (provenance in the execution plan).

The security policy engine can use *both* dimensions: capabilities to check
authorization, and the dependency graph to detect that a value was influenced
by an untrusted source variable (STRICT mode closes the control-flow channel).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# DependencyGraph — public, frozen snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyGraph:
    """Immutable snapshot of the upstream dependency subgraph for one variable.

    Returned by :meth:`CaMeLInterpreter.get_dependency_graph`.  Represents
    all variables (transitively) that influenced the value of ``variable``
    within the current session.

    Attributes
    ----------
    variable:
        The name of the root variable this graph was queried for.
    direct_deps:
        The set of variable names that *directly* appear in the RHS expression
        (or context-flow contribution in STRICT mode) of the most-recent
        assignment to ``variable``.
    all_upstream:
        Transitive closure of all upstream variable names — the full set of
        variables (at any depth) that contributed to the current value of
        ``variable``.  Does **not** include ``variable`` itself unless there
        is a dependency cycle (which can arise with augmented assignment:
        ``x += y`` makes x depend on x and y).
    edges:
        All directed edges ``(var, dep)`` in the subgraph, where the edge
        means "``var`` directly depends on ``dep``".  Only nodes reachable
        from ``variable`` through upstream edges are included.

    Notes
    -----
    * If ``variable`` has never been assigned in the session, all fields are
      empty (``direct_deps=frozenset()``, ``all_upstream=frozenset()``,
      ``edges=frozenset()``).
    * In NORMAL mode the edges reflect data flow only.  In STRICT mode the
      edges also include control-flow contributions (if-test and for-iterable
      variable references), making the graph larger.

    Examples
    --------
    Given the session::

        a = get_email()          # a depends on nothing (tool call, no var refs)
        b = a                    # b depends on {a}
        c = b + literal          # c depends on {b}  (literal is a constant)

    ``get_dependency_graph("c")`` in NORMAL mode returns::

        DependencyGraph(
            variable="c",
            direct_deps=frozenset({"b"}),
            all_upstream=frozenset({"a", "b"}),
            edges=frozenset({("c", "b"), ("b", "a")}),
        )

    In STRICT mode, given additionally::

        if flag:          # flag was assigned from an untrusted source
            result = c   # result depends on {c} ∪ {flag} in STRICT mode

    ``get_dependency_graph("result")`` returns::

        DependencyGraph(
            variable="result",
            direct_deps=frozenset({"c", "flag"}),
            all_upstream=frozenset({"a", "b", "c", "flag"}),
            edges=frozenset({
                ("result", "c"), ("result", "flag"),
                ("c", "b"), ("b", "a"),
            }),
        )
    """

    variable: str
    direct_deps: frozenset[str]
    all_upstream: frozenset[str]
    edges: frozenset[tuple[str, str]]


# ---------------------------------------------------------------------------
# _InternalGraph — mutable session-scoped graph (not public API)
# ---------------------------------------------------------------------------


class _InternalGraph:
    """Mutable dependency graph stored on the interpreter for the lifetime of a session.

    This is an *implementation detail*; external code should use
    :meth:`CaMeLInterpreter.get_dependency_graph` rather than interacting with
    this class directly.

    Representation
    --------------
    ``_direct: dict[str, set[str]]``
        Maps each variable name to the set of variable names it directly
        depends on.  The set is the **union** across all assignments to that
        variable within the session (because ``a = x; a = y`` means a depends
        on both x and y historically, though typically the interpreter replaces
        values rather than accumulating them — recording the union is the
        conservative safe choice).

    Lifecycle
    ---------
    * Created in ``CaMeLInterpreter.__init__``.
    * :meth:`record` is called from ``_exec_assign`` / ``_exec_augassign``
      once per assignment (including augmented assignment).
    * :meth:`subgraph` is called from ``get_dependency_graph``.
    * The graph is never reset during the session (it mirrors the variable
      store's monotonically growing history).

    Thread-safety
    -------------
    The CaMeL interpreter is synchronous.  No locking is required.
    """

    def __init__(self) -> None:
        # Maps variable name → set of direct dependency variable names.
        self._direct: dict[str, set[str]] = {}

    def record(self, variable: str, deps: frozenset[str]) -> None:
        """Record (merge) that *variable* directly depends on *deps*.

        Called by the interpreter after each assignment to accumulate the
        dependency edges for *variable*.  The edges are **merged** (union)
        with any previously recorded edges for *variable*, so that the graph
        represents the full history of what contributed to a variable's value
        across all assignments in the session.

        Parameters
        ----------
        variable:
            The name of the variable being assigned.
        deps:
            The frozenset of variable names directly referenced in the
            assignment's RHS expression (plus any context-flow contributions
            in STRICT mode).

        Notes
        -----
        * Calling ``record("x", frozenset())`` is a no-op for the edge set
          (x still gets an entry in ``_direct`` with an empty set).  This is
          intentional: it marks x as "seen" even if it has no variable deps
          (e.g. ``x = get_tool()`` — x came from a tool call, no var refs).
        * Self-edges (``"x"`` in ``deps`` when recording for ``"x"``) are
          valid for augmented assignments like ``x += y``.

        Examples
        --------
        ::

            graph = _InternalGraph()
            graph.record("b", frozenset({"a"}))
            graph.record("c", frozenset({"b"}))
            assert graph.direct_deps("c") == frozenset({"b"})
        """
        existing = self._direct.get(variable)
        if existing is None:
            self._direct[variable] = set(deps)
        else:
            existing.update(deps)

    def direct_deps(self, variable: str) -> frozenset[str]:
        """Return the set of variables that *variable* directly depends on.

        Parameters
        ----------
        variable:
            The variable name to query.

        Returns
        -------
        frozenset[str]
            Direct dependency variable names; ``frozenset()`` if *variable*
            has never been recorded or has no variable dependencies.
        """
        return frozenset(self._direct.get(variable, set()))

    def all_upstream(self, variable: str) -> frozenset[str]:
        """Return the full transitive closure of upstream dependencies.

        Performs a breadth-first traversal starting from *variable*'s direct
        dependencies, following edges upward.  *variable* itself is excluded
        from the result unless a dependency cycle exists.

        Parameters
        ----------
        variable:
            The root variable to query.

        Returns
        -------
        frozenset[str]
            All variable names that (directly or transitively) contributed to
            the current value of *variable*; ``frozenset()`` if *variable* has
            no known dependencies.

        Notes
        -----
        Cycle detection: the visited set prevents infinite loops in the
        presence of self-referential augmented assignments (``x += x``).

        Complexity: O(V + E) where V = number of distinct variable names and
        E = number of recorded edges.

        Examples
        --------
        ::

            graph = _InternalGraph()
            graph.record("b", frozenset({"a"}))
            graph.record("c", frozenset({"b"}))
            assert graph.all_upstream("c") == frozenset({"a", "b"})
        """
        visited: set[str] = set()
        queue: list[str] = list(self._direct.get(variable, set()))
        while queue:
            dep = queue.pop()
            if dep not in visited:
                visited.add(dep)
                queue.extend(self._direct.get(dep, set()))
        return frozenset(visited)

    def subgraph(self, variable: str) -> DependencyGraph:
        """Build and return a :class:`DependencyGraph` snapshot for *variable*.

        Computes the upstream transitive closure and collects all edges in the
        resulting subgraph.  The snapshot is immutable (frozen dataclass).

        Parameters
        ----------
        variable:
            The root variable to query.

        Returns
        -------
        DependencyGraph
            Frozen snapshot.  If *variable* is unknown, returns a
            ``DependencyGraph`` with all empty fields.

        Implementation notes for the backend developer
        -----------------------------------------------
        1. Compute ``direct = self.direct_deps(variable)``.
        2. Compute ``upstream = self.all_upstream(variable)``.
        3. Build the edge set by iterating over
           ``{variable} | upstream`` and for each node ``n`` emitting
           ``(n, dep)`` for every ``dep`` in ``self._direct.get(n, set())``.
           Only emit edges where ``dep`` is reachable from ``variable`` (i.e.
           ``dep in upstream`` or ``dep == variable``).  This bounds the
           returned edges to the relevant subgraph only.
        4. Construct and return ``DependencyGraph(...)``.

        Examples
        --------
        ::

            graph = _InternalGraph()
            graph.record("b", frozenset({"a"}))
            graph.record("c", frozenset({"b"}))
            dg = graph.subgraph("c")
            assert dg.variable == "c"
            assert dg.direct_deps == frozenset({"b"})
            assert dg.all_upstream == frozenset({"a", "b"})
            assert ("c", "b") in dg.edges
            assert ("b", "a") in dg.edges
        """
        direct = self.direct_deps(variable)
        upstream = self.all_upstream(variable)
        reachable = upstream | {variable}

        edges: set[tuple[str, str]] = set()
        for node in reachable:
            for dep in self._direct.get(node, set()):
                if dep in reachable:
                    edges.add((node, dep))

        return DependencyGraph(
            variable=variable,
            direct_deps=direct,
            all_upstream=upstream,
            edges=frozenset(edges),
        )

    def export(self) -> dict[str, frozenset[str]]:
        """Export the full graph as a plain-dict snapshot.

        Returns a ``{variable: frozenset_of_direct_deps}`` mapping for every
        variable that has been recorded.  Used by M4-F8 snapshot/restore to
        preserve STRICT mode annotations across ``NotEnoughInformationError``
        re-generation cycles.

        Returns
        -------
        dict[str, frozenset[str]]
            A shallow-copy snapshot; mutations do not affect the internal
            graph.
        """
        return {var: frozenset(deps) for var, deps in self._direct.items()}

    def import_(self, snapshot: dict[str, frozenset[str]]) -> None:
        """Restore the graph from a snapshot produced by :meth:`export`.

        Replaces the current graph contents with the snapshot data.  Any
        edges not present in the snapshot are discarded.  This is the
        counterpart to :meth:`export` used by M4-F8 to restore the
        dependency state after a ``NotEnoughInformationError`` re-generation
        cycle.

        Parameters
        ----------
        snapshot:
            ``{variable: frozenset_of_direct_deps}`` mapping, as returned by
            :meth:`export`.
        """
        self._direct = {var: set(deps) for var, deps in snapshot.items()}
