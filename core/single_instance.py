"""Single-instance guard with command forwarding.

Windows : named mutex + a message-only window receiving WM_COPYDATA.
Others  : ``flock`` on a lock file + a Unix domain socket (development /
          portable deployments).

Behaviour required by specification §43: a second launch detects the running
instance, asks it to come to the foreground and can forward a command such as
``open-quick-chat``. It must never spawn a second database writer.
"""

import ctypes
import os
import socket
import struct
import sys
import threading
from typing import Callable, Optional

from core.logging_setup import get_logger

log = get_logger("single_instance")

WM_COPYDATA = 0x004A
HWND_MESSAGE = -3
ERROR_ALREADY_EXISTS = 187


class SingleInstanceGuard(object):
    """Acquire once per process; release on shutdown."""

    def __init__(self, app_id: str, socket_dir: Optional[str] = None) -> None:
        self.app_id = app_id
        self.is_windows = sys.platform == "win32"
        self.socket_dir = socket_dir or os.path.join(
            os.path.expanduser("~"), ".core2chat")
        self._mutex = None
        self._hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[socket.socket] = None
        self._lock_handle = None
        self._stop = threading.Event()
        self.handler: Optional[Callable[[str], None]] = None
        self.is_primary = False

    # ------------------------------------------------------------------ paths
    @property
    def lock_path(self) -> str:
        """Resolved lazily so ``socket_dir`` can be set before first use."""
        return os.path.join(self.socket_dir, "%s.lock" % self.app_id)

    @property
    def socket_path(self) -> str:
        return os.path.join(self.socket_dir, "%s.sock" % self.app_id)

    # ---------------------------------------------------------------- public
    def acquire(self, handler: Optional[Callable[[str], None]] = None) -> bool:
        """Return True when this process is the primary instance."""
        self.handler = handler
        self.is_primary = self._acquire_windows() if self.is_windows \
            else self._acquire_posix()
        if self.is_primary and handler is not None:
            self._start_listener()
        log.info("single_instance.acquired primary=%s platform=%s",
                 self.is_primary, sys.platform)
        return self.is_primary

    def forward_command(self, command: str, timeout: float = 2.0) -> bool:
        """Ask the running instance to handle ``command`` (e.g. show/quick-chat)."""
        if not command:
            return False
        try:
            if self.is_windows:
                return self._forward_windows(command, timeout)
            return self._forward_posix(command, timeout)
        except OSError as exc:
            log.warning("single_instance.forward_failed err=%s", exc)
            return False

    def release(self) -> None:
        self._stop.set()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
            if not self.is_windows and os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
        if self._lock_handle is not None:
            try:
                import fcntl
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None
        self.is_primary = False
        if self._hwnd is not None and self.is_windows:
            try:
                ctypes.windll.user32.DestroyWindow(self._hwnd)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass
            self._hwnd = None
        if self._mutex is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._mutex)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass
            self._mutex = None
        self.is_primary = False

    # --------------------------------------------------------------- windows
    def _acquire_windows(self) -> bool:  # pragma: no cover - Windows only
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            user32 = ctypes.windll.user32      # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("single_instance.win32_unavailable err=%s", exc)
            return True
        name = "Local\\%s" % self.app_id
        self._mutex = kernel32.CreateMutexW(None, False, name)
        if not self._mutex:
            return True
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        self._create_message_window(user32)
        return True

    def _create_message_window(self, user32) -> None:  # pragma: no cover
        wnd_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint64,
            ctypes.c_int64)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_COPYDATA:
                try:
                    payload = ctypes.cast(
                        lparam,
                        ctypes.POINTER(_CopyDataStruct)).contents
                    text = ctypes.wstring_at(payload.lpData,
                                             payload.cbData // 2)
                    if self.handler is not None:
                        self.handler(text)
                except Exception as exc:
                    log.warning("single_instance.copydata_failed err=%s", exc)
                return 1
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc = wnd_proc_type(wnd_proc)   # keep a reference alive

        class _WndClassEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", wnd_proc_type), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        wc = _WndClassEx()
        wc.cbSize = ctypes.sizeof(_WndClassEx)
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self.app_id
        user32.RegisterClassExW(ctypes.byref(wc))
        self._hwnd = user32.CreateWindowExW(
            0, self.app_id, self.app_id, 0, 0, 0, 0, 0, HWND_MESSAGE, None,
            wc.hInstance, None)

    def _forward_windows(self, command: str, timeout: float) -> bool:
        # pragma: no cover - Windows only
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.FindWindowW(self.app_id, self.app_id)
        if not hwnd:
            return False
        buffer = ctypes.create_unicode_buffer(command)
        data = _CopyDataStruct()
        data.dwData = 1
        data.cbData = len(command) * 2
        data.lpData = ctypes.cast(buffer, ctypes.c_void_p)
        return bool(user32.SendMessageW(hwnd, WM_COPYDATA, 0,
                                        ctypes.byref(data)))

    # ----------------------------------------------------------------- posix
    def _acquire_posix(self) -> bool:
        """Take an exclusive advisory lock.

        Only a *contended* lock means "another instance is running". Any other
        OSError (read-only filesystem, permissions, missing directory) means we
        could not enforce the rule - in that case the application still starts,
        because refusing to launch would be worse than a possible duplicate.
        """
        import errno
        import fcntl

        handle = None
        try:
            os.makedirs(self.socket_dir, exist_ok=True)
            handle = open(self.lock_path, "a+")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            contended = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}
            if exc.errno in contended:
                log.info("single_instance.already_running err=%s", exc)
                return False
            log.warning("single_instance.lock_unavailable err=%s -> "
                        "uruchamiam mimo to", exc)
            return True
        try:
            handle.write("%d\n" % os.getpid())
            handle.flush()
        except OSError:
            pass
        self._lock_handle = handle
        return True

    def _start_listener(self) -> None:
        if self.is_windows:
            return                      # the message window already listens
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(self.socket_path)
            server.listen(4)
            server.settimeout(0.5)
            self._server = server
        except OSError as exc:
            log.warning("single_instance.listener_failed err=%s", exc)
            return
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="c2c-instance", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                connection.settimeout(1.0)
                raw = connection.recv(4096)
                command = raw.decode("utf-8", "replace").strip()
                if command and self.handler is not None:
                    self.handler(command)
                connection.sendall(b"ok")
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _forward_posix(self, command: str, timeout: float) -> bool:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(self.socket_path)
            client.sendall(command.encode("utf-8"))
            client.recv(16)
            return True
        except OSError as exc:
            log.info("single_instance.forward_unavailable err=%s", exc)
            return False
        finally:
            client.close()


class _CopyDataStruct(ctypes.Structure):  # pragma: no cover - Windows only
    _fields_ = [("dwData", ctypes.c_void_p), ("cbData", ctypes.c_uint),
                ("lpData", ctypes.c_void_p)]


def parse_forwarded(command: str) -> tuple:
    """``"open-quick-chat"`` -> ``("open-quick-chat", {})`` (extensible)."""
    parts = (command or "").strip().split(" ", 1)
    name = parts[0] if parts else ""
    payload = parts[1] if len(parts) > 1 else ""
    return name, {"payload": payload}


def encode_length_prefix(text: str) -> bytes:
    """Helper kept for tests: length-prefixed framing used by the socket."""
    data = text.encode("utf-8")
    return struct.pack(">I", len(data)) + data
