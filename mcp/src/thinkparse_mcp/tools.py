"""MCP tool implementations for the ThinkParse /api/v2 contract.

Tools are plain async methods on `Tools` so unit tests can drive them with a
fake client; `register_tools` binds them to an MCPServer with descriptions the
model reads (spec §6). All upstream HTTP goes through ThinkParseClient.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

# Runtime import: the registered parse_document annotates ctx: Context so
# the SDK injects it, and typing.get_type_hints resolves the annotation
# against this module's globals.
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError as SDKToolError

from .client import ThinkParseClient, UpstreamError
from .config import Settings
from .observability import Metrics, log_event, monotonic_ms
from .security import SecurityError, fetch_public, resolve_local_path, sniff_mime

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


class ToolError(SDKToolError):
    """An error whose message is safe and useful for the calling model.

    Subclasses the SDK's ToolError so the server surfaces the message to the
    client (is_error=True) instead of the generic crash text.
    """


TERMINAL_STATES = {"completed", "failed", "canceled"}
OCR_MODES = ("auto", "txt", "ocr")
UPLOAD_HINT = (
    "upload the file with PUT /uploads?filename=<name> (same Bearer token, "
    "raw body, Content-Length required), then pass the returned file_id"
)
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
        trust_env=False,
        follow_redirects=False,
        transport=transport,
        timeout=httpx.Timeout(60.0, connect=5.0),  # spec §7: read 60s, connect 5s
    )
    response: httpx.Response | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            response = await fetch_public(
                url, client, allowed_ports=settings.allowed_ports, resolver=resolver
            )
            if response.is_error:
                raise ToolError(f"the download failed: HTTP {response.status_code}")
            deadline = time.monotonic() + settings.download_deadline_s
            async with asyncio.timeout_at(deadline):
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
    except TimeoutError as exc:
        path.unlink(missing_ok=True)
        raise ToolError(
            f"the download exceeded the {settings.download_deadline_s}s deadline; "
            "aborting"
        ) from exc
    except httpx.TransportError as exc:
        path.unlink(missing_ok=True)
        raise ToolError(f"the download failed: {exc}") from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()


RESULT_KINDS = ("markdown", "middle_json", "content_list", "images")

# Same charset the gateway accepts for batch ids (see control/gateway.py).
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_BATCH_FILES = 100


def _attach_created_jobs(exc: ToolError, jobs: list[dict[str, Any]]) -> ToolError:
    """A mid-batch failure still reports the jobs that were created."""
    if not jobs:
        return exc
    ids = [job["job_id"] for job in jobs]
    return ToolError(f"{exc}; jobs already created under this batch: {ids}")


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

    async def get_result(
        self,
        job_id: str,
        kind: str = "markdown",
        offset: int = 0,
        limit: int | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Page through a completed job's outputs (spec §6.3).

        Text kinds fetch the whole file from the gateway and slice client-side
        (the gateway has no range support); slicing is by Python codepoint, so
        emoji and CJK never split mid-character.
        """
        if offset < 0:
            raise ToolError("offset must be a non-negative number of characters")
        if limit is not None and limit <= 0:
            raise ToolError("limit must be a positive number of characters (or entries)")
        if kind == "content_list":
            raise ToolError(
                "v2 未暴露 content_list 独立产物，请用 kind='middle_json'（可配 path 过滤）"
            )
        if kind not in RESULT_KINDS:
            raise ToolError(
                f"kind must be one of {list(RESULT_KINDS)}, got {kind!r}"
            )
        try:
            job = await self.client.get_job(job_id)
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        if job.get("status") != "completed":
            raise ToolError(
                f"job {job_id} is {job.get('status')}; call get_job(job_id={job_id}) "
                "until it is completed"
            )
        outputs = (job.get("files") or [{}])[0].get("output_files") or {}
        if kind == "images":
            return self._image_page(job_id, outputs, offset, limit)
        file_id = (outputs.get(kind) or {}).get("file_id")
        if not file_id:
            raise ToolError(
                f"job {job_id} has no {kind} output; call get_job(job_id={job_id}) "
                "to list what it produced"
            )
        try:
            text = await self.client.get_file_text(str(file_id))
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        if kind == "middle_json" and path:
            text = self._json_subtree(text, path)
        max_chars = self.settings.max_result_chars
        page_limit = max_chars if limit is None else min(int(limit), max_chars)
        total = len(text)
        page = text[offset : offset + page_limit]
        end = offset + page_limit
        return {
            "kind": kind,
            "total_chars": total,
            "offset": offset,
            "next_offset": end if end < total else None,
            "text": page,
            "items": None,
            "eof": end >= total,
        }

    def _image_page(
        self, job_id: str, outputs: dict[str, Any], offset: int, limit: int | None
    ) -> dict[str, Any]:
        images = outputs.get("images") or []
        page_limit = DEFAULT_LIST_LIMIT if limit is None else min(int(limit), MAX_LIST_LIMIT)
        end = offset + page_limit
        items = [
            {
                "filename": image.get("filename"),
                "bytes": image.get("bytes"),
                "uri": f"thinkparse://jobs/{job_id}/images/{image.get('filename')}",
            }
            for image in images[offset:end]
        ]
        total = len(images)
        return {
            "kind": "images",
            "total_chars": total,
            "offset": offset,
            "next_offset": end if end < total else None,
            "text": None,
            "items": items,
            "eof": end >= total,
        }

    @staticmethod
    def _json_subtree(text: str, path: str) -> str:
        """Extract a dot-path subtree from JSON text and re-serialize it."""
        try:
            value: Any = json.loads(text)
            for part in path.split("."):
                if isinstance(value, dict):
                    value = value[part]
                elif isinstance(value, list) and part.lstrip("-").isdigit():
                    value = value[int(part)]
                else:
                    raise KeyError(part)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ToolError(f"path {path!r} does not resolve in the middle_json") from exc
        return json.dumps(value, ensure_ascii=False)

    async def submit_batch(
        self,
        paths: list[str] | None = None,
        batch_id: str = "",
        tier: str = "basic",
        ocr_mode: str = "auto",
        file_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit several files under one batch id.

        Provide exactly one of paths (server-local files, uploaded through the
        same three-step pipeline as parse_document) or file_ids (already
        uploaded via PUT /uploads). The jobs start queued and are polled
        individually with get_job / get_batch.
        """
        if not _BATCH_ID_RE.fullmatch(batch_id):
            raise ToolError(
                "batch_id must be 1-64 characters of letters, digits, '_' or '-'"
            )
        if ocr_mode not in OCR_MODES:
            raise ToolError(f"ocr_mode must be one of {list(OCR_MODES)}")
        if (paths is None) == (file_ids is None):
            raise ToolError("provide exactly one of paths or file_ids")
        sources = paths if paths is not None else file_ids
        assert sources is not None
        if not sources:
            raise ToolError("paths/file_ids must contain at least one file")
        if len(sources) > _MAX_BATCH_FILES:
            raise ToolError(f"a batch is limited to {_MAX_BATCH_FILES} files")
        if paths is not None:
            self._check_local_path_enabled("batch")
        start = monotonic_ms()
        jobs: list[dict[str, Any]] = []
        try:
            await self.tiers.validate(tier)
            for source in sources:
                if paths is not None:
                    file_id, _sha, _size = await self._source_to_file(source, None)
                else:
                    file_id = source
                job = await self.client.create_job(
                    file_id, tier, ocr_mode, batch_id=batch_id
                )
                job_id = job.get("job_id")
                if not job_id:
                    raise ToolError("the gateway created a job without an id")
                jobs.append({"job_id": job_id, "status": job.get("status", "queued")})
        except UpstreamError as exc:
            raise _attach_created_jobs(_map_upstream_error(exc), jobs) from exc
        except ToolError as exc:
            raise _attach_created_jobs(exc, jobs) from exc
        log_event(
            "submit_batch",
            batch_id=batch_id,
            files=len(sources),
            tier=tier,
            duration_ms=round(monotonic_ms() - start, 1),
        )
        return {"batch_id": batch_id, "jobs": jobs}

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        """Summarize one batch: status counts plus its jobs with errors."""
        try:
            batch = await self.client.get_batch(batch_id)
            listing = await self.client.get_batch_jobs(batch_id)
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        jobs: list[dict[str, Any]] = []
        for entry in listing.get("data") or []:
            view: dict[str, Any] = {
                "job_id": entry.get("job_id"),
                "status": entry.get("status"),
            }
            error = (entry.get("files") or [{}])[0].get("error")
            if error:
                view["error"] = error
            jobs.append(view)
        return {**batch, "jobs": jobs}

    async def job_markdown(self, job_id: str) -> str:
        """Full markdown of a completed job, truncated at max_result_chars."""
        outputs = await self._completed_outputs(job_id)
        file_id = (outputs.get("markdown") or {}).get("file_id")
        if not file_id:
            raise ToolError(
                f"job {job_id} has no markdown output; call get_job(job_id={job_id}) "
                "to list what it produced"
            )
        try:
            text = await self.client.get_file_text(str(file_id))
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        limit = self.settings.max_result_chars
        if len(text) > limit:
            return (
                text[:limit]
                + f"\n\n[truncated: showing {limit} of {len(text)} characters; "
                "continue with get_result pagination]"
            )
        return text

    async def job_image(self, job_id: str, name: str) -> bytes:
        """One image output of a completed job, as raw bytes."""
        outputs = await self._completed_outputs(job_id)
        file_id = next(
            (
                image.get("file_id")
                for image in outputs.get("images") or []
                if image.get("filename") == name
            ),
            None,
        )
        if not file_id:
            raise ToolError(f"job {job_id} has no image named {name!r}")
        try:
            return await self.client.get_file_bytes(str(file_id))
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc

    async def _completed_outputs(self, job_id: str) -> dict[str, Any]:
        try:
            job = await self.client.get_job(job_id)
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        if job.get("status") != "completed":
            raise ToolError(
                f"job {job_id} is {job.get('status')}; call get_job(job_id={job_id}) "
                "until it is completed"
            )
        return (job.get("files") or [{}])[0].get("output_files") or {}

    async def _wait_for_terminal(
        self, job_id: str, wait_seconds: int, ctx: Any = None
    ) -> dict[str, Any]:
        """Poll get_job until a terminal state or the caller's budget runs out."""
        interval = self.settings.poll_interval_s
        deadline = time.monotonic() + wait_seconds
        while True:
            job = await self.client.get_job(job_id)
            if job.get("status") in TERMINAL_STATES:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return job
            if ctx is not None:
                elapsed = min(wait_seconds, int(wait_seconds - remaining))
                await ctx.report_progress(elapsed, total=wait_seconds)
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * 1.5, self.settings.poll_max_interval_s)

    async def parse_document(
        self,
        path: str | None = None,
        url: str | None = None,
        file_id: str | None = None,
        tier: str = "basic",
        ocr_mode: str = "auto",
        wait_seconds: int | None = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Upload a document, submit it, and wait within the given budget.

        The return contract is spec §6.1: a failed job is a normal return with
        `error` populated, never a raised error.
        """
        if ocr_mode not in OCR_MODES:
            raise ToolError(f"ocr_mode must be one of {list(OCR_MODES)}")
        if wait_seconds is None:
            wait_seconds = self.settings.default_wait_s
        wait_seconds = max(0, min(int(wait_seconds), self.settings.max_wait_s))
        start = monotonic_ms()
        try:
            await self.tiers.validate(tier)
            file_id, sha, size = await self._resolve_source(path, url, file_id)
            job = await self.client.create_job(file_id, tier, ocr_mode)
            job_id = job.get("job_id")
            if not job_id:
                raise ToolError("the gateway created a job without an id")
            if wait_seconds > 0:
                job = await self._wait_for_terminal(job_id, wait_seconds, ctx=ctx)
            payload = {}
            if job.get("status") == "completed":
                outputs = (job.get("files") or [{}])[0].get("output_files") or {}
                markdown = outputs.get("markdown")
                if markdown and markdown.get("file_id"):
                    text = await self.client.get_file_text(str(markdown["file_id"]))
                    limit = self.settings.max_result_chars
                    payload = {
                        "summary": text[:limit],
                        "truncated": len(text) > limit,
                    }
        except UpstreamError as exc:
            raise _map_upstream_error(exc) from exc
        view = _job_view(job)
        status = view["status"]
        hint = (
            None
            if status in TERMINAL_STATES
            else f"job still running; call get_job(job_id={view['job_id']})"
        )
        result: dict[str, Any] = {
            "job_id": view["job_id"],
            "status": status,
            "tier": view["tier"],
            "attempt": view["attempt"],
            "pages": view["pages"],
            "timing": view["timing"],
            "files": view["files"],
            "error": view["error"],
            "hint": hint,
            "truncated": False,
        }
        result.update(payload)
        log_event(
            "parse_document",
            file_sha256=sha,
            bytes=size,
            tier=tier,
            ocr_mode=ocr_mode,
            status=status,
            duration_ms=round(monotonic_ms() - start, 1),
        )
        return result

    async def _resolve_source(
        self, path: str | None, url: str | None, file_id: str | None
    ) -> tuple[str, str | None, int | None]:
        """Turn exactly one of path / url / file_id into a gateway file id."""
        provided = [source for source in (path, url, file_id) if source is not None]
        if len(provided) != 1:
            raise ToolError("provide exactly one of path, url or file_id")
        if file_id is not None:
            try:
                meta = await self.client.get_file_meta(file_id)
            except UpstreamError as exc:
                raise _map_upstream_error(exc) from exc
            return file_id, meta.get("sha256sum"), meta.get("bytes")
        self._check_local_path_enabled(path)
        return await self._source_to_file(path, url)

    def _check_local_path_enabled(self, path: str | None) -> None:
        """Remote deployments cannot see client paths; point at PUT /uploads."""
        if path is not None and not self.settings.enable_local_path:
            raise ToolError(
                "path refers to a file on the MCP server, not on the client, "
                "and local paths are disabled in this deployment; " + UPLOAD_HINT
            )

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
            upload_id, lambda: _file_chunks(source), size
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


_PARSE_DOCUMENT_DESCRIPTION = (
    "Parse a document into markdown and structured JSON. Provide exactly one "
    "source: file_id (a file already uploaded to this server via "
    "PUT /uploads?filename=<name> with the same Bearer token — the way to "
    "parse a client-local file of any size), url (a public URL the server "
    "fetches), or path (a file on the MCP server itself; only enabled in "
    "local/mounted deployments). Submits the job and waits up to wait_seconds "
    "for the result. wait_seconds=0 returns immediately with the job id; poll "
    "with get_job afterwards. A failed parse is returned as status='failed' "
    "with error details, not as an exception."
)


_GET_RESULT_DESCRIPTION = (
    "Read a completed job's outputs, paginated. kind='markdown' or "
    "'middle_json' returns text sliced by character offset/limit (limit caps "
    "at the server's max_result_chars); kind='middle_json' also accepts a dot "
    "path to return one subtree. kind='images' pages the image list by "
    "entries. Use the returned next_offset to continue; eof=true means done."
)

_SUBMIT_BATCH_DESCRIPTION = (
    "Parse several files under one batch: provide exactly one of file_ids "
    "(files already uploaded via PUT /uploads) or paths (server-local files, "
    "only in local/mounted deployments). Submits each as a job sharing the "
    "same batch_id (1-64 chars of letters, digits, '_' or '-') and returns "
    "the created job ids. Poll individual jobs with get_job, or summarize "
    "the whole batch with get_batch."
)

_GET_BATCH_DESCRIPTION = (
    "Summarize a batch: per-status job counts, pages completed, and the list "
    "of its jobs — with error details for the failed ones."
)

_MARKDOWN_RESOURCE_DESCRIPTION = (
    "The full markdown output of a completed parse job, as text. Content "
    "beyond the server's max_result_chars is cut with a note pointing at "
    "the get_result tool for pagination."
)

_IMAGE_RESOURCE_DESCRIPTION = (
    "One image output of a completed parse job, as raw bytes; name is the "
    "image filename as listed by get_result(kind='images')."
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

    @server.tool(name="parse_document", description=_PARSE_DOCUMENT_DESCRIPTION)
    async def parse_document(
        path: str | None = None,
        url: str | None = None,
        file_id: str | None = None,
        tier: str = "basic",
        ocr_mode: str = "auto",
        wait_seconds: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await _counting(
            "parse_document",
            lambda: tools.parse_document(
                path=path,
                url=url,
                file_id=file_id,
                tier=tier,
                ocr_mode=ocr_mode,
                wait_seconds=wait_seconds,
                ctx=ctx,
            ),
        )

    @server.tool(name="get_result", description=_GET_RESULT_DESCRIPTION)
    async def get_result(
        job_id: str,
        kind: str = "markdown",
        offset: int = 0,
        limit: int | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        return await _counting(
            "get_result",
            lambda: tools.get_result(
                job_id, kind=kind, offset=offset, limit=limit, path=path
            ),
        )

    @server.tool(name="submit_batch", description=_SUBMIT_BATCH_DESCRIPTION)
    async def submit_batch(
        batch_id: str,
        paths: list[str] | None = None,
        file_ids: list[str] | None = None,
        tier: str = "basic",
        ocr_mode: str = "auto",
    ) -> dict[str, Any]:
        return await _counting(
            "submit_batch",
            lambda: tools.submit_batch(
                paths, batch_id, tier=tier, ocr_mode=ocr_mode, file_ids=file_ids
            ),
        )

    @server.tool(name="get_batch", description=_GET_BATCH_DESCRIPTION)
    async def get_batch(batch_id: str) -> dict[str, Any]:
        return await _counting("get_batch", lambda: tools.get_batch(batch_id))

    @server.resource(
        "thinkparse://jobs/{job_id}/markdown",
        name="job_markdown",
        description=_MARKDOWN_RESOURCE_DESCRIPTION,
    )
    async def job_markdown(job_id: str) -> str:
        return await tools.job_markdown(job_id)

    @server.resource(
        "thinkparse://jobs/{job_id}/images/{name}",
        name="job_image",
        description=_IMAGE_RESOURCE_DESCRIPTION,
    )
    async def job_image(job_id: str, name: str) -> bytes:
        return await tools.job_image(job_id, name)
