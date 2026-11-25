"""Assemble the MCPServer with custom healthz/readyz/metrics routes.

The mcp 2.x SDK renamed FastMCP to MCPServer and exposes
`custom_route()` so our HTTP endpoints ride on the same Starlette app the
SDK builds — including its session-manager lifespan. Tools are wired in by
`tools.register_tools` (Task 6); the import is deferred so this module never
imports what does not exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .client import ThinkParseClient
from .config import Settings
from .observability import Metrics
from .security import SecurityError, caller_identity

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
            "blocking the conversation. To parse a file that lives on the "
            "client, upload it first: PUT the raw bytes to /uploads?filename="
            "<name> on this server (same Bearer token, Content-Length "
            "required), then call parse_document(file_id=...). The url "
            "parameter fetches a public URL server-side instead."
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
        from .upload import handle_upload

        tools_module.register_tools(server, tools)

        @server.custom_route("/uploads", methods=["PUT"])
        async def uploads(request: Request) -> JSONResponse:
            return await handle_upload(request, tools, settings)

    return server


def identity_middleware(app, settings: Settings):
    """Pure ASGI wrapper: authenticate /mcp and /uploads, leave the rest alone.

    The resolved identity lands in ``scope["state"]["user"]`` for logging;
    stdio has no middleware path, so tools log ``local`` there instead.
    """

    async def middleware(scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") not in ("/mcp", "/uploads"):
            await app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        peer_ip = (scope.get("client") or ("", 0))[0]
        try:
            user = caller_identity(
                headers,
                peer_ip,
                settings.trusted_proxy_cidrs,
                settings.auth_header,
                settings.static_token,
            )
        except SecurityError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=401)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        await app(scope, receive, send)

    return middleware


def build_app(
    settings: Settings,
    tools: "Tools | None" = None,
    client: ThinkParseClient | None = None,
    metrics: Metrics | None = None,
) -> Any:
    """Return the streamable-HTTP ASGI app (identity-wrapped) for this server."""
    server = create_mcp(settings, tools=tools, client=client, metrics=metrics)
    app = server.streamable_http_app(host=settings.host)
    return identity_middleware(app, settings)


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

    app = identity_middleware(server.streamable_http_app(host=settings.host), settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=10,
    )
