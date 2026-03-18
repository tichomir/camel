"""Tests that QLLMWrapper.extract enforces schema validation.

Verifies that malformed backend responses (e.g. raw dicts missing required
fields) are caught by QLLMWrapper.extract via explicit schema.model_validate()
and surfaced as pydantic.ValidationError rather than propagating silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from camel.llm.qllm import QLLMWrapper
from camel.llm.schemas import QResponse

# ---------------------------------------------------------------------------
# Test schema
# ---------------------------------------------------------------------------


class _PersonSchema(QResponse):
    """Minimal schema with two required fields beyond the base class."""

    name: str
    age: int


# ---------------------------------------------------------------------------
# Stub backends
# ---------------------------------------------------------------------------


class _MissingFieldsBackend:
    """Returns a dict that satisfies the base QResponse field only.

    This simulates a poorly-implemented adapter (or a model response) that
    omits required subclass fields.  The dict is returned instead of a
    validated Pydantic instance, mimicking the failure mode the fix addresses.
    """

    async def structured_complete(self, messages: list[Any], schema: type[Any]) -> dict[str, Any]:
        # Only the base field present; 'name' and 'age' are missing.
        return {"have_enough_information": True}


class _ValidBackend:
    """Returns a fully-populated, valid dict for the happy-path check."""

    async def structured_complete(self, messages: list[Any], schema: type[Any]) -> dict[str, Any]:
        return {"have_enough_information": True, "name": "Alice", "age": 30}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_extract_raises_validation_error_when_backend_omits_required_fields() -> None:
    """QLLMWrapper.extract must raise ValidationError when the backend returns
    a value that is missing required schema fields.

    Without the explicit schema.model_validate() call in extract(), this test
    would either raise AttributeError (dict has no attribute access) or
    silently return an incomplete object.  With the fix it raises
    pydantic.ValidationError, providing actionable field-level detail.
    """
    wrapper = QLLMWrapper(_MissingFieldsBackend())  # type: ignore[arg-type]

    with pytest.raises(ValidationError) as exc_info:
        asyncio.run(wrapper.extract("some raw content", _PersonSchema))

    errors = exc_info.value.errors()
    missing_fields = {str(e["loc"][0]) for e in errors if e["type"] == "missing"}
    assert "name" in missing_fields, f"Expected 'name' in missing fields, got: {errors}"
    assert "age" in missing_fields, f"Expected 'age' in missing fields, got: {errors}"


def test_extract_succeeds_when_backend_returns_complete_dict() -> None:
    """Happy-path: a dict with all required fields passes validation and is
    returned as a properly-typed _PersonSchema instance.
    """
    wrapper = QLLMWrapper(_ValidBackend())  # type: ignore[arg-type]

    result: _PersonSchema = asyncio.run(wrapper.extract("Alice is 30 years old.", _PersonSchema))

    assert isinstance(result, _PersonSchema)
    assert result.name == "Alice"
    assert result.age == 30
    assert result.have_enough_information is True
