"""Qt worker objects (QRunnable) - all blocking work happens here.

Architecture (specification §40): the GUI thread calls a service, the service
submits a worker to a QThreadPool, the worker talks to httpx/SQLite and emits
Qt signals which are delivered back on the GUI thread. Widgets are never
touched from a worker.
"""

import threading
import traceback
from typing import Any, Callable, Dict, Optional

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal

from core.logging_setup import get_logger
from models.provider_models import ProviderError, StreamingEvent, StreamEventType

log = get_logger("workers")


class WorkerSignals(QObject):
    """Signal surface shared by every worker type."""

    started = pyqtSignal(str)                 # request_id
    event = pyqtSignal(str, object)           # request_id, StreamingEvent
    progress = pyqtSignal(str, float)         # request_id, 0..1
    result = pyqtSignal(str, object)          # request_id, payload
    failed = pyqtSignal(str, object)          # request_id, ProviderError
    cancelled = pyqtSignal(str)               # request_id
    finished = pyqtSignal(str)                # request_id (always last)


class BaseWorker(QRunnable):
    """Common bookkeeping: identity, cancellation, guaranteed finish signal."""

    def __init__(self, request_id: str) -> None:
        super(BaseWorker, self).__init__()
        self.request_id = request_id
        self.signals = WorkerSignals()
        self._cancel = threading.Event()
        self.setAutoDelete(True)

    # ------------------------------------------------------------- control
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    # -------------------------------------------------------------- helpers
    def _emit_failure(self, exc: BaseException) -> None:
        error = exc if isinstance(exc, ProviderError) else ProviderError(
            category="UNKNOWN", code=type(exc).__name__,
            message_user="Wystąpił nieoczekiwany błąd aplikacji.",
            message_debug="%s: %s" % (type(exc).__name__, exc),
            provider="core2chat")
        if not isinstance(exc, ProviderError):
            log.error("worker.unexpected request=%s err=%s\n%s",
                      self.request_id, exc, traceback.format_exc(limit=4))
        self.signals.failed.emit(self.request_id, error)


class StreamWorker(BaseWorker):
    """Runs ``provider.stream_message`` and forwards typed events."""

    def __init__(self, request_id: str, provider: Any,
                 params: Dict[str, Any]) -> None:
        super(StreamWorker, self).__init__(request_id)
        self.provider = provider
        self.params = dict(params)

    def run(self) -> None:  # executed in the pool thread
        self.signals.started.emit(self.request_id)
        completed_normally = False
        try:
            self.params["cancel_event"] = self._cancel
            stream = self.provider.stream_message(**self.params)
            for event in stream:
                if not isinstance(event, StreamingEvent):
                    continue
                if event.event_type == StreamEventType.CANCELLED:
                    self.signals.cancelled.emit(self.request_id)
                    completed_normally = True
                    break
                self.signals.event.emit(self.request_id, event)
                if event.event_type == StreamEventType.ERROR:
                    error_payload = (event.metadata or {}).get("error")
                    if isinstance(error_payload, dict):
                        completed_normally = True
                        self.signals.failed.emit(
                            self.request_id,
                            ProviderError(**_error_kwargs(error_payload)))
                        break
                if self._cancel.is_set():
                    self.signals.cancelled.emit(self.request_id)
                    completed_normally = True
                    break
            else:
                completed_normally = True
            if not completed_normally and self._cancel.is_set():
                self.signals.cancelled.emit(self.request_id)
        except ProviderError as exc:
            self._emit_failure(exc)
        except Exception as exc:  # pragma: no cover - defensive
            self._emit_failure(exc)
        finally:
            self.signals.finished.emit(self.request_id)


class TaskWorker(BaseWorker):
    """Runs an arbitrary blocking callable (model refresh, upload, export)."""

    def __init__(self, request_id: str, func: Callable[..., Any],
                 args: tuple = (), kwargs: Optional[Dict[str, Any]] = None,
                 progress_arg: str = "progress") -> None:
        super(TaskWorker, self).__init__(request_id)
        self.func = func
        self.args = args
        self.kwargs = dict(kwargs or {})
        self._progress_arg = progress_arg

    def run(self) -> None:
        self.signals.started.emit(self.request_id)
        try:
            kwargs = dict(self.kwargs)
            if self._progress_arg and self._progress_arg not in kwargs:
                kwargs[self._progress_arg] = self._report_progress
            result = self.func(*self.args, **kwargs)
            if self._cancel.is_set():
                self.signals.cancelled.emit(self.request_id)
            else:
                self.signals.result.emit(self.request_id, result)
        except TypeError:
            # Callable does not accept a progress callback - retry without it.
            try:
                result = self.func(*self.args, **self.kwargs)
                self.signals.result.emit(self.request_id, result)
            except ProviderError as exc:
                self._emit_failure(exc)
            except Exception as exc:  # pragma: no cover - defensive
                self._emit_failure(exc)
        except ProviderError as exc:
            self._emit_failure(exc)
        except Exception as exc:  # pragma: no cover - defensive
            self._emit_failure(exc)
        finally:
            self.signals.finished.emit(self.request_id)

    def _report_progress(self, value: float) -> None:
        if self._cancel.is_set():
            raise ProviderError(category="CANCELLED", code="cancelled",
                                message_user="Operacja anulowana.",
                                message_debug="task cancelled",
                                provider="core2chat")
        try:
            self.signals.progress.emit(self.request_id, float(value))
        except (TypeError, ValueError):
            pass


def _error_kwargs(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"category", "code", "http_status", "message_user",
               "message_debug", "retryable", "retry_after_seconds", "provider",
               "timestamp", "raw_reference"}
    return {k: v for k, v in payload.items() if k in allowed}
