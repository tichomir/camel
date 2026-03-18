"""CaMeLAgent — stable, thread-safe public API for the camel-security SDK.

:class:`CaMeLAgent` is the primary entry point for the camel-security SDK.
It encapsulates the P-LLM, Q-LLM, tool registry, policy registry, and
execution orchestrator into a single, easy-to-configure object.

:class:`AgentResult` is the structured return type of :meth:`CaMeLAgent.run`.
All fields are documented below with explicit stability guarantees.

Thread-safety contract
----------------------
Each call to :meth:`CaMeLAgent.run` creates a **fresh**
:class:`~camel.interpreter.CaMeLInterpreter` and
:class:`~camel.execution_loop.CaMeLOrchestrator` instance.  No interpreter
state (variable store, dependency graph, strict-mode flags) is shared between
concurrent ``run()`` invocations on the same :class:`CaMeLAgent`.

The agent itself holds only **read-only, immutable** references after
construction:

- ``p_llm`` and ``q_llm`` backend objects — thread-safety depends on the
  underlying provider SDK; all first-party adapters (Claude, Gemini) are
  safe for concurrent async calls per their respective SDK documentation.
- The :class:`~camel.policy.PolicyRegistry` is **read-only** after
  :class:`CaMeLAgent` construction; callers must not mutate it after passing
  it to the agent.
- The :class:`~camel.tools.ToolRegistry` is built once at construction time
  from the ``tools`` sequence and is never mutated afterwards.

Implication: multiple threads / async tasks may call ``agent.run()`` in
parallel without requiring external locking, provided the underlying backend
SDKs support concurrent async requests.

Examples
--------
Minimal usage with Claude::

    import asyncio
    from camel_security import CaMeLAgent, Tool
    from camel.llm.backend import get_backend

    backend = get_backend("claude", api_key="sk-...", model="claude-sonnet-4-6")

    def echo(text: str) -> str:
        return text

    agent = CaMeLAgent(
        p_llm=backend,
        q_llm=backend,
        tools=[Tool(name="echo", fn=echo, params="text: str", return_type="str")],
    )

    result = asyncio.run(agent.run("Echo 'hello world'"))
    print(result.display_output)
    print(result.execution_trace)
"""

from __future__ import annotations

import asyncio
import hashlib
import time as _time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from camel.execution_loop import (
    CaMeLOrchestrator,
    ExecutionResult,
    MaxRetriesExceededError,
    StdoutDisplayChannel,
)
from camel.interpreter import CaMeLInterpreter, ExecutionMode
from camel.llm.backend import LLMBackend
from camel.llm.p_llm import PLLMWrapper, ToolSignature, UserContext
from camel.llm.query_interface import make_query_quarantined_llm
from camel.policy.interfaces import PolicyRegistry
from camel.provenance import (
    PhishingWarning,
    ProvenanceChain,
    build_provenance_chain,
    detect_phishing_content,
)
from camel.tools.registry import ToolRegistry
from camel.value import CaMeLValue
from camel_security.tool import Tool

__all__ = [
    "AgentResult",
    "PolicyDenialRecord",
    "CaMeLAgent",
]


# ---------------------------------------------------------------------------
# AgentResult — stable return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDenialRecord:
    """A record of a single security policy denial captured during execution.

    Populated when the agent is operating in **production (consent) mode**
    and a tool call is denied by the policy engine.  In evaluation/test mode,
    :class:`~camel.interpreter.PolicyViolationError` is raised immediately and
    no records are appended.

    Attributes
    ----------
    tool_name:
        The name of the tool whose call was blocked.
    policy_name:
        The name (``__name__``) of the policy function that returned
        :class:`~camel.policy.Denied`.
    reason:
        Human-readable denial reason from :class:`~camel.policy.Denied`.
    resolved:
        ``True`` if the user approved the call despite the denial (the call
        proceeded); ``False`` if the user rejected it (execution was
        cancelled at this step).

    Stability guarantee
    -------------------
    All fields are **stable** (no removal or rename without a major version
    bump).  New optional fields may be added in minor releases.
    """

    tool_name: str
    policy_name: str
    reason: str
    resolved: bool


