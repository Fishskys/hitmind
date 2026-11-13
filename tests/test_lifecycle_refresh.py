"""Recall/update refresh the decay clock AND (for TTL kinds) the expiry."""

import tempfile
from datetime import UTC, datetime, timedelta

from hitmind.core import lifecycle
from hitmind.core.engine import Engine


def _eng():
    return Engine(str(tempfile.mkdtemp()) + "/d.db")


def _iso(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_freshness_uses_max_of_updated_and_last_recalled():
    now = datetime.now(UTC)
    old = (now - timedelta(days=200)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    # last_recalled newer than updated -> uses last_recalled -> fresh
    f = lifecycle.freshness(old, "event", last_recalled_at=recent)
    assert f > 0.9
    # no last_recalled -> old updated -> stale
    f2 = lifecycle.freshness(old, "event")
    assert f2 < 0.2


def test_recall_refreshes_last_recalled_and_expiry():
    e = _eng()
    mid = e.remember("看了一次医生", "event")["data"]["id"]
    # force stale timestamps
    e.conn.execute("UPDATE memories SET updated_at=?, valid_until=? WHERE id=?",
                   (_iso(80), _iso(-10), mid))  # updated 80d ago; valid_until ~ now+10d
    e.conn.commit()
    before = e.conn.execute("SELECT valid_until, recalled_times FROM memories WHERE id=?", (mid,)).fetchone()

    e.recall("看医生", top_k=3)  # first recall -> writeback refreshes
    row = e.conn.execute("SELECT last_recalled_at, recalled_times, valid_until FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["last_recalled_at"] is not None
    assert row["recalled_times"] == 1
    # valid_until pushed ~ now + 90d (event TTL)
    vu = datetime.fromisoformat(row["valid_until"])
    assert (vu - datetime.now(UTC)).days >= 89
    assert row["valid_until"] != before["valid_until"]

    # second recall now computes freshness off last_recalled_at -> fresh
    r = e.recall("看医生", top_k=3)
    hit = next(x for x in r["data"]["results"] if x["id"] == mid)
    assert hit["scores"]["freshness"] > 0.9
    e.close()


def test_recall_fact_keeps_no_expiry():
    e = _eng()
    mid = e.remember("喜欢黑咖啡", "fact")["data"]["id"]
    e.recall("黑咖啡", top_k=3)
    row = e.conn.execute("SELECT last_recalled_at, valid_until FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["last_recalled_at"] is not None
    assert row["valid_until"] is None  # fact has no default TTL
    e.close()


def test_update_refreshes_expiry():
    e = _eng()
    mid = e.remember("旧日程", "event")["data"]["id"]
    e.conn.execute("UPDATE memories SET valid_until=? WHERE id=?", (_iso(-5), mid))  # nearly expired? set old
    e.conn.commit()
    new = e.update(mid, content="新日程")["data"]["id"]
    row = e.conn.execute("SELECT valid_until, updated_at FROM memories WHERE id=?", (new,)).fetchone()
    vu = datetime.fromisoformat(row["valid_until"])
    assert (vu - datetime.now(UTC)).days >= 89  # refreshed to now+90d
    e.close()
