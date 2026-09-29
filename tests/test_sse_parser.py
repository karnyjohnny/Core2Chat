"""SSE parser tests: framing, robustness, unknown events, cancellation safety."""

import json

import pytest

from api.gemini_interactions import (SseFrame, SseParser, event_type_of,
                                     parse_frame_json, translate)
from models.provider_models import StreamEventType


def frames_from(text: str, chunk_size: int = 0):
    parser = SseParser()
    out = []
    data = text.encode("utf-8")
    if chunk_size <= 0:
        out.extend(parser.feed(data))
    else:
        for index in range(0, len(data), chunk_size):
            out.extend(parser.feed(data[index:index + chunk_size]))
    tail = parser.flush()
    if tail is not None:
        out.append(tail)
    return out, parser


def test_parses_complete_frames():
    text = ('event: step.delta\ndata: {"index":0,"delta":{"type":"text",'
            '"text":"cześć"},"event_type":"step.delta"}\n\n')
    frames, parser = frames_from(text)
    assert len(frames) == 1
    assert frames[0].event == "step.delta"
    payload, err = parse_frame_json(frames[0])
    assert err == ""
    assert payload["delta"]["text"] == "cześć"
    assert parser.malformed == 0


def test_chunk_boundaries_do_not_break_frames():
    text = ('event: interaction.created\ndata: {"interaction":{"id":"v1_a",'
            '"status":"in_progress","model":"m","object":"interaction"},'
            '"event_type":"interaction.created"}\n\n'
            'event: done\ndata: [DONE]\n\n')
    reference, _ = frames_from(text)
    for chunk_size in (1, 2, 3, 7, 13, 64, 4096):
        frames, _ = frames_from(text, chunk_size)
        assert len(frames) == len(reference), "chunk=%d" % chunk_size
        assert [f.event for f in frames] == [f.event for f in reference]


def test_crlf_and_comments_and_keepalives():
    text = (': keep-alive\r\n\r\nevent: step.delta\r\ndata: {"index":1,'
            '"delta":{"type":"text","text":"x"},"event_type":"step.delta"}\r\n\r\n')
    frames, _ = frames_from(text)
    assert len(frames) == 1
    assert frames[0].event == "step.delta"


def test_multiline_data_is_joined():
    text = ('event: step.delta\ndata: {"index":0,\ndata: "delta":'
            '{"type":"text","text":"ok"},\ndata: "event_type":"step.delta"}\n\n')
    frames, _ = frames_from(text)
    payload, err = parse_frame_json(frames[0])
    assert err == ""
    assert payload["delta"]["text"] == "ok"


def test_done_sentinel_is_not_json():
    frames, _ = frames_from('event: done\ndata: [DONE]\n\n')
    payload, err = parse_frame_json(frames[0])
    assert payload is None and err == ""
    event = translate(frames[0], payload, err)
    assert event.event_type == StreamEventType.DONE


def test_malformed_json_is_reported_not_raised():
    frames, _ = frames_from('event: step.delta\ndata: {oops}\n\n')
    payload, err = parse_frame_json(frames[0])
    assert payload is None
    assert "malformed" in err
    event = translate(frames[0], payload, err)
    assert event.event_type == StreamEventType.UNKNOWN
    assert "malformed" in event.metadata


def test_unknown_event_type_is_ignored_safely():
    text = ('data: {"event_type":"brand.new.event","payload":{"x":1}}\n\n')
    frames, _ = frames_from(text)
    payload, err = parse_frame_json(frames[0])
    event = translate(frames[0], payload, err)
    assert event.event_type == StreamEventType.UNKNOWN
    assert event.metadata["event_type"] == "brand.new.event"


def test_event_type_prefers_json_over_sse_field():
    frame = SseFrame(event="mislabeled",
                     data=json.dumps({"event_type": "step.delta", "index": 2,
                                      "delta": {"type": "text", "text": "a"}}))
    payload, err = parse_frame_json(frame)
    assert event_type_of(frame, payload) == "step.delta"
    assert translate(frame, payload, err).event_type == StreamEventType.TEXT_DELTA


def test_envelope_form_is_unwrapped():
    """The OpenAPI envelope may wrap the event as {"data": {...}}."""
    inner = {"event_type": "step.delta", "index": 0,
             "delta": {"type": "text", "text": "hej"}}
    frame = SseFrame(event="step.delta", data=json.dumps({"data": inner}))
    payload, err = parse_frame_json(frame)
    assert err == ""
    assert payload == inner


def test_oversized_frame_is_dropped_and_counted():
    parser = SseParser(max_data_bytes=64)
    big = "x" * 500
    out = list(parser.feed(('event: step.delta\ndata: %s\n\n' % big).encode()))
    assert out == []                      # frame dropped, memory bounded
    assert parser.malformed == 1
    # The parser keeps working after dropping an oversized frame.
    ok = list(parser.feed(b'event: step.stop\ndata: {"index":0,'
                          b'"event_type":"step.stop"}\n\n'))
    assert len(ok) == 1 and ok[0].event == "step.stop"


