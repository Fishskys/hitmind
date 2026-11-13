"""Hermes per-turn hook: hot path writes nothing; chat flags a pending batch.

Raw per-turn capture was removed. In chat mode sync_turn only notes activity and, once
the configured turn count accumulated, flags a batch for the idle runner. Work mode is
compaction-driven. These tests pin that contract (runner disabled via conftest).
"""

import tempfile

from hitmind.adapters.hermes import HitMindMemoryProvider


class _FakeSource:
    """Minimal StateDBSource stand-in: N fake messages after the cursor."""
    ok = True
    def __init__(self, n):
        self.n = n
    def count_after(self, session_id, after_id=0):
        return self.n
    def rows_after(self, session_id, after_id=0, limit=400):
        return [{"id": i + 1, "role": "user", "content": f"消息{i}", "timestamp": None}
                for i in range(self.n)]
    def latest_summary(self, session_id):
        return None


def _prov(platform="telegram", agent_context="primary"):
    p = HitMindMemoryProvider()
    p._user_min_len = 5
    home = tempfile.mkdtemp()
    p.initialize("s", hermes_home=home, agent_context=agent_context, platform=platform)
    return p


def test_sync_turn_no_longer_writes_memories_chat():
    p = _prov(platform="telegram")  # chat
    p._engine.state_source = _FakeSource(1)
    for i in range(3):
        p.sync_turn(f"这是一句足够长的用户消息内容第{i}条", "好的", session_id="s")
    assert p._engine.list(limit=50)["data"]["total"] == 0
    p.shutdown()


def test_work_mode_sync_turn_is_noop():
    p = _prov(platform="desktop")  # work
    assert p._mode == "work"
    p._engine.state_source = _FakeSource(100)
    p.sync_turn("这是一句足够长的用户消息内容", "好的", session_id="s")
    assert p._engine.list(limit=50)["data"]["total"] == 0
    assert p._engine.session_state("s")["pending_batch"] == 0  # work never flags chat batch
    p.shutdown()


def test_mode_by_platform():
    assert _prov(platform="desktop")._mode == "work"
    assert _prov(platform="cli")._mode == "work"
    assert _prov(platform="feishu")._mode == "chat"
    assert _prov(platform="telegram")._mode == "chat"


def test_non_primary_context_skipped():
    p = _prov(platform="telegram", agent_context="cron")
    p._engine.state_source = _FakeSource(100)
    p.sync_turn("这是一句足够长的用户消息内容", "ok", session_id="s")
    assert p._engine.session_state("s")["pending_batch"] == 0
    p.shutdown()


def test_chat_flags_pending_batch_at_threshold():
    p = _prov(platform="telegram")
    p._engine.consolidate_every_turns = 1  # need >= 2 messages unprocessed
    p._engine.state_source = _FakeSource(2)
    p.sync_turn("足够长的用户消息内容第一轮", "ok", session_id="s")
    assert p._engine.session_state("s")["pending_batch"] == 1
    p.shutdown()
