"""Read-only adapter over Hermes' own session transcript (``state.db``).

NOT a memory layer. It is an external, optional, best-effort source: HitMind keeps a
read-only handle to the host ``state.db`` purely to read the Hermes *compaction summary*
at compaction time. The raw conversation body is never copied into HitMind, and HitMind
works fine if this source is unavailable (episodes/memories already stand alone).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_state_db(hermes_home: str | os.PathLike | None = None) -> Path:
    home = Path(hermes_home) if hermes_home else Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "state.db"


class StateDBSource:
    """Read-only handle over a Hermes ``state.db``. Never writes to the host DB."""

    def __init__(self, db_path: str | os.PathLike):
        self.db_path = str(db_path)
        self.ok = os.path.exists(self.db_path)
        self._conn = None

    def _connect(self):
        if self._conn is None:
            # immutable/ro: never lock or modify the host DB.
            self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _rows(self, sql: str, params: list) -> list[dict]:
        if not self.ok:
            return []
        try:
            cur = self._connect().execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def latest_summary(self, session_id: str) -> str | None:
        """The newest Hermes cumulative compaction summary for a session (prefer the
        live/active one). Returns the summary TEXT — this is what we store as episode."""
        rows = self._rows(
            "SELECT content, active FROM messages WHERE session_id=? "
            "AND (content LIKE '[CONTEXT COMPACTION%' OR content LIKE '[PRIOR CONTEXT%' "
            "OR content LIKE '[CONTEXT SUMMARY%') "
            "ORDER BY active DESC, id DESC LIMIT 1",
            [session_id],
        )
        if rows and (rows[0].get("content") or "").strip():
            return rows[0]["content"]
        return None

    def rows_after(self, session_id: str, after_id: int = 0, limit: int = 400) -> list[dict]:
        """Real conversation messages newer than rowid ``after_id`` for a session,
        ordered by id, EXCLUDING compaction-summary rows (those are not chat content).

        Used by the chat batch processor: it reads ``[cursor, ...]`` so nothing is
        skipped and nothing is processed twice (cursor only advances on success).
        """
        return self._rows(
            "SELECT id, role, content, timestamp FROM messages "
            "WHERE session_id=? AND id>? AND active=1 "
            "AND content NOT LIKE '[CONTEXT COMPACTION%' AND content NOT LIKE '[PRIOR CONTEXT%' "
            "AND content NOT LIKE '[CONTEXT SUMMARY%' "
            "ORDER BY id LIMIT ?",
            [session_id, after_id, limit],
        )

    def count_after(self, session_id: str, after_id: int = 0) -> int:
        """How many real (non-summary) messages are newer than ``after_id``."""
        rows = self._rows(
            "SELECT COUNT(*) c FROM messages WHERE session_id=? AND id>? AND active=1 "
            "AND content NOT LIKE '[CONTEXT COMPACTION%' AND content NOT LIKE '[PRIOR CONTEXT%' "
            "AND content NOT LIKE '[CONTEXT SUMMARY%'",
            [session_id, after_id],
        )
        return int(rows[0]["c"]) if rows else 0
