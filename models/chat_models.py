"""Session, context and preset structures."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class StateMode(object):
    """How conversation context reaches the provider."""

    LOCAL = "local"        # reconstruct context from SQLite every turn
    STATEFUL = "stateful"  # continue via previous_interaction_id

    ALL = (LOCAL, STATEFUL)

    LABELS = {
        LOCAL: "Lokalny (baza jest źródłem prawdy)",
        STATEFUL: "Stan po stronie dostawcy (previous_interaction_id)",
    }


class GenerationState(object):
    IDLE = "idle"
    PREPARING = "preparing"
    COUNTING_TOKENS = "counting_tokens"
    UPLOADING = "uploading"
    STREAMING = "streaming"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    BUSY = (PREPARING, COUNTING_TOKENS, UPLOADING, STREAMING, CANCELLING)


class ConnectionState(object):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    ERROR = "error"


@dataclass
class Session:
    """One chat conversation."""

    id: Optional[int] = None
    title: str = "Nowa rozmowa"
    provider_id: str = "gemini"
    model_id: str = ""
    remote_interaction_id: str = ""
    state_mode: str = StateMode.LOCAL
    created_at: int = 0
    updated_at: int = 0
    archived: bool = False
    pinned: bool = False
    system_preset: str = ""
    parent_session_id: Optional[int] = None
    fork_message_id: Optional[int] = None
    draft: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_fork(self) -> bool:
        return self.parent_session_id is not None

    def state_label(self) -> str:
        return StateMode.LABELS.get(self.state_mode, self.state_mode)


@dataclass
class PinnedContext:
    """User-pinned content that must always reach the model."""

    id: Optional[int] = None
    session_id: Optional[int] = None
    label: str = ""
    text: str = ""
    source: str = "manual"
    enabled: bool = True
    tokens: int = 0
    created_at: int = 0


@dataclass
class PromptPreset:
    """Reusable system instruction."""

    id: Optional[int] = None
    name: str = ""
    system_instruction: str = ""
    builtin: bool = False
    enabled: bool = True


@dataclass
class ContextInfo:
    """Snapshot of what will be (or was) sent to the model."""

    model_id: str = ""
    input_limit: int = 0
    estimated_tokens: int = 0
    counted_tokens: Optional[int] = None
    included_messages: int = 0
    excluded_messages: int = 0
    compacted_messages: int = 0
    pinned_items: int = 0
    pinned_tokens: int = 0
    attachment_bytes: int = 0
    attachment_tokens: int = 0
    system_instruction_tokens: int = 0
    state_mode: str = StateMode.LOCAL
    remote_interaction_id: str = ""
    cache_mode: str = "implicit"
    min_cached_tokens: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def percent(self) -> float:
        if not self.input_limit:
            return 0.0
        used = self.counted_tokens if self.counted_tokens is not None \
            else self.estimated_tokens
        return (float(used) / float(self.input_limit)) * 100.0

    @property
    def effective_tokens(self) -> int:
        return self.counted_tokens if self.counted_tokens is not None \
            else self.estimated_tokens

    def over_limit(self, threshold_percent: float) -> bool:
        if not self.input_limit:
            return False
        return self.percent >= threshold_percent


@dataclass
class SearchResult:
    """One search hit (session + optional message)."""

    session_id: int
    session_title: str
    message_id: Optional[int] = None
    role: str = ""
    preview: str = ""
    timestamp: int = 0
    model_name: str = ""
