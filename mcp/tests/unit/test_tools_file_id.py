"""file_id sources and the local-path gate for remote deployments."""

import pytest

from thinkparse_mcp.client import UpstreamError
from thinkparse_mcp.config import ConfigError, Settings
from thinkparse_mcp.observability import Metrics
from thinkparse_mcp.tools import ToolError, Tools


def make_settings(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "k",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return Settings.from_env(env)


class FileIdClient:
    """Fake gateway: files already exist; uploads must never be called."""

    def __init__(self, meta=None, meta_error=None):
        self.meta = meta or {"sha256sum": "ab" * 32, "bytes": 27}
        self.meta_error = meta_error
        self.calls = []

    async def get_tiers(self):
        return {"object": "list", "discovered": True, "data": [{"id": "basic"}]}

    async def get_file_meta(self, file_id):
        self.calls.append(("get_file_meta", file_id))
        if self.meta_error is not None:
            raise self.meta_error
        return self.meta

    async def create_job(self, file_id, tier, ocr_mode, batch_id=None, priority=0):
        self.calls.append(("create_job", file_id, tier, ocr_mode, batch_id))
        return {"job_id": f"job_{file_id}", "status": "queued", "tier": tier}

    async def get_job(self, job_id):
        return {"job_id": job_id, "status": "queued", "tier": "basic"}

    async def create_upload(self, *args, **kwargs):
        raise AssertionError("file_id sources must not upload")

    async def put_upload_content(self, *args, **kwargs):
        raise AssertionError("file_id sources must not upload")

    async def complete_upload(self, *args, **kwargs):
        raise AssertionError("file_id sources must not upload")


def make_tools(client, **overrides):
    return Tools(client, make_settings(**overrides), Metrics())


# --- parse_document(file_id=...) ---------------------------------------------


async def test_parse_file_id_skips_upload_and_submits_directly():
    client = FileIdClient()
    tools = make_tools(client)

    payload = await tools.parse_document(file_id="file_9", wait_seconds=0)

    assert payload["job_id"] == "job_file_9"
    assert payload["status"] == "queued"
    assert ("get_file_meta", "file_9") in client.calls
    assert ("create_job", "file_9", "basic", "auto", None) in client.calls


async def test_parse_unknown_file_id_is_a_clear_error():
    client = FileIdClient(meta_error=UpstreamError(404, "file not found", False))
    tools = make_tools(client)

    with pytest.raises(ToolError, match="not found"):
        await tools.parse_document(file_id="file_missing", wait_seconds=0)


async def test_parse_requires_exactly_one_source(tmp_path):
    tools = make_tools(FileIdClient())
    with pytest.raises(ToolError, match="exactly one"):
        await tools.parse_document(file_id="file_9", url="http://example.com/a.pdf")
    with pytest.raises(ToolError, match="exactly one"):
        await tools.parse_document()


# --- local path gate ----------------------------------------------------------


async def test_path_rejected_in_remote_deployment_with_upload_hint(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7 x")
    tools = make_tools(FileIdClient())  # no MCP_ALLOWED_ROOT -> paths disabled

    with pytest.raises(ToolError, match="/uploads"):
        await tools.parse_document(path=str(pdf), wait_seconds=0)


async def test_path_can_be_forced_off_even_with_roots(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7 x")
    tools = make_tools(
        FileIdClient(),
        MCP_ALLOWED_ROOT=str(tmp_path),
        MCP_ENABLE_LOCAL_PATH="0",
    )

    with pytest.raises(ToolError, match="/uploads"):
        await tools.parse_document(path=str(pdf), wait_seconds=0)


def test_enable_local_path_defaults_follow_roots():
    assert make_settings().enable_local_path is False
    assert make_settings(MCP_ALLOWED_ROOT="/tmp").enable_local_path is True
    assert (
        make_settings(MCP_ALLOWED_ROOT="/tmp", MCP_ENABLE_LOCAL_PATH="false")
        .enable_local_path
        is False
    )
    assert make_settings(MCP_ENABLE_LOCAL_PATH="1").enable_local_path is True


def test_enable_local_path_rejects_garbage():
    with pytest.raises(ConfigError, match="MCP_ENABLE_LOCAL_PATH"):
        make_settings(MCP_ENABLE_LOCAL_PATH="maybe")


def test_upload_deadline_config():
    assert make_settings().upload_deadline_s == 120.0
    assert make_settings(MCP_UPLOAD_DEADLINE_S="5.5").upload_deadline_s == 5.5
    with pytest.raises(ConfigError, match="MCP_UPLOAD_DEADLINE_S"):
        make_settings(MCP_UPLOAD_DEADLINE_S="0")


# --- submit_batch(file_ids=...) -----------------------------------------------


async def test_batch_file_ids_submits_without_uploading():
    client = FileIdClient()
    tools = make_tools(client)

    payload = await tools.submit_batch(file_ids=["file_a", "file_b"], batch_id="b1")

    assert payload["batch_id"] == "b1"
    assert [job["job_id"] for job in payload["jobs"]] == ["job_file_a", "job_file_b"]
    assert ("create_job", "file_a", "basic", "auto", "b1") in client.calls
    assert ("create_job", "file_b", "basic", "auto", "b1") in client.calls


async def test_batch_requires_exactly_one_source_kind():
    tools = make_tools(FileIdClient())
    with pytest.raises(ToolError, match="exactly one"):
        await tools.submit_batch(paths=["a.pdf"], file_ids=["file_a"], batch_id="b1")
    with pytest.raises(ToolError, match="exactly one"):
        await tools.submit_batch(batch_id="b1")


async def test_batch_empty_file_ids_rejected():
    tools = make_tools(FileIdClient())
    with pytest.raises(ToolError, match="at least one"):
        await tools.submit_batch(file_ids=[], batch_id="b1")


async def test_batch_paths_rejected_in_remote_deployment(tmp_path):
    tools = make_tools(FileIdClient())
    with pytest.raises(ToolError, match="/uploads"):
        await tools.submit_batch(paths=[str(tmp_path / "a.pdf")], batch_id="b1")


# --- error messages must reach the MCP client ---------------------------------


async def test_registered_tool_error_message_reaches_client():
    """The SDK only surfaces messages of its own ToolError type; ours must
    subclass it, otherwise every rejection becomes a generic crash text."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as SDKToolError
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    from thinkparse_mcp.tools import register_tools

    tools = make_tools(FileIdClient())
    server = MCPServer("test")
    register_tools(server, tools)

    with pytest.raises(SDKToolError) as excinfo:
        await server.call_tool("parse_document", {"path": "/client/local/doc.pdf"})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "/uploads" in str(excinfo.value)