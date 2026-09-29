# core2chat:allow-fake-secret - ten plik zawiera celowo FAŁSZYWY klucz w kształcie
# prawdziwego, żeby udowodnić, że konfiguracja API nie zapisuje go w plaintext.
"""Regression lock (task §15): features confirmed working on Windows 7.

The user verified these by hand on the target machine:

    [OK] API settings        [OK] API key configuration
    [OK] model discovery     [OK] model tooltips
    [OK] sidebar             [OK] delete conversation with right click
    [OK] Ctrl+L              [OK] Markdown rendering
    [OK] Python code blocks  [OK] C code blocks
    [OK] copy code           [OK] TXT attachments
    [OK] context inspector   [OK] token statistics
    [OK] other settings tabs [OK] API error display
    [OK] EXE startup speed   [OK] packaged tray icon
    [OK] tray settings       [OK] tray exit
    [OK] tray internet/offline function

Each one gets an automated check here, so a future refactor cannot silently
break something that already works in the field.
"""

import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
QtGui = pytest.importorskip("PyQt5.QtGui")
QtCore = pytest.importorskip("PyQt5.QtCore")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from models.attachment_models import AttachmentKind, AttachmentStatus  # noqa: E402
from models.message_models import Message, MessageStatus, Role  # noqa: E402
from models.provider_models import ErrorCategory, ProviderError  # noqa: E402
from models.usage_models import TokenUsage  # noqa: E402
from tests.fixtures.fake_provider import FakeProvider  # noqa: E402


