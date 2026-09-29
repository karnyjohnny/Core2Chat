"""Attachment pipeline tests: detection, limits, encoding, previews."""

import os

import pytest

from core.attachment_manager import (AttachmentError, AttachmentManager,
                                     read_text_file)
from models.attachment_models import AttachmentKind, AttachmentStatus
from utils import mime as mime_util


@pytest.fixture
def manager():
    return AttachmentManager(max_text_bytes=4096, max_image_bytes=8192,
                             max_inline_bytes=16384)


def write(tmp_path, name, data):
    path = tmp_path / name
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------- detection
def test_text_file_is_detected_and_decoded(manager, tmp_path):
    path = write(tmp_path, "skrypt.py", "print('cześć')\n")
    attachment = manager.from_path(path)
    assert attachment.kind == AttachmentKind.TEXT
    assert attachment.mime_type == "text/x-python"
    assert attachment.language == "python"
    assert attachment.status == AttachmentStatus.READY
    assert "cześć" in attachment.text_preview
    assert attachment.sha256 and len(attachment.sha256) == 64
    assert attachment.size_bytes == len("print('cześć')\n".encode("utf-8"))


def test_image_file_uses_magic_bytes_not_extension(manager, tmp_path):
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    path = write(tmp_path, "udaje.txt", png)
    attachment = manager.from_path(path)
    assert attachment.kind == AttachmentKind.IMAGE
    assert attachment.mime_type == "image/png"


def test_pdf_is_classified_as_document(manager, tmp_path):
    path = write(tmp_path, "dokument.pdf", b"%PDF-1.7\n%trailer\n")
    attachment = manager.from_path(path)
    assert attachment.kind == AttachmentKind.DOCUMENT
    assert attachment.mime_type == "application/pdf"


def test_unsupported_extension_is_rejected(manager, tmp_path):
    path = write(tmp_path, "archiwum.zip", b"PK\x03\x04binary")
    with pytest.raises(AttachmentError) as info:
        manager.from_path(path)
    assert "Nieobsługiwany" in str(info.value)


def test_missing_file_is_reported(manager, tmp_path):
    with pytest.raises(AttachmentError):
        manager.from_path(str(tmp_path / "nie-ma.log"))


def test_oversized_text_file_is_rejected(manager, tmp_path):
    path = write(tmp_path, "duzy.log", "x" * 10_000)
    with pytest.raises(AttachmentError) as info:
        manager.from_path(path)
    assert "za duży" in str(info.value)


def test_size_limits_are_configurable(tmp_path):
    small = AttachmentManager(max_text_bytes=16, max_image_bytes=16)
    path = write(tmp_path, "a.txt", "0123456789abcdef0123456789")
    with pytest.raises(AttachmentError):
        small.from_path(path)
    large = AttachmentManager(max_text_bytes=4096, max_image_bytes=4096)
    assert large.from_path(path).kind == AttachmentKind.TEXT


# ----------------------------------------------------------------- encodings
def test_utf8_bom_is_handled(manager, tmp_path):
    path = write(tmp_path, "bom.txt", "\ufefftreść z BOM".encode("utf-8"))
    attachment = manager.from_path(path)
    assert not attachment.text_preview.startswith("\ufeff")
    assert "treść z BOM" in attachment.text_preview


def test_cp1250_fallback(manager, tmp_path):
    path = write(tmp_path, "win.txt", "zażółć gęślą jaźń".encode("cp1250"))
    attachment = manager.from_path(path)
    assert "zażółć" in attachment.text_preview


def test_binary_content_is_not_silently_accepted_as_text(manager, tmp_path):
    data = bytes(range(0, 32)) * 20 + b"tekst"
    path = write(tmp_path, "bin.log", data)
    attachment = manager.from_path(path)
    # Control characters are stripped and the payload is flagged as limited,
    # so a binary file can never be sent to the model pretending to be text.
    assert attachment.kind == AttachmentKind.TEXT
    assert attachment.truncated is True
    assert "tekst" in attachment.text_preview
    assert not any(ord(ch) < 32 and ch not in "\n\t"
                   for ch in attachment.text_preview)


def test_oversized_files_are_rejected_with_a_clear_limit(tmp_path):
    """Hard limit protects memory; trimming happens later in ContextManager."""
    manager = AttachmentManager(max_text_bytes=64, max_image_bytes=64)
    path = write(tmp_path, "długie.txt", "a" * 200)
    with pytest.raises(AttachmentError) as info:
        manager.from_path(path)
    assert "limit" in str(info.value)
    assert "64 B" in str(info.value)


def test_clipboard_text_over_the_limit_is_trimmed_and_flagged(manager):
    attachment = manager.from_bytes(("z" * 9000).encode("utf-8"), "duzy.txt")
    assert attachment.truncated is True
    assert len(attachment.text_preview) <= manager.max_text_bytes


# ------------------------------------------------------------------ clipboard
def test_from_bytes_image(manager):
    png = b"\x89PNG\r\n\x1a\n" + b"\x11" * 64
    attachment = manager.from_bytes(png, "zrzut.png")
    assert attachment.kind == AttachmentKind.IMAGE
    assert attachment.mime_type == "image/png"
    assert attachment.status == AttachmentStatus.READY
    assert attachment.payload == png


