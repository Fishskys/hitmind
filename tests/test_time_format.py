"""Timestamp format: second precision, 'T' separator, configured UTC offset (default +08:00)."""

import re

from hitmind.core import lifecycle
from hitmind.core.engine import Engine

SEC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _ts(engine, mid, col="created_at"):
    return engine.conn.execute(f"SELECT {col} FROM memories WHERE id=?", (mid,)).fetchone()[0]


def test_capture_uses_seconds_and_default_offset(tmp_path):
    e = Engine(str(tmp_path / "d.db"), agent_id="x")
    try:
        mid = e.capture("一条事实", "fact")["data"]["id"]
        for col in ("created_at", "updated_at"):
            ts = _ts(e, mid, col)
            assert SEC_RE.match(ts), ts
            assert "." not in ts, "no microseconds: " + ts
            assert ts.endswith("+08:00"), ts
        # event carries a default TTL -> valid_until same format
        ev = e.capture("一件事", "event")["data"]["id"]
        assert SEC_RE.match(_ts(e, ev, "valid_until"))
    finally:
        e.close()


def test_tz_hours_is_configurable(tmp_path):
    for tz, suffix in [(0, "+00:00"), (5, "+05:00"), (8, "+08:00"), (-5, "-05:00")]:
        e = Engine(str(tmp_path / f"d{tz}.db"), agent_id="x", tz_hours=tz)
        try:
            mid = e.capture("时区测试", "fact")["data"]["id"]
            ts = _ts(e, mid)
            assert SEC_RE.match(ts) and ts.endswith(suffix), (tz, ts)
        finally:
            e.close()


def test_explicit_created_at_is_honored(tmp_path):
    e = Engine(str(tmp_path / "d.db"), agent_id="x")
    try:
        mid = e.capture("历史记忆", "fact", created_at="2026-06-10T20:00:00+08:00")["data"]["id"]
        assert _ts(e, mid) == "2026-06-10T20:00:00+08:00"
    finally:
        e.close()


def test_lifecycle_helpers_emit_seconds_offset():
    vu = lifecycle.default_valid_until("event", "2026-01-01T00:00:00+08:00", tz_hours=8)
    assert vu == "2026-04-01T00:00:00+08:00", vu
    assert lifecycle.refresh_expiry("persona", tz_hours=8) is None  # no TTL
    ev = lifecycle.refresh_expiry("event", tz_hours=8)
    assert SEC_RE.match(ev) and ev.endswith("+08:00"), ev


def test_tz_resolution_priority(monkeypatch):
    from hitmind.core.engine import Engine
    monkeypatch.delenv("HITMIND_TZ_HOURS", raising=False)
    # explicit numeric wins
    assert Engine._resolve_tz_hours(5) == 5.0
    # "auto"/""/None -> system tz
    sys_h = lifecycle.system_tz_hours()
    assert Engine._resolve_tz_hours("auto") == sys_h
    assert Engine._resolve_tz_hours("") == sys_h
    assert Engine._resolve_tz_hours(None) == sys_h
    # env override when arg is auto/None
    monkeypatch.setenv("HITMIND_TZ_HOURS", "-3")
    assert Engine._resolve_tz_hours("auto") == -3.0
    assert Engine._resolve_tz_hours(None) == -3.0
    # explicit still beats env
    assert Engine._resolve_tz_hours(7) == 7.0


def test_auto_follows_system_tz(tmp_path):
    from hitmind.core.engine import Engine
    e = Engine(str(tmp_path / "d.db"), agent_id="x", tz_hours="auto")
    try:
        sys_h = lifecycle.system_tz_hours()
        assert e.tz_hours == sys_h
        mid = e.capture("自动时区", "fact")["data"]["id"]
        ts = _ts(e, mid)
        assert SEC_RE.match(ts), ts
        # suffix matches the system offset
        off = int(sys_h)
        expected = f"{off:+03d}:00"
        assert ts.endswith(expected), (ts, expected)
    finally:
        e.close()

