"""Async memory review: write-time versioned/inactive both defer to `inactive`; the idle
review task (LLM) decides same-matter + update -> new version, else activate."""

import tempfile

from hitmind.core.engine import Engine


class FakeEmbedder:
    model = "fake"
    dim = 4
    ok = True

    def _v(self, t):
        return [1.0, 0.0, 0.0, 0.0]

    def embed_documents(self, texts, titles=None):
        return [self._v(t) for t in texts]

    def embed_query(self, text):
        return self._v(text)


class ReviewLLM:
    ok = True

    def __init__(self, same=True, update=True, fail=False):
        self.same, self.update, self.fail = same, update, fail
        self.calls = 0

    def review_verdict(self, candidate, prior):
        self.calls += 1
        if self.fail:
            return None
        return {"same": self.same, "update": self.update}


def _eng(**kw):
    kw.setdefault("review_trigger_min", 0)
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=FakeEmbedder(), **kw)


def test_near_duplicate_remember_now_inactive_with_conflict_of():
    e = _eng()
    a = e.remember("咖啡三点后不喝", "fact")["data"]
    assert a["action"] == "created" and a["status"] == "active"
    # Dice ~0.83 -> old behaviour "versioned"; now deferred: stored inactive
    b = e.remember("咖啡三点后不碰", "fact")["data"]
    assert b["action"] == "inactive" and b["status"] == "inactive"
    assert b["conflict_of"] == a["id"]            # points at the prior candidate
    # prior untouched (no supersede at write time)
    assert e.conn.execute("SELECT status FROM memories WHERE id=?", (a["id"],)).fetchone()["status"] == "active"
    e.close()


def test_review_task_below_threshold_skips():
    e = _eng(review_trigger_min=3)
    e.remember("咖啡三点后不喝", "fact")
    e.remember("咖啡三点后不碰", "fact")   # 1 inactive, <= 3
    e._llm = ReviewLLM()
    assert e.run_review_task()["data"].get("skipped") == "below_threshold"
    e.close()


def test_review_same_and_update_creates_new_version():
    e = _eng()
    a = e.remember("系统默认阈值参数是4", "fact")["data"]["id"]
    b = e.remember("系统默认阈值参数是8", "fact")["data"]   # ~0.89 dice -> inactive, conflict_of=a
    assert b["status"] == "inactive"
    e._llm = ReviewLLM(same=True, update=True)
    st = e.run_review_task()["data"]
    assert st["versioned"] >= 1
    ra = e.conn.execute("SELECT status FROM memories WHERE id=?", (a,)).fetchone()["status"]
    rb = e.conn.execute("SELECT status, root_id, version, conflict_of FROM memories WHERE id=?", (b["id"],)).fetchone()
    assert ra == "superseded"                     # prior superseded by review
    assert rb["status"] == "active" and rb["version"] == 2 and rb["conflict_of"] is None
    e.close()


def test_review_same_no_update_just_activates():
    e = _eng()
    a = e.remember("咖啡三点后不喝", "fact")["data"]["id"]
    b = e.remember("咖啡三点后不碰", "fact")["data"]
    e._llm = ReviewLLM(same=True, update=False)   # corroboration
    st = e.run_review_task()["data"]
    assert st["activated"] >= 1
    assert e.conn.execute("SELECT status FROM memories WHERE id=?", (a,)).fetchone()["status"] == "active"
    assert e.conn.execute("SELECT status FROM memories WHERE id=?", (b["id"],)).fetchone()["status"] == "active"
    e.close()


def test_review_not_same_activates_independently():
    e = _eng()
    e.remember("咖啡三点后不喝", "fact")
    b = e.remember("咖啡三点后不碰", "fact")["data"]
    e._llm = ReviewLLM(same=False, update=False)
    st = e.run_review_task()["data"]
    assert st["activated"] >= 1
    assert e.conn.execute("SELECT status FROM memories WHERE id=?", (b["id"],)).fetchone()["status"] == "active"
    e.close()


def test_review_failure_keeps_inactive():
    e = _eng()
    e.remember("咖啡三点后不喝", "fact")
    b = e.remember("咖啡三点后不碰", "fact")["data"]
    e._llm = ReviewLLM(fail=True)
    st = e.run_review_task()["data"]
    assert st["failed"] >= 1
    assert e.conn.execute("SELECT status FROM memories WHERE id=?", (b["id"],)).fetchone()["status"] == "inactive"
    e.close()
