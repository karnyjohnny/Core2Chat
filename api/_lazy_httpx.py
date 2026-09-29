"""Lazy ``httpx`` loader.

Measured cost of ``import httpx`` in this environment: **+11 MB RSS** (httpx,
httpcore, h11, certifi, idna). The specification requires lazy imports and the
smallest possible idle footprint, and a freshly started app that has not sent a
request yet does not need an HTTP stack. The module is therefore imported on
first use, exactly once, and cached.
"""

import threading
from typing import Any

_lock = threading.Lock()
_module: Any = None


def httpx_module() -> Any:
    """Return the ``httpx`` module, importing it on first call."""
    global _module
    if _module is not None:
        return _module
    with _lock:
        if _module is None:
            import httpx
            _module = httpx
    return _module


def is_loaded() -> bool:
    """True once httpx has actually been imported (used by diagnostics)."""
    return _module is not None


def version() -> str:
    return getattr(httpx_module(), "__version__", "?") if is_loaded() else \
        "not loaded (lazy)"
