"""Session export/import (Markdown, plain text, JSON, optional HTML).

The JSON schema is versioned and self-describing. Exports never contain
credentials: only session/message data is written, and the writer asserts that
no secret-looking key survives.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.constants import APP_NAME, APP_VERSION
from core.logging_setup import get_logger
from models.chat_models import Session
from models.message_models import Message, Role
from utils.markdown import render, to_markdown_document

log = get_logger("export")

SCHEMA_NAME = "core2chat.session"
SCHEMA_VERSION = 1
FORMAT_MARKDOWN = "md"
FORMAT_TEXT = "txt"
FORMAT_JSON = "json"
FORMAT_HTML = "html"
SUPPORTED_FORMATS = (FORMAT_MARKDOWN, FORMAT_TEXT, FORMAT_JSON, FORMAT_HTML)

FORBIDDEN_EXPORT_KEYS = ("api_key", "apikey", "secret", "password",
                         "credential", "access_token", "refresh_token",
                         "authorization")


class ExportError(RuntimeError):
    pass


class ImportService(object):
    """Reads an exported JSON document back into the database."""

    def __init__(self, context: Any) -> None:
        self.context = context

    def read_document(self, path: str) -> Dict[str, Any]:
        if not os.path.isfile(path):
            raise ExportError("Plik nie istnieje: %s" % path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ExportError("Nie można odczytać pliku: %s" % exc)
        if not isinstance(document, dict):
            raise ExportError("Nieprawidłowa struktura eksportu.")
        if document.get("schema") != SCHEMA_NAME:
            raise ExportError("To nie jest eksport Core2Chat "
                              "(brak pola schema).")
        version = int(document.get("version") or 0)
        if version > SCHEMA_VERSION:
            raise ExportError("Eksport pochodzi z nowszej wersji aplikacji "
                              "(schema v%d, obsługiwana v%d)."
                              % (version, SCHEMA_VERSION))
        return document

    def import_document(self, document: Dict[str, Any],
                        title_suffix: str = " (import)") -> Session:
        session_data = document.get("session") or {}
        messages = document.get("messages") or []
        if not isinstance(messages, list):
            raise ExportError("Pole messages musi być listą.")
        session = self.context.sessions.create(
            title=("%s%s" % (session_data.get("title") or "Import",
                             title_suffix))[:200],
            provider_id=str(session_data.get("provider_id") or "gemini"),
            model_id=str(session_data.get("model_id") or ""))
        imported = 0
        for entry in messages:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or Role.USER)
            if role not in Role.ALL:
                role = Role.USER
            message = Message(
                session_id=session.id, role=role,
                text=str(entry.get("text") or ""),
                timestamp=int(entry.get("timestamp") or
                              int(time.time() * 1000)),
                model_name=str(entry.get("model_name") or ""),
                status=str(entry.get("status") or "completed"),
                thought_summary=str(entry.get("thought_summary") or ""))
            self.context.messages.insert(message)
            imported += 1
        log.info("export.imported session=%s messages=%d", session.id, imported)
        return session

    def import_file(self, path: str) -> Session:
        return self.import_document(self.read_document(path))


class ExportService(object):
    """Writes a session out in one of the supported formats."""

    def __init__(self, context: Any) -> None:
        self.context = context

    def build_document(self, session_id: int) -> Dict[str, Any]:
        session = self.context.sessions.get(int(session_id))
        if session is None:
            raise ExportError("Sesja %s nie istnieje." % session_id)
        messages = self.context.messages.page(int(session_id), limit=100000)
        document = {
            "schema": SCHEMA_NAME,
            "version": SCHEMA_VERSION,
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": {
                "title": session.title,
                "provider_id": session.provider_id,
                "model_id": session.model_id,
                "state_mode": session.state_mode,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "system_preset": session.system_preset,
            },
            "messages": [_message_payload(m) for m in messages],
        }
        _assert_no_secrets(document)
        return document

    def export(self, session_id: int, path: str,
               fmt: str = FORMAT_MARKDOWN) -> Tuple[str, int]:
        """Write the export; returns (path, message_count)."""
        fmt = (fmt or FORMAT_MARKDOWN).lower().lstrip(".")
        if fmt not in SUPPORTED_FORMATS:
            raise ExportError("Nieobsługiwany format: %s" % fmt)
        document = self.build_document(session_id)
        if fmt == FORMAT_JSON:
            content = json.dumps(document, ensure_ascii=False, indent=1)
        elif fmt == FORMAT_TEXT:
            content = _to_text(document)
        elif fmt == FORMAT_HTML:
            content = _to_html(document)
        else:
            content = _to_markdown(document)
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise ExportError("Nie można zapisać pliku: %s" % exc)
        log.info("export.written path=%s format=%s messages=%d", path, fmt,
                 len(document["messages"]))
        return path, len(document["messages"])

    def suggest_filename(self, session_id: int, fmt: str) -> str:
        session = self.context.sessions.get(int(session_id))
        title = (session.title if session else "rozmowa") or "rozmowa"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        from utils.paths import safe_filename
        return "%s-%s.%s" % (safe_filename(title, 60), stamp, fmt)


# ------------------------------------------------------------------- helpers
def _message_payload(message: Message) -> Dict[str, Any]:
    usage = message.usage
    payload: Dict[str, Any] = {
        "role": message.role,
        "text": message.text or "",
        "timestamp": message.timestamp,
        "status": message.status,
        "model_name": message.model_name,
    }
    if message.thought_summary:
        payload["thought_summary"] = message.thought_summary
    if usage and not usage.is_empty():
        payload["usage"] = usage.to_row()
    if message.attachments:
        payload["attachment_ids"] = list(message.attachments)
    return payload


def _assert_no_secrets(document: Dict[str, Any]) -> None:
    blob = json.dumps(document, ensure_ascii=False).lower()
    for key in FORBIDDEN_EXPORT_KEYS:
        if key in blob:
            raise ExportError("Eksport zawierałby pole '%s' - przerwano." % key)


def _to_markdown(document: Dict[str, Any]) -> str:
    entries = [(m.get("role", "user"),
                time.strftime("%Y-%m-%d %H:%M",
                              time.localtime((m.get("timestamp") or 0) / 1000.0)),
                m.get("text", "")) for m in document["messages"]]
    header = [
        "<!-- Core2Chat export · schema v%s · %s -->"
        % (document["version"], document["exported_at"]),
        "",
    ]
    return "\n".join(header) + to_markdown_document(
        document["session"].get("title", ""), entries)


def _to_text(document: Dict[str, Any]) -> str:
    lines: List[str] = [document["session"].get("title", ""),
                        "Eksport: %s" % document["exported_at"], "=" * 60, ""]
    for entry in document["messages"]:
        who = "Użytkownik" if entry.get("role") == Role.USER else "Asystent"
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime((entry.get("timestamp") or 0) / 1000.0))
        lines.append("[%s] %s" % (stamp, who))
        lines.append(entry.get("text", ""))
        lines.append("-" * 60)
    return "\n".join(lines)


def _to_html(document: Dict[str, Any]) -> str:
    """HTML built from the same Markdown renderer used in the chat view."""
    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="pl"><head><meta charset="utf-8">',
        "<title>%s</title>" % _esc(document["session"].get("title", "")),
        "<style>body{background:#1e1e1e;color:#d4d4d4;font-family:'Segoe UI',"
        "sans-serif;max-width:900px;margin:24px auto;padding:0 16px}"
        ".role{color:#8a8a8a;font-size:12px;margin-top:16px}"
        "pre{background:#1a1a1a;border:1px solid #3f3f46;padding:6px;"
        "overflow-x:auto}code{font-family:Consolas,monospace}"
        "a{color:#007acc}.user{border-left:2px solid #007acc;padding-left:8px}"
        "</style></head><body>",
        "<h1>%s</h1>" % _esc(document["session"].get("title", "")),
        "<p class='role'>Eksport Core2Chat · %s</p>"
        % _esc(document["exported_at"]),
    ]
    for entry in document["messages"]:
        role = entry.get("role", "user")
        who = "Użytkownik" if role == Role.USER else "Asystent"
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime((entry.get("timestamp") or 0) / 1000.0))
        body = render(entry.get("text", ""), finalize=True).html
        parts.append("<div class='%s'><div class='role'>%s · %s</div>%s</div>"
                     % (_esc(role), _esc(who), _esc(stamp), body))
    parts.append("</body></html>")
    return "\n".join(parts)


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def detect_format(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower().lstrip(".")
    if ext in SUPPORTED_FORMATS:
        return ext
    if ext == "markdown":
        return FORMAT_MARKDOWN
    return FORMAT_MARKDOWN
