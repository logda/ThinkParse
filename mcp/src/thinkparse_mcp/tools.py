"""MCP tool implementations for the ThinkParse /api/v2 contract.

Tools are plain async methods on `Tools` so unit tests can drive them with a
fake client; `register_tools` binds them to an MCPServer with descriptions the
model reads (spec §6). All upstream HTTP goes through ThinkParseClient.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .client import ThinkParseClient, UpstreamError
from .config import Settings
from .observability import Metrics, log_event, monotonic_ms

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


class ToolError(Exception):
    """An error whose message is safe and useful for the calling model."""


TERMINAL_STATES = {"completed", "failed", "canceled"}
OCR_MODES = ("auto", "txt", "ocr")
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500


class TierCache:
    """60-second cache of the tiers offered by the gateway (spec §6.5)."""

    def __init__(self, client: ThinkParseClient, ttl_s: float = 60.0) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._value: tuple[bool, list[str]] | None = None
        self._expires_at = 0.0

    async def get(self) -> tuple[bool, list[str]]:
        now = time.monotonic()
        if self._value is None or now >= self._expires_at:
            body = await self._client.get_tiers()
            self._value = (
                bool(body.get("discovered")),
                [str(item["id"]) for item in body.get("data", [])],
            )
            self._expires_at = now + self._ttl_s
        return self._value

    async def validate(self, tier: str) -> list[str]:
        discovered, tiers = await self.get()
        if tier not in tiers:
            offered = ", ".join(tiers) if tiers else "none discovered"
            raise ToolError(
                f"tier '{tier}' is not offered by this deployment "
                f"(available: {offered}); call list_tiers to re-check"
            )
        return tiers


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Project the gateway job JSON onto the tool contract (spec §6.1/6.2)."""
    files = job.get("files") or []
    first: dict[str, Any] = files[0] if files else {}
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "tier": job.get("tier"),
        "attempt": job.get("attempt"),
        "pages": job.get("pages"),
        "timing": job.get("timing"),
        "files": first.get("output_files"),
        "error": first.get("error"),
        "links": job.get("links"),
    }


def _map_upstream_error(exc: UpstreamError) -> ToolError:
    """Map upstream failures to model-facing errors (spec §5.4)."""
    if exc.retryable:
        return ToolError(
            f"ThinkParse is unreachable or erroring ({exc}); retry the call later"
        )
    if exc.status == 400:
        return ToolError(
            f"rejected by ThinkParse: {exc.detail}; call list_tiers to check the tiers "
            "this deployment offers"
        )
    if exc.status in {401, 403}:
        return ToolError(
            "ThinkParse rejected the MCP server's credentials; the deployment's "
            "THINKPARSE_API_KEY is misconfigured"
        )
    if exc.status == 404:
        return ToolError(
            f"not found on the ThinkParse gateway ({exc.detail}); check the job id"
        )
    if exc.status == 413:
        return ToolError(f"the file exceeds the ThinkParse size limit: {exc.detail}")
    if exc.status == 507:
        return ToolError(f"ThinkParse object storage is above its watermark: {exc.detail}")
    return ToolError(str(exc))


class Tools:
    """Tool bodies; one instance per server process."""

    def __init__(self, client: ThinkParseClient, settings: Settings, metrics: Metrics) -> None:
        self.client = client
        self.settings = settings
        self.metrics = metrics
        self.tiers = TierCache(client)

    async def list_tiers(self) -> dict[str, Any]:
        try:
            discovered, tiers = await self.tiers.get()
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        recommended = "basic" if "basic" in tiers else (tiers[0] if tiers else None)
        return {
            "discovered": discovered,
            "tiers": [{"id": tier} for tier in tiers],
            "recommended_default": recommended,
        }

    async def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            job = await self.client.get_job(job_id)
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        return _job_view(job)

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        try:
            body = await self.client.cancel_job(job_id)
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        return {"job_id": body.get("job_id", job_id), "status": body.get("status", "canceled")}


_LIST_TIERS_DESCRIPTION = (
    "List the parse tiers this ThinkParse deployment offers (e.g. flash/basic/"
    "standard/advanced). Availability depends on the deployment; call this "
    "before submitting when unsure which tier to use."
)
_GET_JOB_DESCRIPTION = (
    "Fetch one parse job's current state: status (queued/running/completed/"
    "failed/canceled), timing, page count, and — when completed — the output "
    "file references (markdown/middle_json/images). Use this instead of "
    "re-calling parse_document to poll a running job."
)
_CANCEL_JOB_DESCRIPTION = (
    "Cancel a queued or running parse job. Cancellation is durable on the "
    "ThinkParse gateway; the job can still be queried afterwards."
)


def register_tools(server: "MCPServer", tools: Tools) -> None:
    """Attach the Tools instance to an MCPServer with timing/metrics."""

    async def _counting(name: str, call):
        start = monotonic_ms()
        try:
            result = await call()
        except ToolError:
            _finish(name, "error", start)
            raise
        _finish(name, "ok", start)
        return result

    def _finish(name: str, status: str, start: float) -> None:
        duration_ms = monotonic_ms() - start
        tools.metrics.observe_call(name, status, duration_ms / 1000.0)
        log_event("tool_call", tool=name, status=status, duration_ms=round(duration_ms, 1))

    @server.tool(name="list_tiers", description=_LIST_TIERS_DESCRIPTION)
    async def list_tiers() -> dict[str, Any]:
        return await _counting("list_tiers", tools.list_tiers)

    @server.tool(name="get_job", description=_GET_JOB_DESCRIPTION)
    async def get_job(job_id: str) -> dict[str, Any]:
        return await _counting("get_job", lambda: tools.get_job(job_id))

    @server.tool(name="cancel_job", description=_CANCEL_JOB_DESCRIPTION)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return await _counting("cancel_job", lambda: tools.cancel_job(job_id))
