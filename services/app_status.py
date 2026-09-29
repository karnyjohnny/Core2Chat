"""Application status state machine (task §4.3).

Problem being fixed: the old UI mixed connectivity with activity in one label
and never left the error state, so the window could read "Błąd" for the rest of
the session even though the application was perfectly usable. That reads as
"permanently broken".

Design:

* one explicit state, always derived - never glued together ad hoc;
* **transient** states (``SUCCESS``, ``ERROR``) return to ``IDLE`` on their own
  after a short, fixed delay, and the detailed reason stays visible in the
  status bar text instead of in the state itself;
* any successful provider response moves the machine out of ``ERROR``;
* connectivity (``OFFLINE``, ``PAUSED``, ``AUTH_REQUIRED``) is *sticky* because
  it describes the environment, not one operation;
* no sleeps, no polling loops: transitions are event-driven, and the timed
  return to ``IDLE`` uses a caller-supplied scheduler so the module stays
  Qt-free and testable.

Legal transitions are declared in :data:`TRANSITIONS`; an illegal transition is
logged and ignored instead of corrupting the state.
"""

import time
from typing import Callable, Dict, List, Optional, Set, Tuple

IDLE = "idle"
CONNECTING = "connecting"
SENDING = "sending"
STREAMING = "streaming"
SUCCESS = "success"
ERROR = "error"
OFFLINE = "offline"
PAUSED = "paused"
AUTH_REQUIRED = "auth_required"
RATE_LIMITED = "rate_limited"
CANCELLED = "cancelled"

ALL_STATES: Tuple[str, ...] = (IDLE, CONNECTING, SENDING, STREAMING, SUCCESS,
                               ERROR, OFFLINE, PAUSED, AUTH_REQUIRED,
                               RATE_LIMITED, CANCELLED)

#: States that describe an operation in flight.
BUSY_STATES: Set[str] = {CONNECTING, SENDING, STREAMING}
#: States that must not persist: they fall back to IDLE after a delay.
TRANSIENT_STATES: Set[str] = {SUCCESS, ERROR, RATE_LIMITED}
#: States caused by the environment; they stay until the environment changes.
STICKY_STATES: Set[str] = {OFFLINE, PAUSED, AUTH_REQUIRED}

TRANSITIONS: Dict[str, Set[str]] = {
    IDLE: {CONNECTING, SENDING, STREAMING, OFFLINE, PAUSED, AUTH_REQUIRED,
           SUCCESS, ERROR, RATE_LIMITED, CANCELLED},
    CONNECTING: {SENDING, STREAMING, IDLE, ERROR, OFFLINE, AUTH_REQUIRED,
                 SUCCESS, RATE_LIMITED},
    SENDING: {STREAMING, SUCCESS, ERROR, IDLE, OFFLINE, RATE_LIMITED,
              CANCELLED},
    STREAMING: {SUCCESS, ERROR, IDLE, OFFLINE, RATE_LIMITED, CANCELLED},
    SUCCESS: {IDLE, CONNECTING, SENDING, STREAMING, ERROR, OFFLINE},
    ERROR: {IDLE, CONNECTING, SENDING, STREAMING, SUCCESS, OFFLINE,
            AUTH_REQUIRED, RATE_LIMITED},
    OFFLINE: {IDLE, CONNECTING, AUTH_REQUIRED, PAUSED},
    PAUSED: {IDLE, OFFLINE, CONNECTING},
    AUTH_REQUIRED: {IDLE, CONNECTING, OFFLINE},
    RATE_LIMITED: {IDLE, CONNECTING, SENDING, STREAMING, OFFLINE, ERROR},
    CANCELLED: {IDLE, SENDING, STREAMING, ERROR},
}

# A transient state may re-enter itself: a second failure right after the first
# must update the detail and re-arm the return-to-idle timer. Without this the
# transition table rejects ERROR -> ERROR and the UI keeps the stale message.
for _transient in (SUCCESS, ERROR, RATE_LIMITED, CANCELLED):
    TRANSITIONS[_transient].add(_transient)

