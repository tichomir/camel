"""Tests for camel.observability — metrics and audit log sink.

Acceptance criteria covered:
- /metrics endpoint returns valid Prometheus text format with all five metrics
- All five metrics are updated correctly (policy denials, Q-LLM errors,
  P-LLM retries, task completions, consent prompts)
- Structured JSON audit log records contain all required fields
- CAMEL_AUDIT_SINK=file, stdout, and external sink modes all functional
- NFR-6: tool calls, policy evaluations, consent decisions, and capability
  assignments all produce audit log entries
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from camel.observability.audit_sink import (
    AuditLogRecord,
    AuditSink,
    AuditSinkConfig,
    SinkMode,
    _config_from_env,
    _reset_default_sink,
)
from camel.observability.metrics import (
    CamelMetricsCollector,
    get_global_collector,
    start_metrics_server,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**kwargs: object) -> AuditLogRecord:
    """Return a minimal valid AuditLogRecord with sensible defaults."""
    defaults = dict(
        session_id="test-session",
        event_type="policy_evaluation",
        tool_name="send_email",
        policy_name="send_email_policy",
        decision="Allowed",
        capability_summary="sources={'User literal'}, readers=Public",
        backend_id="claude:claude-opus-4-6",
    )
    defaults.update(kwargs)  # type: ignore[arg-type]
    return AuditLogRecord(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# Metrics — CamelMetricsCollector
# ===========================================================================


class TestCamelMetricsCollector:
    """Unit tests for CamelMetricsCollector."""

    def setup_method(self) -> None:
        """Create a fresh collector for each test."""
        self.collector = CamelMetricsCollector()

    # ------------------------------------------------------------------
    # record_policy_denial
    # ------------------------------------------------------------------

    def test_policy_denial_increments_counter(self) -> None:
        """policy_denial counter increments on each call."""
        self.collector.record_policy_denial("s1", "my_policy", "send_email")
        self.collector.record_policy_denial("s1", "my_policy", "send_email")

        text = self.collector.get_metrics_text()
        assert "camel_policy_denial_rate" in text
        # Should have a line with value 2.0 for these labels
        assert "2.0" in text

    def test_policy_denial_labels_present(self) -> None:
        """policy_denial counter includes session_id, policy_name, tool_name labels."""
        self.collector.record_policy_denial("sess42", "strict_policy", "write_file")
        text = self.collector.get_metrics_text()
        assert "sess42" in text
        assert "strict_policy" in text
        assert "write_file" in text

    # ------------------------------------------------------------------
    # record_qlm_error
    # ------------------------------------------------------------------

    def test_qlm_error_increments_counter(self) -> None:
        """Q-LLM error counter increments on each call."""
        self.collector.record_qlm_error("s2", "parse_email")
        self.collector.record_qlm_error("s2", "parse_email")
        self.collector.record_qlm_error("s2", "parse_email")

        text = self.collector.get_metrics_text()
        assert "camel_qlm_error_rate" in text
        assert "3.0" in text

    def test_qlm_error_labels_present(self) -> None:
        """Q-LLM error counter includes session_id and tool_name labels."""
        self.collector.record_qlm_error("sess-q", "extract_info")
        text = self.collector.get_metrics_text()
        assert "sess-q" in text
        assert "extract_info" in text

    # ------------------------------------------------------------------
    # record_pllm_retry
    # ------------------------------------------------------------------

    def test_pllm_retry_histogram_observations(self) -> None:
        """P-LLM retry histogram records observations."""
        self.collector.record_pllm_retry("s3", 0)
        self.collector.record_pllm_retry("s3", 2)
        self.collector.record_pllm_retry("s3", 5)

        text = self.collector.get_metrics_text()
        assert "camel_pllm_retry_count_histogram" in text
        assert "_bucket" in text
        assert "_sum" in text
        assert "_count" in text

    def test_pllm_retry_histogram_labels(self) -> None:
        """P-LLM retry histogram includes session_id label."""
        self.collector.record_pllm_retry("session-h", 3)
        text = self.collector.get_metrics_text()
        assert "session-h" in text

    def test_pllm_retry_histogram_sum_matches(self) -> None:
        """P-LLM retry histogram sum equals sum of observed values."""
        self.collector.record_pllm_retry("sess-sum", 1)
        self.collector.record_pllm_retry("sess-sum", 4)

        # sum should be 5.0
        text = self.collector.get_metrics_text()
        assert "5.0" in text

    # ------------------------------------------------------------------
    # record_task_completion
    # ------------------------------------------------------------------

    def test_task_success_rate_all_success(self) -> None:
        """task_success_rate gauge is 1.0 when all tasks succeed."""
        self.collector.record_task_completion("s4", success=True)
        self.collector.record_task_completion("s4", success=True)

        text = self.collector.get_metrics_text()
        assert "camel_task_success_rate" in text
        assert "1.0000" in text

    def test_task_success_rate_mixed(self) -> None:
        """task_success_rate gauge is 0.5 when half succeed."""
        self.collector.record_task_completion("s5", success=True)
        self.collector.record_task_completion("s5", success=False)

        text = self.collector.get_metrics_text()
        assert "0.5000" in text

    def test_task_success_rate_all_failure(self) -> None:
        """task_success_rate gauge is 0.0 when all tasks fail."""
        self.collector.record_task_completion("s6", success=False)

        text = self.collector.get_metrics_text()
        assert "0.0000" in text

    def test_task_success_rate_label(self) -> None:
        """task_success_rate gauge includes session_id label."""
        self.collector.record_task_completion("my-session", success=True)
        text = self.collector.get_metrics_text()
        assert "my-session" in text

    # ------------------------------------------------------------------
    # record_consent_prompt
    # ------------------------------------------------------------------

    def test_consent_prompt_increments_counter(self) -> None:
        """consent_prompt counter increments on each call."""
        self.collector.record_consent_prompt("s7", "delete_file")
        self.collector.record_consent_prompt("s7", "delete_file")

        text = self.collector.get_metrics_text()
        assert "camel_consent_prompt_rate" in text
        assert "2.0" in text

    def test_consent_prompt_labels_present(self) -> None:
        """consent_prompt counter includes session_id and tool_name labels."""
        self.collector.record_consent_prompt("sess-c", "send_money")
        text = self.collector.get_metrics_text()
        assert "sess-c" in text
        assert "send_money" in text

    # ------------------------------------------------------------------
    # get_metrics_text — Prometheus format
    # ------------------------------------------------------------------

    def test_all_five_metrics_present_in_text(self) -> None:
        """get_metrics_text includes all five metric names."""
        text = self.collector.get_metrics_text()
        assert "camel_policy_denial_rate" in text
        assert "camel_qlm_error_rate" in text
        assert "camel_pllm_retry_count_histogram" in text
        assert "camel_task_success_rate" in text
        assert "camel_consent_prompt_rate" in text

    def test_prometheus_help_lines_present(self) -> None:
        """get_metrics_text includes # HELP lines for each metric."""
        text = self.collector.get_metrics_text()
        assert "# HELP camel_policy_denial_rate" in text
        assert "# HELP camel_qlm_error_rate" in text
        assert "# HELP camel_pllm_retry_count_histogram" in text
        assert "# HELP camel_task_success_rate" in text
        assert "# HELP camel_consent_prompt_rate" in text

    def test_prometheus_type_lines_present(self) -> None:
        """get_metrics_text includes # TYPE lines for each metric."""
        text = self.collector.get_metrics_text()
        assert "# TYPE camel_policy_denial_rate counter" in text
        assert "# TYPE camel_qlm_error_rate counter" in text
        assert "# TYPE camel_pllm_retry_count_histogram histogram" in text
        assert "# TYPE camel_task_success_rate gauge" in text
        assert "# TYPE camel_consent_prompt_rate counter" in text

    def test_metrics_text_ends_with_newline(self) -> None:
        """get_metrics_text output ends with a newline."""
        text = self.collector.get_metrics_text()
        assert text.endswith("\n")

    # ------------------------------------------------------------------
    # Thread safety
    # ------------------------------------------------------------------

    def test_concurrent_increments_are_consistent(self) -> None:
        """Concurrent counter increments produce the correct total."""
        n = 100
        errors: list[Exception] = []

        def _worker() -> None:
            try:
                for _ in range(n):
                    self.collector.record_policy_denial("cs", "p", "t")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snapshot = self.collector._policy_denial_rate.snapshot()
        total = sum(snapshot.values())
        assert total == 5 * n


