"""Tool registration interface for the camel-security SDK.

:class:`Tool` is the single object SDK users pass to :class:`~camel_security.CaMeLAgent`
to register a callable with the CaMeL runtime.  It bundles:

* the raw Python callable (*fn*)
* the name used in P-LLM-generated plans
* optional human-readable metadata (description, typed parameter string, return type)
  that are injected into the P-LLM system prompt so the model knows how to call the tool
* an optional :data:`~camel.capabilities.CapabilityAnnotationFn` for fine-grained
  provenance tagging of the tool's return value
* zero or more :data:`~camel.policy.PolicyFn` callables enforced before every tool call

Stability guarantee
-------------------
All fields listed in the :class:`Tool` docstring are **stable** and will not be removed
or renamed without a **major-version bump**.  New optional fields may be added in minor
releases; they will always have default values so existing code is unaffected.

Examples
--------
Minimal registration — name and callable only::

    from camel_security import Tool

    def get_email(max_count: int = 10) -> list[dict]:
        ...

    email_tool = Tool(name="get_email", fn=get_email)

With description and typed signature (improves P-LLM plan quality)::

    email_tool = Tool(
        name="get_email",
        fn=get_email,
        description="Fetch the most recent emails from the inbox.",
        params="max_count: int = 10",
        return_type="list[EmailMessage]",
    )

With custom capability annotation and an inline policy::

    from camel.value import CaMeLValue, Public
    from camel.policy import Allowed, Denied, SecurityPolicyResult
    from collections.abc import Mapping

    def annotate_email(return_value, tool_kwargs):
        return CaMeLValue(
            value=return_value,
            sources=frozenset({"get_email"}),
            inner_source="inbox",
            readers=Public,
        )

    def no_forwarding_policy(tool_name, kwargs):
        return Allowed()

    email_tool = Tool(
        name="get_email",
        fn=get_email,
        capability_annotation=annotate_email,
        policies=[no_forwarding_policy],
    )
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from camel.policy.interfaces import PolicyFn
from camel.value import CaMeLValue

__all__ = ["Tool"]


@dataclass
class Tool:
    """Bundles a callable with all CaMeL runtime metadata needed for registration.

    Instances are passed to :class:`~camel_security.CaMeLAgent` as the ``tools``
    sequence.  The agent converts each :class:`Tool` into a
    :class:`~camel.tools.ToolRegistry` entry and a matching
    :class:`~camel.llm.ToolSignature` for the P-LLM system prompt.

    Parameters
    ----------
    name:
        The identifier used in P-LLM-generated pseudo-Python plans
        (e.g. ``"send_email"``).  Must be a valid Python identifier and
        unique across all tools passed to the same agent.
    fn:
        The underlying Python callable.  It is invoked with raw (unwrapped)
        argument values and must return a raw Python value or a
        :class:`~camel.value.CaMeLValue`.
    description:
        Human-readable description injected into the P-LLM system prompt.
        A clear description improves plan quality.  Defaults to an empty
        string (the P-LLM falls back to inferring purpose from the name).
    params:
        Parameter signature string injected into the P-LLM system prompt,
        e.g. ``"to: str, subject: str, body: str"``.  Defaults to ``""``
        (no parameters shown to the P-LLM).
    return_type:
        Return-type string injected into the P-LLM system prompt,
        e.g. ``"EmailMessage"``.  Defaults to ``"Any"``.
    capability_annotation:
        Optional callable with signature
        ``(return_value: Any, tool_kwargs: Mapping[str, Any]) -> CaMeLValue``.
        When ``None``, the default annotation (``sources={name}``,
        ``readers=Public``) is applied.
    policies:
        Zero or more :data:`~camel.policy.PolicyFn` callables evaluated
        (in list order) before every execution of this tool.  The first
        :class:`~camel.policy.Denied` result blocks the call.  When
        ``None`` or an empty list, no per-tool policies are enforced
        (global policies on the :class:`~camel.policy.PolicyRegistry` still
        apply).

    Stability guarantee
    -------------------
    All listed fields are **stable** (no removal or rename without a major
    version bump).  New optional fields may be added in minor releases.

    Examples
    --------
    ::

        tool = Tool(
            name="read_file",
            fn=read_file_fn,
            description="Read a file from disk and return its text content.",
            params="path: str",
            return_type="str",
        )
    """

    name: str
    fn: Callable[..., Any]
    description: str = ""
    params: str = ""
    return_type: str = "Any"
    capability_annotation: Callable[[Any, Mapping[str, Any]], CaMeLValue] | None = None
    policies: list[PolicyFn] = field(default_factory=list)
