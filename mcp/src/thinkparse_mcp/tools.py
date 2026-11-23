"""MCP tool implementations for the ThinkParse /api/v2 contract.

Tools are plain async methods on `Tools` so unit tests can drive them with a
fake client; `register_tools` binds them to an MCPServer with descriptions the
model reads (spec §6). All upstream HTTP goes through ThinkParseClient.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .client import ThinkParseClient, UpstreamError
from .config import Settings
from .observability import Metrics, log_event, monotonic_ms
from .security import SecurityError, fetch_public, resolve_local_path, sniff_mime

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


_CHUNK_BYTES = 1024 * 1024
_HEAD_BYTES = 32


def _ext_and_name(url: str) -> tuple[str, str]:
    """Filename and extension from a URL's path."""
    name = httpx.URL(url).path.rsplit("/", 1)[-1] or "download"
    ext = Path(name).suffix
    return ext, name


async def _file_chunks(path: Path) -> Any:
    """Yield a file's bytes in upload-sized chunks."""
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            yield chunk


async def _download_to_temp(
    url: str,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver=None,
    temp_dir: Path | None = None,
) -> tuple[Path, str, int]:
    """Download a URL into a 0600 temp file with size and content checks.

    Returns (path, sha256sum, size); the caller deletes the temp file. On any
    rejection (oversize, magic-number mismatch, fetch failure) the temp file
    is removed here. Every hop is re-validated by fetch_public (spec §8).
    """
    max_bytes = settings.max_upload_bytes
    ext, _ = _ext_and_name(url)
    fd, name = tempfile.mkstemp(dir=None if temp_dir is None else str(temp_dir))
    path = Path(name)
    sha = hashlib.sha256()
    size = 0
    head = b""
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False, transport=transport
    )
    response: httpx.Response | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            response = await fetch_public(
                url, client, allowed_ports=settings.allowed_ports, resolver=resolver
            )
            if response.is_error:
                raise ToolError(f"the download failed: HTTP {response.status_code}")
            async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                if max_bytes is not None and size + len(chunk) > max_bytes:
                    raise ToolError(
                        f"the download exceeds the ThinkParse size limit of "
                        f"{max_bytes} bytes"
                    )
                if len(head) < _HEAD_BYTES:
                    head = (head + chunk)[:_HEAD_BYTES]
                fh.write(chunk)
                sha.update(chunk)
                size += len(chunk)
        if sniff_mime(head, ext) is None:
            raise ToolError(
                f"content does not match the {ext or 'declared'} extension; refusing to upload"
            )
        return path, sha.hexdigest(), size
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()


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

    async def _source_to_file(
        self, path: str | None, url: str | None
    ) -> tuple[str, str, int]:
        """Turn a local path or URL into a ThinkParse file id.

        Returns (file_id, sha256sum, size). The upload is the gateway's three
        steps — create, PUT content, complete — with the sha256 sent on create
        so a known blob short-circuits to a completed upload (spec §6.4).
        """
        if (path is None) == (url is None):
            raise ToolError("provide exactly one of path or url")
        try:
            return await self._upload_source(path, url)
        except SecurityError as exc:
            raise ToolError(str(exc)) from exc
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc

    async def _upload_source(
        self, path: str | None, url: str | None
    ) -> tuple[str, str, int]:
        if url is not None:
            ext, name = _ext_and_name(url)
            temp, sha, size = await _download_to_temp(url, self.settings)
            try:
                with open(temp, "rb") as fh:
                    head = fh.read(_HEAD_BYTES)
                mime = sniff_mime(head, ext)
                if mime is None:  # already validated by the download; belt and braces
                    raise ToolError("content does not match the declared extension")
                return await self._three_step_upload(temp, name, mime, sha, size)
            finally:
                temp.unlink(missing_ok=True)
        assert path is not None
        resolved = resolve_local_path(path, self.settings.allowed_roots)
        sha = hashlib.sha256()
        size = 0
        head = b""
        with open(resolved, "rb") as fh:
            while chunk := fh.read(_CHUNK_BYTES):
                sha.update(chunk)
                size += len(chunk)
                if len(head) < _HEAD_BYTES:
                    head = (head + chunk)[:_HEAD_BYTES]
        mime = sniff_mime(head, resolved.suffix)
        if mime is None:
            raise ToolError(
                f"content does not match the {resolved.suffix or 'declared'} extension; "
                "refusing to upload"
            )
        return await self._three_step_upload(
            resolved, resolved.name, mime, sha.hexdigest(), size
        )

    async def _three_step_upload(
        self,
        source: Path,
        filename: str,
        mime_type: str,
        sha256sum: str,
        size: int,
    ) -> tuple[str, str, int]:
        created = await self.client.create_upload(
            filename, size, mime_type, sha256sum=sha256sum
        )
        existing = created.get("file") or {}
        file_id = existing.get("id")
        if created.get("status") == "completed" and file_id:
            return str(file_id), sha256sum, size
        upload_id = created.get("id")
        if not upload_id:
            raise ToolError("the gateway returned an upload without an id")
        await self.client.put_upload_content(
            upload_id, _file_chunks(source), size
        )
        completed = await self.client.complete_upload(upload_id, sha256sum)
        file_id = (completed.get("file") or {}).get("id")
        if not file_id:
            raise ToolError("the upload completed without a file id")
        return str(file_id), sha256sum, size


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
