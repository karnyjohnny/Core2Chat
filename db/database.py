"""SQLite connection management.

Threading model (explicit, per specification §61):

* one connection per thread, created lazily and owned by that thread;
* all writes are serialised through a process-wide lock, which is safe with
  WAL and avoids ``database is locked`` under concurrent workers;
* reads run without the write lock (WAL allows concurrent readers);
* connections are tracked so :meth:`Database.close_all` can release them at
  shutdown - no dangling file handles, no lost writes.
"""

import os
import sqlite3
import threading
import time
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from core.constants import DB_PAGE_SIZE, DEFAULT_BUSY_TIMEOUT_MS
from core.logging_setup import get_logger
from db import migrations

log = get_logger("db")

PRAGMAS_ON_CONNECT: Tuple[Tuple[str, Any], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),     # safe with WAL, far cheaper than FULL
    ("foreign_keys", "ON"),
    ("busy_timeout", DEFAULT_BUSY_TIMEOUT_MS),
    ("temp_store", "MEMORY"),
    ("cache_size", -2048),         # ~2 MB page cache per connection
    ("mmap_size", 0),              # keep RSS low on legacy hardware
)


class DatabaseError(RuntimeError):
    """Raised for unrecoverable persistence failures."""


class Database(object):
    """Owns connections and executes statements on behalf of repositories."""

    def __init__(self, path: str, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self.path = path
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._conn_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._closed = False
        self.journal_mode = ""
        self.schema_version = 0
        self.applied_migrations: List[int] = []

    # ------------------------------------------------------------- lifecycle
    def initialize(self) -> None:
        """Create the file if needed, apply pragmas and run migrations."""
        if self.path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
        conn = self.connection()
        self.journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        try:
            self.applied_migrations = migrations.apply_migrations(conn)
        except sqlite3.Error as exc:
            raise DatabaseError("Migracja bazy danych nie powiodła się: %s" % exc)
        self.schema_version = migrations.get_user_version(conn)
        if self.applied_migrations:
            log.info("db.migrations_applied versions=%s", self.applied_migrations)
        log.info("db.initialized path=%s journal=%s schema=%s",
                 self.path, self.journal_mode, self.schema_version)

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self._closed:
            raise DatabaseError("Baza danych została zamknięta.")
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000.0,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        for name, value in PRAGMAS_ON_CONNECT:
            if name == "busy_timeout":
                value = self.busy_timeout_ms
            try:
                conn.execute("PRAGMA %s=%s" % (name, value)
                             if isinstance(value, str) else
                             "PRAGMA %s=%d" % (name, int(value)))
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                log.warning("db.pragma_failed name=%s err=%s", name, exc)
        with self._conn_lock:
            self._connections.append(conn)
        setattr(self._local, "conn", conn)
        return conn

    def close_thread_connection(self) -> None:
        """Release the calling thread's connection (used by workers on exit)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        setattr(self._local, "conn", None)
        with self._conn_lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def close_all(self) -> None:
        with self._conn_lock:
            connections = list(self._connections)
            self._connections.clear()
            self._closed = True
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        setattr(self._local, "conn", None)
        log.info("db.closed path=%s", self.path)

    # ------------------------------------------------------------ execution
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        try:
            return self.connection().execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            log.error("db.query_failed err=%s sql=%.80s", exc, sql)
            raise DatabaseError("Błąd odczytu bazy danych: %s" % exc)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: int = 0) -> int:
        row = self.query_one(sql, params)
        if not row:
            return default
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return default

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one write statement; returns ``lastrowid`` (0 when N/A)."""
        with self._write_lock:
            conn = self.connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(sql, tuple(params))
                conn.execute("COMMIT")
                return int(cursor.lastrowid or 0)
            except sqlite3.Error as exc:
                self._rollback(conn)
                log.error("db.execute_failed err=%s sql=%.80s", exc, sql)
                raise DatabaseError("Błąd zapisu do bazy danych: %s" % exc)

    def execute_many(self, statements: Sequence[Tuple[str, Sequence[Any]]]) -> None:
        """Run several writes in a single transaction (atomic)."""
        if not statements:
            return
        with self._write_lock:
            conn = self.connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for sql, params in statements:
                    conn.execute(sql, tuple(params))
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                log.error("db.execute_many_failed err=%s", exc)
                raise DatabaseError("Błąd zapisu do bazy danych: %s" % exc)

    def transaction(self) -> "_Transaction":
        return _Transaction(self)

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------- maintenance
    def integrity_check(self) -> str:
        row = self.query_one("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"

    def vacuum(self) -> None:
        with self._write_lock:
            try:
                self.connection().execute("VACUUM")
            except sqlite3.Error as exc:
                raise DatabaseError("VACUUM nie powiódł się: %s" % exc)

    def checkpoint(self) -> None:
        """Force a WAL checkpoint (used at shutdown to keep the DB compact)."""
        try:
            self.connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log.warning("db.checkpoint_failed err=%s", exc)

    def size_bytes(self) -> int:
        try:
            if self.path == ":memory:":
                return 0
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def stats(self) -> dict:
        return {
            "path": self.path,
            "journal_mode": self.journal_mode,
            "schema_version": self.schema_version,
            "integrity": self.integrity_check(),
            "size_bytes": self.size_bytes(),
            "sessions": self.scalar("SELECT COUNT(*) FROM sessions"),
            "messages": self.scalar("SELECT COUNT(*) FROM messages"),
            "token_stats_rows": self.scalar("SELECT COUNT(*) FROM token_stats"),
            "open_connections": len(self._connections),
        }


class _Transaction(object):
    """Context manager serialising a group of writes."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        self._db._write_lock.acquire()
        self._conn = self._db.connection()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self._db._write_lock.release()
            raise DatabaseError("Nie można rozpocząć transakcji: %s" % exc)
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        conn = self._conn
        assert conn is not None
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                Database._rollback(conn)
        except sqlite3.Error:
            Database._rollback(conn)
        finally:
            self._db._write_lock.release()
        return False


def now_ms() -> int:
    return int(time.time() * 1000)


def iter_rows(db: Database, sql: str, params: Sequence[Any] = ()) -> Iterator[sqlite3.Row]:
    for row in db.query(sql, params):
        yield row
