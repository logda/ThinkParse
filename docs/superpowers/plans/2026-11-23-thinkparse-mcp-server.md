# ThinkParse MCP Server 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在仓库内新建独立的 `mcp/` 包，把 ThinkParse `/api/v2` 包装成 MCP 工具集（parse_document / get_job / get_result / cancel_job / list_tiers），无状态、可分页、可安全部署。

**Architecture:** 协议适配层：Agent ⇄ MCP（stdio / streamable HTTP）⇄ ThinkParse `/api/v2`。所有上游 HTTP 调用集中在 `client.py`；`tools.py` 只做语义编排与分页；`security.py` 负责路径白名单与 SSRF 防护；不 import `control/` 包、不改 ThinkParse 源码。

**Tech Stack:** Python 3.12+，`mcp`（官方 SDK FastMCP）、`httpx`、`prometheus-client`。测试用 `pytest` + `pytest-asyncio`，集成桩用 `fastapi`/`uvicorn`（仅 dev 依赖）。构建 `hatchling`。

**Spec:** `/Users/caiyida/Documents/Qoder/2026-11-22/71cc1876/thinkparse-mcp-spec.md`（规格 §5 的 API 契约已对照 ThinkParse commit `335e4c2` 源码核实，本计划中的契约字段以源码核实结果为准）

## Global Constraints

- 不修改 `control/` 源码，不 `import control`，不直连 MinerU `:8002`（spec §13）。
- 所有对 ThinkParse 的 HTTP 调用集中在 `mcp/src/thinkparse_mcp/client.py`（spec §3）。
- 运行时依赖仅：`mcp>=1.9`、`httpx>=0.27`、`prometheus-client>=0.21`；不引入 fastapi/psycopg/boto3（spec §3）。
- 任何工具单次返回文本 ≤ `MCP_MAX_RESULT_CHARS`（默认 20000 字符），超出置 `truncated=true` + `next_offset`（spec §7）。
- `THINKPARSE_API_KEY` 不落日志、不进工具返回（spec §8）。
- 临时文件 0600、用完即删；日志记 sha256 不记文件名（spec §7/§8）。
- 轮询：1s 起步 ×1.5，上限 5s，总预算 `wait_seconds`（上限 120），终态立即返回（spec §7）。
- 单例 `httpx.AsyncClient(trust_env=False)`，连接池 20/keepalive 10，超时 connect 5s / read 60s；连接失败与 5xx 自动重试 1 次（退避 1s）（spec §7/§5.4）。
- **契约修正（源码核实，覆盖 spec §5 原文）：**
  - `POST /api/v2/uploads` body 必含 `filename`、`bytes`、`mime_type`，可选 `sha256sum`；带 `sha256sum` 且命中已有 blob 时直接返回 `status:"completed"` + `file.id`，此时跳过 PUT/complete（dedupe 短路）。
  - `POST /api/v2/uploads/{id}/complete` body 键是 `sha256sum`（不是 spec 写的 `sha256`）。
  - PUT 的字节数必须与 create 时声明的 `bytes` 完全一致，否则 400。
  - job 响应中 `output_files`、`error` 在 `files[0]` 下（非顶层）；顶层有 `links`、`timing{queue_ms,parse_ms,project_ms}`、`attempt`、`pages`、`tier`、`batch_id`、`priority`。
  - v2 状态五值：`queued/running/completed/failed/canceled`（canceled 单 l）。
  - `DELETE /api/v2/jobs/{id}` 命中返回 `{"job_id","status":"canceled"}`，未知 404。
  - compose 网关服务名是 `gateway`（spec §10 片段里的 `thinkparse` 不正确）。
  - `output_files` 实际键只有 `markdown`、`middle_json`、`images[]`、（docling 路由时）`docling_document`；**没有 `content_list`**。
- 上游错误映射（spec §5.4）：400→透传原文+建议 `list_tiers`；401/403→"服务端鉴权配置问题"（不暴露 key）；404→"job/upload 不存在"；413→超服务端上限；507→存储水位不足；重试后仍失败→可重试错误。任务 `failed` 是正常返回（含 `error.message`、`attempt`、`timing`），**不是** MCP 错误。

## Assumptions（超出/偏离 spec 的决策）

- v1 兼容接口不实现（spec §5.5 标"备选"，无工具使用它；故 spec §11 提到的 v1 状态映射测试不适用）。
- 幂等 LRU 缓存不实现（spec §7 标"可选，默认关闭"）。
- 新增两个 env：`MCP_STATIC_TOKEN`（spec §8 已提及）、`MCP_ALLOWED_PORTS`（默认 `80,443`，落实 spec §7"非标准端口按策略白名单"）。
- 真实 MinerU 端到端（compose cpu profile）为手工验收步骤（DoD），CI 用 stub 网关跑集成测试。
- P5（batch、resources）为独立任务，砍掉不影响 DoD。

## File Structure

