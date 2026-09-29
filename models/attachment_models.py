"""Attachment structures."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class AttachmentStatus(object):
    QUEUED = "queued"
    READING = "reading"
    READY = "ready"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    STALE = "stale"
    FAILED = "failed"
    REMOVED = "removed"

    USABLE = (READY, UPLOADED)


class AttachmentKind(object):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    UNSUPPORTED = "unsupported"


@dataclass
class Attachment:
    """A file (or clipboard image) attached to a message."""

    id: Optional[int] = None
    session_id: Optional[int] = None
    message_id: Optional[int] = None
    filename: str = ""
    mime_type: str = ""
    kind: str = AttachmentKind.UNSUPPORTED
    local_path: str = ""
    size_bytes: int = 0
    sha256: str = ""
    remote_uri: str = ""
    remote_name: str = ""
    remote_expires_at: str = ""
    created_at: int = 0
    status: str = AttachmentStatus.QUEUED
    truncated: bool = False
    error: str = ""
    language: str = "text"
    # In-memory payload for inline transmission; never persisted to SQLite.
    payload: Optional[bytes] = None
    text_preview: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status in AttachmentStatus.USABLE

    @property
    def has_remote(self) -> bool:
        return bool(self.remote_uri or self.remote_name)

    def release_payload(self) -> None:
        """Drop the in-memory copy once it is no longer needed."""
        self.payload = None

    def describe(self) -> str:
        return "%s (%s, %d B)" % (self.filename, self.mime_type or "?",
                                  self.size_bytes)
