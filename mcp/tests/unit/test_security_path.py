"""Local path allowlist, symlink escape, and magic-number checks."""

from pathlib import Path

import pytest

from thinkparse_mcp.security import ALLOWED_EXTENSIONS, SecurityError, resolve_local_path, sniff_mime


def write_pdf(directory: Path, name: str = "doc.pdf", head: bytes = b"%PDF-1.7 rest") -> Path:
    target = directory / name
    target.write_bytes(head)
    return target


def test_pdf_inside_root_accepted(tmp_path):
    inside = write_pdf(tmp_path)
    assert resolve_local_path(str(inside), [tmp_path]) == inside.resolve()


def test_relative_path_inside_root_accepted(tmp_path, monkeypatch):
    write_pdf(tmp_path)
    monkeypatch.chdir(tmp_path)
    resolved = resolve_local_path("doc.pdf", [tmp_path])
    assert resolved == (tmp_path / "doc.pdf").resolve()


def test_path_outside_root_rejected(tmp_path):
    outside = write_pdf(tmp_path.parent)
    with pytest.raises(SecurityError, match="outside"):
        resolve_local_path(str(outside), [tmp_path])


def test_relative_escape_rejected(tmp_path):
    with pytest.raises(SecurityError):
        resolve_local_path("../evil.pdf", [tmp_path])


def test_symlink_escape_rejected(tmp_path):
    # The link lives inside the root but points at a file outside it.
    outside_dir = tmp_path.parent / "security-outside"
    outside_dir.mkdir(exist_ok=True)
    write_pdf(outside_dir)
    link = tmp_path / "link.pdf"
    link.symlink_to(outside_dir / "doc.pdf")
    with pytest.raises(SecurityError, match="outside"):
        resolve_local_path(str(link), [tmp_path])


def test_unknown_extension_rejected(tmp_path):
    evil = tmp_path / "doc.exe"
    evil.write_bytes(b"%PDF-1.7")
    with pytest.raises(SecurityError, match="extension"):
        resolve_local_path(str(evil), [tmp_path])


def test_missing_file_rejected(tmp_path):
    with pytest.raises(SecurityError):
        resolve_local_path("nope.pdf", [tmp_path])


def test_directory_rejected(tmp_path):
    (tmp_path / "dir.pdf").mkdir()
    with pytest.raises(SecurityError):
        resolve_local_path("dir.pdf", [tmp_path])


def test_fake_pdf_header_rejected_by_sniff():
    assert sniff_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 8, ".pdf") is None


def test_sniff_magic_matches():
    assert sniff_mime(b"%PDF-1.7", ".pdf") == "application/pdf"
    assert sniff_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 8, ".png") == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0", ".jpg") == "image/jpeg"
    assert sniff_mime(b"\xff\xd8\xff\xe0", ".jpeg") == "image/jpeg"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8", ".webp") == "image/webp"
    assert sniff_mime(b"II*\x00\x00\x00\x00\x00", ".tiff") == "image/tiff"
    assert sniff_mime(b"MM\x00*\x00\x00", ".tif") == "image/tiff"
    assert sniff_mime(b"BM\x00\x00\x00\x00", ".bmp") == "image/bmp"


def test_sniff_text_extensions_skip_magic():
    assert sniff_mime(b"Subject: hi", ".eml") == "message/rfc822"
    assert sniff_mime(b"<xml/>", ".xml") == "application/xml"
    assert sniff_mime(b"\\documentclass", ".tex") == "text/x-tex"


def test_allowed_extensions_set():
    assert ".pdf" in ALLOWED_EXTENSIONS
    assert ".exe" not in ALLOWED_EXTENSIONS
    assert ALLOWED_EXTENSIONS == frozenset(
        {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".xml", ".tex", ".eml"}
    )
