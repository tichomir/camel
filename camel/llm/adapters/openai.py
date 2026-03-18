"""OpenAI backend adapter.

Implements both :class:`~camel.llm.protocols.LLMBackend` (P-LLM) and
:class:`~camel.llm.protocols.QlLMBackend` (Q-LLM) structural protocols,
as well as the unified :class:`~camel.llm.backend.LLMBackend` protocol
(``generate`` / ``generate_structured``).

Structured-output strategy
--------------------------
:meth:`OpenAIBackend.structured_complete` uses OpenAI's native
``response_format`` parameter with ``type="json_schema"`` to constrain the
model's output to the caller-supplied :class:`~camel.llm.schemas.QResponse`
subclass.  The JSON response is then validated by Pydantic.

For models in the ``o3`` / ``o4-mini`` series that do not support
``response_format``, the adapter falls back to requesting JSON output via
the system prompt and validating the parsed response against the schema.

Requirements
------------
``openai>=1.30.0``  (``pip install openai``)
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from camel.llm.protocols import Message, QResponseT


class OpenAIBackend:
    """OpenAI backend satisfying both LLMBackend and QlLMBackend protocols.

    Supports GPT-4.1, o3, and o4-mini class models.  Uses the OpenAI
    Async client for all requests.

    Parameters
    ----------
    api_key:
        OpenAI API key.  If *None*, the ``OPENAI_API_KEY`` environment
        variable is used.
    model:
        OpenAI model identifier (e.g. ``"gpt-4.1"``, ``"o3"``,
        ``"o4-mini"``).
    max_tokens:
        Maximum tokens to generate per request.  For ``o3``/``o4-mini``
        reasoning models, this maps to ``max_completion_tokens``.
    **default_kwargs:
        Additional keyword arguments forwarded to every ``chat.completions``
        call (e.g. ``temperature``, ``top_p``).
    """

    #: Models that support ``response_format`` with JSON schema mode.
    _STRUCTURED_OUTPUT_MODELS: frozenset[str] = frozenset(
        {
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
        }
    )

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4.1",
        max_tokens: int = 4096,
        **default_kwargs: Any,
    ) -> None:
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "The 'openai' package is required for OpenAIBackend. "
                "Install it with: pip install openai"
            ) from exc

        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._default_kwargs = default_kwargs

    # ------------------------------------------------------------------
    # Identity & capability methods
    # ------------------------------------------------------------------

    def get_backend_id(self) -> str:
        """Return a stable identifier for this backend instance.

        Returns
        -------
        str
            A string of the form ``"openai:<model>"``,
            e.g. ``"openai:gpt-4.1"``.
        """
        return f"openai:{self._model}"

    def supports_structured_output(self) -> bool:
        """Return whether this model supports native structured output.

        Models in the GPT-4.1 / GPT-4o family support ``response_format``
        with JSON schema mode.  Reasoning models (``o3``, ``o4-mini``) fall
        back to prompt-based JSON extraction.

        Returns
        -------
        bool
            ``True`` for GPT-4.1/GPT-4o-class models; ``False`` for
            ``o3``/``o4-mini`` reasoning models.
        """
        return self._model in self._STRUCTURED_OUTPUT_MODELS

    # ------------------------------------------------------------------
    # LLMBackend protocol (P-LLM)
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion for *messages* (P-LLM path).

        Parameters
        ----------
        messages:
            Ordered list of chat messages.
        **kwargs:
            Forwarded to the OpenAI ``chat.completions.create`` call.

        Returns
        -------
        str
            The model's text response.
        """
        create_kwargs: dict[str, Any] = {
            **self._default_kwargs,
            **kwargs,
            "model": self._model,
            "messages": messages,
        }
        # Reasoning models use max_completion_tokens instead of max_tokens.
        if self._model.startswith(("o3", "o4")):
            create_kwargs["max_completion_tokens"] = self._max_tokens
        else:
            create_kwargs["max_tokens"] = self._max_tokens

        response = await self._client.chat.completions.create(**create_kwargs)
        content = response.choices[0].message.content
        if content is None:  # pragma: no cover
            raise ValueError(f"OpenAI returned no message content. Full response: {response}")
        return content

    # ------------------------------------------------------------------
    # QlLMBackend protocol (Q-LLM)
    # ------------------------------------------------------------------

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Return a structured response conforming to *schema* (Q-LLM path).

        Uses ``response_format`` with JSON schema mode for GPT-4.1/GPT-4o
        family models.  Falls back to prompt-based JSON extraction for
        reasoning models (``o3``, ``o4-mini``) which do not support
        ``response_format``.

        No tool definitions are passed to the underlying API call, satisfying
        the Q-LLM isolation contract.

        Parameters
        ----------
        messages:
            Ordered list of chat messages providing the untrusted content.
        schema:
            A :class:`~camel.llm.schemas.QResponse` subclass describing the
            expected output shape.

        Returns
        -------
        QResponseT
            A validated instance of *schema*.
        """
        if self.supports_structured_output():
            return await self._structured_complete_native(messages, schema)
        return await self._structured_complete_prompt(messages, schema)

    async def _structured_complete_native(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Structured output via OpenAI ``response_format`` JSON schema mode."""
        json_schema = schema.model_json_schema()

        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": json_schema,
                "strict": True,
            },
        }

        create_kwargs: dict[str, Any] = {
            **self._default_kwargs,
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": messages,
            "response_format": response_format,
        }

        response = await self._client.chat.completions.create(**create_kwargs)
        content = response.choices[0].message.content
        if content is None:  # pragma: no cover
            raise ValueError(f"OpenAI returned no message content. Full response: {response}")
        raw: dict[str, Any] = json.loads(content)
        return schema.model_validate(raw)

    async def _structured_complete_prompt(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Structured output via prompt-based JSON extraction (fallback path).

        Used for reasoning models (o3, o4-mini) that do not support the
        ``response_format`` parameter.
        """
        json_schema = schema.model_json_schema()
        schema_instruction = (
            "You must respond with valid JSON that exactly conforms to this "
            f"JSON schema:\n{json.dumps(json_schema, indent=2)}\n"
            "Do not include any text outside the JSON object."
        )

        augmented_messages: list[Message] = [
            {"role": "system", "content": schema_instruction},
            *messages,
        ]

        create_kwargs: dict[str, Any] = {
            **self._default_kwargs,
            "model": self._model,
            "max_completion_tokens": self._max_tokens,
            "messages": augmented_messages,
        }

        response = await self._client.chat.completions.create(**create_kwargs)
        content = response.choices[0].message.content
        if content is None:  # pragma: no cover
            raise ValueError(f"OpenAI returned no message content. Full response: {response}")
        # Strip markdown fencing if present.
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```")[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
        raw: dict[str, Any] = json.loads(stripped)
        return schema.model_validate(raw)

    # ------------------------------------------------------------------
    # Unified LLMBackend protocol (generate / generate_structured)
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion for *messages* (unified P-LLM path).

        Delegates to :meth:`complete` and wraps any provider error as
        :class:`~camel.llm.backend.LLMBackendError`.

        Parameters
        ----------
        messages:
            Ordered list of chat messages.
        **kwargs:
            Forwarded to :meth:`complete` (and then to the OpenAI API).

        Returns
        -------
        str
            The model's text response.

        Raises
        ------
        ~camel.llm.backend.LLMBackendError
            On any OpenAI API failure.
        """
        from camel.llm.backend import LLMBackendError  # noqa: PLC0415

        try:
            return await self.complete(messages, **kwargs)
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Return a schema-validated structured response (unified path).

        Accepts any :class:`pydantic.BaseModel` subclass (not just
        :class:`~camel.llm.schemas.QResponse`).  Uses native
        ``response_format`` for GPT-4.1/GPT-4o family models; falls back
        to prompt-based JSON extraction for reasoning models.  Provider
        errors are wrapped as
        :class:`~camel.llm.backend.LLMBackendError`.

        Parameters
        ----------
        messages:
            Ordered list of chat messages providing the content to parse.
        schema:
            A :class:`pydantic.BaseModel` subclass describing the expected
            output shape.

        Returns
        -------
        BaseModel
            A validated instance of *schema*.

        Raises
        ------
        ~camel.llm.backend.LLMBackendError
            On any OpenAI API failure.
        """
        from camel.llm.backend import LLMBackendError  # noqa: PLC0415

        try:
            if self.supports_structured_output():
                json_schema = schema.model_json_schema()
                response_format: dict[str, Any] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": json_schema,
                        "strict": True,
                    },
                }
                create_kwargs: dict[str, Any] = {
                    **self._default_kwargs,
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "messages": messages,
                    "response_format": response_format,
                }
                response = await self._client.chat.completions.create(**create_kwargs)
                content = response.choices[0].message.content
                if content is None:  # pragma: no cover
                    raise ValueError(
                        f"OpenAI returned no message content. Full response: {response}"
                    )
                raw: dict[str, Any] = json.loads(content)
                return schema.model_validate(raw)
            else:
                # Prompt-based fallback for reasoning models.
                json_schema = schema.model_json_schema()
                schema_instruction = (
                    "You must respond with valid JSON that exactly conforms "
                    f"to this JSON schema:\n{json.dumps(json_schema, indent=2)}\n"
                    "Do not include any text outside the JSON object."
                )
                augmented: list[Message] = [
                    {"role": "system", "content": schema_instruction},
                    *messages,
                ]
                create_kwargs = {
                    **self._default_kwargs,
                    "model": self._model,
                    "max_completion_tokens": self._max_tokens,
                    "messages": augmented,
                }
                response = await self._client.chat.completions.create(**create_kwargs)
                content = response.choices[0].message.content
                if content is None:  # pragma: no cover
                    raise ValueError(
                        f"OpenAI returned no message content. Full response: {response}"
                    )
                stripped = content.strip()
                if stripped.startswith("```"):
                    stripped = stripped.split("```")[1]
                    if stripped.startswith("json"):
                        stripped = stripped[4:]
                    stripped = stripped.strip()
                raw_d: dict[str, Any] = json.loads(stripped)
                return schema.model_validate(raw_d)
        except LLMBackendError:
            raise
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc
