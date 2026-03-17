"""Top-level CaMeL exceptions.

This module contains exceptions that are part of the stable public API and
may be raised by components outside the ``camel.llm`` sub-package.
"""

from __future__ import annotations


class NotEnoughInformationError(Exception):
    """Raised when the Q-LLM indicates it cannot populate the requested schema.

    The error message is a fixed, static string.  No untrusted content (LLM
    output, field values, prompt text) is ever interpolated into the message,
    preventing callers from accidentally forwarding adversary-controlled data
    into exception handlers or log sinks.
    """

    MESSAGE: str = "Q-LLM indicated insufficient information"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class SchemaValidationError(Exception):
    """Raised when a Q-LLM response fails Pydantic schema validation.

    Only the developer-declared schema class name is included in the message.
    No untrusted field values or raw LLM output are interpolated, preventing
    adversary-controlled data from appearing in exception handlers or logs.

    Attributes
    ----------
    schema_name:
        The ``__name__`` of the Pydantic model class that the response failed
        to conform to.
    """

    def __init__(self, schema_name: str) -> None:
        self.schema_name = schema_name
        super().__init__(
            f"Q-LLM response failed schema validation for {schema_name!r}"
        )