def test_flush_emits_trailing_frame_without_blank_line():
    parser = SseParser()
    out = list(parser.feed(b'event: step.stop\ndata: {"index":1,'
                           b'"event_type":"step.stop"}'))
    assert out == []
    tail = parser.flush()
    assert tail is not None and tail.event == "step.stop"


def test_translate_text_delta_carries_index_and_id():
    frame = SseFrame(event="step.delta", data=json.dumps({
        "index": 3, "event_id": "evt-9", "event_type": "step.delta",
        "delta": {"type": "text", "text": "fragment"},
        "metadata": {"total_usage": {"total_input_tokens": 12,
                                     "total_output_tokens": 4,
                                     "total_tokens": 16}}}))
    payload, err = parse_frame_json(frame)
    event = translate(frame, payload, err)
    assert event.event_type == StreamEventType.TEXT_DELTA
    assert event.step_index == 3
    assert event.step_type == "model_output"
    assert event.text == "fragment"
    assert event.metadata["event_id"] == "evt-9"
    assert event.metadata["usage"]["input_tokens"] == 12


def test_translate_thought_summary_delta():
    frame = SseFrame(event="step.delta", data=json.dumps({
        "index": 0, "event_type": "step.delta",
        "delta": {"type": "thought_summary",
                  "content": {"type": "text", "text": "Planuję odpowiedź"}}}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.THOUGHT_DELTA
    assert event.text == "Planuję odpowiedź"
    assert event.step_type == "thought"


def test_translate_thought_signature_is_not_shown_as_text():
    frame = SseFrame(event="step.delta", data=json.dumps({
        "index": 0, "event_type": "step.delta",
        "delta": {"type": "thought_signature", "signature": "abc123"}}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.METADATA
    assert event.delta_type == "thought_signature"
    assert event.text == ""
    assert "abc123" not in json.dumps(event.metadata)


def test_translate_media_delta_does_not_copy_payload():
    frame = SseFrame(event="step.delta", data=json.dumps({
        "index": 1, "event_type": "step.delta",
        "delta": {"type": "image", "mime_type": "image/png",
                  "data": "A" * 4096}}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.MEDIA_DELTA
    assert event.media["type"] == "image"
    assert event.media["data_b64_len"] == 4096
    assert "data" not in event.media          # raw bytes are not retained


def test_translate_completed_extracts_usage_and_final_text():
    frame = SseFrame(event="interaction.completed", data=json.dumps({
        "event_type": "interaction.completed",
        "interaction": {
            "id": "v1_done", "status": "completed", "model": "m",
            "steps": [{"type": "thought", "signature": "s"},
                      {"type": "model_output",
                       "content": [{"type": "text", "text": "Gotowe"}]}],
            "usage": {"total_tokens": 42, "total_input_tokens": 10,
                      "total_output_tokens": 30, "total_thought_tokens": 2,
                      "total_cached_tokens": 4,
                      "input_tokens_by_modality": [{"modality": "text",
                                                    "tokens": 10}]}}}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.COMPLETED
    assert event.interaction_id == "v1_done"
    assert event.status == "completed"
    assert event.text == "Gotowe"
    usage = event.metadata["usage"]
    assert usage["total_tokens"] == 42
    assert usage["cached_tokens"] == 4


def test_translate_error_event():
    frame = SseFrame(event="error", data=json.dumps({
        "event_type": "error",
        "error": {"code": "rate_limit_exceeded", "message": "Quota exceeded"}}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.ERROR
    assert event.metadata["code"] == "rate_limit_exceeded"


def test_translate_status_update():
    frame = SseFrame(event="interaction.status_update", data=json.dumps({
        "event_type": "interaction.status_update",
        "interaction_id": "v1_x", "status": "requires_action"}))
    payload, _ = parse_frame_json(frame)
    event = translate(frame, payload)
    assert event.event_type == StreamEventType.STATUS
    assert event.status == "requires_action"


def test_empty_stream_produces_no_frames():
    frames, parser = frames_from("")
    assert frames == []
    assert parser.flush() is None


def test_recorded_live_stream_roundtrip():
    """The captured live transcript must translate into the expected events."""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "sse_stream.txt")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    frames, _ = frames_from(text)
    events = []
    for frame in frames:
        payload, err = parse_frame_json(frame)
        events.append(translate(frame, payload, err))
    kinds = [e.event_type for e in events]
    assert StreamEventType.CREATED in kinds
    assert StreamEventType.STEP_START in kinds
    assert StreamEventType.TEXT_DELTA in kinds
    assert StreamEventType.STEP_STOP in kinds
    assert StreamEventType.COMPLETED in kinds
    assert StreamEventType.DONE in kinds
    # thought_signature deltas are protocol metadata, not unknown events
    assert StreamEventType.METADATA in kinds
    assert StreamEventType.UNKNOWN not in kinds