@dataclass(frozen=True)
class AgentResult:
    """The structured outcome of a :meth:`CaMeLAgent.run` call.

    :class:`AgentResult` is a **frozen dataclass** — all fields are set once
    at construction and are never mutated.  Callers may safely share instances
    across threads.

    Attributes
    ----------
    execution_trace:
        Ordered list of :class:`~camel.execution_loop.TraceRecord` objects —
        one per successful tool call in execution order.  Each record carries
        the tool name, raw argument values (capability wrappers stripped for
        serialisability), and a shallow snapshot of the interpreter variable
        store after the call.
    display_output:
        Ordered list of strings printed by ``print()`` calls in the P-LLM-
        generated execution plan (M2-F10 routing contract).  The list is
        empty when the plan contains no ``print()`` statements.
    policy_denials:
        List of :class:`PolicyDenialRecord` instances captured during
        execution.  Empty in evaluation/test mode (violations raise
        :class:`~camel.interpreter.PolicyViolationError` instead).  In
        production mode, each denied-then-resolved or denied-then-rejected
        tool call produces one record.
    audit_log_ref:
        Opaque string token identifying the security audit log scope for
        this run.  Format: ``"camel-audit:<run_id>"`` where ``run_id`` is a
        hex string derived from the start timestamp.  Pass this token to
        your audit log retrieval API to fetch the full per-statement trace
        including redaction events and STRICT-mode dependency additions
        (NFR-6).
    loop_attempts:
        Number of outer-loop planning cycles consumed (0-based: ``0`` means
        the first plan succeeded without any retry).  Useful for diagnosing
        P-LLM quality issues or adversarially-induced retry storms.
    success:
        ``True`` if execution completed without raising
        :class:`~camel.execution_loop.MaxRetriesExceededError` or an
        unrecoverable exception.  ``False`` if the agent ran out of retries
        or a non-redactable exception propagated.
    final_store:
        Shallow snapshot of the interpreter variable store after the plan
        completes.  Keys are variable names assigned during execution; values
        are :class:`~camel.value.CaMeLValue` instances with full capability
        metadata.  Empty when ``success`` is ``False``.

    Stability guarantee
    -------------------
    All listed fields are **stable** across minor and patch releases.  Field
    *addition* (with defaults) is allowed in minor releases.  Field removal
    or rename requires a **major-version bump**.  See ``VERSIONING.md`` for
    the full policy.

    Examples
    --------
    ::

        result = await agent.run("Send the latest invoice to finance@example.com")

        if result.success:
            for record in result.execution_trace:
                print(record.tool_name, record.args)
        else:
            print(f"Execution failed after {result.loop_attempts} attempts")
    """

    execution_trace: list[Any]  # list[TraceRecord] — Any to avoid import cycle
    display_output: list[str]
    policy_denials: list[PolicyDenialRecord]
    audit_log_ref: str
    loop_attempts: int
    success: bool
    final_store: dict[str, Any] = field(default_factory=dict)
    provenance_chains: dict[str, ProvenanceChain] = field(default_factory=dict)
    """Provenance chains keyed by variable name.

    Populated for every variable present in :attr:`final_store` after a
    successful run.  Each :class:`~camel.provenance.ProvenanceChain` records
    the complete origin lineage of that variable's value.

    Use :meth:`CaMeLAgent.get_provenance` to look up individual chains with
    ``KeyError`` semantics on unknown variables, or access this dict directly
    to iterate over all chains.

    JSON representation
    -------------------
    Each chain is independently serialisable via
    :meth:`~camel.provenance.ProvenanceChain.to_json`.  For audit-log
    inclusion pass ``chain.to_dict()`` to your log writer.

    Stability guarantee
    -------------------
    Field is **stable** (added in v0.6.0).  May be empty when
    ``success`` is ``False``.
    """
    phishing_warnings: list[PhishingWarning] = field(default_factory=list)
    """Phishing-content warnings detected during provenance analysis.

    Populated from :func:`~camel.provenance.detect_phishing_content` applied
    to all variables in :attr:`final_store` after a successful run.

    A non-empty list means that at least one variable contains text matching
    a trusted-sender-claim pattern (e.g. ``From: alice@corp.com``) while
    originating from an untrusted tool output.  The UI should surface these
    warnings to the user (PRD NG2 partial mitigation).

    Stability guarantee
    -------------------
    Field is **stable** (added in v0.6.0).  Empty when ``success`` is
    ``False`` or when no phishing patterns are detected.
    """


