"""Structured JSON security audit log with configurable sink.

Every security-relevant event in CaMeL produces a structured
:class:`AuditLogRecord` containing all required fields.  The record is then
written to whichever sink is configured — ``stdout``, a local ``file``, or an
``external`` HTTP aggregator.

Configuration
-------------
The sink is configured via the ``CAMEL_AUDIT_SINK`` environment variable:

+------------------------------+-------------------------------------------+
| ``CAMEL_AUDIT_SINK`` value   | Behaviour                                 |
+==============================+===========================================+
| ``stdout`` (default)         | Write to ``sys.stdout`` as newline-       |
|                              | delimited JSON.                           |
+------------------------------+-------------------------------------------+
| ``file:/path/to/audit.log``  | Append newline-delimited JSON to the      |
|                              | specified file path.  Created if missing. |
+------------------------------+-------------------------------------------+
| ``external:http://host/path``| HTTP POST each record as JSON to the URL. |
|                              | Set ``CAMEL_AUDIT_EXTERNAL_AUTH`` to a    |
|                              | ``Bearer <token>`` string to add an       |
|                              | ``Authorization`` header.                 |
+------------------------------+-------------------------------------------+

Programmatic configuration is supported via :class:`AuditSinkConfig` passed
directly to :class:`AuditSink`; this overrides the environment variable.

AuditLogRecord schema
---------------------
Every record contains the following fields:

``timestamp``
    UTC ISO-8601 timestamp of the event.
``session_id``
    Opaque session identifier correlating records from a single agent run.
``event_type``
    One of: ``"policy_evaluation"``, ``"tool_call"``, ``"consent_decision"``,
    ``"capability_assignment"``, ``"qlm_error"``, ``"pllm_retry"``,
    ``"task_completion"``, ``"security_violation"``.
``tool_name``
    Name of the tool involved; empty string when not applicable.
``policy_name``
    Name of the policy function; empty string when not applicable.
``decision``
    Outcome string, e.g. ``"Allowed"``, ``"Denied"``, ``"UserApproved"``,
    ``"TaskSuccess"``, ``"TaskFailure"``.
``capability_summary``
    Human-readable provenance summary of the tool arguments, e.g.
    ``"sources={'get_last_email'}, readers=Public"``.
``backend_id``
    Backend identifier from :meth:`~camel.llm.LLMBackend.get_backend_id`,
    e.g. ``"claude:claude-opus-4-6"``.

Thread safety
-------------
:class:`AuditSink` uses a :class:`threading.Lock` to serialise concurrent
writes, making it safe for use across multiple ``agent.run()`` calls.

Examples
--------
Stdout sink (default)::

    from camel.observability.audit_sink import get_default_sink, AuditLogRecord
    sink = get_default_sink()
    sink.write(AuditLogRecord(
        session_id="s1",
        event_type="policy_evaluation",
        tool_name="send_email",
        policy_name="send_email_policy",
        decision="Allowed",
        capability_summary="sources={'User literal'}, readers=Public",
        backend_id="claude:claude-opus-4-6",
    ))

File sink::

    import os
    os.environ["CAMEL_AUDIT_SINK"] = "file:/tmp/camel_audit.log"
    from camel.observability.audit_sink import get_default_sink
    sink = get_default_sink()

External sink::

    os.environ["CAMEL_AUDIT_SINK"] = "external:http://logs.internal/camel"
    os.environ["CAMEL_AUDIT_EXTERNAL_AUTH"] = "Bearer my-token"
    sink = get_default_sink()
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_logger = logging.getLogger(__name__)

__all__ = [
    "AuditLogRecord",
    "AuditSink",
    "AuditSinkConfig",
    "SinkMode",
    "get_default_sink",
]


# ---------------------------------------------------------------------------
# SinkMode enum
# ---------------------------------------------------------------------------


class SinkMode(Enum):
    """Supported audit log sink modes.

    Attributes
    ----------
    STDOUT:
        Write newline-delimited JSON records to ``sys.stdout``.
    FILE:
        Append newline-delimited JSON records to a file.
    EXTERNAL:
        HTTP POST each record as JSON to an external aggregator URL.
    """

    STDOUT = "stdout"
    FILE = "file"
    EXTERNAL = "external"


# ---------------------------------------------------------------------------
# AuditLogRecord
# ---------------------------------------------------------------------------


@dataclass
class AuditLogRecord:
    """Structured JSON audit log record for a single CaMeL security event.

    All fields are serialisable to JSON via :meth:`to_dict` or
    :meth:`to_json`.

    Attributes
    ----------
    session_id:
        Opaque session identifier correlating records within a single
        :meth:`~camel_security.CaMeLAgent.run` invocation.
    event_type:
        Discriminant string identifying the kind of event.  Supported values:
        ``"policy_evaluation"``, ``"tool_call"``, ``"consent_decision"``,
        ``"capability_assignment"``, ``"qlm_error"``, ``"pllm_retry"``,
        ``"task_completion"``, ``"security_violation"``.
    tool_name:
        Name of the tool involved in the event.  Empty string when not
        applicable (e.g. task-level events).
    policy_name:
        Name of the policy function that produced the decision.  Empty string
        when not applicable.
    decision:
        Outcome string, e.g. ``"Allowed"``, ``"Denied"``,
        ``"UserApproved"``, ``"UserRejected"``, ``"TaskSuccess"``,
        ``"TaskFailure"``.
    capability_summary:
        Human-readable provenance summary of the tool arguments.  Contains
        only metadata (sources, readers labels); never raw data values.
        Example: ``"sources={'get_last_email'}, readers=Public"``.
    backend_id:
        Backend identifier string from
        :meth:`~camel.llm.LLMBackend.get_backend_id`, e.g.
        ``"claude:claude-opus-4-6"``.  Empty string when not applicable.
    timestamp:
        UTC ISO-8601 timestamp set automatically at construction time.
    extra:
        Optional dict of additional event-specific fields.  May be extended
        by callers without breaking the schema.
    """

    session_id: str
    event_type: str
    tool_name: str
    policy_name: str
    decision: str
    capability_summary: str
    backend_id: str
    timestamp: str = field(default_factory=lambda: _utc_now())
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict representation of this record.

        Returns
        -------
        dict[str, Any]
            All fields including ``extra`` contents merged at the top level.
        """
        base = asdict(self)
        extra = base.pop("extra", {})
        base.update(extra)
        return base

    def to_json(self) -> str:
        """Return a compact JSON string representation of this record.

        Returns
        -------
        str
            Single-line JSON object suitable for newline-delimited log files.
        """
        return json.dumps(self.to_dict(), default=str)


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()  # noqa: UP017


