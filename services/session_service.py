"""Session services: lifecycle operations, drafts, forking, cleanup.

Keeps the GUI free of database logic (specification §1 rule 8).
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from models.attachment_models import AttachmentStatus
from models.chat_models import Session, StateMode
from models.message_models import Message, MessageStatus, Role
from utils.text import derive_title

log = get_logger("sessions")

DRAFT_DEBOUNCE_SECONDS = 0.8


class SessionService(object):
    """CRUD + higher level session operations."""

    def __init__(self, context: Any) -> None:
        self.context = context

    # ------------------------------------------------------------- lifecycle
    def create(self, model_id: str = "", title: str = "",
               state_mode: str = "", preset: str = "") -> Session:
        settings = self.context.settings
        session = self.context.sessions.create(
            title=title or "Nowa rozmowa",
            provider_id=settings.provider_id,
            model_id=model_id or settings.model_id,
            state_mode=state_mode or settings.state_mode,
            system_preset=preset)
        log.info("session.created id=%s model=%s", session.id, session.model_id)
        return session

    def get(self, session_id: int) -> Optional[Session]:
        return self.context.sessions.get(int(session_id))

    def list(self, include_archived: bool = False, limit: int = 80,
             offset: int = 0) -> Tuple[List[Session], int]:
        sessions = self.context.sessions.list(include_archived=include_archived,
                                              limit=limit, offset=offset)
        total = self.context.sessions.count(include_archived=include_archived)
        return sessions, total

    def rename(self, session_id: int, title: str) -> bool:
        return self.context.sessions.rename(int(session_id), title)

    def delete(self, session_id: int) -> bool:
        log.info("session.deleted id=%s", session_id)
        return self.context.sessions.delete(int(session_id))

    def set_archived(self, session_id: int, archived: bool) -> None:
        self.context.sessions.set_flags(int(session_id), archived=archived)

    def set_pinned(self, session_id: int, pinned: bool) -> None:
        self.context.sessions.set_flags(int(session_id), pinned=pinned)

    def clear(self, session_id: int) -> int:
        removed = self.context.sessions.clear_messages(int(session_id))
        log.info("session.cleared id=%s messages=%d", session_id, removed)
        return removed

    def set_model(self, session_id: int, model_id: str) -> None:
        self.context.sessions.set_model(int(session_id), model_id)

    def set_state_mode(self, session_id: int, mode: str) -> None:
        if mode not in StateMode.ALL:
            return
        self.context.sessions.set_remote_state(int(session_id), "", mode)

    def touch(self, session_id: int) -> None:
        self.context.sessions.touch(int(session_id))

    def ensure_title(self, session: Session, first_user_text: str) -> bool:
        """Name a fresh session after its first message (never overwrite)."""
        if session.title and session.title != "Nowa rozmowa":
            return False
        title = derive_title(first_user_text)
        if not title:
            return False
        self.context.sessions.rename(int(session.id or 0), title)
        session.title = title
        return True

    # ---------------------------------------------------------------- drafts
    def save_draft(self, session_id: int, text: str) -> None:
        self.context.sessions.set_draft(int(session_id), text or "")

    def load_draft(self, session_id: int) -> str:
        session = self.context.sessions.get(int(session_id))
        return session.draft if session else ""

    # ------------------------------------------------------------------ fork
    def fork(self, session_id: int, up_to_message_id: Optional[int] = None,
             title: str = "") -> Session:
        """Branch a session, sharing the prefix by copy-on-fork of rows only."""
        source = self.context.sessions.get(int(session_id))
        if source is None:
            raise ValueError("Sesja %s nie istnieje." % session_id)
        base_title = title or ("%s (kopia)" % source.title)
        fork = self.context.sessions.create(
            title=base_title[:200], provider_id=source.provider_id,
            model_id=source.model_id, state_mode=StateMode.LOCAL,
            parent_session_id=source.id, fork_message_id=up_to_message_id,
            system_preset=source.system_preset)
        copied = self.context.messages.copy_range(int(session_id),
                                                  int(fork.id or 0),
                                                  up_to_message_id)
        # Remote interaction state must never be inherited: the provider does
        # not know about our local branch.
        self.context.sessions.set_remote_state(int(fork.id or 0), "",
                                               StateMode.LOCAL)
        log.info("session.forked source=%s fork=%s messages=%d", session_id,
                 fork.id, copied)
        return fork

    # ------------------------------------------------------------- history
    def history_page(self, session_id: int, limit: int = 60,
                     before_id: Optional[int] = None
                     ) -> Tuple[List[Message], int, Optional[int]]:
        """Return (messages, total, oldest_id_in_db) for one page."""
        messages = self.context.messages.page(int(session_id), limit=limit,
                                              before_id=before_id)
        total = self.context.messages.count(int(session_id))
        oldest = self.context.messages.oldest_id(int(session_id))
        return messages, total, oldest

    def all_messages(self, session_id: int) -> List[Message]:
        """Full transcript - used only by export, never by the UI."""
        return self.context.messages.page(int(session_id), limit=100000)

    def last_message(self, session_id: int,
                     role: Optional[str] = None) -> Optional[Message]:
        return self.context.messages.last(int(session_id), role)

    def remove_tail_from(self, session_id: int, message_id: int) -> int:
        return self.context.messages.delete_from(int(session_id),
                                                 int(message_id))

    # ------------------------------------------------------------- attachments
    def attachments_for_message(self, message_id: int) -> List[Any]:
        return self.context.attachments_repo.for_message(int(message_id))

    def cleanup_remote_files(self, retention_days: int) -> int:
        """Delete remote files that are expired or no longer referenced."""
        if retention_days < 0:
            return 0
        removed = 0
        cutoff_ms = int(time.time() * 1000) - retention_days * 86400 * 1000
        provider = self.context.provider
        for attachment in self.context.attachments_repo.pending_remote():
            expired = _expired(attachment.remote_expires_at)
            stale = attachment.created_at < cutoff_ms
            if not (expired or stale):
                continue
            if provider is not None and attachment.remote_name:
                try:
                    provider.delete_file(attachment.remote_name)
                except Exception as exc:
                    log.warning("sessions.remote_delete_failed id=%s err=%s",
                                attachment.id, exc)
            self.context.attachments_repo.update_status(
                int(attachment.id or 0), AttachmentStatus.REMOVED,
                "usunięto zdalny plik")
            removed += 1
        if removed:
            log.info("sessions.remote_files_cleaned count=%d", removed)
        return removed

    def purge_orphan_attachments(self, retention_days: int) -> int:
        if retention_days < 0:
            return 0
        cutoff_ms = int(time.time() * 1000) - retention_days * 86400 * 1000
        return self.context.attachments_repo.purge_older_than(cutoff_ms)

    # ------------------------------------------------------------ statistics
    def usage_summary(self) -> Dict[str, Any]:
        return {
            "sessions": self.context.sessions.count(include_archived=True),
            "messages": self.context.db.scalar("SELECT COUNT(*) FROM messages"),
            "windows": [stats.as_list()
                        for stats in self.context.stats.all_windows()],
        }


def _expired(timestamp: str) -> bool:
    if not timestamp:
        return False
    try:
        cleaned = timestamp.replace("Z", "+00:00")
        moment = time.mktime(time.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return False
    return moment < time.time()
