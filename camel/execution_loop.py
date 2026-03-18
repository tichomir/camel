"""CaMeL Execution Loop Orchestrator.

This module wires the P-LLM wrapper, CaMeL interpreter, Q-LLM wrapper, and
tool dispatch into a complete execution loop with retry, exception redaction,
execution trace recording, and print-output routing.

Design document: ``docs/adr/007-execution-loop-orchestrator.md``

Architecture overview
---------------------
The :class:`CaMeLOrchestrator` wires the following components into a single
``async run()`` call:

1. **P-LLM** (:class:`~camel.llm.PLLMWrapper`) — generates the initial
   execution plan from the user query.
2. **CaMeL Interpreter** (:class:`~camel.CaMeLInterpreter`) — executes the
   plan statement by statement; maintains capability-tagged variable store.
3. **Q-LLM** (:class:`~camel.llm.QLLMWrapper`) — performs schema-validated
   structured extraction on untrusted content; called from within the plan
   via the ``query_quarantined_llm`` builtin.
4. **Tool executor** — tools registered in the interpreter; each returns a
   :class:`~camel.CaMeLValue`.

On runtime exception the orchestrator:

- **Redacts** the error via :class:`ExceptionRedactor` based on data-provenance
  trust (full message / type+lineno only / fully redacted).
- **Captures** the accepted state: variable names already in the store and the
  remaining unexecuted source code.
- **Rebuilds** a P-LLM retry prompt via :class:`RetryPromptBuilder` (M2-F14).
- **Retries** up to ``max_loop_retries`` times (default 10, M2-F8).

Routing contract
----------------
- ``print()`` calls in execution plans → :class:`DisplayChannel` (M2-F10).
- Successful tool calls → :class:`TraceRecord` appended to the
  :class:`ExecutionTrace` (M2-F12).
- These two output streams are **strictly separated**.

Key feature references
----------------------
M2-F8   MaxRetriesExceededError and outer-loop retry ceiling (10).
M2-F10  print() output routed to a distinct display channel.
M2-F12  Execution trace: ordered (tool_name, args, memory_snapshot) tuples.
M2-F14  Partial re-execution prompt builder with accepted-state snapshot.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from camel.interpreter import CaMeLInterpreter
from camel.llm.exceptions import NotEnoughInformationError as LLMNotEnoughInfoError
from camel.llm.p_llm import PLLMWrapper, ToolSignature, UserContext
from camel.value import CaMeLValue, wrap

__all__ = [
    # Data models
    "RedactedError",
    "RedactionAuditEvent",
    "AcceptedState",
    "TraceRecord",
    "ExecutionTrace",
    "ExecutionResult",
    # Exceptions
    "MaxRetriesExceededError",
    # Protocols
    "DisplayChannel",
    # Concrete display channel
    "StdoutDisplayChannel",
    # Core helper classes
    "ExceptionRedactor",
    "RetryPromptBuilder",
    "TraceRecorder",
    # Orchestrator
    "CaMeLOrchestrator",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactedError:
    """A sanitised exception representation safe to forward to the P-LLM.

    :class:`ExceptionRedactor` produces instances of this class.  Exactly
    which fields are populated depends on the redaction case applied:

    - **trusted**: ``error_type``, ``lineno``, ``message`` all set.
    - **untrusted**: ``error_type`` and ``lineno`` only; ``message`` is
      ``None``.
    - **not_enough_information**: ``error_type`` only; ``lineno`` and
      ``message`` are both ``None``.

    Attributes
    ----------
    error_type:
        Python exception class name (e.g. ``"TypeError"``).  Always present.
    lineno:
        1-based source line number from the failing statement, or ``None``
        when unavailable or redacted.
    message:
        Human-readable error text, or ``None`` when redacted for security.
    trust_level:
        One of ``"trusted"``, ``"untrusted"``, or
        ``"not_enough_information"``.  Records which redaction rule applied;
        written to the security audit log per NFR-6.
    """

    error_type: str
    lineno: int | None
    message: str | None
    trust_level: Literal["trusted", "untrusted", "not_enough_information"]


@dataclass(frozen=True)
class RedactionAuditEvent:
    """Audit log entry emitted by :class:`ExceptionRedactor` on every classification.

    Emitted for every exception processed by
    :meth:`ExceptionRedactor.classify`, regardless of whether redaction was
    applied.  Provides a complete audit trail of all exception events so
    operators can review redaction decisions (NFR-6 / M4-F17).

    Attributes
    ----------
    timestamp:
        ISO-8601 UTC timestamp of the redaction decision.
    line_number:
        1-based AST source line number of the failing statement, or ``None``
        if unavailable.
    redaction_reason:
        Human-readable code indicating the redaction rule applied.
        One of:

        - ``"untrusted_dependency"`` — M4-F6 dependency taint check returned
          ``True``; message body replaced with ``None``.
        - ``"not_enough_information"`` — M4-F7 NEIE handler applied; all
          content stripped.
        - ``"loop_body_exception"`` — M4-F9 loop propagation applied.
        - ``"none"`` — trusted exception; no redaction.
    dependency_chain:
        Ordered list of ``(variable_name, source_label)`` pairs representing
        the taint propagation path that triggered redaction.  Empty list when
        ``redaction_reason`` is ``"none"``.
        Example: ``[("email_body", "get_last_email"), ("summary", "CaMeL")]``
        Capped at 50 entries.
    trust_level:
        The ``trust_level`` field from the resulting :class:`RedactedError`.
        One of ``"trusted"``, ``"untrusted"``, ``"not_enough_information"``.
    error_type:
        Exception class name (always included; never untrusted data).
    redacted_message_length:
        Length in characters of the original exception message before
        redaction, or ``0`` if the message was empty or ``None``.  Allows
        operators to gauge information density without re-exposing content.
    m4_f9_applied:
        ``True`` if M4-F9 loop-body exception STRICT propagation was applied
        to this event.

    Examples
    --------
    ::

        redactor = ExceptionRedactor(audit_log=my_log)
        redacted = redactor.classify(exc, store_snapshot)
        event = my_log[-1]
        assert event.error_type == type(exc).__name__
        assert event.trust_level == redacted.trust_level
    """

    timestamp: str
    line_number: int | None
    redaction_reason: str
    dependency_chain: list[tuple[str, str]]
    trust_level: str
    error_type: str
    redacted_message_length: int
    m4_f9_applied: bool


@dataclass(frozen=True)
class AcceptedState:
    """Snapshot of successfully executed interpreter state at the point of failure.

    Passed to :class:`RetryPromptBuilder` to construct the partial
    re-execution prompt (M2-F14).  Variable values are **never** included —
    only names — to preserve the P-LLM isolation invariant (ADR-005 §7).

    Attributes
    ----------
    variable_names:
        Frozenset of variable names currently defined in the interpreter
        store.  Names only — the P-LLM must treat them as opaque handles.
    executed_statement_count:
        Number of top-level AST statements successfully executed before
        the failure occurred.
    remaining_source:
        Source text of plan statements that had not yet executed when the
        failure occurred.  Reconstructed via ``ast.unparse()`` from the
        unexecuted portion of the original plan's AST.
    """

    variable_names: frozenset[str]
    executed_statement_count: int
    remaining_source: str
    # M4-F8: STRICT mode annotation preservation across NEIE re-generation.
    # Both fields are None for non-NEIE exceptions.
    dependency_graph_snapshot: dict[str, frozenset[str]] | None = None
    dep_ctx_stack_snapshot: tuple[frozenset[str], ...] | None = None
    # M4-F9: Loop-body exception STRICT propagation.
    # Both fields are None when M4-F9 does not apply.
    loop_iter_deps: frozenset[str] | None = None
    loop_iter_caps: CaMeLValue | None = None


@dataclass(frozen=True)
class TraceRecord:
    """A single successful tool-call event in the execution trace (M2-F12).

    Appended to the :data:`ExecutionTrace` by :class:`TraceRecorder` after
    each tool call that completes without raising an exception.

    Attributes
    ----------
    tool_name:
        The registered tool name (e.g. ``"send_email"``).
    args:
        Raw (unwrapped) argument values passed to the tool, keyed by
        parameter name.  These are ``CaMeLValue.raw`` extractions; the
        capability wrappers are stripped before recording to keep the trace
        serialisable.
    memory_snapshot:
        Shallow copy of the interpreter variable store **after** the tool
        call completes.  Values are full :class:`~camel.CaMeLValue` instances
        (with capability metadata intact) for policy audit purposes.
    """

    tool_name: str
    args: dict[str, Any]
    memory_snapshot: dict[str, CaMeLValue]


#: Ordered list of trace records produced by a completed execution (M2-F12).
ExecutionTrace = list[TraceRecord]


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of a successful :meth:`CaMeLOrchestrator.run` call.

    Attributes
    ----------
    trace:
        Ordered :data:`ExecutionTrace` — one :class:`TraceRecord` per
        successful tool call (M2-F12).
    print_outputs:
        Ordered list of :class:`~camel.CaMeLValue` instances that were
        passed to ``print()`` during execution (M2-F10).
    final_store:
        Shallow snapshot of the interpreter variable store after the plan
        completes.
    loop_attempts:
        Number of outer-loop retries consumed.  Zero means the first
        attempt succeeded without any runtime exception.
    """

    trace: ExecutionTrace
    print_outputs: list[CaMeLValue]
    final_store: dict[str, CaMeLValue]
    loop_attempts: int


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MaxRetriesExceededError(Exception):
    """Raised when the outer execution-loop retry ceiling is reached (M2-F8).

    The :class:`CaMeLOrchestrator` retries the execution loop up to
    ``max_loop_retries`` times (default 10) on any runtime exception.  When
    all attempts are exhausted without a successful plan completion, this
    exception is raised.

    Attributes
    ----------
    attempts:
        The number of outer-loop retries attempted before giving up.
    last_error:
        The most recent :class:`RedactedError` produced by the exception
        redactor, or ``None`` if failure occurred before any exception was
        classified (e.g. a P-LLM plan-generation failure on the first
        attempt).
    """

    def __init__(
        self,
        attempts: int,
        last_error: RedactedError | None = None,
    ) -> None:
        """Initialise with attempt count and optional last error."""
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"CaMeL execution loop exhausted after {attempts} "
            "outer-loop retries without a successful plan completion."
        )


