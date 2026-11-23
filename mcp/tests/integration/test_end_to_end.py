"""End-to-end tests against the in-memory v2 gateway stub (spec §11).

Run with MCP_IT=1 (or --run-integration); skipped by default.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest
import uvicorn

from thinkparse_mcp.client import ThinkParseClient
from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools

from .stub_gateway import StubState, create_app

import sys

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_IT") != "1" and "--run-integration" not in sys.argv,
        reason="set MCP_IT=1 (or pass --run-integration) to run integration tests",
    ),
]


def make_settings(base_url: str, api_key: str = "stub-key", **overrides) -> Settings:
    env = {
        "THINKPARSE_BASE_URL": base_url,
        "THINKPARSE_API_KEY": api_key,
        "MCP_TRUSTED_PROXY_CIDRS": "127.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


@pytest.fixture
async def gateway():
    state = StubState()
    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    await task


def make_tools(base_url: str, **setting_overrides) -> Tools:
    settings = make_settings(base_url, **setting_overrides)
    client = ThinkParseClient(settings, retry_backoff_s=0.05)
    return Tools(client, settings, Metrics())


def write_pdf(tmp_path) -> str:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub integration pdf")
    return str(pdf)


async def wait_terminal(tools: Tools, job_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = await tools.get_job(job_id)
        if job["status"] in {"completed", "failed", "canceled"}:
            return job
        assert asyncio.get_running_loop().time() < deadline, "job never terminated"
        await asyncio.sleep(0.02)


async def test_local_pdf_parses_to_full_markdown(gateway, tmp_path):
    url, state = gateway
    state.markdown_text = "# thinkparse\n\n解析结果\n"
    tools = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), tier="basic", ocr_mode="auto", wait_seconds=10
    )

    assert payload["status"] == "completed"
    assert payload["summary"] == state.markdown_text
    result = await tools.get_result(payload["job_id"], kind="markdown")
    assert result["text"] == state.markdown_text
    assert result["eof"] is True


async def test_wait_zero_then_poll_to_completion(gateway, tmp_path):
    url, state = gateway
    state.delay_s = 0.25
    tools = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), wait_seconds=0
    )

    assert payload["status"] in {"queued", "running"}
    job = await wait_terminal(tools, payload["job_id"])
    assert job["status"] == "completed"
    assert job["files"]["markdown"]["file_id"].startswith("file_")


async def test_cancel_is_durable_and_job_still_queryable(gateway, tmp_path):
    url, state = gateway
    state.delay_s = 30.0
    tools = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), wait_seconds=0
    )
    canceled = await tools.cancel_job(payload["job_id"])
    assert canceled["status"] == "canceled"

    job = await tools.get_job(payload["job_id"])
    assert job["status"] == "canceled"


async def test_pagination_reassembles_multibyte_text(gateway, tmp_path):
    url, state = gateway
    state.markdown_text = "解析🎉文本" * 2000  # 12000 codepoints, > page size
    tools = make_tools(
        url, MCP_ALLOWED_ROOT=str(tmp_path), MCP_MAX_RESULT_CHARS="7"
    )

    payload = await tools.parse_document(
        path=write_pdf(tmp_path), wait_seconds=10
    )

    pieces: list[str] = []
    offset = 0
    while True:
        page = await tools.get_result(payload["job_id"], offset=offset)
        pieces.append(page["text"])
        if page["eof"]:
            break
        offset = page["next_offset"]

    assert "".join(pieces) == state.markdown_text


async def test_unavailable_tier_lists_offered_tiers(gateway, tmp_path):
    url, state = gateway
    state.tiers = ["flash"]
    tools = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="flash"):
        await tools.parse_document(path=write_pdf(tmp_path), tier="basic")


async def test_dead_gateway_is_retryable_error(gateway, tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    tools = make_tools(f"http://127.0.0.1:{port}", MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="retry"):
        await tools.parse_document(path=write_pdf(tmp_path), wait_seconds=0)


async def test_new_tools_instance_sees_old_jobs(gateway, tmp_path):
    url, state = gateway
    state.delay_s = 0.1
    first = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await first.parse_document(path=write_pdf(tmp_path), wait_seconds=0)

    second = make_tools(url, MCP_ALLOWED_ROOT=str(tmp_path))
    job = await wait_terminal(second, payload["job_id"])
    assert job["status"] == "completed"


async def test_sdk_client_lists_tools_and_calls_list_tiers(gateway):
    url, _state = gateway
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from thinkparse_mcp.server import build_app

    settings = make_settings(url)
    metrics = Metrics()
    client = ThinkParseClient(settings, retry_backoff_s=0.05)
    tools = Tools(client, settings, metrics)
    app = build_app(settings, tools=tools, client=client, metrics=metrics)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with streamable_http_client(
            f"http://127.0.0.1:{port}/mcp"
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert {
                    "list_tiers",
                    "get_job",
                    "cancel_job",
                    "parse_document",
                    "get_result",
                } <= names
                result = await session.call_tool("list_tiers")
                assert not result.is_error
                text = result.content[0].text if result.content else ""
                assert "basic" in text
    finally:
        server.should_exit = True
        await task
