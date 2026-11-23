# ThinkParse MCP server image: streamable-http transport, non-root.
# Build from the mcp/ directory: docker build -t thinkparse-mcp:0.1.0 .
ARG BUILD_BASE=python:3.12-slim

FROM ${BUILD_BASE} AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
# Runtime deps only (uvicorn/starlette arrive as mcp's transitive deps).
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http
USER app
EXPOSE 8765
ENTRYPOINT ["python", "-m", "thinkparse_mcp"]