```
mcp/
  pyproject.toml
  Dockerfile
  src/thinkparse_mcp/
    __init__.py            # 版本号
    __main__.py            # python -m thinkparse_mcp 入口（按 MCP_TRANSPORT 分流）
    config.py              # env 读取与校验（Task 1）
    client.py              # ThinkParse /api/v2 客户端，唯一 HTTP 出口（Task 2）
    observability.py       # JSON Lines 日志 + prometheus 指标（Task 3）
    security.py            # 路径白名单/魔数 + URL/IP/DNS/重定向校验 + 身份（Task 4/5/10）
    tools.py               # 工具实现与 FastMCP 注册（Task 6-9, 13）
    server.py              # FastMCP 装配、healthz/readyz/metrics、transport（Task 3）
  tests/
    unit/                  # 每个 Task 一个文件
    integration/
      stub_gateway.py      # 复刻已核实网关语义的内存桩（Task 12）
      test_end_to_end.py
    conftest.py
docs/superpowers/plans/    # 本计划
```

修改的既有文件：`docker/docker-compose.yml`（追加 `mcp` service）、`.github/workflows/ci.yml`（追加 job）。仅此两处。

## Review Focus

实现者最可能踩的五个坑（测试已分别钉死在对应任务里）：

1. **多字节 UTF-8 切片**：按 Python 字符（码点）切片而非字节，`offset+limit` 不会劈开一个字符 → Task 9 `test_slice_boundaries_are_codepoint_safe`。
2. **dedupe 短路**：`create_upload` 带 `sha256sum` 命中已有 blob 时返回 `completed`+`file.id`，此时再 PUT 会 409 → Task 7 `test_dedupe_short_circuit_skips_put`。
3. **重定向跳内网**：首跳校验通过后 302 到 `127.0.0.1` 必须再拒 → Task 5 `test_redirect_to_private_ip_rejected`。
4. **轮询超预算**：`sleep` 量不得超过剩余预算，`wait_seconds` 不会被动多等一轮 → Task 8 `test_poll_never_sleeps_past_budget`。
5. **下载超限中止**：URL 下载越过 `MCP_MAX_UPLOAD_BYTES` 时立即中止、删除临时文件、返回 413 语义错误 → Task 7 `test_download_aborts_at_limit_and_cleans_up`。

---

### Task 1: 包骨架与 `config.py`

**Files:**
- Create: `mcp/pyproject.toml`、`mcp/src/thinkparse_mcp/__init__.py`、`mcp/src/thinkparse_mcp/config.py`、`mcp/tests/unit/test_config.py`、`mcp/tests/conftest.py`

**Interfaces:**
- Produces:
  - `class ConfigError(RuntimeError)`；`@dataclass(frozen=True) class Settings`，字段：`base_url:str`（已 rstrip `/`）、`api_key:str`、`transport:str`（`"streamable-http"|"stdio"`）、`host:str`、`port:int`、`allowed_roots:tuple[Path,...]`、`trusted_proxy_cidrs:tuple`、`auth_header:str`、`static_token:str|None`、`allowed_ports:frozenset[int]`、`max_upload_bytes:int|None`、`poll_interval_s:float`、`poll_max_interval_s:float=5.0`、`default_wait_s:int=30`、`max_wait_s:int=120`、`max_result_chars:int=20000`、`log_level:str`。
  - `Settings.from_env(env: Mapping[str,str] | None = None) -> Settings`（默认读 `os.environ`，便于测试注入）。
  - 默认值与必填规则见 spec §4 表；`MCP_DEFAULT_WAIT_S` 上限 120，超出报 ConfigError。

- [ ] **Step 1: 写 `mcp/pyproject.toml`** — `[project]` name `thinkparse-mcp`、`requires-python = ">=3.12"`、dependencies 三项（Global Constraints）；`[dependency-groups] dev = ["pytest>=8","pytest-asyncio>=0.24","ruff>=0.6","fastapi>=0.115","uvicorn>=0.30"]`；hatchling 构建、src 布局；`[tool.pytest.ini_options] asyncio_mode = "auto"`。

- [ ] **Step 2: 写失败测试**（`tests/unit/test_config.py`）

```python
def test_missing_base_url_exits_with_clear_message():
    with pytest.raises(ConfigError, match="THINKPARSE_BASE_URL"):
        Settings.from_env({"THINKPARSE_API_KEY": "k"})

def test_stdio_requires_allowed_root():
    with pytest.raises(ConfigError, match="MCP_ALLOWED_ROOT"):
        Settings.from_env({"THINKPARSE_BASE_URL": "http://g:8000/", "THINKPARSE_API_KEY": "k", "MCP_TRANSPORT": "stdio"})

def test_http_requires_trusted_proxy_cidrs():
    ...  # 同上，transport 默认，断言 match="MCP_TRUSTED_PROXY_CIDRS"

def test_base_url_trailing_slash_stripped_and_wait_capped():
    s = Settings.from_env({... 合法 env，base_url "http://g:8000/"，MCP_DEFAULT_WAIT_S "999"})
    assert s.base_url == "http://g:8000"
    with pytest.raises(ConfigError, match="120"): Settings.from_env({... "MCP_DEFAULT_WAIT_S": "999"})
```

再加：transport 枚举非法报错、CIDR 解析错误报错、默认值（host `0.0.0.0`、port 8765、`X-Auth-User`、poll 1.0、max_result_chars 20000、allowed_ports `{80,443}`）。

