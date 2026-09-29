"""Reusable HTTP layer (httpx) with timeouts, retries and redaction.

One client instance is shared by all provider calls and lives as long as the
provider; connections are pooled and closed explicitly at shutdown.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Dict, Optional

from api._lazy_httpx import httpx_module

from api import errors as error_mapper
from core.constants import (DEFAULT_CONNECT_TIMEOUT, DEFAULT_MAX_RETRIES,
                            DEFAULT_POOL_TIMEOUT, DEFAULT_READ_TIMEOUT,
                            DEFAULT_STREAM_READ_TIMEOUT, DEFAULT_WRITE_TIMEOUT,
                            RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
                            RETRYABLE_STATUS_CODES)
from core.logging_setup import get_logger, sanitize_headers
from models.provider_models import ProviderError

log = get_logger("http")


class HttpClient(object):
    """Thin, provider-agnostic wrapper around :class:`httpx.Client`."""

    def __init__(self, base_url: str, api_key: str = "",  # noqa: C901
                 auth_header: str = "x-goog-api-key",
                 extra_headers: Optional[Dict[str, str]] = None,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT,
                 write_timeout: float = DEFAULT_WRITE_TIMEOUT,
                 pool_timeout: float = DEFAULT_POOL_TIMEOUT,
                 stream_read_timeout: float = DEFAULT_STREAM_READ_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 max_keepalive: int = 4,
                 max_connections: int = 8,
                 provider: str = "gemini",
                 proxy: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.auth_header = auth_header
        self.extra_headers = dict(extra_headers or {})
        self.max_retries = max(0, int(max_retries))
        self.provider = provider
        # Plain numbers only: no httpx object is built here, so importing this
        # module and constructing a provider costs nothing until a request is
        # actually made (measured: importing httpx costs ~11 MB RSS).
        self._timeouts = {"connect": float(connect_timeout),
                          "read": float(read_timeout),
                          "write": float(write_timeout),
                          "pool": float(pool_timeout)}
        self._stream_read_timeout = float(stream_read_timeout)
        self._max_keepalive = int(max_keepalive)
        self._max_connections = int(max_connections)
        self._timeout_cache: Optional[Any] = None
        self._stream_timeout_cache: Optional[Any] = None
        self._lock = threading.RLock()
        self._client = None
        self._proxy = proxy
        # Test seam: a MockTransport can be injected instead of real sockets.
        self._transport = transport
        self.stats: Dict[str, int] = {"requests": 0, "retries": 0,
                                      "errors": 0, "streams": 0}

    # ------------------------------------------------------- lazy httpx types
    @property
    def timeout(self) -> Any:
        if self._timeout_cache is None:
            httpx = httpx_module()
            self._timeout_cache = httpx.Timeout(**self._timeouts)
        return self._timeout_cache

    @property
    def stream_timeout(self) -> Any:
        if self._stream_timeout_cache is None:
            httpx = httpx_module()
            # NOTE: passing the idle timeout positionally would be silently
            # overridden by the explicit read= keyword below.
            timeouts = dict(self._timeouts)
            timeouts["read"] = max(float(self._stream_read_timeout),
                                   float(timeouts.get("read") or 0.0))
            self._stream_timeout_cache = httpx.Timeout(**timeouts)
        return self._stream_timeout_cache

    # ------------------------------------------------------------- lifecycle
    def set_api_key(self, api_key: str) -> None:
        with self._lock:
            self.api_key = api_key or ""

    def set_base_url(self, base_url: str) -> None:
        with self._lock:
            self.base_url = (base_url or "").rstrip("/")

    def set_timeouts(self, connect: Optional[float] = None,
                     read: Optional[float] = None) -> None:
        with self._lock:
            if connect is not None:
                self._timeouts["connect"] = float(connect)
            if read is not None:
                self._timeouts["read"] = float(read)
                # A stream idle timeout shorter than the unary read timeout
                # would be meaningless; keep the invariant explicit.
                self._stream_read_timeout = max(self._stream_read_timeout,
                                                float(read))
            self._timeout_cache = None
            self._stream_timeout_cache = None

    def set_stream_read_timeout(self, seconds: float) -> None:
        """Idle timeout between SSE bytes (see DEFAULT_STREAM_READ_TIMEOUT)."""
        with self._lock:
            self._stream_read_timeout = float(seconds)
            self._stream_timeout_cache = None

    @property
    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None or self._client.is_closed:
                httpx = httpx_module()
                kwargs: Dict[str, Any] = {
                    "timeout": self.timeout,
                    "limits": httpx.Limits(
                        max_keepalive_connections=self._max_keepalive,
                        max_connections=self._max_connections,
                        keepalive_expiry=30.0),
                    "follow_redirects": False,
                    "http2": False,   # keeps h2 dependency out; measured faster cold
                }
                if self._proxy:
                    kwargs["proxy"] = self._proxy
                if self._transport is not None:
                    kwargs["transport"] = self._transport
                self._client = httpx.Client(**kwargs)
            return self._client

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - defensive
                pass
            log.info("http.closed provider=%s", self.provider)

    # --------------------------------------------------------------- headers
    def headers(self, content_type: Optional[str] = "application/json",
                accept: Optional[str] = None,
                extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.api_key:
            out[self.auth_header] = self.api_key
        if content_type:
            out["Content-Type"] = content_type
        if accept:
            out["Accept"] = accept
        out.update(self.extra_headers)
        if extra:
            out.update(extra)
        return out

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    # -------------------------------------------------------------- requests
    def request(self, method: str, path: str,
                params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Any] = None,
                content: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None,
                timeout: Optional[httpx.Timeout] = None,
                operation: str = "",
                retryable_methods: bool = True) -> httpx.Response:
        """Execute a request with bounded retries on retryable failures."""
        attempts = 0
        max_attempts = self.max_retries + 1
        last_error: Optional[ProviderError] = None
        while attempts < max_attempts:
            attempts += 1
            started = time.monotonic()
            # httpx rejects json= and content= at the same time.
            body_kwargs: Dict[str, Any] = {}
            if content is not None:
                body_kwargs["content"] = content
            elif json_body is not None:
                body_kwargs["json"] = json_body
            try:
                response = self.client.request(
                    method, self.url(path), params=params,
                    **body_kwargs,
                    headers=headers or self.headers(),
                    timeout=timeout or self.timeout)
                self.stats["requests"] += 1
                elapsed = (time.monotonic() - started) * 1000.0
                if response.status_code >= 400:
                    err = error_mapper.from_response(self.provider, response,
                                                     operation)
                    self.stats["errors"] += 1
                    log.warning("http.error op=%s status=%d cat=%s ms=%.0f",
                                operation, response.status_code, err.category,
                                elapsed)
                    # A 429 proves the request was rejected before processing,
                    # so retrying a POST is safe; 5xx does not give that
                    # guarantee, hence POST retries stay opt-in there.
                    safe_to_retry = response.status_code == 429 or retryable_methods
                    if (safe_to_retry and err.retryable
                            and attempts < max_attempts
                            and response.status_code in RETRYABLE_STATUS_CODES):
                        self._sleep_before_retry(err.retry_after_seconds,
                                                 attempts)
                        self.stats["retries"] += 1
                        last_error = err
                        continue
                    raise err
                log.info("http.ok op=%s status=%d ms=%.0f", operation,
                         response.status_code, elapsed)
                return response
            except ProviderError:
                raise
            except httpx_module().HTTPError as exc:
                err = error_mapper.from_exception(self.provider, exc, operation)
                self.stats["errors"] += 1
                last_error = err
                can_retry = (retryable_methods and err.retryable
                             and attempts < max_attempts
                             and method.upper() in ("GET", "HEAD", "OPTIONS",
                                                    "DELETE"))
                log.warning("http.exception op=%s type=%s retry=%s",
                            operation, type(exc).__name__, can_retry)
                if can_retry:
                    self._sleep_before_retry(None, attempts)
                    self.stats["retries"] += 1
                    continue
                raise err
        assert last_error is not None
        raise last_error

    def get(self, path: str, params: Optional[Dict[str, Any]] = None,
            operation: str = "", headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.request("GET", path, params=params, operation=operation,
                            headers=headers)

    def post(self, path: str, json_body: Optional[Any] = None,
             operation: str = "", headers: Optional[Dict[str, str]] = None,
             retryable: bool = False,
             content: Optional[bytes] = None) -> httpx.Response:
        return self.request("POST", path, json_body=json_body, content=content,
                            operation=operation, headers=headers,
                            retryable_methods=retryable)

    def delete(self, path: str, operation: str = "",
               headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.request("DELETE", path, operation=operation, headers=headers)

    # -------------------------------------------------------------- streaming
    class _StreamContext(object):
        def __init__(self, client: "HttpClient", method: str, path: str,
                     json_body: Any, headers: Dict[str, str],
                     operation: str) -> None:
            self._client = client
            self._method = method
            self._path = path
            self._body = json_body
            self._headers = headers
            self._operation = operation
            self._cm = None  # type: Any
            self.response: Optional[httpx.Response] = None

        def __enter__(self) -> httpx.Response:
            started = time.monotonic()
            try:
                self._cm = self._client.client.stream(
                    self._method, self._client.url(self._path),
                    json=self._body, headers=self._headers,
                    timeout=self._client.stream_timeout)
                self.response = self._cm.__enter__()
            except httpx_module().HTTPError as exc:
                raise error_mapper.from_exception(self._client.provider, exc,
                                                  self._operation)
            self._client.stats["streams"] += 1
            if self.response.status_code >= 400:
                try:
                    self.response.read()
                except httpx_module().HTTPError:
                    pass
                err = error_mapper.from_response(self._client.provider,
                                                 self.response, self._operation)
                self._client.stats["errors"] += 1
                self.__exit__(type(err), err, None)
                raise err
            log.info("http.stream_open op=%s status=%d ms=%.0f",
                     self._operation, self.response.status_code,
                     (time.monotonic() - started) * 1000.0)
            return self.response

        def __exit__(self, exc_type, exc, tb) -> bool:
            if self._cm is None:
                return False
            try:
                return bool(self._cm.__exit__(exc_type, exc, tb))
            except httpx_module().HTTPError:
                return False
            finally:
                self._cm = None
                self.response = None

    def stream_ctx(self, method: str, path: str,
                   json_body: Optional[Any] = None,
                   headers: Optional[Dict[str, str]] = None,
                   operation: str = "") -> "HttpClient._StreamContext":
        return HttpClient._StreamContext(self, method, path, json_body,
                                         headers or self.headers(
                                             accept="text/event-stream"),
                                         operation)

    # ---------------------------------------------------------------- helpers
    def _sleep_before_retry(self, retry_after: Optional[float],
                            attempt: int) -> None:
        if retry_after is not None:
            delay = min(max(0.0, retry_after), RETRY_BACKOFF_MAX * 4)
        else:
            delay = min(RETRY_BACKOFF_MAX,
                        RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)  # jitter avoids thundering herd
        log.info("http.retry_wait seconds=%.2f attempt=%d", delay, attempt)
        time.sleep(delay)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "provider": self.provider,
            "max_retries": self.max_retries,
            "timeout": dict(self._timeouts),
            "stream_read_timeout": self._stream_read_timeout,
            "stats": dict(self.stats),
            "headers": sanitize_headers(self.headers()),
        }


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
