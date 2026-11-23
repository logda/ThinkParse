"""Environment-driven configuration with startup validation.

Every required variable that is missing or malformed raises ConfigError with
the variable name in the message so the operator gets an actionable message
instead of a traceback with a default secret.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


_TRANSPORTS = ("streamable-http", "stdio")
_MAX_WAIT_S = 120


@dataclass(frozen=True)
class Settings:
    """Validated server configuration."""

    base_url: str
    api_key: str
    transport: str = "streamable-http"
    host: str = "0.0.0.0"
    port: int = 8765
    allowed_roots: tuple[Path, ...] = ()
    trusted_proxy_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    auth_header: str = "X-Auth-User"
    static_token: str | None = None
    allowed_ports: frozenset[int] = frozenset({80, 443})
    max_upload_bytes: int | None = None
    poll_interval_s: float = 1.0
    poll_max_interval_s: float = 5.0
    default_wait_s: int = 30
    max_wait_s: int = _MAX_WAIT_S
    max_result_chars: int = 20000
    log_level: str = "INFO"
    retry_backoff_s: float = 1.0
    _source: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = dict(os.environ) if env is None else dict(env)

        base_url = _required(source, "THINKPARSE_BASE_URL").rstrip("/")
        if urlparse(base_url).scheme not in {"http", "https"}:
            raise ConfigError("THINKPARSE_BASE_URL must be an http(s) URL")

        api_key = _required(source, "THINKPARSE_API_KEY")

        transport = source.get("MCP_TRANSPORT", "streamable-http")
        if transport not in _TRANSPORTS:
            raise ConfigError(f"MCP_TRANSPORT must be one of {', '.join(_TRANSPORTS)}")

        port = _int(source.get("MCP_PORT", "8765"), "MCP_PORT")
        if not 0 < port < 65536:
            raise ConfigError("MCP_PORT must be a valid port number")

        allowed_roots: tuple[Path, ...] = ()
        raw_roots = source.get("MCP_ALLOWED_ROOT", "")
        if raw_roots.strip():
            allowed_roots = tuple(Path(part.strip()) for part in raw_roots.split(",") if part.strip())

        raw_cidrs = source.get("MCP_TRUSTED_PROXY_CIDRS", "").strip()
        cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
        if raw_cidrs:
            try:
                cidrs = tuple(
                    ipaddress.ip_network(part.strip()) for part in raw_cidrs.split(",") if part.strip()
                )
            except ValueError as exc:
                raise ConfigError(f"MCP_TRUSTED_PROXY_CIDRS contains an invalid network: {exc}") from exc

        if transport == "stdio" and not allowed_roots:
            raise ConfigError("MCP_ALLOWED_ROOT is required for the stdio transport")
        if transport == "streamable-http" and not cidrs:
            raise ConfigError("MCP_TRUSTED_PROXY_CIDRS is required for the streamable-http transport")

        raw_ports = source.get("MCP_ALLOWED_PORTS", "80,443")
        allowed_ports = frozenset(
            _int(part, "MCP_ALLOWED_PORTS") for part in raw_ports.split(",") if part.strip()
        )

        poll_interval_s = _float(source.get("MCP_POLL_INTERVAL_S", "1.0"), "MCP_POLL_INTERVAL_S")
        if poll_interval_s <= 0:
            raise ConfigError("MCP_POLL_INTERVAL_S must be positive")

        default_wait_s = _int(source.get("MCP_DEFAULT_WAIT_S", "30"), "MCP_DEFAULT_WAIT_S")
        if default_wait_s < 0 or default_wait_s > _MAX_WAIT_S:
            raise ConfigError(f"MCP_DEFAULT_WAIT_S must be between 0 and {_MAX_WAIT_S}")

        max_result_chars = _int(source.get("MCP_MAX_RESULT_CHARS", "20000"), "MCP_MAX_RESULT_CHARS")
        if max_result_chars <= 0:
            raise ConfigError("MCP_MAX_RESULT_CHARS must be positive")

        return cls(
            base_url=base_url,
            api_key=api_key,
            transport=transport,
            host=source.get("MCP_HOST", "0.0.0.0"),
            port=port,
            allowed_roots=allowed_roots,
            trusted_proxy_cidrs=cidrs,
            auth_header=source.get("MCP_AUTH_HEADER", "X-Auth-User"),
            static_token=source.get("MCP_STATIC_TOKEN") or None,
            allowed_ports=allowed_ports,
            max_upload_bytes=_optional_int(source.get("MCP_MAX_UPLOAD_BYTES"), "MCP_MAX_UPLOAD_BYTES"),
            poll_interval_s=poll_interval_s,
            poll_max_interval_s=_MAX_POLL_INTERVAL_S,
            default_wait_s=default_wait_s,
            max_result_chars=max_result_chars,
            log_level=source.get("LOG_LEVEL", "INFO").upper(),
            _source=source,
        )


_MAX_POLL_INTERVAL_S = 5.0


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _optional_int(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    return _int(raw, name)


def _float(raw: str, name: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