# ===========================================================================
# Metrics — HTTP server
# ===========================================================================


class TestMetricsServer:
    """Integration tests for the /metrics HTTP endpoint."""

    def test_metrics_endpoint_returns_200(self) -> None:
        """GET /metrics returns HTTP 200 with Prometheus text."""
        collector = CamelMetricsCollector()
        collector.record_policy_denial("srv-s1", "pol", "tool")

        server = start_metrics_server(port=0, collector=collector)
        port = server.server_address[1]

        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/metrics", timeout=5
            ) as resp:
                assert resp.status == 200
                body = resp.read().decode("utf-8")
                assert "camel_policy_denial_rate" in body
                assert "camel_qlm_error_rate" in body
                assert "camel_pllm_retry_count_histogram" in body
                assert "camel_task_success_rate" in body
                assert "camel_consent_prompt_rate" in body
        finally:
            server.shutdown()

    def test_metrics_endpoint_content_type(self) -> None:
        """GET /metrics returns text/plain content-type."""
        collector = CamelMetricsCollector()
        server = start_metrics_server(port=0, collector=collector)
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/metrics", timeout=5
            ) as resp:
                ct = resp.headers.get("Content-Type", "")
                assert "text/plain" in ct
        finally:
            server.shutdown()

    def test_non_metrics_path_returns_404(self) -> None:
        """GET /other returns HTTP 404."""
        collector = CamelMetricsCollector()
        server = start_metrics_server(port=0, collector=collector)
        port = server.server_address[1]
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(
                    f"http://localhost:{port}/other", timeout=5
                )
            assert exc_info.value.code == 404
        finally:
            server.shutdown()

    def test_all_five_metrics_in_endpoint_response(self) -> None:
        """GET /metrics body includes all five metric names and labels."""
        collector = CamelMetricsCollector()
        collector.record_policy_denial("e2e-s", "pol", "send_email")
        collector.record_qlm_error("e2e-s", "parse")
        collector.record_pllm_retry("e2e-s", 2)
        collector.record_task_completion("e2e-s", success=True)
        collector.record_consent_prompt("e2e-s", "write_file")

        server = start_metrics_server(port=0, collector=collector)
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/metrics", timeout=5
            ) as resp:
                body = resp.read().decode("utf-8")
            assert "camel_policy_denial_rate" in body
            assert "camel_qlm_error_rate" in body
            assert "camel_pllm_retry_count_histogram" in body
            assert "camel_task_success_rate" in body
            assert "camel_consent_prompt_rate" in body
            assert "e2e-s" in body
        finally:
            server.shutdown()


