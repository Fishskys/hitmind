"""Tests for the shared capture-policy gate (hitmind/capture_policy.py) and its DSH binding."""

from __future__ import annotations

import tempfile

from hitmind import capture_policy as cp


def test_should_capture_role_filter():
    roles = cp.parse_roles("user")
    assert cp.should_capture("assistant", "x" * 50, roles=roles, user_min=5, assistant_min=10)[0] is False
    assert cp.should_capture("user", "a long enough user message", roles=roles, user_min=5, assistant_min=10)[0] is True


def test_should_capture_min_length():
    roles = cp.parse_roles("user,assistant")
    assert cp.should_capture("user", "hi", roles=roles, user_min=5, assistant_min=10)[0] is False      # too short
    assert cp.should_capture("user", "hello there", roles=roles, user_min=5, assistant_min=10)[0] is True
    assert cp.should_capture("assistant", "short", roles=roles, user_min=5, assistant_min=10)[0] is False
    assert cp.should_capture("assistant", "a long assistant reply", roles=roles, user_min=5, assistant_min=10)[0] is True


def test_should_capture_skip_context():
    roles = cp.parse_roles("user")
    skip = cp.parse_contexts("cron,subagent")
    ok, _ = cp.should_capture("user", "a sufficiently long message here", roles=roles, user_min=5,
                              assistant_min=10, context="cron", skip_contexts=skip)
    assert ok is False
    ok, _ = cp.should_capture("user", "a sufficiently long message here", roles=roles, user_min=5,
                              assistant_min=10, context="primary", skip_contexts=skip)
    assert ok is True


def test_machine_envelope_stripped():
    roles = cp.parse_roles("user")
    ok, clean = cp.should_capture("user", "[CONTEXT COMPACTION] blah blah", roles=roles, user_min=5, assistant_min=10)
    assert ok is False  # whole-turn machine scaffolding dropped
    ok, clean = cp.should_capture("user", "[SYSTEM NOTE] the real human question is long enough",
                                  roles=roles, user_min=5, assistant_min=10)
    assert ok is True and clean == "the real human question is long enough"


def test_custom_sanitizer_used():
    roles = cp.parse_roles("user")
    ok, clean = cp.should_capture("user", "XXX keep me please", roles=roles, user_min=5, assistant_min=10,
                                  sanitizer=lambda t: (t or "").replace("XXX ", ""))
    assert ok is True and clean == "keep me please"


def test_daemon_append_turn_applies_policy(monkeypatch):
    """Defensive backstop: the daemon drops short/skip turns even if the plugin sends them."""
    monkeypatch.setenv("HITMIND_SYNC_ROLES", "user")
    monkeypatch.setenv("HITMIND_SYNC_USER_MIN", "5")
    from hitmind.daemon import Daemon
    d = Daemon(tempfile.mktemp(suffix=".db"), agent_id="t")
    try:
        # too-short user turn -> skipped, no row
        r = d.handle("append_turn", {"session_id": "s1", "role": "user", "content": "hi"})
        assert r.get("skipped") == "policy"
        assert d.store.count_after("s1") == 0
        # assistant not in sync_roles -> skipped
        r = d.handle("append_turn", {"session_id": "s1", "role": "assistant", "content": "a long assistant reply"})
        assert r.get("skipped") == "policy"
        # long-enough user turn -> stored
        r = d.handle("append_turn", {"session_id": "s1", "role": "user", "content": "this is a real long user message"})
        assert r.get("ok") is True and r.get("id")
        assert d.store.count_after("s1") == 1
    finally:
        d.store.close()
        for e in (d.serve, d.worker):
            try:
                e.conn.close()
            except Exception:
                pass
