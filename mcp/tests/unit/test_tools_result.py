"""get_result: codepoint-safe pagination across markdown/json/images."""

import json

import pytest

from thinkparse_mcp.config import Settings
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


class ResultClient:
    def __init__(self, job, texts=None):
        self.job = job
        self.texts = texts or {}
        self.calls = []

    async def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return self.job

    async def get_file_text(self, file_id):
        self.calls.append(("get_file_text", file_id))
        return self.texts[file_id]


def make_tools(client, **setting_overrides):
    return Tools(client, make_settings(**setting_overrides), Metrics())


def _completed_job(images=None):
    outputs = {
        "markdown": {"file_id": "file_md", "bytes": 3},
        "middle_json": {"file_id": "file_mid", "bytes": 4},
    }
    if images is not None:
        outputs["images"] = images
    return {
        "job_id": "job_1",
        "status": "completed",
        "files": [{"output_files": outputs, "error": None}],
    }


EMOJI_TEXT = "\U0001F3AC ab\U0001F4C4你好"  # 7 codepoints


async def test_slice_boundaries_are_codepoint_safe():
    client = ResultClient(_completed_job(), texts={"file_md": EMOJI_TEXT})
    tools = make_tools(client)

    payload = await tools.get_result("job_1", kind="markdown", offset=1, limit=3)

    assert payload["total_chars"] == 7
    assert payload["text"] == EMOJI_TEXT[1:4] == " ab"
    assert payload["offset"] == 1
    assert payload["next_offset"] == 4
    assert payload["eof"] is False
    assert payload["items"] is None


async def test_offset_past_end_returns_empty_eof():
    client = ResultClient(_completed_job(), texts={"file_md": "abc"})
    tools = make_tools(client)

    payload = await tools.get_result("job_1", kind="markdown", offset=100)

    assert payload["text"] == ""
    assert payload["next_offset"] is None
    assert payload["eof"] is True
    assert payload["total_chars"] == 3


async def test_limit_clamped_to_max_result_chars():
    client = ResultClient(_completed_job(), texts={"file_md": "x" * 100})
    tools = make_tools(client, MCP_MAX_RESULT_CHARS="10")

    default = await tools.get_result("job_1", kind="markdown", offset=0)
    assert default["total_chars"] == 100
    assert len(default["text"]) == 10
    assert default["next_offset"] == 10

    huge = await tools.get_result("job_1", kind="markdown", limit=99999)
    assert len(huge["text"]) == 10
    assert huge["next_offset"] == 10

    small = await tools.get_result("job_1", kind="markdown", limit=5)
    assert len(small["text"]) == 5


async def test_negative_offset_rejected():
    tools = make_tools(ResultClient(_completed_job(), texts={"file_md": "abc"}))
    with pytest.raises(ToolError, match="offset"):
        await tools.get_result("job_1", kind="markdown", offset=-1)


async def test_images_paginates_by_entries_with_uri():
    images = [
        {"file_id": f"file_img_{i}", "filename": f"img_{i}.jpg", "bytes": 5}
        for i in range(60)
    ]
    client = ResultClient(_completed_job(images=images))
    tools = make_tools(client)

    first = await tools.get_result("job_1", kind="images")
    assert first["total_chars"] == 60
    assert len(first["items"]) == 50  # default limit
    assert first["items"][0] == {
        "filename": "img_0.jpg",
        "bytes": 5,
        "uri": "thinkparse://jobs/job_1/images/img_0.jpg",
    }
    assert first["next_offset"] == 50
    assert first["eof"] is False

    second = await tools.get_result("job_1", kind="images", offset=50)
    assert len(second["items"]) == 10
    assert second["next_offset"] is None
    assert second["eof"] is True

    clamped = await tools.get_result("job_1", kind="images", limit=600)
    assert len(clamped["items"]) == 60  # 60 < clamped-to-500 limit


async def test_middle_json_path_filter():
    doc = {"pdf_info": {"pages": 3}, "other": 1}
    client = ResultClient(
        _completed_job(), texts={"file_mid": json.dumps(doc, ensure_ascii=False)}
    )
    tools = make_tools(client)

    payload = await tools.get_result(
        "job_1", kind="middle_json", path="pdf_info"
    )
    expected = json.dumps({"pages": 3}, ensure_ascii=False)
    assert payload["text"] == expected
    assert payload["total_chars"] == len(expected)
    assert payload["eof"] is True


async def test_content_list_returns_helpful_error():
    tools = make_tools(ResultClient(_completed_job(), texts={"file_md": "abc"}))
    with pytest.raises(ToolError, match="middle_json"):
        await tools.get_result("job_1", kind="content_list")


async def test_running_job_get_result_errors_with_hint():
    job = {
        "job_id": "job_1",
        "status": "running",
        "files": [{"output_files": None, "error": None}],
    }
    tools = make_tools(ResultClient(job))
    with pytest.raises(ToolError, match="get_job"):
        await tools.get_result("job_1", kind="markdown")


async def test_unknown_kind_rejected():
    tools = make_tools(ResultClient(_completed_job(), texts={"file_md": "abc"}))
    with pytest.raises(ToolError, match="kind"):
        await tools.get_result("job_1", kind="html")


async def test_register_tools_exposes_get_result():
    from mcp.server.mcpserver import MCPServer

    from thinkparse_mcp.tools import register_tools

    server = MCPServer("test")
    tools = make_tools(ResultClient(_completed_job(), texts={"file_md": "abc"}))
    register_tools(server, tools)
    names = {tool.name for tool in await server.list_tools()}
    assert "get_result" in names
