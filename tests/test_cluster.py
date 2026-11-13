"""Cluster lifecycle: pre-cluster (cosine, capped, no collapse), LLM finalize,
states, and recall expansion that only uses `clustered` topics and dedupes."""

import tempfile

from hitmind.core import topic as T
from hitmind.core.engine import Engine


class FakeEmbedder:
    """Deterministic vector by keyword; ok=True so the cosine path is exercised."""
    model = "fake"
    dim = 4
    ok = True

    def _vec(self, text):
        if "咖啡" in text:
            return [1.0, 0.0, 0.0, 0.0]
        if "花盆" in text:
            return [0.0, 1.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0]

    def embed_documents(self, texts, titles=None):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=FakeEmbedder(), **kw)


def test_similar_precluster_together_different_apart():
    e = _eng()
    a = e.capture("下午三点后不喝咖啡了", "fact")["data"]["id"]
    # a lone memory is honestly UNCLUSTERED (not pre_clustered) — cluster_id = itself
    ra = e.conn.execute("SELECT cluster_id, cluster_stats FROM memories WHERE id=?", (a,)).fetchone()
    assert ra["cluster_stats"] == T.UNCLUSTERED and ra["cluster_id"] == a
    b = e.capture("咖啡三点后就不碰了怕睡不着", "fact")["data"]["id"]
    c = e.capture("周末把阳台旧花盆全换了土", "fact")["data"]["id"]
    ga = e.conn.execute("SELECT cluster_id, cluster_stats, topic FROM memories WHERE id=?", (a,)).fetchone()
    gb = e.conn.execute("SELECT cluster_id, cluster_stats, topic FROM memories WHERE id=?", (b,)).fetchone()
    gc = e.conn.execute("SELECT cluster_id, cluster_stats, topic FROM memories WHERE id=?", (c,)).fetchone()
    assert ga["cluster_id"] == gb["cluster_id"]           # similar -> same cluster
    assert ga["cluster_id"] != gc["cluster_id"]           # unrelated -> different
    # a real >=2 group is pre_clustered (the lone seed got promoted when b joined)
    assert ga["cluster_stats"] == T.PRE_CLUSTERED and gb["cluster_stats"] == T.PRE_CLUSTERED
    assert ga["topic"] is None                             # topic empty until LLM finalize
    # the unrelated lone memory stays honestly unclustered
    assert gc["cluster_stats"] == T.UNCLUSTERED and gc["cluster_id"] == c
    e.close()


def test_precluster_never_collapses_whole_corpus():
    e = _eng()
    for i in range(30):
        e.capture(f"同一句关于咖啡的记录 第{i}次", "fact")   # all identical vector
    clusters = {}
    for r in e.conn.execute("SELECT cluster_id FROM memories"):
        clusters[r["cluster_id"]] = clusters.get(r["cluster_id"], 0) + 1
    assert max(clusters.values()) <= e.topic_max_members  # capped, never one giant cluster
    assert len(clusters) >= 2
    e.close()


def test_finalize_sets_topic_and_state():
    e = _eng()
    ids = [e.capture("冻干那袋的事", "fact")["data"]["id"] for _ in range(3)]
    cid = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (ids[0],)).fetchone()["cluster_id"]
    assert e.finalize_cluster(cid, "冻干存量")["data"]["members"] == 3
    rows = e.conn.execute("SELECT topic, cluster_stats FROM memories").fetchall()
    assert all(r["topic"] == "冻干存量" and r["cluster_stats"] == T.CLUSTERED for r in rows)
    e.close()


def test_recall_expands_only_clustered_and_dedupes():
    e = Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=None)  # NullEmbedder -> BM25
    ids = [e.capture(f"冻干那袋的事 说法{i}", "fact")["data"]["id"] for i in range(3)]
    # force one cluster, then finalize so items become `clustered`
    e.conn.execute("UPDATE memories SET cluster_id='C1'")
    e.conn.commit()
    e.finalize_cluster("C1", "冻干存量")
    res = e.recall("冻干", top_k=10)["data"]["results"]
    got = [m["id"] for m in res]
    assert len(got) == len(set(got)), "no duplicate memories injected"
    assert set(ids).issubset(set(got)), "all same-topic members surfaced once"
    e.close()


def test_unclustered_topic_not_expanded():
    e = Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=None)
    e.capture("冻干那袋的事", "fact")
    # topic left None + not clustered -> nothing to expand, returns just the hit
    res = e.recall("冻干", top_k=10)["data"]["results"]
    assert len(res) == 1
    e.close()


