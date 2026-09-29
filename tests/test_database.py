"""Database layer tests: schema, WAL, CRUD, pagination, migrations, stats."""

import os
import sqlite3
import threading

import pytest

from db.database import Database, DatabaseError
from db.migrations import (apply_migrations, get_user_version,
                           migration_history, table_names)
from db.repositories import (AttachmentRepository, MessageRepository,
                             ModelCacheRepository, PinnedContextRepository,
                             PresetRepository, ProviderCacheRepository,
                             SearchService, SessionRepository,
                             SettingsRepository, TokenStatsRepository)
from models.attachment_models import Attachment, AttachmentKind, AttachmentStatus
from models.message_models import Message, MessageStatus, Role
from models.provider_models import ModelCapabilities, ModelInfo
from models.usage_models import TokenUsage


# ------------------------------------------------------------------ schema
def test_schema_created_and_wal_enabled(temp_db):
    assert temp_db.journal_mode == "wal"
    assert temp_db.schema_version >= 1
    tables = set(table_names(temp_db.connection()))
    for expected in ("sessions", "messages", "token_stats", "attachments",
                     "settings", "provider_cache", "model_cache",
                     "prompt_presets", "pinned_context", "schema_migrations"):
        assert expected in tables


def test_pragmas_applied(temp_db):
    conn = temp_db.connection()
    assert str(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == "1"
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) > 0
    assert str(conn.execute("PRAGMA synchronous").fetchone()[0]).lower() in (
        "1", "normal")


def test_migrations_are_idempotent(temp_db):
    conn = temp_db.connection()
    version_before = get_user_version(conn)
    assert apply_migrations(conn) == []          # nothing new to apply
    assert get_user_version(conn) == version_before
    history = migration_history(conn)
    assert history and history[0][0] == 1


def test_migration_upgrade_path_from_scratch(tmp_path):
    path = str(tmp_path / "fresh.sqlite3")
    db = Database(path)
    db.initialize()
    assert db.applied_migrations == [1]
    db.close_all()
    db2 = Database(path)
    db2.initialize()
    assert db2.applied_migrations == []          # already migrated
    assert db2.schema_version == 1
    db2.close_all()


def test_integrity_check_ok(temp_db):
    assert temp_db.integrity_check() == "ok"


# ---------------------------------------------------------------- sessions
def test_session_crud_and_listing(memory_db):
    repo = SessionRepository(memory_db)
    first = repo.create(title="Pierwsza", model_id="gemini-3.8-flash")
    second = repo.create(title="Druga")
    assert first.id and second.id and first.id != second.id

    repo.rename(first.id, "Zmieniona")
    loaded = repo.get(first.id)
    assert loaded is not None and loaded.title == "Zmieniona"
    assert loaded.model_id == "gemini-3.8-flash"

    repo.set_flags(second.id, archived=True)
    active = repo.list()
    assert [s.id for s in active] == [first.id]
    assert len(repo.list(include_archived=True)) == 2

    repo.set_flags(second.id, archived=False, pinned=True)
    repo.touch(second.id, when=first.updated_at + 10_000)
    ordered = repo.list()
    assert ordered[0].id == second.id           # pinned first
    assert repo.count() == 2


