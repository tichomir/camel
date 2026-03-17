"""Runtime protocols (structural interfaces) for LLM backends.

CaMeL defines two complementary LLM roles:

P-LLM (Privileged LLM)
    The planning and orchestration model.  Operates with full capability:
    may call tools, produce free-form text, and manage conversation state.
    Backed by :class:`LLMBackend`.

Q-LLM (Quarantined LLM)
    A read-only, structured-output-only model used to parse untrusted content
    (e.g. email bodies, web pages).  It has *no* tool-calling capability and
    *cannot* return free-form text.  All outputs are :class:`QResponse`
    instances tagged as untrusted ``CaMeLValue``s by the caller.
    Backed by :class:`QlLMBackend`.

Both protocols use structural subtyping (``typing.Protocol``) so that test
doubles and alternative implementations do not need to inherit from a
concrete base class.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from camel.llm.schemas import QResponse

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

#: A single chat message as a plain dict, e.g. {"role": "user", "content": "…"}.
#: Concrete implementations may narrow this to a typed dataclass.
Message = dict[str, Any]

QResponseT = TypeVar("QResponseT", bound=QResponse)


# ---------------------------------------------------------------------------
# P-LLM backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMBackend(Protocol):
    """Structural interface for the Privileged LLM backend.

    Implementations power the P-LLM planning loop.  They MAY support tool
    definitions via additional keyword arguments; the protocol captures only
    the minimal surface required by the runtime.
    """

    async def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> str:
        """Return a free-form completion string for *messages*.

        Parameters
        ----------
        messages:
            Ordered list of chat messages (system, user, assistant turns).
        **kwargs:
            Backend-specific options such as ``tools``, ``temperature``, etc.

        Returns
        -------
        str
            The model's text response.
        """
        ...


# ---------------------------------------------------------------------------
# Q-LLM backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class QlLMBackend(Protocol):
    """Structural interface for the Quarantined LLM backend.

    **Isolation contract** — implementors MUST respect all of the following:

    1. **No tool calling.** ``structured_complete`` must not accept, pass
       through, or otherwise expose a ``tools`` parameter.  The underlying
       model call MUST be made without any tool/function definitions.

    2. **No free-form text.** The return type is always a concrete
       :class:`~camel.llm.schemas.QResponse` subclass.  If the model would
       otherwise produce a natural-language refusal or partial answer, it
       MUST instead set ``have_enough_information = False`` in the schema.

    3. **Caller tags output as untrusted.** The caller is responsible for
       wrapping the returned :class:`~camel.llm.schemas.QResponse` in an
       untrusted ``CaMeLValue`` before passing any field values back to the
       P-LLM context.

    4. **Raise on insufficient information.** If the returned schema has
       ``have_enough_information == False``, the caller MUST raise
       :class:`~camel.llm.exceptions.NotEnoughInformationError` before any
       field is consumed.
    """

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[QResponseT],
    ) -> QResponseT:
        """Return a structured response conforming to *schema*.

        Parameters
        ----------
        messages:
            Ordered list of chat messages providing the (untrusted) content
            to be parsed or analysed.
        schema:
            A :class:`~camel.llm.schemas.QResponse` subclass describing the
            expected output shape.  The backend MUST request structured
            output from the model using this schema; it MUST NOT pass tool
            definitions to the underlying API call.

        Returns
        -------
        QResponseT
            A validated instance of *schema*.  The caller must check
            ``result.have_enough_information`` before using any field.

        Notes
        -----
        Implementations should use the model provider's native structured-
        output or JSON-mode feature (e.g. Anthropic's ``tool_choice`` with a
        synthetic extraction tool, or OpenAI's ``response_format``) rather
        than prompt engineering alone, to guarantee schema conformance.
        """
        ...
