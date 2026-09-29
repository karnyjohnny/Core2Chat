"""Attachment ingestion: read, validate, hash, preview, upload.

Design rules (specification §13):

* never load an unbounded file into memory - hard, configurable limits;
* text files are decoded with a fallback chain (UTF-8 -> BOM -> latin-1);
* images get a small preview instead of retaining the decoded original;
* every attachment carries a SHA-256 fingerprint for cache/request identity;
* all blocking I/O happens in worker threads, never on the GUI thread.
"""

import hashlib
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.logging_setup import get_logger
from models.attachment_models import Attachment, AttachmentKind, AttachmentStatus
from utils import mime as mime_util
from utils.paths import human_size, safe_filename
from utils.text import estimate_tokens

log = get_logger("attachments")

# Decoding fallback chain: strict UTF-8, then UTF-8 with BOM, then lossy.
# utf-8-sig first: strips a BOM when present, behaves like utf-8 otherwise.
_TEXT_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "cp1250", "latin-1")
CHUNK = 64 * 1024
SNIFF_BYTES = 4096


class AttachmentError(RuntimeError):
    """User-presentable attachment failure."""


class AttachmentManager(object):
    """Prepares attachments for the provider and keeps memory bounded."""

    def __init__(self, max_text_bytes: int = 2 * 1024 * 1024,
                 max_image_bytes: int = 8 * 1024 * 1024,
                 max_inline_bytes: int = 15 * 1024 * 1024,
                 preview_max_edge: int = 256) -> None:
        self.max_text_bytes = int(max_text_bytes)
        self.max_image_bytes = int(max_image_bytes)
        self.max_inline_bytes = int(max_inline_bytes)
        self.preview_max_edge = int(preview_max_edge)
        self._lock = threading.RLock()
        self._by_id: Dict[int, Attachment] = {}
        self.preview_cache: Dict[int, Any] = {}

    # ------------------------------------------------------------ ingestion
    def from_path(self, path: str,
                  session_id: Optional[int] = None) -> Attachment:
        """Create an attachment from a file on disk (blocking I/O)."""
        if not path or not os.path.isfile(path):
            raise AttachmentError("Plik nie istnieje: %s" % os.path.basename(path or ""))
        filename = safe_filename(os.path.basename(path))
        size = os.path.getsize(path)
        head = self._read_head(path, SNIFF_BYTES)
        mime = mime_util.guess_mime(filename, head)
        kind = mime_util.kind_of(filename, mime, head)
        if kind == AttachmentKind.UNSUPPORTED:
            raise AttachmentError(
                "Nieobsługiwany typ pliku: %s" % (mime or filename))
        limit = self._limit_for(kind)
        if size > limit:
            raise AttachmentError(
                "Plik %s jest za duży (%s, limit %s)."
                % (filename, human_size(size), human_size(limit)))
        payload = self._read_bytes(path, limit)
        attachment = Attachment(
            session_id=session_id, filename=filename, mime_type=mime, kind=kind,
            local_path=os.path.abspath(path), size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            status=AttachmentStatus.READY,
            language=mime_util.language_of(filename), payload=payload)
        if kind == AttachmentKind.TEXT:
            text, truncated = self.decode_text(payload, self.max_text_bytes)
            attachment.text_preview = text
            attachment.truncated = truncated
            attachment.size_bytes = len(payload)
        with self._lock:
            if attachment.id is not None:
                self._by_id[attachment.id] = attachment
        log.info("attachments.added file=%s kind=%s size=%d", filename, kind,
                 len(payload))
        return attachment

    def from_bytes(self, data: bytes, filename: str, mime_type: str = "",
                   session_id: Optional[int] = None,
                   source: str = "clipboard") -> Attachment:
        """Create an attachment from memory (clipboard image, drag payload)."""
        if not data:
            raise AttachmentError("Schowek nie zawiera danych do wysłania.")
        filename = safe_filename(filename or "clipboard.png")
        head = data[:SNIFF_BYTES]
        mime = mime_type or mime_util.guess_mime(filename, head)
        kind = mime_util.kind_of(filename, mime, head)
        if kind == AttachmentKind.UNSUPPORTED:
            raise AttachmentError("Nieobsługiwany typ danych: %s" % mime)
        limit = self._limit_for(kind)
        truncated = False
        if len(data) > limit:
            if kind == AttachmentKind.TEXT:
                data = data[:limit]
                truncated = True
            else:
                raise AttachmentError(
                    "Obraz jest za duży (%s, limit %s)."
                    % (human_size(len(data)), human_size(limit)))
        attachment = Attachment(
            session_id=session_id, filename=filename, mime_type=mime, kind=kind,
            local_path="", size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            status=AttachmentStatus.READY, truncated=truncated,
            language=mime_util.language_of(filename), payload=data,
            metadata={"source": source})
        if kind == AttachmentKind.TEXT:
            text, text_truncated = self.decode_text(data, self.max_text_bytes)
            attachment.text_preview = text
            attachment.truncated = truncated or text_truncated
        return attachment

    def from_text(self, text: str, filename: str = "schowek.txt",
                  session_id: Optional[int] = None) -> Attachment:
        return self.from_bytes(text.encode("utf-8"), filename, "text/plain",
                               session_id, source="clipboard-text")

    # -------------------------------------------------------------- reading
    @staticmethod
    def _read_head(path: str, size: int) -> bytes:
        try:
            with open(path, "rb") as handle:
                return handle.read(size)
        except OSError as exc:
            raise AttachmentError("Nie można odczytać pliku: %s" % exc)

    @staticmethod
    def _read_bytes(path: str, limit: int) -> bytes:
        chunks: List[bytes] = []
        total = 0
        try:
            with open(path, "rb") as handle:
                while total < limit:
                    block = handle.read(min(CHUNK, limit - total))
                    if not block:
                        break
                    chunks.append(block)
                    total += len(block)
        except OSError as exc:
            raise AttachmentError("Nie można odczytać pliku: %s" % exc)
        return b"".join(chunks)

    @staticmethod
    def decode_text(data: bytes, limit: int) -> Tuple[str, bool]:
        """Decode with fallbacks; returns (text, truncated).

        Binary payloads are never passed through silently: control characters
        are dropped and the result is flagged as truncated/limited so the UI
        can warn the user.
        """
        truncated = False
        if len(data) > limit:
            data = data[:limit]
            truncated = True
        text = None
        for encoding in _TEXT_ENCODINGS:
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return data.decode("utf-8", "replace"), True
        if _looks_binary(text):
            cleaned = "".join(ch for ch in text
                              if ch in "\n\t" or ord(ch) >= 32)
            return cleaned, True
        return text, truncated

    # -------------------------------------------------------------- previews
    def preview_for(self, attachment: Attachment) -> Optional[Any]:
        """Return a scaled QImage preview, or None (no Qt in headless mode)."""
        if attachment.kind != AttachmentKind.IMAGE or not attachment.payload:
            return None
        with self._lock:
            key = attachment.id if attachment.id is not None else \
                hash(attachment.sha256)
            cached = self.preview_cache.get(key)
            if cached is not None:
                return cached
        try:
            from PyQt5.QtGui import QImage
            from PyQt5.QtCore import QBuffer, QIODevice, Qt
        except ImportError:
            return None
        buffer = QBuffer()
        buffer.setData(bytes(attachment.payload))
        buffer.open(QIODevice.ReadOnly)
        reader = QImage()
        # QImageReader is used through QImage.load to keep imports lazy.
        if not reader.load(buffer, None):
            return None
        scaled = reader.scaled(self.preview_max_edge, self.preview_max_edge,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        with self._lock:
            if len(self.preview_cache) > 24:
                self.preview_cache.clear()
            self.preview_cache[key] = scaled
        return scaled

    def clear_previews(self) -> None:
        with self._lock:
            self.preview_cache.clear()

    # ------------------------------------------------------------- lifecycle
    def register(self, attachment: Attachment) -> Attachment:
        with self._lock:
            if attachment.id is not None:
                self._by_id[attachment.id] = attachment
        return attachment

    def get(self, attachment_id: int) -> Optional[Attachment]:
        with self._lock:
            return self._by_id.get(int(attachment_id))

    def release(self, attachment_id: int) -> None:
        """Drop the in-memory payload and preview once persisted/uploaded."""
        with self._lock:
            attachment = self._by_id.get(int(attachment_id))
            if attachment is not None:
                attachment.release_payload()
            key = int(attachment_id)
            self.preview_cache.pop(key, None)

    def release_all(self, ids: Sequence[int]) -> None:
        for attachment_id in ids:
            self.release(attachment_id)

    def describe(self, attachments: Sequence[Attachment]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for attachment in attachments:
            out.append({
                "filename": attachment.filename,
                "kind": attachment.kind,
                "mime_type": attachment.mime_type,
                "size": human_size(attachment.size_bytes),
                "status": attachment.status,
                "truncated": attachment.truncated,
                "error": attachment.error,
                "estimated_tokens": estimate_tokens(attachment.text_preview)
                if attachment.kind == AttachmentKind.TEXT else None,
            })
        return out

    def validate_drop(self, paths: Sequence[str]) -> Tuple[List[str], List[str]]:
        """Split dropped paths into (accepted, rejected-with-reason)."""
        accepted: List[str] = []
        rejected: List[str] = []
        for path in paths:
            if not os.path.isfile(path):
                rejected.append("%s (nie jest plikiem)" % os.path.basename(path))
                continue
            name = os.path.basename(path)
            kind = mime_util.kind_of(name)
            if kind == AttachmentKind.UNSUPPORTED:
                rejected.append("%s (nieobsługiwany typ)" % name)
                continue
            size = os.path.getsize(path)
            if size > self._limit_for(kind):
                rejected.append("%s (za duży: %s)" % (name, human_size(size)))
                continue
            accepted.append(path)
        return accepted, rejected

    def _limit_for(self, kind: str) -> int:
        if kind == AttachmentKind.TEXT:
            return self.max_text_bytes
        if kind == AttachmentKind.IMAGE:
            return self.max_image_bytes
        return self.max_inline_bytes

    def update_limits(self, max_text_bytes: Optional[int] = None,
                      max_image_bytes: Optional[int] = None) -> None:
        if max_text_bytes:
            self.max_text_bytes = int(max_text_bytes)
        if max_image_bytes:
            self.max_image_bytes = int(max_image_bytes)


def _looks_binary(text: str) -> bool:
    sample = text[:2048]
    if not sample:
        return False
    suspicious = sum(1 for ch in sample if ord(ch) < 9 or (13 < ord(ch) < 32))
    return suspicious > len(sample) * 0.02


def read_text_file(path: str, limit: int = 2 * 1024 * 1024) -> Tuple[str, bool]:
    """Convenience helper used by export/import and diagnostics."""
    data = AttachmentManager._read_bytes(path, limit)
    return AttachmentManager.decode_text(data, limit)


def upload_with_progress(callback: Callable[[str], str],
                         attachment: Attachment,
                         progress: Optional[Callable[[float], None]] = None) -> str:
    """Small indirection so tests can inject a fake uploader."""
    return callback(attachment)
