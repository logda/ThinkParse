"""submit_batch/get_batch and the thinkparse:// resources."""

import pytest

from thinkparse_mcp.config import Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools, register_tools

PDF_A = b"%PDF-1.4 batch document a"
PDF_B = b"%PDF-1.4 batch document b"


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class BatchClient:
    def __init__(self):
        self.calls = []
        self.jobs_created = []
        self.batch = {
            "batch_id": "b1",
            "total": 2,
            "queued": 0,
            "running": 0,
            "completed": 1,
            "failed": 1,
            "canceled": 0,
            "oldest_queued_seconds": None,
            "pages_completed": 5,
        }
        self.batch_jobs = {
            "object": "list",
            "data": [
                {
                    "job_id": "job_a",
                    "status": "completed",
                    "files": [{"output_files": {"markdown": {"file_id": "file_md"}}, "error": None}],
                },
                {
                    "job_id": "job_b",
                    "status": "failed",
                    "files": [{"output_files": None, "error": {"message": "boom"}}],
                },
            ],
        }
        self.file_meta = {
            "id": "file_img",
            "object": "file",
            "filename": "page-0.jpg",
            "bytes": 10,
            "mime_type": "image/jpeg",
            "purpose": "output",
            "sha256sum": "0",
        }
        self.file_bytes = b"\xff\xd8\xff image bytes"
        self.file_text = "M" * 100

    async def get_tiers(self):
        self.calls.append("get_tiers")
        return {"object": "list", "discovered": True, "data": [{"id": "basic"}]}

    async def create_upload(self, filename, size, mime_type, sha256sum=None):
        self.calls.append(("create_upload", filename))
        return {"id": f"upload_{filename}", "status": "pending"}

    async def put_upload_content(self, upload_id, content_factory, size):
        async for _ in content_factory():
            pass
        self.calls.append(("put", upload_id, size))

    async def complete_upload(self, upload_id, sha256sum):
        self.calls.append(("complete", upload_id))
        return {"id": upload_id, "status": "completed", "file": {"id": f"file_{upload_id}"}}

    async def create_job(self, file_id, tier, ocr_mode, batch_id=None, priority=0):
        self.calls.append(("create_job", file_id, tier, ocr_mode, batch_id))
        job = {"job_id": f"job_{len(self.jobs_created)}", "status": "queued", "tier": tier}
        self.jobs_created.append(dict(job, batch_id=batch_id))
        return job

    async def get_batch(self, batch_id):
        self.calls.append(("get_batch", batch_id))
        return self.batch

    async def get_batch_jobs(self, batch_id, status=None, limit=50):
        self.calls.append(("get_batch_jobs", batch_id))
        return self.batch_jobs

    async def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return {
            "job_id": job_id,
            "status": "completed",
            "tier": "basic",
            "files": [
                {
                    "output_files": {
                        "markdown": {"file_id": "file_md", "bytes": 3},
                        "images": [{"file_id": "file_img", "filename": "page-0.jpg", "bytes": 10}],
                    },
                    "error": None,
                }
            ],
        }

    async def get_file_text(self, file_id):
        self.calls.append(("get_file_text", file_id))
        return self.file_text

    async def get_file_meta(self, file_id):
        self.calls.append(("get_file_meta", file_id))
        return self.file_meta

    async def get_file_bytes(self, file_id):
        self.calls.append(("get_file_bytes", file_id))
        return self.file_bytes


def make_tools(client, **setting_overrides):
    return Tools(client, make_settings(**setting_overrides), Metrics())


def write_pdfs(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(PDF_A)
    b = tmp_path / "b.pdf"
    b.write_bytes(PDF_B)
    return [str(a), str(b)]


async def test_submit_batch_shares_batch_id_across_jobs(tmp_path):
    client = BatchClient()
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    payload = await tools.submit_batch(
        write_pdfs(tmp_path), "b-2000_1", tier="basic", ocr_mode="auto"
    )

    assert payload["batch_id"] == "b-2000_1"
    assert [job["job_id"] for job in payload["jobs"]] == ["job_0", "job_1"]
    assert all(job["status"] == "queued" for job in payload["jobs"])
    created = [call for call in client.calls if call[0] == "create_job"]
    assert len(created) == 2
    assert all(call[4] == "b-2000_1" for call in created)


async def test_invalid_batch_id_rejected_before_any_upload(tmp_path):
    client = BatchClient()
    tools = make_tools(client, MCP_ALLOWED_ROOT=str(tmp_path))

    with pytest.raises(ToolError, match="batch_id"):
        await tools.submit_batch(write_pdfs(tmp_path), "bad id!")
    assert not any(call[0] == "create_upload" for call in client.calls)


async def test_get_batch_summarizes_and_lists_failed_jobs():
    client = BatchClient()
    tools = make_tools(client)

    payload = await tools.get_batch("b1")

    assert payload["total"] == 2
    assert payload["failed"] == 1
    assert payload["pages_completed"] == 5
    assert [job["job_id"] for job in payload["jobs"]] == ["job_a", "job_b"]
    failed = payload["jobs"][1]
    assert failed["status"] == "failed"
    assert failed["error"]["message"] == "boom"


async def test_register_tools_exposes_batch_tools():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("test")
    register_tools(server, make_tools(BatchClient()))
    names = {tool.name for tool in await server.list_tools()}
    assert {"submit_batch", "get_batch"} <= names


async def test_resources_registered_as_templates():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("test")
    register_tools(server, make_tools(BatchClient()))
    templates = await server.list_resource_templates()
    uris = {template.uri_template for template in templates}
    assert "thinkparse://jobs/{job_id}/markdown" in uris
    assert "thinkparse://jobs/{job_id}/images/{name}" in uris


async def test_markdown_resource_truncates_with_note():
    from mcp.server.mcpserver import MCPServer

    client = BatchClient()
    server = MCPServer("test")
    register_tools(server, make_tools(client, MCP_MAX_RESULT_CHARS="10"))

    contents = await server.read_resource("thinkparse://jobs/job_a/markdown")
    data = list(contents)
    text = data[0].content
    assert text.startswith("M" * 10)
    assert "truncated" in text


async def test_markdown_resource_full_text_when_short():
    from mcp.server.mcpserver import MCPServer

    client = BatchClient()
    client.file_text = "short"
    server = MCPServer("test")
    register_tools(server, make_tools(client))

    contents = await server.read_resource("thinkparse://jobs/job_a/markdown")
    data = list(contents)
    assert data[0].content == "short"


async def test_image_resource_returns_bytes():
    from mcp.server.mcpserver import MCPServer

    client = BatchClient()
    server = MCPServer("test")
    register_tools(server, make_tools(client))

    contents = await server.read_resource(
        "thinkparse://jobs/job_a/images/page-0.jpg"
    )
    data = list(contents)
    assert data[0].content == client.file_bytes