- [ ] **Step 3: 运行验证失败** — `cd mcp && uv run pytest tests/unit/test_config.py -v`，预期 FAIL（模块不存在）。

- [ ] **Step 4: 实现 `config.py`** — 纯 env 解析 + 校验，无 I/O；`__init__.py` 放 `__version__ = "0.1.0"`。

- [ ] **Step 5: 运行验证通过** — 同命令，预期 PASS。

- [ ] **Step 6: Commit** — `git add mcp && git commit -m "feat(mcp): package skeleton and env config validation"`

### Task 2: `client.py` — ThinkParse 客户端（唯一 HTTP 出口）

**Files:**
- Create: `mcp/src/thinkparse_mcp/client.py`、`mcp/tests/unit/test_client.py`

**Interfaces:**
- Consumes: `Settings`（Task 1）。
- Produces:
  - `class UpstreamError(Exception)`，属性 `status:int`、`detail:str`、`retryable:bool`（401/403/404/400/413/507 → retryable=False；连接错误与 5xx 且重试用尽 → retryable=True）。
  - `class ThinkParseClient`：
    - `__init__(self, settings: Settings) -> None` — 构建单例 `httpx.AsyncClient(base_url=settings.base_url, headers={"Authorization": f"Bearer {settings.api_key}"}, trust_env=False, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10), timeout=httpx.Timeout(60.0, connect=5.0))`
    - `async def aclose(self) -> None`
    - `async def get_tiers(self) -> dict`（`GET /api/v2/tiers`，校验 `data` 为 `[{"id": str}]` 列表，否则 UpstreamError）
    - `async def health(self) -> dict`（`GET /api/v2/health`，非 2xx → UpstreamError）
    - `async def create_upload(self, filename: str, size: int, mime_type: str, sha256sum: str | None = None) -> dict`
    - `async def put_upload_content(self, upload_id: str, content: AsyncIterator[bytes], size: int) -> None` — 手动设 `Content-Length: size` 头（httpx 对生成器不自动设）
    - `async def complete_upload(self, upload_id: str, sha256sum: str) -> dict`
    - `async def create_job(self, file_id: str, tier: str, ocr_mode: str, batch_id: str | None = None, priority: int = 0) -> dict`
    - `async def get_job(self, job_id: str) -> dict`、`async def cancel_job(self, job_id: str) -> dict`
    - `async def get_file_meta(self, file_id: str) -> dict`、`async def get_file_text(self, file_id: str) -> str`
  - 内部 `async def _request(self, method: str, path: str, *, json=None, content=None, headers=None) -> httpx.Response` — 连接错误/5xx 重试 1 次（退避 1s，测试中可注入短退避），4xx 按映射抛 UpstreamError。

- [ ] **Step 1: 写失败测试**（`tests/unit/test_client.py`）— 用 `httpx.MockTransport` + `respx` 皆可；选 `httpx.MockTransport`（构造 client 后替换 `_client._transport`，或提供 `ThinkParseClient(settings, transport=...)` 注入参数）。用例：
  - `test_create_upload_posts_mime_type_and_bytes`：断言请求 JSON 含 `filename/bytes/mime_type`，dedupe 命中时响应 `status=="completed"` 且 `file.id` 存在则直接返回。
  - `test_complete_upload_uses_sha256sum_key`：断言请求体键名是 `sha256sum`。
  - `test_put_upload_streams_with_content_length`：断言 PUT 请求头 `Content-Length == size`。
  - `test_5xx_retries_once_then_raises_retryable`：两次 503 → UpstreamError(retryable=True)；一次 503 后 200 → 成功。
  - `test_401_maps_to_non_retryable_without_key`：UpstreamError.status==401，`str(exc)` 不含 api_key。
  - `test_create_job_posts_202_body`：断言 body `{"file_id","tier","ocr_mode"}`，接受 202。
  - `test_get_tiers_validates_shape`：畸形 `data` → UpstreamError。
- [ ] **Step 2: 运行验证失败** — `cd mcp && uv run pytest tests/unit/test_client.py -v`，预期 FAIL。
- [ ] **Step 3: 实现 `client.py`** — 全部走 `_request`；重试退避量作为构造参数 `retry_backoff_s: float = 1.0`（测试传 0）。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): httpx client for /api/v2 with retry and error mapping"`

### Task 3: `observability.py` + `server.py` — FastMCP 装配与 healthz/readyz

**Files:**
- Create: `mcp/src/thinkparse_mcp/observability.py`、`mcp/src/thinkparse_mcp/server.py`、`mcp/src/thinkparse_mcp/__main__.py`、`mcp/tests/unit/test_observability.py`、`mcp/tests/unit/test_server.py`

