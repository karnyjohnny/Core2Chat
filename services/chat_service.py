"""Chat orchestration: context build -> provider stream -> persistence.

Race defence (specification §63): every in-flight turn owns a
``request_id`` and the target ``session_id``/``message_id``. Events from any
other request are dropped, so switching chats, cancelling or re-sending can
never write text into the wrong message.
"""

import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject, QThreadPool, pyqtSignal

from core.context_manager import ContextBuildResult, ContextManager
from core.logging_setup import get_logger
from models.attachment_models import Attachment, AttachmentStatus
from models.chat_models import ContextInfo, GenerationState, Session, StateMode
from models.message_models import Message, MessageStatus, Role
from models.provider_models import ProviderError, StreamingEvent, StreamEventType
from models.usage_models import TokenUsage
from services.workers import StreamWorker, TaskWorker

log = get_logger("chat")


@dataclass
class ActiveRequest:
    request_id: str
    session_id: int
    message_id: int
    model_id: str
    worker: StreamWorker
    text: List[str] = field(default_factory=list)
    thought: List[str] = field(default_factory=list)
    interaction_id: str = ""
    status: str = ""
    usage: Optional[TokenUsage] = None
    state: str = GenerationState.STREAMING
    started_at: float = 0.0
    first_chunk_ms: float = -1.0
    interrupted: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def text_value(self) -> str:
        return "".join(self.text)


