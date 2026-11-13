"""Core-level dedup / TTL sweep tests (host-agnostic gates live in Core)."""

import tempfile

from hitmind.core.engine import Engine


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", **kw)


def test_capture_skip_if_duplicate_noop():
    e = _eng()
    a = e.capture("同一句足够长的记忆内容", "fact", skip_if_duplicate=True)["data"]
    b = e.capture("同一句足够长的记忆内容", "fact", skip_if_duplicate=True)["data"]
    assert a["id"] == b["id"] and b.get("duplicate") is True
    assert e.list(limit=50)["data"]["total"] == 1
    e.close()


def test_capture_default_keeps_verbatim_duplicates():
    e = _eng()
    e.capture("同一句足够长的记忆内容", "fact")
    e.capture("同一句足够长的记忆内容", "fact")
    # benchmark/corpus ingestion must stay verbatim (every turn kept)
    assert e.list(limit=50)["data"]["total"] == 2
    e.close()


def test_raw_ttl_sweep_expires_old_pending_rows():
    e = _eng(raw_ttl_days=30)
    # old auto-captured row (pending_since set from created_at) -> expired on recall
    old = e.capture("很久以前的一条自动捕获记忆", "fact", source="hook",
                    created_at="2020-01-01T00:00:00+00:00")["data"]["id"]
    # manual remember has no pending_since -> never swept by raw TTL
    man = e.remember("手工写入的长期事实记忆", "fact", source="manual")["data"]["id"]
    e.recall("随便问一句", top_k=5)  # triggers _expire_due
    st = {r["id"]: r["status"] for r in e.list(limit=50)["data"]["items"]}
    assert st[old] != "active"
    assert st[man] == "active"
    e.close()
