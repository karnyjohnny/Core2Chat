"""Main window: layout, actions and wiring between UI and services.

Rules enforced here (specification §1, §40, §45, §62, §63):

* no provider REST logic and no SQL in this file - only service calls;
* every streaming signal is validated against the session it belongs to, so
  switching chats mid-stream can never paint text into the wrong transcript;
* the send button reflects GenerationState and duplicate sends are ignored;
* startup never blocks on the network: models are refreshed asynchronously.
"""

import os
import time
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAction, QApplication, QDialog, QFileDialog,
                             QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QSizePolicy, QSplitter,
                             QVBoxLayout, QWidget)

from core.constants import (APP_NAME, APP_VERSION, MESSAGES_PAGE_SIZE)
from core.logging_setup import get_logger
from gui.attachment_widget import AttachmentWidget
from gui.chat_widget import ChatWidget
from gui.model_selector import ModelSelector
from gui.sidebar import Sidebar
from gui.smart_input import SmartInput
from gui.theme import apply_theme, repolish, validate_color
from gui.widgets.context_meter import ContextMeter
from gui.widgets.status_pill import StatusPill
from models.attachment_models import Attachment, AttachmentStatus
from models.chat_models import (ConnectionState, ContextInfo,
                                GenerationState, Session)
from models.message_models import Message, MessageStatus, Role
from models.provider_models import ProviderError
from services.app_status import (CANCELLED, CONNECTING, ERROR, IDLE, OFFLINE,
                                 PAUSED, SENDING, STREAMING, SUCCESS, AppStatus,
                                 status_from_connectivity,
                                 status_from_provider_error)
from utils.markdown import MarkdownRenderer
from utils.text import format_tokens

log = get_logger("gui.main")

SEND_LABEL = "Wyślij"
STOP_LABEL = "Stop"
RETRY_LABEL = "Ponów"


