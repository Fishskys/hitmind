"""Config knobs actually take effect (clustering floor, topic cap, LLM toggle, lifecycle)."""

import tempfile
from datetime import UTC, datetime, timedelta

from hitmind.core import lifecycle as L
from hitmind.core.engine import Engine


class FakeEmbedder:
    model = "fake"
    dim = 4
    ok = True

    def _v(self, t):
        if "咖啡" in t:
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    def embed_documents(self, texts, titles=None):
        return [self._v(t) for t in texts]

    def embed_query(self, text):
        return self._v(text)


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=FakeEmbedder(), **kw)


def test_cluster_cos_floor_knob_controls_joining():
    # very high floor -> even identical-vector memories stay apart
    e = _eng(cluster_cos_floor=1.01)
    a = e.capture("咖啡记录甲", "fact")["data"]["id"]
    b = e.capture("咖啡记录乙", "fact")["data"]["id"]
    ca = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (a,)).fetchone()["cluster_id"]
    cb = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (b,)).fetchone()["cluster_id"]
    assert ca != cb  # floor too high to merge
    e.close()
    # low floor -> they merge
    e2 = _eng(cluster_cos_floor=0.5)
    a2 = e2.capture("咖啡记录甲", "fact")["data"]["id"]
    b2 = e2.capture("咖啡记录乙", "fact")["data"]["id"]
    c2a = e2.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (a2,)).fetchone()["cluster_id"]
    c2b = e2.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (b2,)).fetchone()["cluster_id"]
    assert c2a == c2b
    e2.close()


def test_topic_max_members_caps_cluster():
    e = _eng(cluster_cos_floor=0.5, topic_max_members=2)
    ids = [e.capture(f"咖啡记录{i}", "fact")["data"]["id"] for i in range(3)]
    sizes = {}
    for i in ids:
        cid = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (i,)).fetchone()["cluster_id"]
        sizes[cid] = sizes.get(cid, 0) + 1
    assert max(sizes.values()) <= 2  # never exceeds the configured cap
    assert len(sizes) >= 2
    e.close()


def test_consolidate_llm_toggle_disables_run_topic_task():
    e = _eng(cluster_min_size=2, consolidate_llm_enabled=False)
    for _ in range(2):
        e.capture("咖啡记录", "fact")
    assert e.run_topic_task()["data"].get("skipped") == "llm_disabled"
    e.close()


def test_lifecycle_ttl_and_half_life_knobs():
    e = _eng(ttl_days={"persona": None, "fact": 1, "event": 2, "procedure": None},
             half_life_days={"persona": 100, "fact": 10, "event": 1, "procedure": 5})
    vu = L.default_valid_until("fact", None, e.ttl_days)
    assert vu is not None  # fact now has a 1-day TTL (default is None)
    assert L.default_valid_until("persona", None, e.ttl_days) is None
    # half-life: after 10 days a fact with half-life 10 -> freshness ~0.5
    past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    f = L.freshness(past, "fact", half_life_days=e.half_life_days)
    assert abs(f - 0.5) < 0.02
    e.close()


def test_dense_floor_drops_weak_vectors():
    # A "咖啡" query maps to vec [1,0,0,0]; non-咖啡 memories map to [0,1,0,0] (cosine 0).
    # High dense_floor -> weak (cosine-0) neighbor is dropped from dense candidates.
    e = _eng(dense_floor=0.9)
    e.capture("咖啡记录", "fact")
    e.capture("别的东西", "fact")
    assert e.retrieval_config["dense_floor"] == 0.9
    r = e.recall("咖啡")["data"]["results"]
    assert r, "the matching memory must still be returned"
    assert all(m.get("scores", {}).get("dense") is None or m["scores"]["dense"] >= 0.9 for m in r)
    e.close()
    # Low floor -> weak neighbor may enter via dense (still governed by BM25/other paths).
    e2 = _eng(dense_floor=0.0)
    e2.capture("咖啡记录", "fact")
    e2.capture("别的东西", "fact")
    assert e2.retrieval_config["dense_floor"] == 0.0
    e2.close()


def test_version_keep_limits_history():
    e = _eng(version_keep=2)
    mid = e.remember("版本记忆 v1", "fact")["data"]["id"]
    cur = mid
    for i in range(2, 9):  # 7 updates
        cur = e.update(cur, content=f"版本记忆 v{i}")["data"]["id"]
    hist = e.get(cur, history=True)["data"]["versions"]
    non_deleted = [v for v in hist if v["status"] != "deleted"]
    assert len(non_deleted) <= 1 + 2  # active + up to version_keep old versions
    e.close()
