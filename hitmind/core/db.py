"""SQLite connection helpers: WAL, busy_timeout, thread-safe access, IMMEDIATE
write transactions.

The engine may be driven from different threads over its lifetime (e.g. an MCP
SDK runs each tool call on a worker thread). A single sqlite3.Connection is not
safe to use concurrently from multiple threads, so we open with
``check_same_thread=False`` and serialize every access with a per-connection
RLock (``conn._hitmind_lock``). Cross-process safety is still SQLite's own
WAL + busy_timeout; the lock only guards in-process concurrent use.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import models
from .schema import init_schema


class _HitMindConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that carries a per-connection RLock so a single
    connection can be shared safely across threads (check_same_thread=False)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hitmind_lock = threading.RLock()


def _retry_locked(fn, attempts: int = 60, delay: float = 0.05):
    """Retry an init step that can transiently hit 'database is locked' when many
    connections open the same file at once (WAL switch + schema DDL)."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            low = str(exc).lower()
            if "locked" not in low and "busy" not in low:
                raise
            last = exc
            time.sleep(delay)
    if last is not None:
        raise last


def prepare_db_path(db_path: str | Path) -> None:
    """Create the database's parent directory and tighten permissions to match
    DSH's storage convention (owner-only: dir 0o700, db file 0o600), since the
    memory DB is sensitive. Idempotent and best-effort: never raises, so a
    read-only or unusual filesystem cannot break opening the DB (the connection
    itself still surfaces a real I/O error). No-op for ``:memory:``.
    """
    if str(db_path) == ":memory:":
        return
    try:
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
    except Exception:
        pass
    try:
        if Path(db_path).exists():
            os.chmod(db_path, 0o600)
    except Exception:
        pass


def connect(db_path: str | Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a connection tuned for multi-process single-writer + in-process
    multi-thread usage."""
    is_mem = str(db_path) == ":memory:"
    if not is_mem:
        prepare_db_path(db_path)
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,
        check_same_thread=False,
        factory=_HitMindConnection,
    )
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)
    if not is_mem:
        # The file is created by sqlite3.connect; tighten it now that it exists.
        try:
            os.chmod(str(db_path), 0o600)
        except Exception:
            pass
        _retry_locked(lambda: cur.execute("PRAGMA journal_mode=WAL"))
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=OFF")
    _retry_locked(lambda: init_schema(conn))
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """BEGIN IMMEDIATE: takes the write lock up front so the whole
    recall->decide->commit sequence is serialized across processes (and across
    threads via the per-connection lock)."""
    lock = getattr(conn, "_hitmind_lock", None)
    with (lock or _null_ctx()):
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:  # locked / busy
            raise models.HitMindError(models.DB_BUSY, "database is busy", str(exc)) from exc
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    lock = getattr(conn, "_hitmind_lock", None)
    with (lock or _null_ctx()):
        cur = conn.cursor()
        yield cur


@contextmanager
def _null_ctx():
    yield
