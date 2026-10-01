"""Settings dialog (specification §26) with validation before saving."""

from typing import Any, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from core.config import (CLOSE_ACTION_LABELS, CLOSE_ACTION_MINIMIZE,
                         AppSettings)
from core.constants import (API_KEY_ENV_VAR, APP_NAME, GEMINI_PROVIDER_ID)
from core.hotkey_manager import parse_hotkey
from core.security import METHOD_DPAPI
from models.chat_models import StateMode

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class SettingsDialog(QDialog):
    """Tabbed editor over :class:`AppSettings` plus the API key store."""

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super(SettingsDialog, self).__init__(parent)
        self.context = context
        self.setWindowTitle("Ustawienia")
        self.setMinimumWidth(560)
        self._settings = AppSettings.from_dict(context.settings.to_dict())
        self._key_changed = False
        self._key_value = ""

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_general(), "Ogólne")
        self._tabs.addTab(self._build_appearance(), "Wygląd")
        self._tabs.addTab(self._build_api(), "API")
        self._tabs.addTab(self._build_context(), "Kontekst")
        self._tabs.addTab(self._build_hotkeys(), "Skróty")
        self._tabs.addTab(self._build_privacy(), "Prywatność")
        self._tabs.addTab(self._build_advanced(), "Zaawansowane")
        # Opis dostępności modeli powstaje po zbudowaniu wszystkich zakładek:
        # widget lives w "Zaawansowane", a wcześniej był odpytywany z "API".
        self._describe_availability()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel
                                   | QDialogButtonBox.Apply)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        apply_button = buttons.button(QDialogButtonBox.Apply)
        if apply_button is not None:
            apply_button.clicked.connect(self._on_apply)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ tabs
    def _build_general(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)
        self.language = QComboBox(panel)
        self.language.addItem("Polski", "pl")
        self.language.addItem("English", "en")
        self.language.setCurrentIndex(0 if self._settings.language == "pl" else 1)
        form.addRow("Język interfejsu:", self.language)

        self.start_minimized = QCheckBox("Uruchom zminimalizowany", panel)
        self.start_minimized.setChecked(self._settings.start_minimized)
        form.addRow(self.start_minimized)

        self.minimize_to_tray = QCheckBox("Minimalizuj do zasobnika", panel)
        self.minimize_to_tray.setChecked(self._settings.minimize_to_tray)
        form.addRow(self.minimize_to_tray)

        self.close_action = QComboBox(panel)
        for value, label in CLOSE_ACTION_LABELS.items():
            self.close_action.addItem(label, value)
        index = self.close_action.findData(self._settings.close_action)
        self.close_action.setCurrentIndex(max(0, index))
        self.close_action.setToolTip(
            "Co robi przycisk X na oknie programu.\n"
            "„Pytaj za każdym razem” (domyślne) pokazuje okno dialogowe "
            "Anuluj / Zminimalizuj / Zamknij program. Zaznaczenie w nim "
            "„Zapamiętaj wybór” ustawia tę opcję na wybraną odpowiedź.")
        form.addRow("Zachowanie zamknięcia okna:", self.close_action)

        self.close_hint = QLabel("")
        self.close_hint.setObjectName("MessageMeta")
        self.close_hint.setWordWrap(True)
        self.close_action.currentIndexChanged.connect(
            lambda _index: self._describe_close_action())
        self._describe_close_action()
        form.addRow("", self.close_hint)

        self.tray_menu = QCheckBox("Menu w ikonie zasobnika (tray icon)", panel)
        self.tray_menu.setChecked(self._settings.tray_menu_enabled)
        self.tray_menu.setToolTip(
            "Funkcja zbufowana (eksperymentalna): może działać, może nie "
            "działać - zależy od powłoki systemowej i wersji Windows.\n"
            "Gdy WYŁĄCZONE (domyślnie): ikona nie ma menu, a zarówno lewy, jak "
            "i prawy przycisk myszy przywracają okno programu.")
        form.addRow(self.tray_menu)

        self.tray_menu_warning = QLabel(
            "⚠ Funkcja zbufowana - menu zasobnika może działać "
            "niestabilnie na starszych systemach. Gdy wyłączone, ikona "
            "przywraca okno na lewy i prawy przycisk myszy.")
        self.tray_menu_warning.setObjectName("MessageMeta")
        self.tray_menu_warning.setWordWrap(True)
        self.tray_menu_warning.setVisible(self.tray_menu.isChecked())
        self.tray_menu.toggled.connect(self.tray_menu_warning.setVisible)
        form.addRow("", self.tray_menu_warning)

        self.single_instance = QCheckBox("Tylko jedna instancja aplikacji", panel)
        self.single_instance.setChecked(self._settings.single_instance)
        form.addRow(self.single_instance)

        self.confirm_delete = QCheckBox("Potwierdzaj usunięcie rozmowy", panel)
        self.confirm_delete.setChecked(self._settings.confirm_delete_session)
        form.addRow(self.confirm_delete)
        return panel

    def _build_appearance(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)
        self.dark_theme = QCheckBox("Ciemny motyw", panel)
        self.dark_theme.setChecked(self._settings.dark_theme)
        form.addRow(self.dark_theme)

        self.font_size = QSpinBox(panel)
        self.font_size.setRange(7, 24)
        self.font_size.setValue(int(self._settings.font_size))
        self.font_size.setSuffix(" pt")
        form.addRow("Rozmiar czcionki:", self.font_size)

        self.code_font_size = QSpinBox(panel)
        self.code_font_size.setRange(7, 24)
        self.code_font_size.setValue(int(self._settings.code_font_size))
        self.code_font_size.setSuffix(" pt")
        form.addRow("Czcionka kodu:", self.code_font_size)

        self.spacing = QComboBox(panel)
        self.spacing.addItem("Kompaktowe", "compact")
        self.spacing.addItem("Wygodne", "comfortable")
        self.spacing.setCurrentIndex(0 if self._settings.message_spacing
                                     == "compact" else 1)
        form.addRow("Odstępy wiadomości:", self.spacing)

        self.accent = QLineEdit(panel)
        self.accent.setText(self._settings.accent_color)
        self.accent.setPlaceholderText("#007acc")
        self.accent.setToolTip("Kolor akcentu w formacie #rrggbb")
        form.addRow("Kolor akcentu:", self.accent)

        self.auto_scroll = QCheckBox("Automatyczne przewijanie podczas "
                                     "generowania", panel)
        self.auto_scroll.setChecked(self._settings.auto_scroll)
        form.addRow(self.auto_scroll)
        return panel

    def _build_api(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        form = QFormLayout()

        provider_label = QLabel("Google Gemini (Interactions API)", panel)
        provider_label.setObjectName("DialogTitle")
        form.addRow("Dostawca:", provider_label)

        self.api_key = QLineEdit(panel)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("••••••••")
        stored = self.context.secret_store.masked("api_key")
        self.api_key.setText("")
        self.api_key.textEdited.connect(self._on_key_edited)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key, 1)
        self.clear_key = QPushButton("Usuń klucz")
        self.clear_key.clicked.connect(self._on_clear_key)
        key_row.addWidget(self.clear_key)
        form.addRow("Klucz API:", key_row)

        self.key_status = QLabel("")
        self.key_status.setObjectName("MessageMeta")
        self.key_status.setWordWrap(True)
        method = self.context.secret_store.method()
        method_text = ("DPAPI (Windows)" if method == METHOD_DPAPI else
                       "plik chroniony (bez DPAPI - nie jest to szyfrowanie)")
        self.key_status.setText("Zapisano: %s · metoda: %s · zmienna %s ma "
                                "pierwszeństwo" % (stored, method_text,
                                                  API_KEY_ENV_VAR))
        form.addRow("", self.key_status)

        self.base_url = QLineEdit(panel)
        self.base_url.setText(self._settings.base_url)
        form.addRow("Adres bazowy API:", self.base_url)

        self.api_revision = QLineEdit(panel)
        self.api_revision.setText(self._settings.api_revision)
        self.api_revision.setPlaceholderText("puste = wersja domyślna API")
        self.api_revision.setToolTip("Nagłówek Api-Revision (pozostaw puste, "
                                     "chyba że dokumentacja wymaga inaczej)")
        form.addRow("Api-Revision:", self.api_revision)

        self.connect_timeout = QDoubleSpinBox(panel)
        self.connect_timeout.setRange(1.0, 120.0)
        self.connect_timeout.setValue(float(self._settings.connect_timeout))
        self.connect_timeout.setSuffix(" s")
        form.addRow("Limit połączenia:", self.connect_timeout)

        self.read_timeout = QDoubleSpinBox(panel)
        self.read_timeout.setRange(5.0, 900.0)
        self.read_timeout.setValue(float(self._settings.read_timeout))
        self.read_timeout.setSuffix(" s")
        form.addRow("Limit odczytu:", self.read_timeout)

        self.max_retries = QSpinBox(panel)
        self.max_retries.setRange(0, 5)
        self.max_retries.setValue(int(self._settings.max_retries))
        form.addRow("Ponowienia (GET/429):", self.max_retries)

        self.model_ttl = QSpinBox(panel)
        self.model_ttl.setRange(0, 86400)
        self.model_ttl.setValue(int(self._settings.model_cache_ttl_seconds))
        self.model_ttl.setSuffix(" s")
        self.model_ttl.setToolTip("0 = zawsze pobieraj listę modeli z API")
        form.addRow("Cache listy modeli:", self.model_ttl)

        validate = QPushButton("Sprawdź połączenie")
        validate.clicked.connect(self._validate_connection)
        form.addRow("", validate)
        layout.addLayout(form)
        layout.addStretch(1)
        return panel

    def _build_context(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)

        self.limit_percent = QSpinBox(panel)
        self.limit_percent.setRange(10, 100)
        self.limit_percent.setValue(int(self._settings.context_limit_percent))
        self.limit_percent.setSuffix(" %")
        self.limit_percent.setToolTip("Próg ostrzeżenia względem limitu modelu")
        form.addRow("Próg kontekstu:", self.limit_percent)

        self.sliding_window = QSpinBox(panel)
        self.sliding_window.setRange(2, 500)
        self.sliding_window.setValue(int(self._settings.sliding_window_messages))
        form.addRow("Okno historii (wiadomości):", self.sliding_window)

        self.auto_compact = QCheckBox("Automatycznie skracaj kontekst", panel)
        self.auto_compact.setChecked(self._settings.auto_compact_context)
        form.addRow(self.auto_compact)

        self.count_tokens = QCheckBox("Zliczaj tokeny przez API przed wysłaniem",
                                      panel)
        self.count_tokens.setChecked(self._settings.count_tokens_before_send)
        self.count_tokens.setToolTip("Dokładny licznik dostawcy; kosztuje jedno "
                                     "dodatkowe zapytanie.")
        form.addRow(self.count_tokens)

        self.prompt_large = QCheckBox("Ostrzegaj przy dużym kontekście", panel)
        self.prompt_large.setChecked(self._settings.prompt_on_large_context)
        form.addRow(self.prompt_large)

        self.state_mode = QComboBox(panel)
        for value, label in StateMode.LABELS.items():
            self.state_mode.addItem(label, value)
        index = self.state_mode.findData(self._settings.state_mode)
        self.state_mode.setCurrentIndex(max(0, index))
        form.addRow("Tryb rozmowy:", self.state_mode)

        self.max_output = QSpinBox(panel)
        self.max_output.setRange(0, 1_000_000)
        self.max_output.setSingleStep(256)
        self.max_output.setValue(int(self._settings.max_output_tokens))
        self.max_output.setSpecialValueText("domyślny modelu")
        form.addRow("Maks. tokenów odpowiedzi:", self.max_output)

        self.thinking_level = QComboBox(panel)
        self.thinking_level.addItem("domyślny", "")
        for level in ("minimal", "low", "medium", "high"):
            self.thinking_level.addItem(level, level)
        index = self.thinking_level.findData(self._settings.thinking_level)
        self.thinking_level.setCurrentIndex(max(0, index))
        form.addRow("Poziom rozumowania:", self.thinking_level)

        self.min_cached = QSpinBox(panel)
        self.min_cached.setRange(0, 10_000_000)
        self.min_cached.setSingleStep(1024)
        self.min_cached.setValue(int(self._settings.min_cached_tokens))
        self.min_cached.setToolTip("Minimalna liczba tokenów, od której "
                                   "niejawne buforowanie ma sens")
        form.addRow("Próg buforowania:", self.min_cached)
        return panel

    def _build_hotkeys(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)
        self.activation_hotkey = QLineEdit(panel)
        self.activation_hotkey.setText(self._settings.activation_hotkey)
        self.activation_hotkey.setToolTip(
            "Globalny skrót: pokazuje główne okno i ustawia fokus na polu "
            "wpisywania. Format: Win+C, Ctrl+Alt+Q, Shift+F9")
        form.addRow("Skrót globalny (pokaż okno):", self.activation_hotkey)

        self.activation_hotkey.textChanged.connect(
            lambda _text: self._validate_hotkey())
        self.hotkey_status = QLabel("")
        self.hotkey_status.setObjectName("MessageMeta")
        self.hotkey_status.setWordWrap(True)
        form.addRow("", self.hotkey_status)

        self.ctrl_enter = QCheckBox("Ctrl+Enter wysyła (Enter = nowa linia)",
                                    panel)
        self.ctrl_enter.setChecked(self._settings.send_with_ctrl_enter)
        form.addRow(self.ctrl_enter)

        info = QLabel("Pozostałe skróty: Ctrl+N nowa rozmowa · Ctrl+K "
                      "szukaj · Ctrl+L fokus na pole wpisywania · "
                      "Ctrl+Shift+C kopiuj odpowiedź · Ctrl+, ustawienia · "
                      "Enter wyślij · Shift+Enter nowa linia · Esc stop")
        info.setObjectName("MessageMeta")
        info.setWordWrap(True)
        form.addRow(info)
        self._validate_hotkey()
        return panel

    def _build_privacy(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)
        self.store_remote = QCheckBox("Zezwalaj dostawcy na zapis interakcji "
                                      "(store=true)", panel)
        self.store_remote.setChecked(self._settings.store_remote_interactions)
        form.addRow(self.store_remote)

        self.attachment_retention = QSpinBox(panel)
        self.attachment_retention.setRange(0, 365)
        self.attachment_retention.setValue(int(self._settings.attachment_retention_days))
        self.attachment_retention.setSuffix(" dni")
        form.addRow("Lokalne załączniki (bez wiadomości):", self.attachment_retention)

        self.remote_retention = QSpinBox(panel)
        self.remote_retention.setRange(0, 30)
        self.remote_retention.setValue(int(self._settings.remote_file_retention_days))
        self.remote_retention.setSuffix(" dni")
        form.addRow("Zdalne pliki w API:", self.remote_retention)

        self.log_level = QComboBox(panel)
        for level in LOG_LEVELS:
            self.log_level.addItem(level, level)
        index = self.log_level.findData(self._settings.log_level)
        self.log_level.setCurrentIndex(max(0, index))
        form.addRow("Poziom logów:", self.log_level)

        note = QLabel("Historia rozmów jest przechowywana lokalnie w SQLite. "
                      "Treść wiadomości jest wysyłana do wybranego dostawcy - "
                      "„lokalna historia” nie oznacza, że nic nie opuściło "
                      "komputera.")
        note.setObjectName("MessageMeta")
        note.setWordWrap(True)
        form.addRow(note)
        return panel

    def _build_advanced(self) -> QWidget:
        panel = QWidget(self)
        form = QFormLayout(panel)
        self.developer_logging = QCheckBox("Logi deweloperskie (bez treści "
                                           "promptów)", panel)
        self.developer_logging.setChecked(self._settings.developer_logging)
        form.addRow(self.developer_logging)

        self.api_diagnostics = QCheckBox("Diagnostyka odpowiedzi API", panel)
        self.api_diagnostics.setChecked(self._settings.api_diagnostics)
        form.addRow(self.api_diagnostics)

        self.show_thinking = QCheckBox("Pokazuj podsumowanie rozumowania", panel)
        self.show_thinking.setChecked(self._settings.show_thinking)
        self.show_thinking.setToolTip("Wyświetlane tylko wtedy, gdy model "
                                      "zwróci jawne podsumowanie.")
        form.addRow(self.show_thinking)

        self.allow_legacy = QCheckBox("Pokazuj starsze generacje modeli", panel)
        self.allow_legacy.setChecked(self._settings.allow_legacy_models)
        form.addRow(self.allow_legacy)

        self.hide_unavailable = QCheckBox(
            "Ukryj nieobsługiwane modele Gemini", panel)
        self.hide_unavailable.setChecked(
            self._settings.hide_unavailable_models)
        self.hide_unavailable.setToolTip(
            "Ukrywa modele, które API odrzuciło dla tego klucza (np. HTTP 404 "
            "\"no longer available to new users\"). Lista jest oparta na "
            "faktach z API, nie na nazwach modeli.")
        form.addRow(self.hide_unavailable)

        verify_row = QHBoxLayout()
        self.verify_models = QPushButton("Zweryfikuj dostępność modeli")
        self.verify_models.setToolTip(
            "Wykonuje darmowe zapytanie countTokens dla każdego modelu "
            "(bez generowania treści), żeby zapełnić filtr przed pierwszą "
            "rozmową.")
        self.verify_models.clicked.connect(self._verify_models)
        verify_row.addWidget(self.verify_models)
        self.forget_availability = QPushButton("Wyczyść nauczone")
        self.forget_availability.setToolTip(
            "Usuwa zapisaną listę niedostępnych modeli - użyj po zmianie "
            "planu/klucza API.")
        self.forget_availability.clicked.connect(self._forget_availability)
        verify_row.addWidget(self.forget_availability)
        verify_row.addStretch(1)
        form.addRow("", verify_row)

        self.availability_status = QLabel("")
        self.availability_status.setObjectName("MessageMeta")
        self.availability_status.setWordWrap(True)
        form.addRow("", self.availability_status)

        self.max_text_kb = QSpinBox(panel)
        self.max_text_kb.setRange(16, 204800)
        self.max_text_kb.setValue(int(self._settings.max_text_attachment_kb))
        self.max_text_kb.setSuffix(" KB")
        form.addRow("Limit pliku tekstowego:", self.max_text_kb)

        self.max_image_kb = QSpinBox(panel)
        self.max_image_kb.setRange(64, 102400)
        self.max_image_kb.setValue(int(self._settings.max_image_attachment_kb))
        self.max_image_kb.setSuffix(" KB")
        form.addRow("Limit obrazu:", self.max_image_kb)

        self.sidebar_width = QSpinBox(panel)
        self.sidebar_width.setRange(160, 600)
        self.sidebar_width.setValue(int(self._settings.sidebar_width))
        self.sidebar_width.setSuffix(" px")
        form.addRow("Szerokość panelu bocznego:", self.sidebar_width)
        return panel

    # --------------------------------------------------------------- actions
    def _on_key_edited(self, text: str) -> None:
        self._key_value = text.strip()
        self._key_changed = bool(self._key_value)

    def _on_clear_key(self) -> None:
        self.context.store_api_key("")
        self.api_key.clear()
        self._key_value = ""
        self._key_changed = False
        self.key_status.setText("Klucz usunięty z magazynu.")

    def _validate_hotkey(self) -> None:
        combo = self.activation_hotkey.text().strip()
        _modifiers, _vk, error = parse_hotkey(combo)
        if error:
            self.hotkey_status.setText("⚠ %s" % error)
        else:
            self.hotkey_status.setText(
                "Poprawny skrót. Rejestracja nastąpi po zapisaniu ustawień.")

    def _describe_close_action(self) -> None:
        """Explain the current choice in plain language (no surprises at X)."""
        if getattr(self, "close_hint", None) is None:
            return                      # zakładka jeszcze nie zbudowana
        value = self.close_action.currentData()
        if value == CLOSE_ACTION_MINIMIZE:
            self.close_hint.setText(
                "Kliknięcie X chowa okno do zasobnika (albo minimalizuje na "
                "pasku zadań, gdy zasobnik jest niedostępny). Program działa "
                "dalej.")
        elif value == "exit":
            self.close_hint.setText(
                "Kliknięcie X kończy działanie programu bez pytania.")
        else:
            self.close_hint.setText(
                "Kliknięcie X pyta: Anuluj / Zminimalizuj / Zamknij program. "
                "Zaznaczenie „Zapamiętaj wybór” w tym oknie ustawia tę opcję "
                "na wybraną odpowiedź.")

    def _describe_availability(self) -> None:
        if getattr(self, "availability_status", None) is None:
            return                      # zakładka jeszcze nie zbudowana
        provider = self.context.provider
        report = getattr(provider, "availability_report", lambda: [])()
        blocked = [entry for entry in report if not entry["available"]]
        if not report:
            self.availability_status.setText(
                "Brak zapisanych faktów o dostępności. Użyj przycisku "
                "„Zweryfikuj dostępność modeli” albo poczekaj na pierwszy "
                "błąd API - filtr wypełni się automatycznie.")
            return
        self.availability_status.setText(
            "Zapisane: %d modeli, w tym %d niedostępnych%s"
            % (len(report), len(blocked),
               ("\n" + "\n".join(
                   "· %s (%s)" % (entry["model_name"],
                                  entry["reason"][:70] or "brak powodu")
                   for entry in blocked[:5])) if blocked else ""))

    def _verify_models(self) -> None:
        """Blocking on purpose: the user asked for it and it is free."""
        provider = self.context.provider
        verify = getattr(provider, "verify_models", None)
        if verify is None:
            self.availability_status.setText("Ten dostawca nie wspiera "
                                             "weryfikacji dostępności.")
            return
        from PyQt5.QtWidgets import QApplication

        self.verify_models.setEnabled(False)
        self.availability_status.setText("Weryfikuję… (darmowe countTokens)")
        QApplication.processEvents()
        try:
            results = verify(limit=20)
        except Exception as exc:
            self.availability_status.setText("Weryfikacja nie powiodła się: %s"
                                             % exc)
            self.verify_models.setEnabled(True)
            return
        blocked = [name for name, ok in results.items() if not ok]
        self.availability_status.setText(
            "Zweryfikowano %d modeli; niedostępnych: %d%s"
            % (len(results), len(blocked),
               (" (" + ", ".join(blocked[:4]) + ")") if blocked else ""))
        self.verify_models.setEnabled(True)
        self._describe_availability()

    def _forget_availability(self) -> None:
        provider = self.context.provider
        forget = getattr(provider, "forget_availability", None)
        if forget is None:
            return
        forget(None)
        self._describe_availability()

    def _validate_connection(self) -> None:
        provider = self.context.provider
        if provider is None:
            QMessageBox.warning(self, "API", "Brak dostawcy.")
            return
        key = self._key_value or self.context.resolve_api_key()
        if not key:
            QMessageBox.warning(self, "API", "Brak klucza API.")
            return
        state = provider.connectivity_check()
        messages = {
            "online": "Połączenie OK. Lista modeli pobrana pomyślnie.",
            "offline": "Brak połączenia sieciowego z API.",
            "auth_failed": "Klucz API został odrzucony.",
            "rate_limited": "Limit API osiągnięty.",
            "no_key": "Brak klucza API.",
        }
        QMessageBox.information(self, "API",
                                messages.get(state, "Nieznany stan: %s" % state))

    def _collect(self) -> AppSettings:
        settings = self._settings
        settings.language = self.language.currentData()
        settings.start_minimized = self.start_minimized.isChecked()
        settings.minimize_to_tray = self.minimize_to_tray.isChecked()
        action = self.close_action.currentData()
        settings.close_action = action
        # The legacy flag is derived, not edited: X -> minimize means the
        # window hides to the tray, anything else really quits.
        settings.close_to_tray = action == CLOSE_ACTION_MINIMIZE
        settings.tray_menu_enabled = self.tray_menu.isChecked()
        settings.single_instance = self.single_instance.isChecked()
        settings.confirm_delete_session = self.confirm_delete.isChecked()

        settings.dark_theme = self.dark_theme.isChecked()
        settings.font_size = self.font_size.value()
        settings.code_font_size = self.code_font_size.value()
        settings.message_spacing = self.spacing.currentData()
        settings.accent_color = self.accent.text().strip()
        settings.auto_scroll = self.auto_scroll.isChecked()

        settings.provider_id = GEMINI_PROVIDER_ID
        settings.base_url = self.base_url.text().strip()
        settings.api_revision = self.api_revision.text().strip()
        settings.connect_timeout = self.connect_timeout.value()
        settings.read_timeout = self.read_timeout.value()
        settings.max_retries = self.max_retries.value()
        settings.model_cache_ttl_seconds = self.model_ttl.value()

        settings.context_limit_percent = self.limit_percent.value()
        settings.sliding_window_messages = self.sliding_window.value()
        settings.auto_compact_context = self.auto_compact.isChecked()
        settings.count_tokens_before_send = self.count_tokens.isChecked()
        settings.prompt_on_large_context = self.prompt_large.isChecked()
        settings.state_mode = self.state_mode.currentData()
        settings.max_output_tokens = self.max_output.value()
        settings.thinking_level = self.thinking_level.currentData()
        settings.min_cached_tokens = self.min_cached.value()

        settings.activation_hotkey = self.activation_hotkey.text().strip()
        settings.send_with_ctrl_enter = self.ctrl_enter.isChecked()

        settings.store_remote_interactions = self.store_remote.isChecked()
        settings.attachment_retention_days = self.attachment_retention.value()
        settings.remote_file_retention_days = self.remote_retention.value()
        settings.log_level = self.log_level.currentData()

        settings.developer_logging = self.developer_logging.isChecked()
        settings.api_diagnostics = self.api_diagnostics.isChecked()
        settings.show_thinking = self.show_thinking.isChecked()
        settings.allow_legacy_models = self.allow_legacy.isChecked()
        settings.hide_unavailable_models = self.hide_unavailable.isChecked()
        settings.max_text_attachment_kb = self.max_text_kb.value()
        settings.max_image_attachment_kb = self.max_image_kb.value()
        settings.sidebar_width = self.sidebar_width.value()
        return settings

    def _validate(self) -> List[str]:
        errors: List[str] = []
        settings = self._collect()
        from gui.theme import validate_color
        if validate_color(settings.accent_color, "") == "":
            errors.append("Kolor akcentu musi mieć format #rgb lub #rrggbb.")
        _modifiers, _vk, hotkey_error = parse_hotkey(settings.activation_hotkey)
        if hotkey_error:
            errors.append("Skrót: %s" % hotkey_error)
        errors.extend(settings.validate())
        return errors

    def _on_apply(self) -> bool:
        errors = self._validate()
        blocking = [e for e in errors if "poza zakresem" in e or "musi" in e
                    or "Skrót" in e]
        if blocking:
            QMessageBox.warning(self, "Ustawienia",
                                "Popraw poniższe wartości:\n" + "\n".join(blocking[:6]))
            return False
        if self._key_changed and self._key_value:
            method = self.context.store_api_key(self._key_value)
            self._key_changed = False
            self._key_value = ""
            self.api_key.clear()
            self.key_status.setText("Klucz zapisany (metoda: %s)." % method)
        ok, save_errors = self.context.save_settings(self._collect())
        if not ok:
            QMessageBox.critical(self, "Ustawienia",
                                 "Zapis nie powiódł się:\n" + "\n".join(save_errors[:4]))
            return False
        if save_errors:
            self._tabs.widget(2).setFocus()
        return True

    def _on_accept(self) -> None:
        if self._on_apply():
            self.accept()
