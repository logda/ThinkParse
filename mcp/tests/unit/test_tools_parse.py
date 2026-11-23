"""parse_document: submit, poll within the caller's budget, summarize."""

import time

import pytest

from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools

PDF_BYTES = b"%PDF-1.7 parse me"


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class FakeCtx:
    def __init__(self):
        self.report_calls = []

    async def report_progress(self, progress, total=None):
        self.report_calls.append((progress, total))


class ParseClient:
    """Fake gateway: upload always succeeds, get_job pops a scripted sequence."""

    def __init__(self, tiers=None, jobs=(), text=None):
        self.tiers = tiers or {
            "object": "list",
            "discovered": True,
            "data": [{"id": "basic"}],
        }
        self._jobs = list(jobs)
        self.text = "A" * 100 if text is None else text
        self.calls = []

    async def get_tiers(self):
        self.calls.append("get_tiers")
        return self.tiers

    async def create_upload(self, filename, size, mime_type, sha256sum=None):
        self.calls.append(("create_upload", filename, size, mime_type))
        return {"id": "upload_1", "object": "upload", "status": "pending"}

    async def put_upload_content(self, upload_id, content_factory, size):
        async for _ in content_factory():
            pass
        self.calls.append(("put", upload_id, size))

    async def complete_upload(self, upload_id, sha256sum):
        self.calls.append(("complete", upload_id, sha256sum))
        return {"id": upload_id, "status": "completed", "file": {"id": "file_1"}}

    async def create_job(self, file_id, tier, ocr_mode, batch_id=None, priority=0):
        self.calls.append(("create_job", file_id, tier, ocr_mode))
        return {
            "job_id": "job_1",
            "status": "queued",
            "tier": tier,
            "files": [{"output_files": None, "error": None}],
        }

    async def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        if len(self._jobs) > 1:
            return self._jobs.pop(0)
        return self._jobs[0]

    async def get_file_text(self, file_id):
        self.calls.append(("get_file_text", file_id))
        return self.text


def _queued():
    return {
        "job_id": "job_1",
        "status": "queued",
        "tier": "basic",
        "files": [{"output_files": None, "error": None}],
    }


def _running():
    return {
        "job_id": "job_1",
        "status": "running",
        "tier": "basic",
        "files": [{"output_files": None, "error": None}],
    }


def _completed():
    return {
        "job_id": "job_1",
        "status": "completed",
        "tier": "basic",
        "attempt": 1,
        "pages": 12,
        "timing": {"queue_ms": 5, "parse_ms": 500},
        "files": [
            {
                "output_files": {
                    "markdown": {"file_id": "file_md", "bytes": 3},
                    "middle_json": {"file_id": "file_mid", "bytes": 4},
                },
                "error": None,
            }
        ],
    }


def make_tools(client, **setting_overrides):
    return Tools(client, make_settings(**setting_overrides), Metrics())


def write_pdf(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BYTES)
    return str(pdf)


async def test_parse_wait_zero_returns_queued_view(tmp_path):
    client = ParseClient(jobs=[_queued()])
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto", wait_seconds=0
    )

    assert payload["job_id"] == "job_1"
    assert payload["status"] == "queued"
    assert "get_job" in payload["hint"]
    assert "summary" not in payload
    assert not any(call == "get_job" or call[0] == "get_job" for call in client.calls)


async def test_parse_polls_until_terminal(tmp_path):
    client = ParseClient(jobs=[_queued(), _running(), _completed()])
    tools = make_tools(
        client,
        MCP_ALLOWED_ROOT=str(tmp_path),
        MCP_POLL_INTERVAL_S="0.001",
        MCP_MAX_RESULT_CHARS="10",
    )

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto", wait_seconds=5
    )

    assert payload["status"] == "completed"
    assert payload["files"]["markdown"]["file_id"] == "file_md"
    assert payload["summary"] == "A" * 10
    assert payload["truncated"] is True
    assert ("get_file_text", "file_md") in client.calls


async def test_poll_never_sleeps_past_budget(tmp_path):
    client = ParseClient(jobs=[_running()])
    tools = make_tools(
        client, MCP_ALLOWED_ROOT=str(tmp_path), MCP_POLL_INTERVAL_S="0.05"
    )

    start = time.monotonic()
    payload = await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto", wait_seconds=2
    )
    elapsed = time.monotonic() - start

    assert payload["status"] == "running"
    assert "get_job" in payload["hint"]
    assert elapsed <= 2.3


async def test_failed_job_is_normal_return_not_error(tmp_path):
    failed = {
        "job_id": "job_1",
        "status": "failed",
        "tier": "basic",
        "attempt": 2,
        "pages": 3,
        "timing": {"queue_ms": 5, "parse_ms": 500},
        "files": [{"output_files": None, "error": {"message": "boom", "code": "E"}}],
    }
    tools = make_tools(ParseClient(jobs=[failed]), MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto", wait_seconds=5
    )

    assert payload["status"] == "failed"
    assert payload["error"]["message"] == "boom"
    assert payload["attempt"] == 2
    assert payload["timing"] == {"queue_ms": 5, "parse_ms": 500}


async def test_unavailable_tier_short_circuits_before_submit(tmp_path):
    client = ParseClient(
        tiers={"object": "list", "discovered": True, "data": [{"id": "flash"}]}
    )
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="flash"):
        await tools.parse_document(
            path=write_pdf(tmp_path), tier="basic", ocr_mode="auto"
        )
    assert not any(
        call[0] in {"create_job", "create_upload"} for call in client.calls
    )


async def test_progress_reported_each_round(tmp_path):
    client = ParseClient(jobs=[_queued(), _running(), _completed()])
    tools = make_tools(
        client, MCP_ALLOWED_ROOT=str(tmp_path), MCP_POLL_INTERVAL_S="0.001"
    )
    ctx = FakeCtx()

    await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto",
        wait_seconds=30, ctx=ctx,
    )

    assert len(ctx.report_calls) >= 2
    assert all(total == 30 for _, total in ctx.report_calls)
    progresses = [progress for progress, _ in ctx.report_calls]
    assert progresses == sorted(progresses)


async def test_invalid_ocr_mode_rejected(tmp_path):
    tools = make_tools(ParseClient(jobs=[_queued()]), MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="ocr_mode"):
        await tools.parse_document(
            path=write_pdf(tmp_path), ocr_mode="fast", wait_seconds=0
        )


async def test_register_tools_exposes_parse_document(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from thinkparse_mcp.tools import register_tools

    server = MCPServer("test")
    tools = make_tools(
        ParseClient(jobs=[_queued()]), MCP_ALLOWED_ROOT=str(tmp_path)
    )
    register_tools(server, tools)
    names = {tool.name for tool in await server.list_tools()}
    assert "parse_document" in names
