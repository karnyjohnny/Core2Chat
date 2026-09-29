"""Domain models for Core2Chat (plain dataclasses, no Qt, no I/O)."""

from models.attachment_models import (Attachment, AttachmentKind,
                                      AttachmentStatus)
from models.chat_models import (ConnectionState, ContextInfo, GenerationState,
                                PinnedContext, PromptPreset, SearchResult,
                                Session, StateMode)
from models.message_models import (ContentPart, ContentPartType, Message,
                                   MessageStatus, Role)
from models.provider_models import (Capability, ErrorCategory, ModelCapabilities,
                                    ModelInfo, ModelLifecycle, ProviderError,
                                    StreamingEvent, StreamEventType)
from models.usage_models import TokenStats, TokenUsage

__all__ = [
    "Attachment", "AttachmentKind", "AttachmentStatus",
    "ConnectionState", "ContextInfo", "GenerationState", "PinnedContext",
    "PromptPreset", "SearchResult", "Session", "StateMode",
    "ContentPart", "ContentPartType", "Message", "MessageStatus", "Role",
    "Capability", "ErrorCategory", "ModelCapabilities", "ModelInfo",
    "ModelLifecycle", "ProviderError", "StreamingEvent", "StreamEventType",
    "TokenStats", "TokenUsage",
]