# ---------------------------------------------------------------------------
# AuditSinkConfig
# ---------------------------------------------------------------------------


@dataclass
class AuditSinkConfig:
    """Programmatic configuration for an :class:`AuditSink`.

    When provided to :class:`AuditSink`, this config takes precedence over
    the ``CAMEL_AUDIT_SINK`` environment variable.

    Attributes
    ----------
    mode:
        The :class:`SinkMode` to use.
    file_path:
        Path to the log file.  Required when ``mode`` is ``SinkMode.FILE``.
    external_url:
        HTTP endpoint URL.  Required when ``mode`` is ``SinkMode.EXTERNAL``.
    auth_header:
        Value for the ``Authorization`` HTTP header, e.g.
        ``"Bearer my-token"``.  Optional; only used when ``mode`` is
        ``SinkMode.EXTERNAL``.
    """

    mode: SinkMode = SinkMode.STDOUT
    file_path: str = ""
    external_url: str = ""
    auth_header: str = ""


# ---------------------------------------------------------------------------
# AuditSink
# ---------------------------------------------------------------------------


class AuditSink:
    """Thread-safe structured JSON audit log sink.

    Writes :class:`AuditLogRecord` instances as newline-delimited JSON to
    the configured sink (stdout, file, or external HTTP endpoint).

    Parameters
    ----------
    config:
        Explicit :class:`AuditSinkConfig`.  When ``None``, the sink is
        configured from the ``CAMEL_AUDIT_SINK`` environment variable.

    Configuration from environment
    ------------------------------
    - ``CAMEL_AUDIT_SINK=stdout`` → stdout mode (default).
    - ``CAMEL_AUDIT_SINK=file:/path/to/log`` → file mode.
    - ``CAMEL_AUDIT_SINK=external:http://host/path`` → external HTTP mode.
    - ``CAMEL_AUDIT_EXTERNAL_AUTH=Bearer <token>`` → auth header for external.
    """

    def __init__(self, config: AuditSinkConfig | None = None) -> None:
        """Initialise the sink from *config* or environment variables."""
        self._config = config or _config_from_env()
        self._lock = threading.Lock()
        self._records: list[AuditLogRecord] = []

    # ------------------------------------------------------------------
    # Public write interface
    # ------------------------------------------------------------------

    def write(self, record: AuditLogRecord) -> None:
        """Write *record* to the configured sink.

        Thread-safe: concurrent calls are serialised via an internal lock.

        Parameters
        ----------
        record:
            The :class:`AuditLogRecord` to write.
        """
        with self._lock:
            self._records.append(record)
            self._dispatch(record)

    def write_from_dict(self, data: dict[str, Any]) -> None:
        """Convenience: build an :class:`AuditLogRecord` from *data* and write it.

        Parameters
        ----------
        data:
            Dict containing at least the required :class:`AuditLogRecord`
            fields.  Extra keys are stored in ``extra``.
        """
        required = {
            "session_id", "event_type", "tool_name", "policy_name",
            "decision", "capability_summary", "backend_id",
        }
        base: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for k, v in data.items():
            if k in required or k in ("timestamp",):
                base[k] = v
            else:
                extra[k] = v
        record = AuditLogRecord(**base, extra=extra)
        self.write(record)

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def get_records(self) -> list[AuditLogRecord]:
        """Return an immutable copy of all records written so far.

        Returns
        -------
        list[AuditLogRecord]
            All records in write order.  The list is a shallow copy;
            record objects themselves are immutable.
        """
        with self._lock:
            return list(self._records)

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, record: AuditLogRecord) -> None:
        """Route *record* to the appropriate sink backend (called under lock)."""
        mode = self._config.mode
        if mode is SinkMode.STDOUT:
            self._write_stdout(record)
        elif mode is SinkMode.FILE:
            self._write_file(record)
        elif mode is SinkMode.EXTERNAL:
            self._write_external(record)

    def _write_stdout(self, record: AuditLogRecord) -> None:
        """Write *record* as a JSON line to stdout."""
        sys.stdout.write(record.to_json() + "\n")
        sys.stdout.flush()

    def _write_file(self, record: AuditLogRecord) -> None:
        """Append *record* as a JSON line to the configured file path."""
        path = self._config.file_path
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(record.to_json() + "\n")

    def _write_external(self, record: AuditLogRecord) -> None:
        """POST *record* as JSON to the configured external URL.

        Failures are logged at WARNING level so misconfigurations are
        discoverable by operators.  Exceptions are never propagated to
        prevent observability from disrupting the agent's security-critical
        execution path.
        """
        try:
            url = self._config.external_url
            data = record.to_json().encode("utf-8")
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._config.auth_header:
                headers["Authorization"] = self._config.auth_header
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                status = resp.status
            if status < 200 or status >= 300:
                _logger.warning(
                    "AuditSink external HTTP POST returned non-2xx status %d for URL %s",
                    status,
                    url,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "AuditSink external HTTP POST failed for URL %s: %s",
                self._config.external_url,
                exc,
            )


