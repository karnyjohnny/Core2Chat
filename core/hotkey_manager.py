"""Global hotkey (Win32 RegisterHotKey) with a safe non-Windows fallback.

Windows implementation notes:

* ``RegisterHotKey`` binds the hotkey to the *calling thread*, so a dedicated
  thread owns registration and runs a ``GetMessageW`` loop.
* The thread signals Qt through a queued signal; widgets are never touched
  from it.
* Registration failure (another process owns Win+C) is reported, never fatal:
  the application keeps working without the hotkey (specification §29).
"""

import ctypes
import sys
import threading
from typing import Callable, Dict, List, Optional, Tuple

from core.constants import HOTKEY_ID_QUICK_CHAT
from core.logging_setup import get_logger

log = get_logger("hotkey")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

_MODIFIER_TOKENS: Dict[str, int] = {
    "win": MOD_WIN, "windows": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "norepeat": MOD_NOREPEAT,
}

_KEY_NAMES: Dict[str, int] = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}


def _virtual_key_code(token: str) -> Optional[int]:
    token = token.strip().lower()
    if not token:
        return None
    if token in _KEY_NAMES:
        return _KEY_NAMES[token]
    if len(token) == 1:
        code = ord(token.upper())
        if 0x30 <= code <= 0x5A:      # 0-9, A-Z
            return code
        return None
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return None


def parse_hotkey(combo: str) -> Tuple[int, int, Optional[str]]:
    """Parse ``"Win+C"`` into ``(modifiers, vk, error)``.

    Returns ``(0, 0, message)`` when the combination is invalid so the caller
    can show a settings warning instead of crashing.
    """
    raw = (combo or "").strip()
    if not raw:
        return 0, 0, "Skrót nie może być pusty."
    tokens = [t for t in raw.replace("++", "+plus").split("+") if t != ""]
    tokens = [t.replace("plus", "+") if t == "plus" else t for t in tokens]
    if len(tokens) < 2:
        return 0, 0, "Skrót wymaga modyfikatora i klawisza (np. Win+C)."
    modifiers = 0
    key_token = ""
    for token in tokens[:-1]:
        value = _MODIFIER_TOKENS.get(token.strip().lower())
        if value is None:
            return 0, 0, "Nieznany modyfikator: %s" % token
        if modifiers & value:
            return 0, 0, "Modyfikator powtórzony: %s" % token
        modifiers |= value
    key_token = tokens[-1]
    vk = _virtual_key_code(key_token)
    if vk is None:
        return 0, 0, "Nieznany klawisz: %s" % key_token
    if modifiers == 0:
        return 0, 0, "Skrót globalny wymaga co najmniej jednego modyfikatora."
    return modifiers | MOD_NOREPEAT, vk, None


def format_hotkey(modifiers: int, vk: int) -> str:
    parts: List[str] = []
    if modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & MOD_ALT:
        parts.append("Alt")
    if modifiers & MOD_SHIFT:
        parts.append("Shift")
    if modifiers & MOD_WIN:
        parts.append("Win")
    if 0x30 <= vk <= 0x39:
        parts.append(chr(vk))
    elif 0x41 <= vk <= 0x5A:
        parts.append(chr(vk))
    elif 0x70 <= vk <= 0x87:
        parts.append("F%d" % (vk - 0x70 + 1))
    else:
        name = [k for k, v in _KEY_NAMES.items() if v == vk]
        parts.append(name[0].upper() if name else hex(vk))
    return "+".join(parts)


