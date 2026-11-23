"""Structured JSON Lines logging and Prometheus metrics (spec §9)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_threshold = _LEVELS["INFO"]


def configure_logging(level: str) -> None:
    """Set the minimum level that log_event will emit."""
    global _threshold
    _threshold = _LEVELS.get(level.upper(), _LEVELS["INFO"])


def log_event(event: str, **fields: Any) -> None:
    """Write one JSON line to stderr: ts, level, event first, then fields.

    stderr keeps the stdio transport's protocol stream on stdout clean —
    stdout carries MCP JSON-RPC messages only.
    """
    level = str(fields.pop("level", "INFO")).upper()
    if _LEVELS.get(level, 20) < _threshold:
        return
    body: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
    }
    body.update(fields)
    print(json.dumps(body, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


class Metrics:
    """Prometheus metrics owned by the MCP server process."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry or CollectorRegistry()
        self.tool_calls = Counter(
            "mcp_tool_calls_total",
            "Tool invocations by outcome",
            labelnames=("tool", "status"),
            registry=self._registry,
        )
        self.tool_duration = Histogram(
            "mcp_tool_duration_seconds",
            "Tool call duration in seconds",
            labelnames=("tool",),
            registry=self._registry,
        )
        self.jobs_wait = Histogram(
            "mcp_jobs_wait_seconds",
            "Observed submit-to-terminal wait for jobs",
            registry=self._registry,
        )
        self.upstream_errors = Counter(
            "mcp_thinkparse_errors_total",
            "Upstream ThinkParse errors by code",
            labelnames=("code",),
            registry=self._registry,
        )

    def observe_call(self, tool: str, status: str, duration_s: float) -> None:
        self.tool_calls.labels(tool=tool, status=status).inc()
        self.tool_duration.labels(tool=tool).observe(max(0.0, duration_s))

    def observe_wait(self, seconds: float) -> None:
        self.jobs_wait.observe(max(0.0, seconds))

    def observe_upstream_error(self, code: int) -> None:
        self.upstream_errors.labels(code=str(code)).inc()

    def render(self) -> bytes:
        return generate_latest(self._registry)

    @staticmethod
    def content_type() -> str:
        return CONTENT_TYPE_LATEST


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0
