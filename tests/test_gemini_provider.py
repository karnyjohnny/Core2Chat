"""Gemini provider tests against recorded live payloads (mocked transport).

All fixtures in ``tests/fixtures`` were captured from the real API on
2026-09-29 and scrubbed of identifiers, so request/response shapes here are
facts rather than guesses.
"""

import json
import os
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from api import gemini_interactions as interactions
from api.errors import from_response
from api.gemini_provider import GeminiProvider, build_provider
from api.provider_capabilities import (classify_lifecycle, derive_capabilities,
                                       filter_models, is_chat_candidate,
                                       sort_models)
from models.message_models import ContentPart, ContentPartType
from models.provider_models import (Capability, ErrorCategory, ProviderError,
                                    StreamEventType)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASE = "https://generativelanguage.googleapis.com"


def fixture(name: str) -> Any:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


class Recorder(object):
    """Collects outgoing requests so tests can assert on the wire format."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def bodies(self) -> List[Any]:
        out = []
        for request in self.requests:
            if not request.content:
                out.append(None)
                continue
            try:
                out.append(json.loads(request.content.decode("utf-8")))
            except ValueError:
                out.append(request.content)
        return out

    def last(self) -> httpx.Request:
        return self.requests[-1]

    def urls(self) -> List[str]:
        return [str(r.url) for r in self.requests]


def make_provider(handler: Callable[[httpx.Request], httpx.Response],
                  api_key: str = "test-key-not-a-secret",
                  **kwargs: Any) -> GeminiProvider:
    recorder = getattr(handler, "recorder", None)

    def wrapped(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    provider = build_provider(api_key=api_key, base_url=BASE,
                              transport=transport, **kwargs)
    provider.recorder = recorder  # type: ignore[attr-defined]
    return provider


def recording(handler: Callable[[httpx.Request], httpx.Response]) -> Callable:
    handler.recorder = Recorder()  # type: ignore[attr-defined]
    return handler


def json_response(payload: Any, status: int = 200,
                  headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    out_headers = {"Content-Type": "application/json"}
    if headers:
        out_headers.update(headers)
    return httpx.Response(status, json=payload, headers=out_headers)


def sse_response(text: str, chunk_size: int = 37) -> httpx.Response:
    """Stream fixture in odd-sized chunks to stress the incremental parser."""
    data = text.encode("utf-8")
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                          stream=_IteratorStream(iter(chunks)))


class _IteratorStream(httpx.SyncByteStream):
    """Feeds an iterable of byte chunks to httpx as a response stream."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        pass


# ------------------------------------------------------------- model discovery
def test_model_discovery_parses_real_payload():
    payload = fixture("models_list.json")
    provider = make_provider(recording(
        lambda request: json_response(payload)))
    models = provider.list_models(force_refresh=True)
    ids = [m.model_id for m in models]
    assert "gemini-3.8-flash" in ids
    assert "gemini-2.5-flash" in ids
    # embedding / video / tts / agent models are not chat candidates
    assert "gemini-embedding-001" not in ids
    assert "veo-3.1-generate-preview" not in ids
    assert "aqa" not in ids


def test_model_metadata_fields_are_populated():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    model = provider.get_model("gemini-3.8-flash")
    assert model is not None
    assert model.display_name == "Gemini 3.8 Flash"
    assert model.input_token_limit == 1048576
    assert model.output_token_limit == 65536
    assert "generateContent" in model.supported_methods
    assert "countTokens" in model.supported_methods
    assert model.thinking is True
    assert model.resource_name == "models/gemini-3.8-flash"


def test_capabilities_derived_from_metadata_not_names():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    model = provider.get_model("gemini-3.8-flash")
    caps = model.capabilities
    assert caps.supports(Capability.TEXT_INPUT)
    assert caps.supports(Capability.STREAMING)
    assert caps.supports(Capability.TOKEN_COUNTING)
    assert caps.supports(Capability.CONTEXT_CACHING_IMPLICIT)
    assert caps.supports(Capability.CONVERSATION_STATE)
    assert caps.supports(Capability.THINKING)
    assert caps.min_cached_tokens == 4096
    # Unproven capabilities stay False until a probe or real traffic says so.
    assert not caps.supports(Capability.IMAGE_INPUT)
    assert not caps.supports(Capability.SERVER_SIDE_CANCEL)
    assert provider.supports("gemini-3.8-flash", Capability.TOKEN_COUNTING)


def test_lifecycle_classification():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    provider.list_models(force_refresh=True, include_legacy=True,
                         chat_only=False)
    models = {m.model_id: m for m in provider.discovery.all_models()}
    assert models["gemini-3.8-flash"].lifecycle == "operational"
    assert models["gemini-3.1-flash-lite"].lifecycle == "operational"
    # Gemini 2.5 is documented as access-limited but *not* deprecated.
    assert models["gemini-2.5-flash"].lifecycle == "operational"


