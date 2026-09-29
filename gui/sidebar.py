"""Session sidebar: search box, new-chat button, paged session list.

The list is populated lazily in pages (specification §19): hundreds of
historical sessions must not create hundreds of widgets up front.
"""

from typing import Callable, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

from core.constants import SESSIONS_PAGE_SIZE, SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH
from models.chat_models import Session

SEARCH_DEBOUNCE_MS = 220


class Sidebar(QWidget):
    """Left-hand session navigator."""

    new_chat_requested = pyqtSignal()
    session_selected = pyqtSignal(int)
    session_action = pyqtSignal(str, int)      # action name, session id
    search_requested = pyqtSignal(str)
    load_more_requested = pyqtSignal()

    def __init__(self, page_size: int = SESSIONS_PAGE_SIZE,
                 parent: Optional[QWidget] = None) -> None:
        super(Sidebar, self).__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.page_size = max(10, int(page_size))
        self._sessions: List[Session] = []
        self._selected_id: Optional[int] = None
        self._filter = ""
        self._has_more = False

        header = QWidget(self)
        header.setObjectName("SidebarHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(6, 6, 6, 6)
        header_layout.setSpacing(4)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self._new_button = QPushButton("＋ Nowa rozmowa")
        self._new_button.setObjectName("PrimaryButton")
        self._new_button.setToolTip("Nowa rozmowa (Ctrl+N)")
        self._new_button.setCursor(Qt.PointingHandCursor)
        self._new_button.clicked.connect(self.new_chat_requested)
        buttons.addWidget(self._new_button, 1)
        self._search_button = QPushButton("🔍")
        self._search_button.setObjectName("IconButton")
        self._search_button.setToolTip("Szukaj w historiach (Ctrl+K)")
        self._search_button.setCursor(Qt.PointingHandCursor)
        self._search_button.clicked.connect(
            lambda: self.search_requested.emit(self._search_box.text().strip()))
        buttons.addWidget(self._search_button)
        header_layout.addLayout(buttons)

        self._search_box = QLineEdit(self)
        self._search_box.setPlaceholderText("Filtruj rozmowy…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.setToolTip("Filtruje tytuły; Enter szuka w treści")
        self._search_box.textChanged.connect(self._on_filter_changed)
        self._search_box.returnPressed.connect(
            lambda: self.search_requested.emit(self._search_box.text().strip()))
        header_layout.addWidget(self._search_box)

        self._count_label = QLabel("")
        self._count_label.setObjectName("MessageMeta")
        header_layout.addWidget(self._count_label)

        self._list = QListWidget(self)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)
        self._list.setUniformItemSizes(True)     # cheap layout for long lists
        self._list.setVerticalScrollMode(QListWidget.ScrollPerItem)

        self._load_more = QPushButton("Wczytaj więcej")
        self._load_more.setObjectName("IconButton")
        self._load_more.setCursor(Qt.PointingHandCursor)
        self._load_more.clicked.connect(self.load_more_requested)
        self._load_more.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._load_more)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_filter)

    # ------------------------------------------------------------------ api
    def set_sessions(self, sessions: List[Session], has_more: bool = False,
                     total: Optional[int] = None) -> None:
        self._sessions = list(sessions)
        self._has_more = bool(has_more)
        self._load_more.setVisible(self._has_more)
        self._rebuild()
        count = total if total is not None else len(self._sessions)
        self._count_label.setText("%s rozmów%s" % (
            "{:,}".format(int(count)).replace(",", " "),
            " (wczytane: %d)" % len(self._sessions)
            if len(self._sessions) < count else ""))

    def append_sessions(self, sessions: List[Session],
                        has_more: bool = False) -> None:
        existing = {s.id for s in self._sessions}
        for session in sessions:
            if session.id not in existing:
                self._sessions.append(session)
        self._has_more = bool(has_more)
        self._load_more.setVisible(self._has_more)
        self._rebuild()

    def upsert_session(self, session: Session) -> None:
        for index, existing in enumerate(self._sessions):
            if existing.id == session.id:
                self._sessions[index] = session
                break
        else:
            self._sessions.insert(0, session)
        self._sort()
        self._rebuild()
        self.select_session(session.id)

    def remove_session(self, session_id: int) -> None:
        self._sessions = [s for s in self._sessions if s.id != session_id]
        if self._selected_id == session_id:
            self._selected_id = None
        self._rebuild()

    def select_session(self, session_id: Optional[int],
                       emit: bool = False) -> None:
        self._selected_id = session_id
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(Qt.UserRole) == session_id:
                self._list.setCurrentItem(item)
                return
        self._list.setCurrentItem(None)
        if emit and session_id is not None:
            self.session_selected.emit(session_id)

    def selected_session_id(self) -> Optional[int]:
        return self._selected_id

    def session_count(self) -> int:
        return len(self._sessions)

    def filter_text(self) -> str:
        return self._search_box.text().strip()

    def set_filter_text(self, text: str) -> None:
        self._search_box.setText(text or "")

    def focus_search(self) -> None:
        self._search_box.setFocus(Qt.ShortcutFocusReason)
        self._search_box.selectAll()

    # -------------------------------------------------------------- internal
    def _sort(self) -> None:
        self._sessions.sort(key=lambda s: (not s.pinned, -int(s.updated_at or 0)))

    def _rebuild(self) -> None:
        self._sort()
        needle = self._filter.lower()
        self._list.clear()
        shown = 0
        for session in self._sessions:
            if needle and needle not in (session.title or "").lower():
                continue
            item = QListWidgetItem(self._label_for(session))
            item.setData(Qt.UserRole, session.id)
            item.setToolTip(self._tooltip_for(session))
            if session.id == self._selected_id:
                item.setSelected(True)
                self._list.setCurrentItem(item)
            self._list.addItem(item)
            shown += 1
        if shown == 0:
            placeholder = QListWidgetItem(
                "Brak rozmów" if not needle else "Nic nie pasuje do filtru")
            placeholder.setFlags(Qt.NoItemFlags)
            self._list.addItem(placeholder)

    @staticmethod
    def _label_for(session: Session) -> str:
        pin = "📌 " if session.pinned else ""
        fork = "⑂ " if session.is_fork else ""
        title = session.title or "Nowa rozmowa"
        if len(title) > 46:
            title = title[:45] + "…"
        return "%s%s%s" % (pin, fork, title)

    @staticmethod
    def _tooltip_for(session: Session) -> str:
        import time
        updated = time.strftime("%Y-%m-%d %H:%M",
                                time.localtime((session.updated_at or 0) / 1000.0)) \
            if session.updated_at else "-"
        return "%s\nModel: %s\nAktualizacja: %s\nTryb: %s%s" % (
            session.title, session.model_id or "-", updated,
            session.state_label(),
            "\nZarchiwizowana" if session.archived else "")

    def _on_filter_changed(self, text: str) -> None:
        self._filter = text.strip()
        self._debounce.start()

    def _apply_filter(self) -> None:
        self._rebuild()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.UserRole)
        if session_id is None:
            return
        if session_id != self._selected_id:
            self._selected_id = session_id
            self.session_selected.emit(int(session_id))

    def _show_menu(self, position) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        session_id = item.data(Qt.UserRole)
        if session_id is None:
            return
        session = next((s for s in self._sessions if s.id == session_id), None)
        menu = QMenu(self)
        entries: List[tuple] = [
            ("Otwórz", "open"), ("Zmień nazwę", "rename"),
            ("Duplikuj / rozgałęź", "fork"), ("Eksportuj…", "export"),
            ("Wyczyść treść", "clear"),
            ("Przypnij" if session and not session.pinned else "Odepnij",
             "unpin" if session and session.pinned else "pin"),
            ("Archiwizuj" if session and not session.archived
             else "Przywróć z archiwum", "archive"),
        ]
        for label, action in entries:
            menu.addAction(label, lambda a=action, sid=session_id:
                           self.session_action.emit(a, sid))
        menu.addSeparator()
        menu.addAction("Usuń", lambda sid=session_id:
                       self.session_action.emit("delete", sid))
        menu.exec_(self._list.mapToGlobal(position))
