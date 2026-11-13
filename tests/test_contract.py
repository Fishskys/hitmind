from hitmind.core import models


def test_recall_kind_filter(engine):
    engine.remember("项目用 sqlite 存储", "fact", title="存储")
    engine.remember("用户偏好深色", "persona", title="主题")
    res = engine.recall("存储 深色", top_k=5, filters={"kind": ["persona"]})
    assert all(m["kind"] == "persona" for m in res["data"]["results"])
    assert res["data"]["count"] >= 1


def test_recall_tags_filter(engine):
    engine.remember("alpha note one", "fact", tags=["projX"])
    engine.remember("alpha note two", "fact", tags=["projY"])
    res = engine.recall("alpha note", top_k=5, filters={"tags": ["projX"]})
    assert res["data"]["count"] == 1
    assert "projX" in res["data"]["results"][0]["tags"]


def test_list_tags_filter(engine):
    engine.remember("one", "fact", tags=["a", "b"])
    engine.remember("two", "fact", tags=["c"])
    out = engine.list(filters={"tags": ["b"]})
    assert out["data"]["total"] == 1


def test_update_dedup_guard(engine):
    engine.remember("原始内容 AAA", "fact", title="甲")
    r2 = engine.remember("另一条 BBB", "fact", title="乙")
    try:
        engine.update(r2["data"]["id"], content="原始内容 AAA")
    except models.HitMindError as exc:
        assert exc.code == models.INVALID_ARGUMENT
    else:
        raise AssertionError("expected INVALID_ARGUMENT on duplicate update")


def test_get_unknown_raises(engine):
    try:
        engine.get("mem_does_not_exist")
    except models.HitMindError as exc:
        assert exc.code == models.NOT_FOUND
    else:
        raise AssertionError("expected NOT_FOUND")