def test_legacy_models_hidden_unless_enabled():
    from api.gemini_models import parse_model

    legacy = parse_model({
        "name": "models/gemini-1.5-pro", "displayName": "Gemini 1.5 Pro",
        "inputTokenLimit": 2000000, "outputTokenLimit": 8192,
        "supportedGenerationMethods": ["generateContent", "countTokens"]})
    assert legacy.lifecycle == "deprecated"
    assert filter_models([legacy], include_legacy=False) == []
    assert filter_models([legacy], include_legacy=True) == [legacy]
    # Current-generation models are never hidden by the legacy filter.
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    default_ids = [m.model_id for m in provider.list_models(force_refresh=True)]
    assert "gemini-3.8-flash" in default_ids


def test_model_filter_is_evidence_based():
    from api.gemini_models import parse_model

    unavailable = parse_model({"name": "models/dead-model",
                               "supportedGenerationMethods": []})
    # No methods at all -> lifecycle is unknown, and it is never offered.
    assert unavailable.lifecycle == "unknown"
    assert not is_chat_candidate(unavailable)
    assert filter_models([unavailable]) == []
    shut_down = parse_model({"name": "models/odd-model",
                             "supportedGenerationMethods": ["someFutureMethod"]})
    assert shut_down.lifecycle == "shut_down"
    assert filter_models([shut_down]) == []

    tiny = parse_model({"name": "models/tiny", "inputTokenLimit": 512,
                        "outputTokenLimit": 128,
                        "supportedGenerationMethods": ["generateContent"]})
    assert not is_chat_candidate(tiny)


def test_models_pagination_is_followed():
    pages = [
        {"models": [{"name": "models/m1",
                     "supportedGenerationMethods": ["generateContent"],
                     "inputTokenLimit": 100000, "outputTokenLimit": 8000}],
         "nextPageToken": "next"},
        {"models": [{"name": "models/m2",
                     "supportedGenerationMethods": ["generateContent"],
                     "inputTokenLimit": 100000, "outputTokenLimit": 8000}],
         "nextPageToken": ""},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = calls["n"]
        calls["n"] += 1
        return json_response(pages[min(index, len(pages) - 1)])

    provider = make_provider(recording(handler))
    models = provider.list_models(force_refresh=True, chat_only=False)
    assert calls["n"] == 2
    assert sorted(m.model_id for m in models) == ["m1", "m2"]
    assert "pageToken=next" in provider.recorder.urls()[1]


def test_model_cache_avoids_second_request(memory_db):
    from db.repositories import ModelCacheRepository

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(fixture("models_list.json"))

    provider = make_provider(recording(handler),
                             model_cache=ModelCacheRepository(memory_db),
                             model_ttl_seconds=600)
    first = provider.list_models(force_refresh=True)
    assert calls["n"] == 1
    second = provider.list_models(force_refresh=False)
    assert calls["n"] == 1                      # served from memory cache
    assert len(second) == len(first)

    # A brand new provider (app restart) is served from the SQLite cache.
    fresh = make_provider(recording(handler),
                          model_cache=ModelCacheRepository(memory_db),
                          model_ttl_seconds=600)
    offline = fresh.list_models(offline=True)
    assert calls["n"] == 1
    assert any(m.model_id == "gemini-3.8-flash" for m in offline)


def test_unavailable_model_marked_from_runtime_evidence():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    provider.list_models(force_refresh=True)
    provider.discovery.mark_unavailable("gemini-3.8-flash", "404 not found")
    ids = [m.model_id for m in provider.list_models()]
    assert "gemini-3.8-flash" not in ids


# ------------------------------------------------------------- token counting
def test_count_tokens_uses_provider_tokeniser():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":countTokens")
        return json_response(fixture("count_tokens.json"))

    provider = make_provider(recording(handler))
    parts = [ContentPart(type=ContentPartType.TEXT, text="The quick brown fox.")]
    assert provider.count_tokens("gemini-3.8-flash", parts) == 5
    body = provider.recorder.bodies()[-1]
    # Verified live: countTokens accepts ONLY "contents" - no systemInstruction.
    assert set(body.keys()) == {"contents"}
    assert body["contents"][0]["parts"][0]["text"] == "The quick brown fox."


def test_count_tokens_includes_system_instruction_as_content():
    provider = make_provider(recording(
        lambda request: json_response(fixture("count_tokens.json"))))
    parts = [ContentPart(type=ContentPartType.TEXT, text="hi")]
    provider.count_tokens("gemini-3.8-flash", parts,
                          system_instruction="Bądź zwięzły.")
    body = provider.recorder.bodies()[-1]
    assert body["contents"][0]["parts"][0]["text"] == "Bądź zwięzły."
    assert body["contents"][-1]["parts"][0]["text"] == "hi"


def test_count_tokens_reports_modality_breakdown():
    provider = make_provider(recording(
        lambda request: json_response(fixture("count_tokens.json"))))
    usage = provider.count_tokens_usage(
        "gemini-3.8-flash", [ContentPart(text="abc")])
    assert usage.input_tokens == 5
    assert usage.modality_breakdown.get("input:text") == 5


