"""Application configuration: typed settings with validation and atomic save.

The API key is *not* stored here - see :mod:`core.security`.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

from core.constants import (ACCENT_COLOR, DEFAULT_CONNECT_TIMEOUT,
                            DEFAULT_CONTEXT_LIMIT_PERCENT,
                            DEFAULT_FONT_SIZE, DEFAULT_CODE_FONT_SIZE,
                            DEFAULT_MAX_RETRIES, DEFAULT_QUICK_CHAT_HOTKEY,
                            DEFAULT_READ_TIMEOUT, DEFAULT_SLIDING_WINDOW_MESSAGES,
                            GEMINI_DEFAULT_API_REVISION,
                            GEMINI_DEFAULT_BASE_URL, GEMINI_PROVIDER_ID)
from models.chat_models import StateMode

_lock = threading.RLock()

def _declared_type_name(spec: Any) -> str:
    declared = spec.type
    if isinstance(declared, str):
        return declared.strip()
    return getattr(declared, "__name__", str(declared))


_TRUE_STRINGS = ("1", "true", "yes", "on", "t", "y")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in _TRUE_STRINGS


_BOOL_FIELDS = ("dark_theme", "start_minimized", "minimize_to_tray",
                "close_to_tray", "single_instance", "auto_compact_context",
                "count_tokens_before_send", "developer_logging",
                "api_diagnostics", "show_thinking", "allow_legacy_models",
                "store_remote_interactions", "prompt_on_large_context",
                "confirm_delete_session", "auto_scroll", "portable_mode")


@dataclass
class AppSettings:
    """Every user-tunable option, grouped by settings dialog tab."""

    # general
    language: str = "pl"
    start_minimized: bool = False
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    single_instance: bool = True
    portable_mode: bool = False

    # appearance
    dark_theme: bool = True
    font_size: int = DEFAULT_FONT_SIZE
    code_font_size: int = DEFAULT_CODE_FONT_SIZE
    message_spacing: str = "compact"
    accent_color: str = ACCENT_COLOR

    # api
    provider_id: str = GEMINI_PROVIDER_ID
    model_id: str = ""
    base_url: str = GEMINI_DEFAULT_BASE_URL
    api_revision: str = GEMINI_DEFAULT_API_REVISION
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    model_cache_ttl_seconds: int = 900

    # context
    context_limit_percent: int = DEFAULT_CONTEXT_LIMIT_PERCENT
    sliding_window_messages: int = DEFAULT_SLIDING_WINDOW_MESSAGES
    auto_compact_context: bool = True
    count_tokens_before_send: bool = True
    prompt_on_large_context: bool = True
    min_cached_tokens: int = 4096
    state_mode: str = StateMode.LOCAL
    max_output_tokens: int = 0
    thinking_level: str = ""

    # hotkeys
    quick_chat_hotkey: str = DEFAULT_QUICK_CHAT_HOTKEY
    send_with_ctrl_enter: bool = False

    # privacy
    store_remote_interactions: bool = False
    attachment_retention_days: int = 1
    remote_file_retention_days: int = 2
    log_level: str = "INFO"
    developer_logging: bool = False
    api_diagnostics: bool = False
    show_thinking: bool = False

    # attachments
    max_text_attachment_kb: int = 2048
    max_image_attachment_kb: int = 8192

    # ui state (not shown in settings dialog)
    last_session_id: Optional[int] = None
    sidebar_width: int = 240
    window_geometry: str = ""
    auto_scroll: bool = True
    confirm_delete_session: bool = True

    # advanced
    allow_legacy_models: bool = False
    experimental_features: List[str] = field(default_factory=list)

    # ------------------------------------------------------------- conversion
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AppSettings":
        known = {f.name for f in fields(cls)}
        kwargs: Dict[str, Any] = {}
        for key, value in (payload or {}).items():
            if key in known:
                kwargs[key] = value
        return cls(**kwargs)

    # ------------------------------------------------------------- validation
    def coerce_types(self) -> List[str]:
        """Fix JSON type drift (e.g. ``"12"`` stored instead of ``12``).

        ``dataclasses.Field.type`` is the annotation itself - a ``type`` object
        on CPython, or a string when ``from __future__ import annotations`` is
        used - so both forms are handled explicitly.
        """
        warnings: List[str] = []
        for spec in fields(self):
            current = getattr(self, spec.name)
            if current is None:
                continue
            declared = _declared_type_name(spec)
            if declared == "bool":
                if not isinstance(current, bool):
                    setattr(self, spec.name, _as_bool(current))
                    warnings.append(spec.name)
            elif declared == "int":
                if not isinstance(current, int) or isinstance(current, bool):
                    try:
                        setattr(self, spec.name, int(float(current)))
                        warnings.append(spec.name)
                    except (TypeError, ValueError):
                        warnings.append(spec.name)
            elif declared == "float":
                if not isinstance(current, float):
                    try:
                        setattr(self, spec.name, float(current))
                        warnings.append(spec.name)
                    except (TypeError, ValueError):
                        warnings.append(spec.name)
            elif declared == "str":
                if not isinstance(current, str):
                    setattr(self, spec.name, str(current))
                    warnings.append(spec.name)
            elif declared == "List[str]":
                if not isinstance(current, list):
                    setattr(self, spec.name, [])
                    warnings.append(spec.name)
        return warnings

    def validate(self) -> List[str]:
        """Return human-readable validation errors; clamps trivially bad values."""
        errors: List[str] = []

        def clamp(attr: str, low: float, high: float) -> None:
            value = getattr(self, attr)
            try:
                value = type(low)(value)
            except (TypeError, ValueError):
                errors.append("Nieprawidłowa wartość: %s" % attr)
                setattr(self, attr, low)
                return
            if value < low or value > high:
                errors.append("%s poza zakresem (%s-%s): ustawiono %s"
                              % (attr, low, high, value))
            setattr(self, attr, max(low, min(high, value)))

        clamp("font_size", 7, 24)
        clamp("code_font_size", 7, 24)
        clamp("connect_timeout", 1.0, 120.0)
        clamp("read_timeout", 5.0, 900.0)
        clamp("max_retries", 0, 5)
        clamp("context_limit_percent", 10, 100)
        clamp("sliding_window_messages", 2, 500)
        clamp("attachment_retention_days", 0, 365)
        clamp("remote_file_retention_days", 0, 30)
        clamp("max_text_attachment_kb", 16, 204800)
        clamp("max_image_attachment_kb", 64, 102400)
        clamp("model_cache_ttl_seconds", 0, 86400)
        clamp("max_output_tokens", 0, 1000000)
        clamp("min_cached_tokens", 0, 10000000)
        clamp("sidebar_width", 160, 600)

        if self.message_spacing not in ("compact", "comfortable"):
            errors.append("message_spacing: nieznana wartość, ustawiono compact")
            self.message_spacing = "compact"
        if self.state_mode not in StateMode.ALL:
            errors.append("state_mode: nieznana wartość, ustawiono local")
            self.state_mode = StateMode.LOCAL
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            errors.append("log_level: nieznana wartość, ustawiono INFO")
            self.log_level = "INFO"
        self.log_level = self.log_level.upper()
        if self.thinking_level and self.thinking_level not in (
                "minimal", "low", "medium", "high"):
            errors.append("thinking_level: nieznana wartość, wyczyszczono")
            self.thinking_level = ""
        if self.base_url:
            if not self.base_url.startswith(("http://", "https://")):
                errors.append("base_url musi zaczynać się od http(s)://")
                self.base_url = GEMINI_DEFAULT_BASE_URL
            self.base_url = self.base_url.rstrip("/")
        if self.language not in ("pl", "en"):
            self.language = "pl"
        if isinstance(self.experimental_features, list):
            self.experimental_features = [str(x) for x in
                                          self.experimental_features][:32]
        else:
            self.experimental_features = []
        return errors

    # ----------------------------------------------------------------- limits
    @property
    def max_text_attachment_bytes(self) -> int:
        return int(self.max_text_attachment_kb) * 1024

    @property
    def max_image_attachment_bytes(self) -> int:
        return int(self.max_image_attachment_kb) * 1024


class ConfigStore(object):
    """Loads/saves :class:`AppSettings` from a JSON file (atomic write)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.settings = AppSettings()
        self.last_errors: List[str] = []
        self.loaded = False

    def load(self) -> AppSettings:
        with _lock:
            payload: Dict[str, Any] = {}
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as handle:
                        raw = json.load(handle)
                    if isinstance(raw, dict):
                        payload = raw.get("settings", raw)
                except (OSError, ValueError):
                    self.last_errors = ["Nie udało się odczytać konfiguracji; "
                                        "użyto wartości domyślnych."]
                    payload = {}
            self.settings = AppSettings.from_dict(payload)
            drift = self.settings.coerce_types()
            if drift:
                self.last_errors.append("Skorygowano typy pól: %s"
                                        % ", ".join(drift[:8]))
            self.last_errors.extend(self.settings.validate())
            self.loaded = True
            return self.settings

    def save(self, settings: Optional[AppSettings] = None) -> Tuple[bool, List[str]]:
        with _lock:
            candidate = settings or self.settings
            errors = candidate.coerce_types()
            errors = errors + candidate.validate()
            blocking = [e for e in errors if "poza zakresem" in e
                        or "musi" in e]
            directory = os.path.dirname(self.path) or "."
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            payload = {"version": 1, "settings": candidate.to_dict()}
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=1, ensure_ascii=False)
                os.replace(tmp, self.path)
                try:
                    if os.name != "nt":
                        os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                self.last_errors = ["Zapis konfiguracji nie powiódł się: %s" % exc]
                return False, self.last_errors
            self.settings = candidate
            self.last_errors = errors
            return True, (blocking or errors)

    # ------------------------------------------------------ exported snapshot
    def export_snapshot(self) -> Dict[str, Any]:
        """Settings export that can never contain a secret."""
        data = self.settings.to_dict()
        for key in list(data.keys()):
            low = key.lower()
            if any(word in low for word in ("key", "token", "secret")):
                data[key] = "<REDACTED>"
        return {"version": 1, "app": "Core2Chat", "settings": data}


def default_settings() -> AppSettings:
    settings = AppSettings()
    settings.validate()
    return settings


def bool_field_names() -> Tuple[str, ...]:
    return _BOOL_FIELDS