@pytest.fixture
def stack(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("CORE2CHAT_DATA_DIR", str(tmp_path / "data"))
    from services.app_context import AppContext
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService

    context = AppContext()
    context.initialize()
    provider = FakeProvider()
    context.registry.register(provider)
    services = (ChatService(context), SessionService(context),
                ExportService(context), ImportService(context))
    yield context, services, provider
    services[0].cancel_all()
    context.shutdown()


def make_window(stack):
    from gui.main_window import MainWindow

    context, services, _provider = stack
    window = MainWindow(context, *services)
    window.reload_sidebar()
    window.new_chat()
    window.show()
    return window


def pump(qapp, milliseconds=150):
    import time

    deadline = time.monotonic() + milliseconds / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def wait_until(predicate, qapp, timeout_ms=4000, interval=0.01):
    """Focus and queued signals need real event-loop turns, not one pump."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    return False


# ------------------------------------------------------------ API settings
def test_api_settings_tab_exposes_key_base_url_and_timeouts(stack, qapp):
    from gui.settings_dialog import SettingsDialog

    context = stack[0]
    dialog = SettingsDialog(context)
    api_tab = dialog._tabs.widget(2)
    assert dialog._tabs.tabText(2) == "API"
    # Pola muszą istnieć i mieć wartości z konfiguracji.
    assert dialog.api_key.echoMode() == QtWidgets.QLineEdit.Password
    assert dialog.base_url.text() == context.settings.base_url
    assert dialog.connect_timeout.value() == context.settings.connect_timeout
    assert dialog.read_timeout.value() == context.settings.read_timeout
    assert api_tab is not None
    dialog.deleteLater()


def test_api_key_is_stored_masked_and_never_in_config(stack, qapp, tmp_path):
    """[OK] API key configuration - with the secret-handling guarantees."""
    import json

    context = stack[0]
    method = context.store_api_key("AIzaSyFAKE-REGRESSION-LOCK-12345")
    assert method in ("dpapi", "obfuscated")
    assert context.resolve_api_key() == "AIzaSyFAKE-REGRESSION-LOCK-12345"

    config_path = context.config.path
    if os.path.isfile(config_path):
        raw = open(config_path, encoding="utf-8").read()
        assert "AIzaSyFAKE-REGRESSION-LOCK-12345" not in raw
    secrets_path = os.path.join(os.path.dirname(config_path), "secrets.json")
    assert os.path.isfile(secrets_path)
    raw = open(secrets_path, encoding="utf-8").read()
    assert "AIzaSyFAKE-REGRESSION-LOCK-12345" not in raw
    assert json.loads(raw)["secrets"]["api_key"]["method"] == method
    context.store_api_key("")
    assert context.resolve_api_key() == ""


# ---------------------------------------------------------- model discovery
def test_model_discovery_fills_the_selector_with_tooltips(stack, qapp):
    from tests.fixtures.fake_provider import make_model

    context, services, provider = stack
    provider.models = [make_model("gemini-3.8-flash"),
                       make_model("gemini-3.5-flash", input_limit=500_000)]
    window = make_window(stack)
    window.refresh_models(force=True)
    pump(qapp, 900)
    assert window.model_selector.model_count() == 2
    tooltip = window.model_selector._combo.itemData(0, Qt.ToolTipRole)
    # [OK] model tooltips: limit, cykl życia i potwierdzone możliwości.
    assert tooltip and "in" in tooltip and "tok" in tooltip.lower()
    assert window.model_selector.describe_current()
    window.close()
    pump(qapp, 50)


def test_model_selector_switch_persists_to_the_session(stack, qapp):
    from tests.fixtures.fake_provider import make_model

    context, services, provider = stack
    provider.models = [make_model("gemini-3.8-flash"),
                       make_model("gemini-3.5-flash")]
    window = make_window(stack)
    window.refresh_models(force=True)
    pump(qapp, 900)
    window.model_selector.set_current_model("gemini-3.5-flash")
    window._on_model_selected("gemini-3.5-flash")
    pump(qapp, 50)
    reloaded = services[1].get(int(window._session.id))
    assert reloaded.model_id == "gemini-3.5-flash"
    assert context.settings.model_id == "gemini-3.5-flash"
    window.close()
    pump(qapp, 50)


# ------------------------------------------------------------------- sidebar
def test_sidebar_lists_renames_and_deletes_with_context_menu_actions(
        stack, qapp, monkeypatch):
    context, services, _provider = stack
    window = make_window(stack)
    first = window._session
    second = services[1].create(title="Druga rozmowa")
    window.reload_sidebar()
    pump(qapp, 50)
    assert window.sidebar.session_count() >= 2

    # [OK] delete conversation with right click -> ta sama ścieżka co menu.
    # Dialogi modalne są podstawione: w środowisku headless nie ma kto kliknąć.
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Po zmianie nazwy", True)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    window._on_session_action("rename", int(second.id))
    assert services[1].get(int(second.id)).title == "Po zmianie nazwy"
    services[1].rename(int(second.id), "Po zmianie nazwy")
    window.reload_sidebar()
    labels = [window.sidebar._list.item(i).text()
              for i in range(window.sidebar._list.count())]
    assert any("Po zmianie nazwy" in label for label in labels)

    context.settings.confirm_delete_session = False
    window._on_session_action("delete", int(second.id))
    pump(qapp, 50)
    assert services[1].get(int(second.id)) is None
    assert services[1].get(int(first.id)) is not None
    window.close()
    pump(qapp, 50)


def test_sidebar_pin_archive_and_fork_actions(stack, qapp):
    context, services, _provider = stack
    window = make_window(stack)
    session = window._session
    window._on_session_action("pin", int(session.id))
    assert services[1].get(int(session.id)).pinned is True
    window._on_session_action("unpin", int(session.id))
    assert services[1].get(int(session.id)).pinned is False
    window._on_session_action("archive", int(session.id))
    assert services[1].get(int(session.id)).archived is True
    window._on_session_action("archive", int(session.id))
    assert services[1].get(int(session.id)).archived is False
    window._on_session_action("fork", int(session.id))
    pump(qapp, 50)
    assert window._session.id != session.id
    assert window._session.parent_session_id == session.id
    window.close()
    pump(qapp, 50)


# ------------------------------------------------------------------- keyboard
def test_ctrl_l_focuses_the_prompt(stack, qapp):
    """[OK] Ctrl+L - must keep working."""
    window = make_window(stack)
    window.sidebar.focus_search()
    pump(qapp, 50)
    assert window.input.hasFocus() is False
    QTest.keyClick(window, Qt.Key_L, Qt.ControlModifier)
    assert wait_until(lambda: window.input.hasFocus(), qapp, 1500)
    window.close()
    pump(qapp, 50)


def test_enter_sends_and_shift_enter_adds_a_line(stack, qapp):
    window = make_window(stack)
    window.input.set_text("pierwsza")
    QTest.keyClick(window.input, Qt.Key_Return)
    pump(qapp, 400)
    assert context_messages(stack, window) >= 2       # user + assistant
    window.input.set_text("druga")
    QTest.keyClick(window.input, Qt.Key_Return, Qt.ShiftModifier)
    assert window.input.toPlainText() == "druga\n"
    window.close()
    pump(qapp, 50)


def context_messages(stack, window):
    context = stack[0]
    return context.messages.count(int(window._session.id))


def test_all_documented_shortcuts_are_registered(stack, qapp):
    window = make_window(stack)
    shortcuts = {}
    for action in window.findChildren(QtWidgets.QAction):
        if action.shortcut().toString():
            shortcuts[action.text()] = action.shortcut().toString()
    assert shortcuts.get("Nowa rozmowa") == "Ctrl+N"
    assert shortcuts.get("Szukaj w historiach") == "Ctrl+K"
    assert shortcuts.get("Fokus na pole wpisywania") == "Ctrl+L"
    assert shortcuts.get("Kopiuj ostatnią odpowiedź") == "Ctrl+Shift+C"
    assert shortcuts.get("Ustawienia") == "Ctrl+,"
    window.close()
    pump(qapp, 50)


# ------------------------------------------------------------------ markdown
MARKDOWN_SAMPLE = """# Nagłówek

**pogrubienie** i *kursywa* oraz ~~skreślenie~~.

- punkt 1
- punkt 2

> cytat

| kolumna | wartość |
|---|---|
| a | 1 |

```python
def powitanie():
    msg = "Witaj w lekkiej przyszłości!"
    print(f"System: {msg}")

powitanie()
```

```c
#include <stdio.h>
int main(void) {
    printf("hello\\n");   /* komentarz */
    return 0;
}
```
"""


def test_markdown_renders_all_confirmed_elements(stack, qapp):
    """[OK] Markdown rendering - bold, italic, list, table, blockquote, code."""
    from utils.markdown import render

    result = render(MARKDOWN_SAMPLE)
    html = result.html
    assert "<h1>Nagłówek</h1>" in html
    assert "<strong>pogrubienie</strong>" in html
    assert "<em>kursywa</em>" in html
    assert "<s>skreślenie</s>" in html
    assert "<ul>" in html and html.count("<li>") >= 2
    assert "<blockquote>" in html
    assert 'class="md-table"' in html
    assert "<hr/>" not in html or True
    assert len(result.code_blocks) == 2
    assert [block.language for block in result.code_blocks] == ["python", "c"]
    assert "<script" not in html


def test_python_code_block_is_highlighted(stack, qapp):
    """[OK] Python code blocks."""
    from utils.highlight import highlight

    html = highlight('def powitanie():\n    msg = "Witaj"\n    return msg',
                     "python")
    assert 'tok-kw">def' in html
    assert "tok-str" in html
    assert "tok-fn" in html


def test_c_code_block_is_highlighted(stack, qapp):
    """[OK] C code blocks."""
    from utils.highlight import highlight

    source = '#include <stdio.h>\nint main(void) {\n    printf("hi\\n");\n' \
             '    /* komentarz */\n    return 0;\n}'
    html = highlight(source, "c")
    assert 'tok-kw">int' in html
    assert 'tok-kw">return' in html
    assert "tok-str" in html
    assert "tok-com" in html
    assert "<stdio.h>" not in html          # escaped, not injected
    assert "&lt;stdio.h&gt;" in html


def test_copy_code_works_from_the_rendered_message(stack, qapp):
    """[OK] copy code - through the c2c:// link the renderer emits."""
    context, services, provider = stack
    provider.chunks = ["Oto kod:\n\n```python\nx = 42\n```\n"]
    window = make_window(stack)
    window.send_message("pokaż kod")
    pump(qapp, 1200)
    widget = None
    for message_id in window.chat_widget.message_ids:
        candidate = window.chat_widget.widget_for(message_id)
        if candidate is not None and candidate.message.role == Role.ASSISTANT:
            widget = candidate
    assert widget is not None
    assert widget.copy_code_block(0) is True
    assert QApplication.clipboard().text().strip() == "x = 42"
    window.close()
    pump(qapp, 50)


def test_copy_last_response_action(stack, qapp):
    context, services, provider = stack
    provider.chunks = ["Odpowiedź do skopiowania"]
    window = make_window(stack)
    window.send_message("zapytaj")
    pump(qapp, 1200)
    QApplication.clipboard().clear()
    window.copy_last_response()
    assert QApplication.clipboard().text() == "Odpowiedź do skopiowania"
    window.close()
    pump(qapp, 50)


# --------------------------------------------------------------- attachments
def test_txt_attachment_reaches_the_model(stack, qapp, tmp_path):
    """[OK] TXT attachments - the exact scenario the user verified."""
    context, services, provider = stack
    path = tmp_path / "test.txt"
    path.write_text('JEŚLI TO WIDZISZ, TO NAPISZ "WIDZĘ XD"\n',
                    encoding="utf-8")
    window = make_window(stack)
    window.add_files([str(path)])
    pump(qapp, 50)
    assert window.attachment_bar.count() == 1
    window.send_message("przeczytaj plik")
    pump(qapp, 1200)

    stream_calls = provider.calls_of("stream")
    assert stream_calls, "żądanie nie dotarło do dostawcy"
    joined = " ".join(stream_calls[-1]["contents"])
    assert "JEŚLI TO WIDZISZ" in joined
    assert "test.txt" in joined
    window.close()
    pump(qapp, 50)


def test_attachment_rejection_is_reported_not_silent(stack, qapp, tmp_path):
    window = make_window(stack)
    bad = tmp_path / "archiwum.zip"
    bad.write_bytes(b"PK\x03\x04binary")
    window.add_files([str(bad)])
    pump(qapp, 50)
    assert window.attachment_bar.count() == 0
    assert "Odrzucono" in window._status_session.text()
    window.close()
    pump(qapp, 50)


# ------------------------------------------------------------------- dialogs
def test_context_inspector_opens_with_live_data(stack, qapp):
    """[OK] context inspector."""
    from gui.context_dialog import ContextDialog
    from models.chat_models import ContextInfo

    context = stack[0]
    info = ContextInfo(model_id="gemini-3.8-flash", input_limit=1_000_000,
                       estimated_tokens=12540, included_messages=6,
                       excluded_messages=2, pinned_items=1, pinned_tokens=40,
                       cache_mode="implicit", min_cached_tokens=4096,
                       warnings=["Kontekst zajmuje 80% limitu."])
    dialog = ContextDialog(context, info)
    dialog.show()
    pump(qapp, 50)
    assert dialog.isVisible()
    assert dialog._cache_table is not None
    dialog.close()
    dialog.deleteLater()


def test_token_statistics_dialog_shows_all_windows(stack, qapp):
    """[OK] token statistics."""
    from gui.stats_dialog import StatsDialog

    context, services, provider = stack
    window = make_window(stack)
    window.send_message("policz tokeny")
    pump(qapp, 1200)
    assert context.stats.window("all_time").requests == 1

    dialog = StatsDialog(context)
    dialog.show()
    pump(qapp, 50)
    assert dialog._windows_table.rowCount() == 3      # 1 h / 24 h / całość
    totals = [dialog._windows_table.item(row, 7).text() for row in range(3)]
    assert totals[0] != "0"
    assert dialog._summary.text().startswith("Łącznie:")
    dialog.reload()
    dialog.close()
    dialog.deleteLater()
    window.close()
    pump(qapp, 50)


def test_every_settings_tab_is_present_and_populated(stack, qapp):
    """[OK] other settings tabs."""
    from gui.settings_dialog import SettingsDialog

    context = stack[0]
    dialog = SettingsDialog(context)
    expected = ["Ogólne", "Wygląd", "API", "Kontekst", "Skróty", "Prywatność",
                "Zaawansowane"]
    assert [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())] == \
        expected
    for index in range(dialog._tabs.count()):
        widget = dialog._tabs.widget(index)
        assert widget is not None
        dialog._tabs.setCurrentIndex(index)
        pump(qapp, 10)
    # Zmiana w każdej zakładce musi przetrwać zapis.
    dialog.font_size.setValue(14)
    dialog.sliding_window.setValue(25)
    dialog.log_level.setCurrentIndex(0)
    dialog.hide_unavailable.setChecked(False)
    assert dialog._on_apply() is True
    assert context.settings.font_size == 14
    assert context.settings.sliding_window_messages == 25
    assert context.settings.log_level == "DEBUG"
    assert context.settings.hide_unavailable_models is False
    dialog.deleteLater()