def test_from_text_clipboard(manager):
    attachment = manager.from_text("wklejony tekst")
    assert attachment.kind == AttachmentKind.TEXT
    assert attachment.text_preview == "wklejony tekst"
    assert attachment.metadata["source"] == "clipboard-text"


def test_from_bytes_rejects_empty_and_unsupported(manager):
    with pytest.raises(AttachmentError):
        manager.from_bytes(b"", "pusty.png")
    with pytest.raises(AttachmentError):
        manager.from_bytes(b"MZ\x90\x00binary", "plik.exe")


def test_from_bytes_truncates_text_but_rejects_huge_images(manager):
    text = manager.from_bytes(("y" * 9000).encode(), "duzy.txt")
    assert text.truncated is True
    with pytest.raises(AttachmentError):
        manager.from_bytes(b"\xff\xd8\xff" + b"0" * 9000, "duze.jpg")


# -------------------------------------------------------------- drag and drop
def test_validate_drop_splits_accepted_and_rejected(manager, tmp_path):
    good = write(tmp_path, "dobry.py", "x = 1")
    bad_ext = write(tmp_path, "zly.zip", b"PK\x03\x04xxxx")
    missing = str(tmp_path / "brak.txt")
    accepted, rejected = manager.validate_drop([good, bad_ext, missing,
                                                str(tmp_path)])
    assert accepted == [good]
    assert any("nieobsługiwany" in item for item in rejected)
    assert any("nie jest plikiem" in item for item in rejected)


# -------------------------------------------------------------------- previews
def test_preview_requires_qt_and_is_cached(manager):
    pytest.importorskip("PyQt5")
    from PyQt5.QtGui import QImage

    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(0xFF3366CC)
    import base64
    from PyQt5.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    png = bytes(buffer.data())

    attachment = manager.from_bytes(png, "duzy_obraz.png")
    attachment.id = 42
    preview = manager.preview_for(attachment)
    assert preview is not None
    assert max(preview.width(), preview.height()) <= manager.preview_max_edge
    assert manager.preview_for(attachment) is preview    # cached instance
    manager.clear_previews()
    assert manager.preview_cache == {}


def test_preview_is_none_for_non_images(manager):
    attachment = manager.from_text("tekst")
    assert manager.preview_for(attachment) is None


# --------------------------------------------------------------- memory rules
def test_payload_can_be_released_after_use(manager, tmp_path):
    path = write(tmp_path, "a.txt", "zawartość")
    attachment = manager.from_path(path)
    attachment.id = 7
    manager.register(attachment)
    assert attachment.payload
    manager.release(7)
    assert attachment.payload is None
    assert manager.get(7) is attachment


def test_release_all_is_safe_for_unknown_ids(manager):
    manager.release_all([1, 2, 3])


def test_describe_reports_human_readable_state(manager, tmp_path):
    path = write(tmp_path, "raport.md", "# tytuł")
    attachment = manager.from_path(path)
    info = manager.describe([attachment])[0]
    assert info["filename"] == "raport.md"
    assert info["kind"] == AttachmentKind.TEXT
    assert info["size"].endswith("B")
    assert info["status"] == AttachmentStatus.READY
    assert info["estimated_tokens"] >= 1


def test_update_limits_applies(manager):
    manager.update_limits(max_text_bytes=1234, max_image_bytes=4321)
    assert manager.max_text_bytes == 1234
    assert manager.max_image_bytes == 4321


# ------------------------------------------------------------------- helpers
def test_read_text_file_helper(tmp_path):
    path = write(tmp_path, "x.txt", "linie\ndwie\n")
    text, truncated = read_text_file(path, limit=4096)
    assert text == "linie\ndwie\n"
    assert truncated is False


def test_mime_helpers():
    assert mime_util.guess_mime("a.md") == "text/markdown"
    assert mime_util.guess_mime("a.unknownext") == "application/octet-stream"
    assert mime_util.kind_of("a.webp") == AttachmentKind.IMAGE
    assert mime_util.kind_of("a.pdf") == AttachmentKind.DOCUMENT
    assert mime_util.kind_of("a.mp3") == AttachmentKind.AUDIO
    assert mime_util.kind_of("a.mp4") == AttachmentKind.VIDEO
    assert mime_util.kind_of("a.exe") == AttachmentKind.UNSUPPORTED
    assert mime_util.is_supported("a.py") is True
    assert mime_util.language_of("main.rs") == "rust"
    assert mime_util.is_textual_mime("text/plain") is True
    assert mime_util.is_textual_mime("image/png") is False


def test_webp_magic_requires_riff_webp():
    assert mime_util.guess_mime("x.bin", b"RIFFxxxxWEBP") == "image/webp"
    assert mime_util.guess_mime("x.bin", b"RIFFxxxxAVI ") != "image/webp"


def test_filename_sanitisation_in_attachments(manager, tmp_path):
    path = write(tmp_path, "ok.txt", "x")
    attachment = manager.from_path(path)
    attachment.filename = "../../etc/passwd"
    from utils.paths import safe_filename
    assert safe_filename(attachment.filename) == "etc_passwd" \
        or "/" not in safe_filename(attachment.filename)
    assert safe_filename("") == "attachment"
    assert safe_filename("a" * 400).count("a") <= 120