def test_count_tokens_unsupported_capability_returns_minus_one():
    provider = make_provider(recording(
        lambda request: json_response({"models": [
            {"name": "models/no-count",
             "supportedGenerationMethods": ["generateContent"],
             "inputTokenLimit": 100000, "outputTokenLimit": 8000}]})))
    provider.list_models(force_refresh=True)
    assert provider.count_tokens("no-count", [ContentPart(text="x")]) == -1
    # No countTokens call may reach the wire when the capability is absent.
    assert len(provider.recorder.requests) == 1
    assert ":countTokens" not in provider.recorder.urls()[0]


# ------------------------------------------------------------------ requests
def test_send_message_parses_real_unary_response():
    payload = fixture("interaction_unary.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1beta/interactions")
        assert request.headers["x-goog-api-key"] == "test-key-not-a-secret"
        assert request.headers["Content-Type"] == "application/json"
        return json_response(payload)

    provider = make_provider(recording(handler))
    result = provider.send_message(
        "gemini-3.5-flash-lite",
        [ContentPart(type=ContentPartType.TEXT, text="Reply with exactly: OK")],
        generation_config={"max_output_tokens": 64, "thinking_level": "minimal"})
    assert result["status"] == "completed"
    assert result["text"] == "OK"
    assert result["interaction_id"]
    usage = result["usage"]
    assert usage.input_tokens == 6
    assert usage.output_tokens == 1
    assert usage.total_tokens == 7
    body = provider.recorder.bodies()[-1]
    assert body["model"] == "gemini-3.5-flash-lite"
    assert body["input"] == "Reply with exactly: OK"
    assert body.get("stream") is not True       # unary request: no stream flag
    assert body["generation_config"] == {"max_output_tokens": 64,
                                         "thinking_level": "minimal"}


def test_request_body_omits_empty_and_unsupported_fields():
    provider = make_provider(recording(
        lambda request: json_response(fixture("interaction_unary.json"))))
    provider.send_message(
        "gemini-3.8-flash", [ContentPart(text="hi")],
        generation_config={"temperature": 0.7, "top_p": 0.9, "top_k": 40,
                           "max_output_tokens": 0, "thinking_level": "ultra",
                           "stop_sequences": []})
    body = provider.recorder.bodies()[-1]
    # Deprecated/unsupported sampling parameters must never be sent blindly.
    assert "temperature" not in body.get("generation_config", {})
    assert "top_p" not in body.get("generation_config", {})
    assert "top_k" not in body.get("generation_config", {})
    assert "generation_config" not in body      # everything dropped -> absent
    assert "system_instruction" not in body
    assert "previous_interaction_id" not in body
    assert "store" not in body


def test_multimodal_input_encoded_as_content_array():
    provider = make_provider(recording(
        lambda request: json_response(fixture("interaction_unary.json"))))
    provider.send_message("gemini-3.8-flash", [
        ContentPart(type=ContentPartType.IMAGE, data=b"\x89PNG\r\n\x1a\n1234",
                    mime_type="image/png"),
        ContentPart(type=ContentPartType.TEXT, text="Co to jest?"),
    ])
    body = provider.recorder.bodies()[-1]
    assert isinstance(body["input"], list)
    assert body["input"][0]["type"] == "image"
    assert body["input"][0]["mime_type"] == "image/png"
    assert body["input"][0]["data"]
    assert body["input"][1] == {"type": "text", "text": "Co to jest?"}