**Interfaces:**
- Consumes: `Settings`（Task 1）、`ThinkParseClient`（Task 2，可为 None）。
- Produces:
  - `observability.log_event(event: str, **fields) -> None` — JSON Lines 到 stdout，固定字段顺序含 `ts, level, event`；`LOG_LEVEL` 过滤。
  - `observability.Metrics` — 计数器 `mcp_tool_calls_total{tool,status}`、直方图 `mcp_tool_duration_seconds{tool}`、`mcp_jobs_wait_seconds`、`mcp_thinkparse_errors_total{code}`；`render() -> bytes`（prometheus text 格式）。
  - `server.build_app(settings: Settings, tools: Tools | None = None, client: ThinkParseClient | None = None) -> Starlette` — 路由：`GET /healthz`（恒 200 `{"status":"alive"}`）、`GET /readyz`（client 为 None 或 `client.health()` 非 2xx 或 tiers 不可解析 → 503）、`GET /metrics`（render 输出）、`Mount("/mcp", app=FastMCP 实例的 streamable_http_app())`。
  - `server.create_mcp(settings, tools) -> FastMCP` — `FastMCP("ThinkParse")`；工具注册由 `tools.register_tools(mcp, tools_instance)`（Task 6 实现；本任务 `tools=None` 时跳过注册）。
  - `__main__.py`：读 Settings；stdio → `mcp.run()`；streamable-http → `uvicorn.run(build_app(...), host, port)`；SIGTERM 优雅退出由 uvicorn `timeout_graceful_shutdown=10` 承担。
- 日志字段（spec §9）：`ts, level, event, user, tool, job_id, file_sha256, bytes, tier, ocr_mode, status, error_code, duration_ms, attempt`——`log_event` 接受任意 kwargs，工具层按此命名传参。

- [ ] **Step 1: 写失败测试** — `test_observability.py`：`log_event` 输出单行合法 JSON 且含 `ts/event`；`Metrics.render()` 含全部四个指标名。`test_server.py`：用 `httpx.AsyncClient(transport=ASGITransport(app=build_app(settings, tools=None, client=None)))` 断言 `/healthz` 200、`/readyz` 503；client 为注入 MockTransport 的健康 client 时 `/readyz` 200。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现两个文件** — `prometheus_client` 直接 import（运行时依赖）；`build_app` 接受注入便于测试。
- [ ] **Step 4: 运行验证通过** — `uv run pytest tests/unit -v`（此时应全绿，含 Task 1/2）。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): fastmcp assembly with healthz/readyz/metrics and jsonl logging"`

### Task 4: `security.py` 之一 — 本地路径白名单与魔数

**Files:**
- Create: `mcp/src/thinkparse_mcp/security.py`、`mcp/tests/unit/test_security_path.py`

**Interfaces:**
- Produces:
  - `class SecurityError(Exception)`（`str(exc)` 即面向用户的可读原因）。
  - `ALLOWED_EXTENSIONS = frozenset({".pdf",".png",".jpg",".jpeg",".webp",".tif",".tiff",".bmp",".xml",".tex",".eml"})`。
  - `resolve_local_path(raw: str, roots: Sequence[Path]) -> Path` — `Path(raw).resolve()` 后必须 `is_relative_to` 任一 root 的 resolved 形式；扩展名在白名单；存在且是常规文件（拒绝 FIFO/目录/缺失）。
  - `sniff_mime(head: bytes, ext: str) -> str | None` — 返回 `application/pdf`（`%PDF-`）、`image/png`（`\x89PNG`）、`image/jpeg`（`\xff\xd8\xff`）、`image/webp`（`RIFF…WEBP`）、`image/tiff`（`II*\x00`/`MM\x00*`）、`image/bmp`（`BM`）；`.xml/.tex/.eml` 不做魔数校验，按扩展映射 `application/xml`/`text/x-tex`/`message/rfc822`；二进制类魔数不符 → None（调用方转 SecurityError）。`head` 为文件前 16 字节。

- [ ] **Step 1: 写失败测试** — 用 `tmp_path` 构造：根内合法 PDF（写 `%PDF-1.7` 头）；根外路径拒绝；`ln -s` 指向根外的符号链接拒绝（`resolve()` 后逃逸）；`.exe` 扩展拒绝；`.pdf` 扩展但头是 PNG 拒绝（sniff 返回 None）；`.eml` 文本不做魔数校验；相对路径 `../escape` 拒绝。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — 无 I/O 之外依赖；符号链接逃逸靠 resolve 后比较。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): local path allowlist with symlink escape and magic-number checks"`

### Task 5: `security.py` 之二 — URL/IP/DNS/重定向 SSRF 防护

**Files:**
- Modify: `mcp/src/thinkparse_mcp/security.py`
- Test: `mcp/tests/unit/test_security_url.py`

**Interfaces:**
- Produces:
  - `validate_url(raw: str, allowed_ports: frozenset[int]) -> httpx.URL` — 仅 http/https；拒绝 userinfo、非白名单端口、非 ASCII 主机异常；返回 httpx.URL（host 已规范化）。
  - `ip_is_public(addr: ipaddress.IPv4Address | IPv6Address) -> bool` — 拒绝 loopback/private/link_local/reserved/multicast/unspecified 及 `169.254.169.254`、`100.64.0.0/10`（shared，`is_private` 在 3.12+ 已含，显式再列）。
  - `check_host_literal(host: str) -> None` — host 是 IP 字面量（含十进制/八进制整数形式，先 `ipaddress.ip_address` 尝试归一化）时校验 `ip_is_public`，否则交给 DNS 解析。
  - `async def resolve_public_host(host: str, resolver=None) -> list` — 用 `loop.getaddrinfo` 解析两次比对（DNS 重绑定防护，两次结果集合必须一致且全部 `ip_is_public`）；`resolver` 参数为可注入的 `async (host) -> list[addr]` 以便测试。
  - `async def fetch_public(url: httpx.URL, client: httpx.AsyncClient, max_hops: int = 5) -> httpx.Response` — `follow_redirects=False` 手动跟随：每跳先 `validate_url` + `check_host_literal`（域名时 `resolve_public_host`），3xx 则取 `Location` 继续；超 5 跳或任一跳违规 → SecurityError。
