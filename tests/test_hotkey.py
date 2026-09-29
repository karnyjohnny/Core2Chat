"""Hotkey parsing/registration tests (Windows API is mocked off-Windows)."""

import sys

import pytest

from core.constants import HOTKEY_ID_FOCUS, HOTKEY_ID_QUICK_CHAT
from core.hotkey_manager import (MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT,
                                 MOD_WIN, HotkeyManager, format_hotkey,
                                 parse_hotkey)


# --------------------------------------------------------------------- parsing
@pytest.mark.parametrize("combo,expected_mod,expected_vk", [
    ("Win+C", MOD_WIN, 0x43),
    ("win+c", MOD_WIN, 0x43),
    ("Ctrl+Alt+Q", MOD_CONTROL | MOD_ALT, 0x51),
    ("Ctrl+Shift+F12", MOD_CONTROL | MOD_SHIFT, 0x70 + 11),
    ("Alt+Space", MOD_ALT, 0x20),
    ("Ctrl+7", MOD_CONTROL, 0x37),
])
def test_parse_valid_combinations(combo, expected_mod, expected_vk):
    modifiers, vk, error = parse_hotkey(combo)
    assert error is None
    assert vk == expected_vk
    assert modifiers & expected_mod == expected_mod
    assert modifiers & MOD_NOREPEAT == MOD_NOREPEAT   # no auto-repeat storms


@pytest.mark.parametrize("combo,why", [
    ("", "pusty"),
    ("C", "brak modyfikatora"),
    ("Win+", "brak klawisza"),
    ("Hyper+C", "nieznany modyfikator"),
    ("Win+Ż", "nieznany klawisz"),
    ("Win+Win+C", "powtórzony modyfikator"),
    ("Win+F99", "klawisz funkcyjny poza zakresem"),
])
def test_parse_invalid_combinations_report_reasons(combo, why):
    modifiers, vk, error = parse_hotkey(combo)
    assert error, why
    assert modifiers == 0 and vk == 0


def test_format_hotkey_roundtrip():
    modifiers, vk, error = parse_hotkey("Ctrl+Shift+K")
    assert error is None
    assert format_hotkey(modifiers, vk) == "Ctrl+Shift+K"
    modifiers, vk, _ = parse_hotkey("Win+C")
    assert format_hotkey(modifiers, vk) == "Win+C"
    modifiers, vk, _ = parse_hotkey("Alt+F4")
    assert format_hotkey(modifiers, vk) == "Alt+F4"


# --------------------------------------------------------------- registration
def test_register_reports_unavailable_platform_without_crashing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    manager = HotkeyManager()
    calls = []
    assert manager.register("Win+C", lambda: calls.append(1)) is False
    assert manager.last_error
    assert calls == []
    status = manager.status()
    assert status["available"] is False
    manager.shutdown()


def test_invalid_combo_is_rejected_before_touching_the_os(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    manager = HotkeyManager()
    manager._user32 = None
    manager._kernel32 = None
    manager.available = True
    assert manager.register("nonsense", lambda: None) is False
    assert "Skrót" in manager.last_error
    manager.shutdown()


def test_registration_failure_is_reported_not_fatal(monkeypatch):
    """Simulates another process owning Win+C (Win32 error 1409)."""
    monkeypatch.setattr(sys, "platform", "win32")
    manager = HotkeyManager()

    class FakeUser32(object):
        def RegisterHotKey(self, *_args):
            return 0

        def GetMessageW(self, *_args):
            return 0

        def UnregisterHotKey(self, *_args):
            return 1

    class FakeKernel32(object):
        def GetLastError(self):
            return 1409

    manager._user32 = FakeUser32()
    manager._kernel32 = FakeKernel32()
    manager.available = True
    assert manager.register("Win+C", lambda: None) is False
    assert "zajęty" in manager.last_error
    assert manager.status()["registered"] == {}
    manager.shutdown()


def test_successful_registration_records_the_combo(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    manager = HotkeyManager()

    class FakeUser32(object):
        def __init__(self):
            self.registered = []

        def RegisterHotKey(self, _hwnd, hotkey_id, modifiers, vk):
            self.registered.append((hotkey_id, modifiers, vk))
            return 1

        def GetMessageW(self, *_args):
            return 0            # exit the loop immediately

        def UnregisterHotKey(self, *_args):
            return 1

    class FakeKernel32(object):
        def GetLastError(self):
            return 0

    fake = FakeUser32()
    manager._user32 = fake
    manager._kernel32 = FakeKernel32()
    manager.available = True
    fired = []
    assert manager.register("Win+C", lambda: fired.append(True),
                            HOTKEY_ID_QUICK_CHAT) is True
    assert fake.registered == [(HOTKEY_ID_QUICK_CHAT,
                                MOD_WIN | MOD_NOREPEAT, 0x43)]
    assert manager.status()["registered"][HOTKEY_ID_QUICK_CHAT] == "Win+C"
    manager.unregister(HOTKEY_ID_QUICK_CHAT)
    assert manager.status()["registered"] == {}
    manager.shutdown()


def test_dispatch_invokes_callback_and_survives_errors(monkeypatch):
    manager = HotkeyManager()

    def boom():
        raise RuntimeError("callback failure")

    fired = []
    manager._callbacks[HOTKEY_ID_QUICK_CHAT] = lambda: fired.append(1)
    manager._callbacks[HOTKEY_ID_FOCUS] = boom
    manager._dispatch(HOTKEY_ID_QUICK_CHAT)
    manager._dispatch(HOTKEY_ID_FOCUS)          # must not propagate
    manager._dispatch(99999)                    # unknown id is ignored
    assert fired == [1]


def test_shutdown_is_idempotent():
    manager = HotkeyManager()
    manager.shutdown()
    manager.shutdown()
    assert manager.status()["thread_alive"] is False