def test_stateful_mode_sends_previous_interaction_id():
    payload = fixture("stateful_turns.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(payload["t2"])

    provider = make_provider(recording(handler))
    result = provider.send_message(
        "gemini-3.5-flash-lite", [ContentPart(text="What is my name?")],
        previous_interaction_id="v1_previous", store=True)
    assert "Bob" in result["text"]
    body = provider.recorder.bodies()[-1]
    assert body["previous_interaction_id"] == "v1_previous"
    assert body["store"] is True


def test_api_key_never_appears_in_query_string():
    provider = make_provider(recording(
        lambda request: json_response(fixture("interaction_unary.json"))),
        api_key="SUPER-SECRET-VALUE")
    provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    for url in provider.recorder.urls():
        assert "SUPER-SECRET-VALUE" not in url
        assert "key=" not in url
    assert provider.recorder.last().headers["x-goog-api-key"] == \
        "SUPER-SECRET-VALUE"


def test_api_revision_header_is_optional_and_centralised():
    provider = make_provider(recording(
        lambda request: json_response(fixture("interaction_unary.json"))))
    provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    assert "Api-Revision" not in provider.recorder.last().headers
    provider.configure(api_revision="2026-05-20")
    provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    assert provider.recorder.last().headers["Api-Revision"] == "2026-05-20"


# ------------------------------------------------------------------ streaming
def test_stream_yields_typed_events_in_order():
    text = fixture_text("sse_stream.txt")
    provider = make_provider(recording(
        lambda request: sse_response(text)))
    events = list(provider.stream_message(
        "gemini-3.5-flash-lite", [ContentPart(text="Count from 1 to 12.")],
        generation_config={"max_output_tokens": 128}))
    kinds = [e.event_type for e in events]
    assert kinds[0] == StreamEventType.STARTED
    assert StreamEventType.CREATED in kinds
    assert StreamEventType.STEP_START in kinds
    assert kinds.count(StreamEventType.TEXT_DELTA) >= 2
    assert StreamEventType.COMPLETED in kinds
    assert kinds[-1] in (StreamEventType.USAGE, StreamEventType.DONE,
                         StreamEventType.COMPLETED)

    body = provider.recorder.bodies()[-1]
    assert body["stream"] is True

    created = next(e for e in events if e.event_type == StreamEventType.CREATED)
    assert created.interaction_id
    assert created.status == "in_progress"

    step_starts = [e for e in events if e.event_type == StreamEventType.STEP_START]
    assert [e.step_type for e in step_starts] == ["thought", "model_output"]

    deltas = [e for e in events if e.event_type == StreamEventType.TEXT_DELTA]
    assert all(d.step_type == "model_output" for d in deltas)
    streamed = "".join(d.text for d in deltas)
    assert streamed.strip().startswith("1, 2, 3")

    completed = [e for e in events if e.event_type == StreamEventType.COMPLETED][0]
    assert completed.status == "completed"
    usage = completed.metadata["usage"]
    # Values below come from the recorded live fixture (normalised names).
    assert usage["input_tokens"] == 13
    assert usage["output_tokens"] == 37
    assert usage["total_tokens"] == 50


def test_stream_reports_usage_event():
    provider = make_provider(recording(
        lambda request: sse_response(fixture_text("sse_stream.txt"))))
    events = list(provider.stream_message("gemini-3.5-flash-lite",
                                          [ContentPart(text="hi")]))
    usage_events = [e for e in events if e.event_type == StreamEventType.USAGE]
    assert usage_events, "usage event must be emitted for statistics"
    assert usage_events[-1].metadata["usage"]["total_tokens"] == 50


def test_stream_survives_unknown_event_types():
    stream = (
        'event: interaction.created\n'
        'data: {"interaction":{"id":"v1_x","status":"in_progress"},'
        '"event_type":"interaction.created"}\n\n'
        'event: google.new.future.event\n'
        'data: {"event_type":"google.new.future.event","whatever":123}\n\n'
        ': keep-alive comment\n\n'
        'event: step.delta\n'
        'data: {"index":0,"delta":{"type":"text","text":"Działa"},'
        '"event_type":"step.delta"}\n\n'
        'event: done\ndata: [DONE]\n\n'
    )
    provider = make_provider(recording(lambda request: sse_response(stream)))
    events = list(provider.stream_message("m", [ContentPart(text="hi")]))
    kinds = [e.event_type for e in events]
    assert StreamEventType.UNKNOWN in kinds
    assert StreamEventType.TEXT_DELTA in kinds
    assert StreamEventType.DONE in kinds
    texts = "".join(e.text for e in events
                    if e.event_type == StreamEventType.TEXT_DELTA)
    assert texts == "Działa"


def test_stream_handles_malformed_json_without_crashing():
    stream = (
        'event: step.delta\ndata: {not-json}\n\n'
        'event: step.delta\n'
        'data: {"index":0,"delta":{"type":"text","text":"ok"},'
        '"event_type":"step.delta"}\n\n'
        'event: done\ndata: [DONE]\n\n'
    )
    provider = make_provider(recording(lambda request: sse_response(stream)))
    events = list(provider.stream_message("m", [ContentPart(text="hi")]))
    malformed = [e for e in events if e.event_type == StreamEventType.UNKNOWN
                 and e.metadata.get("malformed")]
    assert malformed
    assert any(e.text == "ok" for e in events)


def test_stream_marks_interruption_when_socket_closes_early():
    stream = ('event: interaction.created\n'
              'data: {"interaction":{"id":"v1_cut","status":"in_progress"},'
              '"event_type":"interaction.created"}\n\n'
              'event: step.delta\n'
              'data: {"index":0,"delta":{"type":"text","text":"część"},'
              '"event_type":"step.delta"}\n\n')
    provider = make_provider(recording(lambda request: sse_response(stream)))
    events = list(provider.stream_message("m", [ContentPart(text="hi")]))
    final = [e for e in events if e.event_type == StreamEventType.COMPLETED]
    assert final and final[-1].status == "interrupted"
    assert final[-1].metadata.get("interrupted") is True


def test_stream_error_event_is_normalised():
    stream = ('event: error\n'
              'data: {"error":{"code":"rate_limit_exceeded",'
              '"message":"Quota exceeded"},"event_type":"error"}\n\n')
    provider = make_provider(recording(lambda request: sse_response(stream)))
    events = list(provider.stream_message("m", [ContentPart(text="hi")]))
    errors = [e for e in events if e.event_type == StreamEventType.ERROR]
    assert errors
    assert errors[0].metadata["code"] == "rate_limit_exceeded"


def test_stream_http_error_before_body_is_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"error": {"message": "API key not valid.",
                                        "code": "invalid_request"}}, status=400)

    provider = make_provider(recording(handler))
    events = list(provider.stream_message("m", [ContentPart(text="hi")]))
    assert [e.event_type for e in events][-1] == StreamEventType.ERROR
    payload = events[-1].metadata["error"]
    assert payload["category"] == ErrorCategory.AUTHENTICATION


