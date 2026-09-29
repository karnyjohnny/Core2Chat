"""Deterministic in-process provider used by GUI/integration tests.

It implements the real :class:`BaseProvider` contract and replays recorded
Interactions API shapes, including unknown event types, failures and
cancellation.
"""

import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from api.base_provider import BaseProvider
from models.message_models import ContentPart
from models.provider_models import (Capability, ModelCapabilities, ModelInfo,
                                    ProviderError, StreamingEvent,
                                    StreamEventType)
from models.usage_models import TokenUsage


def make_model(model_id: str = "gemini-3.8-flash",
               input_limit: int = 1_000_000) -> ModelInfo:
    return ModelInfo(
        model_id=model_id, display_name=model_id.title(),
        input_token_limit=input_limit, output_token_limit=65536,
        supported_methods=["generateContent", "countTokens",
                           "createCachedContent"],
        version="001", thinking=True, lifecycle="operational",
        capabilities=ModelCapabilities(
            {Capability.TEXT_INPUT: True, Capability.STREAMING: True,
             Capability.TOKEN_COUNTING: True,
             Capability.CONVERSATION_STATE: True,
             Capability.CONTEXT_CACHING_IMPLICIT: True}, 4096))


class FakeProvider(BaseProvider):
    """Scriptable provider; no sockets, no cost, fully deterministic."""

    provider_id = "gemini"
    display_name = "Fake Gemini"

    def __init__(self, chunks: Optional[List[str]] = None,
                 delay: float = 0.0, fail_with: Optional[ProviderError] = None,
                 usage: Optional[TokenUsage] = None,
                 models: Optional[List[ModelInfo]] = None,
                 unknown_event: bool = False,
                 emit_thought: bool = False) -> None:
        self.chunks = chunks if chunks is not None else ["Witaj", " ", "świecie"]
        self.delay = float(delay)
        self.fail_with = fail_with
        self.usage = usage or TokenUsage(input_tokens=11, output_tokens=7,
                                         thought_tokens=3, total_tokens=21)
        self.models = models if models is not None else [make_model()]
        self.unknown_event = unknown_event
        self.emit_thought = emit_thought
        self.calls: List[Dict[str, Any]] = []
        self.cancelled_ids: List[str] = []
        self.interaction_counter = 0
        self.count_calls: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- models
    def list_models(self, force_refresh: bool = False, **kwargs) -> List[ModelInfo]:
        with self._lock:
            self.calls.append({"op": "list_models", "force": force_refresh})
        return list(self.models)

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        for model in self.models:
            if model.model_id == model_id:
                return model
        return self.models[0] if self.models else None

    # -------------------------------------------------------------- messaging
    def send_message(self, model_id: str, contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "",
                     generation_config: Optional[Dict[str, Any]] = None,
                     session_id: Optional[int] = None,
                     store: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            self.calls.append({"op": "send", "model": model_id,
                               "contents": len(contents),
                               "system": system_instruction,
                               "previous": previous_interaction_id})
        if self.fail_with is not None:
            raise self.fail_with
        text = "".join(self.chunks)
        return {"text": text, "thought_summary": "", "status": "completed",
                "interaction_id": self._next_id(), "usage": self.usage,
                "media": [], "tool_calls": [], "errors": [], "latency_ms": 1.0,
                "model": model_id}

    def stream_message(self, model_id: str, contents: List[ContentPart],
                       system_instruction: str = "",
                       previous_interaction_id: str = "",
                       generation_config: Optional[Dict[str, Any]] = None,
                       cancel_event: Optional[threading.Event] = None,
                       session_id: Optional[int] = None,
                       store: Optional[bool] = None
                       ) -> Iterator[StreamingEvent]:
        interaction_id = self._next_id()
        with self._lock:
            self.calls.append({
                "op": "stream", "model": model_id,
                "contents": [c.text for c in contents if c.type == "text"],
                "system": system_instruction,
                "previous": previous_interaction_id,
                "generation_config": generation_config or {},
                "session_id": session_id, "store": store,
                "interaction_id": interaction_id})
        yield StreamingEvent(event_type=StreamEventType.STARTED,
                             metadata={"model": model_id})
        yield StreamingEvent(event_type=StreamEventType.CREATED,
                             interaction_id=interaction_id,
                             status="in_progress")
        yield StreamingEvent(event_type=StreamEventType.STATUS,
                             interaction_id=interaction_id, status="in_progress")
        if self.fail_with is not None:
            yield StreamingEvent(event_type=StreamEventType.ERROR,
                                 interaction_id=interaction_id,
                                 metadata={"error": self.fail_with.to_dict()})
            return
        if self.unknown_event:
            yield StreamingEvent(event_type=StreamEventType.UNKNOWN,
                                 interaction_id=interaction_id,
                                 metadata={"event_type": "google.future.thing"})
        yield StreamingEvent(event_type=StreamEventType.STEP_START,
                             interaction_id=interaction_id, step_index=0,
                             step_type="thought")
        if self.emit_thought:
            yield StreamingEvent(event_type=StreamEventType.THOUGHT_DELTA,
                                 interaction_id=interaction_id, step_index=0,
                                 step_type="thought", text="Analizuję pytanie.")
        yield StreamingEvent(event_type=StreamEventType.STEP_STOP,
                             interaction_id=interaction_id, step_index=0)
        yield StreamingEvent(event_type=StreamEventType.STEP_START,
                             interaction_id=interaction_id, step_index=1,
                             step_type="model_output")
        for chunk in self.chunks:
            if cancel_event is not None and cancel_event.is_set():
                yield StreamingEvent(event_type=StreamEventType.CANCELLED,
                                     interaction_id=interaction_id,
                                     status="cancelled")
                return
            if self.delay:
                time.sleep(self.delay)
            yield StreamingEvent(event_type=StreamEventType.TEXT_DELTA,
                                 interaction_id=interaction_id, step_index=1,
                                 step_type="model_output", delta_type="text",
                                 text=chunk)
        yield StreamingEvent(event_type=StreamEventType.STEP_STOP,
                             interaction_id=interaction_id, step_index=1,
                             metadata={"usage": self.usage.to_row()})
        yield StreamingEvent(event_type=StreamEventType.COMPLETED,
                             interaction_id=interaction_id, status="completed",
                             text="".join(self.chunks),
                             thought_summary="Analizuję pytanie."
                             if self.emit_thought else "",
                             metadata={"usage": self.usage.to_row()})
        yield StreamingEvent(event_type=StreamEventType.USAGE,
                             interaction_id=interaction_id,
                             metadata={"usage": self.usage.to_row()})
        yield StreamingEvent(event_type=StreamEventType.DONE)

    def cancel_request(self, interaction_id: str) -> bool:
        with self._lock:
            self.cancelled_ids.append(interaction_id)
        return True

    def count_tokens(self, model_id: str, contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "") -> int:
        with self._lock:
            self.count_calls.append({"model": model_id,
                                     "parts": len(contents),
                                     "system": system_instruction})
        text = "".join(c.text for c in contents) + system_instruction
        return max(1, len(text) // 4)

    def connectivity_check(self) -> str:
        return "online"

    def validate_credentials(self) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            self.calls.append({"op": "close"})

    # -------------------------------------------------------------- internal
    def _next_id(self) -> str:
        with self._lock:
            self.interaction_counter += 1
            return "v1_fake_%d" % self.interaction_counter

    def calls_of(self, op: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("op") == op]