- 单元测试不真实联网：`check_host_literal`/`ip_is_public` 纯函数直接测；`resolve_public_host` 注入 resolver；`fetch_public` 用 MockTransport（对 302 Location 指向 `http://127.0.0.1/x` 的用例，MockTransport 返回该 302 响应，断言被拒）。

- [ ] **Step 1: 写失败测试** — 用例（spec §11 安全用例）：
  - `http://127.0.0.1:8000`、`http://10.0.0.5`、`http://[::1]`、`http://169.254.169.254`、十进制 `http://2130706433`、八进制 `http://0177.0.0.1` → 全部 SecurityError。
  - `http://user:pass@example.com` → 拒绝（userinfo）；`http://example.com:8080`（默认端口集）→ 拒绝。
  - 公网 IP `http://8.8.8.8`、域名 `https://example.com` → 通过。
  - `test_dns_rebinding_detected`：注入 resolver 两次返回不同 IP → SecurityError。
  - `test_redirect_to_private_ip_rejected`（Review Focus #3）。
  - `test_redirect_chain_exceeds_five_hops_rejected`。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — IPv6 字面量注意方括号剥离（httpx.URL.host 已无括号）。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): ssrf guard covering ip literals, rebinding, and redirects"`

### Task 6: `tools.py` 之一 — `list_tiers` / `get_job` / `cancel_job` 与注册

**Files:**
- Create: `mcp/src/thinkparse_mcp/tools.py`、`mcp/tests/unit/test_tools_basic.py`

**Interfaces:**
- Consumes: `ThinkParseClient`、`Settings`、`Metrics`、`log_event`。
- Produces:
  - `class ToolError(Exception)` — `str(exc)` 面向模型；FastMCP 会转为 isError 响应。
  - `class TierCache` — `__init__(self, client, ttl_s: float = 60.0)`；`async def get(self) -> tuple[bool, list[str]]`（60s 内缓存）；`async def validate(self, tier: str) -> list[str]`（不在列表 → ToolError 列出可用档位）。
  - `_job_view(job: dict) -> dict` — 把网关 job JSON 投影为工具契约：`{job_id, status, tier, attempt, pages, timing, files(=files[0].output_files 或 None), error(=files[0].error 或 None), links}`。
  - `class Tools` — `__init__(self, client, settings, metrics)`；方法（即 MCP 工具体）：
    - `async def list_tiers(self) -> dict` → `{"discovered": bool, "tiers": [{"id": ...}], "recommended_default": "basic"}`（档位不可用时仍返回空列表+discovered，不抛错）。
    - `async def get_job(self, job_id: str) -> dict` → `_job_view`；404 → ToolError("job 不存在")。
    - `async def cancel_job(self, job_id: str) -> dict` → `{"job_id", "status": "canceled"}`。
  - `def register_tools(mcp: FastMCP, tools: Tools) -> None` — 用 `@mcp.tool()` 装饰器语义注册三个工具，`description` 按 spec §6（用途、参数语义、默认档位、何时改用 `get_job`）。
- 网关 404/400 等经 `UpstreamError` → `_map_upstream_error(exc)` → ToolError 文案（Global Constraints 错误映射表）。

- [ ] **Step 1: 写失败测试** — `Tools` 注入假 client（手写 FakeClient 记录调用、返回固定 dict）。用例：`list_tiers` 形状；`TierCache` 60s 内第二次不触发上游调用（假 client 计数）；`_job_view` 扁平化 `files[0].output_files`；`get_job` 404 → ToolError；`cancel_job` 透传；`_map_upstream_error` 各状态码文案（400 文案含 "list_tiers"、401 文案不含 api_key）。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — 同时把 Task 3 的 `build_app` 接上真 `register_tools`（`server.py` 已预留调用点）。
- [ ] **Step 4: 运行验证通过** — `uv run pytest tests/unit -v` 全绿。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): list_tiers/get_job/cancel_job tools with tier cache"`

### Task 7: `tools.py` 之二 — 上传管线（本地路径 / URL → file_id）

**Files:**
- Modify: `mcp/src/thinkparse_mcp/tools.py`
- Test: `mcp/tests/unit/test_tools_upload.py`