# ---------------------------------------------------------------------------
# DisplayChannel protocol (M2-F10)
# ---------------------------------------------------------------------------


@runtime_checkable
class DisplayChannel(Protocol):
    """Routing sink for ``print()`` output from execution plans (M2-F10).

    The :class:`CaMeLOrchestrator` registers a custom ``print`` builtin that
    calls :meth:`write` instead of writing to ``sys.stdout``.  This decouples
    the user-visible output stream from both the execution trace and the audit
    log.

    Implementations may write to a terminal, a message queue, a websocket, or
    an in-memory buffer for testing.  The default concrete implementation is
    :class:`StdoutDisplayChannel`.
    """

    def write(self, value: CaMeLValue) -> None:
        """Route *value* to this channel's output sink.

        Parameters
        ----------
        value:
            The :class:`~camel.CaMeLValue` passed to ``print()`` in the
            execution plan.  Implementations should display ``value.raw``
            (the unwrapped Python value) to the end user.
        """
        ...


class StdoutDisplayChannel:
    """Default :class:`DisplayChannel` implementation — writes to ``sys.stdout``.

    Calls ``print(value.raw)`` for each value routed to it.  Suitable for
    CLI usage and development.  Production deployments should supply a
    custom implementation that writes to the appropriate UI sink.
    """

    def write(self, value: CaMeLValue) -> None:
        """Print the raw value of *value* to standard output.

        Parameters
        ----------
        value:
            Capability-tagged value from the execution plan's ``print()``
            call.  The raw Python value is displayed via ``print()``.
        """
        print(value.raw)