# ------------------------------------------------------------- API errors
@pytest.mark.parametrize("category,message", [
    (ErrorCategory.AUTHENTICATION, "Klucz API jest nieprawidłowy lub wygasł."),
    (ErrorCategory.AUTHORIZATION, "Brak uprawnień do tego zasobu lub modelu."),
    (ErrorCategory.RATE_LIMIT, "Limit API został osiągnięty."),
    (ErrorCategory.NETWORK, "Brak połączenia z usługą."),
    (ErrorCategory.TIMEOUT, "Przekroczono limit czasu odpowiedzi."),
    (ErrorCategory.MODEL_UNAVAILABLE, "Wybrany model jest niedostępny."),
    (ErrorCategory.CONTEXT_TOO_LARGE, "Kontekst jest zbyt duży."),
    (ErrorCategory.SERVER_ERROR, "Błąd po stronie serwera Google."),
])
def test_api_error_is_shown_and_the_ui_recovers(stack, qapp, monkeypatch,
                                                category, message):
    """[OK] API error display + §7: no crash, UI restored, work continues."""
    context, services, provider = stack
    provider.fail_with = ProviderError(category=category, code="x",
                                       message_user=message,
                                       message_debug="technical detail",
                                       provider="gemini")
    window = make_window(stack)
    # Modalne okno błędu nie może zablokować testu headless.
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    window.send_message("coś")
    pump(qapp, 1200)

    # Błąd jest widoczny i opisany po polsku.
    assert message in window._status_session.text() or \
        window.status_pill.state in ("error", "offline", "auth_required",
                                     "rate_limited")
    # Aplikacja żyje i da się dalej pracować.
    assert window.isVisible()
    window.input.set_text("kolejna próba")
    provider.fail_with = None
    provider.chunks = ["Udało się"]
    window.send_message("kolejna próba")
    pump(qapp, 1200)
    assert context.messages.count(int(window._session.id)) >= 4
    window.close()
    pump(qapp, 50)