**Interfaces:**
- Consumes: `resolve_local_path`/`sniff_mime`（Task 4）、`validate_url`/`fetch_public`（Task 5）、client 的三个上传方法（Task 2）。
- Produces:
  - `async def _source_to_file(self, path: str | None, url: str | None) -> tuple[str, str, int]`（返回 `(file_id, sha256sum, size)`，内部：本地读文件流式算 sha → create → PUT → complete；URL 下载到 `tempfile`（0600）流式算 sha/size → 同三步；dedupe 命中时跳过 PUT/complete）。
  - `_download_to_temp(url: str, settings) -> tuple[Path, str, int]` — `httpx.AsyncClient(trust_env=False, follow_redirects=False)` + `fetch_public`；边写边计数，超 `max_upload_bytes` 立即中止、删临时文件、ToolError（413 语义）；下载完成后 `sniff_mime` 校验头（文本扩展不校验）；用 `finally` 删临时文件。
  - MIME：`sniff_mime(head, ext)`；魔数失败 → SecurityError。
- `parse_document` 本任务先实现 `wait_seconds=0` 路径（提交即返回），轮询留给 Task 8。

- [ ] **Step 1: 写失败测试** — FakeClient + tmp 文件。用例：
  - `test_upload_local_pdf_calls_three_steps_in_order`：断言 create 请求 `mime_type=="application/pdf"`、`bytes==size`、complete 收到正确 `sha256sum`。
  - `test_dedupe_short_circuit_skips_put`（Review Focus #2）：create 返回 `{"status":"completed","file":{"id":"file_x"}}` → PUT/complete 未被调用，file_id 直接拿到。
  - `test_upload_declared_bytes_must_match`：FakeClient PUT 模拟网关 400（字节不符）→ ToolError。
  - `test_download_aborts_at_limit_and_cleans_up`（Review Focus #5）：MockTransport 提供无限流，`max_upload_bytes` 设小 → ToolError 且 tmp 目录无残留。
  - `test_download_rejects_fake_extension`：URL 下载 `.pdf` 内容实为 PNG → 拒绝。
  - `test_path_and_url_mutually_exclusive`：两者都传 / 都不传 → ToolError。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — PUT 用异步生成器按 1MB 分块；`Content-Length` 显式设置（Task 2 已支持）。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): three-step upload pipeline from local path or url"`

### Task 8: `tools.py` 之三 — `parse_document` 轮询/进度/摘要

**Files:**
- Modify: `mcp/src/thinkparse_mcp/tools.py`
- Test: `mcp/tests/unit/test_tools_parse.py`

**Interfaces:**
- Produces:
  - `async def _wait_for_terminal(self, job_id: str, wait_seconds: int, ctx=None) -> dict` — 轮询循环：`interval = settings.poll_interval_s`；每轮 `get_job`，终态即返回；`sleep = min(interval, deadline - now)`，`sleep <= 0` 即退出；`interval = min(interval*1.5, poll_max_interval_s)`；每轮 `ctx.report_progress(progress, total=wait_seconds)`（ctx 为 None 时跳过）。
  - `async def parse_document(self, path=None, url=None, tier="basic", ocr_mode="auto", wait_seconds=None, ctx=None) -> dict` — 返回 spec §6.1 契约：`{job_id, status, tier, pages, timing, files, error, hint, truncated}` + 完成时 `summary`（markdown 前 `max_result_chars` 字符，`truncated` 标记）。`wait_seconds` 默认取 `settings.default_wait_s`，钳到 `max_wait_s`；0 = 只提交。未终态时 `hint="job still running; call get_job(job_id=...)"`。`failed` 是正常返回（Global Constraints）。日志记 `file_sha256/bytes/tier/ocr_mode/status/duration_ms`。
- 假 ctx：`FakeCtx(report_calls: list)` 记录 `(progress, total)`。

- [ ] **Step 1: 写失败测试** — 用例：
  - `test_parse_wait_zero_returns_queued_view`（提交即返回，无第二次 get_job）。
  - `test_parse_polls_until_terminal`：FakeClient 依序返回 queued→running→completed（可控制序列）+ 注入 `poll_interval_s=0` → 返回 completed + `summary` 来自 `get_file_text("file_md")` 截断到 `max_result_chars`。
  - `test_poll_never_sleeps_past_budget`（Review Focus #4）：`wait_seconds=2`、interval 1.0×1.5…，FakeClient 永远 running → 总耗时 ≤ 2.0s+ε（对 FakeClient 的 sleep 打桩或用 monotonic 实测，interval 用 0.05 量级）。
  - `test_failed_job_is_normal_return_not_error`：终态 failed → 返回 dict 含 `error.message`、`attempt`、`timing`，不抛。
  - `test_unavailable_tier_short_circuits_before_submit`：TierCache 返回 `["flash"]`，请求 `basic` → ToolError 文案列出 `flash`，FakeClient 的 create_job 未被调用。
  - `test_progress_reported_each_round`。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — 档位校验在最前（TierCache.validate）；摘要字符切片复用 Task 9 前先内联简单切片，Task 9 统一。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): parse_document with backoff polling, progress, and summary"`

### Task 9: `tools.py` 之四 — `get_result` 分页

**Files:**
- Modify: `mcp/src/thinkparse_mcp/tools.py`
- Test: `mcp/tests/unit/test_tools_result.py`

