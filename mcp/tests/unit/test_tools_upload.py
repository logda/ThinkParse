"""The three-step upload pipeline: local path or URL -> file_id."""

import hashlib

import httpx
import pytest

from thinkparse_mcp.client import UpstreamError
from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools, _download_to_temp


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class FakeUploadClient:
    """Records the upload calls and enforces the gateway's byte-match rule."""

    def __init__(self, create_body=None, put_error=None):
        self.create_body = create_body
        self.put_error = put_error
        self.calls = []
        self.put_content = b""

    async def create_upload(self, filename, size, mime_type, sha256sum=None):
        self.calls.append(
            ("create", filename, size, mime_type, sha256sum)
        )
        return self.create_body or {
            "id": "upload_1",
            "object": "upload",
            "status": "pending",
            "upload_url": "/api/v2/uploads/upload_1/content",
        }

    async def put_upload_content(self, upload_id, content_factory, size):
        chunks = []
        async for chunk in content_factory():
            chunks.append(chunk)
        self.put_content = b"".join(chunks)
        self.calls.append(("put", upload_id, size))
        if self.put_error is not None:
            raise self.put_error
        if len(self.put_content) != size:
            raise UpstreamError(400, "upload byte count does not match", False)

    async def complete_upload(self, upload_id, sha256sum):
        self.calls.append(("complete", upload_id, sha256sum))
        return {
            "id": upload_id,
            "object": "upload",
            "status": "completed",
            "file": {"id": "file_1"},
        }


def make_tools(client, **setting_overrides):
    return Tools(client, make_settings(**setting_overrides), Metrics())


PDF_BYTES = b"%PDF-1.7 fake document body"


async def test_upload_local_pdf_calls_three_steps_in_order(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BYTES)
    client = FakeUploadClient()
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    file_id, sha, size = await tools._source_to_file(str(pdf), None)

    assert file_id == "file_1"
    assert size == len(PDF_BYTES)
    assert sha == hashlib.sha256(PDF_BYTES).hexdigest()
    assert [call[0] for call in client.calls] == ["create", "put", "complete"]
    create = client.calls[0]
    assert create[1] == "doc.pdf"
    assert create[2] == len(PDF_BYTES)
    assert create[3] == "application/pdf"
    assert create[4] == sha
    assert client.put_content == PDF_BYTES
    assert client.calls[2] == ("complete", "upload_1", sha)


async def test_dedupe_short_circuit_skips_put(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BYTES)
    sha = hashlib.sha256(PDF_BYTES).hexdigest()
    client = FakeUploadClient(
        create_body={
            "id": "upload_d",
            "object": "upload",
            "status": "completed",
            "file": {"id": "file_x"},
        }
    )
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    file_id, returned_sha, size = await tools._source_to_file(str(pdf), None)

    assert file_id == "file_x"
    assert returned_sha == sha
    assert [call[0] for call in client.calls] == ["create"]


async def test_upload_declared_bytes_must_match(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BYTES)
    client = FakeUploadClient(
        put_error=UpstreamError(400, "upload byte count does not match", False)
    )
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="rejected by ThinkParse"):
        await tools._source_to_file(str(pdf), None)


async def test_path_and_url_mutually_exclusive(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BYTES)
    tools = make_tools(FakeUploadClient(), MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="exactly one"):
        await tools._source_to_file(str(pdf), "http://example.com/doc.pdf")
    with pytest.raises(ToolError, match="exactly one"):
        await tools._source_to_file(None, None)


# --- URL download path ------------------------------------------------------


def _public_resolver(host):
    async def resolve(name):
        return ["93.184.216.34"]

    return resolve


def _serve(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


class _Infinite:
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        async def stream():
            while True:
                yield b"x" * 1024

        return httpx.Response(200, content=stream())


async def test_download_aborts_at_limit_and_cleans_up(tmp_path):
    transport = httpx.MockTransport(_Infinite())
    settings = make_settings(MCP_MAX_UPLOAD_BYTES="2048")

    with pytest.raises(ToolError) as excinfo:
        await _download_to_temp(
            "http://example.com/doc.pdf",
            settings,
            transport=transport,
            resolver=_public_resolver("example.com"),
            temp_dir=tmp_path,
        )
    assert "exceeds the ThinkParse size limit" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


async def test_download_rejects_fake_extension(tmp_path):
    transport = _serve(b"\x89PNG\r\n\x1a\n not a pdf")
    settings = make_settings()

    with pytest.raises(ToolError) as excinfo:
        await _download_to_temp(
            "http://example.com/doc.pdf",
            settings,
            transport=transport,
            resolver=_public_resolver("example.com"),
            temp_dir=tmp_path,
        )
    assert "content does not match" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


async def test_download_ok_returns_temp_path_sha_size(tmp_path):
    transport = _serve(PDF_BYTES)
    settings = make_settings()

    path, sha, size = await _download_to_temp(
        "http://example.com/doc.pdf",
        settings,
        transport=transport,
        resolver=_public_resolver("example.com"),
        temp_dir=tmp_path,
    )

    assert path.read_bytes() == PDF_BYTES
    assert sha == hashlib.sha256(PDF_BYTES).hexdigest()
    assert size == len(PDF_BYTES)
    path.unlink()
