"""Recall/expansion/memory-length knobs: top_k, relevance floor, expand cap & hits,
newest-first expansion, per-memory char limit (+ dynamic prompt hint)."""

import tempfile

from hitmind.adapters.hermes.llm import HostClusterLLM
from hitmind.core.engine import Engine


def _eng(**kw):
    return Engine(str(tempfile.mkdtemp()) + "/d.db", embedder=None, **kw)


def test_recall_top_k_knob():
    e = _eng(recall_top_k=3)
    for i in range(8):
        e.remember(f"咖啡记录{i}", "fact")
    r = e.recall("咖啡记录")["data"]["results"]
    assert len(r) <= 3
    e.close()


def test_memory_max_chars_truncates_on_remember():
    e = _eng(memory_max_chars=10)
    mid = e.remember("这是一条很长很长的记忆内容绝对超过十个字符", "fact")["data"]["id"]
    content = e.conn.execute("SELECT content FROM memories WHERE id=?", (mid,)).fetchone()["content"]
    assert len(content) <= 10
    e.close()


def test_memory_max_chars_appears_in_llm_prompt():
    h = HostClusterLLM(object())
    h.memory_max_chars = 1024
    prompt = h._sys("BASE PROMPT")
    assert "1024" in prompt
    assert "语义单元" in prompt and "自包含" in prompt  # beyond cap -> split by semantic unit, self-contained
    h.memory_max_chars = 0
    assert h._sys("BASE PROMPT") == "BASE PROMPT"  # 0 = off, no hint


def _clustered(e, mid, topic, updated_at):
    e.conn.execute(
        "UPDATE memories SET topic=?, cluster_stats='clustered', updated_at=? WHERE id=?",
        (topic, updated_at, mid),
    )


def test_topic_expand_cap_counts_the_hit():
    # cap = TOTAL memories kept per expanded topic, and the hit counts toward it.
    # 1 seed(hit) + 5 extras, cap=3 -> only 2 extras added (total 3 in topic), newest-first.
    e = _eng(topic_expand_hits=3, topic_expand_cap=3)
    sid = e.capture("种子唯一锚点", "fact")["data"]["id"]
    _clustered(e, sid, "TX", "2026-01-01T00:00:00+00:00")
    extras = []
    for j, ts in enumerate(["2026-01-05", "2026-03-01", "2026-02-01", "2026-04-01", "2026-01-20"]):
        xid = e.capture(f"填充内容{j}", "fact")["data"]["id"]
        _clustered(e, xid, "TX", ts + "T00:00:00+00:00")
        extras.append((xid, ts))
    e.conn.commit()
    r = e.recall("种子唯一锚点")["data"]["results"]
    exp = [m for m in r if (m.get("scores") or {}).get("topic_expansion")]
    assert len(exp) == 2                                   # 3 total minus the 1 hit
    newest2 = {x for x, ts in extras if ts in ("2026-04-01", "2026-03-01")}
    assert {m["id"] for m in exp} == newest2               # newest-first, no backfill
    e.close()


def test_topic_expand_hits_caps_expanded_topics():
    e = _eng(recall_top_k=20, topic_expand_hits=2, topic_expand_cap=3)
    # 3 topics, each: one seed (matches query) + one non-matching extra
    for i in range(3):
        sid = e.capture(f"种子锚点{i}", "fact")["data"]["id"]
        _clustered(e, sid, f"T{i}", "2026-01-01T00:00:00+00:00")
        xid = e.capture(f"填充{i}", "fact")["data"]["id"]
        _clustered(e, xid, f"T{i}", "2026-01-02T00:00:00+00:00")
    e.conn.commit()
    r = e.recall("种子锚点")["data"]["results"]
    exp = [m for m in r if (m.get("scores") or {}).get("topic_expansion")]
    # only the first 2 hit memories' topics expanded; each topic 1 seed + at most
    # (cap=3 - 1) = 2 extras, but only 1 extra exists -> at most 2 extras total
    assert len(exp) <= 2
    e.close()


def test_topic_expand_shared_topic_not_double_expanded():
    # Two of the top-3 hits share topic "A"; A must be expanded only ONCE and its
    # TOTAL kept in the result must not exceed cap (the old per-hit loop double-added).
    e = _eng(recall_top_k=10, topic_expand_hits=3, topic_expand_cap=3)
    for topic, n_seed in (("A", 2), ("B", 1)):
        for s in range(n_seed):
            sid = e.capture(f"咖啡种子{topic}{s}", "fact")["data"]["id"]
            _clustered(e, sid, topic, "2026-01-01T00:00:00+00:00")
        for j in range(5):
            xid = e.capture(f"填充物{topic}{j}", "fact")["data"]["id"]  # no 咖啡 -> not in base hits
            _clustered(e, xid, topic, f"2026-02-0{j + 1}T00:00:00+00:00")
    e.conn.commit()
    r = e.recall("咖啡种子")["data"]["results"]
    from collections import Counter
    cnt = Counter(m.get("topic") for m in r if m.get("topic"))
    assert cnt.get("A", 0) <= 3, cnt   # base(2 hits) + <=1 extra, NOT 4
    assert cnt.get("B", 0) <= 3, cnt
    e.close()
