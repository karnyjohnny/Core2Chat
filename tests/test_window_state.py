"""Window-state persistence: the crash that only appeared on the 2nd launch."""

import base64

import pytest

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
QtCore = pytest.importorskip("PyQt5.QtCore")

from PyQt5.QtCore import QByteArray  # noqa: E402

from gui import window_state  # noqa: E402
from gui.window_state import (FLAG_FULLSCREEN, FLAG_MAXIMIZED, SnapshotError,  # noqa: E402
                             decode, encode, is_valid, migrate)


def test_encode_decode_roundtrip():
    geometry = QByteArray(b"\x01\xd9\xd0\xcb\x00\x03\x00\x00fakegeometry")
    snapshot = encode(geometry, [240, 900], maximized=True)
    raw, sizes, flags = decode(snapshot)
    assert bytes(raw) == bytes(geometry)
    assert sizes == [240, 900]
    assert flags & FLAG_MAXIMIZED
    assert not flags & FLAG_FULLSCREEN


def test_qbytearrayliteral_is_not_used_anywhere():
    """Regression: PyQt5 does not export QByteArrayLiteral (C++ macro only)."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "build", "dist",
                                                "__pycache__")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            # Only real *usage* counts: importing or calling the symbol.
            # Comments and docstrings may legitimately mention the name.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                code = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', "", line)
                if re.search(r"(import|from)\s+.*\bQByteArrayLiteral\b", code) \
                        or re.search(r"\bQByteArrayLiteral\s*\(", code):
                    offenders.append("%s: %s"
                                     % (os.path.relpath(path, root), stripped[:80]))
                    break
    assert offenders == []
    assert not hasattr(QtCore, "QByteArrayLiteral")


@pytest.mark.parametrize("bad", [
    "",
    None,
    "garbage",
    "c2cw1|0|!!!not-base64!!!|240,900",
    "c2cw1|0||240,900",
    "c2cw1|x|AAAA|1,2",
    "c2cw1|0|AAAA|x,y",
    "c2cw9|0|AAAA|1,2",                    # nowsza wersja
    "xxxx1|0|AAAA|1,2",                    # obcy format
    "c2cw1|0",                             # za mało pól
    "c2cw1|0|AAAA|1,2|extra",              # za dużo pól
    "c2cw1|0|" + base64.b64encode(b"x").decode() + "|1,2,3,4,5,6,7,8,9",
])

def test_invalid_snapshots_are_rejected_not_applied(bad):
    assert is_valid(bad) is False
    with pytest.raises(SnapshotError):
        decode(bad or "")


def test_many_splitter_values_are_accepted():
    """A splitter may legitimately have more than two panes."""
    snapshot = "c2cw1|0|" + base64.b64encode(b"geom").decode() + "|1,2,3,4,5"
    assert is_valid(snapshot) is True
    _raw, sizes, _flags = decode(snapshot)
    assert sizes == [1, 2, 3, 4, 5]


def test_oversized_snapshot_is_rejected():
    huge = "c2cw1|0|" + base64.b64encode(b"a" * 200000).decode() + "|1,2"
    assert is_valid(huge) is False


def test_legacy_snapshot_is_migrated_not_discarded():
    """Upgrade path: old '<base64>|<sizes>' snapshots keep working."""
    geometry = QByteArray(b"legacy-geometry-bytes")
    legacy = "%s|240,900" % base64.b64encode(bytes(geometry)).decode("ascii")
    migrated = migrate(legacy)
    assert migrated is not None
    raw, sizes, flags = decode(migrated)
    assert bytes(raw) == bytes(geometry)
    assert sizes == [240, 900]
    assert flags == 0


def test_migration_of_current_format_is_identity():
    snapshot = encode(QByteArray(b"geom"), [1, 2])
    assert migrate(snapshot) == snapshot


@pytest.mark.parametrize("broken", ["", "!!!", "a|b|c", None])
def test_migration_of_junk_returns_none(broken):
    assert migrate(broken) is None


# ------------------------------------------------------------ main window level
def _make_window(context, chat, sessions, exporter, importer):
    from gui.main_window import MainWindow

    return MainWindow(context, chat, sessions, exporter, importer)


@pytest.fixture
def stack(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("CORE2CHAT_DATA_DIR", str(tmp_path / "data"))
    from services.app_context import AppContext
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService

    context = AppContext()
    context.initialize()
    services = (ChatService(context), SessionService(context),
                ExportService(context), ImportService(context))
    yield context, services
    context.shutdown()


def test_main_window_snapshot_roundtrip(stack, qapp):
    context, services = stack
    window = _make_window(context, *services)
    window.resize(1024, 700)
    window.show()
    qapp.processEvents()
    snapshot = window.window_state_snapshot()
    assert snapshot.startswith("c2cw1|")
    assert is_valid(snapshot)
    window.resize(800, 600)
    assert window.apply_window_state(snapshot) is True
    qapp.processEvents()
    assert window.size().width() > 100
    window.close()
    window.deleteLater()


def test_maximized_state_survives_the_snapshot(stack, qapp):
    context, services = stack
    window = _make_window(context, *services)
    window.show()
    window.showMaximized()
    qapp.processEvents()
    snapshot = window.window_state_snapshot()
    _raw, _sizes, flags = decode(snapshot)
    assert flags & FLAG_MAXIMIZED
    window.showNormal()
    qapp.processEvents()
    assert window.apply_window_state(snapshot) is True
    qapp.processEvents()
    assert window.isMaximized() is True
    window.close()
    window.deleteLater()


@pytest.mark.parametrize("broken", [
    "", "garbage", "c2cw1|0|!!!|1,2",
    "c2cw9|0|AAAA|1,2",
    base64.b64encode(b"random-bytes").decode(),
    "x" * 70000,
])
def test_corrupt_snapshot_never_crashes(stack, qapp, broken):
    context, services = stack
    window = _make_window(context, *services)
    assert window.apply_window_state(broken) is False
    qapp.processEvents()
    window.show()                     # defaults must still work
    qapp.processEvents()
    assert window.isVisible() is True
    window.close()
    window.deleteLater()