# ===========================================================================
# Metrics — global singleton
# ===========================================================================


class TestGetGlobalCollector:
    """Tests for the global collector singleton."""

    def test_returns_same_instance(self) -> None:
        """get_global_collector returns the same object on multiple calls."""
        c1 = get_global_collector()
        c2 = get_global_collector()
        assert c1 is c2

    def test_is_camel_metrics_collector(self) -> None:
        """Global collector is a CamelMetricsCollector."""
        assert isinstance(get_global_collector(), CamelMetricsCollector)


# ===========================================================================
# AuditLogRecord
# ===========================================================================


class TestAuditLogRecord:
    """Tests for AuditLogRecord schema and serialisation."""

    def test_required_fields_present(self) -> None:
        """AuditLogRecord contains all required fields."""
        record = _make_record()
        d = record.to_dict()
        assert "timestamp" in d
        assert "session_id" in d
        assert "event_type" in d
        assert "tool_name" in d
        assert "policy_name" in d
        assert "decision" in d
        assert "capability_summary" in d
        assert "backend_id" in d

    def test_timestamp_is_iso8601(self) -> None:
        """timestamp field is a valid UTC ISO-8601 string."""
        record = _make_record()
        from datetime import datetime  # noqa: PLC0415

        dt = datetime.fromisoformat(record.timestamp)
        assert dt.tzinfo is not None

    def test_to_json_is_valid_json(self) -> None:
        """to_json returns a valid JSON string."""
        record = _make_record()
        parsed = json.loads(record.to_json())
        assert parsed["session_id"] == "test-session"

    def test_extra_fields_merged_in_to_dict(self) -> None:
        """Extra fields appear at the top level in to_dict."""
        record = _make_record(extra={"run_ref": "camel-audit:abc123"})
        d = record.to_dict()
        assert d["run_ref"] == "camel-audit:abc123"

    def test_all_event_types_representable(self) -> None:
        """All documented event_type values are representable."""
        event_types = [
            "policy_evaluation",
            "tool_call",
            "consent_decision",
            "capability_assignment",
            "qlm_error",
            "pllm_retry",
            "task_completion",
            "security_violation",
        ]
        for et in event_types:
            record = _make_record(event_type=et)
            assert record.event_type == et

    def test_decision_values_representable(self) -> None:
        """Standard decision values are representable."""
        decisions = ["Allowed", "Denied", "UserApproved", "UserRejected",
                     "TaskSuccess", "TaskFailure"]
        for d in decisions:
            record = _make_record(decision=d)
            assert record.decision == d


