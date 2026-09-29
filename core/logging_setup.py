"""Structured logging with mandatory secret redaction.

Two guarantees:

1. Nothing that looks like a credential reaches a handler.
2. Registered secrets (the live API key) are replaced even if they appear
   inside an unexpected message body.
"""

import logging
import logging.handlers
import re
import threading
from typing import Iterable, List, Optional, Set

_REDACTED = "<REDACTED>"

_HEADER_PATTERNS = (
    re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?([^\s,;\"']+)"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)(\"?(?:access_?token|refresh_?token)\"?\s*[:=]\s*\"?)"
               r"([A-Za-z0-9._\-]{16,})"),
)
_KNOWN_KEY_SHAPE = re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")

_lock = threading.RLock()
_registered_secrets: Set[str] = set()


def register_secret(value: Optional[str]) -> None:
    """Register a secret string so the filter can scrub it from all records."""
    if not value or len(value) < 8:
        return
    with _lock:
        _registered_secrets.add(value)


def unregister_secret(value: Optional[str]) -> None:
    if not value:
        return
    with _lock:
        _registered_secrets.discard(value)


def registered_secrets() -> List[str]:
    with _lock:
        return sorted(_registered_secrets, key=len, reverse=True)


def redact_text(text: str) -> str:
    """Remove credentials from an arbitrary string."""
    if not text:
        return text
    out = text
    with _lock:
        secrets = sorted(_registered_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, _REDACTED)
    out = _KNOWN_KEY_SHAPE.sub(_REDACTED, out)
    for pattern in _HEADER_PATTERNS:
        groups = pattern.groups
        if groups == 2:
            out = pattern.sub(lambda m: m.group(1) + _REDACTED, out)
        else:
            out = pattern.sub(
                lambda m: m.group(1) + (m.group(2) or "") + _REDACTED, out)
    return out


class RedactingFilter(logging.Filter):
    """Applied to every handler; scrubs message text and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact_text(v) if isinstance(v, str)
                                   else v for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact_text(a) if isinstance(a, str) else a
                        for a in record.args)
                elif isinstance(record.args, str):
                    record.args = redact_text(record.args)
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text = redact_text(str(exc))
                if text != str(exc):
                    record.msg = "%s | %s" % (record.msg, text)
                    record.exc_info = None
        except Exception:  # pragma: no cover - logging must never raise
            pass
        return True


class JsonFormatter(logging.Formatter):
    """Compact single-line structured format (cheap to parse, cheap to write)."""

    def format(self, record: logging.LogRecord) -> str:
        base = super(JsonFormatter, self).format(record)
        return "%s | %s | %s | %s" % (
            self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            record.levelname,
            record.name,
            base.replace("\n", " \\n "),
        )


def _level_from_name(name: str) -> int:
    return getattr(logging, str(name or "INFO").upper(), logging.INFO)


def setup_logging(level: str = "INFO", log_file: Optional[str] = None,
                  console: bool = False,
                  max_bytes: int = 1024 * 1024,
                  backup_count: int = 2) -> logging.Logger:
    """Configure the root ``core2chat`` logger. Idempotent."""
    root = logging.getLogger("core2chat")
    root.setLevel(_level_from_name(level))
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = JsonFormatter("%(message)s")
    scrubber = RedactingFilter()

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(scrubber)
        root.addHandler(stream)

    if log_file:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.addFilter(scrubber)
            root.addHandler(file_handler)
        except OSError:
            # Unwritable log directory must never prevent the app from booting.
            if not root.handlers:
                root.addHandler(logging.NullHandler())
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    return root


def get_logger(name: str) -> logging.Logger:
    if name.startswith("core2chat"):
        return logging.getLogger(name)
    return logging.getLogger("core2chat.%s" % name)


def sanitize_headers(headers: Optional[Iterable]) -> str:
    """Header summary safe for diagnostics."""
    if not headers:
        return ""
    if hasattr(headers, "items"):
        items = list(headers.items())
    else:
        items = [(str(h), "") for h in headers]
    parts = []
    for key, value in items:
        if str(key).lower() in ("authorization", "x-goog-api-key", "api-key"):
            parts.append("%s: %s" % (key, _REDACTED))
        else:
            parts.append("%s: %s" % (key, value))
    return "; ".join(parts)