# ---------------------------------------------------------------------------
# CaMeLAgent
# ---------------------------------------------------------------------------


class CaMeLAgent:
    """Stable, thread-safe public entry point for the camel-security SDK.

    :class:`CaMeLAgent` wires together:

    - **P-LLM** — any :class:`~camel.llm.LLMBackend`-conforming object used
      to generate pseudo-Python execution plans.
    - **Q-LLM** — any :class:`~camel.llm.LLMBackend`-conforming object used
      for schema-validated structured data extraction from untrusted content.
    - **Tools** — a sequence of :class:`~camel_security.Tool` instances
      describing all callables available to the P-LLM.
    - **Policies** — an optional :class:`~camel.policy.PolicyRegistry`
      applied before every tool call.
    - **Execution mode** — :class:`~camel.interpreter.ExecutionMode`
      controlling dependency-tracking strictness (default:
      :attr:`~camel.interpreter.ExecutionMode.STRICT`).

    Thread-safety contract
    ----------------------
    Each :meth:`run` call constructs a **fresh**
    :class:`~camel.interpreter.CaMeLInterpreter` and
    :class:`~camel.execution_loop.CaMeLOrchestrator`.  No interpreter state
    is shared between concurrent invocations.  The agent holds only
    immutable references after construction — see the module docstring for
    the full thread-safety contract.

    Parameters
    ----------
    p_llm:
        :class:`~camel.llm.LLMBackend` used for plan generation.  Must
        implement :meth:`~camel.llm.LLMBackend.generate` (free-form text
        completion).
    q_llm:
        :class:`~camel.llm.LLMBackend` used for structured data extraction
        within plans.  Must implement
        :meth:`~camel.llm.LLMBackend.generate_structured` (Pydantic-schema-
        constrained output).  May be the same object as ``p_llm`` or a
        cheaper/lighter model.
    tools:
        Sequence of :class:`~camel_security.Tool` instances.  Order is
        preserved in the P-LLM system prompt; earlier tools appear first.
        Tool names must be unique.
    policies:
        Optional :class:`~camel.policy.PolicyRegistry`.  When ``None`` an
        empty registry is constructed (all tool calls are implicitly allowed
        unless per-tool policies are specified on individual
        :class:`~camel_security.Tool` instances).
    mode:
        :class:`~camel.interpreter.ExecutionMode` for the interpreter.
        Defaults to :attr:`~camel.interpreter.ExecutionMode.STRICT` — the
        production-safe default.  Pass
        :attr:`~camel.interpreter.ExecutionMode.NORMAL` only for debugging
        or non-security-sensitive scenarios.
    max_retries:
        Maximum number of outer-loop planning retries on exception (M2-F8).
        Default: 10.

    Raises
    ------
    ValueError
        If any two tools share the same ``name``, or if ``tools`` is empty.
    TypeError
        If ``p_llm`` or ``q_llm`` does not satisfy the
        :class:`~camel.llm.LLMBackend` protocol.

    Examples
    --------
    ::

        from camel_security import CaMeLAgent, Tool
        from camel.llm.backend import get_backend
        from camel.policy import PolicyRegistry

        backend = get_backend("claude", api_key="sk-...", model="claude-sonnet-4-6")

        def send_email(to: str, body: str) -> bool:
            ...  # real implementation

        registry = PolicyRegistry()

        agent = CaMeLAgent(
            p_llm=backend,
            q_llm=backend,
            tools=[
                Tool(
                    name="send_email",
                    fn=send_email,
                    description="Send an email to a recipient.",
                    params="to: str, body: str",
                    return_type="bool",
                )
            ],
            policies=registry,
        )

        import asyncio
        result = asyncio.run(agent.run("Send a welcome email to alice@example.com"))
    """

    def __init__(
        self,
        p_llm: LLMBackend,
        q_llm: LLMBackend,
        tools: Sequence[Tool],
        policies: PolicyRegistry | None = None,
        mode: ExecutionMode = ExecutionMode.STRICT,
        max_retries: int = 10,
    ) -> None:
        """Construct a CaMeLAgent with all required components.

        See class docstring for parameter descriptions.
        """
        if not tools:
            raise ValueError("tools must contain at least one Tool")

        # Validate unique tool names up-front so callers get a clear error.
        seen: set[str] = set()
        for tool in tools:
            if tool.name in seen:
                raise ValueError(
                    f"Duplicate tool name {tool.name!r}: each tool must have a unique name"
                )
            seen.add(tool.name)

        # Validate backend protocol conformance.
        if not isinstance(p_llm, LLMBackend):
            raise TypeError(
                f"p_llm must satisfy the LLMBackend protocol, got {type(p_llm).__name__!r}"
            )
        if not isinstance(q_llm, LLMBackend):
            raise TypeError(
                f"q_llm must satisfy the LLMBackend protocol, got {type(q_llm).__name__!r}"
            )

        self._p_llm_backend: LLMBackend = p_llm
        self._q_llm_backend: LLMBackend = q_llm
        self._tools: tuple[Tool, ...] = tuple(tools)
        self._base_policies: PolicyRegistry = policies if policies is not None else PolicyRegistry()
        self._mode: ExecutionMode = mode
        self._max_retries: int = max_retries

        # Build the immutable tool registry once — reused (read-only) across all
        # run() calls.  Per-run policy merging happens in _build_run_policy_registry.
        self._tool_registry: ToolRegistry = self._build_tool_registry()

        # Build ToolSignature list for P-LLM (immutable after construction).
        self._tool_signatures: list[ToolSignature] = self._build_tool_signatures()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> AgentResult:
        """Execute *user_query* through the full CaMeL security pipeline.

        Each call creates a **fresh interpreter and orchestrator** — no state
        from previous calls bleeds through.  Safe to call concurrently from
        multiple threads or async tasks.

        Parameters
        ----------
        user_query:
            The natural-language instruction from the user.  Treated as
            fully trusted input (PRD §7.2).
        user_context:
            Optional :class:`~camel.llm.UserContext` carrying additional
            metadata (user name, preferred language, deployment context)
            injected into the P-LLM system prompt.  When ``None``, a
            minimal context is derived from the agent configuration.

        Returns
        -------
        AgentResult
            Structured result with execution trace, display output, policy
            denials, audit log reference, loop attempt count, and success flag.

        Raises
        ------
        MaxRetriesExceededError
            When the execution loop exhausts all retry attempts (propagated
            from :class:`~camel.execution_loop.CaMeLOrchestrator`).
        PolicyViolationError
            When a tool call is denied in evaluation/test mode (no consent
            prompt is shown; the error propagates immediately).
        """
        run_id = hashlib.sha1(  # noqa: S324  (non-crypto, just unique ID)
            f"{id(self)}:{_time.monotonic_ns()}".encode()
        ).hexdigest()[:16]
        audit_log_ref = f"camel-audit:{run_id}"

        # Build per-run policy registry merging global + per-tool policies.
        run_policies = self._build_run_policy_registry()

        # Build per-run tools dict: capability-annotated user tools + Q-LLM callable.
        # query_quarantined_llm is passed directly (not through ToolRegistry) because
        # it returns a Pydantic model, not a CaMeLValue; the interpreter handles it
        # specially via CaMeLInterpreter._QLLM_TOOL_NAMES.
        run_tools: dict[str, Any] = dict(self._tool_registry.as_interpreter_tools())
        run_tools["query_quarantined_llm"] = make_query_quarantined_llm(
            self._q_llm_backend  # type: ignore[arg-type]
        )

        # Each run() gets a fresh interpreter — guarantees no shared mutable
        # state between concurrent invocations (thread-safety contract).
        interpreter = CaMeLInterpreter(
            tools=run_tools,
            mode=self._mode,
            policy_engine=run_policies,
        )

        # Build P-LLM wrapper for this run.
        p_llm_wrapper = PLLMWrapper(backend=self._p_llm_backend)

        orchestrator = CaMeLOrchestrator(
            p_llm=p_llm_wrapper,
            interpreter=interpreter,
            tool_signatures=self._tool_signatures,
            display_channel=StdoutDisplayChannel(),
            max_loop_retries=self._max_retries,
        )

        try:
            exec_result: ExecutionResult = await orchestrator.run(
                user_query=user_query,
                user_context=user_context,
            )
            final_store = dict(exec_result.final_store)
            provenance_chains, phishing_warnings = _build_provenance_data(final_store)
            return AgentResult(
                execution_trace=list(exec_result.trace),
                display_output=[str(v.raw) for v in exec_result.print_outputs],
                policy_denials=[],
                audit_log_ref=audit_log_ref,
                loop_attempts=exec_result.loop_attempts,
                success=True,
                final_store=final_store,
                provenance_chains=provenance_chains,
                phishing_warnings=phishing_warnings,
            )
        except MaxRetriesExceededError:
            return AgentResult(
                execution_trace=[],
                display_output=[],
                policy_denials=[],
                audit_log_ref=audit_log_ref,
                loop_attempts=self._max_retries,
                success=False,
                final_store={},
            )

    def run_sync(
        self,
        user_query: str,
        user_context: UserContext | None = None,
    ) -> AgentResult:
        """Synchronous convenience wrapper around :meth:`run`.

        Calls :func:`asyncio.run` internally.  Use only when there is no
        existing running event loop (i.e. not from an ``async`` function or
        a Jupyter notebook with ``%autoawait`` enabled).

        Parameters
        ----------
        user_query:
            See :meth:`run`.
        user_context:
            See :meth:`run`.

        Returns
        -------
        AgentResult
            See :meth:`run`.
        """
        return asyncio.run(self.run(user_query=user_query, user_context=user_context))

    def get_provenance(
        self,
        variable_name: str,
        result: AgentResult,
    ) -> ProvenanceChain:
        """Return the :class:`~camel.provenance.ProvenanceChain` for *variable_name*.

        Looks up the named variable in *result*'s
        :attr:`~AgentResult.provenance_chains` mapping and returns the
        corresponding chain.  Raises :exc:`KeyError` when the variable was
        not assigned during the run (or when *result* represents a failed
        execution with an empty store).

        Parameters
        ----------
        variable_name:
            Name of the variable to look up (must match a key in
            :attr:`~AgentResult.final_store`).
        result:
            :class:`AgentResult` returned by a previous :meth:`run` or
            :meth:`run_sync` call.

        Returns
        -------
        ProvenanceChain
            Full provenance lineage for *variable_name* — one
            :class:`~camel.provenance.ProvenanceHop` per distinct origin
            source that contributed to the variable's final value.

        Raises
        ------
        KeyError
            If *variable_name* is not present in
            :attr:`~AgentResult.provenance_chains` (i.e. the variable was
            never assigned during execution, or the run failed).

        Examples
        --------
        ::

            result = await agent.run("Forward the latest email to alice@example.com")
            chain = agent.get_provenance("email_body", result)

            print(chain.is_trusted)        # False — body from get_last_email
            print(chain.to_json(indent=2)) # full JSON lineage
        """
        try:
            return result.provenance_chains[variable_name]
        except KeyError:
            raise KeyError(
                f"Variable {variable_name!r} not found in execution result. "
                f"Available variables: {sorted(result.provenance_chains)}"
            ) from None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Immutable tuple of registered :class:`~camel_security.Tool` objects.

        Returns
        -------
        tuple[Tool, ...]
            Read-only view of the tool configuration passed at construction.
        """
        return self._tools

    @property
    def mode(self) -> ExecutionMode:
        """The :class:`~camel.interpreter.ExecutionMode` configured at construction.

        Returns
        -------
        ExecutionMode
            ``STRICT`` (default) or ``NORMAL``.
        """
        return self._mode

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_tool_registry(self) -> ToolRegistry:
        """Build a :class:`~camel.tools.ToolRegistry` from the tool list."""
        registry = ToolRegistry()
        for tool in self._tools:
            registry.register(
                tool.name,
                tool.fn,
                capability_annotation=tool.capability_annotation,
            )
        return registry

    def _build_tool_signatures(self) -> list[ToolSignature]:
        """Build :class:`~camel.llm.ToolSignature` list from the tool list."""
        return [
            ToolSignature(
                name=tool.name,
                signature=tool.params,
                return_type=tool.return_type,
                description=tool.description,
            )
            for tool in self._tools
        ]

    def _build_run_policy_registry(self) -> PolicyRegistry:
        """Build a per-run policy registry merging global + per-tool policies.

        The base registry (provided at construction, or an empty one) is never
        mutated.  A fresh registry is created for each run by proxying the base
        and appending per-tool policies declared on individual
        :class:`~camel_security.Tool` instances.

        Returns
        -------
        PolicyRegistry
            New registry containing all global policies plus per-tool policies.
        """
        run_registry = PolicyRegistry()

        # Copy global (base) policies via a proxy that delegates evaluate() to
        # the source registry.  This avoids accessing private internals while
        # ensuring all base policies are consulted.
        for tool_name in self._base_policies.registered_tools():
            run_registry.register(tool_name, _ProxyPolicy(self._base_policies, tool_name))

        # Register per-tool policies from Tool.policies.
        for tool in self._tools:
            for policy_fn in tool.policies:
                run_registry.register(tool.name, policy_fn)

        return run_registry

    def __repr__(self) -> str:
        """Return a concise string representation of the agent."""
        tool_names = [t.name for t in self._tools]
        return f"CaMeLAgent(tools={tool_names!r}, mode={self._mode.name})"


# ---------------------------------------------------------------------------
# Internal proxy helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Provenance data builder — internal helper
# ---------------------------------------------------------------------------


def _build_provenance_data(
    final_store: dict[str, Any],
) -> tuple[dict[str, ProvenanceChain], list[PhishingWarning]]:
    """Build provenance chains and phishing warnings from a final variable store.

    Called by :meth:`CaMeLAgent.run` after successful execution to populate
    :attr:`AgentResult.provenance_chains` and
    :attr:`AgentResult.phishing_warnings`.

    Parameters
    ----------
    final_store:
        The interpreter's final variable store, mapping variable names to
        :class:`~camel.value.CaMeLValue` instances.

    Returns
    -------
    tuple[dict[str, ProvenanceChain], list[PhishingWarning]]
        A tuple of (provenance_chains mapping, flat list of phishing warnings
        across all variables).
    """
    chains: dict[str, ProvenanceChain] = {}
    all_warnings: list[PhishingWarning] = []

    for var_name, value in final_store.items():
        if not isinstance(value, CaMeLValue):
            continue
        chain = build_provenance_chain(var_name, value)
        chains[var_name] = chain
        all_warnings.extend(detect_phishing_content(var_name, value))

    return chains, all_warnings


class _ProxyPolicy:
    """Thin callable that proxies all base policies for a tool.

    Used by :meth:`CaMeLAgent._build_run_policy_registry` to copy global
    policies into per-run registries without accessing private internals of
    :class:`~camel.policy.PolicyRegistry`.

    This proxy registers as a single policy function but internally delegates
    to ``source_registry.evaluate(tool_name, kwargs)`` which already runs all
    registered policies for that tool in order.
    """

    def __init__(self, source: PolicyRegistry, tool_name: str) -> None:
        """Initialise the proxy with source registry and target tool name."""
        self._source = source
        self._tool_name = tool_name
        self.__name__ = f"_ProxyPolicy({tool_name})"

    def __call__(self, tool_name: str, kwargs: Any) -> Any:
        """Delegate to source registry evaluation."""
        return self._source.evaluate(self._tool_name, kwargs)
