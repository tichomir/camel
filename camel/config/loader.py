"""CaMeL allowlist loader — reads and validates ``allowlist.yaml``.

This module is the runtime interface for the ``camel/config/allowlist.yaml``
configuration file.  It is responsible for:

- Parsing the YAML file.
- Validating the parsed structure (Pydantic models).
- Enforcing the security review gate (M4-F13).
- Caching the parsed config at module level so repeated interpreter
  construction does not re-read the file.
- Exposing a convenient ``build_permitted_namespace()`` helper that maps
  permitted builtin names to their Python callables.

All validation failures raise :class:`~camel.exceptions.ConfigurationSecurityError`,
preventing the interpreter from starting in a misconfigured state.

Usage
-----
::

    from camel.config.loader import load_allowlist, build_permitted_namespace

    config = load_allowlist()           # reads from the default path
    ns = build_permitted_namespace()    # ready-to-inject builtins dict
"""

from __future__ import annotations

import builtins as _builtins_module
import os
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

from camel.exceptions import ConfigurationSecurityError

# ---------------------------------------------------------------------------
# Default allowlist path
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWLIST_PATH = os.path.join(os.path.dirname(__file__), "allowlist.yaml")

# ---------------------------------------------------------------------------
# Pydantic models for schema validation
# ---------------------------------------------------------------------------


class ReviewGate(BaseModel):
    """Security review gate metadata from the YAML ``review_gate`` section."""

    last_reviewed: str
    reviewers: list[str]
    review_required: bool = True

    @field_validator("reviewers")
    @classmethod
    def reviewers_not_empty(cls, v: list[str]) -> list[str]:
        """Ensure at least one reviewer is listed."""
        if not v:
            raise ValueError("review_gate.reviewers must not be empty")
        return v

    @field_validator("last_reviewed")
    @classmethod
    def last_reviewed_not_empty(cls, v: str) -> str:
        """Ensure the last-reviewed date is not blank."""
        if not v.strip():
            raise ValueError("review_gate.last_reviewed must not be empty")
        return v


class PermittedBuiltinEntry(BaseModel):
    """A single entry in the ``permitted_builtins`` list."""

    name: str
    risk_level: str
    justification: str


class ExcludedTimingEntry(BaseModel):
    """A single entry in the ``excluded_timing_names`` list."""

    name: str
    category: str
    rationale: str


class AllowlistConfig(BaseModel):
    """Full schema for ``allowlist.yaml``."""

    review_gate: ReviewGate
    permitted_builtins: list[PermittedBuiltinEntry]
    excluded_timing_names: list[ExcludedTimingEntry]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_allowlist(path: str = _DEFAULT_ALLOWLIST_PATH) -> AllowlistConfig:
    """Parse and validate ``allowlist.yaml``, raising on any violation.

    The result is cached so that repeated calls (one per interpreter
    construction) do not repeatedly hit the file system.

    Parameters
    ----------
    path:
        Absolute or relative path to the YAML file.  Defaults to the
        bundled ``camel/config/allowlist.yaml``.

    Returns
    -------
    AllowlistConfig
        Fully validated configuration object.

    Raises
    ------
    ConfigurationSecurityError
        If the file is missing, unparseable, fails Pydantic validation,
        or the security review gate is not satisfied.
    FileNotFoundError
        Propagated as-is if the YAML file does not exist at *path*.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw: Any = yaml.safe_load(fh)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ConfigurationSecurityError(
            f"Failed to parse allowlist.yaml at {path!r}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationSecurityError(
            f"allowlist.yaml must be a YAML mapping; got {type(raw).__name__}"
        )

    try:
        config = AllowlistConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationSecurityError(f"allowlist.yaml failed schema validation: {exc}") from exc

    # M4-F13: enforce the review gate when review_required is True.
    gate = config.review_gate
    if gate.review_required:
        if not gate.last_reviewed.strip():
            raise ConfigurationSecurityError(
                "allowlist.yaml security review gate violation: "
                "review_gate.last_reviewed is empty but review_required=true"
            )
        if not gate.reviewers:
            raise ConfigurationSecurityError(
                "allowlist.yaml security review gate violation: "
                "review_gate.reviewers is empty but review_required=true"
            )

    # Sanity-check: no name should appear in both sections.
    permitted_names = frozenset(e.name for e in config.permitted_builtins)
    excluded_names = frozenset(e.name for e in config.excluded_timing_names)
    overlap = permitted_names & excluded_names
    if overlap:
        raise ConfigurationSecurityError(
            f"allowlist.yaml integrity violation: names appear in both "
            f"permitted_builtins and excluded_timing_names: {sorted(overlap)}"
        )

    return config


def get_permitted_names(path: str = _DEFAULT_ALLOWLIST_PATH) -> frozenset[str]:
    """Return the frozenset of permitted builtin names from the allowlist.

    Parameters
    ----------
    path:
        Path to the allowlist YAML (forwarded to :func:`load_allowlist`).

    Returns
    -------
    frozenset[str]
        Names listed under ``permitted_builtins``.
    """
    config = load_allowlist(path)
    return frozenset(e.name for e in config.permitted_builtins)


def get_excluded_timing_names(
    path: str = _DEFAULT_ALLOWLIST_PATH,
) -> frozenset[str]:
    """Return the frozenset of excluded timing-primitive names.

    Parameters
    ----------
    path:
        Path to the allowlist YAML (forwarded to :func:`load_allowlist`).

    Returns
    -------
    frozenset[str]
        Names listed under ``excluded_timing_names``.
    """
    config = load_allowlist(path)
    return frozenset(e.name for e in config.excluded_timing_names)


def build_permitted_namespace(
    path: str = _DEFAULT_ALLOWLIST_PATH,
) -> dict[str, Any]:
    """Build a restricted builtins namespace from the allowlist.

    Resolves each name in ``permitted_builtins`` against Python's built-in
    namespace (``vars(builtins)``).  Names that are not found in the built-in
    namespace are silently skipped (they may be injected as CaMeL tools
    instead).

    As a defence-in-depth measure, any name in ``excluded_timing_names``
    is explicitly removed from the result after construction (M4-F12).

    Parameters
    ----------
    path:
        Path to the allowlist YAML (forwarded to :func:`load_allowlist`).

    Returns
    -------
    dict[str, Any]
        Mapping from permitted name to the Python callable/type.  Contains
        exactly the names that are both listed in ``permitted_builtins`` and
        present in Python's built-in namespace.

    Raises
    ------
    ConfigurationSecurityError
        Propagated from :func:`load_allowlist` on any configuration error.
    """
    config = load_allowlist(path)
    all_builtins = vars(_builtins_module)
    excluded = frozenset(e.name for e in config.excluded_timing_names)

    namespace: dict[str, Any] = {}
    for entry in config.permitted_builtins:
        name = entry.name
        if name in excluded:
            # Should not happen due to overlap check in load_allowlist,
            # but guard defensively.
            continue
        if name in all_builtins:
            namespace[name] = all_builtins[name]

    # Defence-in-depth: remove any excluded name that might have slipped in.
    for excluded_name in excluded:
        namespace.pop(excluded_name, None)

    return namespace
