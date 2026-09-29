"""SQLite schema and forward-only migrations.

Rules:
* migrations are additive and idempotent (safe to re-run),
* ``PRAGMA user_version`` records the applied schema version,
* destructive changes are never performed automatically.
"""

import sqlite3
import time
from typing import List, Tuple

from core.constants import DB_SCHEMA_VERSION

SCHEMA_V1: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        title               TEXT NOT NULL DEFAULT 'Nowa rozmowa',
        provider_id         TEXT NOT NULL DEFAULT 'gemini',
        model_used          TEXT NOT NULL DEFAULT '',
        remote_interaction_id TEXT NOT NULL DEFAULT '',
        state_mode          TEXT NOT NULL DEFAULT 'local',
        system_preset       TEXT NOT NULL DEFAULT '',
        created_at          INTEGER NOT NULL DEFAULT 0,
        updated_at          INTEGER NOT NULL DEFAULT 0,
        archived            INTEGER NOT NULL DEFAULT 0,
        pinned              INTEGER NOT NULL DEFAULT 0,
        draft               TEXT NOT NULL DEFAULT '',
        parent_session_id   INTEGER,
        fork_message_id     INTEGER,
        metadata_json       TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated "
    "ON sessions(archived, pinned DESC, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role                TEXT NOT NULL DEFAULT 'user',
        content_text        TEXT NOT NULL DEFAULT '',
        content_json        TEXT NOT NULL DEFAULT '',
        media_json          TEXT NOT NULL DEFAULT '',
        attachments_json    TEXT NOT NULL DEFAULT '',
        timestamp           INTEGER NOT NULL DEFAULT 0,
        interaction_id      TEXT NOT NULL DEFAULT '',
        status              TEXT NOT NULL DEFAULT 'completed',
        model_name          TEXT NOT NULL DEFAULT '',
        thought_summary     TEXT NOT NULL DEFAULT '',
        parent_message_id   INTEGER,
        metadata_json       TEXT NOT NULL DEFAULT '',
        input_tokens        INTEGER NOT NULL DEFAULT 0,
        output_tokens       INTEGER NOT NULL DEFAULT 0,
        thought_tokens      INTEGER NOT NULL DEFAULT 0,
        cached_tokens       INTEGER NOT NULL DEFAULT 0,
        tool_tokens         INTEGER NOT NULL DEFAULT 0,
        total_tokens        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session "
    "ON messages(session_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_interaction "
    "ON messages(interaction_id)",
    """
    CREATE TABLE IF NOT EXISTS token_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       INTEGER NOT NULL DEFAULT 0,
        provider_id     TEXT NOT NULL DEFAULT 'gemini',
        model_name      TEXT NOT NULL DEFAULT '',
        session_id      INTEGER,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        thought_tokens  INTEGER NOT NULL DEFAULT 0,
        cached_tokens   INTEGER NOT NULL DEFAULT 0,
        tool_tokens     INTEGER NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stats_ts ON token_stats(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stats_model "
    "ON token_stats(model_name, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stats_session "
    "ON token_stats(session_id, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
        message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        filename        TEXT NOT NULL DEFAULT '',
        mime_type       TEXT NOT NULL DEFAULT '',
        kind            TEXT NOT NULL DEFAULT 'unsupported',
        local_path      TEXT NOT NULL DEFAULT '',
        size_bytes      INTEGER NOT NULL DEFAULT 0,
        sha256          TEXT NOT NULL DEFAULT '',
        remote_uri      TEXT NOT NULL DEFAULT '',
        remote_name     TEXT NOT NULL DEFAULT '',
        remote_expires_at TEXT NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'queued',
        truncated       INTEGER NOT NULL DEFAULT 0,
        error           TEXT NOT NULL DEFAULT '',
        language        TEXT NOT NULL DEFAULT 'text'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attachments_message "
    "ON attachments(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_session "
    "ON attachments(session_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_cache (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id  TEXT NOT NULL DEFAULT 'gemini',
        model_name   TEXT NOT NULL DEFAULT '',
        remote_name  TEXT NOT NULL DEFAULT '',
        source_hash  TEXT NOT NULL DEFAULT '',
        created_at   INTEGER NOT NULL DEFAULT 0,
        expires_at   INTEGER NOT NULL DEFAULT 0,
        token_count  INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'active'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_provider_cache_hash "
    "ON provider_cache(provider_id, source_hash)",
    """
    CREATE TABLE IF NOT EXISTS model_cache (
        provider_id  TEXT NOT NULL DEFAULT 'gemini',
        model_name   TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '',
        fetched_at   INTEGER NOT NULL DEFAULT 0,
        expires_at   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (provider_id, model_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prompt_presets (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        name               TEXT NOT NULL DEFAULT '',
        system_instruction TEXT NOT NULL DEFAULT '',
        builtin            INTEGER NOT NULL DEFAULT 0,
        enabled            INTEGER NOT NULL DEFAULT 1,
        sort_order         INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pinned_context (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
        label       TEXT NOT NULL DEFAULT '',
        text        TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT 'manual',
        enabled     INTEGER NOT NULL DEFAULT 1,
        tokens      INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pinned_session ON pinned_context(session_id)",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL DEFAULT 0,
        note       TEXT NOT NULL DEFAULT ''
    )
    """,
)

# Ordered list of (version, statements). New versions are appended here.
MIGRATIONS: List[Tuple[int, Tuple[str, ...]]] = [
    (1, SCHEMA_V1),
]

BUILTIN_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("Ogólny", "Odpowiadaj zwięźle i konkretnie, w języku użytym przez "
                "użytkownika."),
    ("Kodowanie", "Jesteś doświadczonym inżynierem oprogramowania. Podawaj "
                  "kod w blokach z określeniem języka, unikaj zbędnych "
                  "wyjaśnień, zwracaj uwagę na wydajność i bezpieczeństwo."),
    ("Debugowanie", "Analizuj błąd krok po kroku: przyczyna, dowód, "
                    "minimalna poprawka, sposób weryfikacji."),
    ("Tłumaczenie", "Tłumacz wiernie, zachowując formatowanie i terminologię "
                    "techniczną. Nie dodawaj komentarzy."),
    ("Streszczenie", "Streszczaj w punktach, zachowując liczby, nazwy i "
                     "kolejność faktów."),
    ("Research", "Wymieniaj fakty osobno od wniosków i oznaczaj niepewność."),
)


def get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError, IndexError):
        return 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept bound parameters; version is an int we control.
    conn.execute("PRAGMA user_version = %d" % int(version))


def apply_migrations(conn: sqlite3.Connection,
                     target: int = DB_SCHEMA_VERSION) -> List[int]:
    """Bring the schema up to ``target``. Returns versions applied now."""
    current = get_user_version(conn)
    applied: List[int] = []
    for version, statements in MIGRATIONS:
        if version <= current or version > target:
            continue
        for statement in statements:
            conn.execute(statement)
        set_user_version(conn, version)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version, applied_at, note)"
            " VALUES(?,?,?)", (version, int(time.time() * 1000), "auto"))
        applied.append(version)
    conn.commit()
    seed_presets(conn)
    return applied


def seed_presets(conn: sqlite3.Connection) -> int:
    """Insert built-in prompt presets once; never overwrites user edits."""
    row = conn.execute(
        "SELECT COUNT(*) FROM prompt_presets WHERE builtin = 1").fetchone()
    if row and int(row[0]) >= len(BUILTIN_PRESETS):
        return 0
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM prompt_presets WHERE builtin = 1").fetchall()}
    inserted = 0
    for order, (name, instruction) in enumerate(BUILTIN_PRESETS):
        if name in existing:
            continue
        conn.execute(
            "INSERT INTO prompt_presets(name, system_instruction, builtin,"
            " enabled, sort_order) VALUES(?,?,1,1,?)",
            (name, instruction, order))
        inserted += 1
    if inserted:
        conn.commit()
    return inserted


def migration_history(conn: sqlite3.Connection) -> List[Tuple[int, int, str]]:
    try:
        rows = conn.execute(
            "SELECT version, applied_at, note FROM schema_migrations "
            "ORDER BY version").fetchall()
    except sqlite3.Error:
        return []
    return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]


def table_names(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [str(r[0]) for r in rows]
