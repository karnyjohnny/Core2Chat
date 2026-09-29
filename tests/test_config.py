"""Configuration tests: defaults, validation, persistence, secret-free export."""

# core2chat:allow-fake-secret - contains a FAKE key shape used to prove
# that settings exports never leak credentials.

import json
import os

import pytest

from core.config import AppSettings, ConfigStore, default_settings
from core.constants import (DEFAULT_ACTIVATION_HOTKEY,
                            DEFAULT_CONTEXT_LIMIT_PERCENT,
                            GEMINI_DEFAULT_BASE_URL)
from models.chat_models import StateMode


@pytest.fixture
def store(tmp_path):
    return ConfigStore(str(tmp_path / "config.json"))


# -------------------------------------------------------------------- defaults
def test_defaults_match_the_specification(store):
    settings = store.load()
    assert settings.language == "pl"
    assert settings.dark_theme is True
    assert settings.provider_id == "gemini"
    assert settings.base_url == GEMINI_DEFAULT_BASE_URL
    assert settings.activation_hotkey == DEFAULT_ACTIVATION_HOTKEY
    assert settings.context_limit_percent == DEFAULT_CONTEXT_LIMIT_PERCENT
    assert settings.state_mode == StateMode.LOCAL
    assert settings.close_to_tray is True
    assert settings.single_instance is True
    assert settings.log_level == "INFO"


def test_defaults_are_valid_without_errors(store):
    store.load()
    assert store.last_errors == []


def test_loading_missing_file_creates_nothing(store, tmp_path):
    settings = store.load()
    assert settings is not None
    assert not os.path.exists(store.path)      # only saved on demand


# --------------------------------------------------------------- persistence
def test_save_and_reload_roundtrip(store, tmp_path):
    settings = store.load()
    settings.model_id = "gemini-3.8-flash"
    settings.font_size = 13
    settings.context_limit_percent = 70
    settings.state_mode = StateMode.STATEFUL
    settings.experimental_features = ["cache-inspector"]
    ok, errors = store.save(settings)
    assert ok is True and errors == []

    second = ConfigStore(store.path)
    reloaded = second.load()
    assert reloaded.model_id == "gemini-3.8-flash"
    assert reloaded.font_size == 13
    assert reloaded.context_limit_percent == 70
    assert reloaded.state_mode == StateMode.STATEFUL
    assert reloaded.experimental_features == ["cache-inspector"]


def test_config_file_permissions_are_restricted(store, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits do not apply on Windows")
    store.save(store.load())
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_renamed_hotkey_setting_is_migrated(store):
    """Upgrade path: 'quick_chat_hotkey' (Quick Chat removed) must not be lost."""
    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1,
                   "settings": {"quick_chat_hotkey": "Ctrl+Alt+Q"}}, handle)
    settings = store.load()
    assert settings.activation_hotkey == "Ctrl+Alt+Q"
    assert not hasattr(settings, "quick_chat_hotkey")
    # Zapis utrwala nową nazwę.
    store.save(settings)
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["settings"]["activation_hotkey"] == "Ctrl+Alt+Q"
    assert "quick_chat_hotkey" not in raw["settings"]


def test_unknown_keys_are_ignored(store):
    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "settings": {"model_id": "m",
                                              "field_from_future": 42}},
                  handle)
    settings = store.load()
    assert settings.model_id == "m"
    assert not hasattr(settings, "field_from_future")


def test_corrupted_config_falls_back_to_defaults(store):
    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("{ broken json")
    settings = store.load()
    assert settings.language == "pl"
    assert store.last_errors


def test_json_type_drift_is_coerced(store):
    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump({"settings": {"font_size": "12", "dark_theme": "false",
                                "read_timeout": "45"}}, handle)
    settings = store.load()
    assert settings.font_size == 12
    assert settings.dark_theme is False
    assert settings.read_timeout == 45.0


# ----------------------------------------------------------------- validation
def test_out_of_range_values_are_clamped_with_message():
    settings = AppSettings(font_size=99, connect_timeout=0.001,
                           context_limit_percent=500, sliding_window_messages=0)
    errors = settings.validate()
    assert settings.font_size == 24
    assert settings.connect_timeout == 1.0
    assert settings.context_limit_percent == 100
    assert settings.sliding_window_messages == 2
    assert any("font_size" in e for e in errors)


def test_invalid_enums_are_rejected():
    settings = AppSettings(state_mode="quantum", message_spacing="huge",
                           log_level="trace", thinking_level="ultra",
                           language="de")
    errors = settings.validate()
    assert settings.state_mode == StateMode.LOCAL
    assert settings.message_spacing == "compact"
    assert settings.log_level == "INFO"
    assert settings.thinking_level == ""
    assert settings.language == "pl"
    assert len(errors) >= 4


def test_invalid_base_url_is_rejected():
    settings = AppSettings(base_url="ftp://example.com")
    errors = settings.validate()
    assert settings.base_url == GEMINI_DEFAULT_BASE_URL
    assert any("base_url" in e for e in errors)


def test_base_url_trailing_slash_is_normalised():
    settings = AppSettings(base_url="https://example.com///")
    settings.validate()
    assert settings.base_url == "https://example.com"


def test_negative_limits_are_clamped():
    settings = AppSettings(max_text_attachment_kb=-5, max_output_tokens=-1,
                           min_cached_tokens=-100)
    settings.validate()
    assert settings.max_text_attachment_kb >= 16
    assert settings.max_output_tokens == 0
    assert settings.min_cached_tokens == 0


def test_byte_limit_helpers_follow_the_settings():
    settings = AppSettings(max_text_attachment_kb=1024,
                           max_image_attachment_kb=2048)
    assert settings.max_text_attachment_bytes == 1024 * 1024
    assert settings.max_image_attachment_bytes == 2048 * 1024


# ------------------------------------------------------------------- security
def test_export_snapshot_contains_no_secret_material(store, monkeypatch):
    """A stored key must never travel through the settings export."""
    from core.logging_setup import register_secret

    secret = "AIzaSyFAKE-EXPORT-CHECK-1234567890"
    register_secret(secret)
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    settings = store.load()
    settings.model_id = "gemini-3.8-flash"
    store.save(settings)
    snapshot = store.export_snapshot()
    blob = json.dumps(snapshot)
    assert snapshot["app"] == "Core2Chat"
    assert snapshot["version"] == 1
    assert snapshot["settings"]["model_id"] == "gemini-3.8-flash"
    assert secret not in blob
    assert "AIza" not in blob
    for forbidden in ("api_key", "apikey", "secret", "password", "credential"):
        assert forbidden not in blob.lower()


def test_config_never_stores_an_api_key_field(store):
    """No credential-shaped field may exist in AppSettings at all."""
    from core.config import AppSettings as Settings
    from dataclasses import fields as dc_fields

    names = {f.name for f in dc_fields(Settings)}
    for forbidden in ("api_key", "apikey", "secret", "password", "credential",
                      "access_token", "refresh_token"):
        assert forbidden not in names
    # Fields merely *named* after tokens must be numeric counters, not strings.
    sample = Settings()
    for name in names:
        if "token" in name:
            assert isinstance(getattr(sample, name), int), name


def test_save_failure_is_reported(store, monkeypatch):
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(
        OSError("disk full")))
    ok, errors = store.save(store.load())
    assert ok is False
    assert any("nie powiódł" in e for e in errors)


def test_default_settings_helper():
    settings = default_settings()
    assert settings.validate() == []
    assert settings.provider_id == "gemini"
