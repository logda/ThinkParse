"""Security helpers: local path allowlist, magic numbers, URL/IP checks.

The module is split in two halves: path handling (this task) and URL/SSRF
guards appended by Task 5.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import re
from pathlib import Path
from typing import Sequence

import httpx


class SecurityError(Exception):
    """A user-facing rejection with the reason in str(exc)."""


ALLOWED_EXTENSIONS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".xml", ".tex", ".eml"}
)

_TEXT_MIME_BY_EXT = {
    ".xml": "application/xml",
    ".tex": "text/x-tex",
    ".eml": "message/rfc822",
}


def resolve_local_path(raw: str, roots: Sequence[Path]) -> Path:
    """Resolve a user-supplied path against the allowed roots.

    Symlinks are resolved away before comparison, so a link pointing outside
    a root is rejected even when its own name sits inside the root.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    allowed = [root.expanduser().resolve() for root in roots]
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise SecurityError(
            f"path is outside the allowed roots: {raw} (allowed: {[str(r) for r in allowed]})"
        )
    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise SecurityError(
            f"extension {resolved.suffix or '<none>'!r} is not allowed; "
            f"allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )
    if not resolved.exists():
        raise SecurityError(f"file does not exist: {raw}")
    if not resolved.is_file():
        raise SecurityError(f"path is not a regular file: {raw}")
    return resolved


def sniff_mime(head: bytes, ext: str) -> str | None:
    """Map leading bytes + extension to a MIME type; None means rejected.

    Text formats (.xml/.tex/.eml) are not magic-checked; everything else must
    match its magic number or the content is not trusted (spec §7).
    """
    lowered = ext.lower()
    if lowered in _TEXT_MIME_BY_EXT:
        return _TEXT_MIME_BY_EXT[lowered]
    if head.startswith(b"%PDF-"):
        return "application/pdf" if lowered == ".pdf" else None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png" if lowered == ".png" else None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg" if lowered in {".jpg", ".jpeg"} else None
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp" if lowered == ".webp" else None
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image/tiff" if lowered in {".tif", ".tiff"} else None
    if head.startswith(b"BM"):
        return "image/bmp" if lowered == ".bmp" else None
    return None


# --- URL / SSRF guards ------------------------------------------------------

_DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
_SHARED_V4 = ipaddress.ip_network("100.64.0.0/10")
_NUMERIC_HOST = re.compile(r"^[0-9.]+$")


def ip_is_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for public unicast addresses (spec §7)."""
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return False
    if addr.version == 4 and addr in _SHARED_V4:
        return False
    return True


def check_host_literal(host: str) -> None:
    """Reject non-public IP literals, including non-canonical numeric forms."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if not ip_is_public(addr):
            raise SecurityError(
                f"host is an address outside the public unicast range: {host}"
            )
        return
    if _NUMERIC_HOST.fullmatch(host):
        raise SecurityError(f"non-canonical numeric IP literal rejected: {host}")


def validate_url(raw: str, allowed_ports: frozenset[int]) -> httpx.URL:
    """Parse and validate a fetch URL: scheme, credentials, port policy, host."""
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol, ValueError) as exc:
        raise SecurityError(f"invalid URL: {raw!r} ({exc})") from exc
    if url.scheme not in {"http", "https"}:
        raise SecurityError(f"only http/https URLs are allowed: {raw!r}")
    if url.username or url.password:
        raise SecurityError("URLs with embedded credentials are rejected")
    port = url.port
    if port is not None and port not in allowed_ports:
        raise SecurityError(
            f"port {port} is not in the allowed port policy {sorted(allowed_ports)}"
        )
    if not url.host:
        raise SecurityError("URL has no host")
    check_host_literal(url.host)
    return url


async def resolve_public_host(
    host: str,
    resolver=None,
) -> list[str]:
    """Resolve a hostname twice and require stable, public answers.

    Two differing answer sets are treated as DNS rebinding (spec §8).
    """
    if resolver is None:
        resolver = _system_resolver
    first = await resolver(host)
    second = await resolver(host)
    if set(first) != set(second):
        raise SecurityError(
            f"DNS rebinding detected for {host}: answers changed between resolutions"
        )
    addresses: list[str] = []
    for item in first:
        addr = ipaddress.ip_address(item)
        if not ip_is_public(addr):
            raise SecurityError(f"host {host} resolves to a private address: {addr}")
        addresses.append(item)
    return addresses


async def _system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return sorted({info[4][0] for info in infos})


async def fetch_public(
    url: httpx.URL | str,
    client: httpx.AsyncClient,
    max_hops: int = 5,
    allowed_ports: frozenset[int] = _DEFAULT_ALLOWED_PORTS,
    resolver=None,
) -> httpx.Response:
    """Fetch with manual redirect following: every hop is re-validated.

    The response is streamed (not buffered) so callers can enforce download
    size limits while reading; the caller must `await response.aclose()`.
    """
    current = validate_url(str(url), allowed_ports)
    redirects = 0
    while True:
        await _guard_host(current.host, resolver)
        request = client.build_request("GET", current)
        response = await client.send(request, stream=True)
        if response.has_redirect_location:
            location = response.headers["location"]
            await response.aclose()
            redirects += 1
            if redirects > max_hops:
                raise SecurityError(
                    f"more than {max_hops} redirects while fetching {url}"
                )
            current = validate_url(str(current.join(location)), allowed_ports)
            continue
        return response


async def _guard_host(host: str, resolver) -> None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        await resolve_public_host(host, resolver=resolver)


# --- caller identity (trusted proxy + static token) -------------------------


def _in_trusted_cidrs(peer_ip: str, cidrs) -> bool:
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in network for network in cidrs)


def caller_identity(
    headers,
    peer_ip: str,
    cidrs,
    auth_header: str,
    static_token: str | None,
) -> str:
    """Resolve the calling identity for the HTTP transport (spec §8).

    The client-supplied identity header is honored only when the TCP peer is
    inside a trusted proxy CIDR; anything else must present the static token.
    """
    if _in_trusted_cidrs(peer_ip, cidrs):
        return headers.get(auth_header) or "anonymous"
    if static_token is None:
        raise SecurityError(
            "the request is not from a trusted proxy and no static token is "
            "configured; refusing to trust client-supplied credentials"
        )
    presented = headers.get("authorization") or ""
    expected = f"Bearer {static_token}"
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise SecurityError("invalid or missing static token")
    return "static"
