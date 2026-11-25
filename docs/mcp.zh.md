# MCP 接入指南

ThinkParse MCP 服务（`mcp/`）把 `/api/v2` 网关包装成 MCP 工具集，供 AI Agent 客户端（OpenCode、Qoder、Claude Desktop 等）或自研应用调用。

**部署模型：MCP 在服务端，文件在客户端。** 两侧文件系统互相独立——客户端的本地路径对服务端没有意义。客户端文件通过 `PUT /uploads` 上传（与 `/mcp` 同端口、同 Bearer 鉴权），拿到 `file_id` 后再提交解析任务。文件字节走 HTTP 流式通道，不经过模型上下文，任意大小（默认上限 100 MiB）都可传。

## 连接

服务端点：`http://<host>:8765/mcp`（streamable-http），鉴权 `Authorization: Bearer <MCP_STATIC_TOKEN>`。

OpenCode 项目配置（`opencode.json`）：

```json
{
  "mcp": {
    "thinkparse": {
      "type": "remote",
      "url": "http://<host>:8765/mcp",
      "headers": { "Authorization": "Bearer <MCP_STATIC_TOKEN>" },
      "enabled": true
    }
  }
}
```

## 解析一个客户端本地文件（两步）

**第 1 步：上传。** 把文件原始字节 PUT 到 `/uploads`，`filename` 查询参数带扩展名：

```bash
curl -X PUT "http://<host>:8765/uploads?filename=paper.pdf" \
  -H "Authorization: Bearer <MCP_STATIC_TOKEN>" \
  --data-binary @paper.pdf
```

返回：

```json
{
  "file_id": "file_5ed318f21334722a",
  "filename": "paper.pdf",
  "bytes": 20073016,
  "sha256sum": "…",
  "status": "completed",
  "hint": "call parse_document(file_id=…) to submit a parse job"
}
```

要求与校验：`Content-Length` 必填（curl `--data-binary` 自动带上）；扩展名白名单（pdf/png/jpg/jpeg/webp/tif/tiff/bmp/xml/tex/eml）；魔数与扩展名必须一致；尺寸超限返回 413。相同 sha256 的文件在网关侧自动去重，重复上传秒回。

**第 2 步：提交解析。** 调 MCP 工具：

```
parse_document(file_id="file_5ed318f21334722a", tier="basic", wait_seconds=0)
→ get_job(job_id=…) 轮询 → get_result(job_id=…, kind="markdown") 分页读取
```

Agent 客户端中第 1 步用 shell 执行 curl 即可，文件字节不进入对话上下文。

## 工具一览

| 工具 | 用途 |
|---|---|
| `list_tiers` | 查询本部署可用档位（flash/basic/…） |
| `parse_document` | 提交解析。来源三选一：`file_id`（已上传）、`url`（服务端拉取公网 URL）、`path`（仅本地/挂载部署启用） |
| `get_job` | 查询任务状态、耗时、页数、产物引用 |
| `get_result` | 分页读取 markdown / middle_json / images |
| `cancel_job` | 取消排队或运行中的任务 |
| `submit_batch` | 批量提交：`file_ids`（已上传）或 `paths`（本地部署）二选一 |
| `get_batch` | 批次汇总 |

## 服务端配置（mcp 服务环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `THINKPARSE_BASE_URL` / `THINKPARSE_API_KEY` | 必填 | 网关地址与凭证（仅服务端持有，不下发客户端） |
| `MCP_STATIC_TOKEN` | 空 | 客户端 Bearer token，`/mcp` 与 `/uploads` 共用 |
| `MCP_TRUSTED_PROXY_CIDRS` | 必填（http） | 可信反代网段，命中后按 `X-Auth-User` 记身份 |
| `MCP_MAX_UPLOAD_BYTES` | 104857600 | 上传/下载尺寸上限（上传与 URL 下载共用） |
| `MCP_UPLOAD_DEADLINE_S` | 120 | 单次上传的整体时限（防慢速占用） |
| `MCP_ALLOWED_ROOT` | 空 | 服务端本地路径白名单；配置后默认启用 `path` 参数 |
| `MCP_ENABLE_LOCAL_PATH` | 随白名单 | 显式开/关 `path` 参数（1/0/true/false） |

远程部署（docker compose 默认）不挂载任何宿主目录，`path` 参数关闭；误传 `path` 会得到指向 `PUT /uploads` 的明确错误提示。本地 stdio 部署必须配置 `MCP_ALLOWED_ROOT`，`path` 行为不变。

## 安全边界

- 网关完全藏在内网，客户端只接触 MCP 一个端点、一套 token
- 上传文件名只接受纯 basename，拒绝路径穿越；内容做魔数校验
- 上传先落 0600 临时文件（容器内 tmpfs），完成或失败后一律清理
- `url` 来源沿用 SSRF 防护：仅公网地址、端口白名单、逐跳重校验