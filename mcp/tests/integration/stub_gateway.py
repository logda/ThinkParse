"""An in-memory FastAPI stub of the ThinkParse /api/v2 contract.

Matches the gateway's verified semantics: three-step uploads (byte-count
check, sha256 dedupe on create, `sha256sum` key on complete), lazy job
progression to completed after `delay_s`, durable cancellation, and Bearer
key enforcement. Tests only; fastapi lives in the dev group.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response


@dataclass
class StubState:
    tiers: list[str] = field(default_factory=lambda: ["basic", "flash"])
    api_key: str = "stub-key"
    delay_s: float = 0.0
    markdown_text: str = "# parsed\n\nthinkparse stub output\n"
    middle_json: dict[str, Any] = field(
        default_factory=lambda: {"pdf_info": {"pages": 1}}
    )
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[str, str] = field(default_factory=dict)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)


def _now() -> float:
    return time.monotonic()


def create_app(state: StubState) -> FastAPI:
    app = FastAPI()

    def require_key(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {state.api_key}":
            raise HTTPException(status_code=401, detail="invalid api key")

    def _upload_body(
        upload_id: str, record: dict[str, Any], status: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": upload_id,
            "object": "upload",
            "bytes": record["bytes"],
            "filename": record["filename"],
            "mime_type": record["mime_type"],
            "status": status,
            "sha256sum": record.get("sha"),
        }
        if record.get("file_id"):
            body["file"] = {"id": record["file_id"]}
        else:
            body["upload_url"] = f"/api/v2/uploads/{upload_id}/content"
        return body

    @app.get("/api/v2/health")
    def health(request: Request) -> dict[str, str]:
        require_key(request)
        return {"status": "ok"}

    @app.get("/api/v2/tiers")
    def tiers(request: Request) -> dict[str, Any]:
        require_key(request)
        return {
            "object": "list",
            "discovered": True,
            "data": [{"id": tier} for tier in state.tiers],
        }

    @app.post("/api/v2/uploads")
    async def create_upload(request: Request) -> dict[str, Any]:
        require_key(request)
        body = await request.json()
        record = {
            "filename": str(body["filename"]),
            "bytes": int(body["bytes"]),
            "mime_type": str(body["mime_type"]),
            "sha": body.get("sha256sum"),
        }
        sha = record["sha"]
        if sha and sha in state.blobs:
            record["file_id"] = state.blobs[sha]
            upload_id = "upload_" + secrets.token_hex(8)
            state.uploads[upload_id] = record
            return _upload_body(upload_id, record, "completed")
        upload_id = "upload_" + secrets.token_hex(8)
        state.uploads[upload_id] = record
        return _upload_body(upload_id, record, "pending")

    @app.put("/api/v2/uploads/{upload_id}/content")
    async def upload_content(upload_id: str, request: Request) -> Response:
        require_key(request)
        record = state.uploads.get(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail="upload not found")
        if record.get("file_id"):
            raise HTTPException(status_code=409, detail="upload is not pending")
        data = await request.body()
        if len(data) != record["bytes"]:
            raise HTTPException(status_code=400, detail="upload byte count does not match")
        record["data"] = data
        return Response(status_code=200)

    @app.post("/api/v2/uploads/{upload_id}/complete")
    async def complete_upload(upload_id: str, request: Request) -> dict[str, Any]:
        require_key(request)
        record = state.uploads.get(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail="upload not found")
        if record.get("file_id"):
            return _upload_body(upload_id, record, "completed")
        data = record.get("data")
        if data is None:
            raise HTTPException(status_code=409, detail="upload has no content")
        raw = await request.body()
        sha = json.loads(raw).get("sha256sum") if raw else None
        digest = hashlib.sha256(data).hexdigest()
        if sha and sha != digest:
            raise HTTPException(status_code=400, detail="sha256sum mismatch")
        file_id = "file_" + secrets.token_hex(8)
        state.files[file_id] = {
            "filename": record["filename"],
            "bytes": data,
            "mime_type": record["mime_type"],
            "sha": digest,
        }
        state.blobs[digest] = file_id
        record["file_id"] = file_id
        return _upload_body(upload_id, record, "completed")

    @app.get("/api/v2/files/{file_id}/content")
    def file_content(file_id: str, request: Request) -> Response:
        require_key(request)
        record = state.files.get(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="file not found")
        return Response(content=record["bytes"], media_type=record["mime_type"])

    @app.post("/api/v2/jobs", status_code=202)
    async def create_job(request: Request) -> dict[str, Any]:
        require_key(request)
        body = await request.json()
        file_id = str(body.get("file_id") or "")
        record = state.files.get(file_id)
        if record is None:
            raise HTTPException(status_code=400, detail="unknown file_id")
        tier = body.get("tier")
        if tier not in state.tiers:
            raise HTTPException(status_code=400, detail=f"unknown tier {tier}")
        job_id = "job_" + secrets.token_hex(8)
        state.jobs[job_id] = {
            "job_id": job_id,
            "tier": tier,
            "file_id": file_id,
            "filename": record["filename"],
            "created_at": _now(),
            "canceled": False,
        }
        return _job_body(state.jobs[job_id], "queued")

    def _current(job: dict[str, Any]) -> str:
        if job["canceled"]:
            return "canceled"
        if _now() - job["created_at"] >= state.delay_s:
            job.setdefault("outputs", _outputs(state))
            return "completed"
        return "running"

    def _job_body(job: dict[str, Any], status: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "job_id": job["job_id"],
            "status": status,
            "tier": job["tier"],
            "attempt": 1,
            "pages": 3,
            "timing": {"queue_ms": 5, "parse_ms": 50, "project_ms": 1},
            "links": {"self": f"/api/v2/jobs/{job['job_id']}"},
        }
        if status == "completed":
            body["files"] = [
                {
                    "name": job["filename"],
                    "status": "completed",
                    "output_files": job.get("outputs"),
                    "error": None,
                }
            ]
        else:
            body["files"] = [
                {"name": job["filename"], "status": status, "output_files": None, "error": None}
            ]
        return body

    def _outputs(state: StubState) -> dict[str, Any]:
        markdown = "file_md_" + secrets.token_hex(4)
        middle = "file_mid_" + secrets.token_hex(4)
        image = "file_img_" + secrets.token_hex(4)
        state.files[markdown] = {
            "filename": "doc.md",
            "bytes": state.markdown_text.encode("utf-8"),
            "mime_type": "text/markdown",
            "sha": "0",
        }
        middle_text = json.dumps(state.middle_json, ensure_ascii=False)
        state.files[middle] = {
            "filename": "doc.json",
            "bytes": middle_text.encode("utf-8"),
            "mime_type": "application/json",
            "sha": "0",
        }
        state.files[image] = {
            "filename": "page-0.jpg",
            "bytes": b"\xff\xd8\xff stub",
            "mime_type": "image/jpeg",
            "sha": "0",
        }
        return {
            "markdown": {"file_id": markdown, "bytes": len(state.markdown_text)},
            "middle_json": {"file_id": middle, "bytes": len(middle_text)},
            "images": [{"file_id": image, "filename": "page-0.jpg", "bytes": 10}],
        }

    @app.get("/api/v2/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        require_key(request)
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_body(job, _current(job))

    @app.delete("/api/v2/jobs/{job_id}")
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        require_key(request)
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        job["canceled"] = True
        return {"job_id": job_id, "status": "canceled"}

    return app
