"""CaMeL observability package.

Provides Prometheus/OpenTelemetry-compatible metrics collection and
structured JSON audit log sinks for the CaMeL security layer.

Submodules
----------
:mod:`camel.observability.metrics`
    Five named metrics (policy denial rate, Q-LLM error rate, P-LLM retry
    histogram, task success rate, consent prompt rate) with Prometheus text
    format export and optional OTLP push.

:mod:`camel.observability.audit_sink`
    Structured JSON audit log with configurable sink (``file``, ``stdout``,
    ``external`` HTTP POST) controlled via the ``CAMEL_AUDIT_SINK`` environment
    variable or a programmatic :class:`~camel.observability.audit_sink.AuditSinkConfig`.
"""

from camel.observability.audit_sink import (
    AuditLogRecord,
    AuditSink,
    AuditSinkConfig,
    SinkMode,
    get_default_sink,
)
from camel.observability.metrics import (
    CamelMetricsCollector,
    get_global_collector,
    start_metrics_server,
)

__all__ = [
    # Metrics
    "CamelMetricsCollector",
    "get_global_collector",
    "start_metrics_server",
    # Audit sink
    "AuditLogRecord",
    "AuditSink",
    "AuditSinkConfig",
    "SinkMode",
    "get_default_sink",
]
