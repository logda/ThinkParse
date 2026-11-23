"""Tier cache, job projection, error mapping, and the first three tools."""

import pytest

from thinkparse_mcp.client import UpstreamError
from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import (
    ToolError,
    TierCache,
    Tools,
    _job_view,
    _map_upstream_error,
    register_tools,
)


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class FakeClient:
    def __init__(self, tiers=None, job=None, cancel=None, error=None):
        self.tiers = tiers or {"object": "list", "discovered": True, "data": [{"id": "basic"}]}
        self.job = job
        self.cancel = cancel or {"job_id": "job_1", "status": "canceled"}
        self.error = error
        self.calls = []

    async def get_tiers(self):
        self.calls.append("get_tiers")
        if self.error:
            raise self.error
        return self.tiers

    async def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        if self.error:
            raise self.error
        return self.job

    async def cancel_job(self, job_id):
        self.calls.append(("cancel_job", job_id))
        if self.error:
            raise self.error
        return self.cancel


def make_tools(client):
    return Tools(client, make_settings(), Metrics())


def _full_job():
    return {
        "job_id": "job_1",
        "status": "completed",
        "tier": "basic",
        "attempt": 1,
        "pages": 12,
        "timing": {"queue_ms": 5, "parse_ms": 500, "project_ms": 7},
        "files": [
            {
                "name": "doc.pdf",
                "status": "completed",
                "output_files": {
                    "markdown": {"file_id": "file_md", "bytes": 3},
                    "middle_json": {"file_id": "file_mid", "bytes": 4},
                    "images": [{"file_id": "file_img", "filename": "a.jpg", "bytes": 5}],
                },
                "error": None,
            }
        ],
        "links": {"self": "/api/v2/jobs/job_1"},
    }


def test_job_view_flattens_files0():
    view = _job_view(_full_job())
    assert view["job_id"] == "job_1"
    assert view["status"] == "completed"
    assert view["timing"] == {"queue_ms": 5, "parse_ms": 500, "project_ms": 7}
    assert view["files"]["markdown"] == {"file_id": "file_md", "bytes": 3}
    assert view["error"] is None
    assert view["links"] == {"self": "/api/v2/jobs/job_1"}


def test_job_view_handles_missing_outputs():
    view = _job_view({"job_id": "job_2", "status": "queued", "files": [{"output_files": None, "error": None}]})
    assert view["files"] is None
    assert view["error"] is None
    assert view["timing"] is None


def test_map_upstream_error_messages():
    four_hundred = _map_upstream_error(UpstreamError(400, "unknown tier", False))
    assert "list_tiers" in str(four_hundred)
    four_oh_one = _map_upstream_error(UpstreamError(401, "invalid api key", False))
    assert "misconfigured" in str(four_oh_one)
    assert "Bearer" not in str(four_oh_one)
    four_oh_four = _map_upstream_error(UpstreamError(404, "job not found", False))
    assert "not found" in str(four_oh_four)
    four_thirteen = _map_upstream_error(UpstreamError(413, "File too large", False))
    assert "size limit" in str(four_thirteen)
    five_oh_seven = _map_upstream_error(UpstreamError(507, "watermark", False))
    assert "storage" in str(five_oh_seven)
    retryable = _map_upstream_error(UpstreamError(0, "connection refused", True))
    assert "retry" in str(retryable).lower()


async def test_list_tiers_shape():
    tools = make_tools(FakeClient())
    payload = await tools.list_tiers()
    assert payload == {
        "discovered": True,
        "tiers": [{"id": "basic"}],
        "recommended_default": "basic",
    }


async def test_list_tiers_recommend_first_available_when_no_basic():
    client = FakeClient(tiers={"object": "list", "discovered": False, "data": [{"id": "flash"}]})
    payload = await make_tools(client).list_tiers()
    assert payload["recommended_default"] == "flash"


async def test_get_job_returns_view():
    tools = make_tools(FakeClient(job=_full_job()))
    view = await tools.get_job("job_1")
    assert view["job_id"] == "job_1"
    assert view["files"]["markdown"]["file_id"] == "file_md"


async def test_get_job_404_maps_to_tool_error():
    tools = make_tools(FakeClient(error=UpstreamError(404, "job not found", False)))
    with pytest.raises(ToolError, match="not found"):
        await tools.get_job("missing")


async def test_cancel_job_returns_payload():
    tools = make_tools(FakeClient())
    payload = await tools.cancel_job("job_1")
    assert payload == {"job_id": "job_1", "status": "canceled"}


async def test_tier_cache_hits_upstream_once_within_ttl():
    client = FakeClient()
    cache = TierCache(client, ttl_s=60.0)
    await cache.get()
    await cache.get()
    assert client.calls.count("get_tiers") == 1


async def test_tier_cache_refetches_after_ttl():
    client = FakeClient()
    cache = TierCache(client, ttl_s=0)
    await cache.get()
    await cache.get()
    assert client.calls.count("get_tiers") == 2


async def test_tier_cache_validate_lists_available_tiers():
    cache = TierCache(FakeClient(), ttl_s=60.0)
    with pytest.raises(ToolError) as excinfo:
        await cache.validate("premium")
    assert "basic" in str(excinfo.value)
    assert await cache.validate("basic") == ["basic"]


async def test_register_tools_exposes_the_three_tools():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("test")
    register_tools(server, make_tools(FakeClient(job=_full_job())))
    listed = await server.list_tools()
    names = {tool.name for tool in listed}
    assert {"list_tiers", "get_job", "cancel_job"} <= names
