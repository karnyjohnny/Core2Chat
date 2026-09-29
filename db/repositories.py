"""Repositories: the only place where SQL is written.

Every GUI/service class goes through these objects, so schema changes stay
local to this module.
"""

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.logging_setup import get_logger
from db.database import Database, DatabaseError
from models.attachment_models import Attachment, AttachmentStatus
from models.chat_models import (PinnedContext, PromptPreset, SearchResult,
                                Session, StateMode)
from models.message_models import Message, MessageStatus, Role
from models.provider_models import ModelInfo
from models.usage_models import TokenStats, TokenUsage

log = get_logger("db.repo")

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS


def _now() -> int:
    return int(time.time() * 1000)


def _int(row: sqlite3.Row, key: str, default: int = 0) -> int:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(row: sqlite3.Row, key: str, default: str = "") -> str:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else str(value)


def _bool(row: sqlite3.Row, key: str) -> bool:
    return bool(_int(row, key))


# --------------------------------------------------------------------- sessions
class SessionRepository(object):
    """CRUD and listing for chat sessions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- mapping
    @staticmethod
    def from_row(row: sqlite3.Row) -> Session:
        metadata: Dict[str, Any] = {}
        raw_meta = _str(row, "metadata_json")
        if raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    metadata = parsed
            except ValueError:
                metadata = {}
        return Session(
            id=_int(row, "id"),
            title=_str(row, "title", "Nowa rozmowa"),
            provider_id=_str(row, "provider_id", "gemini"),
            model_id=_str(row, "model_used"),
            remote_interaction_id=_str(row, "remote_interaction_id"),
            state_mode=_str(row, "state_mode", StateMode.LOCAL),
            created_at=_int(row, "created_at"),
            updated_at=_int(row, "updated_at"),
            archived=_bool(row, "archived"),
            pinned=_bool(row, "pinned"),
            system_preset=_str(row, "system_preset"),
            parent_session_id=(row["parent_session_id"]
                               if row["parent_session_id"] is not None else None),
            fork_message_id=(row["fork_message_id"]
                             if row["fork_message_id"] is not None else None),
            draft=_str(row, "draft"),
            metadata=metadata,
        )

    # --------------------------------------------------------------- writes
    def create(self, title: str = "Nowa rozmowa", provider_id: str = "gemini",
               model_id: str = "", state_mode: str = StateMode.LOCAL,
               parent_session_id: Optional[int] = None,
               fork_message_id: Optional[int] = None,
               system_preset: str = "") -> Session:
        now = _now()
        session = Session(title=title or "Nowa rozmowa", provider_id=provider_id,
                          model_id=model_id, state_mode=state_mode,
                          created_at=now, updated_at=now,
                          parent_session_id=parent_session_id,
                          fork_message_id=fork_message_id,
                          system_preset=system_preset)
        session.id = self.db.execute(
            "INSERT INTO sessions(title, provider_id, model_used,"
            " remote_interaction_id, state_mode, system_preset, created_at,"
            " updated_at, archived, pinned, draft, parent_session_id,"
            " fork_message_id, metadata_json)"
            " VALUES(?,?,?,?,?,?,?,?,0,0,'',?,?,?)",
            (session.title, session.provider_id, session.model_id, "",
             session.state_mode, session.system_preset, session.created_at,
             session.updated_at, parent_session_id, fork_message_id, ""))
        return session

    def rename(self, session_id: int, title: str) -> bool:
        title = (title or "").strip() or "Nowa rozmowa"
        self.db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                        (title[:200], _now(), session_id))
        return True

    def touch(self, session_id: int, when: Optional[int] = None) -> None:
        self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                        (when or _now(), session_id))

    def delete(self, session_id: int) -> bool:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM attachments WHERE session_id=?",
                         (session_id,))
            conn.execute("DELETE FROM token_stats WHERE session_id=?",
                         (session_id,))
            conn.execute("DELETE FROM pinned_context WHERE session_id=?",
                         (session_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return True

    def set_flags(self, session_id: int, archived: Optional[bool] = None,
                  pinned: Optional[bool] = None) -> None:
        assignments: List[str] = []
        params: List[Any] = []
        if archived is not None:
            assignments.append("archived=?")
            params.append(1 if archived else 0)
        if pinned is not None:
            assignments.append("pinned=?")
            params.append(1 if pinned else 0)
        if not assignments:
            return
        params.append(session_id)
        self.db.execute("UPDATE sessions SET %s WHERE id=?"
                        % ", ".join(assignments), tuple(params))

    def set_model(self, session_id: int, model_id: str) -> None:
        self.db.execute("UPDATE sessions SET model_used=? WHERE id=?",
                        (model_id, session_id))

    def set_remote_state(self, session_id: int, interaction_id: str,
                         state_mode: Optional[str] = None) -> None:
        if state_mode:
            self.db.execute(
                "UPDATE sessions SET remote_interaction_id=?, state_mode=?"
                " WHERE id=?", (interaction_id, state_mode, session_id))
        else:
            self.db.execute(
                "UPDATE sessions SET remote_interaction_id=? WHERE id=?",
                (interaction_id, session_id))

    def set_draft(self, session_id: int, draft: str) -> None:
        self.db.execute("UPDATE sessions SET draft=? WHERE id=?",
                        (draft or "", session_id))

    def set_preset(self, session_id: int, preset_name: str) -> None:
        self.db.execute("UPDATE sessions SET system_preset=? WHERE id=?",
                        (preset_name or "", session_id))

    def clear_messages(self, session_id: int) -> int:
        removed = self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,))
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("UPDATE sessions SET remote_interaction_id='',"
                         " updated_at=? WHERE id=?", (_now(), session_id))
        return removed

    # ---------------------------------------------------------------- reads
    def get(self, session_id: int) -> Optional[Session]:
        row = self.db.query_one("SELECT * FROM sessions WHERE id=?",
                                (session_id,))
        return self.from_row(row) if row else None

    def list(self, include_archived: bool = False, limit: int = 80,
             offset: int = 0, pinned_only: bool = False,
             title_filter: str = "") -> List[Session]:
        where: List[str] = []
        params: List[Any] = []
        if not include_archived:
            where.append("archived=0")
        if pinned_only:
            where.append("pinned=1")
        if title_filter:
            where.append("title LIKE ? ESCAPE '\\'")
            params.append("%" + _escape_like(title_filter) + "%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([int(limit), int(offset)])
        rows = self.db.query(
            "SELECT * FROM sessions" + clause +
            " ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            tuple(params))
        return [self.from_row(row) for row in rows]

    def count(self, include_archived: bool = False) -> int:
        if include_archived:
            return self.db.scalar("SELECT COUNT(*) FROM sessions")
        return self.db.scalar("SELECT COUNT(*) FROM sessions WHERE archived=0")

    def most_recent(self) -> Optional[Session]:
        row = self.db.query_one(
            "SELECT * FROM sessions WHERE archived=0"
            " ORDER BY updated_at DESC LIMIT 1")
        return self.from_row(row) if row else None

    def all_ids(self) -> List[int]:
        return [_int(row, "id") for row in
                self.db.query("SELECT id FROM sessions ORDER BY id")]


def _escape_like(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("%", "\\%") \
        .replace("_", "\\_")


# --------------------------------------------------------------------- messages
class MessageRepository(object):
    """Message persistence with paged reads (no full-history loading)."""

    COLUMNS = ("id", "session_id", "role", "content_text", "content_json",
               "media_json", "attachments_json", "timestamp", "interaction_id",
               "status", "model_name", "thought_summary", "parent_message_id",
               "metadata_json", "input_tokens", "output_tokens",
               "thought_tokens", "cached_tokens", "tool_tokens", "total_tokens")

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def from_row(row: sqlite3.Row) -> Message:
        attachments: List[int] = []
        raw_attach = _str(row, "attachments_json")
        if raw_attach:
            try:
                parsed = json.loads(raw_attach)
                if isinstance(parsed, list):
                    attachments = [int(x) for x in parsed]
            except (ValueError, TypeError):
                attachments = []
        metadata: Dict[str, Any] = {}
        raw_meta = _str(row, "metadata_json")
        if raw_meta:
            try:
                parsed_meta = json.loads(raw_meta)
                if isinstance(parsed_meta, dict):
                    metadata = parsed_meta
            except ValueError:
                metadata = {}
        message = Message(
            id=_int(row, "id"),
            session_id=_int(row, "session_id"),
            role=_str(row, "role", Role.USER),
            text=_str(row, "content_text"),
            attachments=attachments,
            timestamp=_int(row, "timestamp"),
            model_name=_str(row, "model_name"),
            interaction_id=_str(row, "interaction_id"),
            status=_str(row, "status", MessageStatus.COMPLETED),
            thought_summary=_str(row, "thought_summary"),
            metadata=metadata,
        )
        message.content_parts = Message.parse_content_json(
            _str(row, "content_json"))
        message.parent_message_id = (
            row["parent_message_id"]
            if row["parent_message_id"] is not None else None)
        message.usage = TokenUsage(
            input_tokens=_int(row, "input_tokens"),
            output_tokens=_int(row, "output_tokens"),
            thought_tokens=_int(row, "thought_tokens"),
            cached_tokens=_int(row, "cached_tokens"),
            tool_tokens=_int(row, "tool_tokens"),
            total_tokens=_int(row, "total_tokens"),
        )
        return message

    @staticmethod
    def _row_values(message: Message) -> Tuple[Any, ...]:
        return (
            message.session_id, message.role, message.text or "",
            message.content_json(), "",
            json.dumps(message.attachments) if message.attachments else "",
            message.timestamp or _now(), message.interaction_id or "",
            message.status or MessageStatus.COMPLETED,
            message.model_name or "", message.thought_summary or "",
            message.parent_message_id, message.metadata_json(),
            message.usage.input_tokens, message.usage.output_tokens,
            message.usage.thought_tokens, message.usage.cached_tokens,
            message.usage.tool_tokens, message.usage.total_tokens,
        )

    _INSERT = (
        "INSERT INTO messages(session_id, role, content_text, content_json,"
        " media_json, attachments_json, timestamp, interaction_id, status,"
        " model_name, thought_summary, parent_message_id, metadata_json,"
        " input_tokens, output_tokens, thought_tokens, cached_tokens,"
        " tool_tokens, total_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def insert(self, message: Message) -> int:
        if message.timestamp == 0:
            message.timestamp = _now()
        message.id = self.db.execute(self._INSERT, self._row_values(message))
        return message.id

    def insert_many(self, messages: Sequence[Message]) -> List[int]:
        ids: List[int] = []
        if not messages:
            return ids
        with self.db.transaction() as conn:
            for message in messages:
                if message.timestamp == 0:
                    message.timestamp = _now()
                cursor = conn.execute(self._INSERT, self._row_values(message))
                message.id = int(cursor.lastrowid or 0)
                ids.append(message.id)
        return ids

    def get(self, message_id: int) -> Optional[Message]:
        row = self.db.query_one("SELECT * FROM messages WHERE id=?",
                                (message_id,))
        return self.from_row(row) if row else None

    def update_text(self, message_id: int, text: str, status: str) -> None:
        self.db.execute(
            "UPDATE messages SET content_text=?, status=? WHERE id=?",
            (text, status, message_id))

    def finalize(self, message_id: int, text: str, status: str,
                 usage: TokenUsage, interaction_id: str = "",
                 thought_summary: str = "", model_name: str = "",
                 content_json: str = "") -> None:
        """Single write that closes out a streamed message."""
        self.db.execute(
            "UPDATE messages SET content_text=?, status=?, interaction_id=?,"
            " thought_summary=?, model_name=?, content_json=?,"
            " input_tokens=?, output_tokens=?, thought_tokens=?,"
            " cached_tokens=?, tool_tokens=?, total_tokens=? WHERE id=?",
            (text, status, interaction_id, thought_summary, model_name,
             content_json, usage.input_tokens, usage.output_tokens,
             usage.thought_tokens, usage.cached_tokens, usage.tool_tokens,
             usage.total_tokens, message_id))

    def set_status(self, message_id: int, status: str) -> None:
        self.db.execute("UPDATE messages SET status=? WHERE id=?",
                        (status, message_id))

    def delete(self, message_id: int) -> bool:
        self.db.execute("DELETE FROM messages WHERE id=?", (message_id,))
        return True

    def delete_from(self, session_id: int, message_id: int) -> int:
        """Delete this message and everything after it (edit/regenerate)."""
        removed = self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND id>=?",
            (session_id, message_id))
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=? AND id>=?",
                         (session_id, message_id))
            conn.execute("UPDATE sessions SET remote_interaction_id='',"
                         " updated_at=? WHERE id=?", (_now(), session_id))
        return removed

    # ------------------------------------------------------------- pagination
    def page(self, session_id: int, limit: int = 60,
             before_id: Optional[int] = None) -> List[Message]:
        """Return up to ``limit`` newest messages, oldest first.

        ``before_id`` walks backwards through history without loading it all.
        """
        if before_id is None:
            rows = self.db.query(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (session_id, int(limit)))
        else:
            rows = self.db.query(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=?"
                " AND id<? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (session_id, int(before_id), int(limit)))
        return [self.from_row(row) for row in rows]

    def count(self, session_id: int) -> int:
        return self.db.scalar("SELECT COUNT(*) FROM messages WHERE session_id=?",
                              (session_id,))

    def oldest_id(self, session_id: int) -> Optional[int]:
        row = self.db.query_one(
            "SELECT MIN(id) AS m FROM messages WHERE session_id=?",
            (session_id,))
        value = row["m"] if row else None
        return int(value) if value is not None else None

    def last(self, session_id: int, role: Optional[str] = None) -> Optional[Message]:
        sql = "SELECT * FROM messages WHERE session_id=?"
        params: List[Any] = [session_id]
        if role:
            sql += " AND role=?"
            params.append(role)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.db.query_one(sql, tuple(params))
        return self.from_row(row) if row else None

    def by_interaction(self, interaction_id: str) -> List[Message]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE interaction_id=? ORDER BY id",
            (interaction_id,))
        return [self.from_row(row) for row in rows]

    def copy_range(self, source_session_id: int, target_session_id: int,
                   up_to_message_id: Optional[int] = None) -> int:
        """Copy a prefix of one session into another (fork/branch)."""
        sql = ("INSERT INTO messages(session_id, role, content_text,"
               " content_json, media_json, attachments_json, timestamp,"
               " interaction_id, status, model_name, thought_summary,"
               " parent_message_id, metadata_json, input_tokens,"
               " output_tokens, thought_tokens, cached_tokens, tool_tokens,"
               " total_tokens) SELECT ?, role, content_text, content_json,"
               " media_json, attachments_json, timestamp, interaction_id,"
               " status, model_name, thought_summary, parent_message_id,"
               " metadata_json, input_tokens, output_tokens, thought_tokens,"
               " cached_tokens, tool_tokens, total_tokens FROM messages"
               " WHERE session_id=?")
        params: List[Any] = [target_session_id, source_session_id]
        if up_to_message_id is not None:
            sql += " AND id<=?"
            params.append(int(up_to_message_id))
        sql += " ORDER BY id"
        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.rowcount or 0)

    def total_text_bytes(self, session_id: int) -> int:
        return self.db.scalar(
            "SELECT COALESCE(SUM(LENGTH(content_text)),0) FROM messages"
            " WHERE session_id=?", (session_id,))


# ------------------------------------------------------------------ attachments
class AttachmentRepository(object):
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def from_row(row: sqlite3.Row) -> Attachment:
        return Attachment(
            id=_int(row, "id"),
            session_id=_int(row, "session_id") or None,
            message_id=_int(row, "message_id") or None,
            filename=_str(row, "filename"),
            mime_type=_str(row, "mime_type"),
            kind=_str(row, "kind", "unsupported"),
            local_path=_str(row, "local_path"),
            size_bytes=_int(row, "size_bytes"),
            sha256=_str(row, "sha256"),
            remote_uri=_str(row, "remote_uri"),
            remote_name=_str(row, "remote_name"),
            remote_expires_at=_str(row, "remote_expires_at"),
            created_at=_int(row, "created_at"),
            status=_str(row, "status", AttachmentStatus.QUEUED),
            truncated=_bool(row, "truncated"),
            error=_str(row, "error"),
            language=_str(row, "language", "text"),
        )

    def insert(self, attachment: Attachment) -> int:
        if not attachment.created_at:
            attachment.created_at = _now()
        attachment.id = self.db.execute(
            "INSERT INTO attachments(session_id, message_id, filename,"
            " mime_type, kind, local_path, size_bytes, sha256, remote_uri,"
            " remote_name, remote_expires_at, created_at, status, truncated,"
            " error, language) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attachment.session_id, attachment.message_id, attachment.filename,
             attachment.mime_type, attachment.kind, attachment.local_path,
             attachment.size_bytes, attachment.sha256, attachment.remote_uri,
             attachment.remote_name, attachment.remote_expires_at,
             attachment.created_at, attachment.status,
             1 if attachment.truncated else 0, attachment.error,
             attachment.language))
        return attachment.id

    def update_status(self, attachment_id: int, status: str,
                      error: str = "") -> None:
        self.db.execute(
            "UPDATE attachments SET status=?, error=? WHERE id=?",
            (status, error[:500], attachment_id))

    def set_remote(self, attachment_id: int, remote_uri: str,
                   remote_name: str, expires_at: str) -> None:
        self.db.execute(
            "UPDATE attachments SET remote_uri=?, remote_name=?,"
            " remote_expires_at=?, status=? WHERE id=?",
            (remote_uri, remote_name, expires_at, AttachmentStatus.UPLOADED,
             attachment_id))

    def attach_to_message(self, attachment_id: int, message_id: int) -> None:
        self.db.execute("UPDATE attachments SET message_id=? WHERE id=?",
                        (message_id, attachment_id))

    def get(self, attachment_id: int) -> Optional[Attachment]:
        row = self.db.query_one("SELECT * FROM attachments WHERE id=?",
                                (attachment_id,))
        return self.from_row(row) if row else None

    def for_message(self, message_id: int) -> List[Attachment]:
        rows = self.db.query(
            "SELECT * FROM attachments WHERE message_id=? ORDER BY id",
            (message_id,))
        return [self.from_row(row) for row in rows]

    def for_session(self, session_id: int) -> List[Attachment]:
        rows = self.db.query(
            "SELECT * FROM attachments WHERE session_id=? ORDER BY id",
            (session_id,))
        return [self.from_row(row) for row in rows]

    def pending_remote(self) -> List[Attachment]:
        rows = self.db.query(
            "SELECT * FROM attachments WHERE remote_name<>''"
            " AND status<>? ORDER BY id", (AttachmentStatus.REMOVED,))
        return [self.from_row(row) for row in rows]

    def delete(self, attachment_id: int) -> bool:
        self.db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        return True

    def purge_older_than(self, cutoff_ms: int) -> int:
        rows = self.db.query(
            "SELECT id FROM attachments WHERE created_at<? AND message_id IS NULL",
            (int(cutoff_ms),))
        ids = [_int(row, "id") for row in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            self.db.execute("DELETE FROM attachments WHERE id IN (%s)"
                            % placeholders, tuple(ids))
        return len(ids)


# ----------------------------------------------------------------- token stats
class TokenStatsRepository(object):
    """Append-only usage ledger plus windowed aggregates."""

    WINDOWS = (("last_hour", HOUR_MS), ("last_24h", DAY_MS), ("all_time", 0))

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, usage: TokenUsage, session_id: Optional[int] = None,
               model_name: str = "", provider_id: str = "gemini",
               when: Optional[int] = None) -> int:
        if usage.is_empty():
            return 0
        return self.db.execute(
            "INSERT INTO token_stats(timestamp, provider_id, model_name,"
            " session_id, input_tokens, output_tokens, thought_tokens,"
            " cached_tokens, tool_tokens, total_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (when or _now(), provider_id, model_name or "", session_id,
             usage.input_tokens, usage.output_tokens, usage.thought_tokens,
             usage.cached_tokens, usage.tool_tokens, usage.total_tokens))

    _SUM = ("COUNT(*) AS requests,"
            " COALESCE(SUM(input_tokens),0) AS input_tokens,"
            " COALESCE(SUM(output_tokens),0) AS output_tokens,"
            " COALESCE(SUM(thought_tokens),0) AS thought_tokens,"
            " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
            " COALESCE(SUM(tool_tokens),0) AS tool_tokens,"
            " COALESCE(SUM(total_tokens),0) AS total_tokens")

    def window(self, key: str, provider_id: Optional[str] = None,
               model_name: Optional[str] = None,
               session_id: Optional[int] = None) -> TokenStats:
        cutoff = 0
        for name, span in self.WINDOWS:
            if name == key:
                cutoff = _now() - span if span else 0
                break
        where: List[str] = []
        params: List[Any] = []
        if cutoff:
            where.append("timestamp>=?")
            params.append(cutoff)
        if provider_id:
            where.append("provider_id=?")
            params.append(provider_id)
        if model_name:
            where.append("model_name=?")
            params.append(model_name)
        if session_id is not None:
            where.append("session_id=?")
            params.append(session_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        row = self.db.query_one(
            "SELECT %s FROM token_stats%s" % (self._SUM, clause), tuple(params))
        if not row:
            return TokenStats(label=key)
        return TokenStats(
            label=key, requests=_int(row, "requests"),
            input_tokens=_int(row, "input_tokens"),
            output_tokens=_int(row, "output_tokens"),
            thought_tokens=_int(row, "thought_tokens"),
            cached_tokens=_int(row, "cached_tokens"),
            tool_tokens=_int(row, "tool_tokens"),
            total_tokens=_int(row, "total_tokens"))

    def all_windows(self, provider_id: Optional[str] = None,
                    model_name: Optional[str] = None,
                    session_id: Optional[int] = None) -> List[TokenStats]:
        return [self.window(key, provider_id, model_name, session_id)
                for key, _span in self.WINDOWS]

    def by_model(self, within_ms: int = 0) -> List[TokenStats]:
        return self._group("model_name", within_ms)

    def by_provider(self, within_ms: int = 0) -> List[TokenStats]:
        return self._group("provider_id", within_ms)

    def by_session(self, within_ms: int = 0, limit: int = 50) -> List[TokenStats]:
        return self._group("session_id", within_ms, limit)

    def _group(self, column: str, within_ms: int, limit: int = 200) -> List[TokenStats]:
        if column not in ("model_name", "provider_id", "session_id"):
            raise DatabaseError("Nieprawidłowa kolumna grupowania")
        where = ""
        params: List[Any] = []
        if within_ms:
            where = " WHERE timestamp>=?"
            params.append(_now() - int(within_ms))
        rows = self.db.query(
            "SELECT %s AS grp, %s FROM token_stats%s GROUP BY %s"
            " ORDER BY total_tokens DESC LIMIT ?"
            % (column, self._SUM, where, column), tuple(params + [int(limit)]))
        out: List[TokenStats] = []
        for row in rows:
            label = row["grp"]
            out.append(TokenStats(
                label="" if label is None else str(label),
                requests=_int(row, "requests"),
                input_tokens=_int(row, "input_tokens"),
                output_tokens=_int(row, "output_tokens"),
                thought_tokens=_int(row, "thought_tokens"),
                cached_tokens=_int(row, "cached_tokens"),
                tool_tokens=_int(row, "tool_tokens"),
                total_tokens=_int(row, "total_tokens")))
        return out

    def models_used(self) -> List[str]:
        rows = self.db.query(
            "SELECT DISTINCT model_name FROM token_stats"
            " WHERE model_name<>'' ORDER BY model_name")
        return [_str(row, "model_name") for row in rows]

    def delete_for_session(self, session_id: int) -> None:
        self.db.execute("DELETE FROM token_stats WHERE session_id=?",
                        (session_id,))

    def purge_before(self, cutoff_ms: int) -> int:
        before = self.db.scalar("SELECT COUNT(*) FROM token_stats WHERE timestamp<?",
                                (int(cutoff_ms),))
        self.db.execute("DELETE FROM token_stats WHERE timestamp<?",
                        (int(cutoff_ms),))
        return before


# --------------------------------------------------------------------- settings
class SettingsRepository(object):
    """Key/value store for small application state (not the API key)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: str = "") -> str:
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return _str(row, "value", default) if row else default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), "" if value is None else str(value)))

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))

    def all(self) -> Dict[str, str]:
        return {_str(row, "key"): _str(row, "value")
                for row in self.db.query("SELECT key, value FROM settings")}