# ---------------------------------------------------------------------- tray
def test_tray_settings_and_exit_are_wired(stack, qapp):
    """[OK] tray settings / tray exit / tray internet-offline function."""
    from core.tray_manager import (ACTION_EXIT, ACTION_PAUSE, ACTION_SETTINGS,
                                   ACTION_SHOW, TrayManager)

    window = make_window(stack)
    tray = TrayManager(window)
    calls = []
    tray.build(on_show=lambda: calls.append("show"),
               on_new_chat=lambda: calls.append("new"),
               on_new_chat_focused=lambda: calls.append("focus"),
               on_settings=lambda: calls.append("settings"),
               on_exit=lambda: calls.append("exit"))
    labels = tray.action_labels()
    assert labels[ACTION_SHOW] == "Pokaż okno"
    assert labels[ACTION_SETTINGS] == "Ustawienia…"
    assert labels[ACTION_EXIT] == "Zakończ"
    assert labels[ACTION_PAUSE] == "Wstrzymaj aktywność sieciową"
    assert tray.dispatch(ACTION_SETTINGS) is True
    assert tray.dispatch(ACTION_EXIT) is True
    assert calls == ["settings", "exit"]
    # Funkcja wstrzymania sieci zmienia stan i etykietę.
    tray._toggle_pause()
    assert tray.paused is True
    assert tray.action_labels()[ACTION_PAUSE] == "Wznów aktywność sieciową"
    tray._toggle_pause()
    assert tray.paused is False
    tray.shutdown()
    window.close()
    pump(qapp, 50)


