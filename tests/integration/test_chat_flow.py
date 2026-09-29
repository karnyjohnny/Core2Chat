"""Integration tests: GUI + services + fake provider, real SQLite, real Qt.

Everything runs offscreen; no network access and no modal dialogs are opened.
"""

import os
import time

import pytest

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
QtCore = pytest.importorskip("PyQt5.QtCore")

from PyQt5.QtCore import QCoreApplication, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from models.message_models import MessageStatus, Role  # noqa: E402
from models.provider_models import ErrorCategory, ProviderError  # noqa: E402
from models.usage_models import TokenUsage  # noqa: E402
from services.app_context import AppContext  # noqa: E402
from services.chat_service import ChatService  # noqa: E402
from services.export_import import ExportService, ImportService  # noqa: E402
from services.session_service import SessionService  # noqa: E402
from tests.fixtures.fake_provider import FakeProvider, make_model  # noqa: E402


@pytest.fixture
def app(qapp):
    QCoreApplication.setOrganizationName("Core2ChatTest")
    QCoreApplication.setApplicationName("Core2ChatTest")
    return qapp


@pytest.fixture
def stack(tmp_path, monkeypatch, app):
    """Hermetic AppContext + services, with a scriptable provider."""
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("CORE2CHAT_DATA_DIR", data_dir)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    context = AppContext()
    context.initialize()
    provider = FakeProvider()
    context.registry.register(provider)
    chat = ChatService(context)
    sessions = SessionService(context)
    yield {
        "context": context, "chat": chat, "sessions": sessions,
        "provider": provider, "data_dir": data_dir,
        "exporter": ExportService(context), "importer": ImportService(context),
        "tmp_path": tmp_path,
    }
    chat.cancel_all()
    context.shutdown()


def pump(app, milliseconds=120):
    """Process queued cross-thread signals like a real event loop would."""
    deadline = time.monotonic() + milliseconds / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_until(predicate, app, timeout_ms=4000, interval=0.01):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    return False


# ------------------------------------------------------------------ end to end
def test_send_message_streams_and_persists(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["Cześć", ", ", "świecie!"]
    session = sessions.create(model_id="gemini-3.8-flash")

    deltas = []
    chat.text_delta.connect(lambda sid, mid, text, rid: deltas.append(text))
    finalized = []
    chat.message_finalized.connect(lambda sid, mid, status, rid:
                                   finalized.append(status))

    request = chat.send_message(session, "Napisz powitanie")
    assert request is not None
    assert wait_until(lambda: bool(finalized), app)
    pump(app)

    assert deltas == ["Cześć", ", ", "świecie!"]
    assert finalized == [MessageStatus.COMPLETED]

    stored = context.messages.get(request.message_id)
    assert stored is not None
    assert stored.text == "Cześć, świecie!"
    assert stored.status == MessageStatus.COMPLETED
    assert stored.role == Role.ASSISTANT
    assert stored.interaction_id.startswith("v1_fake_")
    assert stored.usage.total_tokens == 21
    assert stored.usage.thought_tokens == 3

    # The user turn is persisted too, and history order is stable.
    history, total, _oldest = sessions.history_page(int(session.id))
    assert total == 2
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].text == "Napisz powitanie"

    # Token statistics landed in the ledger.
    stats = context.stats.window("last_hour")
    assert stats.requests == 1 and stats.total_tokens == 21