# ------------------------------------------------------------------ model cache
class ModelCacheRepository(object):
    """Short-lived provider model metadata cache (avoids API hammering)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def put_many(self, provider_id: str, models: Sequence[ModelInfo],
                 ttl_seconds: int) -> int:
        if not models:
            return 0
        now = _now()
        expires = now + int(ttl_seconds) * 1000
        statements = []
        for model in models:
            payload = json.dumps({
                "model_id": model.model_id,
                "display_name": model.display_name,
                "description": model.description,
                "input_token_limit": model.input_token_limit,
                "output_token_limit": model.output_token_limit,
                "supported_methods": model.supported_methods,
                "version": model.version,
                "base_model_id": model.base_model_id,
                "thinking": model.thinking,
                "lifecycle": model.lifecycle,
                "capabilities": model.capabilities.flags,
                "min_cached_tokens": model.capabilities.min_cached_tokens,
            }, ensure_ascii=False)
            statements.append((
                "INSERT INTO model_cache(provider_id, model_name, payload_json,"
                " fetched_at, expires_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(provider_id, model_name) DO UPDATE SET"
                " payload_json=excluded.payload_json,"
                " fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (provider_id, model.model_id, payload, now, expires)))
        statements.append((
            "DELETE FROM model_cache WHERE provider_id=? AND expires_at<?",
            (provider_id, now)))
        self.db.execute_many(statements)
        return len(models)

    def fresh(self, provider_id: str) -> List[ModelInfo]:
        rows = self.db.query(
            "SELECT payload_json FROM model_cache WHERE provider_id=?"
            " AND expires_at>? ORDER BY model_name", (provider_id, _now()))
        out: List[ModelInfo] = []
        for row in rows:
            try:
                payload = json.loads(_str(row, "payload_json"))
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            from models.provider_models import ModelCapabilities
            caps = ModelCapabilities(
                flags=payload.get("capabilities") or {},
                min_cached_tokens=int(payload.get("min_cached_tokens") or 0))
            out.append(ModelInfo(
                model_id=str(payload.get("model_id") or ""),
                display_name=str(payload.get("display_name") or ""),
                description=str(payload.get("description") or ""),
                input_token_limit=int(payload.get("input_token_limit") or 0),
                output_token_limit=int(payload.get("output_token_limit") or 0),
                supported_methods=list(payload.get("supported_methods") or []),
                version=str(payload.get("version") or ""),
                base_model_id=str(payload.get("base_model_id") or ""),
                thinking=payload.get("thinking"),
                lifecycle=str(payload.get("lifecycle") or "unknown"),
                capabilities=caps))
        return out

    def clear(self, provider_id: Optional[str] = None) -> None:
        if provider_id:
            self.db.execute("DELETE FROM model_cache WHERE provider_id=?",
                            (provider_id,))
        else:
            self.db.execute("DELETE FROM model_cache")


# -------------------------------------------------------------- provider cache
class ProviderCacheRepository(object):
    """Local bookkeeping for provider-side caches.

    The Interactions API only supports *implicit* caching, so rows here are
    used for fingerprints/observation (cache hit reporting), not for remote
    object lifecycle. The schema is ready for explicit caching if a provider
    adds it.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def put(self, provider_id: str, model_name: str, source_hash: str,
            remote_name: str = "", token_count: int = 0,
            ttl_seconds: int = 0, status: str = "active") -> int:
        now = _now()
        expires = now + int(ttl_seconds) * 1000 if ttl_seconds else 0
        return self.db.execute(
            "INSERT INTO provider_cache(provider_id, model_name, remote_name,"
            " source_hash, created_at, expires_at, token_count, status)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (provider_id, model_name, remote_name, source_hash, now, expires,
             int(token_count), status))

    def find(self, provider_id: str, model_name: str,
             source_hash: str) -> Optional[sqlite3.Row]:
        return self.db.query_one(
            "SELECT * FROM provider_cache WHERE provider_id=? AND model_name=?"
            " AND source_hash=? AND status='active'"
            " AND (expires_at=0 OR expires_at>?) ORDER BY id DESC LIMIT 1",
            (provider_id, model_name, source_hash, _now()))

    def mark(self, cache_id: int, status: str) -> None:
        self.db.execute("UPDATE provider_cache SET status=? WHERE id=?",
                        (status, int(cache_id)))

    def list_all(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM provider_cache ORDER BY id DESC LIMIT ?",
            (int(limit),))

    def purge_expired(self) -> int:
        removed = self.db.scalar(
            "SELECT COUNT(*) FROM provider_cache WHERE expires_at<>0 AND expires_at<?",
            (_now(),))
        self.db.execute(
            "DELETE FROM provider_cache WHERE expires_at<>0 AND expires_at<?",
            (_now(),))
        return removed


