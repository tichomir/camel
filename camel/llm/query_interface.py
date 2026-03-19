"""Interface stubs for the interpreter-callable Q-LLM extraction function.

This module defines the ``query_quarantined_llm(prompt, output_schema)``
interface that the CaMeL interpreter exposes to P-LLM-generated code, along
with the ``augment_schema_with_hei`` helper that dynamically injects the
``have_enough_information: bool`` sentinel field into any caller-supplied
:class:`~pydantic.BaseModel` without mutating the original class.

Design
------
The interface accepts *any* ``BaseModel`` subclass — callers need not inherit
from :class:`~camel.llm.schemas.QResponse`.  The ``have_enough_information``
field is injected transparently via :func:`pydantic.create_model` each time an
unaugmented schema is passed.  Schemas that already carry the field (i.e.
``QResponse`` subclasses) pass through the idempotency check unchanged.

See ADR-006 for rationale and the full schema validation pipeline.

Isolation guarantee
-------------------
The callable returned by :func:`make_query_quarantined_llm` has **no**
``tools`` or ``functions`` parameter.  The underlying backend call is made via
:class:`~camel.llm.protocols.QlLMBackend.structured_complete`, which
structurally prohibits tool definitions (see ADR-001 §4 and ADR-006 §5).

Usage (interpreter-side)
------------------------
.. code-block:: python

    # Registered in the interpreter tool namespace by the orchestrator:
    from camel.llm.adapters import ClaudeBackend
    from camel.llm.query_interface import make_query_quarantined_llm

    backend = ClaudeBackend(model="claude-haiku-4-5-20251001")
    query_quarantined_llm = make_query_quarantined_llm(backend)

    # P-LLM-generated code then calls it as a plain tool:
    #   result = query_quarantined_llm(raw_email, EmailExtraction)

Usage (schema definition — plain BaseModel)
-------------------------------------------
.. code-block:: python

    from pydantic import BaseModel

    class EmailExtraction(BaseModel):
        sender: str
        subject: str

    # have_enough_information is injected automatically — no QResponse needed.

Usage (schema definition — QResponse subclass, still works)
------------------------------------------------------------
.. code-block:: python

    from camel.llm.schemas import QResponse

    class EmailExtraction(QResponse):
        sender: str
        subject: str

    # Idempotency check: have_enough_information already present → no-op.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from typing import TYPE_CHECKING, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, Field, create_model

from camel.llm.exceptions import NotEnoughInformationError
from camel.llm.schemas import QResponse
from camel.value import CaMeLValue, wrap

if TYPE_CHECKING:
    from camel.llm.protocols import QlLMBackend

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Name of the sentinel field injected into every augmented schema.
_HEI_FIELD_NAME: str = "have_enough_information"

#: Shared thread pool reused across all Q-LLM calls to avoid per-call
#: executor creation/teardown overhead.  Bounded to avoid resource exhaustion
#: under concurrent load.  Size defaults to min(32, cpu_count + 4) which
#: matches the stdlib ThreadPoolExecutor default, but can be overridden via
#: the ``CAMEL_QLLM_THREAD_POOL_SIZE`` environment variable.
_QLLM_EXECUTOR: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(
        os.environ.get(
            "CAMEL_QLLM_THREAD_POOL_SIZE",
            min(32, (os.cpu_count() or 1) + 4),
        )
    )
)

#: Description embedded in the injected field's JSON Schema representation.
_HEI_FIELD_DESCRIPTION: str = (
    "Set to True only when the model has sufficient information to populate "
    "all other fields with meaningful values. When False, the caller MUST "
    "raise NotEnoughInformationError and MUST NOT forward any field values "
    "to the P-LLM."
)


# ---------------------------------------------------------------------------
# Dynamic schema augmentation
# ---------------------------------------------------------------------------


def augment_schema_with_hei(schema: type[T]) -> type[T]:
    """Return *schema* augmented with a ``have_enough_information: bool`` field.

    Parameters
    ----------
    schema:
        Any :class:`~pydantic.BaseModel` subclass describing the caller's
        desired extraction output.  If ``have_enough_information`` is already
        declared in ``schema.model_fields`` (e.g. the caller passed a
        :class:`~camel.llm.schemas.QResponse` subclass), *schema* is returned
        **unchanged** — no copy or subclass is created.

    Returns
    -------
    type[T]
        A new dynamically-constructed model class that:

        * Is a strict subclass of *schema* (``issubclass(result, schema)``).
        * Carries ``have_enough_information: bool`` as a required field.
        * Inherits all field validators and constraints from *schema*.
        * Has the same ``__name__`` as *schema* (for prompt readability).
        * Does **not** mutate the original *schema* class.

    Notes
    -----
    The returned class is created via :func:`pydantic.create_model`.  For
    hot paths, consider memoising the result with ``functools.lru_cache``
    keyed on the original schema class to avoid repeated class creation.

    Examples
    --------
    .. code-block:: python

        from pydantic import BaseModel
        from camel.llm.query_interface import augment_schema_with_hei

        class ProductInfo(BaseModel):
            name: str
            price: float

        Augmented = augment_schema_with_hei(ProductInfo)
        assert "have_enough_information" in Augmented.model_fields
        assert issubclass(Augmented, ProductInfo)
        # Original class is unchanged:
        assert "have_enough_information" not in ProductInfo.model_fields
    """
    if _HEI_FIELD_NAME in schema.model_fields:
        return schema

    augmented: type[T] = create_model(  # type: ignore[call-overload]
        schema.__name__,
        __base__=schema,
        **{
            _HEI_FIELD_NAME: (
                bool,
                Field(..., description=_HEI_FIELD_DESCRIPTION),
            )
        },
    )
    return augmented


# ---------------------------------------------------------------------------
# Callable protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class QueryQLLMCallable(Protocol):
    """Protocol for the ``query_quarantined_llm`` callable.

    The CaMeL interpreter registers an instance satisfying this protocol in
    its tool namespace under the name ``query_quarantined_llm``.  P-LLM-
    generated code may then invoke it as a plain tool::

        result = query_quarantined_llm(
            "Extract sender and subject from this email.",
            EmailExtraction,
        )

    Isolation contract
    ------------------
    Implementations MUST satisfy all Q-LLM isolation invariants:

    1. The underlying LLM call has **no** ``tools``, ``functions``, or
       ``tool_choice`` parameter.
    2. The LLM output is validated against the augmented schema (the one with
       ``have_enough_information`` injected) before being returned or raising.
    3. :class:`~camel.llm.exceptions.NotEnoughInformationError` is raised —
       never swallowed — when ``have_enough_information`` is ``False``.
    4. The returned value is a validated ``T`` instance matching the
       *caller's* original ``output_schema``, not the augmented subclass.
    5. The caller (interpreter) is responsible for tagging the returned value
       as an untrusted :class:`~camel.value.CaMeLValue` before passing any
       field into a P-LLM context.
    """

    def __call__(
        self,
        prompt: str,
        output_schema: type[T],
    ) -> T:
        """Execute a Q-LLM structured extraction call.

        Parameters
        ----------
        prompt:
            The extraction instruction, optionally combined with or followed
            by the untrusted content to parse.  Must be a plain ``str``; the
            caller must not embed live :class:`~camel.value.CaMeLValue`
            instances here (they would carry untrusted data into the prompt).
        output_schema:
            Any :class:`~pydantic.BaseModel` subclass specifying the expected
            output shape.  Need not inherit from
            :class:`~camel.llm.schemas.QResponse`; ``have_enough_information``
            is injected dynamically via :func:`augment_schema_with_hei`.

        Returns
        -------
        T
            A validated instance of *output_schema* (the caller's original
            class, **not** the augmented subclass) with all declared fields
            populated.  The ``have_enough_information`` field is stripped from
            the returned value.

        Raises
        ------
        NotEnoughInformationError
            When the model sets ``have_enough_information = False``.  The
            exception message is a fixed string and contains no untrusted
            field values.
        pydantic.ValidationError
            When the backend response does not conform to the augmented schema
            (missing required fields, wrong types, extra fields forbidden by
            ``model_config["extra"] = "forbid"``).
        ValueError
            When the backend returns structurally invalid data (e.g. raw
            string instead of a dict) that cannot be parsed by Pydantic at
            all.
        """
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_query_quarantined_llm(backend: QlLMBackend) -> QueryQLLMCallable:
    """Create a ``query_quarantined_llm`` callable bound to *backend*.

    The returned callable is intended to be registered in the CaMeL
    interpreter's tool namespace so P-LLM-generated code can call it as::

        result = query_quarantined_llm(prompt, OutputSchema)

    Parameters
    ----------
    backend:
        A :class:`~camel.llm.protocols.QlLMBackend` implementation.  The
        factory never forwards tool definitions to this backend; isolation is
        enforced structurally by the ``QlLMBackend.structured_complete``
        protocol.

    Returns
    -------
    QueryQLLMCallable
        An async callable satisfying :class:`QueryQLLMCallable`.

    Implementation contract
    -----------------------
    The concrete implementation returned by this factory MUST:

    1. Call :func:`augment_schema_with_hei` on *output_schema* before
       invoking the backend.
    2. Call ``augmented_schema.model_validate(raw_response)`` to validate
       the backend's return value — never use ``model_construct()`` or skip
       validation.
    3. Raise :class:`~camel.llm.exceptions.NotEnoughInformationError` with
       ``schema_type=output_schema`` when
       ``validated.have_enough_information is False``.
    4. Return ``output_schema.model_validate(validated.model_dump(exclude={
       _HEI_FIELD_NAME}))`` so the caller receives a clean ``T`` instance
       matching their original schema declaration.

    Notes
    -----
    The ``have_enough_information`` field is *excluded* from the final
    ``model_dump`` before re-validating against ``output_schema``.  This
    prevents ``ValidationError`` when ``output_schema`` does not declare the
    field (i.e. it is a plain ``BaseModel``, not a ``QResponse`` subclass).

    Example
    -------
    .. code-block:: python

        from camel.llm.adapters import ClaudeBackend
        from camel.llm.query_interface import make_query_quarantined_llm

        backend = ClaudeBackend(model="claude-haiku-4-5-20251001")
        query_quarantined_llm = make_query_quarantined_llm(backend)

        # Register in interpreter namespace:
        interpreter.register_tool("query_quarantined_llm", query_quarantined_llm)
    """
    from camel.llm.qllm import QLLMWrapper

    wrapper = QLLMWrapper(backend)

    async def _query_async(prompt: str, output_schema: type[T]) -> T:
        """Async implementation of :class:`QueryQLLMCallable`."""
        augmented = augment_schema_with_hei(output_schema)

        # If the schema is already a QResponse subclass, augmented == output_schema.
        # In both cases we delegate to QLLMWrapper which calls model_validate
        # and raises NotEnoughInformationError on have_enough_information=False.
        if issubclass(augmented, QResponse):
            # Fast path: augmented schema is a QResponse subclass; QLLMWrapper
            # handles validation and NotEnoughInformationError natively.
            qresponse_schema: type[QResponse] = augmented
            validated_qresponse = await wrapper.extract(prompt, qresponse_schema)
            # Strip have_enough_information before returning the plain T instance.
            if output_schema is augmented:
                # Caller used QResponse subclass — return as-is (T includes hei).
                return validated_qresponse  # type: ignore[return-value]
            raw = validated_qresponse.model_dump(exclude={_HEI_FIELD_NAME})
            return output_schema.model_validate(raw)

        # Slow path: augmented is a dynamically created BaseModel subclass.
        # Build a synthetic QResponse wrapper to satisfy QLLMWrapper typing.
        # Instead, validate directly using the augmented schema.

        messages = wrapper._build_messages(prompt, augmented)  # type: ignore[arg-type]
        raw_result = await backend.structured_complete(  # type: ignore[type-var]
            messages, augmented
        )
        validated = augmented.model_validate(raw_result)

        if not validated.have_enough_information:  # type: ignore[attr-defined]
            # schema_type expected to be QResponse subclass; use augmented as proxy.
            raise NotEnoughInformationError(
                schema_type=augmented,  # type: ignore[arg-type]
                partial_response=validated,  # type: ignore[arg-type]
            )

        raw = validated.model_dump(exclude={_HEI_FIELD_NAME})
        return output_schema.model_validate(raw)

    def _query(prompt: str, output_schema: type[T]) -> T:
        """Synchronous bound implementation of :class:`QueryQLLMCallable`.

        The interpreter calls tools synchronously.  When the orchestrator's
        async event loop is already running we cannot use ``asyncio.run()``
        directly (it raises ``RuntimeError: This event loop is already
        running``).  Instead we execute the coroutine in a worker thread from
        the module-level shared :data:`_QLLM_EXECUTOR` pool.  Reusing the
        pool avoids the per-call overhead of creating and tearing down a
        ``ThreadPoolExecutor`` and an asyncio event loop on every invocation.

        The Pydantic result is wrapped in a :class:`~camel.value.CaMeLValue`
        tagged with ``sources={"query_quarantined_llm"}`` so the interpreter's
        type assertion and M4-F3/F4 post-Q-LLM taint propagation work
        correctly.  Attribute access on the result (e.g. ``result.text``) is
        handled by the interpreter's ``_eval_Attribute`` path which re-wraps
        each field with propagated capability tags.
        """
        future = _QLLM_EXECUTOR.submit(asyncio.run, _query_async(prompt, output_schema))
        pydantic_result: T = future.result()
        return cast(
            T,
            wrap(
                pydantic_result,
                sources=frozenset({"query_quarantined_llm"}),
                readers=frozenset(),
            ),
        )

    return _query