def test_provider_receives_context_and_system_instruction(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    session = sessions.create(model_id="gemini-3.8-flash")
    request = chat.send_message(session, "Pytanie",
                                system_instruction="Bądź zwięzły.")
    assert wait_until(lambda: context.messages.get(request.message_id).status
                      != MessageStatus.STREAMING, app)
    stream_calls = provider.calls_of("stream")
    assert len(stream_calls) == 1
    assert stream_calls[0]["system"] == "Bądź zwięzły."
    joined = " ".join(stream_calls[0]["contents"])
    assert "Pytanie" in joined


def test_count_tokens_is_called_before_send_when_enabled(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    context.settings.count_tokens_before_send = True
    context.context_managers.clear()
    session = sessions.create(model_id="gemini-3.8-flash")
    request = chat.send_message(session, "Ile to tokenów?")
    assert wait_until(lambda: context.messages.get(request.message_id).status
                      != MessageStatus.STREAMING, app)
    assert provider.count_calls, "countTokens must be used for the pre-flight"
    assert provider.count_calls[0]["model"] == "gemini-3.8-flash"


def test_cancellation_stops_stream_and_marks_message(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["a"] * 200
    provider.delay = 0.01
    session = sessions.create(model_id="gemini-3.8-flash")
    cancelled = []
    chat.generation_cancelled.connect(lambda sid, mid, rid: cancelled.append(mid))

    request = chat.send_message(session, "długi tekst")
    assert wait_until(lambda: chat.is_busy(int(session.id)), app, 2000)
    assert chat.stop_generation(int(session.id)) is True
    assert wait_until(lambda: bool(cancelled), app)
    pump(app)

    stored = context.messages.get(request.message_id)
    assert stored.status == MessageStatus.CANCELLED
    assert len(stored.text) < 200                 # stopped before the end
    assert chat.is_busy(int(session.id)) is False


def test_failure_is_recorded_with_normalised_error(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.fail_with = ProviderError(
        category=ErrorCategory.RATE_LIMIT, code="429", http_status=429,
        message_user="Limit API został osiągnięty. Spróbuj ponownie za chwilę.",
        message_debug="HTTP 429", retryable=True, provider="gemini")
    session = sessions.create(model_id="gemini-3.8-flash")
    errors = []
    chat.generation_failed.connect(lambda sid, mid, err, rid: errors.append(err))
    request = chat.send_message(session, "coś")
    assert wait_until(lambda: bool(errors), app)
    pump(app)
    assert errors[0].category == ErrorCategory.RATE_LIMIT
    stored = context.messages.get(request.message_id)
    assert stored.status == MessageStatus.FAILED
    assert stored.metadata.get("error", {}).get("category") == "RATE_LIMIT"
    assert context.stats.window("all_time").requests == 0   # nothing billed


def test_unknown_provider_event_does_not_break_the_turn(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.unknown_event = True
    session = sessions.create(model_id="gemini-3.8-flash")
    request = chat.send_message(session, "test")
    assert wait_until(lambda: context.messages.get(request.message_id).status
                      == MessageStatus.COMPLETED, app)
    assert context.messages.get(request.message_id).text == "Witaj świecie"


def test_thinking_summary_is_stored_separately(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.emit_thought = True
    session = sessions.create(model_id="gemini-3.8-flash")
    thoughts = []
    chat.thought_delta.connect(lambda sid, mid, text, rid: thoughts.append(text))
    request = chat.send_message(session, "pomyśl")
    assert wait_until(lambda: context.messages.get(request.message_id).status
                      == MessageStatus.COMPLETED, app)
    stored = context.messages.get(request.message_id)
    assert stored.thought_summary == "Analizuję pytanie."
    assert "Analizuję" not in stored.text        # never mixed into the answer
    assert thoughts == ["Analizuję pytanie."]


# -------------------------------------------------------------- race defence
def test_switching_sessions_does_not_mix_streams(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["x"] * 60
    provider.delay = 0.005
    first = sessions.create(title="Pierwsza", model_id="gemini-3.8-flash")
    second = sessions.create(title="Druga", model_id="gemini-3.8-flash")

    request_first = chat.send_message(first, "pytanie 1")
    assert wait_until(lambda: chat.is_busy(int(first.id)), app, 2000)
    # The user opens another chat while generation is still running.
    request_second = chat.send_message(second, "pytanie 2")
    assert request_second is not None
    assert wait_until(lambda: not chat.is_busy(int(first.id))
                      and not chat.is_busy(int(second.id)), app, 8000)

    stored_first = context.messages.get(request_first.message_id)
    stored_second = context.messages.get(request_second.message_id)
    assert stored_first.session_id == first.id
    assert stored_second.session_id == second.id
    assert stored_first.text and stored_second.text
    assert stored_first.status == MessageStatus.COMPLETED


def test_second_send_while_busy_is_rejected(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["y"] * 100
    provider.delay = 0.005
    session = sessions.create(model_id="gemini-3.8-flash")
    first = chat.send_message(session, "pierwsze")
    assert first is not None
    assert wait_until(lambda: chat.is_busy(int(session.id)), app, 2000)
    assert chat.send_message(session, "drugie") is None    # no duplicate send
    chat.stop_generation(int(session.id))
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app)
    total = context.messages.count(int(session.id))
    assert total == 2          # one user + one assistant message only


def test_stateful_mode_uses_previous_interaction_id(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    session = sessions.create(model_id="gemini-3.8-flash", state_mode="stateful")
    first = chat.send_message(session, "pierwsze")
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app)
    refreshed = sessions.get(int(session.id))
    assert refreshed.remote_interaction_id.startswith("v1_fake_")

    second = chat.send_message(refreshed, "drugie")
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app)
    stream_calls = provider.calls_of("stream")
    assert stream_calls[0]["previous"] == ""
    assert stream_calls[1]["previous"] == refreshed.remote_interaction_id
    # Stateful mode must not resend the whole transcript.
    assert len(stream_calls[1]["contents"]) <= 2


# ---------------------------------------------------------------- persistence
def test_history_survives_application_restart(stack, app, monkeypatch):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    session = sessions.create(title="Trwała", model_id="gemini-3.8-flash")
    request = chat.send_message(session, "zapamiętaj to")
    assert wait_until(lambda: context.messages.get(request.message_id).status
                      == MessageStatus.COMPLETED, app)
    session_id = int(session.id)
    db_path = context.db.path
    context.shutdown()

    monkeypatch.setenv("CORE2CHAT_DATA_DIR", os.path.dirname(db_path))
    reopened = AppContext()
    reopened.initialize()
    try:
        restored = reopened.sessions.get(session_id)
        assert restored is not None and restored.title == "Trwała"
        history = reopened.messages.page(session_id, limit=10)
        assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
        assert history[1].text == "Witaj świecie"
        assert history[1].usage.total_tokens == 21
        assert reopened.stats.window("all_time").requests == 1
        assert reopened.db.journal_mode == "wal"
    finally:
        reopened.shutdown()


def test_draft_is_persisted_and_restored(stack, app):
    context, sessions = stack["context"], stack["sessions"]
    session = sessions.create(title="Szkic")
    sessions.save_draft(int(session.id), "nie wysłane jeszcze")
    assert sessions.load_draft(int(session.id)) == "nie wysłane jeszcze"
    reloaded = sessions.get(int(session.id))
    assert reloaded.draft == "nie wysłane jeszcze"


def test_fork_shares_prefix_and_continues_independently(stack, app):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    source = sessions.create(title="Oryginał", model_id="gemini-3.8-flash")
    request = chat.send_message(source, "pierwsze pytanie")
    assert wait_until(lambda: not chat.is_busy(int(source.id)), app)
    messages, _total, _oldest = sessions.history_page(int(source.id))
    fork = sessions.fork(int(source.id), messages[-1].id, title="Gałąź")
    assert fork.parent_session_id == source.id
    fork_messages, fork_total, _ = sessions.history_page(int(fork.id))
    assert fork_total == 2
    assert fork_messages[0].text == "pierwsze pytanie"
    # The fork must not inherit remote provider state.
    assert fork.remote_interaction_id == ""
    assert sessions.get(int(source.id)).remote_interaction_id != ""


# -------------------------------------------------------------- export/import
def test_export_markdown_and_json_roundtrip(stack, app, tmp_path):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    exporter, importer = stack["exporter"], stack["importer"]
    session = sessions.create(title="Eksport test", model_id="gemini-3.8-flash")
    request = chat.send_message(session, "pytanie eksportowe")
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app)

    md_path = str(tmp_path / "out.md")
    exporter.export(int(session.id), md_path, "md")
    content = open(md_path, encoding="utf-8").read()
    assert "# Eksport test" in content
    assert "pytanie eksportowe" in content
    assert "Witaj świecie" in content

    json_path = str(tmp_path / "out.json")
    exporter.export(int(session.id), json_path, "json")
    imported = importer.import_file(json_path)
    messages, total, _ = sessions.history_page(int(imported.id))
    assert total == 2
    assert messages[0].text == "pytanie eksportowe"
    assert messages[1].text == "Witaj świecie"
    assert "import" in imported.title


def test_export_html_and_text_formats(stack, app, tmp_path):
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    exporter = stack["exporter"]
    session = sessions.create(title="Formaty")
    request = chat.send_message(session, "cześć")
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app)
    html_path = str(tmp_path / "out.html")
    exporter.export(int(session.id), html_path, "html")
    html = open(html_path, encoding="utf-8").read()
    assert "<html" in html and "cześć" in html
    txt_path = str(tmp_path / "out.txt")
    exporter.export(int(session.id), txt_path, "txt")
    assert "Użytkownik" in open(txt_path, encoding="utf-8").read()


def test_export_rejects_foreign_schema(stack, tmp_path):
    importer = stack["importer"]
    path = tmp_path / "fake.json"
    path.write_text('{"schema": "something-else", "version": 1}',
                    encoding="utf-8")
    with pytest.raises(Exception) as info:
        importer.import_file(str(path))
    assert "Core2Chat" in str(info.value)


# ------------------------------------------------------------------- GUI layer
def test_main_window_renders_streaming_reply(stack, app):
    from gui.main_window import MainWindow

    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["Hej", "!", "\n\n```python\nx=1\n```"]
    window = MainWindow(context, chat, sessions, stack["exporter"],
                        stack["importer"])
    window.reload_sidebar()
    session = window.new_chat()
    assert session is not None
    window.show()
    app.processEvents()

    window.input.set_text("Pokaż kod")
    window.send_message("Pokaż kod")
    assert wait_until(lambda: window.chat_widget.message_count() >= 2, app, 4000)
    assert wait_until(lambda: not chat.is_busy(int(session.id)), app, 6000)
    pump(app)

    ids = window.chat_widget.message_ids
    assistant_widget = None
    for message_id in ids:
        widget = window.chat_widget.widget_for(message_id)
        if widget is not None and widget.message.role == Role.ASSISTANT:
            assistant_widget = widget
    assert assistant_widget is not None
    assert "Hej!" in assistant_widget.message.text
    assert assistant_widget.message.status == MessageStatus.COMPLETED
    # Code blocks are exposed for the copy action.
    assert assistant_widget.copy_code_block(0) is True
    assert QApplication.clipboard().text().strip() == "x=1"
    window.close()
    window.deleteLater()


def test_main_window_stop_button_reflects_state(stack, app):
    from gui.main_window import MainWindow
    from models.chat_models import GenerationState

    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["z"] * 150
    provider.delay = 0.008
    window = MainWindow(context, chat, sessions, stack["exporter"],
                        stack["importer"])
    window.reload_sidebar()
    session = window.new_chat()
    window.show()
    app.processEvents()
    assert window.send_button.text() == "Wyślij"

    window.send_message("długa odpowiedź")
    assert wait_until(lambda: window.send_button.text() == "Stop", app, 4000)
    window._on_send_clicked()          # pressing again means Stop
    assert wait_until(lambda: window.send_button.text() != "Stop", app, 6000)
    stored_status = None
    for message_id in window.chat_widget.message_ids:
        widget = window.chat_widget.widget_for(message_id)
        if widget is not None and widget.message.role == Role.ASSISTANT:
            stored_status = context.messages.get(message_id).status
    assert stored_status == MessageStatus.CANCELLED
    window.close()
    window.deleteLater()


def test_model_refresh_populates_selector(stack, app):
    from gui.main_window import MainWindow

    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.models = [make_model("gemini-3.8-flash"),
                       make_model("gemini-3.5-flash", input_limit=500_000)]
    window = MainWindow(context, chat, sessions, stack["exporter"],
                        stack["importer"])
    window.show()
    app.processEvents()
    window.refresh_models(force=True)
    assert wait_until(lambda: window.model_selector.model_count() >= 2, app, 4000)
    ids = [window.model_selector._combo.itemData(i)
           for i in range(window.model_selector.model_count())]
    assert "gemini-3.8-flash" in ids
    window.close()
    window.deleteLater()


def test_paged_history_loading_keeps_widget_count_bounded(stack, app):
    from gui.main_window import MainWindow
    from core.constants import MESSAGES_PAGE_SIZE

    context, sessions = stack["context"], stack["sessions"]
    session = sessions.create(title="Duża historia")
    batch = [__import__("models.message_models", fromlist=["Message"]).Message(
        role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
        text="wiadomość %d" % i, session_id=int(session.id), timestamp=i)
        for i in range(400)]
    context.messages.insert_many(batch)

    window = MainWindow(context, ChatService(context), sessions,
                        stack["exporter"], stack["importer"])
    window.reload_sidebar()
    window.open_session(int(session.id))
    window.show()
    app.processEvents()
    assert window.chat_widget.message_count() == MESSAGES_PAGE_SIZE
    assert window.chat_widget.has_load_more() is True
    window.load_older_messages()
    app.processEvents()
    assert window.chat_widget.message_count() == MESSAGES_PAGE_SIZE * 2
    window.close()
    window.deleteLater()


def test_stop_is_immediate_even_while_the_provider_is_silent(stack, app):
    """The UI must not wait for the socket: partial text is saved at once."""
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["częściowa ", "odpowiedź"] + ["x"] * 500
    provider.delay = 0.02
    session = sessions.create(model_id="gemini-3.8-flash")

    request = chat.send_message(session, "długa odpowiedź")
    # During streaming the partial answer lives in the request buffer; SQLite is
    # written once, at finalisation (no per-chunk I/O).
    assert wait_until(lambda: request.text_value, app, 4000)
    started = time.monotonic()
    assert chat.stop_generation(int(session.id)) is True
    latency_ms = (time.monotonic() - started) * 1000.0
    pump(app)

    stored = context.messages.get(request.message_id)
    assert stored.status == MessageStatus.CANCELLED
    assert stored.text, "partial answer must be persisted"
    assert len(stored.text) < len("".join(provider.chunks))
    # Finalisation is synchronous: the user sees the result without waiting
    # for the socket to be released by the provider.
    assert latency_ms < 500, "stop took %.0f ms" % latency_ms
    assert chat.is_busy(int(session.id)) is False


def test_late_events_after_stop_are_ignored(stack, app):
    """Race defence: a detached request must not write into the message."""
    context, chat, sessions = stack["context"], stack["chat"], stack["sessions"]
    provider = stack["provider"]
    provider.chunks = ["a"] * 300
    provider.delay = 0.01
    session = sessions.create(model_id="gemini-3.8-flash")
    request = chat.send_message(session, "test")
    assert wait_until(lambda: request.text_value, app, 3000)
    chat.stop_generation(int(session.id))
    text_at_stop = context.messages.get(request.message_id).text
    pump(app, 300)
    assert context.messages.get(request.message_id).text == text_at_stop
    assert context.messages.get(request.message_id).status == \
        MessageStatus.CANCELLED
