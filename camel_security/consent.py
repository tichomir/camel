"""camel_security.consent — production consent UX for CaMeL policy denials.

This module is part of the **stable public API** of ``camel-security``.  It
re-exports the consent components implemented in :mod:`camel.consent` so that
consumer code only needs to depend on the versioned ``camel-security``
namespace.

Quick start
-----------
.. code-block:: python

    from camel.interpreter import CaMeLInterpreter, EnforcementMode
    from camel_security.consent import (
        DefaultCLIConsentHandler,
        ConsentDecisionCache,
        ConsentDecision,
        ConsentAuditEntry,
    )

    handler = DefaultCLIConsentHandler()
    cache = ConsentDecisionCache()

    interp = CaMeLInterpreter(
        tools=my_tools,
        policy_engine=my_registry,
        enforcement_mode=EnforcementMode.PRODUCTION,
        consent_handler=handler,
        consent_cache=cache,
    )

    # After execution, inspect the consent audit log:
    for entry in interp.consent_audit_log:
        print(entry.tool_name, entry.decision.value, entry.session_cache_hit)

Custom handler
--------------
Subclass :class:`ConsentHandler` to implement a web UI dialog, async
approval workflow, or mobile push notification::

    from camel_security.consent import ConsentHandler, ConsentDecision

    class MyWebConsentHandler(ConsentHandler):
        def handle_consent(
            self,
            tool_name: str,
            argument_summary: str,
            denial_reason: str,
        ) -> ConsentDecision:
            # Call your web API, block until approved/rejected, return decision
            ...

Exported names
--------------
:class:`ConsentDecision`
    Enum of consent outcomes: ``APPROVE``, ``REJECT``, ``APPROVE_FOR_SESSION``.

:class:`ConsentAuditEntry`
    Immutable audit record written to
    :attr:`~camel.interpreter.CaMeLInterpreter.consent_audit_log` on every
    consent prompt (including cache hits).

:class:`ConsentHandler`
    ABC defining the :meth:`~ConsentHandler.handle_consent` extension point.

:class:`DefaultCLIConsentHandler`
    Built-in CLI implementation: formatted stdout prompt + stdin input.

:class:`ConsentDecisionCache`
    Session-level cache for ``APPROVE_FOR_SESSION`` decisions.
"""

from camel.consent import (
    ConsentAuditEntry,
    ConsentDecision,
    ConsentDecisionCache,
    ConsentHandler,
    DefaultCLIConsentHandler,
)

__all__ = [
    "ConsentDecision",
    "ConsentAuditEntry",
    "ConsentHandler",
    "DefaultCLIConsentHandler",
    "ConsentDecisionCache",
]