# ===========================================================================
# AuditSink — stdout mode
# ===========================================================================


class TestAuditSinkStdout:
    """Tests for AuditSink in STDOUT mode."""

    def test_write_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """write() outputs a JSON line to stdout in stdout mode."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        record = _make_record()
        sink.write(record)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["session_id"] == "test-session"

    def test_stdout_output_contains_all_required_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdout JSON output contains all required schema fields."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        sink.write(_make_record())

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        for field_name in [
            "timestamp", "session_id", "event_type", "tool_name",
            "policy_name", "decision", "capability_summary", "backend_id",
        ]:
            assert field_name in parsed, f"Missing field: {field_name}"

    def test_multiple_writes_produce_multiple_lines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multiple write() calls produce multiple JSON lines."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        sink.write(_make_record(event_type="tool_call"))
        sink.write(_make_record(event_type="policy_evaluation"))

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.strip().splitlines() if ln]
        assert len(lines) == 2
        parsed = [json.loads(ln) for ln in lines]
        event_types = {p["event_type"] for p in parsed}
        assert "tool_call" in event_types
        assert "policy_evaluation" in event_types


# ===========================================================================
# AuditSink — file mode
# ===========================================================================


class TestAuditSinkFile:
    """Tests for AuditSink in FILE mode."""

    def test_write_to_file(self, tmp_path: object) -> None:
        """write() appends a JSON line to the specified file."""
        import pathlib  # noqa: PLC0415
        log_file = pathlib.Path(str(tmp_path)) / "audit.log"  # type: ignore[arg-type]

        sink = AuditSink(AuditSinkConfig(mode=SinkMode.FILE, file_path=str(log_file)))
        sink.write(_make_record())

        content = log_file.read_text(encoding="utf-8")
        parsed = json.loads(content.strip())
        assert parsed["session_id"] == "test-session"

    def test_file_appends_multiple_records(self, tmp_path: object) -> None:
        """Multiple writes append multiple JSON lines."""
        import pathlib  # noqa: PLC0415
        log_file = pathlib.Path(str(tmp_path)) / "audit2.log"  # type: ignore[arg-type]

        sink = AuditSink(AuditSinkConfig(mode=SinkMode.FILE, file_path=str(log_file)))
        for i in range(3):
            sink.write(_make_record(session_id=f"s{i}"))

        lines = [ln for ln in log_file.read_text().splitlines() if ln]
        assert len(lines) == 3
        sessions = [json.loads(ln)["session_id"] for ln in lines]
        assert set(sessions) == {"s0", "s1", "s2"}

    def test_file_contains_all_required_fields(self, tmp_path: object) -> None:
        """File JSON contains all required schema fields."""
        import pathlib  # noqa: PLC0415
        log_file = pathlib.Path(str(tmp_path)) / "audit3.log"  # type: ignore[arg-type]

        sink = AuditSink(AuditSinkConfig(mode=SinkMode.FILE, file_path=str(log_file)))
        sink.write(_make_record())

        parsed = json.loads(log_file.read_text().strip())
        for field_name in [
            "timestamp", "session_id", "event_type", "tool_name",
            "policy_name", "decision", "capability_summary", "backend_id",
        ]:
            assert field_name in parsed

    def test_file_created_if_missing(self, tmp_path: object) -> None:
        """File is created automatically if it does not exist."""
        import pathlib  # noqa: PLC0415
        log_file = pathlib.Path(str(tmp_path)) / "new_dir" / "audit.log"  # type: ignore[arg-type]
        log_file.parent.mkdir(parents=True, exist_ok=True)

        sink = AuditSink(AuditSinkConfig(mode=SinkMode.FILE, file_path=str(log_file)))
        sink.write(_make_record())
        assert log_file.exists()

    def test_env_var_file_mode(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        """CAMEL_AUDIT_SINK=file:/path configures FILE mode via env."""
        import pathlib  # noqa: PLC0415
        log_file = pathlib.Path(str(tmp_path)) / "env_audit.log"  # type: ignore[arg-type]
        monkeypatch.setenv("CAMEL_AUDIT_SINK", f"file:{log_file}")

        _reset_default_sink()
        config = _config_from_env()
        assert config.mode is SinkMode.FILE
        assert config.file_path == str(log_file)


# ===========================================================================
# AuditSink — external mode
# ===========================================================================


class TestAuditSinkExternal:
    """Tests for AuditSink in EXTERNAL mode using a mock HTTP server."""

    def _start_mock_server(
        self,
    ) -> tuple[int, list[dict[str, object]], threading.Event]:
        """Start a minimal HTTP server that captures POST bodies."""
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        received: list[dict[str, object]] = []
        ready = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body.decode()))
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        server = HTTPServer(("localhost", 0), _Handler)
        port = server.server_address[1]

        def _serve() -> None:
            ready.set()
            server.serve_forever()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        ready.wait()
        return port, received, ready

    def test_external_sink_posts_json(self) -> None:
        """External mode POSTs JSON to the configured URL."""
        port, received, _ = self._start_mock_server()
        url = f"http://localhost:{port}/ingest"
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.EXTERNAL, external_url=url))
        sink.write(_make_record(session_id="ext-s1"))

        # Give the server a moment to process.
        deadline = time.monotonic() + 3.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)

        assert received, "No request received by mock server"
        assert received[0]["session_id"] == "ext-s1"

    def test_external_sink_posts_all_required_fields(self) -> None:
        """External POST body contains all required schema fields."""
        port, received, _ = self._start_mock_server()
        url = f"http://localhost:{port}/ingest"
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.EXTERNAL, external_url=url))
        sink.write(_make_record())

        deadline = time.monotonic() + 3.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)

        assert received
        for field_name in [
            "timestamp", "session_id", "event_type", "tool_name",
            "policy_name", "decision", "capability_summary", "backend_id",
        ]:
            assert field_name in received[0], f"Missing field: {field_name}"

    def test_external_sink_sends_auth_header(self) -> None:
        """External mode sends Authorization header when configured."""
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        auth_headers: list[str] = []
        ready = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                auth_headers.append(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        server = HTTPServer(("localhost", 0), _Handler)
        port = server.server_address[1]

        def _serve() -> None:
            ready.set()
            server.serve_forever()

        threading.Thread(target=_serve, daemon=True).start()
        ready.wait()

        sink = AuditSink(AuditSinkConfig(
            mode=SinkMode.EXTERNAL,
            external_url=f"http://localhost:{port}/ingest",
            auth_header="Bearer secret-token",
        ))
        sink.write(_make_record())

        deadline = time.monotonic() + 3.0
        while not auth_headers and time.monotonic() < deadline:
            time.sleep(0.05)

        assert auth_headers
        assert auth_headers[0] == "Bearer secret-token"

    def test_env_var_external_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CAMEL_AUDIT_SINK=external:http://... configures EXTERNAL mode via env."""
        monkeypatch.setenv("CAMEL_AUDIT_SINK", "external:http://logs.internal/camel")
        monkeypatch.setenv("CAMEL_AUDIT_EXTERNAL_AUTH", "Bearer tok")

        config = _config_from_env()
        assert config.mode is SinkMode.EXTERNAL
        assert config.external_url == "http://logs.internal/camel"
        assert config.auth_header == "Bearer tok"


