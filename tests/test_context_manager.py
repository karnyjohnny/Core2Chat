"""Context manager tests: windowing, pinning, budgeting, compaction."""

import pytest

from core.context_manager import COMPACT_NOTICE, ContextManager
from models.attachment_models import Attachment, AttachmentKind, AttachmentStatus
from models.chat_models import PinnedContext, StateMode
from models.message_models import ContentPart, ContentPartType, Message, Role
from models.provider_models import ModelCapabilities, ModelInfo


def make_model(input_limit: int = 100_000, model_id: str = "gemini-3.8-flash",
               min_cached: int = 4096) -> ModelInfo:
    return ModelInfo(model_id=model_id, display_name=model_id,
                     input_token_limit=input_limit, output_token_limit=65536,
                     supported_methods=["generateContent", "countTokens"],
                     thinking=True,
                     capabilities=ModelCapabilities({"text_input": True,
                                                     "streaming": True},
                                                    min_cached))


def history(count: int, prefix: str = "m", words: int = 20):
    out = []
    for index in range(count):
        role = Role.USER if index % 2 == 0 else Role.ASSISTANT
        text = " ".join("%s%d-w%d" % (prefix, index, w) for w in range(words))
        message = Message(id=index + 1, session_id=1, role=role, text=text,
                          timestamp=index)
        out.append(message)
    return out


def test_current_message_is_always_included():
    manager = ContextManager(sliding_window=4, count_before_send=False)
    current = Message.user_text("To jest najważniejsze pytanie")
    result = manager.build(make_model(), current, history(50))
    joined = "\n".join(p.text for p in result.parts)
    assert "To jest najważniejsze pytanie" in joined
    assert result.parts[-1].text == "To jest najważniejsze pytanie"
    assert result.parts[-1].metadata.get("current") is True


def test_sliding_window_keeps_recent_and_reports_dropped():
    manager = ContextManager(sliding_window=6, count_before_send=False)
    result = manager.build(make_model(), Message.user_text("nowe"),
                           history(40))
    assert result.info.included_messages == 6
    assert len(result.dropped_messages) == 34
    assert result.info.compacted_messages == 34
    assert any("Kontekst skrócony" in w for w in result.warnings)
    assert any(COMPACT_NOTICE.split("%d")[0] in p.text for p in result.parts)


def test_window_of_zero_history_sends_only_current_turn():
    manager = ContextManager(sliding_window=10, count_before_send=False)
    result = manager.build(make_model(), Message.user_text("pierwsze pytanie"),
                           [])
    assert result.info.included_messages == 0
    assert result.dropped_messages == []
    assert len(result.parts) == 1


def test_pinned_context_is_kept_and_marked_static():
    manager = ContextManager(sliding_window=2, count_before_send=False)
    pinned = [PinnedContext(id=1, label="Zasady", text="Zawsze po polsku",
                            enabled=True, tokens=4),
              PinnedContext(id=2, label="Wyłączone", text="ignoruj",
                            enabled=False)]
    result = manager.build(make_model(), Message.user_text("cześć"),
                           history(20), pinned=pinned)
    statics = [p for p in result.parts if p.metadata.get("static")]
    assert len(statics) == 1
    assert "Zawsze po polsku" in statics[0].text
    assert "ignoruj" not in "\n".join(p.text for p in result.parts)
    assert result.info.pinned_items == 1
    assert result.info.pinned_tokens == 4


def test_system_instruction_is_separate_and_never_trimmed():
    manager = ContextManager(sliding_window=2, count_before_send=False)
    result = manager.build(make_model(), Message.user_text("hi"), history(30),
                           system_instruction="Jesteś zwięzły.")
    assert result.system_instruction == "Jesteś zwięzły."
    assert result.info.system_instruction_tokens > 0
    assert all("Jesteś zwięzły." not in p.text for p in result.parts)