# Aliases kept for readability at call sites.
TRANSIENT_STATES.add(CANCELLED)

DEFAULT_TRANSIENT_TIMEOUT_MS = 6000

#: Polish labels. The state alone must be understandable without colour.
LABELS: Dict[str, str] = {
    IDLE: "Gotowy",
    CONNECTING: "Łączenie…",
    SENDING: "Wysyłanie…",
    STREAMING: "Generowanie…",
    SUCCESS: "Zakończono",
    ERROR: "Błąd",
    OFFLINE: "Offline",
    PAUSED: "Sieć wstrzymana",
    AUTH_REQUIRED: "Popraw klucz API",
    RATE_LIMITED: "Limit API",
    CANCELLED: "Przerwano",
}


class AppStatus(object):
    """Explicit status with history, timing and safe transitions."""

    def __init__(self, scheduler: Optional[Callable[[int, Callable], None]] = None,
                 transient_timeout_ms: int = DEFAULT_TRANSIENT_TIMEOUT_MS) -> None:
        self._state = IDLE
        self._detail = ""
        self._since = time.monotonic()
        self._history: List[Tuple[float, str, str]] = []
        self._scheduler = scheduler
        self._transient_timeout_ms = int(transient_timeout_ms)
        self._pending_reset: Optional[Callable[[], None]] = None
        self._listeners: List[Callable[[str, str], None]] = []
        self.error_count = 0
        self.success_count = 0

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> str:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def is_busy(self) -> bool:
        return self._state in BUSY_STATES

    @property
    def is_sticky(self) -> bool:
        return self._state in STICKY_STATES

    @property
    def seconds_in_state(self) -> float:
        return time.monotonic() - self._since

    def set(self, state: str, detail: str = "",
            force: bool = False) -> bool:
        """Move to ``state``; returns False when the transition is illegal."""
        if state not in ALL_STATES:
            raise ValueError("nieznany stan: %r" % state)
        if state == self._state and not detail:
            # Same state, nothing new: keep the timer, avoid history spam.
            # Note: an *explicit* detail (even an error without one) re-arms the
            # transient timer below, so repeated failures still recover.
            return True
        allowed = TRANSITIONS.get(self._state, set())
        if not force and state not in allowed:
            self._record("illegal:%s->%s" % (self._state, state), detail)
            return False
        previous = self._state
        self._state = state
        self._detail = detail or ""
        self._since = time.monotonic()
        self._record(state, detail, previous)
        # A repeated failure is still a failure: count every entry, not only
        # the first one, so the diagnostics reflect reality.
        if state == ERROR:
            self.error_count += 1
        elif state == SUCCESS:
            self.success_count += 1
        self._notify()
        if state in TRANSIENT_STATES:
            self._schedule_return_to_idle()
        else:
            self._cancel_pending_reset()
        return True

    def reset_to_idle(self) -> None:
        """Explicitly return to IDLE (used by the transient timer).

        The transient detail is dropped with the state: leaving "Błąd — limit
        API" behind while showing "Gotowy" is exactly the ambiguity this module
        exists to remove. The reason stays available in :meth:`history`.
        """
        if self._state in TRANSIENT_STATES:
            self._state = IDLE
            self._detail = ""
            self._since = time.monotonic()
            self._record(IDLE, "", None)
            self._notify()
        self._pending_reset = None

    def clear_sticky(self) -> None:
        """Environment recovered: leave OFFLINE/PAUSED/AUTH_REQUIRED."""
        if self._state in STICKY_STATES:
            self.set(IDLE, "")

    # -------------------------------------------------------------- rendering
    def label(self) -> str:
        return LABELS.get(self._state, self._state)

    def status_text(self) -> str:
        """One-line text for the UI: state first, detail second."""
        base = self.label()
        if self._detail:
            return "%s — %s" % (base, self._detail)
        return base

    def tooltip(self) -> str:
        hints = {
            IDLE: "Aplikacja gotowa do pracy.",
            CONNECTING: "Trwa połączenie z API.",
            SENDING: "Żądanie zostało wysłane, czekam na odpowiedź.",
            STREAMING: "Odpowiedź jest odbierana fragmentami. Esc przerywa.",
            SUCCESS: "Ostatnia operacja zakończona pomyślnie.",
            ERROR: "Ostatnia operacja nie powiodła się. Możesz pracować dalej -"
                   " stan wróci do „Gotowy” automatycznie.",
            OFFLINE: "Brak połączenia z API. Historia lokalna jest dostępna.",
            PAUSED: "Aktywność sieciowa wstrzymana z zasobnika systemowego.",
            AUTH_REQUIRED: "Klucz API został odrzucony. Popraw go w "
                           "Ustawienia → API.",
            RATE_LIMITED: "Limit API osiągnięty. Spróbuj ponownie za chwilę.",
            CANCELLED: "Generowanie przerwane przez użytkownika.",
        }
        text = hints.get(self._state, "")
        if self._detail:
            text = "%s\nSzczegóły: %s" % (text, self._detail)
        return text

    # --------------------------------------------------------------- history
    def history(self, limit: int = 20) -> List[Tuple[float, str, str]]:
        return list(self._history[-limit:])

    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def set_scheduler(self, scheduler: Optional[Callable[[int, Callable], None]]
                      ) -> None:
        """Install the timer backend (Qt uses QTimer.singleShot)."""
        self._scheduler = scheduler

    def set_transient_timeout(self, milliseconds: int) -> None:
        self._transient_timeout_ms = max(500, int(milliseconds))

    # --------------------------------------------------------------- internal
    def _record(self, state: str, detail: str,
                previous: Optional[str] = None) -> None:
        entry = (time.time(), state if previous is None else state, detail)
        self._history.append(entry)
        if len(self._history) > 200:
            del self._history[:-200]

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self._state, self._detail)
            except Exception:
                # A broken listener must never break the state machine.
                continue

    def _schedule_return_to_idle(self) -> None:
        self._cancel_pending_reset()
        if self._scheduler is None:
            return

        token = [0]

        def reset() -> None:
            if self._pending_reset is None:
                return
            if self._pending_reset is not reset:
                return
            self.reset_to_idle()

        self._pending_reset = reset
        self._scheduler(self._transient_timeout_ms, reset)

    def _cancel_pending_reset(self) -> None:
        # The scheduled callback checks identity, so dropping the reference is
        # enough - no Qt timer object has to be tracked here.
        self._pending_reset = None


