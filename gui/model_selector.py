"""Model selector backed by runtime discovery (specification §25).

Shows only facts taken from the API: identifier, display name, lifecycle,
input/output limits and confirmed capabilities. No subjective ranking.
"""

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from api.provider_capabilities import lifecycle_note, summarize
from models.provider_models import ModelInfo


class ModelSelector(QWidget):
    """Compact model picker with refresh and filter."""

    model_selected = pyqtSignal(str)
    refresh_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(ModelSelector, self).__init__(parent)
        self.setObjectName("ModelSelector")
        self._models: List[ModelInfo] = []
        self._filter = ""
        self._suppress_signal = True

        self._combo = QComboBox(self)
        self._combo.setToolTip("Wybierz model (lista pobierana z API)")
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
        self._combo.setMinimumContentsLength(22)
        self._combo.currentIndexChanged.connect(self._on_changed)

        self._refresh = QPushButton("⟳")
        self._refresh.setObjectName("IconButton")
        self._refresh.setToolTip("Odśwież listę modeli")
        self._refresh.setCursor(Qt.PointingHandCursor)
        self._refresh.setFocusPolicy(Qt.NoFocus)
        self._refresh.clicked.connect(self.refresh_requested)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Model:"))
        layout.addWidget(self._combo, 1)
        layout.addWidget(self._refresh)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    # ------------------------------------------------------------------ api
    def set_models(self, models: List[ModelInfo], selected: str = "") -> None:
        self._models = list(models)
        self._populate(selected)

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").strip().lower()
        self._populate(self.current_model_id())

    def current_model_id(self) -> str:
        data = self._combo.currentData()
        return str(data) if data else ""

    def set_current_model(self, model_id: str) -> None:
        if not model_id:
            return
        index = self._combo.findData(model_id)
        if index < 0:
            return
        self._suppress_signal = True
        self._combo.setCurrentIndex(index)
        self._suppress_signal = False

    def set_busy(self, busy: bool) -> None:
        self._refresh.setEnabled(not busy)
        self._combo.setEnabled(not busy)

    def model_count(self) -> int:
        return self._combo.count()

    def describe_current(self) -> str:
        model = self._find(self.current_model_id())
        return summarize(model) if model else ""

    def set_error(self, message: str) -> None:
        self._combo.setToolTip(message or "Wybierz model")

    # -------------------------------------------------------------- internal
    def _populate(self, selected: str) -> None:
        self._suppress_signal = True
        self._combo.clear()
        for model in self._visible_models():
            label = "%s  ·  %s/%s tok." % (
                model.display_name or model.model_id,
                _compact_number(model.input_token_limit),
                _compact_number(model.output_token_limit))
            self._combo.addItem(label, model.model_id)
            index = self._combo.count() - 1
            note = lifecycle_note(model)
            tooltip = summarize(model)
            if note:
                tooltip = "%s\n%s" % (tooltip, note)
            self._combo.setItemData(index, tooltip, Qt.ToolTipRole)
        if selected:
            position = self._combo.findData(selected)
            if position >= 0:
                self._combo.setCurrentIndex(position)
        if self._combo.count() == 0:
            self._combo.addItem("(brak modeli — odśwież)", "")
        self._suppress_signal = False

    def _visible_models(self) -> List[ModelInfo]:
        if not self._filter:
            return self._models
        return [m for m in self._models
                if self._filter in m.model_id.lower()
                or self._filter in (m.display_name or "").lower()]

    def _find(self, model_id: str) -> Optional[ModelInfo]:
        for model in self._models:
            if model.model_id == model_id:
                return model
        return None

    def _on_changed(self, index: int) -> None:
        if self._suppress_signal or index < 0:
            return
        model_id = self._combo.itemData(index)
        if model_id:
            self.model_selected.emit(str(model_id))


def _compact_number(value: int) -> str:
    if not value:
        return "?"
    if value >= 1_000_000:
        text = "%.1fM" % (value / 1_000_000.0)
    elif value >= 1000:
        text = "%dk" % (value // 1000)
    else:
        text = str(value)
    return text.replace(".0M", "M")


class ModelFilterBox(QLineEdit):
    """Search field feeding :meth:`ModelSelector.set_filter`."""

    def __init__(self, selector: ModelSelector,
                 parent: Optional[QWidget] = None) -> None:
        super(ModelFilterBox, self).__init__(parent)
        self.setPlaceholderText("Filtruj modele…")
        self.setClearButtonEnabled(True)
        self.textChanged.connect(selector.set_filter)


def build_selector_with_filter(parent: Optional[QWidget] = None
                               ) -> tuple:
    """Convenience factory used by the settings dialog."""
    selector = ModelSelector(parent)
    box = ModelFilterBox(selector, parent)
    container = QWidget(parent)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(selector)
    layout.addWidget(box)
    return selector, box, container