**Interfaces:**
- Produces:
  - `async def get_result(self, job_id: str, kind: str = "markdown", offset: int = 0, limit: int | None = None, path: str | None = None) -> dict` — spec §6.3 契约 `{kind, total_chars, offset, next_offset|null, text|null, items|null, eof}`；`limit` 缺省 = `settings.max_result_chars` 并钳到它；负 offset → ToolError。
  - kind 语义（基于已核实 `output_files` 键集）：
    - `markdown`：`get_file_text(files.markdown.file_id)` 全文拉取，按 Python 码点切片。
    - `middle_json`：按 JSON 文本切片；`path` 非空时先 `json.loads`，按点路径取子树再 `json.dumps`（受 max_result_chars 截断，`total_chars` 为子树长度）。
    - `content_list`：`output_files` 无此键（已核实）→ ToolError `"v2 未暴露 content_list 独立产物，请用 kind='middle_json'（可配 path 过滤）"`。
    - `images`：返回 `items=[{filename, bytes, uri}]`，`uri = f"thinkparse://jobs/{job_id}/images/{filename}"`，`offset/limit` 按条目数，默认 50、上限 500，`eof` 按条目计。
  - 未终态/无产物（`files` 为 None 或缺该键）→ ToolError 提示先 `get_job`。

- [ ] **Step 1: 写失败测试** — 用例：
  - `test_slice_boundaries_are_codepoint_safe`（Review Focus #1）：构造含 emoji/CJK 的 markdown，offset 落在代理对/多字节中间，断言切片不劈字符、`total_chars` 为码点数、`next_offset/eof` 正确。
  - `test_offset_past_end_returns_empty_eof`。
  - `test_limit_clamped_to_max_result_chars`。
  - `test_images_paginates_by_entries_with_uri`（limit 默认 50、上限 500）。
  - `test_middle_json_path_filter`：`path="pdf_info"` 取子树。
  - `test_content_list_returns_helpful_error`。
  - `test_running_job_get_result_errors_with_hint`。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现** — 单次拉全文后切片（网关不支持 Range）；文中注明该取舍。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): paginated get_result across markdown, middle_json, images"`

### Task 10: `security.py` 之三 — 身份（可信代理 CIDR + 静态令牌）

**Files:**
- Modify: `mcp/src/thinkparse_mcp/security.py`、`mcp/src/thinkparse_mcp/server.py`
- Test: `mcp/tests/unit/test_identity.py`

**Interfaces:**
- Produces:
  - `caller_identity(headers, peer_ip: str, cidrs, auth_header: str, static_token: str | None) -> str` — peer_ip 命中可信 CIDR → 取身份头（缺失则 `"anonymous"`）；否则若提供了 `MCP_STATIC_TOKEN` 且 `Authorization: Bearer` 匹配 → `"static"`，不匹配或未配置 → 抛 `SecurityError`（HTTP 401）。stdio 场景由 server 层直接给 `"local"`。
  - `server.build_app` 增加纯 ASGI 中间件：仅对 `/mcp` 路径执行身份检查，失败返回 401 JSON；身份写入 `scope["state"]["user"]` 供日志使用；`/healthz` `/readyz` `/metrics` 不做身份检查。
- 注意：stdio 传输无中间件路径，`Tools` 日志的 `user` 取 `"local"`。

- [ ] **Step 1: 写失败测试** — ASGITransport 打到 `/mcp`：
  - 可信 CIDR 内 peer + `X-Auth-User: alice` → 放行且日志 user=alice（透传到下游 mount 的方式可断言 `scope` 注入）。
  - 可信 CIDR 外 peer 带伪造 `X-Auth-User` → 401（spec §8：绝不信任客户端自报身份——注意语义：非可信网段时该头被忽略，仅凭静态令牌）。
  - 非可信 peer + 正确静态 Bearer → 放行 user=static。
  - `/healthz` 无凭据可访问。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): trusted-proxy identity and static-token auth for http transport"`

### Task 11: `mcp/Dockerfile` + compose service + CI

**Files:**
- Create: `mcp/Dockerfile`
- Modify: `docker/docker-compose.yml`（追加 `mcp` service）、`.github/workflows/ci.yml`（追加 job）

**Interfaces:**
- Consumes: `__main__.py`（Task 3）。
- Produces: 可构建镜像与 compose 服务。

- [ ] **Step 1: 写 `mcp/Dockerfile`** — 多阶段：`python:3.12-slim` + uv 安装依赖到 venv；运行阶段非 root 用户 `app`（uid 10001）、`ENV MCP_TRANSPORT=streamable-http`、入口 `python -m thinkparse_mcp`；不含 fastapi/uvicorn 运行时（仅 dev group 不装）。
- [ ] **Step 2: 修改 `docker/docker-compose.yml`** — 追加（对齐既有缩进与 `x-` 锚点风格）：

```yaml
  mcp:
    build:
      context: ../mcp
      dockerfile: Dockerfile
    image: thinkparse-mcp:0.1.0
    environment:
      THINKPARSE_BASE_URL: http://gateway:8000
      THINKPARSE_API_KEY: ${THINKPARSE_API_KEY:-}
      MCP_TRANSPORT: streamable-http
      MCP_TRUSTED_PROXY_CIDRS: ${MCP_TRUSTED_PROXY_CIDRS:-10.0.0.0/8}
    ports: ["127.0.0.1:8765:8765"]
    read_only: true
    tmpfs: ["/tmp"]
    depends_on:
      gateway:
        condition: service_started
    restart: unless-stopped
```