def test_cancellation_stops_the_stream_promptly():
    import threading
    import time

    chunks_sent = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        def gen():
            yield ('event: interaction.created\ndata: {"interaction":'
                   '{"id":"v1_cancel","status":"in_progress"},'
                   '"event_type":"interaction.created"}\n\n').encode()
            for index in range(500):
                chunks_sent["n"] = index
                yield ('event: step.delta\ndata: {"index":0,"delta":'
                       '{"type":"text","text":"x"},"event_type":"step.delta"}'
                       '\n\n').encode()
                time.sleep(0.01)
            yield b"event: done\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=_IteratorStream(gen()))

    provider = make_provider(recording(handler))
    cancel = threading.Event()
    threading.Timer(0.12, cancel.set).start()
    started = time.monotonic()
    events = list(provider.stream_message("m", [ContentPart(text="hi")],
                                          cancel_event=cancel))
    elapsed = time.monotonic() - started
    kinds = [e.event_type for e in events]
    assert StreamEventType.CANCELLED in kinds
    assert elapsed < 2.0, "cancellation took too long: %.2fs" % elapsed
    assert chunks_sent["n"] < 400, "stream was not aborted"
    assert kinds[-1] == StreamEventType.CANCELLED


def test_cancel_endpoint_failure_is_remembered():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            calls["n"] += 1
            return json_response({"error": {"message": "Requested entity was "
                                                       "not found.",
                                            "code": "not_found"}}, status=404)
        return json_response(fixture("interaction_unary.json"))

    provider = make_provider(recording(handler))
    assert provider.cancel_request("v1_x") is False
    assert provider.server_side_cancel_supported is False
    # Second call must not hit the network again (measured: unsupported).
    assert provider.cancel_request("v1_y") is False
    assert calls["n"] == 1
    assert not provider.get_capabilities("gemini-3.5-flash-lite").supports(
        Capability.SERVER_SIDE_CANCEL)


def test_cancel_endpoint_success_enables_capability():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            return json_response({})
        return json_response(fixture("models_list.json"))

    provider = make_provider(recording(handler))
    assert provider.cancel_request("v1_x") is True
    provider.list_models(force_refresh=True)
    assert provider.get_capabilities("gemini-3.8-flash").supports(
        Capability.SERVER_SIDE_CANCEL)


