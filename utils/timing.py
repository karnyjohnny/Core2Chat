"""Monotonic timing helpers for benchmarks and latency logging."""

import time
from typing import Optional


def now_ms() -> float:
    return time.monotonic() * 1000.0


class Stopwatch(object):
    """Minimal elapsed-time measurement (monotonic clock)."""

    __slots__ = ("_start", "_end")

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._end = None  # type: Optional[float]

    def stop(self) -> float:
        self._end = time.monotonic()
        return self.elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        end = self._end if self._end is not None else time.monotonic()
        return (end - self._start) * 1000.0

    def __enter__(self) -> "Stopwatch":
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp in ISO-8601 (seconds precision)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def epoch_ms() -> int:
    return int(time.time() * 1000)


def resident_memory_bytes() -> int:
    """Current resident set size in bytes (0 when unavailable).

    Windows: ``GetProcessMemoryInfo`` (WorkingSetSize) via ctypes.
    Linux:   ``/proc/self/statm`` (pages * page size).
    macOS:   ``ru_maxrss`` fallback (peak, not current).
    """
    import os
    import sys

    if sys.platform == "win32":
        try:
            import ctypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_uint32),
                            ("PageFaultCount", ctypes.c_uint32),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
            psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                          counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            pass
    statm = "/proc/self/statm"
    if os.path.exists(statm):
        try:
            with open(statm, "r", encoding="ascii") as handle:
                pages = int(handle.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes. This is a *peak* value and is
        # only used when no current-RSS source exists on this platform.
        return int(peak) * (1024 if sys.platform != "darwin" else 1)
    except Exception:
        return 0


class MemoryDelta(object):
    """Context manager measuring RSS growth across a block."""

    def __init__(self) -> None:
        self.before = 0
        self.after = 0

    def __enter__(self) -> "MemoryDelta":
        self.before = resident_memory_bytes()
        return self

    def __exit__(self, *exc) -> bool:
        self.after = resident_memory_bytes()
        return False

    @property
    def delta_bytes(self) -> int:
        return max(0, self.after - self.before)

    @property
    def delta_mb(self) -> float:
        return self.delta_bytes / (1024.0 * 1024.0)
