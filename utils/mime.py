"""MIME detection.

Extension mapping is a hint only; for binary payloads we additionally sniff
magic bytes so that a mislabelled file is never sent to the API with a wrong
content type.
"""

import mimetypes
import os
from typing import Optional, Tuple

from core.constants import (DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS,
                            TEXT_EXTENSIONS)

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"
KIND_UNSUPPORTED = "unsupported"

_EXTRA_MIMES = {
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".rs": "text/x-rust",
    ".go": "text/x-go",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".log": "text/plain",
    ".ts": "text/typescript",
    ".js": "text/javascript",
    ".json": "application/json",
    ".sql": "text/x-sql",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

# Language identifier used by the syntax highlighter, keyed by extension.
EXT_TO_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".json": "json", ".md": "markdown", ".html": "html", ".css": "css",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rs": "rust",
    ".go": "go", ".java": "java", ".cs": "csharp", ".sql": "sql",
    ".xml": "xml", ".yaml": "yaml", ".yml": "yaml", ".ini": "ini",
    ".cfg": "ini", ".sh": "bash", ".bash": "bash", ".log": "text",
    ".txt": "text",
}

_MAGIC: Tuple[Tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"RIFF", 0, "image/webp"),
    (b"%PDF-", 0, "application/pdf"),
    (b"GIF8", 0, "image/gif"),
)


def extension_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def guess_mime(filename: str, head: Optional[bytes] = None) -> str:
    """Return a best-effort MIME type; magic bytes win over the extension."""
    if head:
        for magic, offset, mime in _MAGIC:
            end = offset + len(magic)
            if len(head) >= end and head[offset:end] == magic:
                if mime == "image/webp" and head[8:12] != b"WEBP":
                    continue
                return mime
    ext = extension_of(filename)
    if ext in _EXTRA_MIMES:
        return _EXTRA_MIMES[ext]
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    if ext in TEXT_EXTENSIONS:
        return "text/plain"
    return "application/octet-stream"


def kind_of(filename: str, mime: Optional[str] = None,
            head: Optional[bytes] = None) -> str:
    """Classify an attachment into a coarse pipeline category."""
    mime = mime or guess_mime(filename, head)
    ext = extension_of(filename)
    if mime.startswith("image/") or ext in IMAGE_EXTENSIONS:
        return KIND_IMAGE
    if mime == "application/pdf" or ext in DOCUMENT_EXTENSIONS:
        return KIND_DOCUMENT
    if mime.startswith("audio/"):
        return KIND_AUDIO
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime.startswith("text/") or ext in TEXT_EXTENSIONS:
        return KIND_TEXT
    return KIND_UNSUPPORTED


def is_supported(filename: str, mime: Optional[str] = None) -> bool:
    return kind_of(filename, mime) != KIND_UNSUPPORTED


def language_of(filename: str) -> str:
    return EXT_TO_LANGUAGE.get(extension_of(filename), "text")


def is_textual_mime(mime: str) -> bool:
    if mime.startswith("text/"):
        return True
    return mime in ("application/json", "application/xml",
                    "application/x-yaml", "application/javascript")