class HotkeyManager(object):
    """Owns one or more global hotkeys."""

    def __init__(self) -> None:
        self.is_windows = sys.platform == "win32"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._registered: Dict[int, str] = {}
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self.last_error = ""
        self.available = self.is_windows
        self._user32 = None
        self._kernel32 = None
        if self.is_windows:
            try:
                self._user32 = ctypes.windll.user32      # type: ignore[attr-defined]
                self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - Windows only
                self.available = False
                self.last_error = "Win32 API niedostępne: %s" % exc

    # -------------------------------------------------------------- public
    def register(self, combo: str, callback: Callable[[], None],
                 hotkey_id: int = HOTKEY_ID_QUICK_CHAT) -> bool:
        """Register ``combo``; returns False with ``last_error`` set on failure."""
        modifiers, vk, error = parse_hotkey(combo)
        if error:
            self.last_error = error
            log.warning("hotkey.parse_failed combo=%s err=%s", combo, error)
            return False
        with self._lock:
            self._callbacks[hotkey_id] = callback
            self._registered[hotkey_id] = combo
        if not self.available:
            self.last_error = ("Globalne skróty są dostępne tylko w systemie "
                               "Windows.")
            log.info("hotkey.unavailable platform=%s", sys.platform)
            return False
        self._ensure_thread()
        return self._wait_for_registration(hotkey_id)

    def unregister(self, hotkey_id: int = HOTKEY_ID_QUICK_CHAT) -> None:
        with self._lock:
            self._callbacks.pop(hotkey_id, None)
            self._registered.pop(hotkey_id, None)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        with self._lock:
            self._callbacks.clear()
            self._registered.clear()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        log.info("hotkey.shutdown")

    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "available": self.available,
                "platform": sys.platform,
                "registered": dict(self._registered),
                "last_error": self.last_error,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
            }

    # ------------------------------------------------------------- internals
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="c2c-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def _wait_for_registration(self, hotkey_id: int,
                               timeout: float = 2.0) -> bool:
        deadline = threading.Event()
        deadline.wait(timeout)
        with self._lock:
            return hotkey_id in self._registered and not self.last_error

    def _loop(self) -> None:  # pragma: no cover - Windows-only path
        """Owns the message loop; registration happens on this thread."""
        assert self._user32 is not None and self._kernel32 is not None
        user32 = self._user32
        registered: List[int] = []
        with self._lock:
            wanted = dict(self._registered)
        for hotkey_id, combo in wanted.items():
            modifiers, vk, error = parse_hotkey(combo)
            if error:
                self.last_error = error
                continue
            if user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
                registered.append(hotkey_id)
                self.last_error = ""
                log.info("hotkey.registered id=%d combo=%s", hotkey_id, combo)
            else:
                code = self._kernel32.GetLastError()
                with self._lock:
                    self._registered.pop(hotkey_id, None)
                self.last_error = self._describe_failure(code, combo)
                log.warning("hotkey.register_failed id=%d combo=%s win32=%d",
                            hotkey_id, combo, code)
        self._ready.set()

        msg = ctypes.create_string_buffer(64)
        while not self._stop.is_set():
            result = user32.GetMessageW(msg, None, 0, 0)
            if result == 0 or result == -1:
                break
            wm = ctypes.c_uint.from_buffer(msg, 4).value
            if wm == WM_HOTKEY:
                hotkey_id = ctypes.c_int.from_buffer(msg, 8).value
                self._dispatch(hotkey_id)
        for hotkey_id in registered:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:  # pragma: no cover
                pass

    def _dispatch(self, hotkey_id: int) -> None:
        with self._lock:
            callback = self._callbacks.get(hotkey_id)
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # pragma: no cover - defensive
            log.error("hotkey.callback_failed id=%d err=%s", hotkey_id, exc)

    @staticmethod
    def _describe_failure(code: int, combo: str) -> str:
        if code == 1409:      # ERROR_HOTKEY_ALREADY_REGISTERED
            return ("Skrót %s jest już zajęty przez inny program. "
                    "Zmień go w ustawieniach." % combo)
        if code == 87:        # ERROR_INVALID_PARAMETER
            return "Skrót %s jest nieprawidłowy." % combo
        if code == 5:         # ERROR_ACCESS_DENIED
            return "Brak uprawnień do rejestracji skrótu %s." % combo
        return "Nie udało się zarejestrować skrótu %s (kod Win32: %d)." % (
            combo, code)