# ---------------------------------------------------------------------------
# Environment variable parser
# ---------------------------------------------------------------------------


def _config_from_env() -> AuditSinkConfig:
    """Parse ``CAMEL_AUDIT_SINK`` and ``CAMEL_AUDIT_EXTERNAL_AUTH``.

    Returns
    -------
    AuditSinkConfig
        Configuration derived from environment variables.
    """
    raw = os.environ.get("CAMEL_AUDIT_SINK", "stdout").strip()
    auth_header = os.environ.get("CAMEL_AUDIT_EXTERNAL_AUTH", "")

    if raw == "stdout" or not raw:
        return AuditSinkConfig(mode=SinkMode.STDOUT)
    if raw.startswith("file:"):
        path = raw[len("file:"):]
        return AuditSinkConfig(mode=SinkMode.FILE, file_path=path)
    if raw.startswith("external:"):
        url = raw[len("external:"):]
        return AuditSinkConfig(mode=SinkMode.EXTERNAL, external_url=url, auth_header=auth_header)

    # Default fallback — treat unknown values as stdout.
    return AuditSinkConfig(mode=SinkMode.STDOUT)


# ---------------------------------------------------------------------------
# Module-level default sink factory
# ---------------------------------------------------------------------------

_default_sink: AuditSink | None = None
_default_sink_lock = threading.Lock()


def get_default_sink() -> AuditSink:
    """Return the process-wide default :class:`AuditSink` singleton.

    The singleton is constructed on first call using the ``CAMEL_AUDIT_SINK``
    environment variable.  Thread-safe.

    Returns
    -------
    AuditSink
        The shared default audit sink.
    """
    global _default_sink
    if _default_sink is None:
        with _default_sink_lock:
            if _default_sink is None:
                _default_sink = AuditSink()
    return _default_sink


def _reset_default_sink() -> None:
    """Reset the module-level default sink singleton (for testing only)."""
    global _default_sink
    with _default_sink_lock:
        _default_sink = None
