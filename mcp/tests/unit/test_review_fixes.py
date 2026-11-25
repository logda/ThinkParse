"""Final-review fix pass: each test reproduces one reviewer finding first."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from thinkparse_mcp.client import ThinkParseClient, UpstreamError
from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools, register_tools


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class RefusesCalls:
    """Client double that fails the test if the tool reaches the gateway."""

    def __getattr__(self, name):
        async def boom(*args, **kwargs):
            raise AssertionError(f"{name} must not be called")

        return boom


# --- C2: structured logs belong on stderr, never stdout (stdio transport) --


def test_log_event_writes_stderr_not_stdout(capsys):
    from thinkparse_mcp.observability import log_event

    log_event("tool_call", tool="parse_document", status="ok")
    out, err = capsys.readouterr()
    assert out == ""
    body = json.loads(err.strip().splitlines()[-1])
    assert body["event"] == "tool_call"


# --- C1: the production entrypoint must enforce identity on /mcp -----------


async def test_run_http_entrypoint_enforces_identity(monkeypatch):
    from thinkparse_mcp import server as server_module

    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app))
    server_module.run(make_settings(MCP_STATIC_TOKEN="s3cret"))
    transport = httpx.ASGITransport(app=captured["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/mcp")
    assert response.status_code == 401


# --- I3: the registered parse_document must receive the SDK Context ---------


async def test_registered_parse_document_passes_context(tmp_path, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    tools = Tools(RefusesCalls(), make_settings(), Metrics())
    server = MCPServer("test")
    register_tools(server, tools)

    seen = {}

    async def spy(
        path=None, url=None, file_id=None, tier="basic", ocr_mode="auto",
        wait_seconds=None, ctx=None,
    ):
        seen["ctx"] = ctx
        return {"job_id": "job_1", "status": "completed"}

    monkeypatch.setattr(tools, "parse_document", spy)
    await server.call_tool(
        "parse_document", {"path": str(tmp_path / "x.pdf"), "wait_seconds": 0}
    )
    assert seen["ctx"] is not None


# --- I4: get_result limit must be positive -----------------------------------


async def test_get_result_rejects_nonpositive_limit():
    tools = Tools(RefusesCalls(), make_settings(), Metrics())
    with pytest.raises(ToolError, match="limit"):
        await tools.get_result("job_1", kind="markdown", limit=-1)


# --- I5: submit_batch input guards and partial-failure reporting ------------


async def test_submit_batch_rejects_empty_paths():
    tools = Tools(RefusesCalls(), make_settings(), Metrics())
    with pytest.raises(ToolError, match="paths"):
        await tools.submit_batch([], "b1")


async def test_submit_batch_rejects_too_many_paths():
    tools = Tools(RefusesCalls(), make_settings(), Metrics())
    with pytest.raises(ToolError, match="100"):
        await tools.submit_batch(["x.pdf"] * 101, "b1")


class OneShotClient:
    """Serves the first upload, rejects the second (UpstreamError 400)."""

    def __init__(self):
        self.uploads = 0

    async def get_tiers(self):
        return {"object": "list", "discovered": True, "data": [{"id": "basic"}]}

    async def create_upload(self, filename, size, mime_type, sha256sum=None):
        self.uploads += 1
        if self.uploads == 2:
            raise UpstreamError(400, "gateway rejected the second file", False)
        return {"id": "upload_1", "status": "pending"}

    async def put_upload_content(self, upload_id, content, size):
        stream = content() if callable(content) else content
        async for _ in stream:
            pass

    async def complete_upload(self, upload_id, sha256sum):
        return {"id": upload_id, "status": "completed", "file": {"id": "file_1"}}

    async def create_job(self, file_id, tier, ocr_mode, batch_id=None, priority=0):
        return {"job_id": "job_0", "status": "queued"}


async def test_submit_batch_partial_failure_lists_created_jobs(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"%PDF-1.4 a")
    b = tmp_path / "b.pdf"
    b.write_bytes(b"%PDF-1.4 b")
    tools = Tools(
        OneShotClient(), make_settings(MCP_ALLOWED_ROOT=str(tmp_path)), Metrics()
    )
    with pytest.raises(ToolError, match="job_0"):
        await tools.submit_batch([str(a), str(b)], "b1")


# --- I2: a retried streamed PUT must send a fresh body ----------------------


async def test_put_upload_retries_5xx_with_fresh_body():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        if len(bodies) == 1:
            return httpx.Response(500)
        return httpx.Response(200)

    client = ThinkParseClient(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_s=0
    )
    try:
        async def factory():
            yield b"abc"

        await client.put_upload_content("upload_1", factory, 3)
    finally:
        await client.aclose()
    assert bodies == [b"abc", b"abc"]


# --- I6: download deadline, transport-error mapping, size default -----------


async def test_download_deadline_aborts_and_cleans_up(tmp_path):
    async def slow_body():
        yield b"%PDF-1.4 drip"
        await asyncio.sleep(1)
        yield b"tail"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=slow_body())

    from thinkparse_mcp.tools import _download_to_temp

    settings = make_settings(MCP_DOWNLOAD_DEADLINE_S="0.1")
    with pytest.raises(ToolError, match="deadline"):
        await _download_to_temp(
            "http://93.184.216.34/doc.pdf",
            settings,
            transport=httpx.MockTransport(handler),
            temp_dir=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_max_upload_bytes_defaults_to_a_safe_cap():
    assert make_settings().max_upload_bytes == 100 * 1024 * 1024
