"""PUT /uploads: the client-side file ingress for remote deployments.

MCP tool arguments are JSON text, so file bytes cannot ride the MCP channel
for anything but tiny documents. This route lets a client stream a file to
the MCP server with the same Bearer token it uses for /mcp; the server stages
it in a 0600 temp file, then reuses the Tools three-step gateway upload
(create with sha256 -> PUT content -> complete), keeping dedupe and retries.
The returned file_id feeds parse_document(file_id=...) / submit_batch.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .client import UpstreamError
from .observability import log_event, monotonic_ms
from .security import ALLOWED_EXTENSIONS, sniff_mime
from .tools import ToolError

if TYPE_CHECKING:
    from .config import Settings
    from .tools import Tools

_HEAD_BYTES = 32


def _sanitize_filename(raw: str | None) -> str:
    """Reject anything but a plain basename with an allowed extension."""
    if not raw or not raw.strip():
        raise ToolError("the filename query parameter is required")
    name = raw.strip()
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ToolError("filename must be a plain basename, not a path")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ToolError(
            f"extension {Path(name).suffix or '<none>'!r} is not allowed; "
            f"allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )
    return name


async def _stage_body(request: Request, settings: "Settings") -> tuple[Path, str, int]:
    """Stream the request body into a temp file; return (path, sha256, size).

    Enforces the declared Content-Length, the configured size cap (both up
    front and while streaming) and an overall deadline. The caller deletes
    the temp file.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        raise ToolError("Content-Length is required; send the file as a raw body")
    try:
        declared_size = int(declared)
    except ValueError as exc:
        raise ToolError("Content-Length must be an integer") from exc
    if declared_size < 0:
        raise ToolError("Content-Length must not be negative")
    max_bytes = settings.max_upload_bytes
    if max_bytes is not None and declared_size > max_bytes:
        raise _SizeLimit(f"the upload exceeds the size limit of {max_bytes} bytes")

    fd, name = tempfile.mkstemp()
    path = Path(name)
    sha = hashlib.sha256()
    size = 0
    try:
        deadline = time.monotonic() + settings.upload_deadline_s
        with os.fdopen(fd, "wb") as fh:
            async with asyncio.timeout_at(deadline):
                async for chunk in request.stream():
                    if max_bytes is not None and size + len(chunk) > max_bytes:
                        raise _SizeLimit(
                            f"the upload exceeds the size limit of {max_bytes} bytes"
                        )
                    fh.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
        if size != declared_size:
            raise ToolError(
                f"body has {size} bytes but Content-Length declared {declared_size}"
            )
        if size == 0:
            raise ToolError("the upload body is empty")
        return path, sha.hexdigest(), size
    except TimeoutError as exc:
        path.unlink(missing_ok=True)
        raise ToolError(
            f"the upload exceeded the {settings.upload_deadline_s}s deadline; aborting"
        ) from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class _SizeLimit(ToolError):
    """Oversize upload; surfaced as HTTP 413 instead of 400."""


async def handle_upload(request: Request, tools: "Tools", settings: "Settings") -> Response:
    """Receive one client file and return its gateway file_id."""
    start = monotonic_ms()
    temp: Path | None = None
    try:
        filename = _sanitize_filename(request.query_params.get("filename"))
        temp, sha, size = await _stage_body(request, settings)
        with open(temp, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
        mime = sniff_mime(head, Path(filename).suffix)
        if mime is None:
            raise ToolError(
                f"content does not match the {Path(filename).suffix!r} extension; "
                "refusing to upload"
            )
        file_id, _sha, _size = await tools._three_step_upload(temp, filename, mime, sha, size)
    except _SizeLimit as exc:
        return _error(413, str(exc), start)
    except ToolError as exc:
        return _error(400, str(exc), start)
    except UpstreamError as exc:
        status = 502 if exc.retryable else exc.status
        return _error(status, str(exc), start)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    log_event(
        "upload",
        filename=filename,
        file_sha256=sha,
        bytes=size,
        file_id=file_id,
        duration_ms=round(monotonic_ms() - start, 1),
    )
    return JSONResponse(
        {
            "file_id": file_id,
            "filename": filename,
            "bytes": size,
            "sha256sum": sha,
            "status": "completed",
            "hint": f"call parse_document(file_id={file_id}) to submit a parse job",
        }
    )


def _error(status: int, detail: str, start: float) -> JSONResponse:
    log_event(
        "upload",
        status="error",
        http_status=status,
        detail=detail,
        duration_ms=round(monotonic_ms() - start, 1),
    )
    return JSONResponse({"error": detail}, status_code=status)