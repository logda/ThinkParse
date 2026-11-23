"""ThinkParseClient: the single HTTP egress to /api/v2."""

import json

import httpx
import pytest

from thinkparse_mcp.client import ThinkParseClient, UpstreamError
from thinkparse_mcp.config import Settings


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


def make_client(handler, **settings_kw):
    settings = make_settings(**settings_kw)
    return ThinkParseClient(
        settings,
        transport=httpx.MockTransport(handler),
        retry_backoff_s=0,
    )


async def test_create_upload_posts_mime_type_and_bytes():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={"id": "upload_1", "status": "completed", "file": {"id": "file_x"}},
        )

    async with make_client(handler) as client:
        body = await client.create_upload("doc.pdf", 10, "application/pdf", "abc123")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v2/uploads"
    assert captured["auth"] == "Bearer k"
    assert captured["json"] == {
        "filename": "doc.pdf",
        "bytes": 10,
        "mime_type": "application/pdf",
        "sha256sum": "abc123",
    }
    assert body["file"]["id"] == "file_x"


async def test_create_upload_omits_sha_when_not_provided():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "upload_1", "status": "pending", "upload_url": "/api/v2/uploads/upload_1/content"})

    async with make_client(handler) as client:
        await client.create_upload("doc.pdf", 10, "application/pdf")

    assert "sha256sum" not in captured["json"]


async def test_complete_upload_uses_sha256sum_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "upload_1", "status": "completed", "file": {"id": "file_x"}})

    async with make_client(handler) as client:
        await client.complete_upload("upload_1", "deadbeef")

    assert captured["path"] == "/api/v2/uploads/upload_1/complete"
    assert captured["json"] == {"sha256sum": "deadbeef"}


async def test_put_upload_streams_with_content_length():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["length"] = request.headers.get("content-length")
        captured["body"] = request.read()
        return httpx.Response(200)

    async def chunks():
        yield b"hel"
        yield b"lo"

    async with make_client(handler) as client:
        await client.put_upload_content("upload_1", chunks(), 5)

    assert captured["path"] == "/api/v2/uploads/upload_1/content"
    assert captured["length"] == "5"
    assert captured["body"] == b"hello"


async def test_5xx_retries_once_then_raises_retryable():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"detail": "unavailable"})

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await client.get_job("job_1")

    assert len(calls) == 2
    assert excinfo.value.status == 503
    assert excinfo.value.retryable is True


async def test_5xx_retry_succeeds_on_second_attempt():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"job_id": "job_1", "status": "queued"})

    async with make_client(handler) as client:
        job = await client.get_job("job_1")

    assert job["job_id"] == "job_1"
    assert len(calls) == 2


async def test_connect_error_retried_once_then_retryable():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await client.get_job("job_1")

    assert len(calls) == 2
    assert excinfo.value.retryable is True


async def test_401_maps_to_non_retryable_and_hides_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await client.get_job("job_1")

    assert excinfo.value.status == 401
    assert excinfo.value.retryable is False
    assert "Bearer k" not in str(excinfo.value)


async def test_4xx_does_not_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"detail": "job not found"})

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await client.get_job("missing")

    assert len(calls) == 1
    assert excinfo.value.status == 404


async def test_create_job_posts_202_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.read())
        return httpx.Response(202, json={"job_id": "job_1", "status": "queued"})

    async with make_client(handler) as client:
        job = await client.create_job("file_x", "basic", "auto")

    assert captured["path"] == "/api/v2/jobs"
    assert captured["json"] == {"file_id": "file_x", "tier": "basic", "ocr_mode": "auto", "priority": 0}
    assert job["job_id"] == "job_1"


async def test_create_job_includes_batch_and_priority_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.read())
        return httpx.Response(202, json={})

    async with make_client(handler) as client:
        await client.create_job("file_x", "basic", "auto", batch_id="batch_a", priority=3)

    assert captured["json"]["batch_id"] == "batch_a"
    assert captured["json"]["priority"] == 3


async def test_cancel_job_returns_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"job_id": "job_1", "status": "canceled"})

    async with make_client(handler) as client:
        body = await client.cancel_job("job_1")

    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/v2/jobs/job_1"
    assert body["status"] == "canceled"


async def test_get_tiers_validates_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "discovered": True, "data": [{"id": "basic"}]})

    async with make_client(handler) as client:
        tiers = await client.get_tiers()

    assert tiers == {"object": "list", "discovered": True, "data": [{"id": "basic"}]}


async def test_get_tiers_rejects_malformed_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "discovered": True, "data": [{"tier": "basic"}]})

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError):
            await client.get_tiers()


async def test_health_maps_non_2xx_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unavailable"})

    async with make_client(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await client.health()

    assert excinfo.value.status == 503


async def test_get_file_text_decodes_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/files/file_md/content"
        return httpx.Response(200, text="# hello")

    async with make_client(handler) as client:
        text = await client.get_file_text("file_md")

    assert text == "# hello"
