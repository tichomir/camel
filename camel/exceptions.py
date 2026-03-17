"""Top-level CaMeL exceptions.

This module contains exceptions that are part of the stable public API and
may be raised by components outside the ``camel.llm`` sub-package.
"""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass
class ForbiddenImportError(Exception):
    """Raised immediately when any import statement is found in interpreter-executed code.

    Import statements are unconditionally forbidden in CaMeL-executed P-LLM
    plans (M4-F10).  The check is applied at the AST level before any
    statement is executed, ensuring no side-effects occur prior to rejection.

    Attributes
    ----------
    module_name:
        The module name extracted from the offending ``import`` or
        ``from … import`` statement.  For bare ``import os`` this is
        ``"os"``; for ``from os import path`` this is ``"os"``.
    lineno:
        1-based source line number of the offending import statement,
        taken from the AST node.
    """

    module_name: str
    lineno: int

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"Import statements are forbidden in CaMeL-executed code: "
            f"'import {self.module_name}' at line {self.lineno} (M4-F10)"
        )


@dataclass
class ForbiddenNameError(NameError):
    """Raised when a name is accessed that is not in the interpreter's allowed namespace.

    Triggered in the interpreter's name-lookup path when a name is not found
    in the variable store, registered tools, or the permitted-builtins
    namespace loaded from ``allowlist.yaml`` (M4-F14).

    Subclasses :class:`NameError` so existing code that catches ``NameError``
    continues to work.

    Attributes
    ----------
    offending_name:
        The exact identifier string from the ``ast.Name`` node.
    lineno:
        1-based source line number of the name access.  Zero when no line
        information is available on the node.

    Notes
    -----
    The field is named ``offending_name`` rather than ``name`` to avoid
    conflicting with the C-level ``name`` slot on :class:`NameError`, which
    defaults to ``None`` and is not controlled by the dataclass ``__init__``.
    """

    offending_name: str
    lineno: int

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"Name {self.offending_name!r} is not permitted in CaMeL-executed code "
            f"at line {self.lineno} (M4-F14)"
        )


class ConfigurationSecurityError(RuntimeError):
    """Raised at interpreter startup when ``allowlist.yaml`` is misconfigured.

    The loader raises this error when the ``review_gate`` section is missing
    or incomplete while ``review_required`` is ``true``, preventing the
    interpreter from running in a state where the allowed-name set has not
    been security-reviewed (M4-F13).

    The error message describes the specific violation so operators can
    correct the configuration before restarting.
    """