def status_from_provider_error(category: str, message_user: str = "") -> Tuple[str, str]:
    """Map a :class:`ProviderError` category onto (state, detail)."""
    mapping = {
        "AUTHENTICATION": (AUTH_REQUIRED, message_user),
        "AUTHORIZATION": (AUTH_REQUIRED, message_user),
        "RATE_LIMIT": (RATE_LIMITED, message_user),
        "NETWORK": (OFFLINE, message_user),
        "TIMEOUT": (OFFLINE, message_user),
        "CANCELLED": (CANCELLED, message_user),
        "MODEL_UNAVAILABLE": (ERROR, message_user),
        "CONTEXT_TOO_LARGE": (ERROR, message_user),
        "INVALID_REQUEST": (ERROR, message_user),
        "SERVER_ERROR": (ERROR, message_user),
        "FILE_ERROR": (ERROR, message_user),
        "CACHE_ERROR": (ERROR, message_user),
        "UNKNOWN": (ERROR, message_user),
    }
    return mapping.get(category, (ERROR, message_user))


def status_from_connectivity(state: str) -> str:
    """Map ``GeminiProvider.connectivity_check()`` onto an app status."""
    return {
        "online": IDLE,
        "offline": OFFLINE,
        "auth_failed": AUTH_REQUIRED,
        "rate_limited": RATE_LIMITED,
        "no_key": AUTH_REQUIRED,
        "error": ERROR,
    }.get(state, ERROR)
