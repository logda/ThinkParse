"""SSRF guards: IP classification, URL validation, DNS rebinding, redirects."""

import ipaddress

import httpx
import pytest

from thinkparse_mcp.security import (
    SecurityError,
    fetch_public,
    ip_is_public,
    resolve_public_host,
    validate_url,
)

ALLOWED_PORTS = frozenset({80, 443})


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:8000",
        "http://10.0.0.5",
        "http://[::1]",
        "http://169.254.169.254",
        "http://2130706433",
        "http://0177.0.0.1",
        "http://user:pass@example.com",
        "http://example.com:8080",
        "file:///etc/passwd",
        "gopher://example.com",
        "http://example.com@127.0.0.1/",
        "http://100.64.0.1",
    ],
)
def test_validate_url_rejects_dangerous_forms(raw):
    with pytest.raises(SecurityError):
        validate_url(raw, ALLOWED_PORTS)


def test_validate_url_accepts_public_literal_and_domain():
    assert validate_url("http://8.8.8.8", ALLOWED_PORTS).host == "8.8.8.8"
    assert validate_url("https://example.com", ALLOWED_PORTS).host == "example.com"


def test_validate_url_accepts_custom_port_when_allowed():
    url = validate_url("http://8.8.8.8:8443", frozenset({80, 443, 8443}))
    assert url.port == 8443


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12:3456:789a::1",
    ],
)
def test_ip_is_public_rejects_non_public(raw):
    assert ip_is_public(ipaddress.ip_address(raw)) is False


def test_ip_is_public_accepts_public():
    assert ip_is_public(ipaddress.ip_address("8.8.8.8")) is True
    assert ip_is_public(ipaddress.ip_address("2001:4860:4860::8888")) is True


async def test_resolve_public_host_returns_public_addresses():
    async def resolver(host):
        assert host == "example.com"
        return ["93.184.216.34"]

    addresses = await resolve_public_host("example.com", resolver=resolver)
    assert addresses == ["93.184.216.34"]


async def test_dns_rebinding_detected():
    calls = []

    async def resolver(_host):
        calls.append(1)
        return ["93.184.216.34"] if len(calls) == 1 else ["10.0.0.5"]

    with pytest.raises(SecurityError, match="rebinding"):
        await resolve_public_host("example.com", resolver=resolver)


async def test_resolve_public_host_rejects_private_answers():
    async def resolver(_host):
        return ["127.0.0.1"]

    with pytest.raises(SecurityError, match="private"):
        await resolve_public_host("example.com", resolver=resolver)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)


async def test_fetch_public_returns_final_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "8.8.8.8"
        return httpx.Response(200, content=b"payload")

    async with _client(handler) as client:
        response = await fetch_public(
            httpx.URL("http://8.8.8.8/doc"), client, allowed_ports=ALLOWED_PORTS
        )
    assert response.status_code == 200


async def test_redirect_to_private_ip_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/doc":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/x"})
        raise AssertionError("must not follow into the private redirect")

    async with _client(handler) as client:
        with pytest.raises(SecurityError, match="outside the public"):
            await fetch_public(httpx.URL("http://8.8.8.8/doc"), client, allowed_ports=ALLOWED_PORTS)


async def test_redirect_chain_exceeds_five_hops_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://8.8.8.8/next"})

    async with _client(handler) as client:
        with pytest.raises(SecurityError, match="redirect"):
            await fetch_public(httpx.URL("http://8.8.8.8/doc"), client, allowed_ports=ALLOWED_PORTS)
