"""PUT /uploads: client file ingress, auth, validation, gateway forwarding."""

import hashlib
import json
import tempfile

import httpx

from thinkparse_mcp.client import ThinkParseClient
from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.server import build_app
from thinkparse_mcp.tools import Tools

PDF_BYTES = b"%PDF-1.7 fake document body"
TOKEN = "s3cret"


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
        "MCP_STATIC_TOKEN": TOKEN,
    }
    env.update(overrides)
    return Settings.from_env(env)


class GatewayRecorder:
    """Mock gateway that records the three upload steps."""

    def __init__(self, create_status=200, put_status=200):
        self.create_status = create_status
        self.put_status = put_status
        self.created = None
        self.put_body = None
        self.completed_sha = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/uploads" and request.method == "POST":
            self.created = json.loads(request.content)
            if self.create_status != 200:
                return httpx.Response(
                    self.create_status, json={"detail": "gateway says no"}
                )
            return httpx.Response(
                200, json={"id": "upload_1", "object": "upload", "status": "pending"}
            )
        if path == "/api/v2/uploads/upload_1/content" and request.method == "PUT":
            self.put_body = request.content
            if self.put_status != 200:
                return httpx.Response(self.put_status, json={"detail": "boom"})
            return httpx.Response(200)
        if path == "/api/v2/uploads/upload_1/complete" and request.method == "POST":
            self.completed_sha = json.loads(request.content).get("sha256sum")
            return httpx.Response(
                200,
                json={"id": "upload_1", "status": "completed", "file": {"id": "file_9"}},
            )
        if path == "/api/v2/tiers":
            return httpx.Response(
                200, json={"object": "list", "discovered": True, "data": [{"id": "basic"}]}
            )
        return httpx.Response(404, json={"detail": f"unexpected {path}"})


def make_app(gateway=None, **setting_overrides):
    settings = make_settings(**setting_overrides)
    gateway = gateway or GatewayRecorder()
    client = ThinkParseClient(
        settings, transport=httpx.MockTransport(gateway), retry_backoff_s=0
    )
    tools = Tools(client, settings, Metrics())
    return build_app(settings, tools=tools, client=client), gateway


def auth(headers=None):
    merged = {"Authorization": f"Bearer {TOKEN}"}
    merged.update(headers or {})
    return merged


async def put(http, body=PDF_BYTES, filename="doc.pdf", headers=None):
    return await http.put(
        f"http://testserver/uploads?filename={filename}",
        content=body,
        headers=auth(headers),
    )


async def test_upload_success_returns_file_id_and_forwards_three_steps():
    app, gateway = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http)
    assert response.status_code == 200
    body = response.json()
    sha = hashlib.sha256(PDF_BYTES).hexdigest()
    assert body["file_id"] == "file_9"
    assert body["bytes"] == len(PDF_BYTES)
    assert body["sha256sum"] == sha
    assert body["filename"] == "doc.pdf"
    assert "parse_document" in body["hint"]
    assert gateway.created["filename"] == "doc.pdf"
    assert gateway.created["bytes"] == len(PDF_BYTES)
    assert gateway.created["mime_type"] == "application/pdf"
    assert gateway.created["sha256sum"] == sha
    assert gateway.put_body == PDF_BYTES
    assert gateway.completed_sha == sha


async def test_upload_requires_bearer_token():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.put(
            "http://testserver/uploads?filename=doc.pdf", content=PDF_BYTES
        )
    assert response.status_code == 401


async def test_upload_rejects_wrong_token():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.put(
            "http://testserver/uploads?filename=doc.pdf",
            content=PDF_BYTES,
            headers={"Authorization": "Bearer nope"},
        )
    assert response.status_code == 401


async def test_upload_declared_oversize_is_413_before_reading():
    app, gateway = make_app(MCP_MAX_UPLOAD_BYTES="10")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http)
    assert response.status_code == 413
    assert "size limit" in response.json()["error"]
    assert gateway.created is None


async def test_upload_rejects_disallowed_extension():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http, filename="malware.exe")
    assert response.status_code == 400
    assert "not allowed" in response.json()["error"]


async def test_upload_requires_filename():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.put(
            "http://testserver/uploads", content=PDF_BYTES, headers=auth()
        )
    assert response.status_code == 400
    assert "filename" in response.json()["error"]


async def test_upload_rejects_path_like_filename():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http, filename="../../etc/passwd.pdf")
    assert response.status_code == 400
    assert "basename" in response.json()["error"]


async def test_upload_rejects_magic_mismatch():
    app, gateway = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http, body=b"\x89PNG\r\n\x1a\n definitely not a pdf")
    assert response.status_code == 400
    assert "content does not match" in response.json()["error"]
    assert gateway.created is None


async def test_upload_requires_content_length():
    app, _ = make_app()

    async def chunked():
        yield PDF_BYTES

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.put(
            "http://testserver/uploads?filename=doc.pdf",
            content=chunked(),
            headers=auth(),
        )
    assert response.status_code == 400
    assert "Content-Length" in response.json()["error"]


async def test_upload_empty_body_rejected():
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http, body=b"")
    assert response.status_code == 400
    assert "empty" in response.json()["error"]


async def test_upload_gateway_5xx_maps_to_502():
    app, _ = make_app(gateway=GatewayRecorder(create_status=503))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http)
    assert response.status_code == 502


async def test_upload_gateway_413_passes_through():
    app, _ = make_app(gateway=GatewayRecorder(create_status=413))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http)
    assert response.status_code == 413


async def test_upload_cleans_up_temp_files(monkeypatch, tmp_path):
    import thinkparse_mcp.upload as upload_module

    real_mkstemp = tempfile.mkstemp

    def staged_mkstemp(*args, **kwargs):
        kwargs["dir"] = str(tmp_path)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(upload_module.tempfile, "mkstemp", staged_mkstemp)
    app, _ = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        ok = await put(http)
        bad = await put(http, body=b"\x89PNG\r\n\x1a\n not a pdf")
    assert ok.status_code == 200
    assert bad.status_code == 400
    assert list(tmp_path.iterdir()) == []


async def test_uploads_route_absent_without_tools():
    settings = make_settings()
    app = build_app(settings, tools=None, client=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await put(http)
    assert response.status_code == 404