def test_session_delete_cascades(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    stats = TokenStatsRepository(memory_db)
    session = sessions.create(title="Do skasowania")
    messages.insert(Message.user_text("cześć", session.id))
    stats.record(TokenUsage(input_tokens=5, total_tokens=5), session.id, "m")
    sessions.delete(session.id)
    assert sessions.get(session.id) is None
    assert messages.count(session.id) == 0
    assert stats.window("all_time").requests == 0


def test_session_draft_and_state_persist(memory_db):
    repo = SessionRepository(memory_db)
    session = repo.create(title="Draft")
    repo.set_draft(session.id, "nieskończone zdanie")
    repo.set_remote_state(session.id, "v1_abc", "stateful")
    loaded = repo.get(session.id)
    assert loaded.draft == "nieskończone zdanie"
    assert loaded.remote_interaction_id == "v1_abc"
    assert loaded.state_mode == "stateful"
    assert loaded.state_label()


def test_session_pagination_with_many_rows(memory_db):
    repo = SessionRepository(memory_db)
    for index in range(250):
        repo.create(title="Sesja %d" % index)
    page = repo.list(limit=80, offset=0)
    assert len(page) == 80
    assert len(repo.list(limit=80, offset=240)) == 10
    assert repo.count() == 250


# ---------------------------------------------------------------- messages
def test_message_insert_finalize_and_read(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    session = sessions.create()
    user = Message.user_text("Pytanie", session.id)
    messages.insert(user)
    assistant = Message.assistant_placeholder(session.id, "gemini-3.8-flash")
    messages.insert(assistant)
    usage = TokenUsage(input_tokens=12, output_tokens=30, thought_tokens=5,
                       cached_tokens=4, total_tokens=51)
    messages.finalize(assistant.id, "Odpowiedź **markdown**",
                      MessageStatus.COMPLETED, usage,
                      interaction_id="v1_xyz", model_name="gemini-3.8-flash",
                      thought_summary="myślałem")
    loaded = messages.get(assistant.id)
    assert loaded.text == "Odpowiedź **markdown**"
    assert loaded.usage.total_tokens == 51
    assert loaded.usage.cached_tokens == 4
    assert loaded.interaction_id == "v1_xyz"
    assert loaded.thought_summary == "myślałem"
    assert loaded.status == MessageStatus.COMPLETED


def test_message_pagination_returns_pages_not_history(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    session = sessions.create()
    for index in range(500):
        messages.insert(Message.user_text("wiadomość %d" % index, session.id))
    page = messages.page(session.id, limit=60)
    assert len(page) == 60
    assert page[-1].text == "wiadomość 499"       # newest included
    older = messages.page(session.id, limit=60, before_id=page[0].id)
    assert len(older) == 60
    assert older[-1].text == "wiadomość 439"
    assert messages.count(session.id) == 500
    assert messages.oldest_id(session.id) == page[0].id - 440


def test_delete_from_removes_tail(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    session = sessions.create()
    ids = [messages.insert(Message.user_text("m%d" % i, session.id))
           for i in range(5)]
    removed = messages.delete_from(session.id, ids[2])
    assert removed == 3
    assert messages.count(session.id) == 2


def test_fork_copies_prefix_without_touching_source(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    source = sessions.create(title="Oryginał")
    ids = [messages.insert(Message.user_text("m%d" % i, source.id))
           for i in range(4)]
    fork = sessions.create(title="Fork", parent_session_id=source.id,
                           fork_message_id=ids[1])
    copied = messages.copy_range(source.id, fork.id, ids[1])
    assert copied == 2
    assert messages.count(fork.id) == 2
    assert messages.count(source.id) == 4
    assert fork.is_fork


# -------------------------------------------------------------- token stats
def test_token_stats_windows_and_grouping(memory_db):
    stats = TokenStatsRepository(memory_db)
    now = int(__import__("time").time() * 1000)
    stats.record(TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                 1, "gemini-3.8-flash", when=now)
    stats.record(TokenUsage(input_tokens=100, output_tokens=50,
                            thought_tokens=20, total_tokens=170),
                 1, "gemini-3.5-flash", when=now - 2 * 3600 * 1000)
    stats.record(TokenUsage(input_tokens=1000, total_tokens=1000),
                 2, "gemini-3.5-flash", when=now - 48 * 3600 * 1000)

    hour = stats.window("last_hour")
    assert hour.total_tokens == 15 and hour.requests == 1
    day = stats.window("last_24h")
    assert day.total_tokens == 185 and day.requests == 2
    alltime = stats.window("all_time")
    assert alltime.total_tokens == 1185 and alltime.requests == 3
    assert alltime.thought_tokens == 20

    by_model = {row.label: row.total_tokens for row in stats.by_model()}
    assert by_model == {"gemini-3.8-flash": 15, "gemini-3.5-flash": 1170}
    assert set(stats.models_used()) == {"gemini-3.8-flash", "gemini-3.5-flash"}
    assert len(stats.all_windows()) == 3


def test_token_stats_session_filter_and_purge(memory_db):
    stats = TokenStatsRepository(memory_db)
    stats.record(TokenUsage(total_tokens=7), 1, "m")
    stats.record(TokenUsage(total_tokens=9), 2, "m")
    assert stats.window("all_time", session_id=1).total_tokens == 7
    stats.delete_for_session(1)
    assert stats.window("all_time").total_tokens == 9


def test_empty_usage_is_not_recorded(memory_db):
    stats = TokenStatsRepository(memory_db)
    assert stats.record(TokenUsage(), 1, "m") == 0
    assert stats.window("all_time").requests == 0


# -------------------------------------------------------------- attachments
def test_attachment_lifecycle(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    repo = AttachmentRepository(memory_db)
    session = sessions.create()
    message_id = messages.insert(Message.user_text("z plikiem", session.id))
    attachment = Attachment(session_id=session.id, filename="dane.py",
                            mime_type="text/x-python", kind=AttachmentKind.TEXT,
                            size_bytes=1234, sha256="ab" * 32,
                            status=AttachmentStatus.READY, language="python")
    repo.insert(attachment)
    repo.attach_to_message(attachment.id, message_id)
    repo.set_remote(attachment.id, "https://api/files/1", "files/1",
                    "2026-10-01T00:00:00Z")
    loaded = repo.get(attachment.id)
    assert loaded.status == AttachmentStatus.UPLOADED
    assert loaded.remote_name == "files/1"
    assert loaded.language == "python"
    assert [a.id for a in repo.for_message(message_id)] == [attachment.id]
    repo.update_status(attachment.id, AttachmentStatus.FAILED, "limit")
    assert repo.get(attachment.id).error == "limit"
    repo.delete(attachment.id)
    assert repo.get(attachment.id) is None


def test_attachment_purge_keeps_referenced_rows(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    repo = AttachmentRepository(memory_db)
    session = sessions.create(title="Załączniki")
    message_id = messages.insert(Message.user_text("z plikiem", session.id))
    repo.insert(Attachment(session_id=session.id, filename="stary.txt",
                           created_at=1, status=AttachmentStatus.READY))
    referenced = Attachment(session_id=session.id, filename="uzywany.txt",
                            created_at=1, status=AttachmentStatus.READY,
                            message_id=message_id)
    repo.insert(referenced)
    removed = repo.purge_older_than(10**12)
    assert removed == 1
    assert repo.get(referenced.id) is not None


# ------------------------------------------------------------------ settings
def test_settings_repository_roundtrip(memory_db):
    repo = SettingsRepository(memory_db)
    repo.set("ui.theme", "dark")
    repo.set("ui.width", 1024)
    assert repo.get("ui.theme") == "dark"
    assert repo.get_int("ui.width") == 1024
    assert repo.get("missing", "fallback") == "fallback"
    repo.set("ui.theme", "light")
    assert repo.get("ui.theme") == "light"
    repo.delete("ui.theme")
    assert repo.get("ui.theme") == ""
    assert "ui.width" in repo.all()


# --------------------------------------------------------------- model cache
def test_model_cache_ttl_and_refresh(memory_db):
    cache = ModelCacheRepository(memory_db)
    model = ModelInfo(model_id="gemini-3.8-flash", display_name="Gemini 3.8",
                      input_token_limit=1048576, output_token_limit=65536,
                      supported_methods=["generateContent", "countTokens"],
                      thinking=True,
                      capabilities=ModelCapabilities({"text_input": True,
                                                      "streaming": True}, 4096))
    cache.put_many("gemini", [model], ttl_seconds=60)
    fresh = cache.fresh("gemini")
    assert len(fresh) == 1
    assert fresh[0].model_id == "gemini-3.8-flash"
    assert fresh[0].input_token_limit == 1048576
    assert fresh[0].capabilities.supports("streaming")
    assert fresh[0].capabilities.min_cached_tokens == 4096

    cache.put_many("gemini", [model], ttl_seconds=-1)   # already expired
    assert cache.fresh("gemini") == []


# ------------------------------------------------------------ provider cache
def test_provider_cache_fingerprint_lookup(memory_db):
    repo = ProviderCacheRepository(memory_db)
    repo.put("gemini", "gemini-3.8-flash", "hash-1", token_count=5000)
    row = repo.find("gemini", "gemini-3.8-flash", "hash-1")
    assert row is not None
    repo.mark(int(row["id"]), "invalid")
    assert repo.find("gemini", "gemini-3.8-flash", "hash-1") is None
    assert repo.find("gemini", "other", "hash-1") is None


# ------------------------------------------------------------------- presets
def test_presets_seeded_and_editable(memory_db):
    repo = PresetRepository(memory_db)
    names = [p.name for p in repo.list()]
    assert "Kodowanie" in names and "Ogólny" in names
    created = repo.create("Mój", "Bądź zwięzły.")
    assert repo.get(created).name == "Mój"
    repo.update(created, "Mój2", "Nowa instrukcja", enabled=False)
    assert repo.get(created).name == "Mój2"
    assert repo.get(created).enabled is False
    assert repo.delete(created) is True
    builtin = repo.by_name("Kodowanie")
    repo.delete(builtin.id)                       # disables, never deletes
    assert repo.by_name("Kodowanie").enabled is False


def test_pinned_context_scoped_to_session(memory_db):
    sessions = SessionRepository(memory_db)
    repo = PinnedContextRepository(memory_db)
    session = sessions.create()
    repo.add(session.id, "Zasady", "Odpowiadaj po polsku", tokens=6)
    repo.add(None, "Globalne", "Zawsze zwięźle", tokens=3)
    items = repo.list(session.id)
    assert len(items) == 2
    repo.set_enabled(items[0].id, False)
    assert len(repo.list(session.id)) == 1
    repo.delete(items[1].id)
    assert repo.list(session.id) == []


# -------------------------------------------------------------------- search
def test_search_finds_message_and_previews(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    search = SearchService(memory_db)
    session = sessions.create(title="Projekt Core2Chat")
    messages.insert(Message.user_text("Jak skonfigurować WAL w SQLite?",
                                      session.id))
    results = search.search_messages("WAL")
    assert len(results) == 1
    assert results[0].session_id == session.id
    assert "WAL" in results[0].preview
    assert search.search_messages("nic-takiego") == []
    assert [s.id for s in search.search_sessions("Core2Chat")] == [session.id]


def test_search_filters_and_escaping(memory_db):
    sessions = SessionRepository(memory_db)
    messages = MessageRepository(memory_db)
    search = SearchService(memory_db)
    session = sessions.create(title="Filtry")
    messages.insert(Message(role=Role.ASSISTANT, text="100% pewności_że tak",
                            session_id=session.id, model_name="gemini-x"))
    assert len(search.search_messages("100%")) == 1        # % escaped
    assert len(search.search_messages("pewności_że")) == 1  # _ escaped
    assert search.search_messages("pewności", role=Role.USER) == []
    assert len(search.search_messages("pewności", model_name="gemini-x")) == 1
    assert len(search.search_messages("pewności", session_id=session.id)) == 1
    assert search.search_messages("") == []


def test_search_performance_on_thousands_of_messages(temp_db):
    """Search must stay fast without a heavyweight index (specification §18)."""
    import time
    sessions = SessionRepository(temp_db)
    messages = MessageRepository(temp_db)
    search = SearchService(temp_db)
    session = sessions.create(title="Duża")
    batch = []
    for index in range(5000):
        text = "wiadomość testowa numer %d z frazą needle-%d" % (index, index)
        batch.append(Message.user_text(text, session.id, timestamp=index))
    messages.insert_many(batch)
    started = time.monotonic()
    results = search.search_messages("needle-4999")
    elapsed = time.monotonic() - started
    assert len(results) == 1
    assert elapsed < 0.5, "search too slow: %.3fs" % elapsed


# --------------------------------------------------------------- concurrency
def test_concurrent_writes_from_threads(tmp_path):
    db = Database(str(tmp_path / "concurrent.sqlite3"))
    db.initialize()
    sessions = SessionRepository(db)
    messages = MessageRepository(db)
    session = sessions.create(title="współbieżna")
    errors = []

    def writer(thread_index):
        try:
            repo = MessageRepository(db)
            for index in range(40):
                repo.insert(Message.user_text(
                    "t%d-m%d" % (thread_index, index), session.id))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))
        finally:
            db.close_thread_connection()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert messages.count(session.id) == 160
    db.close_all()


def test_database_reports_write_errors(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "err.sqlite3"))
    db.initialize()
    with pytest.raises(DatabaseError):
        db.execute("INSERT INTO nope(x) VALUES(1)")
    db.close_all()


def test_closed_database_rejects_use(tmp_path):
    db = Database(str(tmp_path / "closed.sqlite3"))
    db.initialize()
    db.close_all()
    with pytest.raises(DatabaseError):
        db.connection()


def test_sqlite_row_factory_and_helpers(memory_db):
    row = memory_db.query_one("SELECT 1 AS a, NULL AS b")
    assert row["a"] == 1
    assert memory_db.scalar("SELECT COUNT(*) FROM sessions") == 0
