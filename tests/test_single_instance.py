"""Single-instance guard: lock semantics, command forwarding, failure modes."""

import os
import sys
import time

import pytest

from core.single_instance import SingleInstanceGuard, encode_length_prefix, parse_forwarded


@pytest.fixture
def socket_dir(tmp_path):
    return str(tmp_path / "run")


def test_first_instance_becomes_primary(socket_dir):
    guard = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert guard.acquire() is True
    assert guard.is_primary is True
    assert os.path.isfile(guard.lock_path)
    guard.release()
    assert guard.is_primary is False


def test_second_instance_is_detected(socket_dir):
    first = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert first.acquire() is True
    second = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert second.acquire() is False
    assert second.is_primary is False
    first.release()


def test_lock_is_released_so_a_restart_works(socket_dir):
    first = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert first.acquire() is True
    first.release()
    second = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert second.acquire() is True, "lock must not survive a clean shutdown"
    second.release()


def test_different_data_dirs_are_independent(tmp_path):
    a = SingleInstanceGuard("core2chat.test", socket_dir=str(tmp_path / "a"))
    b = SingleInstanceGuard("core2chat.test", socket_dir=str(tmp_path / "b"))
    assert a.acquire() is True
    assert b.acquire() is True      # portable mode must not fight %APPDATA%
    a.release()
    b.release()


def test_command_forwarding_reaches_the_primary(socket_dir):
    received = []
    first = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert first.acquire(handler=lambda command: received.append(command)) is True
    second = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert second.acquire() is False
    assert second.forward_command("show") is True
    assert second.forward_command("open-quick-chat") is True
    deadline = time.monotonic() + 2.0
    while len(received) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert received == ["show", "open-quick-chat"]
    first.release()


def test_forward_without_primary_reports_failure(socket_dir):
    guard = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    assert guard.forward_command("show") is False
    assert guard.forward_command("") is False


def test_unwritable_lock_dir_does_not_prevent_startup(tmp_path, monkeypatch):
    """A read-only location must not be reported as 'already running'."""
    blocked = tmp_path / "blocked"
    blocked.write_text("to jest plik, nie katalog", encoding="utf-8")
    guard = SingleInstanceGuard("core2chat.test",
                                socket_dir=str(blocked / "run"))
    if os.name == "nt":
        pytest.skip("POSIX-only failure mode")
    assert guard.acquire() is True, \
        "cannot enforce the rule -> start anyway instead of exiting silently"


def test_parse_forwarded_command():
    assert parse_forwarded("show") == ("show", {"payload": ""})
    assert parse_forwarded("open-quick-chat extra") == \
        ("open-quick-chat", {"payload": "extra"})
    assert parse_forwarded("") == ("", {"payload": ""})


def test_length_prefix_helper():
    framed = encode_length_prefix("ab")
    assert framed == b"\x00\x00\x00\x02ab"


def test_release_is_idempotent(socket_dir):
    guard = SingleInstanceGuard("core2chat.test", socket_dir=socket_dir)
    guard.acquire()
    guard.release()
    guard.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 mutex path")
def test_windows_primary_instance():  # pragma: no cover - Windows only
    guard = SingleInstanceGuard("core2chat.test")
    assert guard.acquire() is True
    second = SingleInstanceGuard("core2chat.test")
    assert second.acquire() is False
    guard.release()