class ChatService(QObject):
    """The single entry point the GUI uses for generation."""

    state_changed = pyqtSignal(int, str, str)         # session, state, request
    text_delta = pyqtSignal(int, int, str, str)       # session, message, text, request
    thought_delta = pyqtSignal(int, int, str, str)
    media_received = pyqtSignal(int, int, object, str)
    message_finalized = pyqtSignal(int, int, str, str)  # session, message, status, request
    usage_updated = pyqtSignal(int, int, object)      # session, message, TokenUsage
    context_ready = pyqtSignal(int, object)           # session, ContextInfo
    generation_failed = pyqtSignal(int, int, object, str)
    generation_cancelled = pyqtSignal(int, int, str)
    models_refreshed = pyqtSignal(object, str)        # list[ModelInfo], error
    task_result = pyqtSignal(str, str, object)        # kind, request_id, payload
    task_failed = pyqtSignal(str, str, object)        # kind, request_id, error
    task_progress = pyqtSignal(str, str, float)

    def __init__(self, context: Any, parent: Optional[QObject] = None) -> None:
        super(ChatService, self).__init__(parent)
        self.context = context
        self.pool = QThreadPool.globalInstance()
        # Bounded pool: streaming needs one thread, the rest serve model
        # refresh / uploads. More threads would only raise RSS on weak hardware.
        self.pool.setMaxThreadCount(max(2, min(4, os.cpu_count() or 2)))
        self._active: Dict[int, ActiveRequest] = {}
        self._lock = threading.RLock()
        self._task_requests: Dict[str, str] = {}
        self.generation_states: Dict[int, str] = {}

    # ------------------------------------------------------------------ send
    def send_message(self, session: Session, text: str,
                     attachments: Optional[List[Attachment]] = None,
                     system_instruction: str = "",
                     pinned: Optional[List[Any]] = None,
                     history: Optional[List[Message]] = None,
                     model_id: str = "",
                     generation_config: Optional[Dict[str, Any]] = None
                     ) -> Optional[ActiveRequest]:
        """Persist the user turn and start streaming the assistant reply."""
        attachments = attachments or []
        session_id = int(session.id or 0)
        with self._lock:
            if self._active.get(session_id) is not None:
                log.info("chat.send_ignored reason=busy session=%d", session_id)
                return None
        provider = self.context.provider
        if provider is None:
            self.generation_failed.emit(session_id, 0, ProviderError(
                category="UNKNOWN", message_user="Brak skonfigurowanego "
                                                 "dostawcy API.",
                message_debug="no provider registered"), "")
            return None

        model_id = model_id or session.model_id or self.context.settings.model_id
        if not model_id:
            model_id = self._fallback_model_id(provider)
        if not model_id:
            error = ProviderError(
                category="MODEL_UNAVAILABLE", code="no_model",
                message_user="Nie wybrano modelu. Odśwież listę modeli i "
                             "wybierz jeden z nich.",
                message_debug="no model id resolved",
                provider=provider.provider_id)
            self.generation_failed.emit(session_id, 0, error, "")
            log.warning("chat.send_rejected reason=no_model session=%d",
                        session_id)
            return None
        history = history if history is not None else self._load_history(session_id)
        manager = self.context.context_manager(session.state_mode)

        user_message = Message.user_text(text, session_id)
        user_message.content_parts = self._content_parts_from_attachments(
            attachments)
        self.context.messages.insert(user_message)
        for attachment in attachments:
            if attachment.id is not None:
                self.context.attachments_repo.attach_to_message(
                    attachment.id, int(user_message.id or 0))

        assistant = Message.assistant_placeholder(session_id, model_id)
        self.context.messages.insert(assistant)

        result = self._build_context(manager, model_id, user_message, history,
                                     attachments, pinned or [],
                                     system_instruction, session)
        self.context_ready.emit(session_id, result.info)

        if result.info.warnings:
            log.info("chat.context_warnings session=%d n=%d", session_id,
                     len(result.info.warnings))

        store = True if session.state_mode == StateMode.STATEFUL else \
            bool(self.context.settings.store_remote_interactions)
        request = self._start_stream(
            provider=provider, session=session, model_id=model_id,
            message_id=int(assistant.id or 0), parts=result.parts,
            system_instruction=result.system_instruction,
            previous_interaction_id=(session.remote_interaction_id
                                     if session.state_mode == StateMode.STATEFUL
                                     else ""),
            generation_config=generation_config, store=store)
        if request is not None:
            self.context.sessions.touch(session_id)
        return request

    def retry_message(self, session: Session, assistant_message_id: int,
                      system_instruction: str = "",
                      pinned: Optional[List[Any]] = None) -> Optional[ActiveRequest]:
        """Regenerate a failed/cancelled assistant message in place."""
        message = self.context.messages.get(assistant_message_id)
        if message is None:
            return None
        session_id = int(message.session_id or 0)
        with self._lock:
            if self._active.get(session_id) is not None:
                return None
        provider = self.context.provider
        if provider is None:
            return None
        preceding = self._history_before(session_id, assistant_message_id)
        user_message = self._last_user_message(preceding)
        if user_message is None:
            return None
        attachments = [self.context.attachments_repo.get(a_id)
                       for a_id in (user_message.attachments or [])]
        attachments = [a for a in attachments if a is not None]
        manager = self.context.context_manager(session.state_mode)
        result = self._build_context(manager, session.model_id, user_message,
                                     preceding, attachments, pinned or [],
                                     system_instruction, session)
        self.context.messages.update_text(assistant_message_id, "",
                                          MessageStatus.STREAMING)
        model_id = (session.model_id or self.context.settings.model_id
                    or self._fallback_model_id(provider))
        return self._start_stream(
            provider=provider, session=session,
            model_id=model_id,
            message_id=assistant_message_id, parts=result.parts,
            system_instruction=result.system_instruction,
            previous_interaction_id="",
            generation_config=None,
            store=bool(self.context.settings.store_remote_interactions))

    # ------------------------------------------------------------------ stop
    def stop_generation(self, session_id: int) -> bool:
        """Stop immediately from the user's point of view.

        Measured API behaviour (2026-09-29): the socket can only be closed once
        response headers arrive, and a thinking model may keep the server
        silent for several seconds before that. Waiting for the socket would
        freeze the UI, so:

        1. the request is detached (late events are dropped by id),
        2. the worker is told to cancel - it closes the stream in background,
        3. the partial message is finalised *now* as cancelled.
        """
        with self._lock:
            request = self._active.pop(int(session_id), None)
            if request is None:
                return False
            request.state = GenerationState.CANCELLING
        self._set_state(int(session_id), GenerationState.CANCELLING,
                        request.request_id)
        request.worker.cancel()
        partial_chars = len(request.text_value)
        self._finalize(request, MessageStatus.CANCELLED)
        self.generation_cancelled.emit(request.session_id, request.message_id,
                                       request.request_id)
        log.info("chat.stopped session=%d message=%d partial_chars=%d "
                 "interaction=%s", session_id, request.message_id,
                 partial_chars, bool(request.interaction_id))
        provider = self.context.provider
        if provider is not None and request.interaction_id:
            self.run_task("cancel_remote", provider.cancel_request,
                          (request.interaction_id,))
        return True

    def _fallback_model_id(self, provider: Any) -> str:
        """Pick the first discovered chat model when nothing is configured."""
        try:
            models = provider.list_models(force_refresh=False)
        except Exception as exc:
            log.warning("chat.fallback_model_failed err=%s", exc)
            return ""
        if models:
            model_id = models[0].model_id
            self.context.settings.model_id = model_id
            log.info("chat.fallback_model id=%s", model_id)
            return model_id
        return ""

    def is_busy(self, session_id: int) -> bool:
        with self._lock:
            return self._active.get(int(session_id)) is not None

    def active_request(self, session_id: int) -> Optional[ActiveRequest]:
        with self._lock:
            return self._active.get(int(session_id))

    def cancel_all(self) -> int:
        count = 0
        with self._lock:
            requests = list(self._active.values())
        for request in requests:
            request.worker.cancel()
            count += 1
        if count:
            log.info("chat.cancel_all n=%d", count)
        return count

    # ------------------------------------------------------------- streaming
    def _start_stream(self, provider: Any, session: Session, model_id: str,
                      message_id: int, parts: List[Any],
                      system_instruction: str, previous_interaction_id: str,
                      generation_config: Optional[Dict[str, Any]],
                      store: bool) -> Optional[ActiveRequest]:
        import time
        session_id = int(session.id or 0)
        request_id = uuid.uuid4().hex[:16]
        params: Dict[str, Any] = {
            "model_id": model_id,
            "contents": parts,
            "system_instruction": system_instruction,
            "previous_interaction_id": previous_interaction_id,
            "generation_config": generation_config,
            "session_id": session_id,
            "store": store,
        }
        worker = StreamWorker(request_id, provider, params)
        request = ActiveRequest(request_id=request_id, session_id=session_id,
                                message_id=message_id, model_id=model_id,
                                worker=worker, started_at=time.monotonic())
        worker.signals.event.connect(self._on_event)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        worker.signals.finished.connect(self._on_finished)
        with self._lock:
            self._active[session_id] = request
        self.context.messages.set_status(message_id, MessageStatus.STREAMING)
        self._set_state(session_id, GenerationState.STREAMING, request_id)
        self.pool.start(worker)
        log.info("chat.stream_started session=%d message=%d model=%s req=%s",
                 session_id, message_id, model_id, request_id)
        return request

    def _on_event(self, request_id: str, event: StreamingEvent) -> None:
        request = self._find(request_id)
        if request is None:
            return
        kind = event.event_type
        if kind == StreamEventType.CREATED:
            request.interaction_id = event.interaction_id or request.interaction_id
        elif kind == StreamEventType.STATUS:
            request.status = event.status or request.status
        elif kind == StreamEventType.TEXT_DELTA:
            if event.text:
                if request.first_chunk_ms < 0:
                    request.first_chunk_ms = float(
                        event.metadata.get("first_chunk_ms") or 0.0)
                request.text.append(event.text)
                self.text_delta.emit(request.session_id, request.message_id,
                                     event.text, request_id)
        elif kind == StreamEventType.THOUGHT_DELTA:
            if event.text:
                request.thought.append(event.text)
                self.thought_delta.emit(request.session_id, request.message_id,
                                        event.text, request_id)
        elif kind == StreamEventType.MEDIA_DELTA:
            self.media_received.emit(request.session_id, request.message_id,
                                     event.media, request_id)
        elif kind == StreamEventType.COMPLETED:
            request.status = event.status or request.status
            request.interrupted = bool(event.metadata.get("interrupted"))
            usage_payload = event.metadata.get("usage")
            if usage_payload:
                request.usage = TokenUsage(**_usage_kwargs(usage_payload))
            if event.metadata.get("errors"):
                request.errors.extend(str(e) for e in event.metadata["errors"])
            # A completed event may carry the authoritative final text.
            if event.text and len(event.text) >= len(request.text_value):
                request.text = [event.text]
            if event.thought_summary:
                request.thought = [event.thought_summary]
            self._finalize(request, MessageStatus.COMPLETED
                           if request.status in ("completed", "", None)
                           else _status_from_api(request.status))
        elif kind == StreamEventType.USAGE:
            usage_payload = event.metadata.get("usage")
            if usage_payload:
                usage = TokenUsage(**_usage_kwargs(usage_payload))
                request.usage = usage.merged_with(request.usage)
                self.usage_updated.emit(request.session_id, request.message_id,
                                        request.usage)
        elif kind == StreamEventType.UNKNOWN:
            malformed = event.metadata.get("malformed")
            if malformed:
                request.errors.append("niezrozumiały fragment strumienia")
                log.warning("chat.malformed_event req=%s detail=%s",
                            request_id, str(malformed)[:120])

    def _on_failed(self, request_id: str, error: ProviderError) -> None:
        request = self._find(request_id)
        if request is None:
            return
        status = MessageStatus.FAILED
        if error.category == "CANCELLED":
            status = MessageStatus.CANCELLED
        self._finalize(request, status, error=error)
        self.generation_failed.emit(request.session_id, request.message_id,
                                    error, request_id)

    def _on_cancelled(self, request_id: str) -> None:
        """Worker-side cancellation (stream aborted before the UI asked)."""
        request = self._find(request_id)
        if request is None:
            return          # already finalised by stop_generation()
        self._finalize(request, MessageStatus.CANCELLED)
        self.generation_cancelled.emit(request.session_id, request.message_id,
                                       request_id)

    def _on_finished(self, request_id: str) -> None:
        request = None
        with self._lock:
            for session_id, candidate in list(self._active.items()):
                if candidate.request_id == request_id:
                    request = candidate
                    del self._active[session_id]
                    break
        if request is not None and request.state not in (
                GenerationState.COMPLETED, GenerationState.FAILED,
                GenerationState.CANCELLED):
            # Worker ended without a terminal event (e.g. socket closed).
            self._finalize(request, MessageStatus.INTERRUPTED)

    # ----------------------------------------------------------- finalisation
    def _finalize(self, request: ActiveRequest, status: str,
                  error: Optional[ProviderError] = None) -> None:
        import time
        if request.state in (GenerationState.COMPLETED, GenerationState.FAILED,
                             GenerationState.CANCELLED):
            return
        text = request.text_value
        usage = request.usage or TokenUsage()
        thought = "".join(request.thought)
        metadata: Dict[str, Any] = {
            "latency_ms": round((time.monotonic() - request.started_at) * 1000, 1),
            "first_chunk_ms": round(request.first_chunk_ms, 1),
            "interrupted": request.interrupted,
        }
        if error is not None:
            metadata["error"] = error.to_dict()
        if request.errors:
            metadata["stream_warnings"] = request.errors[:3]
        try:
            self.context.messages.finalize(
                request.message_id, text, status, usage,
                interaction_id=request.interaction_id,
                thought_summary=thought, model_name=request.model_id)
            self.context.messages.db.execute(
                "UPDATE messages SET metadata_json=? WHERE id=?",
                (_dumps(metadata), request.message_id))
        except Exception as exc:  # pragma: no cover - persistence failure
            log.error("chat.finalize_failed message=%d err=%s",
                      request.message_id, exc)
        if usage.total_tokens or usage.input_tokens:
            self.context.stats.record(usage, request.session_id,
                                      request.model_id,
                                      provider_id=self.context.settings.provider_id)
            self.usage_updated.emit(request.session_id, request.message_id, usage)
        if request.interaction_id:
            self.context.sessions.set_remote_state(request.session_id,
                                                   request.interaction_id)
        self.context.sessions.touch(request.session_id)
        request.state = (GenerationState.CANCELLED
                         if status == MessageStatus.CANCELLED else
                         GenerationState.FAILED if status == MessageStatus.FAILED
                         else GenerationState.COMPLETED)
        self._set_state(request.session_id, request.state, request.request_id)
        self.message_finalized.emit(request.session_id, request.message_id,
                                    status, request.request_id)
        provider = self.context.provider
        cache_policy = getattr(provider, "cache_policy", None)
        if cache_policy is not None and usage.cached_tokens > 0:
            try:
                cache_policy.observe_usage(usage, None)
            except Exception as exc:  # pragma: no cover - accounting only
                log.debug("chat.cache_observe_failed err=%s", exc)
        log.info("chat.finalized session=%d message=%d status=%s in=%d out=%d "
                 "thought=%d cached=%d ms=%.0f",
                 request.session_id, request.message_id, status,
                 usage.input_tokens, usage.output_tokens, usage.thought_tokens,
                 usage.cached_tokens, metadata["latency_ms"])

    # ----------------------------------------------------------------- tasks
    def run_task(self, kind: str, func: Any, args: tuple = (),
                 kwargs: Optional[Dict[str, Any]] = None,
                 request_id: str = "") -> str:
        """Fire-and-forget background work with progress + result signals."""
        request_id = request_id or uuid.uuid4().hex[:16]
        worker = TaskWorker(request_id, func, args, kwargs)
        self._task_requests[request_id] = kind
        worker.signals.result.connect(self._on_task_result)
        worker.signals.failed.connect(self._on_task_failed)
        worker.signals.progress.connect(self._on_task_progress)
        worker.signals.finished.connect(self._on_task_finished)
        self.pool.start(worker)
        return request_id

    def _on_task_result(self, request_id: str, payload: Any) -> None:
        self.task_result.emit(self._task_requests.get(request_id, ""),
                              request_id, payload)

    def _on_task_failed(self, request_id: str, error: ProviderError) -> None:
        self.task_failed.emit(self._task_requests.get(request_id, ""),
                              request_id, error)

    def _on_task_progress(self, request_id: str, value: float) -> None:
        self.task_progress.emit(self._task_requests.get(request_id, ""),
                                request_id, value)

    def _on_task_finished(self, request_id: str) -> None:
        self._task_requests.pop(request_id, None)

    # ------------------------------------------------------------- model ops
    def refresh_models(self, force: bool = True) -> str:
        provider = self.context.provider
        if provider is None:
            return ""
        settings = self.context.settings

        def _fetch() -> List[Any]:
            models = provider.list_models(
                force_refresh=force,
                include_legacy=settings.allow_legacy_models,
                include_preview=True, chat_only=True)
            return models

        return self.run_task("models.refresh", _fetch)

    def validate_api_key(self) -> str:
        provider = self.context.provider
        if provider is None:
            return ""
        return self.run_task("api.validate", provider.connectivity_check)

    def count_context_async(self, session_id: int, model_id: str,
                            parts: List[Any], system_instruction: str) -> str:
        provider = self.context.provider
        if provider is None:
            return ""
        return self.run_task(
            "tokens.count",
            lambda: provider.count_tokens(model_id, parts, system_instruction))

    # -------------------------------------------------------------- internal
    def _find(self, request_id: str) -> Optional[ActiveRequest]:
        with self._lock:
            for request in self._active.values():
                if request.request_id == request_id:
                    return request
        return None

    def _set_state(self, session_id: int, state: str, request_id: str) -> None:
        self.generation_states[session_id] = state
        self.state_changed.emit(session_id, state, request_id)

    def _load_history(self, session_id: int, limit: int = 400) -> List[Message]:
        rows = self.context.messages.page(session_id, limit=limit)
        return [m for m in rows if m.role in (Role.USER, Role.ASSISTANT)]

    def _history_before(self, session_id: int,
                        message_id: int) -> List[Message]:
        rows = [m for m in self.context.messages.page(session_id, limit=400)
                if m.id is not None and m.id < message_id]
        return [m for m in rows if m.role in (Role.USER, Role.ASSISTANT)]

    @staticmethod
    def _last_user_message(history: List[Message]) -> Optional[Message]:
        for message in reversed(history):
            if message.role == Role.USER:
                return message
        return None

    def _build_context(self, manager: ContextManager, model_id: str,
                       user_message: Message, history: List[Message],
                       attachments: List[Attachment], pinned: List[Any],
                       system_instruction: str,
                       session: Session) -> ContextBuildResult:
        model = None
        provider = self.context.provider
        if provider is not None:
            try:
                model = provider.get_model(model_id)
            except Exception as exc:  # pragma: no cover
                log.warning("chat.get_model_failed err=%s", exc)
        # The just-inserted user message must not be counted twice: the window
        # is built from history *excluding* it.
        prior = [m for m in history if m.id != user_message.id]
        result = manager.build(
            model=model, current_message=user_message, history=prior,
            attachments=attachments, pinned=pinned,
            system_instruction=system_instruction, model_id=model_id,
            remote_interaction_id=session.remote_interaction_id)
        if result.info.warnings and self.context.settings.auto_compact_context:
            budget_warnings = [w for w in result.info.warnings
                               if "przekracza limit" in w]
            attempts = 0
            while budget_warnings and attempts < 3:
                result = manager.shrink(result, prior)
                budget_warnings = [w for w in result.info.warnings
                                   if "przekracza limit" in w]
                attempts += 1
            if budget_warnings:
                log.warning("chat.context_still_over session=%s", session.id)
        return result

    @staticmethod
    def _content_parts_from_attachments(attachments: List[Attachment]) -> List[Any]:
        from models.message_models import ContentPart, ContentPartType
        parts: List[Any] = []
        for attachment in attachments:
            if attachment.kind == "image" and attachment.remote_uri:
                parts.append(ContentPart(type=ContentPartType.IMAGE,
                                         uri=attachment.remote_uri,
                                         mime_type=attachment.mime_type))
        return parts


def _status_from_api(status: str) -> str:
    mapping = {
        "completed": MessageStatus.COMPLETED,
        "incomplete": MessageStatus.INCOMPLETE,
        "cancelled": MessageStatus.CANCELLED,
        "failed": MessageStatus.FAILED,
        "in_progress": MessageStatus.STREAMING,
        "requires_action": MessageStatus.INCOMPLETE,
    }
    return mapping.get(status, MessageStatus.COMPLETED)


def _usage_kwargs(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"input_tokens", "output_tokens", "thought_tokens",
               "cached_tokens", "tool_tokens", "total_tokens"}
    return {k: int(v or 0) for k, v in payload.items() if k in allowed}


def _dumps(payload: Dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
