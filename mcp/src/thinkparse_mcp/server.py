"""Assemble the MCPServer with custom healthz/readyz/metrics routes.

The mcp 2.x SDK renamed FastMCP to MCPServer and exposes
`custom_route()` so our HTTP endpoints ride on the same Starlette app the
SDK builds — including its session-manager lifespan. Tools are wired in by
`tools.register_tools` (Task 6); the import is deferred so this module never
imports what does not exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .client import ThinkParseClient
from .config import Settings
from .observability import Metrics

if TYPE_CHECKING:
    from .tools import Tools


def create_mcp(
    settings: Settings,
    tools: "Tools | None" = None,
    client: ThinkParseClient | None = None,
    metrics: Metrics | None = None,
) -> MCPServer:
    """Build the MCPServer with health and metrics routes attached."""
    metrics = metrics or Metrics()
    server = MCPServer(
        name="ThinkParse",
        instructions=(
            "Parse PDF and image documents through a private ThinkParse "
            "gateway. Submit, poll, paginate results, and cancel without "
            "blocking the conversation."
        ),
        log_level=settings.log_level,
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        if client is None:
            return JSONResponse(
                {"status": "not_ready", "reason": "upstream client not configured"},
                status_code=503,
            )
        try:
            await client.health()
            await client.get_tiers()
        except Exception as exc:  # noqa: BLE001 - readiness reports any failure
            return JSONResponse(
                {"status": "not_ready", "reason": str(exc)}, status_code=503
            )
        return JSONResponse({"status": "ready"})

    @server.custom_route("/metrics", methods=["GET"])
    async def metrics_endpoint(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            metrics.render().decode("utf-8"),
            media_type=Metrics.content_type(),
        )

    if tools is not None:
        from . import tools as tools_module

        tools_module.register_tools(server, tools)
    return server


def build_app(
    settings: Settings,
    tools: "Tools | None" = None,
    client: ThinkParseClient | None = None,
    metrics: Metrics | None = None,
) -> Starlette:
    """Return the streamable-HTTP Starlette app for this server."""
    server = create_mcp(settings, tools=tools, client=client, metrics=metrics)
    return server.streamable_http_app(host=settings.host)


def run(settings: Settings) -> None:
    """Run the server on the configured transport (entrypoint target)."""
    from .client import ThinkParseClient
    from .observability import configure_logging

    configure_logging(settings.log_level)
    metrics = Metrics()
    client = ThinkParseClient(settings)
    from . import tools as tools_module

    tools = tools_module.Tools(client, settings, metrics)
    server = create_mcp(settings, tools=tools, client=client, metrics=metrics)
    if settings.transport == "stdio":
        server.run("stdio")
        return
    import uvicorn

    app = server.streamable_http_app(host=settings.host)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=10,
    )
