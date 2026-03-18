"""Prometheus/OpenTelemetry-compatible metrics for CaMeL.

Five named metrics are exposed, all scoped by ``session_id``, ``tool_name``,
and ``policy_name`` labels where applicable:

``camel_policy_denial_rate``
    Counter incremented on every policy denial.
    Labels: ``session_id``, ``policy_name``, ``tool_name``.

``camel_qlm_error_rate``
    Counter incremented on every Q-LLM ``NotEnoughInformationError`` or
    backend error.
    Labels: ``session_id``, ``tool_name``.

``camel_pllm_retry_count_histogram``
    Histogram recording the number of P-LLM retries consumed per task.
    Labels: ``session_id``.
    Buckets: 0, 1, 2, 3, 5, 7, 10.

``camel_task_success_rate``
    Gauge tracking the rolling success rate (0.0 – 1.0) of completed tasks.
    Labels: ``session_id``.

``camel_consent_prompt_rate``
    Counter incremented each time a user consent prompt fires.
    Labels: ``session_id``, ``tool_name``.

Prometheus export
-----------------
:meth:`CamelMetricsCollector.get_metrics_text` returns a valid Prometheus
exposition text format string that can be served directly from the
``/metrics`` HTTP endpoint.

:func:`start_metrics_server` launches a lightweight HTTP server on the
specified port that serves this text on ``GET /metrics``.

If the optional ``prometheus_client`` package is installed, metrics are also
registered with it so external Prometheus scrapers work out of the box.

OpenTelemetry export
--------------------
When the ``CAMEL_OTEL_ENDPOINT`` environment variable is set to an OTLP HTTP
endpoint (e.g. ``http://localhost:4318``), metric snapshots are pushed via
OTLP/HTTP every :attr:`CamelMetricsCollector.otel_push_interval_seconds`
seconds (default: 15).  Requires ``opentelemetry-sdk`` and
``opentelemetry-exporter-otlp-proto-http`` to be installed.

Thread safety
-------------
All counter/histogram/gauge operations use a :class:`threading.Lock` so the
collector is safe for concurrent use across multiple ``agent.run()`` sessions.

Examples
--------
Basic usage::

    from camel.observability.metrics import get_global_collector

    collector = get_global_collector()
    collector.record_policy_denial(
        session_id="s1", policy_name="send_email_policy", tool_name="send_email"
    )
    print(collector.get_metrics_text())

Launching the HTTP endpoint::

    from camel.observability.metrics import start_metrics_server
    start_metrics_server(port=9090)
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

__all__ = [
    "CamelMetricsCollector",
    "get_global_collector",
    "start_metrics_server",
]

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

_LabelKey = tuple[str, ...]  # frozen ordered label value tuple


@dataclass
class _Counter:
    """Thread-safe counter with labelled dimensions."""

    name: str
    help_text: str
    label_names: tuple[str, ...]
    _values: dict[_LabelKey, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, labels: dict[str, str], amount: float = 1.0) -> None:
        """Increment the counter for the given label combination."""
        key = tuple(labels.get(n, "") for n in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def snapshot(self) -> dict[_LabelKey, float]:
        """Return a snapshot of all label-value pairs."""
        with self._lock:
            return dict(self._values)

    def to_prometheus_text(self) -> str:
        """Serialise to Prometheus exposition format."""
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        for key, value in self.snapshot().items():
            label_str = _format_labels(self.label_names, key)
            lines.append(f"{self.name}{{{label_str}}} {value:.1f}")
        if not self.snapshot():
            lines.append(f"{self.name} 0.0")
        return "\n".join(lines)


@dataclass
class _Histogram:
    """Thread-safe histogram with labelled dimensions."""

    name: str
    help_text: str
    label_names: tuple[str, ...]
    buckets: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, float("inf"))
    _counts: dict[_LabelKey, list[int]] = field(default_factory=dict)
    _sums: dict[_LabelKey, float] = field(default_factory=dict)
    _total_counts: dict[_LabelKey, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float, labels: dict[str, str]) -> None:
        """Record an observation for the given label combination."""
        key = tuple(labels.get(n, "") for n in self.label_names)
        with self._lock:
            if key not in self._counts:
                self._counts[key] = [0] * len(self.buckets)
                self._sums[key] = 0.0
                self._total_counts[key] = 0
            for i, upper in enumerate(self.buckets):
                if value <= upper:
                    self._counts[key][i] += 1
            self._sums[key] += value
            self._total_counts[key] += 1

    def snapshot(self) -> dict[_LabelKey, tuple[list[int], float, int]]:
        """Return a snapshot: {key: (bucket_counts, sum, total_count)}."""
        with self._lock:
            return {
                k: (list(self._counts[k]), self._sums[k], self._total_counts[k])
                for k in self._counts
            }

    def to_prometheus_text(self) -> str:
        """Serialise to Prometheus exposition format."""
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        for key, (bucket_counts, total_sum, total_count) in self.snapshot().items():
            base_labels = _format_labels(self.label_names, key)
            base_prefix = f"{{{base_labels}, " if base_labels else "{"
            for i, upper in enumerate(self.buckets):
                le_val = "+Inf" if upper == float("inf") else str(int(upper))
                labels_str = f'{base_prefix}le="{le_val}"}}'
                if not base_labels:
                    labels_str = f'{{le="{le_val}"}}'
                else:
                    labels_str = f'{{{base_labels}, le="{le_val}"}}'
                lines.append(f"{self.name}_bucket{labels_str} {bucket_counts[i]}")
            label_str = f"{{{base_labels}}}" if base_labels else ""
            lines.append(f"{self.name}_sum{label_str} {total_sum:.1f}")
            lines.append(f"{self.name}_count{label_str} {total_count}")
        if not self.snapshot():
            lines.append(f'{self.name}_bucket{{le="+Inf"}} 0')
            lines.append(f"{self.name}_sum 0.0")
            lines.append(f"{self.name}_count 0")
        return "\n".join(lines)


@dataclass
class _Gauge:
    """Thread-safe gauge with labelled dimensions."""

    name: str
    help_text: str
    label_names: tuple[str, ...]
    _values: dict[_LabelKey, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, value: float, labels: dict[str, str]) -> None:
        """Set the gauge for the given label combination."""
        key = tuple(labels.get(n, "") for n in self.label_names)
        with self._lock:
            self._values[key] = value

    def snapshot(self) -> dict[_LabelKey, float]:
        """Return a snapshot of all label-value pairs."""
        with self._lock:
            return dict(self._values)

    def to_prometheus_text(self) -> str:
        """Serialise to Prometheus exposition format."""
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
        ]
        for key, value in self.snapshot().items():
            label_str = _format_labels(self.label_names, key)
            lines.append(f"{self.name}{{{label_str}}} {value:.4f}")
        if not self.snapshot():
            lines.append(f"{self.name} 0.0000")
        return "\n".join(lines)


def _format_labels(names: tuple[str, ...], values: _LabelKey) -> str:
    """Return a Prometheus label string e.g. ``session_id="s1",tool_name="echo"``."""
    return ", ".join(f'{n}="{v}"' for n, v in zip(names, values) if v)


# ---------------------------------------------------------------------------
# CamelMetricsCollector
# ---------------------------------------------------------------------------


class CamelMetricsCollector:
    """Thread-safe collector for all CaMeL operational metrics.

    Maintains in-memory state for five named metrics and exposes them in
    Prometheus text exposition format via :meth:`get_metrics_text`.  When
    ``prometheus_client`` is installed, also registers the metrics with it
    so standard Prometheus scrapers work without any additional plumbing.

    The :func:`get_global_collector` function returns a process-wide singleton
    instance.  Create a fresh instance for isolated test scenarios.

    Parameters
    ----------
    otel_push_interval_seconds:
        Interval in seconds between OTLP metric pushes when
        ``CAMEL_OTEL_ENDPOINT`` is configured.  Default: ``15``.
    """

    def __init__(self, otel_push_interval_seconds: int = 15) -> None:
        """Initialise all five metric instruments."""
        self.otel_push_interval_seconds = otel_push_interval_seconds

        self._policy_denial_rate = _Counter(
            name="camel_policy_denial_rate",
            help_text=(
                "Total number of security policy denials, labelled by session, policy, and tool."
            ),
            label_names=("session_id", "policy_name", "tool_name"),
        )
        self._qlm_error_rate = _Counter(
            name="camel_qlm_error_rate",
            help_text=(
                "Total number of Q-LLM errors (NotEnoughInformationError "
                "and backend failures), labelled by session and tool."
            ),
            label_names=("session_id", "tool_name"),
        )
        self._pllm_retry_count = _Histogram(
            name="camel_pllm_retry_count_histogram",
            help_text=(
                "Distribution of P-LLM planning retry counts per task, labelled by session."
            ),
            label_names=("session_id",),
            buckets=(0.0, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, float("inf")),
        )
        self._task_success_rate = _Gauge(
            name="camel_task_success_rate",
            help_text=(
                "Rolling task success rate (0.0–1.0) for completed tasks, labelled by session."
            ),
            label_names=("session_id",),
        )
        self._consent_prompt_rate = _Counter(
            name="camel_consent_prompt_rate",
            help_text=("Total number of user consent prompts fired, labelled by session and tool."),
            label_names=("session_id", "tool_name"),
        )

        # Per-session task counters used for rolling success rate computation.
        self._task_totals: dict[str, int] = defaultdict(int)
        self._task_successes: dict[str, int] = defaultdict(int)
        self._task_lock = threading.Lock()

        # Background OTEL push thread (started lazily).
        self._otel_thread: threading.Thread | None = None
        self._otel_stop_event = threading.Event()

        # Start OTEL push loop if endpoint is configured.
        otel_endpoint = os.environ.get("CAMEL_OTEL_ENDPOINT", "")
        if otel_endpoint:
            self._start_otel_push(otel_endpoint)

    # ------------------------------------------------------------------
    # Public recording methods
    # ------------------------------------------------------------------

    def record_policy_denial(
        self,
        session_id: str,
        policy_name: str,
        tool_name: str,
    ) -> None:
        """Increment ``camel_policy_denial_rate`` for the given labels.

        Parameters
        ----------
        session_id:
            Opaque session identifier.
        policy_name:
            Name of the policy function that returned ``Denied``.
        tool_name:
            Name of the tool whose call was denied.
        """
        self._policy_denial_rate.inc(
            {"session_id": session_id, "policy_name": policy_name, "tool_name": tool_name}
        )

    def record_qlm_error(self, session_id: str, tool_name: str) -> None:
        """Increment ``camel_qlm_error_rate`` for the given labels.

        Parameters
        ----------
        session_id:
            Opaque session identifier.
        tool_name:
            Name of the tool whose Q-LLM call errored.
        """
        self._qlm_error_rate.inc({"session_id": session_id, "tool_name": tool_name})

    def record_pllm_retry(self, session_id: str, retry_count: int) -> None:
        """Observe a P-LLM retry count in ``camel_pllm_retry_count_histogram``.

        Parameters
        ----------
        session_id:
            Opaque session identifier.
        retry_count:
            Number of retries consumed (0 = first plan succeeded).
        """
        self._pllm_retry_count.observe(float(retry_count), {"session_id": session_id})

    def record_task_completion(self, session_id: str, success: bool) -> None:
        """Update ``camel_task_success_rate`` gauge for *session_id*.

        Parameters
        ----------
        session_id:
            Opaque session identifier.
        success:
            ``True`` if the task completed successfully; ``False`` otherwise.
        """
        with self._task_lock:
            self._task_totals[session_id] += 1
            if success:
                self._task_successes[session_id] += 1
            total = self._task_totals[session_id]
            rate = self._task_successes[session_id] / total if total else 0.0
        self._task_success_rate.set(rate, {"session_id": session_id})

    def record_consent_prompt(self, session_id: str, tool_name: str) -> None:
        """Increment ``camel_consent_prompt_rate`` for the given labels.

        Parameters
        ----------
        session_id:
            Opaque session identifier.
        tool_name:
            Name of the tool that triggered the consent prompt.
        """
        self._consent_prompt_rate.inc({"session_id": session_id, "tool_name": tool_name})

    # ------------------------------------------------------------------
    # Prometheus export
    # ------------------------------------------------------------------

    def get_metrics_text(self) -> str:
        """Return all metrics in Prometheus text exposition format.

        The returned string is valid for direct use as the HTTP response body
        of a ``GET /metrics`` endpoint.

        Returns
        -------
        str
            Prometheus text format string with HELP, TYPE, and value lines
            for all five metrics.
        """
        blocks = [
            self._policy_denial_rate.to_prometheus_text(),
            self._qlm_error_rate.to_prometheus_text(),
            self._pllm_retry_count.to_prometheus_text(),
            self._task_success_rate.to_prometheus_text(),
            self._consent_prompt_rate.to_prometheus_text(),
        ]
        return "\n".join(blocks) + "\n"

    # ------------------------------------------------------------------
    # OTEL export (optional)
    # ------------------------------------------------------------------

    def _start_otel_push(self, endpoint: str) -> None:
        """Start a background thread that pushes metrics via OTLP/HTTP.

        The thread runs until :meth:`stop_otel_push` is called or the process
        exits.  It calls :meth:`_push_otel_snapshot` every
        :attr:`otel_push_interval_seconds` seconds.

        When ``opentelemetry-sdk`` is not installed, the fallback
        ``urllib``-based push is used instead.  The fallback produces a
        standards-compliant OTLP/HTTP JSON payload (camelCase field names as
        required by the protobuf JSON mapping spec) that is compatible with the
        standard OpenTelemetry Collector.  A :class:`RuntimeWarning` is emitted
        to alert operators that the native SDK path is unavailable.

        Parameters
        ----------
        endpoint:
            Base OTLP/HTTP endpoint URL, e.g. ``http://localhost:4318``.
            The thread will POST to ``{endpoint}/v1/metrics``.
        """
        import importlib.util  # noqa: PLC0415
        import warnings  # noqa: PLC0415

        if importlib.util.find_spec("opentelemetry") is None:
            warnings.warn(
                "CAMEL_OTEL_ENDPOINT is set but the 'opentelemetry-sdk' and "
                "'opentelemetry-exporter-otlp-proto-http' packages are not "
                "installed.  CaMeL will fall back to a stdlib urllib-based "
                "OTLP/HTTP JSON push.  The fallback payload follows the "
                "standard OTLP JSON schema (camelCase field names) and is "
                "compatible with the OpenTelemetry Collector, but lacks "
                "resource attributes and exemplars.  Install "
                "'opentelemetry-sdk' and "
                "'opentelemetry-exporter-otlp-proto-http' to use the full "
                "SDK path.",
                RuntimeWarning,
                stacklevel=2,
            )

        self._otel_stop_event.clear()

        def _loop() -> None:
            while not self._otel_stop_event.wait(self.otel_push_interval_seconds):
                try:
                    self._push_otel_snapshot(endpoint)
                except Exception:  # pragma: no cover
                    pass  # Observability must never crash the agent

        self._otel_thread = threading.Thread(target=_loop, daemon=True, name="camel-otel-push")
        self._otel_thread.start()

    def stop_otel_push(self) -> None:
        """Stop the background OTEL push thread (if running).

        Safe to call multiple times or when no thread is running.
        """
        self._otel_stop_event.set()

    def _push_otel_snapshot(self, endpoint: str) -> None:
        """Push a current metrics snapshot to the OTLP/HTTP endpoint.

        Uses the ``opentelemetry-sdk`` and
        ``opentelemetry-exporter-otlp-proto-http`` packages when available;
        otherwise falls back to a minimal JSON-over-HTTP POST to
        ``{endpoint}/v1/metrics`` using the stdlib ``urllib``.

        Parameters
        ----------
        endpoint:
            Base OTLP endpoint URL.
        """
        try:
            self._push_otel_via_sdk(endpoint)
        except ImportError:
            self._push_otel_via_urllib(endpoint)

    def _push_otel_via_sdk(self, endpoint: str) -> None:  # pragma: no cover
        """Push via opentelemetry-sdk if available.

        Parameters
        ----------
        endpoint:
            Base OTLP endpoint URL.
        """
        from opentelemetry import metrics as _otel_metrics  # noqa: PLC0415, F401
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            InMemoryMetricReader,
        )

        exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("camel-security")

        # Replay current counter values into OTel instruments.
        policy_counter = meter.create_counter("camel_policy_denial_rate")
        for key, value in self._policy_denial_rate.snapshot().items():
            labels_dict = dict(zip(self._policy_denial_rate.label_names, key))
            policy_counter.add(int(value), labels_dict)

        qlm_counter = meter.create_counter("camel_qlm_error_rate")
        for key, value in self._qlm_error_rate.snapshot().items():
            labels_dict = dict(zip(self._qlm_error_rate.label_names, key))
            qlm_counter.add(int(value), labels_dict)

        consent_counter = meter.create_counter("camel_consent_prompt_rate")
        for key, value in self._consent_prompt_rate.snapshot().items():
            labels_dict = dict(zip(self._consent_prompt_rate.label_names, key))
            consent_counter.add(int(value), labels_dict)

        task_gauge = meter.create_gauge("camel_task_success_rate")
        for key, value in self._task_success_rate.snapshot().items():
            labels_dict = dict(zip(self._task_success_rate.label_names, key))
            task_gauge.set(value, labels_dict)

        metrics_data = reader.get_metrics_data()
        exporter.export(metrics_data)

    def _push_otel_via_urllib(self, endpoint: str) -> None:
        """Push a standards-compliant OTLP/HTTP JSON snapshot via urllib.

        Used as fallback when ``opentelemetry-sdk`` is not installed.  The
        payload follows the OTLP/HTTP JSON encoding specification (protobuf
        JSON mapping), using camelCase field names throughout so that any
        standard OTLP collector (e.g. the OpenTelemetry Collector) will
        accept it without rejecting the request with HTTP 400.

        OTLP JSON schema reference:
        https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding

        Field name mapping (proto → JSON):
        - ``resource_metrics``      → ``resourceMetrics``
        - ``scope_metrics``         → ``scopeMetrics``
        - ``data_points``           → ``dataPoints``
        - ``is_monotonic``          → ``isMonotonic``
        - ``aggregation_temporality`` → ``aggregationTemporality``
        - ``start_time_unix_nano``  → ``startTimeUnixNano`` (string, uint64)
        - ``time_unix_nano``        → ``timeUnixNano`` (string, uint64)
        - ``as_double``             → ``asDouble``
        - ``string_value``          → ``stringValue``

        Note: uint64 timestamp fields are encoded as decimal strings to avoid
        JavaScript/JSON integer precision loss.

        Parameters
        ----------
        endpoint:
            Base OTLP endpoint URL.
        """
        import json  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        # Encode timestamps as strings — OTLP JSON spec requires uint64 fields
        # to be serialised as decimal strings to avoid IEEE-754 precision loss.
        now_ns_str = str(int(time.time() * 1e9))

        # AGGREGATION_TEMPORALITY_CUMULATIVE = 2 (protobuf enum value)
        _CUMULATIVE = 2

        def _make_attrs(attrs: dict[str, str]) -> list[dict[str, Any]]:
            return [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]

        def _make_gauge_dp(value: float, attrs: dict[str, str]) -> dict[str, Any]:
            return {
                "attributes": _make_attrs(attrs),
                "asDouble": value,
                "startTimeUnixNano": now_ns_str,
                "timeUnixNano": now_ns_str,
            }

        def _make_sum_dp(value: float, attrs: dict[str, str]) -> dict[str, Any]:
            return {
                "attributes": _make_attrs(attrs),
                "asDouble": value,
                "startTimeUnixNano": now_ns_str,
                "timeUnixNano": now_ns_str,
            }

        metrics_list: list[dict[str, Any]] = []

        for key, val in self._policy_denial_rate.snapshot().items():
            attrs = dict(zip(self._policy_denial_rate.label_names, key))
            metrics_list.append(
                {
                    "name": "camel_policy_denial_rate",
                    "sum": {
                        "dataPoints": [_make_sum_dp(val, attrs)],
                        "aggregationTemporality": _CUMULATIVE,
                        "isMonotonic": True,
                    },
                }
            )

        for key, val in self._qlm_error_rate.snapshot().items():
            attrs = dict(zip(self._qlm_error_rate.label_names, key))
            metrics_list.append(
                {
                    "name": "camel_qlm_error_rate",
                    "sum": {
                        "dataPoints": [_make_sum_dp(val, attrs)],
                        "aggregationTemporality": _CUMULATIVE,
                        "isMonotonic": True,
                    },
                }
            )

        for key, val in self._task_success_rate.snapshot().items():
            attrs = dict(zip(self._task_success_rate.label_names, key))
            metrics_list.append(
                {
                    "name": "camel_task_success_rate",
                    "gauge": {"dataPoints": [_make_gauge_dp(val, attrs)]},
                }
            )

        for key, val in self._consent_prompt_rate.snapshot().items():
            attrs = dict(zip(self._consent_prompt_rate.label_names, key))
            metrics_list.append(
                {
                    "name": "camel_consent_prompt_rate",
                    "sum": {
                        "dataPoints": [_make_sum_dp(val, attrs)],
                        "aggregationTemporality": _CUMULATIVE,
                        "isMonotonic": True,
                    },
                }
            )

        # OTLP/HTTP JSON envelope — field names are camelCase per protobuf JSON
        # mapping.  This structure is accepted by the OpenTelemetry Collector
        # OTLP receiver and any other OTLP-compliant ingestion endpoint.
        payload: dict[str, Any] = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "scope": {"name": "camel-security"},
                            "metrics": metrics_list,
                        }
                    ],
                }
            ]
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{endpoint}/v1/metrics",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            _ = resp.read()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_global_collector: CamelMetricsCollector | None = None
_global_lock = threading.Lock()


def get_global_collector() -> CamelMetricsCollector:
    """Return the process-wide :class:`CamelMetricsCollector` singleton.

    Creates the instance on first call.  Thread-safe.

    Returns
    -------
    CamelMetricsCollector
        The shared global metrics collector.
    """
    global _global_collector
    if _global_collector is None:
        with _global_lock:
            if _global_collector is None:
                _global_collector = CamelMetricsCollector()
    return _global_collector


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def start_metrics_server(
    port: int = 9090,
    collector: CamelMetricsCollector | None = None,
) -> HTTPServer:
    """Start a background HTTP server serving ``GET /metrics``.

    The server runs in a daemon thread so it shuts down automatically when
    the main process exits.  Call :meth:`HTTPServer.shutdown` on the returned
    server object for explicit teardown.

    Parameters
    ----------
    port:
        TCP port to bind.  Default: ``9090``.
    collector:
        Metrics collector to serve.  Defaults to :func:`get_global_collector`.

    Returns
    -------
    http.server.HTTPServer
        The running server instance.

    Examples
    --------
    ::

        server = start_metrics_server(port=9090)
        # ... agent runs ...
        server.shutdown()
    """
    target_collector = collector or get_global_collector()

    class _Handler(BaseHTTPRequestHandler):
        """Minimal HTTP handler for the /metrics endpoint."""

        def do_GET(self) -> None:  # noqa: N802
            """Handle GET requests; serve metrics on /metrics."""
            if self.path == "/metrics":
                body = target_collector.get_metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN001
            """Suppress default request logging."""

    server = HTTPServer(("", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="camel-metrics-srv")
    thread.start()
    return server
