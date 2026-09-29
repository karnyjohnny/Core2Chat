"""Gemini Interactions API: request building, response and SSE parsing.

Verified against the live API and the official OpenAPI specification on
2026-09-29:

* ``POST /v1beta/interactions`` with ``{"model", "input", "stream"}``
* unary response: ``{id, status, steps[], usage, model, created, updated}``
* streaming response: ``text/event-stream`` with ``event:``/``data:`` pairs,
  event types ``interaction.created``, ``interaction.status_update``,
  ``step.start``, ``step.delta``, ``step.stop``, ``interaction.completed``,
  ``error`` and a terminal ``event: done`` / ``data: [DONE]``
* step types observed: ``thought``, ``model_output`` (plus tool steps)
* delta types observed: ``text``, ``thought_signature``
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from core.logging_setup import get_logger
from models.message_models import ContentPart, ContentPartType
from models.provider_models import StreamingEvent, StreamEventType
from models.usage_models import TokenUsage

log = get_logger("gemini.interactions")

# Bounded retention for raw diagnostics - never keep whole streams in memory.
MAX_RAW_EVENT_BYTES = 4096
MAX_STEP_BYTES = 32 * 1024

DONE_SENTINEL = "[DONE]"

EVENT_INTERACTION_CREATED = "interaction.created"
EVENT_STATUS_UPDATE = "interaction.status_update"
EVENT_STEP_START = "step.start"
EVENT_STEP_DELTA = "step.delta"
EVENT_STEP_STOP = "step.stop"
EVENT_COMPLETED = "interaction.completed"
EVENT_ERROR = "error"
EVENT_DONE = "done"

STEP_MODEL_OUTPUT = "model_output"
STEP_THOUGHT = "thought"

STATUS_COMPLETED = "completed"
STATUS_INCOMPLETE = "incomplete"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_IN_PROGRESS = "in_progress"
STATUS_REQUIRES_ACTION = "requires_action"

FINAL_STATUSES = (STATUS_COMPLETED, STATUS_INCOMPLETE, STATUS_CANCELLED,
                  STATUS_FAILED)


# --------------------------------------------------------------- request build
def build_request(model_id: str,
                  contents: List[ContentPart],
                  system_instruction: str = "",
                  previous_interaction_id: str = "",
                  generation_config: Optional[Dict[str, Any]] = None,
                  stream: bool = False,
                  store: Optional[bool] = None,
                  cached_content: str = "",
                  tools: Optional[List[Dict[str, Any]]] = None,
                  response_modalities: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build the ``ModelInteraction`` body.

    Only fields with a real value are emitted: the API rejects unknown
    parameters, and deprecated sampling parameters must not be sent blindly
    (specification §84).
    """
    body: Dict[str, Any] = {"model": model_id}
    body["input"] = encode_input(contents)
    if system_instruction:
        body["system_instruction"] = system_instruction
    if previous_interaction_id:
        body["previous_interaction_id"] = previous_interaction_id
    if stream:
        body["stream"] = True
    if store is not None:
        body["store"] = bool(store)
    if cached_content:
        body["cached_content"] = cached_content
    if tools:
        body["tools"] = tools
    if response_modalities:
        body["response_modalities"] = list(response_modalities)
    config = sanitize_generation_config(generation_config)
    if config:
        body["generation_config"] = config
    return body


#: generation_config keys accepted by the current API (from the OpenAPI spec).
ALLOWED_GENERATION_KEYS = frozenset({
    "max_output_tokens", "stop_sequences", "seed", "thinking_level",
    "thinking_summaries", "tool_choice", "image_config", "speech_config",
    "transcription_config", "video_config",
})
ALLOWED_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})
ALLOWED_THINKING_SUMMARIES = frozenset({"auto", "none"})