def test_get_interaction_and_remote_state_validation():
    payload = fixture("interaction_unary.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(payload)
        return json_response(payload)

    provider = make_provider(recording(handler))
    info = provider.get_interaction("v1_x")
    assert info is not None and info["status"] == "completed"
    assert provider.remote_state_usable("v1_x") is True

    def missing(request: httpx.Request) -> httpx.Response:
        return json_response({"error": {"message": "not found",
                                        "code": "not_found"}}, status=404)

    provider2 = make_provider(recording(missing))
    assert provider2.get_interaction("v1_missing") is None
    assert provider2.remote_state_usable("v1_missing") is False


# --------------------------------------------------------------- error model
def _error_case(status: int, payload: Any, category: str, retryable: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(payload, status=status)

    provider = make_provider(recording(handler))
    with pytest.raises(ProviderError) as info:
        provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    error = info.value
    assert error.category == category
    assert error.http_status == status
    assert error.retryable is retryable
    assert error.message_user            # Polish, user-facing
    assert "test-key" not in error.message_debug
    return error


def test_invalid_api_key_maps_to_authentication():
    errors = fixture("errors.json")
    status, body = errors["bad_key"]
    error = _error_case(status, body, ErrorCategory.AUTHENTICATION, False)
    assert "Klucz API" in error.message_user


def test_unknown_model_maps_to_model_unavailable():
    errors = fixture("errors.json")
    status, body = errors["bad_model"]
    _error_case(status, body, ErrorCategory.MODEL_UNAVAILABLE, False)


def test_unknown_parameter_maps_to_invalid_request():
    errors = fixture("errors.json")
    status, body = errors["bad_body"]
    _error_case(status, body, ErrorCategory.INVALID_REQUEST, False)


def test_rate_limit_is_retryable_with_polish_message():
    error = _error_case(429, {"error": {"message": "Rate limit exceeded",
                                        "code": "rate_limit"}},
                        ErrorCategory.RATE_LIMIT, True)
    assert "Limit API" in error.message_user


def test_context_too_large_is_classified():
    error = _error_case(400, {"error": {
        "message": "Prompt token count 2000000 exceeds the model input token "
                   "limit of 1048576.", "code": "invalid_request"}},
        ErrorCategory.CONTEXT_TOO_LARGE, False)
    assert "Kontekst" in error.message_user


def test_server_errors_are_retryable():
    _error_case(503, {"error": {"message": "Service unavailable",
                                "code": 503}}, ErrorCategory.SERVER_ERROR, True)


def test_retry_after_header_is_honoured():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return json_response({"error": {"message": "quota", "code": 429}},
                                 status=429, headers={"Retry-After": "0"})
        return json_response(fixture("interaction_unary.json"))

    provider = make_provider(recording(handler), max_retries=1)
    result = provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    assert attempts["n"] == 2
    assert result["status"] == "completed"
    assert provider.client.stats["retries"] == 1


def test_post_is_not_retried_blindly():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return json_response({"error": {"message": "boom", "code": 500}},
                             status=500)

    provider = make_provider(recording(handler), max_retries=3)
    with pytest.raises(ProviderError):
        provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    assert attempts["n"] == 1, "POST must not be retried after possible processing"


def test_get_is_retried_on_server_error():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return json_response({"error": {"message": "boom", "code": 503}},
                                 status=503)
        return json_response(fixture("models_list.json"))

    provider = make_provider(recording(handler), max_retries=3)
    models = provider.list_models(force_refresh=True)
    assert attempts["n"] == 3
    assert models


def test_network_error_maps_to_network_category():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = make_provider(recording(handler))
    with pytest.raises(ProviderError) as info:
        provider.discovery.fetch()
    assert info.value.category == ErrorCategory.NETWORK
    assert info.value.retryable is True
    # The catalogue call must degrade instead of raising (offline-first UI).
    assert provider.list_models(force_refresh=True) == []


def test_timeout_maps_to_timeout_category():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    provider = make_provider(recording(handler))
    with pytest.raises(ProviderError) as info:
        provider.discovery.fetch()
    assert info.value.category == ErrorCategory.TIMEOUT


def test_error_envelope_both_shapes_are_parsed():
    interactions_style = from_response(
        "gemini", httpx.Response(404, json={"error": {
            "message": "Model 'x' not found.", "code": "not_found"}}))
    assert interactions_style.category == ErrorCategory.MODEL_UNAVAILABLE
    legacy_style = from_response(
        "gemini", httpx.Response(400, json={"error": {
            "code": 400, "message": "API key not valid.",
            "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}]}}))
    assert legacy_style.category == ErrorCategory.AUTHENTICATION
    assert legacy_style.code == "API_KEY_INVALID"


def test_error_never_leaks_key_into_debug_message():
    response = httpx.Response(403, json={"error": {
        "message": "Key SUPER-SECRET-VALUE is not authorised",
        "code": "forbidden"}})
    from core.logging_setup import redact_text, register_secret
    error = from_response("gemini", response)
    register_secret("SUPER-SECRET-VALUE")
    # The redaction layer scrubs registered secrets from anything logged.
    assert "SUPER-SECRET-VALUE" not in redact_text(error.message_debug)
    assert "SUPER-SECRET-VALUE" not in redact_text(str(error.to_dict()))


# -------------------------------------------------------------------- files
def test_file_upload_uses_resumable_protocol():
    file_payload = fixture("file_upload.json")
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/upload/session/"):
            seen["finish_headers"] = request.headers
            seen["finish_bytes"] = request.content
            return json_response({"file": file_payload})
        if request.url.path.startswith("/upload/"):
            seen["start_headers"] = request.headers   # case-insensitive httpx.Headers
            seen["start_body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={}, headers={
                "x-goog-upload-url": BASE + "/upload/session/abc",
                "x-goog-upload-status": "active"})
        return json_response({})

    provider = make_provider(recording(handler))
    from models.attachment_models import Attachment, AttachmentKind
    attachment = Attachment(filename="tiny.png", mime_type="image/png",
                            kind=AttachmentKind.IMAGE, payload=b"PNGDATA")
    uri = provider.upload_file(attachment)
    # The fixture is the raw API resource (camelCase); the provider normalises.
    assert uri == file_payload["uri"]
    assert attachment.remote_name == file_payload["name"]
    assert attachment.remote_expires_at == file_payload["expirationTime"]
    from api.gemini_files import normalise_file
    info = normalise_file(file_payload)
    assert info["expires_at"] == file_payload["expirationTime"]
    assert info["state"] == "ACTIVE"
    assert info["size_bytes"] == 70
    assert seen["start_headers"]["X-Goog-Upload-Protocol"] == "resumable"
    assert seen["start_headers"]["X-Goog-Upload-Command"] == "start"
    assert seen["start_headers"]["X-Goog-Upload-Header-Content-Length"] == "7"
    assert seen["start_headers"]["X-Goog-Upload-Header-Content-Type"] == "image/png"
    assert seen["start_body"] == {"file": {"displayName": "tiny.png"}}
    assert seen["finish_headers"]["X-Goog-Upload-Command"] == "upload, finalize"
    assert seen["finish_headers"]["X-Goog-Upload-Offset"] == "0"
    assert seen["finish_bytes"] == b"PNGDATA"


def test_file_listing_and_deletion():
    file_payload = fixture("file_upload.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response({"files": [file_payload]})
        if request.method == "DELETE":
            return json_response({})
        return json_response({})

    provider = make_provider(recording(handler))
    files = provider.list_remote_files()
    assert files[0]["name"] == file_payload["name"]
    assert files[0]["state"] == "ACTIVE"
    assert provider.delete_file(file_payload["name"]) is True
    assert provider.delete_file("") is False


def test_missing_upload_url_header_is_a_file_error():
    provider = make_provider(recording(
        lambda request: json_response({})))
    from models.attachment_models import Attachment, AttachmentKind
    attachment = Attachment(filename="x.png", mime_type="image/png",
                            kind=AttachmentKind.IMAGE, payload=b"1234")
    with pytest.raises(ProviderError) as info:
        provider.upload_file(attachment)
    assert info.value.category == ErrorCategory.FILE_ERROR


# --------------------------------------------------------------------- cache
def test_explicit_cache_is_reported_as_unsupported():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    with pytest.raises(ProviderError) as info:
        provider.create_cache("gemini-3.8-flash", [ContentPart(text="x")])
    assert info.value.category == ErrorCategory.CACHE_ERROR
    assert provider.delete_cache("cachedContents/x") is False


def test_implicit_cache_policy_fingerprint_and_stats(memory_db):
    from api.gemini_cache import ImplicitCachePolicy
    from db.repositories import ProviderCacheRepository
    from models.usage_models import TokenUsage

    policy = ImplicitCachePolicy(repo=ProviderCacheRepository(memory_db))
    static = [ContentPart(text="Duży statyczny kontekst" * 100)]
    decision = policy.decide("gemini-3.8-flash", "Jesteś zwięzły.", static,
                             static_tokens=5000, min_cached_tokens=4096)
    assert decision.eligible is True
    assert len(decision.fingerprint) == 64

    small = policy.decide("gemini-3.8-flash", "", static, static_tokens=100,
                          min_cached_tokens=4096)
    assert small.eligible is False
    assert "progu" in small.reason

    # Same static content -> identical fingerprint (cache reuse precondition).
    again = policy.decide("gemini-3.8-flash", "Jesteś zwięzły.", static,
                          static_tokens=5000, min_cached_tokens=4096)
    assert again.fingerprint == decision.fingerprint
    assert policy.observe_usage(TokenUsage(input_tokens=5000, cached_tokens=4900,
                                           total_tokens=5100), again) is True
    assert policy.stats.hits == 1
    assert policy.stats.cached_tokens == 4900
    assert policy.entries()


def test_capability_probing_uses_real_requests():
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/interactions"):
            calls.append(json.loads(request.content.decode()))
            return json_response(fixture("interaction_unary.json"))
        return json_response(fixture("models_list.json"))

    provider = make_provider(recording(handler))
    provider.list_models(force_refresh=True)
    probed = provider.probe_capabilities("gemini-3.8-flash",
                                         image_bytes=b"\x89PNG\r\n\x1a\n")
    assert probed[Capability.IMAGE_INPUT] is True
    assert provider.get_capabilities("gemini-3.8-flash").supports(
        Capability.IMAGE_INPUT)
    assert calls and calls[-1]["input"][0]["type"] == "image"
    assert calls[-1]["generation_config"]["max_output_tokens"] == 8


def test_observed_capabilities_come_from_real_traffic():
    payload = json.loads(json.dumps(fixture("interaction_unary.json")))
    payload["usage"]["total_cached_tokens"] = 1234
    payload["steps"].append({"type": "thought",
                             "summary": [{"text": "Plan odpowiedzi"}]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(fixture("models_list.json"))
        return json_response(payload)

    provider = make_provider(recording(handler))
    provider.list_models(force_refresh=True)
    provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")])
    caps = provider.get_capabilities("gemini-3.8-flash")
    assert caps.supports(Capability.CONTEXT_CACHING_IMPLICIT)
    assert caps.supports(Capability.THINKING_SUMMARY)


# -------------------------------------------------------------- credentials
def test_connectivity_check_classification():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    assert provider.connectivity_check() == "online"

    offline = make_provider(recording(lambda request: (_ for _ in ()).throw(
        httpx.ConnectError("offline"))))
    assert offline.connectivity_check() == "offline"

    no_key = make_provider(recording(lambda request: json_response({})),
                           api_key="")
    assert no_key.connectivity_check() == "no_key"


def test_validate_credentials():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    assert provider.validate_credentials() is True
    bad = make_provider(recording(lambda request: json_response(
        {"error": {"message": "API key not valid.", "code": 400}}, status=400)))
    assert bad.validate_credentials() is False


def test_diagnostics_are_sanitised():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))),
        api_key="SUPER-SECRET-VALUE")
    provider.list_models(force_refresh=True)
    blob = json.dumps(provider.diagnostics(), default=str)
    assert "SUPER-SECRET-VALUE" not in blob
    assert provider.diagnostics()["has_api_key"] is True


# ------------------------------------------------------------------ ordering
def test_sort_models_is_factual():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    provider.list_models(force_refresh=True)
    models = provider.discovery.all_models()
    ordered = sort_models(models)
    assert ordered, "catalogue must not be empty"
    chat_ids = [m.model_id for m in ordered if is_chat_candidate(m)]
    non_chat_ids = [m.model_id for m in ordered if not is_chat_candidate(m)]
    # Chat-capable models come first; inside a group ordering is factual
    # (input limit desc, then id) - never a subjective "best model" ranking.
    positions = [ordered.index(m) for m in ordered
                 if m.model_id in chat_ids]
    assert positions == sorted(positions)
    assert max(positions) < min(ordered.index(m) for m in ordered
                                if m.model_id in non_chat_ids)
    limits = [m.input_token_limit for m in ordered if is_chat_candidate(m)]
    assert limits == sorted(limits, reverse=True)


def test_provider_close_is_idempotent():
    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    provider.close()
    provider.close()


# ------------------------------------------------- self-healing / cancellation
def test_cancellation_does_not_wait_for_the_next_chunk():
    """Thinking models can stay silent for seconds; Stop must still abort."""
    import threading
    import time

    started_at = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        def gen():
            started_at["t"] = time.monotonic()
            yield ('event: interaction.created\ndata: {"interaction":'
                   '{"id":"v1_silent","status":"in_progress"},'
                   '"event_type":"interaction.created"}\n\n').encode()
            time.sleep(1.5)          # long silent "thinking" phase
            yield ('event: step.delta\ndata: {"index":0,"delta":{"type":"text",'
                   '"text":"late"},"event_type":"step.delta"}\n\n').encode()
            yield b"event: done\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=_IteratorStream(gen()))

    provider = make_provider(recording(handler))
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    events = list(provider.stream_message("m", [ContentPart(text="hi")],
                                          cancel_event=cancel))
    kinds = [e.event_type for e in events]
    assert StreamEventType.CANCELLED in kinds
    assert StreamEventType.TEXT_DELTA not in kinds   # the late chunk was dropped
    assert kinds[-1] == StreamEventType.CANCELLED


def test_unsupported_generation_parameter_is_dropped_and_retried():
    """Live finding: some models reject thinking_level values others accept."""
    attempts: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        attempts.append(body)
        config = body.get("generation_config") or {}
        if config.get("thinking_level") == "minimal":
            return json_response({"error": {
                "message": "'minimal' is not a supported thinking level for "
                           "this model. Allowed values are: medium, low, high.",
                "code": "invalid_request"}}, status=400)
        return json_response(fixture("interaction_unary.json"))

    provider = make_provider(recording(handler))
    result = provider.send_message(
        "gemini-3.8-flash", [ContentPart(text="hi")],
        generation_config={"max_output_tokens": 16, "thinking_level": "minimal"})
    assert result["status"] == "completed"
    assert len(attempts) == 2
    assert attempts[0]["generation_config"]["thinking_level"] == "minimal"
    assert "thinking_level" not in attempts[1]["generation_config"]
    assert provider.rejected_parameters("gemini-3.8-flash") == ["thinking_level"]

    # The lesson is remembered: the next call omits the parameter immediately.
    attempts.clear()
    provider.send_message("gemini-3.8-flash", [ContentPart(text="hi")],
                          generation_config={"max_output_tokens": 16,
                                             "thinking_level": "minimal"})
    assert len(attempts) == 1
    assert "thinking_level" not in attempts[0].get("generation_config", {})


def test_unavailable_model_is_dropped_from_the_catalogue():
    """Gemini 2.5 is advertised by models.list but 404s for new users."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return json_response(fixture("models_list.json"))
        return json_response({"error": {
            "message": "This model models/gemini-2.5-flash is no longer "
                       "available to new users. Please update your code to use "
                       "models/gemini-3.8-flash.", "code": "not_found"}},
            status=404)

    provider = make_provider(recording(handler))
    provider.list_models(force_refresh=True)
    with pytest.raises(ProviderError) as info:
        provider.send_message("gemini-2.5-flash", [ContentPart(text="hi")])
    assert info.value.category == ErrorCategory.MODEL_UNAVAILABLE
    ids = [m.model_id for m in provider.list_models()]
    assert "gemini-2.5-flash" not in ids


def test_verify_model_uses_the_free_count_tokens_probe():
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if ":countTokens" in request.url.path:
            if "gemini-2.5-flash" in request.url.path:
                return json_response({"error": {
                    "message": "This model is no longer available to new users.",
                    "code": "not_found"}}, status=404)
            return json_response(fixture("count_tokens.json"))
        return json_response(fixture("models_list.json"))

    provider = make_provider(recording(handler))
    assert provider.verify_model("gemini-3.8-flash") is True
    assert provider.verify_model("gemini-2.5-flash") is False
    assert all(":countTokens" in c or c.endswith("/models") for c in calls)
    # The unusable model is remembered as unavailable.
    provider.list_models(force_refresh=True)
    assert "gemini-2.5-flash" not in [m.model_id for m in
                                      provider.list_models()]


def test_stream_idle_timeout_exceeds_unary_read_timeout():
    """Live evidence (2026-09-29): a model stayed silent 75 s before the first
    SSE byte. The stream timeout must not be the unary one."""
    from core.constants import DEFAULT_STREAM_READ_TIMEOUT

    provider = make_provider(recording(
        lambda request: json_response(fixture("models_list.json"))))
    assert provider.client.timeout.read == 120.0
    assert provider.client.stream_timeout.read >= DEFAULT_STREAM_READ_TIMEOUT
    assert provider.client.stream_timeout.read > provider.client.timeout.read
    provider.configure(read_timeout=30.0, stream_read_timeout=240.0)
    assert provider.client.timeout.read == 30.0
    assert provider.client.stream_timeout.read == 240.0
