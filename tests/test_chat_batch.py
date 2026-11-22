"""Chat batch pipeline: cursor-based no-gap/no-dup; success commits, failure retries;
extensible idle runner runs registered tasks for idle sessions."""

import tempfile
from datetime import UTC, datetime, timedelta

from hitmind.core.engine import Engine


class _FakeLLM:
    ok = True
    def __init__(self, out, fail=False):
        self._out = out
        self._fail = fail
        self.calls = []
    def extract_from_messages(self, messages):
        self.calls.append(messages)
        if self._fail:
            return None
        return {"memories": self._out}


class _Source:
    def __init__(self):
        self.rows = []
    def add(self, msgs):
        base = len(self.rows)
        for i, c in enumerate(msgs):
            role, content = (c if isinstance(c, tuple) else ("user", c))
            self.rows.append({"id": base + i + 1, "role": role, "content": content, "timestamp": None})
    def count_after(self, session_id, after_id=0):
        return sum(1 for r in self.rows if r["id"] > after_id)
    def count_turns_after(self, session_id, after_id=0):
        return sum(1 for r in self.rows
                   if r["id"] > after_id and r["role"] == "user" and (r["content"] or "").strip())
    def rows_after(self, session_id, after_id=0, limit=400):
        return [r for r in self.rows if r["id"] > after_id][:limit]
    def latest_summary(self, session_id):
        return None


def _eng():
    e = Engine(str(tempfile.mkdtemp()) + "/d.db")
    e.state_source = _Source()
    e.consolidate_every_turns = 1  # need >= 2 turns (user messages)
    e.idle_consolidate_seconds = 0  # idle immediately
    return e


def _old():
    return (datetime.now(UTC) - timedelta(days=1)).isoformat()


def test_note_activity_flags_batch_only_past_threshold():
    e = _eng()
    e.state_source.add(["一"])
    assert e.note_activity("s")["data"]["pending"] is False  # 1 msg < 2
    e.state_source.add(["二"])
    assert e.note_activity("s")["data"]["pending"] is True
    e.close()


def test_threshold_counts_turns_not_rows():
    """A single user turn that fans out into many assistant/tool rows must NOT trip the
    batch threshold — the counter measures real user turns, not transcript rows."""
    e = _eng()  # threshold = 2 * 1 = 2 turns
    e.state_source.add([("user", "问题一"), ("assistant", "回复"), ("tool", "{}"),
                        ("assistant", "再回复"), ("tool", "{}")])
    # 5 rows but only 1 user turn -> below the 2-turn threshold
    assert e.note_activity("s")["data"]["pending"] is False
    e.state_source.add([("user", "问题二")])
    # now 2 user turns -> at threshold
    assert e.note_activity("s")["data"]["pending"] is True
    e.close()


def test_process_batch_success_commits_cursor_and_dedups():
    e = _eng()
    e.state_source.add(["我喜欢喝黑咖啡", "我在北京工作"])
    e._llm = _FakeLLM([{"kind": "fact", "content": "喜欢黑咖啡"},
                       {"kind": "fact", "content": "在北京工作"}])
    e.note_activity("s")
    r = e.process_chat_batch("s")["data"]
    assert r["committed"] and r["memories"] == 2
    st = e.session_state("s")
    assert st["cursor_id"] == 2 and st["pending_batch"] == 0
    assert e.list(limit=10)["data"]["total"] == 2
    # a second identical extraction is a no-op at the gate (cursor advanced -> no msgs)
    r2 = e.process_chat_batch("s")["data"]
    assert r2.get("skipped") == "empty" and e.list(limit=10)["data"]["total"] == 2
    e.close()


def test_process_batch_empty_success_still_commits():
    e = _eng()
    e.state_source.add(["寒暄一句", "再寒暄一句"])
    e._llm = _FakeLLM([])  # success, zero memories
    e.note_activity("s")
    r = e.process_chat_batch("s")["data"]
    assert r["committed"] and r["memories"] == 0
    assert e.session_state("s")["cursor_id"] == 2  # cursor still advanced
    e.close()


def test_process_batch_failure_keeps_cursor_and_pending():
    e = _eng()
    e.state_source.add(["重要事实A", "重要事实B"])
    e._llm = _FakeLLM([], fail=True)  # extraction failed
    e.note_activity("s")
    r = e.process_chat_batch("s")["data"]
    assert r["committed"] is False and r.get("skipped") == "llm_failed"
    st = e.session_state("s")
    assert st["cursor_id"] == 0 and st["pending_batch"] == 1 and st["fails"] == 1
    e.close()


def test_process_pending_defers_busy_processes_idle():
    e = _eng()
    e.state_source.add(["x1", "x2"])
    e._llm = _FakeLLM([{"kind": "fact", "content": "记忆一"}])
    # busy session (just active now) -> deferred
    e._touch_session("s", last_activity=datetime.now(UTC).isoformat(), pending_batch=1)
    e.idle_consolidate_seconds = 300
    d = e.process_pending()["data"]
    assert d["deferred"] == 1 and d["committed"] == 0
    # age it -> idle -> processed
    e._touch_session("s", last_activity=_old())
    d2 = e.process_pending()["data"]
    assert d2["committed"] == 1
    e.close()


def test_extensible_pending_tasks():
    e = _eng()
    e.register_pending_task("noop", lambda: __import__("hitmind.core.models", fromlist=["ok"]).ok({"r": 1}))
    e.register_pending_task("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    out = e.run_pending_pass()["data"]["tasks"]
    assert out["noop"] == {"r": 1} and "error" in out["boom"]
    e.close()


def test_flush_session_processes_pending_even_if_busy():
    e = _eng()
    e.state_source.add(["尾轮事实一", "尾轮事实二"])
    e._llm = _FakeLLM([{"kind": "fact", "content": "尾巴"}])
    e._touch_session("s", last_activity=datetime.now(UTC).isoformat(), pending_batch=1)
    r = e.flush_session("s")["data"]
    assert r["committed"] and r["memories"] == 1
    e.close()


def test_chat_batch_cooldown_keeps_messages_no_loss():
    """Repeated extraction failures enter a COOLDOWN (default 20 min) WITHOUT advancing
    the cursor -> no message is ever dropped; after cooldown it retries the same window."""
    import time

    from hitmind.core import topic as T

    e = _eng()
    e.state_source.add(["失败事实甲", "失败事实乙"])
    e._llm = _FakeLLM([], fail=True)
    e.note_activity("s")
    cooldown_seen = False
    for _ in range(T.CHAT_BATCH_MAX_FAILS):
        r = e.process_chat_batch("s")["data"]
        if r.get("skipped") == "cooldown":
            cooldown_seen = True
            break
    assert cooldown_seen
    st = e.session_state("s")
    # cursor NOT advanced (nothing lost), still pending, cooldown deadline in the future
    assert st["cursor_id"] == 0 and st["pending_batch"] == 1 and st["fails"] == 0
    assert int(st["batch_start"]) > int(time.time())
    # during cooldown: short-circuits, still no advance, still pending
    r2 = e.process_chat_batch("s")["data"]
    assert r2.get("skipped") == "cooldown" and e.session_state("s")["cursor_id"] == 0
    # cooldown expires -> retries the SAME window; now healthy -> both messages processed
    e._touch_session("s", batch_start=0)
    e._llm = _FakeLLM([{"kind": "fact", "content": "事实甲"},
                       {"kind": "fact", "content": "事实乙"}])
    r3 = e.process_chat_batch("s")["data"]
    assert r3["committed"] and r3["range"] == [1, 2] and r3["memories"] == 2
    assert e.session_state("s")["cursor_id"] == 2
    e.close()

