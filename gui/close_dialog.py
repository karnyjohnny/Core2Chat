""""What should the X button do?" dialog (task §2, v0.1.2).

Shown when ``close_action == "ask"`` - which is the default - so the user is
never surprised by the application disappearing into the tray, and never loses
work by closing it by accident.

Three answers, one checkbox:

* **Anuluj** - keep the window open, nothing happens;
* **Zminimalizuj** - hide to the tray when a tray exists, otherwise minimize to
  the taskbar (this is the default button: it is the safe, reversible answer);
* **Zamknij program** - real shutdown.

"Zapamiętaj wybór" persists the choice into ``settings.close_action``, so the
dialog stops appearing. Leaving it unchecked asks every time, exactly as
requested.
"""

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)

from core.config import (CLOSE_ACTION_ASK, CLOSE_ACTION_EXIT,
                         CLOSE_ACTION_MINIMIZE, close_action_to_store)

#: Returned by :meth:`CloseActionDialog.chosen_action`.
CHOICE_CANCEL = "cancel"


class CloseActionDialog(QDialog):
    """Modal picker for the window-close behaviour."""

    def __init__(self, tray_available: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super(CloseActionDialog, self).__init__(parent)
        self.tray_available = bool(tray_available)
        self.remember = False
        self.choice = CHOICE_CANCEL
        self.setWindowTitle("Zamknięcie okna")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build_ui()

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        text = QLabel(
            "Co ma zrobić program po kliknięciu „X”?", self)
        text.setObjectName("DialogTitle")
        text.setWordWrap(True)
        layout.addWidget(text)

        detail = QLabel(
            "Zminimalizuj chowa okno %s - rozmowa trwa dalej i można ją "
            "przywrócić jednym kliknięciem ikony. Zamknij program kończy "
            "działanie Core2Chat."
            % ("w zasobniku systemowym" if self.tray_available
               else "na pasku zadań (brak zasobnika systemowego)"), self)
        detail.setObjectName("MessageMeta")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.minimize_button = QPushButton("Zminimalizuj", self)
        self.minimize_button.setObjectName("PrimaryButton")
        self.minimize_button.setCursor(Qt.PointingHandCursor)
        self.minimize_button.clicked.connect(self._on_minimize)
        self.exit_button = QPushButton("Zamknij program", self)
        self.exit_button.setCursor(Qt.PointingHandCursor)
        self.exit_button.clicked.connect(self._on_exit)
        row.addWidget(self.minimize_button)
        row.addWidget(self.exit_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.remember_check = QCheckBox("Zapamiętaj wybór (nie pytaj ponownie)",
                                        self)
        self.remember_check.setToolTip(
            "Zapisuje odpowiedź w ustawieniach. Bez zaznaczenia program pyta "
            "przy każdym zamknięciu.")
        layout.addWidget(self.remember_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        buttons.setCenterButtons(False)
        cancel = buttons.button(QDialogButtonBox.Cancel)
        if cancel is not None:
            cancel.setText("Anuluj")
            cancel.setCursor(Qt.PointingHandCursor)
        buttons.rejected.connect(self._on_cancel)
        layout.addWidget(buttons)

        # "Zminimalizuj" is the default: Enter confirms the safe answer.
        self.minimize_button.setDefault(True)
        self.minimize_button.setFocus(Qt.OtherFocusReason)

    # -------------------------------------------------------------- handlers
    def _on_minimize(self) -> None:
        self.choice = CLOSE_ACTION_MINIMIZE
        self.remember = self.remember_check.isChecked()
        self.accept()

    def _on_exit(self) -> None:
        self.choice = CLOSE_ACTION_EXIT
        self.remember = self.remember_check.isChecked()
        self.accept()

    def _on_cancel(self) -> None:
        self.choice = CHOICE_CANCEL
        self.remember = False
        self.reject()

    # ------------------------------------------------------------------- api
    def chosen_action(self) -> str:
        return self.choice

    def should_remember(self) -> bool:
        return bool(self.remember)

    @staticmethod
    def ask(tray_available: bool, parent: Optional[QWidget] = None) -> str:
        """Run the dialog and return one of ``cancel``/``minimize``/``exit``."""
        dialog = CloseActionDialog(tray_available, parent)
        dialog.exec_()
        return dialog.choice

    @staticmethod
    def setting_to_remember(choice: str) -> str:
        """Value to store in ``settings.close_action`` for *choice*."""
        return close_action_to_store(choice)
