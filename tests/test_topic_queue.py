"""Topic finalization: DECOUPLED thresholds.

- Global backlog gate: total pre_clustered members >= cluster_min_size.
- Per-cluster eligibility: members >= TOPIC_MIN_CLUSTER (2), pull-based (no write-time
  queueing).
- Growing a FINALIZED cluster inherits its topic (no churn / no re-finalize).
- No starvation: a 9-member cluster finalizes even though cluster_min_size may be 10.
"""

import tempfile

from hitmind.core import topic as T
from hitmind.core.engine import Engine


class FakeEmbedder:
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


class FakeLLM:
    ok = True

    def __init__(self, same=True, fail=False):
        self.same = same
        self.fail = fail
        self.calls = 0

    def finalize(self, members):
        self.calls += 1
        if self.fail:
            return None
        if self.same:
            return {"same_matter": True, "topic": "咖啡：三点后不喝", "corrections": []}
        return {"same_matter": False, "topic": "", "corrections": []}


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=FakeEmbedder(), **kw)


def _cluster_sizes(e):
    out = {}
    for r in e.conn.execute(
        "SELECT cluster_id, cluster_stats, COUNT(*) n FROM memories "
        "WHERE status='active' GROUP BY cluster_id, cluster_stats"
    ):
        out[(r["cluster_id"], r["cluster_stats"])] = r["n"]
    return out


def test_global_backlog_below_gate_does_nothing():
    """Below global backlog (cluster_min_size) -> no LLM, nothing finalized."""
    e = _eng(cluster_min_size=5)          # global gate = 5 pre_clustered members
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")   # 1 cluster, 2 members -> 2 pre_clustered
    e._llm = FakeLLM(same=True)
    st = e.run_topic_task()["data"]
    assert st["skipped"] == "below_global_backlog" and st["backlog"] == 2
    assert e._llm.calls == 0
    e.close()


def test_two_member_clusters_finalize_even_if_cluster_min_size_is_3():
    """Per-cluster line is 2 (not cluster_min_size). Backlog from 2 clusters of 2 = 4."""
    e = _eng(cluster_min_size=3)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    for _ in range(2):
        e.capture("周末把阳台花盆换土", "fact")
    e._llm = FakeLLM(same=True)
    st = e.run_topic_task()["data"]
    assert st["finalized"] == 2                      # both size-2 clusters finalized
    assert all(r["cluster_stats"] == T.CLUSTERED for r in e.conn.execute(
        "SELECT cluster_stats FROM memories"))
    e.close()


def test_nine_member_cluster_is_not_starved():
    """A 9-member cluster finalizes; it is NOT left behind waiting to reach 10."""
    e = _eng(cluster_min_size=5)          # gate satisfied by its own 9 members
    for i in range(9):
        e.capture(f"咖啡三点后不喝了 第{i}次", "fact")
    e._llm = FakeLLM(same=True)
    st = e.run_topic_task()["data"]
    assert st["finalized"] == 1
    rows = e.conn.execute("SELECT topic, cluster_stats FROM memories").fetchall()
    assert all(r["cluster_stats"] == T.CLUSTERED and r["topic"] == "咖啡：三点后不喝" for r in rows)
    e.close()


def test_growing_finalized_cluster_inherits_topic_no_rechurn():
    """After a cluster is finalized, new members INHERIT its topic (no new LLM call)."""
    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = FakeLLM(same=True)
    e.run_topic_task()
    assert e._llm.calls == 1
    # a new similar member joins the SAME (now finalized) cluster
    new_id = e.capture("咖啡三点后真不喝了", "fact")["data"]["id"]
    row = e.conn.execute("SELECT topic, cluster_stats FROM memories WHERE id=?", (new_id,)).fetchone()
    assert row["cluster_stats"] == T.CLUSTERED and row["topic"] == "咖啡：三点后不喝"
    # rerunning the idle task does NOT re-finalize (nothing pre_clustered)
    st = e.run_topic_task()["data"]
    assert st.get("skipped") == "below_global_backlog"
    assert e._llm.calls == 1                          # no extra LLM call
    e.close()


def test_failure_keeps_retry_then_drops_after_max():
    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = FakeLLM(fail=True)
    for _ in range(T.QUEUE_TASK_MAX_FAILS):
        e.run_topic_task()
    rows = e.conn.execute("SELECT status, fails FROM topic_tasks").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "dropped" and rows[0]["fails"] >= T.QUEUE_TASK_MAX_FAILS
    e.close()


def test_not_same_matter_drops_and_unclusters():
    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = FakeLLM(same=False)
    st = e.run_topic_task()["data"]
    assert st["rejected"] >= 1
    mem = e.conn.execute("SELECT id, cluster_id, topic, cluster_stats FROM memories").fetchall()
    # dissolved into honest lone singletons: unclustered, cluster_id == own id, no topic
    assert all(r["topic"] is None and r["cluster_stats"] == T.UNCLUSTERED for r in mem)
    assert all(r["cluster_id"] == r["id"] for r in mem)
    assert e.conn.execute("SELECT status FROM topic_tasks").fetchone()["status"] == "dropped"
    e.close()


def test_no_llm_keeps_everything_intact():
    class NoLLM:
        ok = False

        def finalize(self, m):
            return None

    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = NoLLM()
    st = e.run_topic_task()["data"]
    assert st.get("skipped") == "llm_missing"
    # untouched: still pre_clustered, no task row
    assert all(r["cluster_stats"] == T.PRE_CLUSTERED for r in e.conn.execute(
        "SELECT cluster_stats FROM memories"))
    assert e.conn.execute("SELECT COUNT(*) c FROM topic_tasks").fetchone()["c"] == 0
    e.close()


def test_failure_abandoned_cluster_stays_quiet_until_it_grows():
    """After QUEUE_TASK_MAX_FAILS the cluster is 'dropped'; it must NOT be retried on
    every pass (no churn) until it actually GROWS."""
    e = _eng(cluster_min_size=2)
    for _ in range(2):
        e.capture("下午三点后不喝咖啡", "fact")
    e._llm = FakeLLM(fail=True)
    for _ in range(T.QUEUE_TASK_MAX_FAILS):
        e.run_topic_task()
    row = e.conn.execute("SELECT status, fails FROM topic_tasks").fetchone()
    assert row["status"] == "dropped" and row["fails"] >= T.QUEUE_TASK_MAX_FAILS
    # LLM now healthy, but cluster hasn't grown -> NOT retried
    e._llm = FakeLLM(same=True)
    st = e.run_topic_task()["data"]
    assert st["processed"] == 0
    # a new member joins the SAME cluster -> grows -> eligible again
    e.capture("咖啡三点后真不喝了", "fact")
    st = e.run_topic_task()["data"]
    assert st["finalized"] == 1
    e.close()
