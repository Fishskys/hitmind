

def test_remember_and_get(engine):
    r = engine.remember("用户偏好使用深色模式", "persona", title="深色模式")
    assert r["ok"] and r["data"]["action"] == "created"
    mid = r["data"]["id"]
    g = engine.get(mid)
    assert g["ok"] and g["data"]["memory"]["kind"] == "persona"


def test_exact_duplicate_not_recreated(engine):
    engine.remember("项目代号是 Falcon", "fact")
    r = engine.remember("项目代号是 Falcon", "fact")
    assert r["data"]["action"] == "duplicate"


def test_versioned_update_keeps_history(engine):
    r = engine.remember("当前版本 v1.0", "fact", title="版本号")
    mid = r["data"]["id"]
    u = engine.update(mid, content="当前版本 v1.1")
    assert u["data"]["action"] == "versioned" and u["data"]["version"] == 2
    hist = engine.get(mid, history=True)
    versions = hist["data"]["versions"]
    assert len(versions) == 2
    assert versions[0]["status"] == "superseded"
    assert versions[1]["status"] == "active"


def test_history_capped_at_five(engine):
    r = engine.remember("数值 1", "fact", title="计数")
    mid = r["data"]["id"]
    cur = mid
    for i in range(2, 9):  # 7 more updates -> 8 versions total
        u = engine.update(cur, content=f"数值 {i}")
        cur = u["data"]["id"]
    hist = engine.get(cur, history=True)["data"]["versions"]
    non_deleted = [v for v in hist if v["status"] != "deleted"]
    # active + up to 5 kept history (default version_keep=5)
    assert len(non_deleted) <= 6


def test_conflict_goes_inactive(engine):
    engine.remember("我喜欢喝咖啡", "persona", title="饮品")
    r = engine.remember("我不喜欢喝咖啡", "persona", title="饮品")
    assert r["data"]["action"] == "inactive"
    assert r["data"]["status"] == "inactive"
    revs = engine.reviews()
    assert revs["data"]["count"] >= 1


def test_review_accept(engine):
    engine.remember("我喜欢喝咖啡", "persona", title="饮品")
    r = engine.remember("我不喜欢喝咖啡", "persona", title="饮品")
    mid = r["data"]["id"]
    rr = engine.review_resolve(mid, "accept")
    assert rr["data"]["status"] == "active"


def test_recall_finds_by_bm25_and_orders(engine):
    engine.remember("paper towels in the kitchen", "fact", title="kitchen supplies")
    engine.remember("unrelated note about astrophysics", "fact")
    res = engine.recall("paper towels", top_k=5)
    assert res["ok"]
    ids = [m["id"] for m in res["data"]["results"]]
    assert len(ids) >= 1
    top = res["data"]["results"][0]
    assert "paper" in top["content"].lower()
    assert "final" in top["scores"]


def test_recall_empty_when_no_match(engine):
    engine.remember("completely different topic", "fact")
    res = engine.recall("量子纠缠与超导", top_k=5)
    assert res["ok"] and res["data"]["count"] == 0


def test_scope_private_isolation(tmp_path):
    from hitmind.core.engine import Engine
    a = Engine(str(tmp_path / "db.sqlite"), agent_id="agent_a")
    b = Engine(str(tmp_path / "db.sqlite"), agent_id="agent_b")
    a.remember("secret of A", "fact", scope="private")
    assert b.list()["data"]["total"] == 0  # b cannot see a's private
    assert a.list()["data"]["total"] == 1
    a.close()
    b.close()


def test_consolidate_returns_status(engine):
    # v1: consolidate wires the candidate-cluster queue but is LLM-free
    res = engine.consolidate(dry_run=True)["data"]
    assert res["dry_run"] is True
    assert "qualifying_clusters" in res