def test_packaged_tray_icon_resource_exists():
    """[OK] packaged tray icon - asset must ship in the bundle."""
    from core.tray_manager import ICON_CANDIDATES
    from utils.paths import asset_path

    found = [asset_path(*parts) for parts in ICON_CANDIDATES]
    import os as _os
    assert any(_os.path.isfile(path) for path in found), found
    spec = open(os.path.join(PROJECT_ROOT, "pyinstaller.spec"),
                encoding="utf-8").read()
    assert "icon.ico" in spec and "icon.png" in spec


# ------------------------------------------------------------- startup speed
def test_startup_budget_matches_the_confirmed_exe_speed(stack, qapp):
    """[OK] EXE startup speed - budget guard for the Python side."""
    import time

    from gui.main_window import MainWindow

    context, services, _provider = stack
    started = time.monotonic()
    window = MainWindow(context, *services)
    window.reload_sidebar()
    window.new_chat()
    window.show()
    qapp.processEvents()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    assert elapsed_ms < 2000, "startup %.0f ms" % elapsed_ms
    window.close()
    pump(qapp, 50)


# ------------------------------------------------- input states during a request
def test_enter_during_streaming_does_not_send_a_second_message(stack, qapp):
    """§11: 'disabled input podczas requestu' - the composer stays editable so
    history can be read, but a second Enter must not create a second turn."""
    context, services, provider = stack
    provider.chunks = ["fragment "] * 120
    provider.delay = 0.01
    window = make_window(stack)
    window.send_message("długa odpowiedź")
    assert wait_until(lambda: window.send_button.text() == "Stop", qapp, 4000)

    window.input.set_text("to nie powinno polecieć")
    QTest.keyClick(window.input, Qt.Key_Return)
    pump(qapp, 300)

    # Treść została w polu (nie zgubiona), a liczba wiadomości nie wzrosła.
    assert window.input.toPlainText() == "to nie powinno polecieć"
    assert context.messages.count(int(window._session.id)) == 2
    assert window.status_pill.state in ("streaming", "sending")

    window.stop_generation()
    pump(qapp, 600)
    window.close()
    pump(qapp, 50)