# ===========================================================================
# AuditSink — environment variable parsing
# ===========================================================================


class TestAuditSinkEnvParsing:
    """Tests for CAMEL_AUDIT_SINK environment variable parsing."""

    def test_stdout_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default (no env var) is stdout mode."""
        monkeypatch.delenv("CAMEL_AUDIT_SINK", raising=False)
        config = _config_from_env()
        assert config.mode is SinkMode.STDOUT

    def test_stdout_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CAMEL_AUDIT_SINK=stdout gives stdout mode."""
        monkeypatch.setenv("CAMEL_AUDIT_SINK", "stdout")
        config = _config_from_env()
        assert config.mode is SinkMode.STDOUT

    def test_file_mode_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CAMEL_AUDIT_SINK=file:/path/to/log configures file path."""
        monkeypatch.setenv("CAMEL_AUDIT_SINK", "file:/var/log/camel.log")
        config = _config_from_env()
        assert config.mode is SinkMode.FILE
        assert config.file_path == "/var/log/camel.log"

    def test_unknown_value_falls_back_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown CAMEL_AUDIT_SINK value falls back to stdout."""
        monkeypatch.setenv("CAMEL_AUDIT_SINK", "unknown_sink_type")
        config = _config_from_env()
        assert config.mode is SinkMode.STDOUT


