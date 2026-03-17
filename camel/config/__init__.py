# camel/config — interpreter security configuration package.
"""CaMeL interpreter security configuration.

Exports the allowlist loader utilities for use by the interpreter and tests.
"""

from camel.config.loader import (
    AllowlistConfig,
    build_permitted_namespace,
    get_excluded_timing_names,
    get_permitted_names,
    load_allowlist,
)

__all__ = [
    "AllowlistConfig",
    "build_permitted_namespace",
    "get_excluded_timing_names",
    "get_permitted_names",
    "load_allowlist",
]
