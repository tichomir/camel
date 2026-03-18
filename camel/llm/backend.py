"""Provider-agnostic LLMBackend Protocol, LLMBackendError, and factory.

This module defines the unified interface used by the P-LLM wrapper to
communicate with language model providers without coupling to any specific
SDK.  Concrete adapters (:class:`~camel.llm.adapters.ClaudeBackend` and
:class:`~camel.llm.adapters.GeminiBackend`) satisfy this protocol via
structural subtyping — no inheritance is required.

Usage
-----
.. code-block:: python

    from camel.llm.backend import get_backend, LLMBackend

    backend: LLMBackend = get_backend("claude", api_key="sk-...")
    text = await backend.generate([{"role": "user", "content": "Hello"}])

Factory
-------
:func:`get_backend` selects a concrete adapter by provider string and
forwards constructor kwargs so API keys and model names can be injected
at runtime without hard-coding.

Error handling
--------------
Both adapters convert native SDK exceptions into :class:`LLMBackendError`
so callers do not need to handle provider-specific exception hierarchies.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from camel.llm.protocols import Message

__all__ = [
    "LLMBackend",
    "LLMBackendError",
    "get_backend",
]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class LLMBackendError(Exception):
    """Raised when an LLM backend API call fails.

    Both :class:`~camel.llm.adapters.ClaudeBackend` and
    :class:`~camel.llm.adapters.GeminiBackend` wrap their native SDK
    exceptions in this class so callers can catch a single unified exception
    type regardless of the underlying provider.

    Attributes
    ----------
    cause:
        The original exception raised by the provider SDK, if any.
        ``None`` for errors originating inside the adapter logic itself.
    """

    def __init__(
        self,
        message: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMBackend(Protocol):
    """Structural interface for LLM backends used by the P-LLM wrapper.

    Implementations must provide two methods:

    - :meth:`generate` for free-form text completion (P-LLM planning path).
    - :meth:`generate_structured` for Pydantic-schema-constrained output.

    The protocol is ``runtime_checkable`` so that test doubles and mock
    objects can be verified with ``isinstance`` in integration tests.

    Isolation contract
    ------------------
    Callers MUST NOT pass tool return values (``CaMeLValue`` instances from
    the interpreter) into the *messages* list.  The P-LLM wrapper enforces
    this via a runtime guard (see ``PLLMWrapper._build_messages``).
    """

    async def generate(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion string for *messages*.

        Parameters
        ----------
        messages:
            Ordered list of chat messages (system, user, assistant turns).
            Message dicts contain at minimum ``"role"`` and ``"content"``
            keys.
        **kwargs:
            Backend-specific options forwarded to the underlying API call,
            e.g. ``temperature``, ``max_tokens``.

        Returns
        -------
        str
            The model's raw text response.

        Raises
        ------
        LLMBackendError
            On any API-level failure (network error, rate limit, auth
            failure, etc.).
        """
        ...

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Return a Pydantic-validated structured response conforming to *schema*.

        Implementations must request structured / JSON output from the
        underlying provider (e.g. via ``tool_choice`` for Anthropic or
        ``response_mime_type`` for Gemini).  They MUST NOT pass tool
        definitions to the API call for this path.

        Parameters
        ----------
        messages:
            Ordered list of chat messages providing the content to be
            parsed or analysed.
        schema:
            A :class:`pydantic.BaseModel` subclass describing the expected
            output shape.  The backend uses this to constrain the model's
            output format.

        Returns
        -------
        BaseModel
            A validated instance of *schema*.

        Raises
        ------
        LLMBackendError
            On any API-level failure.
        """
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_backend(provider: str, **kwargs: Any) -> LLMBackend:
    """Create and return an :class:`LLMBackend` for the given *provider*.

    The factory performs a lazy import of the adapter module so that SDK
    packages (``anthropic``, ``google-generativeai``) are only required
    when the corresponding backend is actually used.

    Parameters
    ----------
    provider:
        The provider identifier string.  Supported values:

        - ``"claude"`` — Anthropic Claude via :class:`~camel.llm.adapters.ClaudeBackend`.
        - ``"gemini"`` — Google Gemini via :class:`~camel.llm.adapters.GeminiBackend`.
    **kwargs:
        Constructor arguments forwarded to the concrete adapter.  Typical
        keys: ``api_key``, ``model``, ``max_tokens``.

    Returns
    -------
    LLMBackend
        A concrete adapter instance satisfying the :class:`LLMBackend`
        protocol.

    Raises
    ------
    ValueError
        When *provider* is not a recognised provider string.
    ImportError
        When the required SDK package for *provider* is not installed.

    Examples
    --------
    .. code-block:: python

        backend = get_backend("claude", api_key="sk-...", model="claude-opus-4-6")
        backend = get_backend("gemini", api_key="AI...", model="gemini-2.0-flash")
    """
    if provider == "claude":
        from camel.llm.adapters.claude import ClaudeBackend  # noqa: PLC0415

        return ClaudeBackend(**kwargs)
    if provider == "gemini":
        from camel.llm.adapters.gemini import GeminiBackend  # noqa: PLC0415

        return GeminiBackend(**kwargs)
    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        "Supported providers: 'claude', 'gemini'."
    )
