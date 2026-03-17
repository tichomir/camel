"""Pydantic schema contracts for Q-LLM structured outputs.

All schemas returned by a :class:`~camel.llm.protocols.QlLMBackend` MUST
inherit from :class:`QResponse`.  The mandatory ``have_enough_information``
field is the *only* mechanism the Q-LLM has to signal uncertainty — it has
no tool-calling capability and cannot return free-form text.

Isolation guarantee
-------------------
Q-LLM outputs are *always* tagged as untrusted :class:`CaMeLValue` instances
by the caller before being passed back into the P-LLM context.  The runtime
enforces this by never accepting a raw :class:`QResponse` where a trusted
value is expected.

Usage example
-------------
Define a domain schema by subclassing :class:`QResponse`:

.. code-block:: python

    from camel.llm.schemas import QResponse

    class EmailExtraction(QResponse):
        sender: str
        subject: str
        body_summary: str

Invoke via the Q-LLM backend and handle the uncertainty signal:

.. code-block:: python

    from camel.llm.exceptions import NotEnoughInformationError

    result: EmailExtraction = await q_llm.structured_complete(
        messages=conversation,
        schema=EmailExtraction,
    )

    if not result.have_enough_information:
        raise NotEnoughInformationError(
            schema_type=EmailExtraction,
            partial_response=result,
        )

    # result is now typed EmailExtraction with all fields populated.
    # Tag as untrusted before handing to P-LLM:
    untrusted_value = CaMeLValue(result, trusted=False)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QResponse(BaseModel):
    """Base class for all Q-LLM response schemas.

    Every schema returned by a Q-LLM backend must inherit from this class.
    The ``have_enough_information`` field is mandatory and must be the *last
    semantic decision* the model makes: if it is ``False``, all other fields
    SHOULD be treated as meaningless placeholders and the caller MUST raise
    :class:`~camel.llm.exceptions.NotEnoughInformationError`.

    Subclasses must not override or shadow ``have_enough_information``.

    Attributes
    ----------
    have_enough_information:
        ``True`` when the model had sufficient context to populate every
        other field with a meaningful value.  ``False`` signals that the
        model lacked context and the response MUST NOT be used.
    """

    have_enough_information: bool = Field(
        ...,
        description=(
            "Set to True only when the model has sufficient information to "
            "populate all other fields with meaningful values.  When False, "
            "the caller must raise NotEnoughInformationError and must not "
            "forward any field values to the P-LLM."
        ),
    )

    model_config = {
        # Prevent accidental extra fields that could smuggle free-form text.
        "extra": "forbid",
        # Freeze instances so callers cannot mutate untrusted data in place.
        "frozen": True,
    }
