"""Q-LLM schema augmentation utilities.

Provides :func:`build_augmented_schema`, which dynamically injects the
``have_enough_information: bool`` sentinel field into any caller-supplied
:class:`~pydantic.BaseModel` subclass without mutating the original class.

The injected field is the mechanism by which the Q-LLM signals whether it
had sufficient context to populate all other fields.  Callers must check
this field and raise :class:`~camel.exceptions.NotEnoughInformationError`
when it is ``False``.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, Field, create_model

T = TypeVar("T", bound=BaseModel)

#: Name of the sentinel boolean injected into every augmented schema.
_HEI_FIELD: str = "have_enough_information"

#: JSON Schema description for the injected field.
_HEI_DESCRIPTION: str = (
    "Set to True only when the model has sufficient information to populate "
    "all other fields meaningfully.  When False, callers MUST raise "
    "NotEnoughInformationError and MUST NOT use any field values."
)


def build_augmented_schema(user_schema: type[T]) -> type[T]:
    """Return a new Pydantic model with ``have_enough_information: bool`` added.

    Parameters
    ----------
    user_schema:
        Any :class:`~pydantic.BaseModel` subclass.  Must **not** already
        declare a field named ``have_enough_information``; pass an
        unaugmented schema.

    Returns
    -------
    type[T]
        A new dynamically-constructed model class that:

        * Is a subclass of *user_schema*
          (``issubclass(result, user_schema)`` is ``True``).
        * Declares ``have_enough_information: bool`` as a required field.
        * Inherits all field validators and constraints from *user_schema*.
        * Has the same ``__name__`` as *user_schema* for prompt readability.
        * Does **not** mutate *user_schema*.

    Raises
    ------
    ValueError
        If *user_schema* already declares a field named
        ``have_enough_information``.  This prevents silent field shadowing
        and makes schema conflicts visible at construction time.

    Examples
    --------
    .. code-block:: python

        from pydantic import BaseModel
        from camel.qllm_schema import build_augmented_schema

        class EmailInfo(BaseModel):
            sender: str
            subject: str

        Augmented = build_augmented_schema(EmailInfo)

        assert "have_enough_information" in Augmented.model_fields
        assert issubclass(Augmented, EmailInfo)
        # Original class is unchanged:
        assert "have_enough_information" not in EmailInfo.model_fields
    """
    if _HEI_FIELD in user_schema.model_fields:
        raise ValueError(
            f"Schema {user_schema.__name__!r} already declares a field named "
            f"{_HEI_FIELD!r}.  Pass an unaugmented schema to "
            "build_augmented_schema()."
        )

    augmented: type[T] = create_model(  # type: ignore[call-overload]
        user_schema.__name__,
        __base__=user_schema,
        **{
            _HEI_FIELD: (
                bool,
                Field(..., description=_HEI_DESCRIPTION),
            )
        },
    )
    return augmented