def test_consolidate_reports_qualifying_clusters():
    e = _eng(cluster_min_size=2, consolidate_min_working=1)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    d = e.consolidate(dry_run=False)["data"]
    assert d["llm_enabled"] is True and d["qualifying_clusters"] >= 1
    e.close()


class _FakeLLM:
    def __init__(self, verdict):
        self.ok = True
        self._v = verdict

    def finalize(self, members):
        return self._v


def test_run_finalize_pass_writes_topic_on_same_matter():
    e = _eng(cluster_min_size=2)
    for _ in range(3):
        e.capture("下午三点后不喝咖啡了怕睡不着", "fact")
    e._llm = _FakeLLM({"same_matter": True, "topic": "咖啡习惯：三点后不喝", "corrections": []})
    st = e.run_finalize_pass()["data"]
    assert st["finalized"] >= 1
    rows = e.conn.execute("SELECT topic, cluster_stats FROM memories").fetchall()
    assert all(r["topic"] == "咖啡习惯：三点后不喝" and r["cluster_stats"] == T.CLUSTERED for r in rows)
    e.close()


def test_run_finalize_pass_rejects_when_not_same_matter():
    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = _FakeLLM({"same_matter": False, "topic": "", "corrections": []})
    st = e.run_finalize_pass()["data"]
    assert st["rejected"] >= 1
    rows = e.conn.execute("SELECT topic, cluster_stats FROM memories").fetchall()
    assert all(r["topic"] is None for r in rows)
    e.close()


def test_run_finalize_pass_without_llm_is_noop():
    class _NoLLM:
        ok = False

        def finalize(self, members):
            return None

    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = _NoLLM()  # explicit no-LLM host
    st = e.run_finalize_pass()["data"]
    assert st.get("skipped") == "llm_missing" and st.get("finalized", 0) == 0
    e.close()


def test_finalize_applies_llm_corrections():
    """LLM says members[1] and members[2] correct members[0] (old fact 4 -> 10):
    the old one is superseded (no delete) and the newer ones get a [更正] note."""
    e = _eng(cluster_min_size=3)
    id_old = e.capture("默认是 4", "fact")["data"]["id"]
    id_new = e.capture("最新：默认提升到 10", "fact")["data"]["id"]
    id_extra = e.capture("再确认：默认 10 没错", "fact")["data"]["id"]
    cid = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (id_old,)).fetchone()["cluster_id"]
    for i in (id_new, id_extra):
        assert e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (i,)).fetchone()["cluster_id"] == cid
    # member order in cached_members == rowid order == [old, new, extra]
    e._llm = _FakeLLM({"same_matter": True, "topic": "默认阈值", "corrections": [
        {"index": 1, "corrects": 0, "note": "默认由 4 改为 10"},
        {"index": 2, "corrects": 0, "note": "再次确认改成 10"},
    ]})
    st = e.run_finalize_pass()["data"]
    assert st["finalized"] >= 1
    rows = {r["id"]: dict(r) for r in e.conn.execute(
        "SELECT id,status,content,topic,cluster_stats FROM memories")}
    assert rows[id_old]["status"] == "superseded"             # 旧说法被更正，不删除
    assert rows[id_new]["status"] == "active" and rows[id_extra]["status"] == "active"
    assert rows[id_new]["content"].startswith("[更正]")        # 新条带更正注记（可被召回）
    assert rows[id_extra]["content"].startswith("[更正]")
    assert rows[id_new]["topic"] == "默认阈值" and rows[id_new]["cluster_stats"] == T.CLUSTERED
    e.close()


def test_finalize_without_corrections_leaves_members_active():
    """No corrections array -> nothing superseded, all members stay active+clustered."""
    e = _eng(cluster_min_size=2)
    ids = [e.capture("冻干那袋的事", "fact")["data"]["id"] for _ in range(2)]
    cid = e.conn.execute("SELECT cluster_id FROM memories WHERE id=?", (ids[0],)).fetchone()["cluster_id"]
    res = e.finalize_cluster(cid, "冻干存量", members=[{"id": i, "content": "x"} for i in ids],
                             corrections=[])
    assert res["data"]["corrections_applied"] == 0
    st = {r["id"]: r["status"] for r in e.conn.execute("SELECT id,status FROM memories")}
    assert all(st[i] == "active" for i in ids)
    e.close()