- [ ] **Step 3: 修改 `.github/workflows/ci.yml`** — 追加 job `mcp-test`（setup-python 3.12、`cd mcp && uv run pytest`、`uvx ruff check src tests`）与 `mcp-docker-build`（buildx 构建 `mcp/Dockerfile`，不推送）。
- [ ] **Step 4: 本地验证** — `cd mcp && uv run pytest` 全绿；`uvx ruff check src tests` 通过；`docker build` 成功（本机有 docker 时；无则在 CI 验证）。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): docker image, compose service, and ci jobs"`

### Task 12: 集成测试（stub 网关，覆盖 spec §11 集成 1–7）

**Files:**
- Create: `mcp/tests/integration/stub_gateway.py`、`mcp/tests/integration/test_end_to_end.py`

**Interfaces:**
- Consumes: 全部已实现模块；`mcp` SDK 客户端（`mcp.client.streamable_http`）做一次协议级调用。
- Produces: `stub_gateway.create_app(state) -> FastAPI` — 内存实现已核实的 v2 语义：三步上传（字节计数校验、sha256 dedupe、`sha256sum` complete 键）、jobs CRUD（可配置延迟后转 completed，产出 markdown/middle_json/images 的 `files[0].output_files`）、`/api/v2/tiers`（可配置集合）、`/api/v2/health`、Bearer 校验。仅测试用（fastapi 在 dev group）。

- [ ] **Step 1: 写 stub** — 按 Global Constraints 修正后的契约实现；一个 `StubState` dataclass 控制延迟与档位。
- [ ] **Step 2: 写集成测试** — `pytest.mark.integration`（默认跳过需 `--run-integration` 或 env `MCP_IT=1`）。uvicorn 起在随机端口，`Settings` 指向它，直接驱动 `Tools`；另起一个 streamable-http MCP server（`build_app` + ASGITransport 或真端口），用 SDK 客户端 list tools 并调 `list_tiers`。用例（对应 spec §11）：
  1. 本地小 PDF → `parse_document` → completed → `get_result(markdown)` 拿到全文。
  2. `wait_seconds=0` → queued → `get_job` 轮询到完成。
  3. `cancel_job` → canceled，随后 `get_job` 仍可查。
  4. 大 markdown（> max_result_chars）分页拼接后与全文一致（含多字节字符）。
  5. stub 档位不含 `basic` → 可读错误列出可用档位。
  6. 指向已关闭端口 → 可重试错误，进程不崩。
  7. 新建第二个 `Tools` 实例（模拟 MCP 重启）→ 旧 job_id 仍可查（stub 状态保留）。
- [ ] **Step 3: 运行** — `cd mcp && MCP_IT=1 uv run pytest tests/integration -v` 预期 PASS；不带 env 时 skip。
- [ ] **Step 4: Commit** — `git commit -m "test(mcp): end-to-end suite against an in-memory v2 gateway stub"`

### Task 13 (P5，可选): `submit_batch` / `get_batch` 与 MCP resources

**Files:**
- Modify: `mcp/src/thinkparse_mcp/tools.py`、`mcp/src/thinkparse_mcp/server.py`
- Test: `mcp/tests/unit/test_tools_batch.py`

**Interfaces:**
- Produces:
  - `async def submit_batch(self, paths: list[str], batch_id: str, tier: str = "basic", ocr_mode: str = "auto") -> dict` — 复用 `_source_to_file` + `create_job(batch_id=...)`，返回 `{batch_id, jobs: [{job_id, status}]}`；`batch_id` 先用与网关一致的 `^[A-Za-z0-9_-]{1,64}$` 本地校验。
  - `async def get_batch(self, batch_id: str) -> dict` — `GET /api/v2/batches/{id}` + `/api/v2/batches/{id}/jobs`（client 增补这两个方法），汇总状态与失败项。
  - resources：`thinkparse://jobs/{id}/markdown`（读 markdown 全文，仍受 max_result_chars 截断返回截断说明）、`thinkparse://jobs/{id}/images/{name}`（图片字节，`mime_type` 来自 `get_file_meta`）——用 FastMCP `@mcp.resource("thinkparse://jobs/{job_id}/markdown")` 注册。
- [ ] **Step 1: 写失败测试** — submit_batch 逐个上传、同一 batch_id；get_batch 汇总（含失败项）；batch_id 格式非法 → ToolError。
- [ ] **Step 2: 运行验证失败** — 预期 FAIL。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行验证通过** — 预期 PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): batch submission, batch summary, and thinkparse resources"`

---

## 手工验收（DoD，spec §11）

完成后在 compose `cpu` profile（或已有 MinerU 服务）上人工执行：真实小 PDF 两入口各一次完整解析；`wait_seconds=0` 轮询；取消；大 PDF 分页 sha256 复原；重连续查；日志可回答"谁、何时、哪个 hash、哪档、耗时"；stdio 冷启动 < 3s（`MCP_TRANSPORT=stdio uv run python -m thinkparse_mcp` 计时）。
