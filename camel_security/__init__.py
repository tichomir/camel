"""camel-security — Stable public SDK for CaMeL prompt-injection defence.

``pip install camel-security``

This package provides the stable, typed, thread-safe public API for the CaMeL
(CApabilities for MachinE Learning) security layer.  The internal ``camel``
package contains the implementation; ``camel_security`` exposes a curated,
versioned surface designed for long-term API stability.

Quick start
-----------
.. code-block:: python

    import asyncio
    from camel_security import CaMeLAgent, Tool
    from camel.llm.backend import get_backend

    backend = get_backend("claude", api_key="sk-...", model="claude-sonnet-4-6")

    def read_email(max_count: int = 10) -> list[dict]:
        ...

    def send_email(to: str, body: str) -> bool:
        ...

    agent = CaMeLAgent(
        p_llm=backend,
        q_llm=backend,
        tools=[
            Tool(
                name="read_email",
                fn=read_email,
                description="Fetch the most recent emails.",
                params="max_count: int = 10",
                return_type="list[EmailMessage]",
            ),
            Tool(
                name="send_email",
                fn=send_email,
                description="Send an email.",
                params="to: str, body: str",
                return_type="bool",
            ),
        ],
    )

    result = asyncio.run(agent.run("Forward the latest email to alice@example.com"))
    print(result.success, result.execution_trace)

Exported names
--------------
The following names are part of the **stable public API**:

High-level agent
~~~~~~~~~~~~~~~~
:class:`CaMeLAgent`
    Main entry point.  Construct once, call :meth:`~CaMeLAgent.run` or
    :meth:`~CaMeLAgent.run_sync` as many times as needed.

:class:`AgentResult`
    Frozen dataclass returned by :meth:`~CaMeLAgent.run`.

:class:`PolicyDenialRecord`
    One entry per policy denial recorded during a run.

Tool registration
~~~~~~~~~~~~~~~~~
:class:`Tool`
    Bundles a callable with its capability annotation and per-tool policies.

Re-exported interpreter types (for advanced users)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
:class:`~camel.interpreter.ExecutionMode`
    ``STRICT`` (default) or ``NORMAL`` — passed to :class:`CaMeLAgent`.

:class:`~camel.policy.PolicyRegistry`
    Register security policies (flat model); pass the instance to
    :class:`CaMeLAgent`.

:class:`~camel.policy.Allowed` / :class:`~camel.policy.Denied`
    Return types for policy functions.

Three-tier governance (ADR-011)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
:class:`~camel.policy.governance.PolicyTier`
    Authorship tier enum (``PLATFORM``, ``TOOL_PROVIDER``, ``USER``).

:class:`~camel.policy.governance.TieredPolicyRegistry`
    Storage layer for three-tier policy entries.

:class:`~camel.policy.governance.PolicyConflictResolver`
    Merges three tiers into a single authoritative result.

:class:`~camel.policy.governance.MergedPolicyResult`
    Merged outcome returned by :meth:`~PolicyConflictResolver.evaluate`.

:class:`~camel.policy.governance.TierEvaluationRecord`
    One record in the :attr:`~MergedPolicyResult.audit_trail`.

:class:`~camel.value.CaMeLValue`
    Capability-tagged runtime value.  Appears in
    :attr:`~AgentResult.execution_trace` memory snapshots.

:class:`~camel.value.Public`
    Open-readers sentinel for :class:`~camel.value.CaMeLValue`.

:func:`~camel.llm.backend.get_backend`
    Factory to create a :class:`~camel.llm.LLMBackend` by provider name.

Version
~~~~~~~
:data:`__version__`
    Package version string, e.g. ``"0.5.0"``.

Versioning policy
-----------------
See ``VERSIONING.md`` at the repository root for the full semantic versioning
policy, including what changes require a major-version bump.

Summary:

- **Patch** (0.5.x): bug fixes, doc improvements, no API changes.
- **Minor** (0.x.0): new optional parameters with defaults, new exported
  names, new :class:`AgentResult` fields with defaults.
- **Major** (x.0.0): removal or rename of any exported name or
  :class:`AgentResult` field, change to constructor parameter semantics,
  change to security model defaults.
"""

from camel.interpreter import ExecutionMode
from camel.llm.backend import get_backend
from camel.policy.governance import (
    MergedPolicyResult,
    PolicyConflictResolver,
    PolicyTier,
    TieredPolicyRegistry,
    TierEvaluationRecord,
)
from camel.policy.interfaces import Allowed, Denied, PolicyRegistry
from camel.value import CaMeLValue, Public
from camel_security.agent import AgentResult, CaMeLAgent, PolicyDenialRecord
from camel_security.tool import Tool

__version__ = "0.5.0"

__all__ = [
    # Version
    "__version__",
    # High-level agent
    "CaMeLAgent",
    "AgentResult",
    "PolicyDenialRecord",
    # Tool registration interface
    "Tool",
    # Interpreter / execution mode
    "ExecutionMode",
    # Flat policy engine (ADR-009)
    "PolicyRegistry",
    "Allowed",
    "Denied",
    # Three-tier governance (ADR-011)
    "PolicyTier",
    "TieredPolicyRegistry",
    "PolicyConflictResolver",
    "MergedPolicyResult",
    "TierEvaluationRecord",
    # Capability system
    "CaMeLValue",
    "Public",
    # Backend factory
    "get_backend",
]
