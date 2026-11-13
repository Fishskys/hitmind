"""Compaction-driven capture: ONE episode per session (revisioned); delta extraction."""

import tempfile

from hitmind.core.engine import Engine


class _FakeLLM:
    ok = True
    def __init__(self, out):
        self._out = out
        self.calls = []
    def extract_from_summary(self, new_summary, prev_summary):
        self.calls.append((new_summary, prev_summary))
        return {"memories": self._out}


S1 = "【摘要1】讨论了文档拆分"
S2 = "【摘要2】讨论了文档拆分；决定采用分层记忆架构"


def _eng():
    return Engine(str(tempfile.mkdtemp()) + "/d.db")


def test_first_compaction_extracts_all():
    e = _eng()
    e._llm = _FakeLLM([{"kind": "fact", "content": "决定分模块写文档"}])
    r = e.ingest_compaction("sess-A", S1)["data"]
    assert r["first"] is True and r["memories"] == 1 and r["revision"] == 1
    eps = e.list_episodes(session_id="sess-A")["data"]["items"]
    assert len(eps) == 1 and eps[0]["summary"] == S1 and eps[0]["session_id"] == "sess-A"
    assert e._llm.calls[-1][1] is None  # first -> no previous summary
    e.close()


def test_second_compaction_same_row_bumps_revision_and_deltas():
    e = _eng()
    e._llm = _FakeLLM([{"kind": "fact", "content": "采用分层记忆架构"}])
    e.ingest_compaction("sess-A", S1)
    r = e.ingest_compaction("sess-A", S2)["data"]
    assert r["first"] is False and r["revision"] == 2
    assert e._llm.calls[-1][1] == S1  # delta compares against previous summary
    # STILL one episode row for the session, summary replaced in place
    eps = e.list_episodes(session_id="sess-A")["data"]["items"]
    assert len(eps) == 1 and eps[0]["summary"] == S2 and eps[0]["revision"] == 2
    e.close()


def test_episode_stores_summary_verbatim():
    e = _eng()
    e._llm = _FakeLLM([])
    e.ingest_compaction("sess-A", S1)
    ep = e.list_episodes(session_id="sess-A")["data"]["items"][0]
    assert ep["summary"] == S1  # stored as-is, NOT re-generated
    e.close()


def test_no_llm_still_stores_episode():
    e = _eng()
    e._llm = None
    r = e.ingest_compaction("sess-A", S1)["data"]
    assert r["memories"] == 0 and r.get("skipped") == "no_llm"
    assert e.list_episodes(session_id="sess-A")["data"]["count"] == 1
    e.close()


def test_episode_ttl_is_physical_delete():
    e = _eng()
    ep = e.add_episode("摘要", session_id="s", mode="work", trigger="compaction")["data"]
    mid = e.remember("x", "fact", episode_id=ep["id"])["data"]["id"]
    e.conn.execute("UPDATE episodes SET valid_until='2000-01-01T00:00:00+00:00'")
    e.conn.commit()
    e._expire_due()
    assert e.list_episodes()["data"]["count"] == 0
    assert e.get(mid)["data"]["memory"]["episode_id"] is None
    e.close()