class MainWindow(QMainWindow):
    """The application's primary window."""

    shutdown_requested = pyqtSignal()

    def __init__(self, context: Any, chat_service: Any, session_service: Any,
                 export_service: Any, import_service: Any,
                 parent: Optional[QWidget] = None) -> None:
        super(MainWindow, self).__init__(parent)
        self.context = context
        self.chat = chat_service
        self.sessions = session_service
        self.exporter = export_service
        self.importer = import_service
        self.settings = context.settings

        self.setWindowTitle("%s %s" % (APP_NAME, APP_VERSION))
        self.setMinimumSize(720, 480)
        self.resize(1120, 720)

        self.renderer = MarkdownRenderer()
        self._session: Optional[Session] = None
        self._pending_attachments: List[Attachment] = []
        self._last_failed_message_id: Optional[int] = None
        self._history_total = 0
        self._history_oldest: Optional[int] = None
        self._generation_started_at = 0.0
        # Single source of truth for what the user sees in the status pill.
        self.status = AppStatus(scheduler=self._schedule_once)
        self.status.add_listener(self._on_status_changed)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._suppress_send = False
        self._busy_states: Dict[int, str] = {}

        # Closing the window must really destroy it: without this every
        # close/open cycle leaks a full widget tree (~3.6 MB measured) and the
        # Nth launch gets progressively slower.
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._build_actions()
        self._build_ui()
        self._connect_services()
        self.apply_settings()

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- top bar
        top_bar = QWidget(self)
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 4, 8, 4)
        top_layout.setSpacing(6)

        self._title_label = QLabel("Nowa rozmowa")
        self._title_label.setObjectName("SessionTitle")
        self._title_label.setToolTip("Kliknij, aby zmienić nazwę rozmowy")
        self._title_label.setCursor(Qt.PointingHandCursor)
        self._title_label.mousePressEvent = lambda _event: self.rename_session()
        top_layout.addWidget(self._title_label, 1)

        self.model_selector = ModelSelector(self)
        self.model_selector.model_selected.connect(self._on_model_selected)
        self.model_selector.refresh_requested.connect(self.refresh_models)
        top_layout.addWidget(self.model_selector)

        self.context_meter = ContextMeter(self)
        top_layout.addWidget(self.context_meter)

        self.status_pill = StatusPill(self)
        top_layout.addWidget(self.status_pill)

        for tooltip, slot, label in (
                ("Statystyki tokenów", self.show_stats, "Σ"),
                ("Inspektor kontekstu", self.show_context_inspector, "⧉"),
                ("Ustawienia (Ctrl+,)", self.show_settings, "⚙"),
                ("Diagnostyka / o programie", self.show_about, "ℹ")):
            button = QPushButton(label)
            button.setObjectName("IconButton")
            button.setToolTip(tooltip)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            top_layout.addWidget(button)
        root.addWidget(top_bar)

        # ---- splitter: sidebar | chat+input
        self._splitter = QSplitter(Qt.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(1)

        self.sidebar = Sidebar(page_size=self.settings.sidebar_width and
                               MESSAGES_PAGE_SIZE)
        self.sidebar.new_chat_requested.connect(self.new_chat)
        self.sidebar.session_selected.connect(self.open_session)
        self.sidebar.session_action.connect(self._on_session_action)
        self.sidebar.search_requested.connect(self.show_search)
        self.sidebar.load_more_requested.connect(self._load_more_sessions)
        self._splitter.addWidget(self.sidebar)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.chat_widget = ChatWidget(renderer=self.renderer,
                                      page_size=MESSAGES_PAGE_SIZE)
        self.chat_widget.action_requested.connect(self._on_message_action)
        self.chat_widget.load_older_requested.connect(self.load_older_messages)
        self.chat_widget.link_clicked.connect(self._on_link_clicked)
        right_layout.addWidget(self.chat_widget, 1)

        input_panel = QWidget(self)
        input_panel.setObjectName("InputPanel")
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(8, 4, 8, 6)
        input_layout.setSpacing(4)

        self.attachment_bar = AttachmentWidget(self)
        self.attachment_bar.remove_requested.connect(self._remove_attachment)
        input_layout.addWidget(self.attachment_bar)

        editor_row = QHBoxLayout()
        editor_row.setSpacing(6)
        self.input = SmartInput(ctrl_enter_sends=self.settings.send_with_ctrl_enter)
        self.input.send_requested.connect(self.send_message)
        self.input.files_dropped.connect(self.add_files)
        self.input.image_pasted.connect(self.add_clipboard_image)
        self.input.draft_changed.connect(self._on_draft_changed)
        editor_row.addWidget(self.input, 1)

        button_column = QVBoxLayout()
        button_column.setSpacing(4)
        self._attach_button = QPushButton("📎")
        self._attach_button.setObjectName("IconButton")
        self._attach_button.setToolTip("Dołącz plik")
        self._attach_button.setCursor(Qt.PointingHandCursor)
        self._attach_button.clicked.connect(self.pick_files)
        self.send_button = QPushButton(SEND_LABEL)
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.setToolTip("Wyślij wiadomość (Enter)")
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.clicked.connect(self._on_send_clicked)
        button_column.addWidget(self._attach_button)
        button_column.addWidget(self.send_button)
        editor_row.addLayout(button_column)
        input_layout.addLayout(editor_row)

        self._input_hint = QLabel("Enter = wyślij · Shift+Enter = nowa linia · "
                                  "Ctrl+N = nowa rozmowa")
        self._input_hint.setObjectName("MessageMeta")
        input_layout.addWidget(self._input_hint)
        right_layout.addWidget(input_panel)

        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([int(self.settings.sidebar_width), 900])
        root.addWidget(self._splitter, 1)
        self.setCentralWidget(central)

        # ---- status bar
        self._status_session = QLabel("")
        self._status_counts = QLabel("")
        self._status_tokens = QLabel("")
        status_bar = self.statusBar()
        status_bar.setObjectName("StatusBar")
        status_bar.setSizeGripEnabled(True)
        status_bar.addWidget(self._status_session, 1)
        status_bar.addPermanentWidget(self._status_tokens)
        status_bar.addPermanentWidget(self._status_counts)

    def _build_actions(self) -> None:
        def action(text: str, slot, shortcut: str = "",
                   tooltip: str = "") -> QAction:
            item = QAction(text, self)
            if shortcut:
                item.setShortcut(QKeySequence(shortcut))
                item.setShortcutContext(Qt.WindowShortcut)
            if tooltip:
                item.setToolTip(tooltip)
            item.triggered.connect(slot)
            self.addAction(item)
            return item

        self.action_new = action("Nowa rozmowa", self.new_chat, "Ctrl+N",
                                 "Rozpocznij nową rozmowę")
        self.action_search = action("Szukaj w historiach", self.show_search,
                                    "Ctrl+K", "Szukaj w treści rozmów")
        self.action_focus_input = action("Fokus na pole wpisywania",
                                         self.focus_input, "Ctrl+L")
        self.action_copy_last = action("Kopiuj ostatnią odpowiedź",
                                       self.copy_last_response, "Ctrl+Shift+C")
        self.action_settings = action("Ustawienia", self.show_settings, "Ctrl+,")
        self.action_stats = action("Statystyki tokenów", self.show_stats)
        self.action_context = action("Inspektor kontekstu",
                                     self.show_context_inspector)
        self.action_about = action("Diagnostyka", self.show_about)
        self.action_export = action("Eksportuj rozmowę…", self.export_session)
        self.action_import = action("Importuj rozmowę…", self.import_session)
        self.action_stop = action("Zatrzymaj generowanie", self.stop_generation,
                                  "Esc")
        self.action_refresh_models = action("Odśwież modele",
                                            self.refresh_models, "F5")

    # ------------------------------------------------------------ appearance
    def apply_settings(self) -> None:
        settings = self.context.settings
        self.settings = settings
        # Palette *and* stylesheet from the same tokens: a stylesheet alone
        # leaves palette-driven areas (e.g. alternate table rows) unreadable.
        apply_theme(QApplication.instance(),
                    dark=settings.dark_theme,
                    accent=validate_color(settings.accent_color),
                    font_size=settings.font_size,
                    code_font_size=settings.code_font_size)
        self.chat_widget.set_show_thinking(settings.show_thinking)
        self.chat_widget.set_spacing(settings.message_spacing == "compact")
        self.chat_widget.set_auto_scroll(settings.auto_scroll)
        self.input.set_ctrl_enter_sends(settings.send_with_ctrl_enter)
        self._splitter.setSizes([int(settings.sidebar_width), 900])
        if settings.model_id:
            self.model_selector.set_current_model(settings.model_id)
        repolish(self)

    # -------------------------------------------------------------- sessions
    def reload_sidebar(self, include_archived: bool = False) -> None:
        sessions, total = self.sessions.list(include_archived=include_archived,
                                            limit=self.sidebar.page_size)
        self.sidebar.set_sessions(sessions, has_more=total > len(sessions),
                                  total=total)
        self._status_counts.setText("rozmów: %d" % total)

    def _load_more_sessions(self) -> None:
        current = self.sidebar.session_count()
        more, total = self.sessions.list(limit=self.sidebar.page_size,
                                         offset=current)
        self.sidebar.append_sessions(more, has_more=(current + len(more)) < total)

    def new_chat(self, model_id: str = "") -> Optional[Session]:
        self._flush_draft()
        session = self.sessions.create(model_id=model_id
                                       or self.model_selector.current_model_id())
        self.reload_sidebar()
        self._activate_session(session)
        self.sidebar.select_session(session.id)
        self.focus_input()
        log.info("ui.new_chat session=%s", session.id)
        return session

    def open_session(self, session_id: int) -> None:
        session = self.sessions.get(int(session_id))
        if session is None:
            return
        self._flush_draft()
        self._activate_session(session)

    def _activate_session(self, session: Session) -> None:
        self._session = session
        self.sessions.remember_active(int(session.id or 0))
        self.setWindowTitle("%s — %s %s" % (session.title or "Nowa rozmowa",
                                            APP_NAME, APP_VERSION))
        self._title_label.setText(session.title or "Nowa rozmowa")
        if session.model_id:
            self.model_selector.set_current_model(session.model_id)
        self.chat_widget.set_session(session.id)
        self._history_total = 0
        self._history_oldest = None
        self.load_older_messages(reset=True)
        self.input.set_text(session.draft or "")
        self._pending_attachments = []
        self.attachment_bar.clear()
        self.context_meter.reset()
        self._update_generation_ui(self.chat.generation_states.get(
            int(session.id or 0), GenerationState.IDLE))
        self._status_session.setText(
            "ID %s · tryb: %s%s" % (session.id, session.state_label(),
                                     " · przypięta" if session.pinned else ""))
        self._refresh_token_status()

    def load_older_messages(self, reset: bool = False) -> None:
        """Paged history load: only one page of widgets exists at a time."""
        if self._session is None or self._session.id is None:
            return
        before = None if reset else self.chat_widget.oldest_loaded_id()
        messages, total, oldest = self.sessions.history_page(
            int(self._session.id), limit=MESSAGES_PAGE_SIZE, before_id=before)
        if reset:
            self.chat_widget.clear()
            self.chat_widget.add_messages(messages, scroll=True)
        else:
            self.chat_widget.prepend_messages(messages)
        self._history_total = total
        self._history_oldest = oldest
        loaded = self.chat_widget.message_count()
        remaining = max(0, total - loaded)
        self.chat_widget.show_load_more(remaining)
        self._status_counts.setText("wiadomości: %d/%d" % (loaded, total))

    def _flush_draft(self) -> None:
        if self._session is None or self._session.id is None:
            return
        text = self.input.flush_draft()
        if text != (self._session.draft or ""):
            self.sessions.save_draft(int(self._session.id), text)
            self._session.draft = text

    def _on_draft_changed(self, text: str) -> None:
        if self._session is None or self._session.id is None:
            return
        self.sessions.save_draft(int(self._session.id), text)
        if self._session is not None:
            self._session.draft = text

    # -------------------------------------------------------------- sending
    def _on_send_clicked(self) -> None:
        state = self._current_state()
        if state == GenerationState.STREAMING:
            self.stop_generation()
        elif state == GenerationState.FAILED and self._last_failed_message_id:
            self.regenerate(self._last_failed_message_id)
        else:
            self.send_message(self.input.text_value())

    def send_message(self, text: str) -> None:
        if self._suppress_send:
            return
        text = (text or "").strip()
        if not text and not self._pending_attachments:
            return
        if self._session is None:
            self._session = self.sessions.create(
                model_id=self.model_selector.current_model_id())
            self.reload_sidebar()
            self._activate_session(self._session)
        if self.chat.is_busy(int(self._session.id or 0)):
            log.info("ui.send_ignored reason=busy session=%s", self._session.id)
            self._status_session.setText(
                "Trwa generowanie - poczekaj lub naciśnij Stop.")
            return
        attachments = list(self._pending_attachments)
        self.input.clear_text()
        self._pending_attachments = []
        self.attachment_bar.clear()
        self.sessions.ensure_title(self._session, text)
        self._title_label.setText(self._session.title)
        self.sidebar.upsert_session(self._session)

        pinned = self.context.pinned.list(int(self._session.id or 0))
        system_instruction = self._system_instruction()
        history, _total, _oldest = self.sessions.history_page(
            int(self._session.id or 0), limit=400)
        # Optimistic local echo; the DB row is written by the service.
        request = self.chat.send_message(
            session=self._session, text=text, attachments=attachments,
            system_instruction=system_instruction, pinned=pinned,
            history=history,
            model_id=self.model_selector.current_model_id())
        if request is None:
            self.input.set_text(text)
            self._pending_attachments = attachments
            self.attachment_bar.set_attachments(attachments)
            return
        echo = Message(id=None, session_id=self._session.id, role=Role.USER,
                       text=text, timestamp=int(time.time() * 1000),
                       status=MessageStatus.COMPLETED)
        self._echo_user_message(echo, attachments)
        self._update_generation_ui(GenerationState.STREAMING)
        log.info("ui.send session=%s chars=%d attachments=%d",
                 self._session.id, len(text), len(attachments))

    def _echo_user_message(self, message: Message,
                           attachments: List[Attachment]) -> None:
        """Show the user's turn immediately; the authoritative row is in SQLite."""
        widget = self.chat_widget.add_message(message, scroll=True) \
            if message.id is not None else None
        if widget is None:
            placeholder = Message(role=Role.USER, text=message.text,
                                  timestamp=message.timestamp,
                                  session_id=message.session_id)
            placeholder.id = _negative_id()
            self.chat_widget.add_message(placeholder, scroll=True)
        if attachments:
            note = "Załączniki: " + ", ".join(
                "%s (%s)" % (a.filename, a.status) for a in attachments)
            info = Message(role=Role.SYSTEM, text=note,
                           timestamp=message.timestamp,
                           session_id=message.session_id)
            info.id = _negative_id()
            self.chat_widget.add_message(info, scroll=True)

    def stop_generation(self) -> bool:
        if self._session is None:
            return False
        session_id = int(self._session.id or 0)
        stopped = self.chat.stop_generation(session_id)
        if stopped:
            # The service may already have finalised the turn (cancellation is
            # immediate for the user), so the authoritative state is read back
            # instead of being forced to CANCELLING here.
            state = self.chat.generation_states.get(session_id,
                                                    GenerationState.CANCELLING)
            self._update_generation_ui(state)
        return stopped

    def regenerate(self, message_id: int) -> None:
        if self._session is None:
            return
        pinned = self.context.pinned.list(int(self._session.id or 0))
        request = self.chat.retry_message(self._session, int(message_id),
                                          system_instruction=self._system_instruction(),
                                          pinned=pinned)
        if request is not None:
            self._last_failed_message_id = None
            self._update_generation_ui(GenerationState.STREAMING)

    def _system_instruction(self) -> str:
        preset_name = (self._session.system_preset if self._session else "") \
            or self.context.app_settings_repo.get("preset.active", "")
        if not preset_name:
            return ""
        preset = self.context.presets.by_name(preset_name)
        return preset.system_instruction if preset else ""

    # ---------------------------------------------------------- attachments
    def pick_files(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self, "Wybierz pliki do wysłania", "",
            "Wszystkie obsługiwane (*.txt *.md *.py *.json *.yaml *.yml *.xml "
            "*.html *.css *.js *.ts *.c *.h *.cpp *.hpp *.rs *.go *.java *.cs "
            "*.sql *.ini *.cfg *.log *.png *.jpg *.jpeg *.webp *.pdf);;"
            "Wszystkie pliki (*)")
        if paths:
            self.add_files(list(paths))

    def add_files(self, paths: List[str]) -> None:
        accepted, rejected = self.context.attachments.validate_drop(paths)
        if rejected:
            self._status_session.setText(
                "Odrzucono: %s" % "; ".join(rejected[:3]))
        for path in accepted:
            self._ingest_file(path)

    def _ingest_file(self, path: str) -> None:
        try:
            attachment = self.context.attachments.from_path(
                path, session_id=int(self._session.id or 0)
                if self._session else None)
        except Exception as exc:
            QMessageBox.warning(self, "Załącznik", str(exc))
            return
        self.context.attachments_repo.insert(attachment)
        self._pending_attachments.append(attachment)
        self.attachment_bar.add_attachment(attachment)
        self._status_session.setText("Dodano załącznik: %s" % attachment.filename)

    def add_clipboard_image(self, data: bytes, mime: str) -> None:
        try:
            attachment = self.context.attachments.from_bytes(
                data, "schowek-%s.png" % time.strftime("%H%M%S"), mime,
                session_id=int(self._session.id or 0) if self._session else None)
        except Exception as exc:
            QMessageBox.warning(self, "Schowek", str(exc))
            return
        self.context.attachments_repo.insert(attachment)
        self._pending_attachments.append(attachment)
        self.attachment_bar.add_attachment(attachment)

    def _remove_attachment(self, key: int) -> None:
        self._pending_attachments = [a for a in self._pending_attachments
                                     if a.id != key]
        self.attachment_bar.remove_attachment(key)
        self.context.attachments_repo.update_status(int(key),
                                                    AttachmentStatus.REMOVED)
        self.context.attachments.release(int(key))

    # ------------------------------------------------------- service signals
    def _connect_services(self) -> None:
        self.chat.state_changed.connect(self._on_state_changed)
        self.chat.text_delta.connect(self._on_text_delta)
        self.chat.thought_delta.connect(self._on_thought_delta)
        self.chat.message_finalized.connect(self._on_message_finalized)
        self.chat.generation_failed.connect(self._on_generation_failed)
        self.chat.generation_cancelled.connect(self._on_generation_cancelled)
        self.chat.context_ready.connect(self._on_context_ready)
        self.chat.usage_updated.connect(self._on_usage_updated)
        self.chat.models_refreshed.connect(self._on_models_refreshed)
        self.chat.task_result.connect(self._on_task_result)
        self.chat.task_failed.connect(self._on_task_failed)

    def _owns(self, session_id: int) -> bool:
        """Race defence: ignore events for sessions that are not displayed."""
        return self._session is not None and int(self._session.id or -1) == \
            int(session_id)

    def _on_state_changed(self, session_id: int, state: str,
                          request_id: str) -> None:
        self._busy_states[int(session_id)] = state
        if self._owns(session_id):
            self._update_generation_ui(state)

    def _on_text_delta(self, session_id: int, message_id: int, text: str,
                       request_id: str) -> None:
        if not self._owns(session_id):
            return
        if self.chat_widget.widget_for(message_id) is None:
            placeholder = Message(id=message_id, session_id=session_id,
                                  role=Role.ASSISTANT, text="",
                                  status=MessageStatus.STREAMING,
                                  model_name=self.model_selector.current_model_id())
            self.chat_widget.add_message(placeholder, scroll=True)
            self.chat_widget.begin_stream(message_id)
        self.chat_widget.append_stream(message_id, text)

    def _on_thought_delta(self, session_id: int, message_id: int, text: str,
                          request_id: str) -> None:
        if not self._owns(session_id) or not self.settings.show_thinking:
            return
        widget = self.chat_widget.widget_for(message_id)
        if widget is not None:
            widget.message.thought_summary = \
                (widget.message.thought_summary or "") + text
            widget._update_thinking()

    def _on_message_finalized(self, session_id: int, message_id: int,
                              status: str, request_id: str) -> None:
        self._refresh_token_status()
        if not self._owns(session_id):
            return
        stored = self.context.messages.get(message_id)
        widget = self.chat_widget.widget_for(message_id)
        if stored is not None and widget is not None:
            widget.update_message(stored)
        elif stored is not None:
            self.chat_widget.add_message(stored, scroll=True)
        self.chat_widget.end_stream(message_id, status)
        if status == MessageStatus.FAILED:
            self._last_failed_message_id = message_id
        self._status_session.setText("Odpowiedź: %s" % _status_text(status))
        if status == MessageStatus.COMPLETED:
            self._update_generation_ui(GenerationState.COMPLETED)
        if self._session is not None:
            self.sidebar.upsert_session(self._session)
            self.sessions.remember_active(int(self._session.id or 0))

    def _on_generation_failed(self, session_id: int, message_id: int,
                              error: ProviderError, request_id: str) -> None:
        self._apply_connection_error(error)
        if not self._owns(session_id):
            return
        widget = self.chat_widget.widget_for(message_id)
        if widget is not None:
            widget.message.metadata["error"] = error.to_dict()
            widget.message.status = MessageStatus.FAILED
            widget.update_message(widget.message)
        self._last_failed_message_id = message_id
        self._status_session.setText(error.message_user)
        self._update_generation_ui(GenerationState.FAILED, error.message_user)
        QMessageBox.warning(self, "Błąd API", _error_dialog_text(error))

    def _on_generation_cancelled(self, session_id: int, message_id: int,
                                 request_id: str) -> None:
        if not self._owns(session_id):
            return
        self.chat_widget.end_stream(message_id, MessageStatus.CANCELLED)
        self._update_generation_ui(GenerationState.CANCELLED)
        widget = self.chat_widget.widget_for(message_id)
        if widget is not None:
            widget.message.status = MessageStatus.CANCELLED
            widget.update_message(widget.message)
        self._status_session.setText("Generowanie przerwane.")

    def _on_context_ready(self, session_id: int, info) -> None:
        if not self._owns(session_id):
            return
        self.context_meter.set_info(info)
        self._last_context_info = info
        if info.warnings:
            self._status_session.setText(info.warnings[-1][:120])

    def _on_usage_updated(self, session_id: int, message_id: int, usage) -> None:
        if not self._owns(session_id):
            return
        self._status_tokens.setText(
            "ostatnio: in %s / out %s%s" % (
                format_tokens(usage.input_tokens),
                format_tokens(usage.output_tokens),
                " / cache %s" % format_tokens(usage.cached_tokens)
                if usage.cached_tokens else ""))

    def _on_models_refreshed(self, models, error: str) -> None:
        pass  # handled through task_result to keep one code path

    def _on_task_result(self, kind: str, request_id: str, payload) -> None:
        if kind == "models.refresh":
            models = list(payload or [])
            self.model_selector.set_models(models, self.settings.model_id)
            self.model_selector.set_busy(False)
            self._status_counts.setText("modeli: %d" % len(models))
            if models:
                # A successful discovery proves connectivity: clear OFFLINE /
                # AUTH_REQUIRED instead of leaving a stale "error" impression.
                self.status.clear_sticky()
                self.set_connection(ConnectionState.ONLINE)
            else:
                self.status.set(ERROR, "Lista modeli jest pusta.", force=True)
        elif kind == "api.validate":
            connectivity = {"online": ConnectionState.ONLINE,
                            "offline": ConnectionState.OFFLINE,
                            "auth_failed": ConnectionState.AUTH_FAILED,
                            "rate_limited": ConnectionState.RATE_LIMITED,
                            "no_key": ConnectionState.OFFLINE}.get(
                                str(payload), ConnectionState.ERROR)
            self.set_connection(connectivity)
            if connectivity != ConnectionState.ONLINE:
                self._status_session.setText(_connection_hint(connectivity))

    def _on_task_failed(self, kind: str, request_id: str,
                        error: ProviderError) -> None:
        self.model_selector.set_busy(False)
        self._apply_connection_error(error)
        if kind == "models.refresh":
            self.model_selector.set_error(error.message_user)

    def _apply_connection_error(self, error: ProviderError) -> None:
        """Map a provider error onto the status machine (task §7).

        The UI must recover: transient states return to "Gotowy" on their own,
        sticky ones stay only while the environment really is broken.
        """
        state, detail = status_from_provider_error(error.category,
                                                   error.message_user)
        if error.category == "CANCELLED":
            return
        if error.retry_after_seconds:
            detail = "%s (ponów za %.0f s)" % (detail, error.retry_after_seconds)
        self.status.set(state, detail, force=True)

    def set_connection(self, state: str) -> None:
        """Connectivity probe result -> state machine (sticky states)."""
        self.status.set(status_from_connectivity(state), "", force=True)

    def _schedule_once(self, delay_ms: int, callback) -> None:
        """Timer backend for the state machine (Qt single-shot)."""
        QTimer.singleShot(int(delay_ms), callback)

    def _on_status_changed(self, state: str, detail: str) -> None:
        self.status_pill.set_status(state, detail)
        if state not in (STREAMING, SENDING, CONNECTING):
            self.status_pill.set_elapsed(None)
        # The send button follows the same state, so the UI cannot contradict
        # itself ("Stop" while the pill says "Gotowy").
        self._apply_send_button_state(state)

    def _apply_send_button_state(self, state: str) -> None:
        if state in (STREAMING, SENDING, CONNECTING):
            self.send_button.setText(STOP_LABEL)
            self.send_button.setToolTip("Zatrzymaj generowanie (Esc)")
        elif state == ERROR:
            self.send_button.setText(RETRY_LABEL if self._last_failed_message_id
                                     else SEND_LABEL)
            self.send_button.setToolTip("Spróbuj ponownie")
        else:
            self.send_button.setText(SEND_LABEL)
            self.send_button.setToolTip("Wyślij wiadomość (Enter)")
        self.input.set_enabled_for_state(state in (STREAMING, SENDING))

    def _on_model_selected(self, model_id: str) -> None:
        """Persist the choice both globally and for the open session."""
        if not model_id:
            return
        self.context.settings.model_id = model_id
        if self._session is not None and self._session.id is not None:
            self.sessions.set_model(int(self._session.id), model_id)
            self._session.model_id = model_id
        model = None
        provider = self.context.provider
        if provider is not None:
            model = provider.get_model(model_id)
        if model is not None:
            self.context_meter.set_info(ContextInfo(
                model_id=model_id, input_limit=model.input_token_limit,
                state_mode=self._session.state_mode if self._session else
                self.context.settings.state_mode,
                min_cached_tokens=model.capabilities.min_cached_tokens))
        self._status_session.setText("Model: %s" % model_id)
        log.info("ui.model_selected id=%s", model_id)

    # ------------------------------------------------------------ generation ui
    #: ChatService GenerationState -> application status state.
    _STATE_MAP = {
        GenerationState.IDLE: IDLE,
        GenerationState.PREPARING: SENDING,
        GenerationState.COUNTING_TOKENS: SENDING,
        GenerationState.UPLOADING: SENDING,
        GenerationState.STREAMING: STREAMING,
        GenerationState.CANCELLING: SENDING,
        GenerationState.COMPLETED: SUCCESS,
        GenerationState.FAILED: ERROR,
        GenerationState.CANCELLED: CANCELLED,
    }

    def _current_state(self) -> str:
        if self._session is None:
            return GenerationState.IDLE
        return self._busy_states.get(int(self._session.id or 0),
                                     GenerationState.IDLE)

    def _update_generation_ui(self, state: str, detail: str = "") -> None:
        """Translate a generation state into the single app status."""
        import time as _time

        mapped = self._STATE_MAP.get(state, IDLE)
        if mapped in (STREAMING, SENDING):
            if not self._generation_started_at:
                self._generation_started_at = _time.monotonic()
                self._elapsed_timer.start()
        else:
            self._generation_started_at = 0.0
            self._elapsed_timer.stop()
            self.status_pill.set_elapsed(None)
        # Sticky environment states are not overwritten by an operation result.
        if self.status.is_sticky and mapped in (SUCCESS, IDLE):
            return
        self.status.set(mapped, detail)

    def _tick_elapsed(self) -> None:
        """1 Hz label update: a thinking model can stay silent for a minute."""
        import time as _time

        if not self._generation_started_at:
            return
        elapsed = _time.monotonic() - self._generation_started_at
        self.status_pill.set_elapsed(int(elapsed))

    def _refresh_token_status(self) -> None:
        stats = self.context.stats.window("last_24h")
        self._status_tokens.setText("24 h: %s tok. (%d zapytań)"
                                    % (format_tokens(stats.total_tokens),
                                       stats.requests))

    # ------------------------------------------------------------- messages ui
    def _on_message_action(self, name: str, message: Message) -> None:
        if name == "copy" or name == "copy_markdown":
            QApplication.clipboard().setText(message.text or "")
            self._status_session.setText("Skopiowano do schowka.")
        elif name == "copy_code":
            widget = self.chat_widget.widget_for(message.id or -1)
            if widget is not None and widget.copy_code_block(0):
                self._status_session.setText("Skopiowano pierwszy blok kodu.")
            else:
                self._status_session.setText("Brak bloku kodu w tej wiadomości.")
        elif name == "regenerate":
            if message.id is not None:
                self.regenerate(message.id)
        elif name == "edit":
            self._edit_message(message)
        elif name == "fork":
            self._fork_session(message.id)
        elif name == "delete":
            self._delete_message(message)

    def _edit_message(self, message: Message) -> None:
        if message.id is None:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "Edytuj wiadomość", "Treść:", message.text or "")
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        # Everything after the edited message is dropped: the turn is replayed.
        removed = self.sessions.remove_tail_from(int(message.session_id or 0),
                                                 int(message.id))
        self.sessions.save_draft(int(message.session_id or 0), text)
        self.load_older_messages(reset=True)
        self._status_session.setText(
            "Usunięto %d wiadomości i wczytano edytowaną treść." % removed)
        self.input.set_text(text)
        self.focus_input()

    def _delete_message(self, message: Message) -> None:
        if message.id is None or message.id < 0:
            return
        answer = QMessageBox.question(
            self, "Usuń wiadomość",
            "Usunąć tę wiadomość z historii?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.context.messages.delete(int(message.id))
        self.chat_widget.remove_message(int(message.id))
        self._status_session.setText("Wiadomość usunięta.")

    def _fork_session(self, message_id: Optional[int]) -> None:
        if self._session is None:
            return
        fork = self.sessions.fork(int(self._session.id), message_id)
        self.reload_sidebar()
        self._activate_session(fork)
        self.sidebar.select_session(fork.id)
        self._status_session.setText("Utworzono gałąź: %s" % fork.title)

    def copy_last_response(self) -> None:
        for message_id in reversed(self.chat_widget.message_ids):
            widget = self.chat_widget.widget_for(message_id)
            if widget is not None and widget.message.role == Role.ASSISTANT \
                    and widget.message.text:
                widget.copy_text()
                self._status_session.setText("Skopiowano ostatnią odpowiedź.")
                return
        self._status_session.setText("Brak odpowiedzi do skopiowania.")

    def _on_link_clicked(self, url: str) -> None:
        if not url:
            self._status_session.setText(
                "Odrzucono niebezpieczny odnośnik z odpowiedzi modelu.")
            return
        answer = QMessageBox.question(
            self, "Otworzyć odnośnik?",
            "Otworzyć w przeglądarce?\n%s" % url,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            from PyQt5.QtGui import QDesktopServices
            from PyQt5.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------- session ops
    def _on_session_action(self, action_name: str, session_id: int) -> None:
        if action_name == "open":
            self.open_session(session_id)
        elif action_name == "rename":
            self.rename_session(session_id)
        elif action_name == "delete":
            self.delete_session(session_id)
        elif action_name == "archive":
            session = self.sessions.get(session_id)
            if session is not None:
                self.sessions.set_archived(session_id, not session.archived)
                self.reload_sidebar(include_archived=True)
        elif action_name == "pin":
            self.sessions.set_pinned(session_id, True)
            self.reload_sidebar()
        elif action_name == "unpin":
            self.sessions.set_pinned(session_id, False)
            self.reload_sidebar()
        elif action_name == "fork":
            self._fork_session(None)
        elif action_name == "clear":
            self.clear_session(session_id)
        elif action_name == "export":
            self.export_session(session_id)

    def rename_session(self, session_id: Optional[int] = None) -> None:
        target = session_id if session_id is not None else \
            (int(self._session.id) if self._session else None)
        if target is None:
            return
        session = self.sessions.get(target)
        if session is None:
            return
        title, ok = QInputDialog.getText(self, "Zmień nazwę", "Tytuł:",
                                         text=session.title)
        if ok and title.strip():
            self.sessions.rename(target, title.strip())
            self.reload_sidebar()
            if self._session is not None and self._session.id == target:
                self._session.title = title.strip()
                self._title_label.setText(title.strip())

    def delete_session(self, session_id: Optional[int] = None) -> None:  # noqa: C901
        target = session_id if session_id is not None else \
            (int(self._session.id) if self._session else None)
        if target is None:
            return
        if self.settings.confirm_delete_session:
            answer = QMessageBox.question(
                self, "Usuń rozmowę",
                "Trwale usunąć tę rozmowę wraz z wiadomościami?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.sessions.delete(target)
        self.reload_sidebar()
        if self._session is not None and self._session.id == target:
            self._session = None
            self.chat_widget.set_session(None)
            self.new_chat()

    def clear_session(self, session_id: Optional[int] = None) -> None:
        target = session_id if session_id is not None else \
            (int(self._session.id) if self._session else None)
        if target is None:
            return
        answer = QMessageBox.question(
            self, "Wyczyść rozmowę",
            "Usunąć wszystkie wiadomości z tej rozmowy?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        removed = self.sessions.clear(target)
        self.load_older_messages(reset=True)
        self._status_session.setText("Usunięto %d wiadomości." % removed)

    def export_session(self, session_id: Optional[int] = None,
                       fmt: str = "md") -> None:
        target = session_id if session_id is not None else \
            (int(self._session.id) if self._session else None)
        if target is None:
            return
        suggested = self.exporter.suggest_filename(target, fmt)
        path, selected = QFileDialog.getSaveFileName(
            self, "Eksportuj rozmowę", suggested,
            "Markdown (*.md);;Tekst (*.txt);;JSON (*.json);;HTML (*.html)")
        if not path:
            return
        from services.export_import import detect_format
        chosen = detect_format(path)
        try:
            written, count = self.exporter.export(target, path, chosen)
        except Exception as exc:
            QMessageBox.critical(self, "Eksport", str(exc))
            return
        self._status_session.setText(
            "Wyeksportowano %d wiadomości do %s" % (count, os.path.basename(written)))

    def import_session(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "Importuj rozmowę", "", "Eksport Core2Chat (*.json)")
        if not path:
            return
        try:
            session = self.importer.import_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import", str(exc))
            return
        self.reload_sidebar()
        self._activate_session(session)
        self._status_session.setText("Zaimportowano: %s" % session.title)

    # ---------------------------------------------------------------- dialogs
    def show_settings(self) -> None:
        from gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.context, parent=self)
        if dialog.exec_() == QDialog.Accepted:
            self.apply_settings()
            self.refresh_models(force=False)
            self._status_session.setText("Ustawienia zapisane.")

    def show_stats(self) -> None:
        from gui.stats_dialog import StatsDialog

        StatsDialog(self.context, parent=self).exec_()

    def show_context_inspector(self) -> None:
        from gui.context_dialog import ContextDialog

        info = getattr(self, "_last_context_info", None)
        ContextDialog(self.context, info, parent=self).exec_()

    def show_search(self, query: str = "") -> None:
        from gui.search_dialog import SearchDialog

        dialog = SearchDialog(self.context, initial_query=query, parent=self)
        dialog.jump_requested.connect(self._jump_to_result)
        dialog.exec_()

    def _jump_to_result(self, session_id: int, message_id: int) -> None:
        self.open_session(session_id)
        # The message may live in an older page: load until it is present.
        for _attempt in range(12):
            if self.chat_widget.jump_to_message(message_id):
                self._status_session.setText("Przejście do wiadomości %d."
                                             % message_id)
                return
            oldest = self.chat_widget.oldest_loaded_id()
            if oldest is None or self._history_oldest == oldest:
                break
            self.load_older_messages()
        self._status_session.setText(
            "Wiadomość znajduje się w starszej części historii.")

    def show_about(self) -> None:
        from gui.about_dialog import AboutDialog

        AboutDialog(self.context, parent=self).exec_()

    # ------------------------------------------------------------------ misc
    def refresh_models(self, force: bool = True) -> None:
        self.model_selector.set_busy(True)
        self._status_session.setText("Pobieranie listy modeli…")
        self.chat.refresh_models(force=force)

    def focus_input(self) -> None:
        self.input.setFocus(Qt.ShortcutFocusReason)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._flush_draft()
        if self.settings.close_to_tray and _tray_available():
            # Hide only: the window stays alive so the tray can restore it.
            event.ignore()
            self.hide()
            self._status_session.setText(
                "Aplikacja działa w zasobniku systemowym.")
            return
        self.shutdown_requested.emit()
        self.teardown()
        event.accept()

    def teardown(self) -> None:
        """Release Qt resources deterministically (called on close/exit)."""
        try:
            self._elapsed_timer.stop()
            # ChatWidget owns the message widgets; ChatService is shared with
            # the application and must survive the window (tray restore).
            self.chat_widget.cleanup()
            self.sidebar.set_sessions([])
            self.attachment_bar.clear()
            for signal, slot in (
                    (self.chat.state_changed, self._on_state_changed),
                    (self.chat.text_delta, self._on_text_delta),
                    (self.chat.thought_delta, self._on_thought_delta),
                    (self.chat.message_finalized, self._on_message_finalized),
                    (self.chat.generation_failed, self._on_generation_failed),
                    (self.chat.generation_cancelled,
                     self._on_generation_cancelled),
                    (self.chat.context_ready, self._on_context_ready),
                    (self.chat.usage_updated, self._on_usage_updated),
                    (self.chat.task_result, self._on_task_result),
                    (self.chat.task_failed, self._on_task_failed)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self.renderer.clear_cache()
        except RuntimeError:      # C++ objects already deleted
            pass
        except Exception as exc:  # pragma: no cover - teardown must not raise
            log.warning("ui.teardown_failed err=%s", exc)

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.focus_input()

    def window_state_snapshot(self) -> str:
        """Serialise geometry + window state + splitter sizes.

        Uses :mod:`gui.window_state` (never ``QByteArrayLiteral``, which PyQt5
        does not export - it crashed every second launch).
        """
        from gui import window_state

        try:
            return window_state.encode(
                self.saveGeometry(), self._splitter.sizes(),
                maximized=self.isMaximized(), fullscreen=self.isFullScreen())
        except Exception as exc:
            # A failed snapshot must never break shutdown.
            log.warning("ui.snapshot_failed err=%s", exc)
            return ""

    def apply_window_state(self, snapshot: str) -> bool:
        """Restore geometry. Returns False (and keeps defaults) when invalid.

        Corrupt or foreign data is rejected, never applied and never causes the
        user's stored data to be deleted.
        """
        from gui import window_state

        if not snapshot:
            return False
        migrated = window_state.migrate(snapshot)
        if migrated is None:
            log.warning("ui.snapshot_rejected reason=invalid len=%d",
                        len(snapshot))
            return False
        try:
            geometry, sizes, flags = window_state.decode(migrated)
        except window_state.SnapshotError as exc:
            log.warning("ui.snapshot_rejected reason=%s", exc)
            return False
        from PyQt5.QtCore import QByteArray

        restored = self.restoreGeometry(QByteArray(geometry))
        if not restored:
            log.warning("ui.snapshot_geometry_not_restored")
        if len(sizes) == 2:
            self._splitter.setSizes([int(sizes[0]), int(sizes[1])])
        if flags & window_state.FLAG_MAXIMIZED:
            self.showMaximized()
        elif flags & window_state.FLAG_FULLSCREEN:
            self.showFullScreen()
        log.info("ui.snapshot_applied maximized=%s sizes=%s",
                 bool(flags & window_state.FLAG_MAXIMIZED), sizes)
        return True


def _error_dialog_text(error: ProviderError) -> str:
    parts = [error.message_user]
    if error.message_debug:
        parts.append("\nSzczegóły techniczne: %s" % error.message_debug)
    if error.retry_after_seconds:
        parts.append("\nSpróbuj ponownie za %.0f s." % error.retry_after_seconds)
    return "\n".join(parts)


def _status_text(status: str) -> str:
    return {
        MessageStatus.COMPLETED: "zakończona",
        MessageStatus.INCOMPLETE: "niepełna (limit tokenów)",
        MessageStatus.CANCELLED: "przerwana",
        MessageStatus.FAILED: "błąd",
        MessageStatus.INTERRUPTED: "przerwane połączenie",
    }.get(status, status)


def _connection_hint(state: str) -> str:
    return {
        ConnectionState.OFFLINE: "Brak połączenia z API - tryb offline.",
        ConnectionState.AUTH_FAILED: "Klucz API wymaga poprawy w ustawieniach.",
        ConnectionState.RATE_LIMITED: "Limit API osiągnięty - poczekaj chwilę.",
        ConnectionState.ERROR: "Błąd połączenia z API.",
    }.get(state, "")


def _tray_available() -> bool:
    from core.tray_manager import tray_available

    return tray_available()


_negative_counter = {"n": 0}


def _negative_id() -> int:
    """Local-only message ids for optimistic echoes (never persisted)."""
    _negative_counter["n"] -= 1
    return _negative_counter["n"]


def _is_qt_sequence(combo: str) -> bool:
    """True when a Win32 combo can be expressed as a Qt shortcut."""
    text = (combo or "").lower()
    if text.startswith("win+"):
        return False        # Qt cannot grab the Windows key reliably
    return bool(combo) and QKeySequence(combo).toString() != ""
