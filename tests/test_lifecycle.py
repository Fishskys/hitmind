from hitmind.core import lifecycle


def test_freshness_decays_by_kind():
    now = lifecycle._parse("2026-11-09T00:00:00+00:00")
    # event half-life 14d -> one half life old ~= 0.5
    f = lifecycle.freshness("2026-10-26T00:00:00+00:00", "event", now=now)
    assert 0.45 < f < 0.55
    # persona half-life 180d -> barely decayed
    p = lifecycle.freshness("2026-10-26T00:00:00+00:00", "persona", now=now)
    assert p > 0.94


def test_final_score_weighting():
    # final = 0.8*rel + 0.1*importance + 0.1*fresh
    assert abs(lifecycle.final_score(1.0, 1.0, 1.0) - 1.0) < 1e-9
    assert abs(lifecycle.final_score(1.0, 0.0, 0.0) - 0.8) < 1e-9   # relevance dominant
    assert abs(lifecycle.final_score(0.0, 1.0, 0.0) - 0.1) < 1e-9   # importance weight
    assert abs(lifecycle.final_score(0.0, 0.0, 1.0) - 0.1) < 1e-9   # freshness weight


def test_default_ttl_per_kind():
    from datetime import datetime
    ev = lifecycle.default_valid_until("event", "2026-01-01T00:00:00+00:00")
    assert ev is not None
    # event TTL 90d; persona/fact/procedure have no default TTL
    assert (datetime.fromisoformat(ev) - datetime.fromisoformat("2026-01-01T00:00:00+00:00")).days == 90
    assert lifecycle.default_valid_until("persona", "2026-01-01T00:00:00+00:00") is None
    assert lifecycle.default_valid_until("fact", "2026-01-01T00:00:00+00:00") is None
    assert lifecycle.default_valid_until("procedure", "2026-01-01T00:00:00+00:00") is None