# ---------------------------------------------------------------------------
# ExceptionRedactor
# ---------------------------------------------------------------------------


class ExceptionRedactor:
    """Classifies runtime exceptions and produces sanitised :class:`RedactedError` instances.

    The classification follows three rules (in priority order):

    1. **NotEnoughInformationError** — fully redacted (type + lineno only;
       no message).  M4-F7.
    2. **Untrusted-dependency exception** — type + lineno; message omitted.
       Trust is checked via the dependency graph when an interpreter is
       available (M4-F6), falling back to a store-level sources scan.
    3. **Trusted-origin exception** — type + lineno + full message included.

    Every classification emits a :class:`RedactionAuditEvent` to the
    injected ``audit_log`` list (M4-F17).

    Parameters
    ----------
    trusted_sources:
        Frozenset of source labels considered trusted.  Defaults to
        ``frozenset({"User literal", "CaMeL"})``.
    audit_log:
        Optional list to which :class:`RedactionAuditEvent` entries are
        appended after each classification.  If ``None``, events are
        silently discarded (backward-compatible default for tests that do
        not configure a log).
    """

    #: Default set of source labels considered trusted for exception message
    #: inclusion.
    DEFAULT_TRUSTED_SOURCES: frozenset[str] = frozenset({"User literal", "CaMeL"})

    def __init__(
        self,
        trusted_sources: frozenset[str] | None = None,
        audit_log: list[RedactionAuditEvent] | None = None,
    ) -> None:
        """Initialise with an optional custom trusted-source set and audit log."""
        self._trusted_sources: frozenset[str] = (
            trusted_sources
            if trusted_sources is not None
            else self.DEFAULT_TRUSTED_SOURCES
        )
        self._audit_log = audit_log

    def classify(
        self,
        exc: BaseException,
        interpreter_store_snapshot: dict[str, CaMeLValue],
        interpreter: CaMeLInterpreter | None = None,
    ) -> RedactedError:
        """Produce a :class:`RedactedError` from *exc* under the store context.

        Parameters
        ----------
        exc:
            The exception raised during execution.
        interpreter_store_snapshot:
            Shallow copy of the interpreter variable store at the moment the
            exception was caught.  Used as a fallback taint check when
            *interpreter* is ``None``.
        interpreter:
            Optional reference to the live interpreter.  When provided,
            enables dependency-graph-aware taint checking (M4-F6) for more
            precise redaction decisions.

        Returns
        -------
        RedactedError
            Sanitised error representation.  The ``trust_level`` field
            documents which redaction case applied.
        """
        from camel.exceptions import NotEnoughInformationError as CamelNEIE  # noqa: PLC0415

        orig_message = str(exc) if str(exc) else None
        orig_message_len = len(str(exc)) if exc.args else 0

        # Check if M4-F9 loop annotation is present.
        m4_f9_applied = hasattr(exc, "__loop_iter_deps__")

        # M4-F7: NotEnoughInformationError — strip all content; expose type
        # and call-site line number only.
        if isinstance(exc, (CamelNEIE, LLMNotEnoughInfoError)):
            # Prefer __lineno__ (set by interpreter._eval_Call) over .lineno.
            lineno: int | None = getattr(exc, "__lineno__", None)
            if lineno is None:
                lineno = getattr(exc, "lineno", None)
            redacted = RedactedError(
                error_type=type(exc).__name__,
                lineno=lineno,
                message=None,
                trust_level="not_enough_information",
            )
            self._emit_audit_event(
                exc=exc,
                redacted=redacted,
                dependency_chain=[],
                redaction_reason="not_enough_information",
                orig_message_len=orig_message_len,
                m4_f9_applied=m4_f9_applied,
            )
            return redacted

        # Extract lineno: prefer __lineno__ (set by _eval_Call), fall back to
        # the exception's own .lineno attribute.
        lineno = getattr(exc, "__lineno__", None)
        if lineno is None:
            lineno = getattr(exc, "lineno", None)

        # M4-F6: dependency-graph-aware taint check.
        tainted, dep_chain = self._is_tainted(exc, interpreter_store_snapshot, interpreter)

        if tainted:
            redaction_reason = "loop_body_exception" if m4_f9_applied else "untrusted_dependency"
            redacted = RedactedError(
                error_type=type(exc).__name__,
                lineno=lineno,
                message=None,
                trust_level="untrusted",
            )
            self._emit_audit_event(
                exc=exc,
                redacted=redacted,
                dependency_chain=dep_chain,
                redaction_reason=redaction_reason,
                orig_message_len=orig_message_len,
                m4_f9_applied=m4_f9_applied,
            )
            return redacted

        # Rule 3: Trusted — include full message.
        redacted = RedactedError(
            error_type=type(exc).__name__,
            lineno=lineno,
            message=str(exc),
            trust_level="trusted",
        )
        self._emit_audit_event(
            exc=exc,
            redacted=redacted,
            dependency_chain=[],
            redaction_reason="none",
            orig_message_len=orig_message_len,
            m4_f9_applied=m4_f9_applied,
        )
        return redacted

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_tainted(
        self,
        exc: BaseException,
        store: dict[str, CaMeLValue],
        interpreter: CaMeLInterpreter | None,
    ) -> tuple[bool, list[tuple[str, str]]]:
        """Return ``(tainted, dependency_chain)`` for *exc*.

        When *interpreter* is available, performs a dependency-graph-aware
        taint check (M4-F6): walks the upstream graph for each store variable
        and checks all upstream sources.  Falls back to a flat store-level
        scan when *interpreter* is ``None``.

        Parameters
        ----------
        exc:
            The exception being classified.
        store:
            Interpreter variable store snapshot at exception time.
        interpreter:
            Live interpreter reference for dep-graph access (optional).

        Returns
        -------
        tuple[bool, list[tuple[str, str]]]
            ``(True, chain)`` when taint is detected; ``(False, [])``
            otherwise.  *chain* is a list of ``(variable_name, source_label)``
            pairs capped at 50 entries.
        """
        dep_chain: list[tuple[str, str]] = []

        if interpreter is not None:
            # M4-F6: dependency-graph-aware check.
            all_vars = set(store.keys())
            seen: set[tuple[str, str]] = set()
            for var in all_vars:
                # Check the variable's own capability sources.
                cv = store.get(var)
                if cv is not None:
                    for src in cv.sources:
                        if src not in self._trusted_sources:
                            pair = (var, src)
                            if pair not in seen:
                                seen.add(pair)
                                dep_chain.append(pair)
                # Walk the upstream dependency graph.
                try:
                    dg = interpreter.get_dependency_graph(var)
                    for upstream_var in dg.all_upstream:
                        upstream_cv = store.get(upstream_var)
                        if upstream_cv is not None:
                            for src in upstream_cv.sources:
                                if src not in self._trusted_sources:
                                    pair = (upstream_var, src)
                                    if pair not in seen:
                                        seen.add(pair)
                                        dep_chain.append(pair)
                except Exception:  # noqa: BLE001
                    pass
            # Cap at 50 entries.
            dep_chain = dep_chain[:50]
            return bool(dep_chain), dep_chain

        # Fallback: flat store-level scan (original behaviour).
        fallback_chain: list[tuple[str, str]] = []
        for var, cv in store.items():
            for source in cv.sources:
                if source not in self._trusted_sources:
                    fallback_chain.append((var, source))
        if fallback_chain:
            return True, fallback_chain[:50]
        return False, []

    def _store_has_untrusted(self, store: dict[str, CaMeLValue]) -> bool:
        """Return True if any value in *store* has at least one untrusted source."""
        for cv in store.values():
            for source in cv.sources:
                if source not in self._trusted_sources:
                    return True
        return False

    def _emit_audit_event(
        self,
        exc: BaseException,
        redacted: RedactedError,
        dependency_chain: list[tuple[str, str]],
        redaction_reason: str,
        orig_message_len: int,
        m4_f9_applied: bool,
    ) -> None:
        """Emit a :class:`RedactionAuditEvent` to the configured audit log.

        No-op when ``self._audit_log`` is ``None``.

        Parameters
        ----------
        exc:
            Original exception (used only for ``error_type``).
        redacted:
            The :class:`RedactedError` produced for this classification.
        dependency_chain:
            Taint chain; ``[]`` for trusted or NEIE cases.
        redaction_reason:
            Reason code string for the event.
        orig_message_len:
            Character length of the original message before redaction.
        m4_f9_applied:
            Whether M4-F9 loop propagation was applied.
        """
        if self._audit_log is None:
            return
        event = RedactionAuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            line_number=redacted.lineno,
            redaction_reason=redaction_reason,
            dependency_chain=list(dependency_chain),
            trust_level=redacted.trust_level,
            error_type=type(exc).__name__,
            redacted_message_length=orig_message_len,
            m4_f9_applied=m4_f9_applied,
        )
        self._audit_log.append(event)


