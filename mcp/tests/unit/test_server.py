"""FastMCP-style assembly: healthz/readyz/metrics custom routes."""

import httpx

from thinkparse_mcp.config import Settings
from thinkparse_mcp.client import ThinkParseClient
from thinkparse_mcp.server import build_app


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


def make_app(handler=None, client=None):
    if client is None and handler is not None:
        client = ThinkParseClient(
            make_settings(), transport=httpx.MockTransport(handler), retry_backoff_s=0
        )
    return build_app(make_settings(), tools=None, client=client)


async def test_healthz_is_alive():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app())) as http:
        response = await http.get("http://testserver/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readyz_503_without_client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app())) as http:
        response = await http.get("http://testserver/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def _healthy_gateway(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v2/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/api/v2/tiers":
        return httpx.Response(
            200, json={"object": "list", "discovered": True, "data": [{"id": "basic"}]}
        )
    return httpx.Response(404)


async def test_readyz_200_with_healthy_client():
    app = make_app(handler=_healthy_gateway)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.get("http://testserver/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_readyz_503_when_upstream_down():
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unavailable"})

    app = make_app(handler=broken)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.get("http://testserver/readyz")
    assert response.status_code == 503


async def test_metrics_endpoint_exposes_prometheus_text():
    app = make_app(handler=_healthy_gateway)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.get("http://testserver/metrics")
    assert response.status_code == 200
    assert "mcp_tool_calls_total" in response.text