# ===========================================================================
# AuditSink — get_records inspection
# ===========================================================================


class TestAuditSinkGetRecords:
    """Tests for AuditSink.get_records()."""

    def test_records_accumulate(self, capsys: pytest.CaptureFixture[str]) -> None:
        """get_records returns all records written to the sink."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        sink.write(_make_record(event_type="tool_call"))
        sink.write(_make_record(event_type="policy_evaluation"))

        records = sink.get_records()
        assert len(records) == 2
        event_types = {r.event_type for r in records}
        assert "tool_call" in event_types
        assert "policy_evaluation" in event_types

    def test_records_returns_copy(self, capsys: pytest.CaptureFixture[str]) -> None:
        """get_records returns a copy; mutating it does not affect the sink."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        sink.write(_make_record())

        records = sink.get_records()
        records.clear()

        assert len(sink.get_records()) == 1


# ===========================================================================
# NFR-6 compliance — event coverage
# ===========================================================================


class TestNFR6Compliance:
    """Verify that all NFR-6 event categories produce AuditLogRecord entries."""

    def _tool_call_record(self) -> AuditLogRecord:
        return _make_record(event_type="tool_call", decision="Allowed")

    def _policy_eval_record(self) -> AuditLogRecord:
        return _make_record(event_type="policy_evaluation", decision="Denied")

    def _consent_record(self) -> AuditLogRecord:
        return _make_record(event_type="consent_decision", decision="UserApproved")

    def _capability_record(self) -> AuditLogRecord:
        return _make_record(
            event_type="capability_assignment",
            capability_summary="sources={'get_last_email'}, readers=Public",
            decision="Allowed",
        )

    def _qlm_error_record(self) -> AuditLogRecord:
        return _make_record(event_type="qlm_error", decision="NotEnoughInformation")

    def _pllm_retry_record(self) -> AuditLogRecord:
        return _make_record(event_type="pllm_retry", decision="Retry")

    def _task_completion_record(self) -> AuditLogRecord:
        return _make_record(event_type="task_completion", decision="TaskSuccess")

    def test_all_nfr6_event_types_produce_records(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All NFR-6 event categories are representable as AuditLogRecords."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        records_to_write = [
            self._tool_call_record(),
            self._policy_eval_record(),
            self._consent_record(),
            self._capability_record(),
            self._qlm_error_record(),
            self._pllm_retry_record(),
            self._task_completion_record(),
        ]
        for r in records_to_write:
            sink.write(r)

        stored = sink.get_records()
        assert len(stored) == 7
        event_types = {r.event_type for r in stored}
        for et in [
            "tool_call", "policy_evaluation", "consent_decision",
            "capability_assignment", "qlm_error", "pllm_retry", "task_completion",
        ]:
            assert et in event_types, f"Missing NFR-6 event type: {et}"

    def test_all_nfr6_records_have_required_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every NFR-6 record contains timestamp, session_id, and backend_id."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        for r in [
            self._tool_call_record(),
            self._policy_eval_record(),
            self._consent_record(),
            self._capability_record(),
        ]:
            sink.write(r)
            d = r.to_dict()
            assert d["timestamp"], f"Missing timestamp in {r.event_type}"
            assert d["session_id"], f"Missing session_id in {r.event_type}"
            assert "backend_id" in d, f"Missing backend_id in {r.event_type}"

    def test_write_from_dict_creates_valid_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """write_from_dict builds and writes a valid AuditLogRecord."""
        sink = AuditSink(AuditSinkConfig(mode=SinkMode.STDOUT))
        sink.write_from_dict({
            "session_id": "s-dict",
            "event_type": "tool_call",
            "tool_name": "echo",
            "policy_name": "",
            "decision": "Allowed",
            "capability_summary": "sources={'User literal'}",
            "backend_id": "gemini:gemini-2.5-flash",
            "loop_attempt": 0,  # extra field
        })

        records = sink.get_records()
        assert len(records) == 1
        assert records[0].session_id == "s-dict"
        assert records[0].extra.get("loop_attempt") == 0


