"""Context assembly, trimming and token budgeting (specification §10-§12).

Priority order that is always respected:

1. system/developer instruction (sent as ``system_instruction``, never trimmed)
2. explicitly pinned context
3. the current user message (+ its attachments) - never dropped
4. recent conversation (sliding window)
5. older conversation (dropped or compacted, always reported)
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.logging_setup import get_logger
from models.attachment_models import Attachment, AttachmentKind
from models.chat_models import ContextInfo, PinnedContext, StateMode
from models.message_models import ContentPart, ContentPartType, Message, Role
from models.provider_models import ModelInfo
from utils.text import estimate_tokens

log = get_logger("context")

COMPACT_NOTICE = ("[Kontekst skrócony: %d wcześniejszych wiadomości nie "
                  "zostało wysłanych do modelu.]")


@dataclass
class ContextBuildResult:
    """Everything the caller needs to send one turn."""

    parts: List[ContentPart] = field(default_factory=list)
    system_instruction: str = ""
    info: ContextInfo = field(default_factory=ContextInfo)
    included_messages: List[int] = field(default_factory=list)
    dropped_messages: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def static_parts(self) -> List[ContentPart]:
        """Prefix that stays identical across turns (cache-friendly)."""
        return [p for p in self.parts if p.metadata.get("static")]


class ContextManager(object):
    """Turns local history into a bounded, cache-friendly provider input."""

    def __init__(self, limit_percent: int = 85,
                 sliding_window: int = 40,
                 auto_compact: bool = True,
                 count_before_send: bool = True,
                 state_mode: str = StateMode.LOCAL,
                 token_counter: Optional[Callable[[str, List[ContentPart], str], int]] = None,
                 min_cached_tokens: int = 4096,
                 max_attachment_chars: int = 200_000) -> None:
        self.limit_percent = max(10, min(100, int(limit_percent)))
        self.sliding_window = max(2, int(sliding_window))
        self.auto_compact = bool(auto_compact)
        self.count_before_send = bool(count_before_send)
        self.state_mode = state_mode
        self._count = token_counter
        self.min_cached_tokens = int(min_cached_tokens)
        self.max_attachment_chars = int(max_attachment_chars)
        self.last_result: Optional[ContextBuildResult] = None

    # ------------------------------------------------------------ public API
    def build(self, model: Optional[ModelInfo],
              current_message: Message,
              history: Sequence[Message],
              attachments: Sequence[Attachment] = (),
              pinned: Sequence[PinnedContext] = (),
              system_instruction: str = "",
              model_id: str = "",
              remote_interaction_id: str = "") -> ContextBuildResult:
        """Assemble the request context.

        In ``STATEFUL`` mode the provider keeps the transcript, so only the
        current turn is sent; local history is still the durable record.
        """
        model_id = model_id or (model.model_id if model else "")
        input_limit = int(model.input_token_limit) if model else 0
        info = ContextInfo(model_id=model_id, input_limit=input_limit,
                           state_mode=self.state_mode,
                           remote_interaction_id=remote_interaction_id,
                           cache_mode="implicit",
                           min_cached_tokens=self.min_cached_tokens)

        pinned_parts, pinned_tokens = self._pinned_parts(pinned)
        info.pinned_items = len([p for p in pinned if p.enabled])
        info.pinned_tokens = pinned_tokens

        system_instruction = (system_instruction or "").strip()
        info.system_instruction_tokens = estimate_tokens(system_instruction)
        info.min_cached_tokens = self.min_cached_tokens
        if model is not None and model.capabilities.min_cached_tokens:
            # The provider's own floor wins over the configured default.
            info.min_cached_tokens = model.capabilities.min_cached_tokens

        attachment_parts, attachment_bytes, attachment_tokens, truncated = \
            self._attachment_parts(attachments)
        info.attachment_bytes = attachment_bytes
        info.attachment_tokens = attachment_tokens
        if truncated:
            info.warnings.append(
                "Załącznik został przycięty do limitu %d znaków."
                % self.max_attachment_chars)

        current_parts = self._current_parts(current_message, attachment_parts)

        stateful = (self.state_mode == StateMode.STATEFUL
                    and bool(remote_interaction_id))
        included: List[int] = []
        dropped: List[int] = []
        if stateful:
            parts = pinned_parts + current_parts
            info.included_messages = 1
            info.excluded_messages = len(history)
            info.warnings.append(
                "Tryb stanowy: historia jest kontynuowana po stronie "
                "dostawcy (previous_interaction_id).")
        else:
            parts, included, dropped = self._window(history, pinned_parts,
                                                    current_parts)
            info.included_messages = len(included)
            info.excluded_messages = len(dropped)

        result = ContextBuildResult(
            parts=parts, system_instruction=system_instruction, info=info,
            included_messages=list(included), dropped_messages=list(dropped),
            warnings=list(info.warnings))

        if result.dropped_messages and self.auto_compact:
            notice = COMPACT_NOTICE % len(result.dropped_messages)
            # Inserted *after* the static prefix so that implicit caching can
            # still match the unchanged leading content.
            insert_at = len([p for p in result.parts
                             if p.metadata.get("static")])
            result.parts.insert(insert_at, ContentPart(
                type=ContentPartType.TEXT, text=notice,
                metadata={"static": False, "compaction": True}))
            info.compacted_messages = len(result.dropped_messages)
            result.warnings.append(
                "Kontekst skrócony: %d starszych wiadomości zostało "
                "pominiętych." % len(result.dropped_messages))

        result.info = self._measure(model_id, result, info)
        self.last_result = result
        return result

    def check_budget(self, info: ContextInfo,
                     limit_percent: Optional[int] = None) -> List[str]:
        """Return warnings when the context is close to (or over) the limit."""
        percent = self.limit_percent if limit_percent is None else limit_percent
        warnings: List[str] = []
        if not info.input_limit:
            return warnings
        used = info.effective_tokens
        if used > info.input_limit:
            warnings.append(
                "Kontekst (%s tok.) przekracza limit modelu (%s tok.)."
                % (used, info.input_limit))
        elif info.percent >= percent:
            warnings.append(
                "Kontekst zajmuje %.1f%% limitu modelu (próg %d%%)."
                % (info.percent, percent))
        elif info.percent >= percent * 0.8:
            warnings.append(
                "Kontekst zajmuje %.1f%% limitu modelu." % info.percent)
        return warnings

    def shrink(self, result: ContextBuildResult,
               history: Sequence[Message], drop_ratio: float = 0.4
               ) -> ContextBuildResult:
        """Rebuild the context with a shorter window.

        Drops the *oldest* part of the history only: pinned context, the
        current user message and its attachments always survive.
        """
        ratio = max(0.05, min(0.9, float(drop_ratio)))
        keep = max(1, int(len(history) * (1.0 - ratio)))
        recent = list(history)[-keep:]
        dropped = [m.id for m in list(history)[:-keep] if m.id is not None]

        static_parts = [p for p in result.parts if p.metadata.get("static")]
        current_parts = [p for p in result.parts
                         if p.metadata.get("current")
                         or p.metadata.get("attachment")]
        history_parts, included = self._history_parts(recent)

        parts = list(static_parts)
        dropped_ids = sorted(set(result.dropped_messages + dropped))
        if dropped_ids:
            parts.append(ContentPart(
                type=ContentPartType.TEXT,
                text=COMPACT_NOTICE % len(dropped_ids),
                metadata={"static": False, "compaction": True}))
        parts.extend(history_parts)
        parts.extend(current_parts)

        # A *new* result is returned: the previous one stays valid for the
        # caller's diagnostics and for the pre-flight retry loop.
        info = ContextInfo(
            model_id=result.info.model_id, input_limit=result.info.input_limit,
            included_messages=len(included), excluded_messages=len(dropped),
            compacted_messages=len(dropped_ids),
            pinned_items=result.info.pinned_items,
            pinned_tokens=result.info.pinned_tokens,
            attachment_bytes=result.info.attachment_bytes,
            attachment_tokens=result.info.attachment_tokens,
            system_instruction_tokens=result.info.system_instruction_tokens,
            state_mode=result.info.state_mode,
            remote_interaction_id=result.info.remote_interaction_id,
            cache_mode=result.info.cache_mode,
            min_cached_tokens=result.info.min_cached_tokens)
        shrunk = ContextBuildResult(
            parts=parts, system_instruction=result.system_instruction,
            info=info, included_messages=list(included),
            dropped_messages=dropped_ids,
            warnings=[w for w in result.warnings if "zajmuje" not in w
                      and "przekracza" not in w])
        notice = ("Kontekst skrócony: %d starszych wiadomości zostało "
                  "usuniętych." % len(dropped))
        if notice not in shrunk.warnings:
            shrunk.warnings.append(notice)
        shrunk.info = self._measure(info.model_id, shrunk, info)
        log.info("context.shrunk keep=%d dropped=%d tokens=%s", keep,
                 len(dropped), shrunk.info.effective_tokens)
        return shrunk

    # ------------------------------------------------------------- internals
    def _pinned_parts(self, pinned: Sequence[PinnedContext]
                      ) -> Tuple[List[ContentPart], int]:
        parts: List[ContentPart] = []
        tokens = 0
        for item in pinned:
            if not item.enabled or not item.text:
                continue
            text = item.text
            if len(text) > self.max_attachment_chars:
                text = text[:self.max_attachment_chars]
            label = item.label or "Kontekst przypięty"
            part = ContentPart(type=ContentPartType.TEXT,
                               text="### %s\n%s" % (label, text),
                               metadata={"static": True, "pinned": True,
                                         "pinned_id": item.id})
            parts.append(part)
            tokens += item.tokens or estimate_tokens(part.text)
        return parts, tokens

    def _attachment_parts(self, attachments: Sequence[Attachment]
                          ) -> Tuple[List[ContentPart], int, int, bool]:
        parts: List[ContentPart] = []
        total_bytes = 0
        total_tokens = 0
        truncated = False
        for attachment in attachments:
            if not attachment.is_ready:
                continue
            total_bytes += int(attachment.size_bytes or 0)
            if attachment.kind == AttachmentKind.TEXT:
                text = ""
                if attachment.payload:
                    text = attachment.payload.decode("utf-8", "replace")
                elif attachment.text_preview:
                    text = attachment.text_preview
                if len(text) > self.max_attachment_chars:
                    text = text[:self.max_attachment_chars]
                    truncated = True
                header = "### Załączony plik: %s (%s)" % (
                    attachment.filename, attachment.mime_type or "text/plain")
                part = ContentPart(
                    type=ContentPartType.TEXT,
                    text="%s\n```\n%s\n```" % (header, text),
                    metadata={"attachment_id": attachment.id,
                              "filename": attachment.filename,
                              "static": False, "attachment": True})
                total_tokens += estimate_tokens(part.text)
                parts.append(part)
            elif attachment.kind == AttachmentKind.IMAGE:
                if attachment.payload:
                    parts.append(ContentPart(
                        type=ContentPartType.IMAGE, data=attachment.payload,
                        mime_type=attachment.mime_type or "image/png",
                        metadata={"attachment_id": attachment.id,
                                  "filename": attachment.filename,
                                  "attachment": True}))
                elif attachment.remote_uri:
                    parts.append(ContentPart(
                        type=ContentPartType.IMAGE, uri=attachment.remote_uri,
                        mime_type=attachment.mime_type or "image/png",
                        metadata={"attachment_id": attachment.id,
                                  "filename": attachment.filename,
                                  "attachment": True}))
                total_tokens += 1089  # measured cost of one small image input
            elif attachment.kind == AttachmentKind.DOCUMENT:
                if attachment.payload:
                    parts.append(ContentPart(
                        type=ContentPartType.DOCUMENT,
                        data=attachment.payload,
                        mime_type=attachment.mime_type or "application/pdf",
                        metadata={"attachment_id": attachment.id,
                                  "filename": attachment.filename,
                                  "attachment": True}))
                elif attachment.remote_uri:
                    parts.append(ContentPart(
                        type=ContentPartType.DOCUMENT,
                        uri=attachment.remote_uri,
                        mime_type=attachment.mime_type or "application/pdf",
                        metadata={"attachment_id": attachment.id,
                                  "filename": attachment.filename,
                                  "attachment": True}))
            else:
                log.info("context.attachment_skipped kind=%s file=%s",
                         attachment.kind, attachment.filename)
        return parts, total_bytes, total_tokens, truncated

    def _current_parts(self, message: Message,
                       attachment_parts: List[ContentPart]) -> List[ContentPart]:
        parts: List[ContentPart] = []
        for part in message.content_parts:
            if part.type == ContentPartType.TEXT and part.text:
                parts.append(ContentPart(type=ContentPartType.TEXT,
                                         text=part.text,
                                         metadata={"current": True}))
            elif part.type != ContentPartType.TEXT:
                parts.append(part)
        if not parts and message.text:
            parts.append(ContentPart(type=ContentPartType.TEXT,
                                     text=message.text,
                                     metadata={"current": True}))
        for part in parts:
            part.metadata["current"] = True
        # Attachments follow the prompt so that the instruction stays first.
        return parts + attachment_parts

    def _window(self, history: Sequence[Message],
                pinned_parts: List[ContentPart],
                current_parts: List[ContentPart]
                ) -> Tuple[List[ContentPart], List[int], List[int]]:
        """Sliding window over history; newest messages always win."""
        ordered = [m for m in history if m.text or m.content_parts]
        window = ordered[-self.sliding_window:] if self.sliding_window else []
        dropped = ordered[:max(0, len(ordered) - len(window))]
        history_parts, included = self._history_parts(window)
        parts = list(pinned_parts) + history_parts + list(current_parts)
        dropped_ids = [m.id for m in dropped if m.id is not None]
        return parts, included, dropped_ids

    def _history_parts(self, messages: Sequence[Message]
                       ) -> Tuple[List[ContentPart], List[int]]:
        """Format history messages as labelled text parts."""
        parts: List[ContentPart] = []
        included: List[int] = []
        for message in messages:
            text = message.text
            if not text:
                text = "".join(p.text for p in message.content_parts
                               if p.type == ContentPartType.TEXT)
            if not text:
                continue
            label = "User" if (message.role or Role.USER) == Role.USER \
                else "Assistant"
            parts.append(ContentPart(
                type=ContentPartType.TEXT, text="%s: %s" % (label, text),
                metadata={"history": True, "message_id": message.id,
                          "role": message.role or Role.USER}))
            if message.id is not None:
                included.append(message.id)
        return parts, included

    def _measure(self, model_id: str, result: ContextBuildResult,
                 info: ContextInfo) -> ContextInfo:
        estimated = sum(estimate_tokens(p.text) for p in result.parts
                        if p.type == ContentPartType.TEXT)
        estimated += info.system_instruction_tokens
        estimated += sum(1089 for p in result.parts
                         if p.type == ContentPartType.IMAGE)
        info.estimated_tokens = estimated
        if self.count_before_send and self._count is not None and model_id:
            try:
                counted = self._count(model_id, result.parts,
                                      result.system_instruction)
                if counted and counted > 0:
                    info.counted_tokens = int(counted)
            except Exception as exc:
                info.warnings.append(
                    "Zliczanie tokenów przez API nie powiodło się - użyto "
                    "szacunku lokalnego.")
                log.warning("context.count_failed err=%s", exc)
        info.warnings.extend(self.check_budget(info))
        return info


def summarize_context_info(info: ContextInfo) -> Dict[str, Any]:
    """Compact dict for the context inspector dialog."""
    return {
        "model_id": info.model_id,
        "input_limit": info.input_limit,
        "counted_tokens": info.counted_tokens,
        "estimated_tokens": info.estimated_tokens,
        "percent": round(info.percent, 2),
        "included_messages": info.included_messages,
        "excluded_messages": info.excluded_messages,
        "compacted_messages": info.compacted_messages,
        "pinned_items": info.pinned_items,
        "pinned_tokens": info.pinned_tokens,
        "attachment_bytes": info.attachment_bytes,
        "attachment_tokens": info.attachment_tokens,
        "state_mode": info.state_mode,
        "remote_interaction_id": info.remote_interaction_id,
        "cache_mode": info.cache_mode,
        "min_cached_tokens": info.min_cached_tokens,
        "system_instruction_tokens": info.system_instruction_tokens,
        "warnings": list(info.warnings),
    }
