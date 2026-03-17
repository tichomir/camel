"""Google Gemini backend adapter.

Implements both :class:`~camel.llm.protocols.LLMBackend` (P-LLM) and
:class:`~camel.llm.protocols.QlLMBackend` (Q-LLM) structural protocols,
as well as the unified :class:`~camel.llm.backend.LLMBackend` protocol
(``generate`` / ``generate_structured``).

Structured-output strategy
--------------------------
:meth:`GeminiBackend.structured_complete` uses the ``response_mime_type`` and
``response_schema`` parameters of the Gemini ``GenerativeModel.generate_content``
API to request JSON output constrained to the caller-supplied
:class:`~camel.llm.schemas.QResponse` subclass.  The JSON is then parsed and
validated by Pydantic — **no tool definitions are passed to the API**.

Requirements
------------
``google-generativeai>=0.7.0``  (``pip install google-generativeai``)
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from camel.llm.protocols import Message, QResponseT


class GeminiBackend:
    """Google Gemini backend satisfying both LLMBackend and QlLMBackend.

    Parameters
    ----------
    api_key:
        Google API key.  If *None*, the ``GOOGLE_API_KEY`` environment
        variable (or Application Default Credentials) is used.
    model:
        Gemini model identifier (e.g. ``"gemini-2.0-flash"``).
    **default_kwargs:
        Additional keyword arguments forwarded to every
        ``generate_content`` call (e.g. ``generation_config``).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.0-flash",
        **default_kwargs: Any,
    ) -> None:
        try:
            import google.generativeai as genai  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "The 'google-generativeai' package is required for GeminiBackend. "
                "Install it with: pip install google-generativeai"
            ) from exc

        if api_key is not None:
            genai.configure(api_key=api_key)

        self._genai = genai
        self._model_name = model
        self._default_kwargs = default_kwargs

    # ------------------------------------------------------------------
    # LLMBackend protocol (P-LLM)
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion for *messages* (P-LLM path)."""
        model = self._genai.GenerativeModel(model_name=self._model_name)
        contents = self._messages_to_contents(messages)

        response = await model.generate_content_async(
            contents,
            **{**self._default_kwargs, **kwargs},
        )
        return response.text

    # ------------------------------------------------------------------
    # QlLMBackend protocol (Q-LLM)
    # ------------------------------------------------------------------

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Return a structured response conforming to *schema* (Q-LLM path).

        Uses Gemini's native JSON output mode (``response_mime_type`` +
        ``response_schema``).  No tool definitions are passed.
        """
        import google.generativeai.types as genai_types  # noqa: PLC0415

        json_schema = schema.model_json_schema()

        generation_config = genai_types.GenerationConfig(
            response_mime_type="application/json",
            response_schema=json_schema,
        )

        model = self._genai.GenerativeModel(model_name=self._model_name)
        contents = self._messages_to_contents(messages)

        response = await model.generate_content_async(
            contents,
            generation_config=generation_config,
        )

        raw: dict[str, Any] = json.loads(response.text)
        return schema.model_validate(raw)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
            Forwarded to :meth:`complete` (and then to the Gemini API).

        Returns
        -------
        str
            The model's text response.

        Raises
        ------
        ~camel.llm.backend.LLMBackendError
            On any Google AI API failure.
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

        Uses the same ``response_mime_type`` + ``response_schema`` pattern
        as :meth:`structured_complete` but accepts any
        :class:`pydantic.BaseModel` subclass.  Provider errors are wrapped
        as :class:`~camel.llm.backend.LLMBackendError`.

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
            On any Google AI API failure.
        """
        from camel.llm.backend import LLMBackendError  # noqa: PLC0415

        try:
            import google.generativeai.types as genai_types  # noqa: PLC0415

            json_schema = schema.model_json_schema()
            generation_config = genai_types.GenerationConfig(
                response_mime_type="application/json",
                response_schema=json_schema,
            )

            model = self._genai.GenerativeModel(model_name=self._model_name)
            contents = self._messages_to_contents(messages)

            response = await model.generate_content_async(
                contents,
                generation_config=generation_config,
            )

            raw: dict[str, Any] = json.loads(response.text)
            return schema.model_validate(raw)
        except LLMBackendError:
            raise
        except Exception as exc:
            raise LLMBackendError(str(exc), cause=exc) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _messages_to_contents(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert CaMeL Message dicts to Gemini content format.

        System messages are prepended as a user turn with a ``[SYSTEM]``
        prefix because Gemini's ``generate_content`` API does not have a
        dedicated ``system`` role in ``contents`` (use ``system_instruction``
        on the model instead for persistent system prompts).
        """
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                # Inline as a user turn; callers may also pass system_instruction
                # at model construction time for persistent system prompts.
                contents.append({"role": "user", "parts": [f"[SYSTEM] {content}"]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [content]})
            else:
                contents.append({"role": "user", "parts": [content]})
        return contents
