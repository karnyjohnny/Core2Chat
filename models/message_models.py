"""Message and content-part structures.

Messages are multimodal by design: ``text`` is a convenience view over
``content_parts`` so that older text-only code keeps working.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from models.usage_models import TokenUsage


class Role(object):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

    ALL = (USER, ASSISTANT, SYSTEM, TOOL)


class MessageStatus(object):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    EDITED = "edited"

    FINAL = (COMPLETED, INCOMPLETE, FAILED, CANCELLED, INTERRUPTED, EDITED)


class ContentPartType(object):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    THOUGHT_SUMMARY = "thought_summary"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass
class ContentPart:
    """One typed piece of message content (Interactions API ``Content``)."""

    type: str = ContentPartType.TEXT
    text: str = ""
    mime_type: str = ""
    data: bytes = b""
    uri: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_api_dict(self, inline_data: bool = True) -> Dict[str, Any]:
        """Serialise to the Interactions API ``Content`` shape."""
        if self.type == ContentPartType.TEXT:
            return {"type": "text", "text": self.text}
        payload: Dict[str, Any] = {"type": self.type}
        if self.uri:
            payload["uri"] = self.uri
        elif self.data and inline_data:
            payload["data"] = base64.b64encode(self.data).decode("ascii")
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        return payload

    def to_storage_dict(self) -> Dict[str, Any]:
        """Persistable form - never stores raw bytes, only references."""
        out = {"type": self.type}
        if self.text:
            out["text"] = self.text
        if self.mime_type:
            out["mime_type"] = self.mime_type
        if self.uri:
            out["uri"] = self.uri
        if self.data:
            out["has_inline_data"] = True
            out["inline_bytes"] = len(self.data)
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_api_dict(cls, payload: Dict[str, Any]) -> Optional["ContentPart"]:
        if not isinstance(payload, dict):
            return None
        ptype = str(payload.get("type") or ContentPartType.TEXT)
        data_b64 = payload.get("data")
        raw = b""
        if isinstance(data_b64, str) and data_b64:
            try:
                raw = base64.b64decode(data_b64, validate=False)
            except Exception:
                raw = b""
        return cls(
            type=ptype,
            text=str(payload.get("text") or ""),
            mime_type=str(payload.get("mime_type") or ""),
            data=raw,
            uri=str(payload.get("uri") or ""),
            metadata={k: v for k, v in payload.items()
                      if k not in ("type", "text", "data", "uri", "mime_type")},
        )

    @classmethod
    def from_storage_dict(cls, payload: Dict[str, Any]) -> Optional["ContentPart"]:
        if not isinstance(payload, dict):
            return None
        return cls(
            type=str(payload.get("type") or ContentPartType.TEXT),
            text=str(payload.get("text") or ""),
            mime_type=str(payload.get("mime_type") or ""),
            uri=str(payload.get("uri") or ""),
            metadata=payload.get("metadata") or {},
        )


@dataclass
class Message:
    """A single chat message."""

    id: Optional[int] = None
    session_id: Optional[int] = None
    role: str = Role.USER
    text: str = ""
    content_parts: List[ContentPart] = field(default_factory=list)
    attachments: List[int] = field(default_factory=list)
    timestamp: int = 0
    model_name: str = ""
    interaction_id: str = ""
    status: str = MessageStatus.COMPLETED
    usage: TokenUsage = field(default_factory=TokenUsage)
    thought_summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_message_id: Optional[int] = None

    # -------------------------------------------------------------- factories
    @classmethod
    def user_text(cls, text: str, session_id: Optional[int] = None,
                  timestamp: int = 0) -> "Message":
        return cls(session_id=session_id, role=Role.USER, text=text,
                   timestamp=timestamp, status=MessageStatus.COMPLETED)

    @classmethod
    def assistant_placeholder(cls, session_id: Optional[int] = None,
                              model_name: str = "",
                              timestamp: int = 0) -> "Message":
        return cls(session_id=session_id, role=Role.ASSISTANT, text="",
                   model_name=model_name, timestamp=timestamp,
                   status=MessageStatus.STREAMING)

    # ---------------------------------------------------------------- helpers
    @property
    def is_final(self) -> bool:
        return self.status in MessageStatus.FINAL

    def append_text(self, chunk: str) -> None:
        self.text += chunk

    def media_parts(self) -> List[ContentPart]:
        return [p for p in self.content_parts
                if p.type != ContentPartType.TEXT and p.type
                != ContentPartType.TOOL_CALL]

    def content_json(self) -> str:
        if not self.content_parts:
            return ""
        return json.dumps([p.to_storage_dict() for p in self.content_parts],
                          ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def parse_content_json(raw: Optional[str]) -> List[ContentPart]:
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(payload, list):
            return []
        parts = []
        for item in payload:
            part = ContentPart.from_storage_dict(item)
            if part is not None:
                parts.append(part)
        return parts

    def metadata_json(self) -> str:
        if not self.metadata:
            return ""
        return json.dumps(self.metadata, ensure_ascii=False,
                          separators=(",", ":"))
