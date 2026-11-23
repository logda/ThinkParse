"""Security helpers: local path allowlist, magic numbers, URL/IP checks.

The module is split in two halves: path handling (this task) and URL/SSRF
guards appended by Task 5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


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
