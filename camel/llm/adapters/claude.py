"""Anthropic Claude backend adapter.

Implements both :class:`~camel.llm.protocols.LLMBackend` (P-LLM) and
:class:`~camel.llm.protocols.QlLMBackend` (Q-LLM) structural protocols.

Structured-output strategy
--------------------------
Claude does not have a ``response_format`` JSON-schema parameter like OpenAI.
Instead, :meth:`ClaudeBackend.structured_complete` uses the *synthetic
extraction tool* pattern described in ADR-001:

1. A single tool is registered whose ``input_schema`` is derived from the
   caller-supplied :class:`~camel.llm.schemas.QResponse` subclass via
   ``model.model_json_schema()``.
2. ``tool_choice`` is set to ``{"type": "tool", "name": "<tool_name>"}`` to
   force the model to always emit a tool call (i.e. structured JSON) rather
   than free-form text.
3. The ``tool_use`` block returned by the API is parsed back into the Pydantic
   schema — **no tool is ever executed**.

This satisfies the isolation contract: the model cannot call real tools because
no real tool definitions are present; it can only return structured JSON via the
"extraction" pseudo-tool.

Requirements
------------
``anthropic>=0.25.0``  (``pip install anthropic``)
"""

from __future__ import annotations

from typing import Any

from camel.llm.protocols import Message, QResponseT

_EXTRACTION_TOOL_NAME = "extract_structured_data"


class ClaudeBackend:
    """Anthropic Claude backend satisfying both LLMBackend and QlLMBackend.

    Parameters
    ----------
    api_key:
        Anthropic API key.  If *None*, the ``ANTHROPIC_API_KEY`` environment
        variable is used.
    model:
        Claude model identifier (e.g. ``"claude-opus-4-6"``).
    max_tokens:
        Maximum tokens to generate per request.
    **default_kwargs:
        Additional keyword arguments forwarded to every
        ``messages.create`` call (e.g. ``temperature``).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-opus-4-6",
        max_tokens: int = 4096,
        **default_kwargs: Any,
    ) -> None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "The 'anthropic' package is required for ClaudeBackend. "
                "Install it with: pip install anthropic"
            ) from exc

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._default_kwargs = default_kwargs

    # ------------------------------------------------------------------
    # LLMBackend protocol (P-LLM)
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion for *messages* (P-LLM path).

        All items in *messages* with ``role == "system"`` are extracted and
        joined into a single Anthropic ``system`` prompt; the remaining
        turns form the ``messages`` list.
        """

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        chat_messages = [m for m in messages if m.get("role") != "system"]

        create_kwargs: dict[str, Any] = {
            **self._default_kwargs,
            **kwargs,
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": chat_messages,
        }
        if system_parts:
            create_kwargs["system"] = "\n\n".join(system_parts)

        response = await self._client.messages.create(**create_kwargs)
        # Extract the first text block.
        for block in response.content:
            if block.type == "text":
                return block.text
        raise ValueError(  # pragma: no cover
            f"Claude returned no text content block. Full response: {response}"
        )

    # ------------------------------------------------------------------
    # QlLMBackend protocol (Q-LLM)
    # ------------------------------------------------------------------

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Return a structured response conforming to *schema* (Q-LLM path).

        Uses the synthetic extraction tool pattern — no real tool is called.
        Tool definitions are scoped to this method and never shared with the
        P-LLM path.
        """
        json_schema = schema.model_json_schema()

        # Build a single pseudo-tool whose input schema matches the requested
        # QResponse subclass.  We force tool_choice so the model always
        # returns structured JSON via this pseudo-tool.
        extraction_tool = {
            "name": _EXTRACTION_TOOL_NAME,
            "description": (
                "Extract structured data from the provided content.  "
                "Return ALL fields according to the schema.  "
                "Set have_enough_information to false only when the content "
                "genuinely lacks the information needed to populate the schema."
            ),
            "input_schema": json_schema,
        }

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        chat_messages = [m for m in messages if m.get("role") != "system"]

        create_kwargs: dict[str, Any] = {
            **self._default_kwargs,
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": chat_messages,
            "tools": [extraction_tool],
            "tool_choice": {"type": "tool", "name": _EXTRACTION_TOOL_NAME},
        }
        if system_parts:
            create_kwargs["system"] = "\n\n".join(system_parts)

        response = await self._client.messages.create(**create_kwargs)

        # Extract the tool_use block.
        for block in response.content:
            if block.type == "tool_use" and block.name == _EXTRACTION_TOOL_NAME:
                raw: dict[str, Any] = block.input  # type: ignore[assignment]
                return schema.model_validate(raw)

        raise ValueError(  # pragma: no cover
            f"Claude did not return a '{_EXTRACTION_TOOL_NAME}' tool_use block. "
            f"Full response: {response}"
        )
