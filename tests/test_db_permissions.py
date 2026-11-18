"""Owner-only filesystem permissions for the memory DB.

The memory DB is sensitive; per DSH's storage convention the parent dir must be
0o700 and the DB file 0o600 (owner-only), regardless of the process umask. This
holds for both the shared ``connect()`` path and the daemon's independent
``DaemonTurnStore`` connection.
"""

from __future__ import annotations

import os
import stat

from hitmind.core.db import connect, prepare_db_path


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_connect_new_db_is_owner_only(tmp_path):
    db = tmp_path / "nested" / "dir" / "hitmind.db"
    old = os.umask(0o022)  # permissive umask must not widen our dirs/files
    try:
        connect(str(db)).close()
    finally:
        os.umask(old)
    assert db.exists()
    assert _mode(db.parent) == 0o700
    assert _mode(db) == 0o600


def test_connect_tightens_existing_loose_permissions(tmp_path):
    db = tmp_path / "d" / "hitmind.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    os.chmod(db.parent, 0o755)
    os.chmod(db, 0o644)
    connect(str(db)).close()
    assert _mode(db.parent) == 0o700
    assert _mode(db) == 0o600


def test_prepare_db_path_best_effort_and_memory():
    # :memory: must be a no-op (no filesystem, no raise).
    prepare_db_path(":memory:")  # must not raise
