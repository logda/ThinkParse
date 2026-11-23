"""Config parsing and validation for the thinkparse-mcp server."""

import ipaddress
from pathlib import Path

import pytest

from thinkparse_mcp.config import ConfigError, Settings


def base_env(**overrides):
    env = {
        "THINKPARSE_BASE_URL": "http://gateway:8000",
        "THINKPARSE_API_KEY": "secret-key",
        "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


def test_missing_base_url_exits_with_clear_message():
    with pytest.raises(ConfigError, match="THINKPARSE_BASE_URL"):
        Settings.from_env({"THINKPARSE_API_KEY": "k", "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8"})


def test_missing_api_key_exits_with_clear_message():
    with pytest.raises(ConfigError, match="THINKPARSE_API_KEY"):
        Settings.from_env({"THINKPARSE_BASE_URL": "http://g:8000", "MCP_TRUSTED_PROXY_CIDRS": "10.0.0.0/8"})


def test_stdio_requires_allowed_root():
    with pytest.raises(ConfigError, match="MCP_ALLOWED_ROOT"):
        Settings.from_env(base_env(MCP_TRANSPORT="stdio"))


def test_http_requires_trusted_proxy_cidrs():
    with pytest.raises(ConfigError, match="MCP_TRUSTED_PROXY_CIDRS"):
        Settings.from_env({"THINKPARSE_BASE_URL": "http://g:8000", "THINKPARSE_API_KEY": "k"})


def test_base_url_trailing_slash_stripped():
    settings = Settings.from_env(base_env(THINKPARSE_BASE_URL="http://g:8000/"))
    assert settings.base_url == "http://g:8000"


def test_base_url_must_be_http():
    with pytest.raises(ConfigError, match="THINKPARSE_BASE_URL"):
        Settings.from_env(base_env(THINKPARSE_BASE_URL="ftp://g:8000"))


def test_default_wait_s_capped_at_120():
    with pytest.raises(ConfigError, match="MCP_DEFAULT_WAIT_S"):
        Settings.from_env(base_env(MCP_DEFAULT_WAIT_S="999"))


def test_invalid_transport_rejected():
    with pytest.raises(ConfigError, match="MCP_TRANSPORT"):
        Settings.from_env(base_env(MCP_TRANSPORT="websocket"))


def test_invalid_cidr_rejected():
    with pytest.raises(ConfigError, match="MCP_TRUSTED_PROXY_CIDRS"):
        Settings.from_env(base_env(MCP_TRUSTED_PROXY_CIDRS="not-a-cidr"))


def test_non_numeric_port_rejected():
    with pytest.raises(ConfigError, match="MCP_PORT"):
        Settings.from_env(base_env(MCP_PORT="https"))


def test_defaults():
    settings = Settings.from_env(base_env())
    assert settings.transport == "streamable-http"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8765
    assert settings.auth_header == "X-Auth-User"
    assert settings.poll_interval_s == 1.0
    assert settings.poll_max_interval_s == 5.0
    assert settings.default_wait_s == 30
    assert settings.max_wait_s == 120
    assert settings.max_result_chars == 20000
    assert settings.allowed_ports == frozenset({80, 443})
    assert settings.static_token is None
    assert settings.max_upload_bytes == 104857600
    assert settings.download_deadline_s == 120.0
    assert settings.allowed_roots == ()
    assert settings.trusted_proxy_cidrs == (ipaddress.ip_network("10.0.0.0/8"),)


def test_stdio_parses_multiple_allowed_roots():
    settings = Settings.from_env(
        base_env(MCP_TRANSPORT="stdio", MCP_ALLOWED_ROOT="/tmp,/var/data")
    )
    assert settings.allowed_roots == (Path("/tmp"), Path("/var/data"))


def test_max_upload_bytes_parsed():
    settings = Settings.from_env(base_env(MCP_MAX_UPLOAD_BYTES="10485760"))
    assert settings.max_upload_bytes == 10485760


def test_poll_interval_bounds_enforced():
    with pytest.raises(ConfigError, match="MCP_POLL_INTERVAL_S"):
        Settings.from_env(base_env(MCP_POLL_INTERVAL_S="0"))