def sanitize_generation_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop unsupported/empty parameters instead of sending them blindly."""
    out: Dict[str, Any] = {}
    if not config:
        return out
    for key, value in config.items():
        if key not in ALLOWED_GENERATION_KEYS:
            log.debug("interactions.drop_param key=%s", key)
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "max_output_tokens":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
        if key == "thinking_level" and value not in ALLOWED_THINKING_LEVELS:
            continue
        if key == "thinking_summaries" and value not in ALLOWED_THINKING_SUMMARIES:
            continue
        out[key] = value
    return out


def encode_input(contents: List[ContentPart]) -> Any:
    """Encode content parts into the Interactions ``input`` field.

    A single text part degrades to a plain string (smallest payload);
    anything multimodal becomes an array of ``Content`` objects.
    """
    if not contents:
        return ""
    if len(contents) == 1 and contents[0].type == ContentPartType.TEXT:
        return contents[0].text
    payload: List[Dict[str, Any]] = []
    for part in contents:
        payload.append(part.to_api_dict())
    return payload


def encode_history(history: List[Tuple[str, str]]) -> List[ContentPart]:
    """Helper for local-context mode: (role, text) pairs -> content parts."""
    parts: List[ContentPart] = []
    for role, text in history:
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        parts.append(ContentPart(type=ContentPartType.TEXT,
                                 text="%s: %s" % (label, text)))
    return parts


# ------------------------------------------------------------- unary response
@dataclass
class InteractionResult:
    """Normalised result of a unary interaction."""

    text: str = ""
    thought_summary: str = ""
    interaction_id: str = ""
    status: str = ""
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    media: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    steps_summary: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_COMPLETED, STATUS_INCOMPLETE)


def extract_step_text(step: Dict[str, Any]) -> str:
    """Concatenate text content of a ``model_output`` step."""
    if not isinstance(step, dict):
        return ""
    content = step.get("content")
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def extract_step_media(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull non-text content (images/audio/documents) out of a step."""
    out: List[Dict[str, Any]] = []
    content = step.get("content") if isinstance(step, dict) else None
    if not isinstance(content, list):
        return out
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in ("text", None):
            continue
        out.append({
            "type": str(itype),
            "mime_type": str(item.get("mime_type") or ""),
            "uri": str(item.get("uri") or ""),
            "has_data": bool(item.get("data")),
            "bytes": len(item.get("data") or "") if item.get("data") else 0,
        })
    return out


def extract_thought_summary(step: Dict[str, Any]) -> str:
    if not isinstance(step, dict):
        return ""
    summary = step.get("summary")
    if isinstance(summary, list):
        chunks = []
        for item in summary:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    if isinstance(summary, str):
        return summary
    return ""


def parse_interaction(payload: Any) -> InteractionResult:
    """Parse a unary interaction response defensively."""
    result = InteractionResult()
    if not isinstance(payload, dict):
        result.errors.append("nieprawidłowa odpowiedź API")
        return result
    result.interaction_id = str(payload.get("id") or "")
    result.status = str(payload.get("status") or "")
    result.model = str(payload.get("model") or "")
    result.usage = TokenUsage.from_api(payload.get("usage"))

    steps = payload.get("steps")
    if isinstance(steps, list):
        texts: List[str] = []
        thoughts: List[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            stype = str(step.get("type") or "")
            result.steps_summary.append(stype)
            if stype == STEP_MODEL_OUTPUT:
                text = extract_step_text(step)
                if text:
                    texts.append(text)
                result.media.extend(extract_step_media(step))
            elif stype == STEP_THOUGHT:
                summary = extract_thought_summary(step)
                if summary:
                    thoughts.append(summary)
            elif stype.endswith("_call"):
                result.tool_calls.append({"type": stype, "step": _bounded(step)})
            error = step.get("error")
            if isinstance(error, dict) and error.get("message"):
                result.errors.append(str(error.get("message"))[:300])
        result.text = "".join(texts)
        result.thought_summary = "\n".join(thoughts)

    api_errors = payload.get("errors")
    if isinstance(api_errors, list):
        for item in api_errors:
            if isinstance(item, dict) and item.get("message"):
                result.errors.append(str(item["message"])[:300])
    return result


def _bounded(payload: Any) -> Any:
    """JSON-safe copy bounded to MAX_STEP_BYTES (diagnostics only)."""
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}
    if len(raw) > MAX_STEP_BYTES:
        return {"truncated": True, "preview": raw[:MAX_STEP_BYTES]}
    return payload


# ------------------------------------------------------------------ SSE parser
@dataclass
class SseFrame:
    """One complete server-sent event."""

    event: str = ""
    data: str = ""
    id: str = ""
    retry: Optional[int] = None


