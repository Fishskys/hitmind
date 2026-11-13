from datetime import datetime, timedelta, timezone


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_expired_not_recalled(engine):
    # explicit past valid_until -> excluded from recall
    engine.remember("临时会议纪要 alpha", "event", title="temp", valid_until=_past(1))
    res = engine.recall("alpha", top_k=5)
    assert res["ok"] and res["data"]["count"] == 0


def test_expire_lazy_sets_status(engine):
    r = engine.remember("会过期的项目事实", "fact", title="p", valid_until=_past(2))
    mid = r["data"]["id"]
    engine.list()  # triggers lazy expiry
    g = engine.get(mid)["data"]["memory"]
    assert g["status"] == "expired"


def test_stats_reflects_status(engine):
    engine.remember("会过期", "event", valid_until=_past(1))
    engine.remember("长期", "persona")
    st = engine.stats()["data"]["counts"]["by_status"]
    assert st.get("expired", 0) >= 1
    assert st.get("active", 0) >= 1


def test_event_default_ttl_applied(engine):
    r = engine.remember("普通事件", "event")
    g = engine.get(r["data"]["id"])["data"]["memory"]
    assert g["valid_until"] is not None  # event default TTL 90d


def test_persona_no_default_ttl(engine):
    r = engine.remember("长期偏好", "persona")
    g = engine.get(r["data"]["id"])["data"]["memory"]
    assert g["valid_until"] is None