# ---------------------------------------------------------------------------
# RetryPromptBuilder (M2-F14)
# ---------------------------------------------------------------------------


class RetryPromptBuilder:
    """Builds the P-LLM user-turn message for partial re-execution retries (M2-F14).

    Constructs a prompt that communicates:

    - The variable names already defined in the interpreter (accepted state).
    - The redacted error that caused the failure.
    - An instruction to regenerate **only** the remaining steps, not the
      already-executed ones.

    The prompt is appended to the existing P-LLM conversation as a new
    ``user`` turn; the orchestrator preserves the full conversation history
    across retries.
    """

    def build(
        self,
        accepted_state: AcceptedState,
        error: RedactedError,
        tool_signatures: list[ToolSignature],
        user_context: UserContext | None = None,
    ) -> str:
        """Construct a partial re-execution retry prompt.

        The returned string is inserted as a ``user``-role message appended
        to the ongoing P-LLM conversation.  It contains:

        1. A list of already-defined variable names (no values).
        2. The redacted error details (type, lineno, message if trusted).
        3. An explicit instruction to regenerate only the remaining code.

        Parameters
        ----------
        accepted_state:
            Snapshot of the successfully executed interpreter state at the
            point of failure, including variable names and remaining source.
        error:
            Sanitised exception representation produced by
            :class:`ExceptionRedactor`.
        tool_signatures:
            Registered tool signatures, included in the retry prompt so the
            P-LLM can reference the available tools when regenerating.
        user_context:
            Optional trusted platform metadata forwarded verbatim.

        Returns
        -------
        str
            User-turn message string to append to the P-LLM conversation.
        """
        # Build the error section.
        lineno_part = (
            f"line {error.lineno}" if error.lineno is not None else "unknown location"
        )
        if error.message is not None:
            error_detail = (
                f"Error type: {error.error_type}\n"
                f"Location:   {lineno_part}\n"
                f"Message:    {error.message}"
            )
        elif error.trust_level == "not_enough_information":
            # M4-F7: NEIE advisory — no untrusted data, only fixed text and lineno.
            advisory = (
                f"The call to query_quarantined_llm() at {lineno_part} reported "
                "that the provided context did not contain enough information "
                "to populate the requested schema. Consider restructuring the "
                "query or passing additional context variables."
            )
            error_detail = (
                f"Error type: {error.error_type}\n"
                f"Location:   {lineno_part}\n"
                f"Advisory:   {advisory}"
            )
        else:
            error_detail = (
                f"Error type: {error.error_type}\n"
                f"Location:   {lineno_part}"
            )

        # Build the accepted-state section (names only, no values).
        if accepted_state.variable_names:
            names_str = ", ".join(sorted(accepted_state.variable_names))
            state_section = (
                f"The following variables are already defined in the interpreter "
                f"(treat them as opaque handles — do NOT read their values):\n"
                f"  {names_str}"
            )
        else:
            state_section = (
                "No variables have been defined yet "
                "(the failure occurred on the first statement)."
            )

        # Build remaining-code section.
        if accepted_state.remaining_source.strip():
            remaining_section = textwrap.dedent(f"""\
                The remaining unexecuted code from the previous plan was:

                ```python
                {accepted_state.remaining_source}
                ```

                Regenerate ONLY the above remaining code section, correcting \
the error.
                Do NOT re-emit the already-executed statements.""")
        else:
            remaining_section = (
                "The failure occurred after the plan completed — "
                "regenerate the full plan."
            )

        return textwrap.dedent(f"""\
            The previous execution attempt produced an error.

            --- ACCEPTED STATE ---
            {state_section}
            Statements executed successfully: {accepted_state.executed_statement_count}

            --- ERROR ---
            {error_detail}

            --- INSTRUCTION ---
            {remaining_section}

            Respond with exactly ONE fenced ```python``` code block containing \
the corrected plan.
            Do NOT include any explanation outside the code block.""")


