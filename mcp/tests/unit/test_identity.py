"""Trusted-proxy identity and static-token auth for the HTTP transport."""

import pytest
from starlette.datastructures import Headers

from thinkparse_mcp.config import Settings
from thinkparse_mcp.security import SecurityError, caller_identity
from thinkparse_mcp.server import identity_middleware


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


CIDR_SETTINGS = make_settings()


def _headers(**pairs):
    return Headers(
        raw=[(key.lower().encode(), value.encode()) for key, value in pairs.items()]
    )


def test_trusted_peer_reads_header():
    identity = caller_identity(
        _headers(**{"X-Auth-User": "alice"}),
        "10.1.2.3",
        CIDR_SETTINGS.trusted_proxy_cidrs,
        CIDR_SETTINGS.auth_header,
        None,
    )
    assert identity == "alice"


def test_trusted_peer_missing_header_is_anonymous():
    identity = caller_identity(
        _headers(),
        "10.1.2.3",
        CIDR_SETTINGS.trusted_proxy_cidrs,
        CIDR_SETTINGS.auth_header,
        "sekret",
    )
    assert identity == "anonymous"


def test_untrusted_peer_without_token_rejected():
    with pytest.raises(SecurityError, match="credentials"):
        caller_identity(
            _headers(**{"X-Auth-User": "forged"}),
            "203.0.113.9",
            CIDR_SETTINGS.trusted_proxy_cidrs,
            CIDR_SETTINGS.auth_header,
            None,
        )


def test_untrusted_peer_wrong_bearer_rejected():
    with pytest.raises(SecurityError):
        caller_identity(
            _headers(**{"Authorization": "Bearer nope"}),
            "203.0.113.9",
            CIDR_SETTINGS.trusted_proxy_cidrs,
            CIDR_SETTINGS.auth_header,
            "sekret",
        )


def test_untrusted_peer_matching_bearer_is_static():
    identity = caller_identity(
        _headers(authorization="Bearer sekret"),
        "203.0.113.9",
        CIDR_SETTINGS.trusted_proxy_cidrs,
        CIDR_SETTINGS.auth_header,
        "sekret",
    )
    assert identity == "static"


def test_untrusted_peer_without_authorization_rejected():
    with pytest.raises(SecurityError):
        caller_identity(
            _headers(),
            "203.0.113.9",
            CIDR_SETTINGS.trusted_proxy_cidrs,
            CIDR_SETTINGS.auth_header,
            "sekret",
        )


# --- pure ASGI middleware ---------------------------------------------------


async def call_middleware(settings, path, headers, peer_ip):
    captured = {"called": False, "user": None}
    sent = []

    async def inner(scope, receive, send):
        captured["called"] = True
        captured["user"] = (scope.get("state") or {}).get("user")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    app = identity_middleware(inner, settings)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": path,
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ],
        "client": (peer_ip, 51000),
        "state": {},
    }
    await app(scope, receive, send)
    return captured, sent


async def test_middleware_passes_trusted_peer_and_injects_user():
    settings = CIDR_SETTINGS
    captured, sent = await call_middleware(
        settings, "/mcp", {"X-Auth-User": "alice"}, "10.9.9.9"
    )
    assert captured["called"] is True
    assert captured["user"] == "alice"
    assert sent[0]["status"] == 200


async def test_middleware_rejects_untrusted_forged_header():
    captured, sent = await call_middleware(
        CIDR_SETTINGS, "/mcp", {"X-Auth-User": "forged"}, "203.0.113.9"
    )
    assert captured["called"] is False
    assert sent[0]["status"] == 401


async def test_middleware_allows_static_bearer():
    settings = make_settings(MCP_STATIC_TOKEN="sekret")
    captured, _sent = await call_middleware(
        settings, "/mcp", {"Authorization": "Bearer sekret"}, "203.0.113.9"
    )
    assert captured["called"] is True
    assert captured["user"] == "static"


async def test_middleware_skips_healthz():
    captured, _sent = await call_middleware(
        CIDR_SETTINGS, "/healthz", {}, "203.0.113.9"
    )
    assert captured["called"] is True