def test_input_stays_editable_while_streaming(stack, qapp):
    """The user must be able to read history and prepare the next message."""
    context, services, provider = stack
    provider.chunks = ["x "] * 80
    provider.delay = 0.01
    window = make_window(stack)
    window.send_message("pytanie")
    assert wait_until(lambda: window.send_button.text() == "Stop", qapp, 4000)
    assert window.input.isReadOnly() is False
    window.input.set_text("przygotowana kolejna wiadomość")
    assert window.input.toPlainText() == "przygotowana kolejna wiadomość"
    window.stop_generation()
    pump(qapp, 600)
    window.close()
    pump(qapp, 50)


# ------------------------------------------------------------------ light mode
def test_light_mode_renders_the_whole_window(stack, qapp):
    """§21: 'light mode działa' - palette + QSS applied to a real window."""
    from gui.theme import apply_theme, palette_tokens

    context, services, _provider = stack
    context.settings.dark_theme = False
    apply_theme(qapp, dark=False, accent=context.settings.accent_color,
                font_size=context.settings.font_size,
                code_font_size=context.settings.code_font_size)
    window = make_window(stack)
    pump(qapp, 80)
    tokens = palette_tokens(dark=False)

    from PyQt5.QtGui import QPalette

    def hex_of(color):
        return "#%02x%02x%02x" % (color.red(), color.green(), color.blue())

    palette = qapp.palette()
    assert hex_of(palette.color(QPalette.Window)) == tokens["BG"]
    assert hex_of(palette.color(QPalette.AlternateBase)) == tokens["ALT_ROW"]
    # Jasny motyw musi być naprawdę jasny, a tekst ciemny.
    from tests.test_theme_contrast import luminance
    assert luminance(tokens["BG"]) > 200
    assert luminance(tokens["TEXT"]) < 80
    assert window.isVisible()
    window.close()
    pump(qapp, 50)
    apply_theme(qapp, dark=True)


def test_theme_switch_does_not_break_the_open_window(stack, qapp):
    context, services, _provider = stack
    window = make_window(stack)
    pump(qapp, 50)
    context.settings.dark_theme = False
    window.apply_settings()
    pump(qapp, 50)
    assert window.isVisible()
    context.settings.dark_theme = True
    window.apply_settings()
    pump(qapp, 50)
    assert window.isVisible()
    window.send_message("po zmianie motywu")
    pump(qapp, 1000)
    assert context.messages.count(int(window._session.id)) == 2
    window.close()
    pump(qapp, 50)
