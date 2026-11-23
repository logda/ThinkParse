"""JSON Lines logging and Prometheus metrics."""

import json

from thinkparse_mcp.observability import Metrics, configure_logging, log_event


def test_log_event_writes_single_json_line(capsys):
    log_event("tool_call", tool="parse_document", status="completed", level="INFO")
    out, err = capsys.readouterr()
    assert out == ""  # stdout carries MCP protocol only (stdio transport)
    lines = err.strip().splitlines()
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["event"] == "tool_call"
    assert body["tool"] == "parse_document"
    assert body["status"] == "completed"
    assert "ts" in body
    assert body["level"] == "INFO"


def test_log_event_respects_level_filter(capsys):
    try:
        configure_logging("ERROR")
        log_event("tool_call", level="INFO")
        assert capsys.readouterr().err == ""
        log_event("upstream_error", level="ERROR", error_code=503)
        assert json.loads(capsys.readouterr().err)["event"] == "upstream_error"
    finally:
        configure_logging("INFO")


def test_metrics_render_contains_all_spec_metrics():
    metrics = Metrics()
    metrics.observe_call("parse_document", "ok", 0.42)
    metrics.observe_wait(12.5)
    metrics.observe_upstream_error(503)
    rendered = metrics.render()
    for name in (
        b"mcp_tool_calls_total",
        b"mcp_tool_duration_seconds",
        b"mcp_jobs_wait_seconds",
        b"mcp_thinkparse_errors_total",
    ):
        assert name in rendered
    assert b'tool="parse_document"' in rendered
    assert b'status="ok"' in rendered
    assert b'code="503"' in rendered