# ---------------------------------------------------------------------------
# TraceRecorder (M2-F12)
# ---------------------------------------------------------------------------


@dataclass
class TraceRecorder:
    """Records successful tool-call events into an :data:`ExecutionTrace` (M2-F12).

    The :class:`CaMeLOrchestrator` uses :meth:`wrap_tools` to inject
    tracing closures around each registered tool callable before handing
    the tools dict to :class:`~camel.CaMeLInterpreter`.  No changes to the
    interpreter interface are required.

    After execution the accumulated trace is retrieved via :attr:`trace`.
    """

    _trace: list[TraceRecord] = field(default_factory=list)
    _interpreter_ref: CaMeLInterpreter | None = field(default=None, repr=False)

    @property
    def trace(self) -> ExecutionTrace:
        """Return the accumulated execution trace (ordered list of records).

        Returns
        -------
        ExecutionTrace
            A copy of the internal trace list.  Mutations do not affect the
            recorder's internal state.
        """
        return list(self._trace)

    def wrap_tools(
        self,
        tools: dict[str, Any],
        interpreter: CaMeLInterpreter,
    ) -> dict[str, Any]:
        """Wrap each tool callable with a tracing closure.

        For each ``(name, fn)`` pair in *tools*, returns a new callable that:

        1. Calls the original ``fn`` with the provided arguments.
        2. On success, appends a :class:`TraceRecord` to the internal trace.
        3. On exception, re-raises without appending (failed calls are not
           recorded in the trace).

        Parameters
        ----------
        tools:
            Dict of tool callables as accepted by
            :class:`~camel.CaMeLInterpreter`.
        interpreter:
            The interpreter whose store will be snapshotted after each
            successful tool call.

        Returns
        -------
        dict[str, Any]
            New dict with the same keys; values are tracing-wrapped callables.
        """
        self._interpreter_ref = interpreter
        wrapped: dict[str, Any] = {}
        for name, fn in tools.items():
            wrapped[name] = self._make_traced_tool(name, fn)
        return wrapped

    def _make_traced_tool(
        self,
        tool_name: str,
        fn: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Return a tracing wrapper for a single tool callable.

        Parameters
        ----------
        tool_name:
            The registered name of the tool.
        fn:
            The original tool callable to wrap.

        Returns
        -------
        Callable[..., Any]
            A wrapped callable that records a :class:`TraceRecord` on success.
        """
        recorder = self

        def traced(*args: Any, **kwargs: Any) -> Any:
            """Traced wrapper — records on success, re-raises on failure."""
            # Snapshot raw arg values for the trace record (strip CaMeLValue
            # wrappers so the trace stays serialisable).
            raw_args: dict[str, Any] = {}
            for i, a in enumerate(args):
                raw_args[f"arg{i}"] = a.raw if isinstance(a, CaMeLValue) else a
            for k, v in kwargs.items():
                raw_args[k] = v.raw if isinstance(v, CaMeLValue) else v

            result = fn(*args, **kwargs)

            # Snapshot interpreter store after successful call.
            mem: dict[str, CaMeLValue] = {}
            if recorder._interpreter_ref is not None:
                mem = recorder._interpreter_ref.store

            recorder._trace.append(
                TraceRecord(
                    tool_name=tool_name,
                    args=raw_args,
                    memory_snapshot=mem,
                )
            )
            return result

        return traced

    def reset(self) -> None:
        """Clear the accumulated trace, resetting to an empty list.

        Called by :class:`CaMeLOrchestrator` at the start of each outer-loop
        iteration to avoid accumulating records from failed attempts.
        """
        self._trace.clear()


# ---------------------------------------------------------------------------
# CaMeLOrchestrator
# ---------------------------------------------------------------------------


class CaMeLOrchestrator:
    """Wires P-LLM, CaMeLInterpreter, Q-LLM, and tool dispatch into a complete
    CaMeL execution loop.

    The orchestrator implements the state machine described in
    ``docs/adr/007-execution-loop-orchestrator.md``:

    ``INITIAL → PLAN_GENERATION → EXECUTING → SUCCEEDED``
    ``EXECUTING → EXCEPTION_RECEIVED → REDACTING → RETRY_PROMPT_BUILD``
    ``→ PLAN_REGENERATION → EXECUTING  (up to max_loop_retries)``
    ``REDACTING → FAILED  (when max_loop_retries exceeded)``

    Parameters
    ----------
    p_llm:
        Configured :class:`~camel.llm.PLLMWrapper` for plan generation and
        re-generation.
    interpreter:
        A :class:`~camel.CaMeLInterpreter` instance pre-loaded with all
        tool callables and an optional policy engine.  The orchestrator
        wraps its tools dict with :class:`TraceRecorder` before each run.
    tool_signatures:
        Ordered list of :class:`~camel.llm.ToolSignature` objects injected
        into the P-LLM system prompt.
    display_channel:
        :class:`DisplayChannel` implementation receiving all ``print()``
        output from execution plans (M2-F10).  Defaults to
        :class:`StdoutDisplayChannel`.
    max_loop_retries:
        Maximum outer-loop retry count (M2-F8).  Default: 10.

    Examples
    --------
    .. code-block:: python

        from camel.execution_loop import CaMeLOrchestrator
        from camel.llm import PLLMWrapper, ToolSignature
        from camel import CaMeLInterpreter
        from camel.llm.backend import get_backend

        backend = get_backend("claude", api_key="sk-...")
        p_llm = PLLMWrapper(backend)

        tools = {"get_email": get_email_tool, "send_email": send_email_tool}
        interp = CaMeLInterpreter(tools=tools)

        sigs = [
            ToolSignature("get_email", "", "EmailMessage", "Fetch the latest email."),
            ToolSignature("send_email", "to: str, body: str", "None", "Send an email."),
        ]

        orchestrator = CaMeLOrchestrator(p_llm=p_llm, interpreter=interp,
                                          tool_signatures=sigs)

        import asyncio
        result = asyncio.run(orchestrator.run("Forward the last email to alice@example.com"))
        print(result.trace)
    """

    def __init__(
        self,
        p_llm: PLLMWrapper,
        interpreter: CaMeLInterpreter,
        tool_signatures: list[ToolSignature],
        display_channel: DisplayChannel | None = None,
        max_loop_retries: int = 10,
    ) -> None:
        """Initialise the orchestrator with all required components."""
        self._p_llm = p_llm
        self._interpreter = interpreter
        self._tool_signatures = tool_signatures
        self._display_channel: DisplayChannel = (
            display_channel if display_channel is not None else StdoutDisplayChannel()
        )
        self._max_loop_retries = max_loop_retries

        # M4-F17: audit log for redaction events; exposed as a public property.
        self._redaction_audit_log: list[RedactionAuditEvent] = []
        self._redactor = ExceptionRedactor(audit_log=self._redaction_audit_log)
        self._retry_builder = RetryPromptBuilder()
        self._trace_recorder = TraceRecorder()

        # print() outputs collected during execution (M2-F10).
        self._print_outputs: list[CaMeLValue] = []

        # Register the custom print builtin in the interpreter (M2-F10).
        print_builtin = self._make_print_builtin()
        self._interpreter._builtins["print"] = print_builtin

        # Wrap interpreter tools with tracing closures (M2-F12).
        wrapped_tools = self._trace_recorder.wrap_tools(
            dict(self._interpreter._tools),
            self._interpreter,
        )
        self._interpreter._tools = wrapped_tools

    async def run(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> ExecutionResult:
        """Execute *user_query* through the full CaMeL pipeline.

        Implements the orchestrator state machine:

        1. Calls P-LLM to generate an initial :class:`~camel.llm.CodePlan`.
        2. Executes the plan statement-by-statement via the interpreter.
        3. On exception: redacts the error, captures accepted state, builds a
           retry prompt, increments the loop counter, and re-generates a plan
           for the remaining code.
        4. On success: returns an :class:`ExecutionResult` with the full trace,
           print outputs, and final store.

        Parameters
        ----------
        user_query:
            Trusted user request string.  Must not contain
            :class:`~camel.CaMeLValue` instances
            (:class:`~camel.llm.PLLMIsolationError` is raised by the P-LLM
            wrapper if this constraint is violated).
        user_context:
            Optional trusted key-value metadata forwarded to the P-LLM
            system prompt.

        Returns
        -------
        ExecutionResult
            Contains the execution trace, print outputs, final variable store,
            and outer-loop attempt count.

        Raises
        ------
        MaxRetriesExceededError
            When ``max_loop_retries`` outer-loop retries are exhausted without
            a successful plan completion (M2-F8).
        ~camel.llm.PLLMRetryExhaustedError
            When the P-LLM inner retry loop exhausts on plan generation or
            re-generation.
        ~camel.llm.PLLMIsolationError
            If a :class:`~camel.CaMeLValue` is detected in *user_query* or
            *user_context*.
        """
        self._print_outputs = []

        # Generate the initial plan.
        plan = await self._p_llm.generate_plan(
            user_query=user_query,
            tool_signatures=self._tool_signatures,
            user_context=user_context,
        )

        last_error: RedactedError | None = None
        plan_source = plan.source

        for attempt in range(self._max_loop_retries):
            # Reset trace for this attempt (don't accumulate from failed runs).
            self._trace_recorder.reset()

            store_snapshot_before: dict[str, CaMeLValue] = {}
            try:
                executed_count, remaining_source = self._exec_plan_statements(
                    plan_source
                )
                # Success path — all statements executed without exception.
                return ExecutionResult(
                    trace=self._trace_recorder.trace,
                    print_outputs=list(self._print_outputs),
                    final_store=self._interpreter.store,
                    loop_attempts=attempt,
                )
            except Exception as exc:
                # Snapshot the store at the point of failure for redaction.
                store_snapshot_before = self._interpreter.store
                last_error = self._redactor.classify(
                    exc, store_snapshot_before, self._interpreter
                )

                # Determine where execution failed and build accepted state.
                executed_count_on_error: int = getattr(exc, "_camel_executed_count", 0)
                remaining_on_error: str = getattr(exc, "_camel_remaining_source", "")

                accepted = self._build_accepted_state(
                    executed_count_on_error,
                    remaining_on_error,
                    exc,
                )

                # If this was the last attempt, break out to raise the error.
                if attempt >= self._max_loop_retries - 1:
                    break

                # Build retry prompt and regenerate plan for remaining code.
                retry_prompt = self._retry_builder.build(
                    accepted_state=accepted,
                    error=last_error,
                    tool_signatures=self._tool_signatures,
                    user_context=user_context,
                )

                new_plan = await self._p_llm.generate_plan(
                    user_query=retry_prompt,
                    tool_signatures=self._tool_signatures,
                    user_context=user_context,
                )
                plan_source = new_plan.source

                # M4-F8: restore STRICT mode dep-state when NEIE triggered
                # the retry so the regenerated plan inherits the correct
                # dependency context.
                if (
                    last_error.trust_level == "not_enough_information"
                    and accepted.dependency_graph_snapshot is not None
                    and accepted.dep_ctx_stack_snapshot is not None
                ):
                    self._interpreter.restore_dep_state(
                        accepted.dependency_graph_snapshot,
                        accepted.dep_ctx_stack_snapshot,
                    )
                # M4-F9: pre-seed the dep-ctx stack when the exception
                # originated in a for-loop body with a non-public iterable.
                elif (
                    accepted.loop_iter_deps is not None
                    and accepted.loop_iter_deps
                ):
                    # Merge the loop iterable's dep context into the bottom
                    # stack frame so the regenerated plan's first statement
                    # already carries the iterable's taint.
                    self._interpreter._dep_ctx_stack[0] = (
                        self._interpreter._dep_ctx_stack[0]
                        | accepted.loop_iter_deps
                    )

        raise MaxRetriesExceededError(
            attempts=self._max_loop_retries,
            last_error=last_error,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _exec_plan_statements(
        self,
        plan_source: str,
    ) -> tuple[int, str | None]:
        """Execute *plan_source* statement by statement; return progress info.

        Parses *plan_source* into individual top-level AST statements and
        executes each one via ``self._interpreter.exec(ast.unparse(stmt))``.

        Parameters
        ----------
        plan_source:
            The full plan source string (previously extracted from a
            :class:`~camel.llm.CodePlan`).

        Returns
        -------
        tuple[int, str | None]
            A pair ``(executed_count, remaining_source)`` where:

            - ``executed_count`` is the number of statements successfully
              executed before any exception (0 if the first statement fails).
            - ``remaining_source`` is the ``ast.unparse()`` reconstruction of
              all unexecuted statements joined by newlines, or ``None`` if all
              statements completed without exception.

        Raises
        ------
        Any exception raised by the interpreter (re-raised after the
        orchestrator captures the store snapshot for redaction).
        """
        module = ast.parse(plan_source, mode="exec")
        stmts = module.body

        for i, stmt in enumerate(stmts):
            stmt_source = ast.unparse(stmt)
            try:
                self._interpreter.exec(stmt_source)
            except Exception as exc:
                # Compute the remaining (unexecuted) statements.
                remaining_stmts = stmts[i:]
                remaining_source = "\n".join(ast.unparse(s) for s in remaining_stmts)
                # Attach progress metadata to the exception for the caller.
                exc._camel_executed_count = i  # type: ignore[attr-defined]
                exc._camel_remaining_source = remaining_source  # type: ignore[attr-defined]
                raise

        return len(stmts), None

    def _build_accepted_state(
        self,
        executed_count: int,
        remaining_source: str,
        exc: BaseException | None = None,
    ) -> AcceptedState:
        """Construct an :class:`AcceptedState` from the current interpreter store.

        Parameters
        ----------
        executed_count:
            Number of statements that executed successfully.
        remaining_source:
            Unexecuted source text from the failure point.
        exc:
            The exception that caused failure, if any.  Used to capture
            M4-F8 (NEIE) and M4-F9 (loop-body exception) metadata.

        Returns
        -------
        AcceptedState
            Snapshot with variable names, optional dependency-state snapshot
            (M4-F8), and optional loop-iter context (M4-F9).
        """
        from camel.exceptions import NotEnoughInformationError as CamelNEIE  # noqa: PLC0415

        dep_graph_snapshot: dict[str, frozenset[str]] | None = None
        dep_ctx_stack_snapshot: tuple[frozenset[str], ...] | None = None
        loop_iter_deps: frozenset[str] | None = None
        loop_iter_caps: CaMeLValue | None = None

        if exc is not None:
            # M4-F8: snapshot dep state for NEIE re-generation cycles.
            if isinstance(exc, (CamelNEIE, LLMNotEnoughInfoError)):
                snap_graph, snap_stack = self._interpreter.snapshot_dep_state()
                dep_graph_snapshot = snap_graph
                dep_ctx_stack_snapshot = snap_stack

            # M4-F9: capture loop-iterable dep context for loop-body exceptions.
            loop_iter_deps = getattr(exc, "__loop_iter_deps__", None)
            loop_iter_caps = getattr(exc, "__loop_iter_caps__", None)

        return AcceptedState(
            variable_names=frozenset(self._interpreter.store.keys()),
            executed_statement_count=executed_count,
            remaining_source=remaining_source,
            dependency_graph_snapshot=dep_graph_snapshot,
            dep_ctx_stack_snapshot=dep_ctx_stack_snapshot,
            loop_iter_deps=loop_iter_deps,
            loop_iter_caps=loop_iter_caps,
        )

    def _make_print_builtin(self) -> Callable[..., CaMeLValue]:
        """Return a callable suitable for use as the ``print`` builtin.

        The returned callable:

        1. Receives a :class:`~camel.CaMeLValue` argument.
        2. Calls ``self._display_channel.write(value)``.
        3. Returns a :class:`~camel.CaMeLValue` wrapping ``None`` with
           ``sources=frozenset({"CaMeL"})``.

        The ``print`` callable is registered in the interpreter's ``builtins``
        dict at orchestrator construction time (M2-F10).

        Returns
        -------
        Callable[..., CaMeLValue]
            Callable with signature ``(value: CaMeLValue) -> CaMeLValue``.
        """
        orchestrator = self

        def print_builtin(*args: Any, **_kwargs: Any) -> CaMeLValue:
            """Route print() output to the display channel (M2-F10)."""
            # Accept both raw values and CaMeLValue instances.
            for arg in args:
                if isinstance(arg, CaMeLValue):
                    cv = arg
                else:
                    cv = wrap(arg, sources=frozenset({"CaMeL"}))
                orchestrator._display_channel.write(cv)
                orchestrator._print_outputs.append(cv)
            return wrap(None, sources=frozenset({"CaMeL"}))

        return print_builtin

    def get_trace(self) -> ExecutionTrace:
        """Return the current execution trace (M2-F12).

        Returns
        -------
        ExecutionTrace
            Ordered list of :class:`TraceRecord` instances appended after
            each successful tool call in the most recent :meth:`run` call.
        """
        return self._trace_recorder.trace

    @property
    def redaction_audit_log(self) -> list[RedactionAuditEvent]:
        """Return the accumulated redaction audit log (M4-F17).

        Contains one :class:`RedactionAuditEvent` for every exception
        classification made by the :class:`ExceptionRedactor` during all
        :meth:`run` calls on this orchestrator instance.

        Returns
        -------
        list[RedactionAuditEvent]
            Snapshot of the internal audit log.
        """
        return list(self._redaction_audit_log)

    @property
    def strict_dep_audit_log(self) -> list[Any]:
        """Return the per-statement STRICT mode dependency addition log (M4-F18).

        Delegates to :attr:`CaMeLInterpreter.strict_dep_audit_log`.  Contains
        one :class:`~camel.interpreter.StrictDependencyAdditionEvent` for every
        unique ``(statement_lineno, assigned_variable)`` pair per :meth:`run`
        call where STRICT mode added context-flow dependencies beyond direct
        data-flow tracking.

        Returns
        -------
        list[StrictDependencyAdditionEvent]
            Snapshot of the interpreter's STRICT dependency audit log.
        """
        return self._interpreter.strict_dep_audit_log
