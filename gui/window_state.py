"""Window state snapshot: serialisation that survives restarts.

Why this module exists: the previous implementation called
``QByteArrayLiteral``, which is a C++ macro and is **not** exported by PyQt5.
The import only ran inside ``apply_window_state()``, so the application crashed
on the *second* launch - exactly when a saved snapshot existed. That is the
worst possible place for a crash, because deleting the user's data "fixed" it.

The snapshot format is therefore explicit, versioned and validated:

    c2cw1|<flags>|<geometry-base64>|<splitter sizes>

* ``flags`` is a bitfield (maximised / full screen) kept separately, because
  restoring geometry alone does not restore the maximised state reliably;
* the geometry blob is ``QMainWindow.saveGeometry()`` encoded as base64 ASCII;
* anything unparsable, truncated or from an unknown version is rejected and the
  window simply opens with default geometry. User data is never deleted.
"""

import base64
import binascii
from typing import Any, List, Optional, Tuple

MAGIC = "c2cw"
VERSION = 1
SEPARATOR = "|"

FLAG_MAXIMIZED = 1
FLAG_FULLSCREEN = 2

MAX_SNAPSHOT_LENGTH = 65536
MAX_SPLITTER_VALUES = 8


class SnapshotError(ValueError):
    """Raised for malformed snapshots; callers fall back to defaults."""


def encode(geometry: Any, sizes: List[int], maximized: bool = False,
           fullscreen: bool = False) -> str:
    """Build a snapshot string from a QByteArray geometry and splitter sizes."""
    raw = bytes(geometry) if geometry is not None else b""
    if not raw:
        raise SnapshotError("pusta geometria okna")
    flags = 0
    if maximized:
        flags |= FLAG_MAXIMIZED
    if fullscreen:
        flags |= FLAG_FULLSCREEN
    if len(sizes or []) > MAX_SPLITTER_VALUES:
        raise SnapshotError("za dużo rozmiarów panelu (%d)" % len(sizes))
    cleaned_sizes = [max(0, int(value)) for value in (sizes or [])]
    return "%s%d%s%d%s%s%s%s" % (
        MAGIC, VERSION, SEPARATOR, flags, SEPARATOR,
        base64.b64encode(raw).decode("ascii"), SEPARATOR,
        ",".join(str(value) for value in cleaned_sizes))


def decode(snapshot: str) -> Tuple[bytes, List[int], int]:
    """Return ``(geometry_bytes, splitter_sizes, flags)``.

    Raises :class:`SnapshotError` for anything that cannot be trusted.
    """
    if not snapshot or not isinstance(snapshot, str):
        raise SnapshotError("brak snapshotu")
    if len(snapshot) > MAX_SNAPSHOT_LENGTH:
        raise SnapshotError("snapshot zbyt duży (%d)" % len(snapshot))
    parts = snapshot.split(SEPARATOR)
    if len(parts) != 4:
        raise SnapshotError("nieprawidłowa liczba pól (%d)" % len(parts))
    header, flags_text, geometry_text, sizes_text = parts
    if not header.startswith(MAGIC):
        raise SnapshotError("nieznany format: %r" % header[:8])
    try:
        version = int(header[len(MAGIC):])
    except ValueError:
        raise SnapshotError("nieprawidłowa wersja nagłówka")
    if version > VERSION:
        raise SnapshotError("snapshot z nowszej wersji aplikacji (v%d)" % version)
    try:
        flags = int(flags_text)
    except ValueError:
        raise SnapshotError("nieprawidłowe flagi")
    try:
        geometry = base64.b64decode(geometry_text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise SnapshotError("geometria nie jest poprawnym base64")
    if not geometry:
        raise SnapshotError("pusta geometria")
    sizes: List[int] = []
    for chunk in sizes_text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            sizes.append(max(0, int(chunk)))
        except ValueError:
            raise SnapshotError("nieprawidłowy rozmiar panelu: %r" % chunk)
    if len(sizes) > MAX_SPLITTER_VALUES:
        raise SnapshotError("za dużo rozmiarów panelu (%d)" % len(sizes))
    return geometry, sizes, flags


def is_valid(snapshot: Optional[str]) -> bool:
    """Cheap validation used before applying a snapshot."""
    try:
        decode(snapshot or "")
        return True
    except SnapshotError:
        return False


def migrate(snapshot: Optional[str]) -> Optional[str]:
    """Accept legacy snapshots (``<base64>|<sizes>``) or return None.

    Legacy values are converted instead of discarded, so an upgrade never
    throws away the user's window layout.
    """
    if not snapshot or not isinstance(snapshot, str):
        return None
    if snapshot.startswith(MAGIC):
        return snapshot if is_valid(snapshot) else None
    parts = snapshot.split(SEPARATOR)
    if len(parts) != 2:
        return None
    try:
        geometry = base64.b64decode(parts[0].encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if not geometry:
        return None
    sizes: List[int] = []
    for chunk in parts[1].split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            sizes.append(max(0, int(chunk)))
        except ValueError:
            return None
    try:
        return encode(_QByteArray(geometry), sizes)
    except SnapshotError:
        return None


def _QByteArray(data: bytes) -> Any:
    from PyQt5.QtCore import QByteArray

    return QByteArray(data)
