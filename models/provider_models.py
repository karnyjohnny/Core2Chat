"""Provider-agnostic API data structures.

The GUI depends only on these types - never on raw provider dictionaries.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Capability(object):
    """Optional provider features. Presence must be proven, never guessed."""

    TEXT_INPUT = "text_input"
    IMAGE_INPUT = "image_input"
    PDF_INPUT = "pdf_input"
    FILE_INPUT = "file_input"
    AUDIO_INPUT = "audio_input"
    STREAMING = "streaming"
    TOKEN_COUNTING = "token_counting"
    CONTEXT_CACHING_EXPLICIT = "context_caching_explicit"
    CONTEXT_CACHING_IMPLICIT = "context_caching_implicit"
    CONVERSATION_STATE = "conversation_state"
    SERVER_SIDE_CANCEL = "server_side_cancel"
    THINKING = "thinking"
    THINKING_SUMMARY = "thinking_summary"
    TOOL_CALLS = "tool_calls"
    FUNCTION_CALLING = "function_calling"
    STRUCTURED_OUTPUT = "structured_output"
    IMAGE_OUTPUT = "image_output"
    AUDIO_OUTPUT = "audio_output"

    ALL = (
        TEXT_INPUT, IMAGE_INPUT, PDF_INPUT, FILE_INPUT, AUDIO_INPUT, STREAMING,
        TOKEN_COUNTING, CONTEXT_CACHING_EXPLICIT, CONTEXT_CACHING_IMPLICIT,
        CONVERSATION_STATE, SERVER_SIDE_CANCEL, THINKING, THINKING_SUMMARY,
        TOOL_CALLS, FUNCTION_CALLING, STRUCTURED_OUTPUT, IMAGE_OUTPUT,
        AUDIO_OUTPUT,
    )


@dataclass
class ModelCapabilities:
    """Runtime-derived capability set for one model."""

    flags: Dict[str, bool] = field(default_factory=dict)
    min_cached_tokens: int = 0

    def supports(self, capability: str) -> bool:
        return bool(self.flags.get(capability, False))

    def enabled(self) -> List[str]:
        return sorted(k for k, v in self.flags.items() if v)

    def merge(self, other: "ModelCapabilities") -> "ModelCapabilities":
        merged = dict(self.flags)
        merged.update(other.flags)
        return ModelCapabilities(flags=merged,
                                 min_cached_tokens=max(self.min_cached_tokens,
                                                       other.min_cached_tokens))


class ModelLifecycle(object):
    """Lifecycle classification derived from runtime metadata."""

    OPERATIONAL = "operational"
    PREVIEW = "preview"
    DEPRECATED = "deprecated"
    SHUT_DOWN = "shut_down"
    UNKNOWN = "unknown"


@dataclass
class ModelInfo:
    """Normalised model metadata returned by ``list_models``."""

    model_id: str
    display_name: str = ""
    description: str = ""
    input_token_limit: int = 0
    output_token_limit: int = 0
    supported_methods: List[str] = field(default_factory=list)
    version: str = ""
    base_model_id: str = ""
    thinking: Optional[bool] = None
    lifecycle: str = ModelLifecycle.UNKNOWN
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def resource_name(self) -> str:
        return self.model_id if self.model_id.startswith("models/") \
            else "models/" + self.model_id

    def supports_method(self, method: str) -> bool:
        return method in self.supported_methods


class ErrorCategory(object):
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    FILE_ERROR = "FILE_ERROR"
    CACHE_ERROR = "CACHE_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ProviderError(Exception):
    """Normalised, user-presentable error.

    ``message_user`` is Polish and safe to display. ``message_debug`` may
    contain technical detail but never secrets.
    """

    category: str = ErrorCategory.UNKNOWN
    code: str = ""
    http_status: int = 0
    message_user: str = "Wystąpił nieznany błąd."
    message_debug: str = ""
    retryable: bool = False
    retry_after_seconds: Optional[float] = None
    provider: str = ""
    timestamp: float = 0.0
    raw_reference: str = ""

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message_debug or self.message_user)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return "[%s] %s" % (self.category, self.message_user)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "http_status": self.http_status,
            "message_user": self.message_user,
            "message_debug": self.message_debug,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "provider": self.provider,
            "timestamp": self.timestamp,
        }


class StreamEventType(object):
    """Internal, provider-agnostic streaming event taxonomy."""

    STARTED = "started"
    CREATED = "interaction_created"
    STATUS = "status_update"
    STEP_START = "step_start"
    TEXT_DELTA = "text_delta"
    THOUGHT_DELTA = "thought_delta"
    MEDIA_DELTA = "media_delta"
    METADATA = "metadata"
    TOOL_CALL = "tool_call"
    STEP_STOP = "step_stop"
    USAGE = "usage"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    DONE = "done"
    UNKNOWN = "unknown"


@dataclass
class StreamingEvent:
    """One typed event produced by a stream parser.

    ``raw_data`` is retained only for diagnostics and is bounded by the
    parser; it must never contain credentials.
    """

    event_type: str
    interaction_id: str = ""
    step_index: int = -1
    step_type: str = ""
    delta_type: str = ""
    text: str = ""
    thought_summary: str = ""
    media: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = ""
    raw_data: Optional[Dict[str, Any]] = None
