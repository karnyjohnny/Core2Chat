"""Provider contract used by the Gemini adapter.

Core2Chat is Gemini-only by decision (v0.1.2): the GUI and services talk to
this interface rather than to the network, which keeps the API code testable
(``tests/`` register an in-process fake provider through it) and keeps SQL and
HTTP out of the widgets. It is an internal seam, not a promise of other
vendors.
"""

import abc
from typing import Any, Callable, Dict, Iterator, List, Optional

from models.attachment_models import Attachment
from models.message_models import ContentPart, Message
from models.provider_models import (ModelCapabilities, ModelInfo, ProviderError,
                                    StreamingEvent)
from models.usage_models import TokenUsage


class BaseProvider(abc.ABC):
    """Minimal surface every provider must implement."""

    #: stable identifier used in the database (e.g. "gemini")
    provider_id: str = "base"
    #: human readable name for the UI
    display_name: str = "Provider"

    # ------------------------------------------------------------ models
    @abc.abstractmethod
    def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        """Return runtime-discovered models (never a hand-written list)."""

    @abc.abstractmethod
    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        """Return metadata for one model, or ``None`` when unknown."""

    def get_capabilities(self, model_id: str) -> ModelCapabilities:
        model = self.get_model(model_id)
        return model.capabilities if model else ModelCapabilities()

    def supports(self, model_id: str, capability: str) -> bool:
        return self.get_capabilities(model_id).supports(capability)

    # ------------------------------------------------------------ messaging
    @abc.abstractmethod
    def send_message(self, model_id: str,
                     contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "",
                     generation_config: Optional[Dict[str, Any]] = None,
                     session_id: Optional[int] = None) -> Dict[str, Any]:
        """Unary request. Returns a normalised result dict:

        ``{"text", "thought_summary", "interaction_id", "status", "usage",
        "media", "raw_steps"}``
        """

    @abc.abstractmethod
    def stream_message(self, model_id: str,
                       contents: List[ContentPart],
                       system_instruction: str = "",
                       previous_interaction_id: str = "",
                       generation_config: Optional[Dict[str, Any]] = None,
                       cancel_event: Optional[Any] = None,
                       session_id: Optional[int] = None
                       ) -> Iterator[StreamingEvent]:
        """Yield :class:`StreamingEvent` objects until completion."""

    def cancel_request(self, interaction_id: str) -> bool:
        """Best-effort server-side cancellation. False when unsupported."""
        return False

    # ------------------------------------------------------------ tokens
    def count_tokens(self, model_id: str,
                     contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "") -> int:
        """Return the provider-side token count, or ``-1`` when unsupported."""
        return -1

    # ------------------------------------------------------------ files
    def upload_file(self, attachment: Attachment,
                    progress: Optional[Callable[[float], None]] = None) -> str:
        raise ProviderError(category="FILE_ERROR",
                            message_user="Ten dostawca nie wspiera wysyłania plików.",
                            message_debug="upload_file not supported",
                            provider=self.provider_id)

    def delete_file(self, remote_name: str) -> bool:
        return False

    def list_remote_files(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------ cache
    def create_cache(self, model_id: str, contents: List[ContentPart],
                     ttl_seconds: int = 0) -> Dict[str, Any]:
        raise ProviderError(category="CACHE_ERROR",
                            message_user="Ten dostawca nie wspiera jawnego "
                                         "buforowania kontekstu.",
                            message_debug="create_cache not supported",
                            provider=self.provider_id)

    def delete_cache(self, remote_name: str) -> bool:
        return False

    # ------------------------------------------------------------ lifecycle
    def validate_credentials(self) -> bool:
        """Cheap check that the credential is accepted (no token spend)."""
        try:
            self.list_models(force_refresh=True)
            return True
        except ProviderError:
            return False

    def close(self) -> None:
        """Release sockets/threads. Must be idempotent."""

    # ------------------------------------------------------------ helpers
    @staticmethod
    def text_of(contents: List[ContentPart]) -> str:
        return "".join(part.text for part in contents
                       if part.type == "text" and part.text)

    def describe(self) -> Dict[str, Any]:
        return {"provider_id": self.provider_id,
                "display_name": self.display_name}


class ProviderRegistry(object):
    """Keeps provider instances out of the GUI."""

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        return self._providers.get(provider_id)

    def ids(self) -> List[str]:
        return sorted(self._providers.keys())

    def all(self) -> List[BaseProvider]:
        return [self._providers[key] for key in self.ids()]

    def close_all(self) -> None:
        for provider in self._providers.values():
            try:
                provider.close()
            except Exception:  # pragma: no cover - shutdown must not raise
                pass
        self._providers.clear()


__all__ = ["BaseProvider", "ProviderRegistry", "Message", "TokenUsage"]