class SseParser(object):
    """Incremental, allocation-light SSE parser.

    Feed it raw bytes (or decoded lines) and it yields complete frames.
    Malformed frames are reported once and skipped - a new Google event type
    must never crash the client (specification §8).
    """

    def __init__(self, max_data_bytes: int = 2 * 1024 * 1024) -> None:
        self._buffer = b""
        self._event = ""
        self._data: List[str] = []
        self._id = ""
        self._retry: Optional[int] = None
        self._max_data = int(max_data_bytes)
        self._data_bytes = 0
        self.malformed = 0

    # -------------------------------------------------------------- feeding
    def feed(self, chunk: bytes) -> Iterator[SseFrame]:
        if not chunk:
            return
        self._buffer += chunk
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1:]
            if line.endswith(b"\r"):
                line = line[:-1]
            frame = self._consume_line(line.decode("utf-8", "replace"))
            if frame is not None:
                yield frame

    def feed_lines(self, lines: Iterator[str]) -> Iterator[SseFrame]:
        """Convenience wrapper for ``httpx.Response.iter_lines()``."""
        for line in lines:
            frame = self._consume_line(line)
            if frame is not None:
                yield frame

    def flush(self) -> Optional[SseFrame]:
        """Emit a trailing frame when the stream ended without a blank line."""
        if self._buffer:
            tail = self._buffer.decode("utf-8", "replace")
            self._buffer = b""
            self._consume_line(tail)
        return self._dispatch()

    # -------------------------------------------------------------- internal
    def _consume_line(self, line: str) -> Optional[SseFrame]:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # SSE comment / keep-alive
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        name = field.strip().lower()
        if name == "event":
            self._event = value.strip()
        elif name == "data":
            if self._data_bytes + len(value) <= self._max_data:
                self._data.append(value)
                self._data_bytes += len(value)
            else:
                # Bound memory: drop the frame instead of buffering it.
                self.malformed += 1
                self._data = []
                self._data_bytes = 0
                self._event = ""
                log.warning("sse.oversized_frame_dropped limit=%d", self._max_data)
        elif name == "id":
            self._id = value.strip()
        elif name == "retry":
            try:
                self._retry = int(value)
            except ValueError:
                self._retry = None
        return None

    def _dispatch(self) -> Optional[SseFrame]:
        if not self._data and not self._event:
            self._reset()
            return None
        frame = SseFrame(event=self._event, data="\n".join(self._data),
                         id=self._id, retry=self._retry)
        self._reset()
        return frame

    def _reset(self) -> None:
        self._event = ""
        self._data = []
        self._data_bytes = 0
        self._id = ""
        self._retry = None