# ------------------------------------------------------- model availability
class ModelAvailabilityRepository(object):
    """Durable record of which models this credential can actually use.

    The mechanism is general (task §6): nothing here knows any model name.
    Facts come from real API responses - a 404 "no longer available" or a
    successful free ``countTokens`` probe.
    """

    SOURCE_ERROR = "error"
    SOURCE_PROBE = "probe"
    SOURCE_USER = "user"

    def __init__(self, db: Database) -> None:
        self.db = db

    def mark(self, provider_id: str, model_name: str, available: bool,
             reason: str = "", http_status: int = 0,
             source: str = SOURCE_ERROR) -> None:
        self.db.execute(
            "INSERT INTO model_availability(provider_id, model_name, available,"
            " reason, http_status, verified_at, source)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(provider_id, model_name) DO UPDATE SET"
            " available=excluded.available, reason=excluded.reason,"
            " http_status=excluded.http_status, verified_at=excluded.verified_at,"
            " source=excluded.source",
            (provider_id, model_name, 1 if available else 0, reason[:300],
             int(http_status), _now(), source))

    def unavailable(self, provider_id: str) -> Dict[str, str]:
        """``{model_name: reason}`` for everything proven unusable."""
        rows = self.db.query(
            "SELECT model_name, reason FROM model_availability"
            " WHERE provider_id=? AND available=0", (provider_id,))
        return {_str(row, "model_name"): _str(row, "reason") for row in rows}

    def is_available(self, provider_id: str, model_name: str) -> Optional[bool]:
        row = self.db.query_one(
            "SELECT available FROM model_availability WHERE provider_id=?"
            " AND model_name=?", (provider_id, model_name))
        if row is None:
            return None
        return bool(_int(row, "available"))

    def all_entries(self, provider_id: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
        if provider_id:
            rows = self.db.query(
                "SELECT * FROM model_availability WHERE provider_id=?"
                " ORDER BY available, model_name LIMIT ?",
                (provider_id, int(limit)))
        else:
            rows = self.db.query(
                "SELECT * FROM model_availability ORDER BY provider_id,"
                " available, model_name LIMIT ?", (int(limit),))
        return [{
            "provider_id": _str(row, "provider_id"),
            "model_name": _str(row, "model_name"),
            "available": bool(_int(row, "available")),
            "reason": _str(row, "reason"),
            "http_status": _int(row, "http_status"),
            "verified_at": _int(row, "verified_at"),
            "source": _str(row, "source"),
        } for row in rows]

    def forget(self, provider_id: str,
               model_name: Optional[str] = None) -> int:
        """Clear learned facts (e.g. after the user fixes their API plan)."""
        if model_name:
            self.db.execute(
                "DELETE FROM model_availability WHERE provider_id=?"
                " AND model_name=?", (provider_id, model_name))
            return 1
        removed = self.db.scalar(
            "SELECT COUNT(*) FROM model_availability WHERE provider_id=?",
            (provider_id,))
        self.db.execute("DELETE FROM model_availability WHERE provider_id=?",
                        (provider_id,))
        return removed


# --------------------------------------------------------------------- presets
class PresetRepository(object):
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def from_row(row: sqlite3.Row) -> PromptPreset:
        return PromptPreset(id=_int(row, "id"), name=_str(row, "name"),
                            system_instruction=_str(row, "system_instruction"),
                            builtin=_bool(row, "builtin"),
                            enabled=_bool(row, "enabled"))

    def list(self, include_disabled: bool = True) -> List[PromptPreset]:
        sql = "SELECT * FROM prompt_presets"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY builtin DESC, sort_order, name"
        return [self.from_row(row) for row in self.db.query(sql)]

    def get(self, preset_id: int) -> Optional[PromptPreset]:
        row = self.db.query_one("SELECT * FROM prompt_presets WHERE id=?",
                                (preset_id,))
        return self.from_row(row) if row else None

    def by_name(self, name: str) -> Optional[PromptPreset]:
        row = self.db.query_one("SELECT * FROM prompt_presets WHERE name=?",
                                (name,))
        return self.from_row(row) if row else None

    def create(self, name: str, system_instruction: str) -> int:
        return self.db.execute(
            "INSERT INTO prompt_presets(name, system_instruction, builtin,"
            " enabled, sort_order) VALUES(?,?,0,1,99)",
            (name.strip()[:80], system_instruction))

    def update(self, preset_id: int, name: str,
               system_instruction: str, enabled: bool = True) -> bool:
        self.db.execute(
            "UPDATE prompt_presets SET name=?, system_instruction=?, enabled=?"
            " WHERE id=?", (name.strip()[:80], system_instruction,
                            1 if enabled else 0, int(preset_id)))
        return True

    def delete(self, preset_id: int) -> bool:
        preset = self.get(preset_id)
        if preset is None:
            return False
        if preset.builtin:
            # Built-ins are disabled rather than deleted so upgrades stay sane.
            self.db.execute("UPDATE prompt_presets SET enabled=0 WHERE id=?",
                            (int(preset_id),))
            return True
        self.db.execute("DELETE FROM prompt_presets WHERE id=?", (int(preset_id),))
        return True


# -------------------------------------------------------------- pinned context
class PinnedContextRepository(object):
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def from_row(row: sqlite3.Row) -> PinnedContext:
        return PinnedContext(
            id=_int(row, "id"),
            session_id=_int(row, "session_id") or None,
            label=_str(row, "label"),
            text=_str(row, "text"),
            source=_str(row, "source", "manual"),
            enabled=_bool(row, "enabled"),
            tokens=_int(row, "tokens"),
            created_at=_int(row, "created_at"))

    def add(self, session_id: Optional[int], label: str, text: str,
            source: str = "manual", tokens: int = 0) -> int:
        return self.db.execute(
            "INSERT INTO pinned_context(session_id, label, text, source,"
            " enabled, tokens, created_at) VALUES(?,?,?,?,1,?,?)",
            (session_id, label.strip()[:120], text, source, int(tokens), _now()))

    def list(self, session_id: Optional[int] = None,
             enabled_only: bool = True) -> List[PinnedContext]:
        where: List[str] = []
        params: List[Any] = []
        if session_id is not None:
            where.append("(session_id=? OR session_id IS NULL)")
            params.append(session_id)
        if enabled_only:
            where.append("enabled=1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.query(
            "SELECT * FROM pinned_context" + clause + " ORDER BY id", tuple(params))
        return [self.from_row(row) for row in rows]

    def set_enabled(self, pinned_id: int, enabled: bool) -> None:
        self.db.execute("UPDATE pinned_context SET enabled=? WHERE id=?",
                        (1 if enabled else 0, int(pinned_id)))

    def delete(self, pinned_id: int) -> bool:
        self.db.execute("DELETE FROM pinned_context WHERE id=?", (int(pinned_id),))
        return True


# ----------------------------------------------------------------------- search
class SearchService(object):
    """LIKE-based search; benchmarked to stay fast on thousands of messages."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def search_messages(self, query: str, limit: int = 300,
                        session_id: Optional[int] = None,
                        role: Optional[str] = None,
                        model_name: Optional[str] = None,
                        since_ms: Optional[int] = None,
                        until_ms: Optional[int] = None,
                        exact_phrase: bool = False) -> List[SearchResult]:
        term = (query or "").strip()
        if not term:
            return []
        where: List[str] = ["m.content_text LIKE ? ESCAPE '\\'"]
        pattern = _escape_like(term)
        params: List[Any] = [pattern if exact_phrase else "%" + pattern + "%"]
        if session_id is not None:
            where.append("m.session_id=?")
            params.append(session_id)
        if role:
            where.append("m.role=?")
            params.append(role)
        if model_name:
            where.append("m.model_name=?")
            params.append(model_name)
        if since_ms:
            where.append("m.timestamp>=?")
            params.append(int(since_ms))
        if until_ms:
            where.append("m.timestamp<=?")
            params.append(int(until_ms))
        params.append(int(limit))
        rows = self.db.query(
            "SELECT m.id AS message_id, m.session_id AS session_id, m.role,"
            " m.content_text, m.timestamp, m.model_name, s.title"
            " FROM messages m JOIN sessions s ON s.id = m.session_id"
            " WHERE " + " AND ".join(where) +
            " ORDER BY m.timestamp DESC LIMIT ?", tuple(params))
        results: List[SearchResult] = []
        for row in rows:
            text = _str(row, "content_text")
            results.append(SearchResult(
                session_id=_int(row, "session_id"),
                session_title=_str(row, "title", "Nowa rozmowa"),
                message_id=_int(row, "message_id"),
                role=_str(row, "role"),
                preview=_make_preview(text, term),
                timestamp=_int(row, "timestamp"),
                model_name=_str(row, "model_name")))
        return results

    def search_sessions(self, query: str, limit: int = 100) -> List[Session]:
        term = (query or "").strip()
        if not term:
            return []
        rows = self.db.query(
            "SELECT * FROM sessions WHERE title LIKE ? ESCAPE '\\'"
            " ORDER BY updated_at DESC LIMIT ?",
            ("%" + _escape_like(term) + "%", int(limit)))
        return [SessionRepository.from_row(row) for row in rows]


def _make_preview(text: str, term: str, width: int = 160) -> str:
    """Snippet centred on the first match, whitespace-collapsed."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    idx = flat.lower().find(term.lower())
    if idx < 0:
        return flat[:width] + ("..." if len(flat) > width else "")
    start = max(0, idx - width // 3)
    end = min(len(flat), start + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(flat) else ""
    return prefix + flat[start:end] + suffix
