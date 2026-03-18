"""RecordingBackend spy and StubBackend for isolation harness tests.

Design follows ADR-008 §2 (Dependency Injection approach).

``StubBackend`` returns pre-baked plan strings via ``generate()``.
``RecordingBackend`` wraps any backend and records every ``generate()`` and
``generate_structured()`` call so tests can assert on what messages were
forwarded to the underlying model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from camel.llm.protocols import Message

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedCall:
    """A single recorded LLM backend call.

    Attributes
    ----------
    method:
        Either ``"generate"`` or ``"generate_structured"``.
    messages:
        The message list exactly as passed to the backend method.
    """

    method: str
    messages: list[Message]


# ---------------------------------------------------------------------------
# StubBackend — deterministic pre-baked responses
# ---------------------------------------------------------------------------


class StubBackend:
    """Deterministic P-LLM backend stub for harness tests.

    Returns pre-baked Python plan strings wrapped in Markdown fences so
    ``PLLMWrapper`` can parse them.  When ``cycle=True`` the response
    list is treated as a ring buffer (useful when a scenario is run
    multiple times and the same plan should be returned each time).

    Parameters
    ----------
    responses:
        Ordered list of raw Python plan source strings (without fence
        markers).  ``generate()`` pops them in order.
    cycle:
        When ``True`` the list is cycled (``responses[i % len(responses)]``).
        When ``False`` an ``IndexError`` is raised after all responses are
        consumed.
    """

    def __init__(self, responses: list[str], cycle: bool = True) -> None:
        """Initialise with plan strings and cycling behaviour."""
        self._responses = responses
        self._cycle = cycle
        self._index = 0

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        """Return the next pre-baked plan wrapped in a Markdown fence.

        Parameters
        ----------
        messages:
            Forwarded from the P-LLM wrapper (not inspected by the stub).
        **kwargs:
            Additional backend options (ignored).

        Returns
        -------
        str
            A Markdown-fenced ``python`` block containing the next plan.

        Raises
        ------
        IndexError
            When ``cycle=False`` and all responses have been consumed.
        """
        if self._cycle:
            plan = self._responses[self._index % len(self._responses)]
        else:
            plan = self._responses[self._index]
        self._index += 1
        return f"```python\n{plan}\n```"

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Not implemented — StubBackend is P-LLM only.

        Parameters
        ----------
        messages:
            Chat messages (unused).
        schema:
            Output schema (unused).

        Raises
        ------
        NotImplementedError
            Always — use a dedicated Q-LLM stub for structured calls.
        """
        raise NotImplementedError(
            "StubBackend does not support generate_structured(); "
            "use a dedicated QStubBackend for Q-LLM calls."
        )

    def get_backend_id(self) -> str:
        """Return a stable stub identifier for protocol conformance."""
        return "stub:test"

    def supports_structured_output(self) -> bool:
        """Return True for protocol conformance."""
        return True

    def reset(self) -> None:
        """Reset the response index to zero."""
        self._index = 0


# ---------------------------------------------------------------------------
# RecordingBackend — dependency-injection spy (ADR-008 §2)
# ---------------------------------------------------------------------------


class RecordingBackend:
    """LLMBackend spy that records all ``generate()`` and ``generate_structured()`` calls.

    Wraps any delegate backend and intercepts every call, storing a
    :class:`RecordedCall` for later assertion.  The spy satisfies the
    ``LLMBackend`` structural protocol (ADR-008 §1).

    Parameters
    ----------
    delegate:
        The underlying backend to forward calls to.

    Attributes
    ----------
    recorded_calls:
        Ordered list of :class:`RecordedCall` instances accumulated since
        construction (or since the last :meth:`reset`).
    """

    def __init__(self, delegate: Any) -> None:
        """Initialise with a delegate backend."""
        self._delegate = delegate
        self.recorded_calls: list[RecordedCall] = []

    async def generate(self, messages: list[Message], **kwargs: Any) -> str:
        """Record the call then delegate to the underlying backend.

        Parameters
        ----------
        messages:
            Chat messages forwarded verbatim to the delegate.
        **kwargs:
            Backend-specific options forwarded verbatim.

        Returns
        -------
        str
            The delegate's response string.
        """
        self.recorded_calls.append(
            RecordedCall(method="generate", messages=list(messages))
        )
        return await self._delegate.generate(messages, **kwargs)

    async def generate_structured(
        self,
        messages: list[Message],
        schema: type[BaseModel],
    ) -> BaseModel:
        """Record the call then delegate to the underlying backend.

        Parameters
        ----------
        messages:
            Chat messages forwarded verbatim to the delegate.
        schema:
            Pydantic schema forwarded verbatim.

        Returns
        -------
        BaseModel
            The delegate's validated structured response.
        """
        self.recorded_calls.append(
            RecordedCall(method="generate_structured", messages=list(messages))
        )
        return await self._delegate.generate_structured(messages, schema)

    def get_backend_id(self) -> str:
        """Return a stable recording-backend identifier for protocol conformance."""
        return "recording:delegate"

    def supports_structured_output(self) -> bool:
        """Delegate supports_structured_output to the underlying backend."""
        if hasattr(self._delegate, "supports_structured_output"):
            return bool(self._delegate.supports_structured_output())
        return True

    def reset(self) -> None:
        """Clear all recorded calls."""
        self.recorded_calls.clear()

    def all_message_content(self) -> list[str]:
        """Return all message content strings from all recorded calls.

        Returns
        -------
        list[str]
            Flat list of ``msg["content"]`` strings extracted from every
            recorded call, in order.  Non-string content values are skipped.
        """
        contents: list[str] = []
        for call in self.recorded_calls:
            for msg in call.messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    contents.append(content)
        return contents
