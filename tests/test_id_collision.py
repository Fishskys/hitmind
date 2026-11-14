"""ID-collision safety: a colliding generated id must never overwrite an existing
memory, and the new memory must still be written under a freshly generated id."""

import hitmind.core.engine as eng_mod


def _force_collisions(monkeypatch, colliding_id, times):
    """Make _new_id() return `colliding_id` for `times` calls, then fall back to real."""
    real = eng_mod._new_id
    state = {"n": 0}

    def fake():
        if state["n"] < times:
            state["n"] += 1
            return colliding_id
        return real()

    monkeypatch.setattr(eng_mod, "_new_id", fake)


def test_capture_id_collision_preserves_existing(engine, monkeypatch):
    first = engine.capture("原始记忆A的内容", "fact")
    existing_id = first["data"]["id"]
    original = engine.get(existing_id)["data"]["memory"]["content"]

    _force_collisions(monkeypatch, existing_id, times=2)

    second = engine.capture("新记忆B的内容", "fact")
    assert second["ok"], second
    new_id = second["data"]["id"]
    assert new_id != existing_id, "collision must be resolved with a fresh id"

    # existing memory untouched
    assert engine.get(existing_id)["data"]["memory"]["content"] == original
    # new memory present with its own content
    assert engine.get(new_id)["data"]["memory"]["content"] == "新记忆B的内容"
    # exactly two active rows -> no overwrite, no loss
    n = engine.conn.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    assert n == 2


def test_remember_id_collision_preserves_existing(engine, monkeypatch):
    first = engine.remember("已有记忆X的内容", "fact")
    existing_id = first["data"]["id"]
    original = engine.get(existing_id)["data"]["memory"]["content"]

    _force_collisions(monkeypatch, existing_id, times=2)

    second = engine.remember("另一条记忆Y的内容", "fact")
    assert second["ok"], second
    new_id = second["data"]["id"]
    assert new_id != existing_id

    assert engine.get(existing_id)["data"]["memory"]["content"] == original
    assert engine.get(new_id)["data"]["memory"]["content"] == "另一条记忆Y的内容"
    n = engine.conn.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    assert n == 2


def test_update_id_collision_preserves_history(engine, monkeypatch):
    first = engine.remember("版本 A", "fact", title="编号")
    mid = first["data"]["id"]
    # a decoy row whose id we force the version insert to collide with
    decoy = engine.remember("无关记忆", "fact")
    decoy_id = decoy["data"]["id"]

    _force_collisions(monkeypatch, decoy_id, times=2)

    u = engine.update(mid, content="版本 B")
    assert u["ok"] and u["data"]["action"] == "versioned" and u["data"]["version"] == 2
    new_id = u["data"]["id"]
    assert new_id != decoy_id

    # decoy untouched; history intact; active content is the new version
    assert engine.get(decoy_id)["data"]["memory"]["content"] == "无关记忆"
    versions = engine.get(mid, history=True)["data"]["versions"]
    assert len(versions) == 2
    assert versions[-1]["content"] == "版本 B" and versions[-1]["id"] == new_id