def parse_frame_json(frame: SseFrame) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (payload, error). Handles ``data: [DONE]`` and bad JSON."""
    data = (frame.data or "").strip()
    if not data:
        return None, ""
    if data == DONE_SENTINEL:
        return None, ""
    try:
        payload = json.loads(data)
    except ValueError as exc:
        return None, "malformed SSE JSON: %s (%s)" % (exc, data[:120])
    if isinstance(payload, dict):
        # The OpenAPI envelope may wrap the event as {"data": {...}}.
        inner = payload.get("data")
        if isinstance(inner, dict) and "event_type" in inner \
                and "event_type" not in payload:
            return inner, ""
        return payload, ""
    return None, "unexpected SSE payload type: %s" % type(payload).__name__


def event_type_of(frame: SseFrame, payload: Optional[Dict[str, Any]]) -> str:
    """Prefer the JSON ``event_type``; fall back to the SSE ``event:`` field."""
    if isinstance(payload, dict):
        value = payload.get("event_type")
        if isinstance(value, str) and value:
            return value
    name = (frame.event or "").strip()
    return name


def translate(frame: SseFrame, payload: Optional[Dict[str, Any]],
              parse_error: str = "") -> StreamingEvent:
    """Map one SSE frame onto the internal typed event representation."""
    if parse_error:
        log.warning("sse.malformed detail=%s", parse_error[:200])
        return StreamingEvent(event_type=StreamEventType.UNKNOWN,
                              metadata={"malformed": parse_error[:200]})
    name = event_type_of(frame, payload)
    payload = payload if isinstance(payload, dict) else {}

    if name == EVENT_DONE or (frame.data or "").strip() == DONE_SENTINEL:
        return StreamingEvent(event_type=StreamEventType.DONE, raw_data=None)

    if name == EVENT_INTERACTION_CREATED:
        interaction = payload.get("interaction") or {}
        return StreamingEvent(
            event_type=StreamEventType.CREATED,
            interaction_id=str(interaction.get("id") or ""),
            status=str(interaction.get("status") or ""),
            metadata={"model": str(interaction.get("model") or "")})

    if name == EVENT_STATUS_UPDATE:
        return StreamingEvent(
            event_type=StreamEventType.STATUS,
            interaction_id=str(payload.get("interaction_id") or ""),
            status=str(payload.get("status") or ""))

    if name == EVENT_STEP_START:
        step = payload.get("step") or {}
        return StreamingEvent(
            event_type=StreamEventType.STEP_START,
            step_index=int(payload.get("index", -1) or -1),
            step_type=str(step.get("type") or ""),
            interaction_id=str(payload.get("interaction_id") or ""),
            metadata={"event_id": str(payload.get("event_id") or "")})

    if name == EVENT_STEP_DELTA:
        return _translate_delta(payload)

    if name == EVENT_STEP_STOP:
        usage = TokenUsage.from_api(payload.get("usage"))
        step_usage = TokenUsage.from_api(payload.get("step_usage"))
        return StreamingEvent(
            event_type=StreamEventType.STEP_STOP,
            step_index=int(payload.get("index", -1) or -1),
            interaction_id=str(payload.get("interaction_id") or ""),
            metadata={"usage": usage.to_row(),
                      "step_usage": step_usage.to_row(),
                      "event_id": str(payload.get("event_id") or "")})

    if name == EVENT_COMPLETED:
        interaction = payload.get("interaction") or {}
        usage = TokenUsage.from_api(interaction.get("usage"))
        result = parse_interaction(interaction) if interaction.get("steps") \
            else InteractionResult()
        return StreamingEvent(
            event_type=StreamEventType.COMPLETED,
            interaction_id=str(interaction.get("id") or ""),
            status=str(interaction.get("status") or ""),
            text=result.text,
            thought_summary=result.thought_summary,
            media={"items": result.media} if result.media else {},
            metadata={"usage": usage.to_row(),
                      "final_text_len": len(result.text),
                      "errors": result.errors[:3],
                      "event_id": str(payload.get("event_id") or "")})

    if name == EVENT_ERROR:
        error = payload.get("error") or {}
        return StreamingEvent(
            event_type=StreamEventType.ERROR,
            metadata={"code": str(error.get("code") or ""),
                      "message": str(error.get("message") or "")[:400],
                      "event_id": str(payload.get("event_id") or "")})

    # Unknown future event types are ignored safely (never crash).
    log.info("sse.unknown_event type=%s", name[:60])
    return StreamingEvent(event_type=StreamEventType.UNKNOWN,
                          metadata={"event_type": name[:60]})


def _translate_delta(payload: Dict[str, Any]) -> StreamingEvent:
    delta = payload.get("delta") or {}
    dtype = str(delta.get("type") or "")
    index = int(payload.get("index", -1) or -1)
    interaction_id = str(payload.get("interaction_id") or "")
    metadata = {"event_id": str(payload.get("event_id") or "")}
    delta_meta = payload.get("metadata")
    if isinstance(delta_meta, dict):
        usage = TokenUsage.from_api(delta_meta.get("total_usage"))
        if not usage.is_empty():
            metadata["usage"] = usage.to_row()

    if dtype == "text":
        return StreamingEvent(event_type=StreamEventType.TEXT_DELTA,
                              interaction_id=interaction_id, step_index=index,
                              step_type=STEP_MODEL_OUTPUT, delta_type=dtype,
                              text=str(delta.get("text") or ""),
                              metadata=metadata)
    if dtype == "thought_summary":
        content = delta.get("content") or {}
        text = str(content.get("text") or "") if isinstance(content, dict) else ""
        return StreamingEvent(event_type=StreamEventType.THOUGHT_DELTA,
                              interaction_id=interaction_id, step_index=index,
                              step_type=STEP_THOUGHT, delta_type=dtype,
                              text=text, metadata=metadata)
    if dtype in ("image", "audio", "video", "document"):
        media = {k: v for k, v in delta.items() if k != "type" and k != "data"}
        media["type"] = dtype
        if delta.get("data"):
            media["data_b64_len"] = len(str(delta["data"]))
        return StreamingEvent(event_type=StreamEventType.MEDIA_DELTA,
                              interaction_id=interaction_id, step_index=index,
                              delta_type=dtype, media=media, metadata=metadata)
    if dtype == "arguments_delta":
        return StreamingEvent(event_type=StreamEventType.TOOL_CALL,
                              interaction_id=interaction_id, step_index=index,
                              delta_type=dtype,
                              text=str(delta.get("arguments") or ""),
                              metadata=metadata)
    if dtype in ("thought_signature", "text_annotation_delta"):
        # Real API deltas that must never be shown as text and are not
        # "unknown" either: they carry protocol metadata only.
        return StreamingEvent(event_type=StreamEventType.METADATA,
                              interaction_id=interaction_id, step_index=index,
                              delta_type=dtype, metadata=metadata)
    metadata["delta_type"] = dtype
    return StreamingEvent(event_type=StreamEventType.UNKNOWN,
                          interaction_id=interaction_id, step_index=index,
                          delta_type=dtype, metadata=metadata)