# ===========================================================================
# OTLP urllib fallback — payload schema conformance
# ===========================================================================


class TestOtlpUrllibFallbackSchema:
    """Verify that the urllib fallback produces a valid OTLP/HTTP JSON payload.

    The OTLP/HTTP JSON encoding specification (protobuf JSON mapping) requires
    camelCase field names.  Any snake_case field names (e.g. ``resource_metrics``,
    ``data_points``) will cause standard OTLP collectors to return HTTP 400.
    """

    def _capture_payload(self) -> dict:
        """Record a few metrics and capture the urllib fallback payload."""
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        received: list[dict] = []
        ready = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body.decode()))
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        server = HTTPServer(("localhost", 0), _Handler)
        port = server.server_address[1]

        def _serve() -> None:
            ready.set()
            server.serve_forever()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        ready.wait()

        collector = CamelMetricsCollector()
        collector.record_policy_denial("s1", "pol", "send_email")
        collector.record_qlm_error("s1", "parse")
        collector.record_task_completion("s1", success=True)
        collector.record_consent_prompt("s1", "write_file")

        collector._push_otel_via_urllib(f"http://localhost:{port}")

        deadline = time.monotonic() + 3.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)

        server.shutdown()
        assert received, "Mock OTLP server received no request"
        return received[0]

    def test_top_level_key_is_resourceMetrics(self) -> None:
        """Payload top-level key is 'resourceMetrics' (camelCase), not 'resource_metrics'."""
        payload = self._capture_payload()
        assert "resourceMetrics" in payload, (
            "Expected camelCase 'resourceMetrics'; got keys: " + str(list(payload.keys()))
        )
        assert "resource_metrics" not in payload

    def test_scope_metrics_key_is_camelCase(self) -> None:
        """Inner key is 'scopeMetrics', not 'scope_metrics'."""
        payload = self._capture_payload()
        resource = payload["resourceMetrics"][0]
        assert "scopeMetrics" in resource, (
            "Expected camelCase 'scopeMetrics'; got: " + str(list(resource.keys()))
        )
        assert "scope_metrics" not in resource

    def test_scope_has_name(self) -> None:
        """scopeMetrics entries contain a 'scope' object with 'name'."""
        payload = self._capture_payload()
        scope_metrics = payload["resourceMetrics"][0]["scopeMetrics"]
        assert scope_metrics, "scopeMetrics list is empty"
        scope = scope_metrics[0].get("scope", {})
        assert scope.get("name") == "camel-security"

    def test_data_points_key_is_camelCase(self) -> None:
        """Metric data points key is 'dataPoints', not 'data_points'."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        assert metrics, "metrics list is empty"
        # Check all metrics with sum or gauge
        for metric in metrics:
            if "sum" in metric:
                assert "dataPoints" in metric["sum"], (
                    f"Missing 'dataPoints' in sum metric '{metric['name']}'"
                )
                assert "data_points" not in metric["sum"]
            elif "gauge" in metric:
                assert "dataPoints" in metric["gauge"], (
                    f"Missing 'dataPoints' in gauge metric '{metric['name']}'"
                )
                assert "data_points" not in metric["gauge"]

    def test_is_monotonic_key_is_camelCase(self) -> None:
        """Sum metrics use 'isMonotonic', not 'is_monotonic'."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        sum_metrics = [m for m in metrics if "sum" in m]
        assert sum_metrics, "No sum metrics found"
        for metric in sum_metrics:
            assert "isMonotonic" in metric["sum"], (
                f"Missing 'isMonotonic' in '{metric['name']}'"
            )
            assert "is_monotonic" not in metric["sum"]

    def test_aggregation_temporality_present(self) -> None:
        """Sum metrics include 'aggregationTemporality' = 2 (CUMULATIVE)."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        sum_metrics = [m for m in metrics if "sum" in m]
        assert sum_metrics, "No sum metrics found"
        for metric in sum_metrics:
            assert metric["sum"].get("aggregationTemporality") == 2, (
                f"Expected aggregationTemporality=2 in '{metric['name']}'"
            )

    def test_data_point_fields_are_camelCase(self) -> None:
        """Data point fields use camelCase: asDouble, startTimeUnixNano, timeUnixNano."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        for metric in metrics:
            container = metric.get("sum") or metric.get("gauge")
            assert container is not None
            for dp in container["dataPoints"]:
                assert "asDouble" in dp, f"Missing 'asDouble' in dp of '{metric['name']}'"
                assert "startTimeUnixNano" in dp
                assert "timeUnixNano" in dp
                assert "as_double" not in dp
                assert "start_time_unix_nano" not in dp
                assert "time_unix_nano" not in dp

    def test_timestamps_are_strings(self) -> None:
        """Timestamp fields are encoded as strings (uint64 precision requirement)."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        for metric in metrics:
            container = metric.get("sum") or metric.get("gauge")
            assert container is not None
            for dp in container["dataPoints"]:
                assert isinstance(dp["startTimeUnixNano"], str), (
                    "startTimeUnixNano must be a string, not "
                    + type(dp["startTimeUnixNano"]).__name__
                )
                assert isinstance(dp["timeUnixNano"], str), (
                    "timeUnixNano must be a string, not "
                    + type(dp["timeUnixNano"]).__name__
                )

    def test_attribute_value_key_is_camelCase(self) -> None:
        """Attribute values use 'stringValue', not 'string_value'."""
        payload = self._capture_payload()
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        for metric in metrics:
            container = metric.get("sum") or metric.get("gauge")
            assert container is not None
            for dp in container["dataPoints"]:
                for attr in dp.get("attributes", []):
                    val = attr.get("value", {})
                    assert "stringValue" in val, (
                        f"Expected 'stringValue' in attribute value, got: {val}"
                    )
                    assert "string_value" not in val


# ===========================================================================
# OTLP urllib fallback — RuntimeWarning when SDK is absent
# ===========================================================================


class TestOtlpRuntimeWarning:
    """Verify that a RuntimeWarning is raised when SDK is absent but endpoint set."""

    def test_runtime_warning_when_sdk_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_start_otel_push emits RuntimeWarning when opentelemetry is not importable."""
        import importlib.util  # noqa: PLC0415

        original_find_spec = importlib.util.find_spec

        def _mock_find_spec(name: str, *args: object, **kwargs: object) -> None:
            if name == "opentelemetry":
                return None
            return original_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", _mock_find_spec)

        collector = CamelMetricsCollector()
        with pytest.warns(RuntimeWarning, match="opentelemetry-sdk"):
            collector._start_otel_push("http://localhost:4318")
        collector.stop_otel_push()

    def test_no_warning_when_sdk_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_start_otel_push does not emit RuntimeWarning when opentelemetry is available."""
        import importlib.util  # noqa: PLC0415

        original_find_spec = importlib.util.find_spec

        def _mock_find_spec_present(name: str, *args: object, **kwargs: object):
            if name == "opentelemetry":
                # Return a truthy spec-like object to simulate SDK presence
                class _FakeSpec:
                    pass
                return _FakeSpec()
            return original_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", _mock_find_spec_present)

        collector = CamelMetricsCollector()
        import warnings  # noqa: PLC0415
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            # Should not raise — SDK appears to be present
            try:
                collector._start_otel_push("http://localhost:4318")
            except RuntimeWarning:
                pytest.fail("RuntimeWarning raised even though SDK appears present")
            finally:
                collector.stop_otel_push()
