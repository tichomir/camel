"""Exceptions raised by LLM backends in the CaMeL runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from camel.llm.schemas import QResponse


class NotEnoughInformationError(Exception):
    """Raised when a Q-LLM backend returns a response whose
    ``have_enough_information`` field is ``False``.

    The Q-LLM is not allowed to produce free-form text or refuse via a
    natural-language message — it must always return a structured
    :class:`~camel.llm.schemas.QResponse` subclass.  When the model lacks
    sufficient context to populate the schema meaningfully it sets
    ``have_enough_information = False`` and the caller MUST raise this
    exception rather than forwarding the partial response to the P-LLM.

    Attributes
    ----------
    schema_type:
        The :class:`~camel.llm.schemas.QResponse` subclass that was
        requested.
    partial_response:
        The raw (untrusted) model output that triggered the error.  Callers
        may inspect it for debugging but MUST NOT use any field values in
        trusted computation.
    """

    def __init__(
        self,
        schema_type: type[QResponse],
        partial_response: QResponse | None = None,
        message: str | None = None,
    ) -> None:
        self.schema_type = schema_type
        self.partial_response = partial_response
        detail = message or (
            f"Q-LLM reported insufficient information to populate "
            f"{schema_type.__name__!r}"
        )
        super().__init__(detail)