def test_stateful_mode_sends_only_current_turn():
    manager = ContextManager(sliding_window=50, count_before_send=False,
                             state_mode=StateMode.STATEFUL)
    result = manager.build(make_model(), Message.user_text("kolejne pytanie"),
                           history(30), remote_interaction_id="v1_prev")
    assert result.info.state_mode == StateMode.STATEFUL
    assert result.info.included_messages == 1
    assert result.info.excluded_messages == 30
    assert len([p for p in result.parts if p.metadata.get("history")]) == 0
    assert any("Tryb stanowy" in w for w in result.info.warnings)


def test_local_mode_without_remote_id_falls_back_to_history():
    manager = ContextManager(sliding_window=5, count_before_send=False,
                             state_mode=StateMode.STATEFUL)
    result = manager.build(make_model(), Message.user_text("hi"), history(10),
                           remote_interaction_id="")
    assert result.info.included_messages == 5


def test_token_counting_uses_provider_when_enabled():
    calls = []

    def counter(model_id, parts, system_instruction):
        calls.append((model_id, len(parts), system_instruction))
        return 1234

    manager = ContextManager(sliding_window=4, count_before_send=True,
                             token_counter=counter)
    result = manager.build(make_model(), Message.user_text("hi"), history(6),
                           system_instruction="sys")
    assert calls and calls[0][0] == "gemini-3.8-flash"
    assert calls[0][2] == "sys"
    assert result.info.counted_tokens == 1234
    assert result.info.effective_tokens == 1234


def test_counting_failure_degrades_to_local_estimate():
    def boom(model_id, parts, system_instruction):
        raise RuntimeError("offline")

    manager = ContextManager(sliding_window=4, count_before_send=True,
                             token_counter=boom)
    result = manager.build(make_model(), Message.user_text("hi"), history(6))
    assert result.info.counted_tokens is None
    assert result.info.estimated_tokens > 0
    assert any("szacunku lokalnego" in w for w in result.info.warnings)


def test_counting_disabled_saves_a_request():
    calls = []

    def counter(model_id, parts, system_instruction):
        calls.append(1)
        return 999

    manager = ContextManager(count_before_send=False, token_counter=counter)
    result = manager.build(make_model(), Message.user_text("hi"), history(3))
    assert calls == []
    assert result.info.counted_tokens is None


def test_budget_warnings_use_the_model_limit_not_a_constant():
    manager = ContextManager(limit_percent=85, count_before_send=False)
    small = make_model(input_limit=200)
    result = manager.build(small, Message.user_text("x " * 2000), [])
    assert result.info.estimated_tokens > 200
    assert any("przekracza limit" in w for w in result.info.warnings)

    large = make_model(input_limit=1_000_000)
    ok = manager.build(large, Message.user_text("krótkie pytanie"), [])
    assert ok.info.warnings == []
    assert ok.info.percent < 1.0


def test_shrink_drops_oldest_and_re_measures():
    manager = ContextManager(sliding_window=40, limit_percent=50,
                             count_before_send=False)
    model = make_model(input_limit=1200)
    result = manager.build(model, Message.user_text("nowe pytanie"),
                           history(40, words=30))
    assert any("przekracza limit" in w for w in result.info.warnings)
    shrunk = manager.shrink(result, history(40, words=30), drop_ratio=0.6)
    assert shrunk.info.effective_tokens < result.info.effective_tokens
    assert any("usuniętych" in w for w in shrunk.warnings)
    # The user's latest request survives every reduction.
    joined = "\n".join(p.text for p in shrunk.parts)
    assert "nowe pytanie" in joined


