"""Shared keyboard policy for every text composer in the application.

Specification of the task (§11) requires one implementation, not per-window
variants, so both the main window input and any future composer go through
:class:`SubmitPolicy`.

Semantics:

==============  =========================================================
Enter           submit
Shift+Enter     newline
Ctrl+Enter      submit (also the only submit key in "Ctrl+Enter" mode)
Alt+Enter       newline (never submits - avoids accidental sends)
Escape          clear the draft if any, otherwise propagate
==============  =========================================================

PyQt5 note: ``event.modifiers() & Qt.ShiftModifier`` returns a
``Qt.KeyboardModifiers`` object, **not** an int. Comparing it with ``== 0`` is
always False, which silently turned Enter into "newline". Every modifier test
here goes through :func:`has_modifier`, which is covered by tests.
"""

from PyQt5.QtCore import Qt

SUBMIT = "submit"
NEWLINE = "newline"
CLEAR_OR_PROPAGATE = "clear_or_propagate"
PROPAGATE = "propagate"
INSERT = "insert"


def has_modifier(modifiers, flag) -> bool:
    """Modifier test that is correct for PyQt5's flag objects."""
    try:
        return bool(int(modifiers) & int(flag))
    except (TypeError, ValueError):
        return False


class SubmitPolicy(object):
    """Decides what a key press in a composer means. Pure logic, no Qt state."""

    def __init__(self, ctrl_enter_sends: bool = False) -> None:
        self.ctrl_enter_sends = bool(ctrl_enter_sends)

    def set_ctrl_enter_sends(self, enabled: bool) -> None:
        self.ctrl_enter_sends = bool(enabled)

    def decide(self, key: int, modifiers) -> str:
        """Map a key press onto an action name."""
        shift = has_modifier(modifiers, Qt.ShiftModifier)
        control = has_modifier(modifiers, Qt.ControlModifier)
        alt = has_modifier(modifiers, Qt.AltModifier)

        if key in (Qt.Key_Return, Qt.Key_Enter):
            if alt:
                return NEWLINE
            if self.ctrl_enter_sends:
                return SUBMIT if control and not shift else NEWLINE
            return NEWLINE if shift else SUBMIT
        if key == Qt.Key_Escape:
            return CLEAR_OR_PROPAGATE
        return INSERT

    def describe(self) -> str:
        """Human readable hint for placeholders and tooltips."""
        if self.ctrl_enter_sends:
            return "Ctrl+Enter = wyślij · Enter = nowa linia"
        return "Enter = wyślij · Shift+Enter = nowa linia"
