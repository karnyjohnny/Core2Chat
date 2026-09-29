"""Filesystem path resolution.

Supports three layouts:

* portable mode  - everything next to the executable (``portable.flag``)
* Windows        - ``%APPDATA%/Core2Chat``
* other platforms- ``$XDG_DATA_HOME/Core2Chat`` (development convenience)

Nothing is ever written inside the source tree unless portable mode is on.
"""

import os
import sys
from typing import Optional

from core.constants import APP_NAME, DB_FILE_NAME, LOG_FILE_NAME

PORTABLE_FLAG = "portable.flag"

# Explicit override used by tests, CI and power users. Highest precedence.
DATA_DIR_ENV = "CORE2CHAT_DATA_DIR"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """Directory holding the executable (frozen) or the project (source)."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable(root: Optional[str] = None) -> bool:
    root = root or app_root()
    return os.path.isfile(os.path.join(root, PORTABLE_FLAG))


def _windows_appdata() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def _xdg_data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME.lower())


def data_dir(root: Optional[str] = None) -> str:
    """Base directory for database, config and logs.

    Precedence: ``CORE2CHAT_DATA_DIR`` > portable mode > platform default.
    """
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return override
    root = root or app_root()
    if is_portable(root):
        return os.path.join(root, "data")
    if sys.platform == "win32":
        return _windows_appdata()
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", APP_NAME)
    return _xdg_data_dir()


def ensure_dir(path: str) -> str:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def database_path(root: Optional[str] = None) -> str:
    return os.path.join(ensure_dir(data_dir(root)), DB_FILE_NAME)


def config_path(root: Optional[str] = None) -> str:
    return os.path.join(ensure_dir(data_dir(root)), "config.json")


def log_dir(root: Optional[str] = None) -> str:
    return ensure_dir(os.path.join(data_dir(root), "logs"))


def log_path(root: Optional[str] = None) -> str:
    return os.path.join(log_dir(root), LOG_FILE_NAME)


def cache_dir(root: Optional[str] = None) -> str:
    return ensure_dir(os.path.join(data_dir(root), "cache"))


def asset_path(*parts: str) -> str:
    """Resolve an asset both from source and from a PyInstaller bundle."""
    root = app_root()
    candidates = [os.path.join(root, *parts)]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(os.path.join(bundle, *parts))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


def safe_filename(name: str, max_len: int = 120) -> str:
    """Sanitise a file name for display/storage (never trust model output)."""
    name = (name or "").replace("\x00", "").strip()
    name = os.path.basename(name.replace("\\", "/"))
    cleaned = []
    for ch in name:
        if ch in '<>:"/\\|?*' or ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    name = "".join(cleaned).strip(". ")
    if not name:
        name = "attachment"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            keep = max_len - len(ext) - 1
            name = stem[:keep] + "." + ext
        else:
            name = name[:max_len]
    return name


def human_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return "%d %s" % (int(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size
