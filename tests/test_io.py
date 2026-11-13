"""Per-kind importance + JSON export/import (validation, merge/replace)."""

import tempfile

from hitmind.core.engine import Engine


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=None, **kw)


def test_kind_importance_defaults_applied():
    e = _eng(kind_importance={"persona": 0.9, "fact": 0.2})
    p = e.remember("用户偏好深色主题", "persona")["data"]["id"]
    f = e.remember("系统默认端口8080", "fact")["data"]["id"]
    ev = e.remember("2026年搬家", "event")["data"]["id"]
    def imp(i):
        return e.conn.execute("SELECT importance FROM memories WHERE id=?", (i,)).fetchone()["importance"]
    assert abs(imp(p) - 0.9) < 1e-9
    assert abs(imp(f) - 0.2) < 1e-9
    assert abs(imp(ev) - 1.0) < 1e-9     # default kind importance is now 1.0
    # explicit importance overrides the kind default
    x = e.remember("显式重要性", "fact", importance=0.77)["data"]["id"]
    assert abs(imp(x) - 0.77) < 1e-9
    e.close()


def test_export_import_roundtrip_merge():
    e = _eng()
    e.remember("记忆甲", "fact", title="甲")
    e.remember("记忆乙", "persona", title="乙")
    dump = e.export_memories()["data"]
    assert dump["count"] == 2
    # import the same dump into a fresh DB -> all inserted
    e2 = _eng()
    r = e2.import_memories(dump["memories"], mode="merge")["data"]
    assert r["imported"] == 2 and r["skipped_existing"] == 0
    assert e2.list(limit=10)["data"]["total"] == 2
    # re-import into the same DB -> all skipped (idempotent by id)
    r2 = e2.import_memories(dump["memories"], mode="merge")["data"]
    assert r2["imported"] == 0 and r2["skipped_existing"] == 2
    e.close()
    e2.close()


def test_import_validation_all_or_nothing():
    e = _eng()
    bad = [
        {"kind": "fact", "content": "好的一条"},
        {"kind": "bogus", "content": "坏 kind"},
    ]
    r = e.import_memories(bad, mode="merge")
    assert r["ok"] is False
    assert r["error"]["code"] == "INVALID_ARGUMENT"
    # nothing written (all-or-nothing)
    assert e.list(limit=10)["data"]["total"] == 0
    e.close()


def test_import_replace_clears_existing():
    e = _eng()
    e.remember("旧记忆保留者", "fact")
    assert e.list(limit=10)["data"]["total"] == 1
    r = e.import_memories([{"kind": "fact", "content": "替换后的唯一一条"}], mode="replace")["data"]
    assert r["imported"] == 1
    items = e.list(limit=10)["data"]["items"]
    assert len(items) == 1 and items[0]["content"] == "替换后的唯一一条"
    e.close()


def test_recall_final_blends_importance():
    # final = 0.8*rel_norm + 0.1*importance + 0.1*freshness
    e = _eng(kind_importance={"fact": 0.4})
    e.remember("重要性参与打分的测试记忆", "fact")
    m = e.recall("重要性参与打分的测试记忆")["data"]["results"][0]
    s = m["scores"]
    expect = 0.8 * s["rel_norm"] + 0.1 * 0.4 + 0.1 * s["freshness"]
    assert abs(s["final"] - expect) < 1e-3
    e.close()