def test_attachments_are_included_with_headers_and_limits():
    manager = ContextManager(sliding_window=4, count_before_send=False,
                             max_attachment_chars=40)
    text_attachment = Attachment(
        id=7, filename="skrypt.py", mime_type="text/x-python",
        kind=AttachmentKind.TEXT, size_bytes=1000,
        status=AttachmentStatus.READY, language="python",
        payload=("x = 1\n" * 200).encode("utf-8"))
    image_attachment = Attachment(
        id=8, filename="zrzut.png", mime_type="image/png",
        kind=AttachmentKind.IMAGE, size_bytes=2048,
        status=AttachmentStatus.READY, payload=b"\x89PNG\r\n\x1a\n")
    result = manager.build(make_model(), Message.user_text("spójrz"),
                           [], attachments=[text_attachment, image_attachment])
    kinds = [p.type for p in result.parts]
    assert ContentPartType.IMAGE in kinds
    text_part = [p for p in result.parts
                 if p.metadata.get("attachment_id") == 7][0]
    assert "skrypt.py" in text_part.text
    assert len(text_part.text) < 200            # truncated to the limit
    assert result.info.attachment_bytes == 3048   # 1000 + 2048
    assert result.info.attachment_tokens > 0
    assert any("przycięty" in w for w in result.info.warnings)


def test_unsupported_and_failed_attachments_are_skipped():
    manager = ContextManager(count_before_send=False)
    failed = Attachment(id=9, filename="zly.pdf", kind=AttachmentKind.DOCUMENT,
                        status=AttachmentStatus.FAILED, payload=b"x")
    result = manager.build(make_model(), Message.user_text("hi"), [],
                           attachments=[failed])
    assert len(result.parts) == 1


def test_remote_file_reference_is_used_when_no_inline_payload():
    manager = ContextManager(count_before_send=False)
    attachment = Attachment(id=10, filename="duzy.pdf",
                            mime_type="application/pdf",
                            kind=AttachmentKind.DOCUMENT,
                            status=AttachmentStatus.UPLOADED,
                            remote_uri="https://api/files/abc")
    result = manager.build(make_model(), Message.user_text("streść"), [],
                           attachments=[attachment])
    doc_parts = [p for p in result.parts
                 if p.type == ContentPartType.DOCUMENT]
    assert doc_parts and doc_parts[0].uri == "https://api/files/abc"
    assert doc_parts[0].data == b""


def test_parts_are_cache_friendly_static_first():
    manager = ContextManager(sliding_window=3, count_before_send=False)
    pinned = [PinnedContext(id=1, label="Zasady", text="statyczne", tokens=2,
                            enabled=True)]
    result = manager.build(make_model(), Message.user_text("pytanie"),
                           history(6), pinned=pinned)
    assert result.parts[0].metadata.get("static") is True
    assert result.static_parts == [result.parts[0]]
    # A compaction notice is inserted after the static prefix, never before.
    compacted = manager.build(make_model(), Message.user_text("pytanie"),
                              history(60), pinned=pinned)
    assert compacted.parts[0].metadata.get("static") is True
    notices = [i for i, p in enumerate(compacted.parts)
               if p.metadata.get("compaction")]
    assert notices and notices[0] >= 1


def test_duplicate_history_is_not_sent_twice():
    """The just-inserted user message must not appear twice in the input."""
    manager = ContextManager(sliding_window=10, count_before_send=False)
    current = Message(id=99, role=Role.USER, text="unikalne pytanie")
    prior = history(4) + [current]
    filtered = [m for m in prior if m.id != current.id]
    result = manager.build(make_model(), current, filtered)
    joined = "\n".join(p.text for p in result.parts)
    assert joined.count("unikalne pytanie") == 1


def test_context_info_summary_is_serialisable():
    from core.context_manager import summarize_context_info
    import json

    manager = ContextManager(sliding_window=2, count_before_send=False)
    result = manager.build(make_model(), Message.user_text("hi"), history(10))
    payload = summarize_context_info(result.info)
    assert json.dumps(payload)
    assert payload["model_id"] == "gemini-3.8-flash"
    assert payload["input_limit"] == 100_000
    assert payload["cache_mode"] == "implicit"
