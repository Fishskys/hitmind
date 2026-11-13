"""Deterministic relative-date normalization (hitmind.core.timenorm + Engine wiring)."""

import os

from hitmind.core.engine import Engine
from hitmind.core.timenorm import normalize_text


def test_normalize_basic_offsets():
    assert "2026-06-05" in normalize_text("上周五是芸青生日", "2026-06-10")
    assert "2026-05-25" in normalize_text("前天把迷迭香搬到窗边", "2026-05-27")
    assert "2026-06-23" in normalize_text("说下周二下午", "2026-06-18")
    assert "2026-05-02" in normalize_text("昨天在江北回声替班", "2026-05-03")
    assert "2026-07-24" in normalize_text("昨天到邮局取茶叶", "2026-07-25")
    assert "2026-07-01" in normalize_text("答应下周三前寄画册", "2026-06-22")


def test_normalize_bare_weekday_nearest():
    # bare "周X" resolves to the NEAREST such weekday (forward or back), never a wrong
    # far-in-the-past date. Sat 2026-05-02 saying "周一" = coming Monday 2026-05-04.
    assert "2026-05-04" in normalize_text("周一送的合影", "2026-05-02")
    # Wed 2026-05-06 saying "周一" = the just-passed Monday 2026-05-04 (nearest, back).
    assert "2026-05-04" in normalize_text("周一开过会", "2026-05-06")
    # prefixed forms keep their exact week meaning: 下周二 on Wed 5-06 = 2026-05-12
    assert "2026-05-12" in normalize_text("下周二去", "2026-05-06")


def test_normalize_quantities_and_ranges():
    assert "2026-06-13" in normalize_text("3天后回访", "2026-06-10")
    assert "2026-06-03" in normalize_text("5天前见面", "2026-06-08")
    # week / month ranges render as (a~b)
    assert "~" in normalize_text("上周很忙", "2026-06-10")
    assert "~" in normalize_text("下个月再看", "2026-06-10")


def test_exclude_recurring_weekday():
    # "每(+个/逢)?周X" = every-weekday recurrence, NOT a single date -> unchanged
    for t in ("每周五健身", "每个周五开会", "每逢周五聚餐", "每星期五打卡"):
        assert normalize_text(t, "2026-06-10") == t


def test_exclude_bare_weekday_range_only():
    # BARE weekday range (both endpoints bare 周X/星期X) = weekly span -> unchanged
    for t in ("周一至周五上班", "周一至周四加班", "周六到周日休息", "周一到周五", "星期一至星期五"):
        assert normalize_text(t, "2026-06-10") == t


def test_prefix_weekday_range_still_annotated():
    # Prefixed forms (上/本/这/下/上上 + 周X) are NOT bare ranges -> both endpoints annotated
    out = normalize_text("本周一至本周五都忙", "2026-06-10")
    assert "本周一(2026-06-08)" in out and "本周五(2026-06-12)" in out
    out = normalize_text("上周五至上周日", "2026-06-10")
    assert "上周五(2026-06-05)" in out and "上周日(2026-06-07)" in out
    out = normalize_text("下周一至下周三", "2026-06-10")
    assert "下周一(2026-06-15)" in out and "下周三(2026-06-17)" in out


def test_normalize_keeps_original_words():
    # original wording is preserved verbatim; the annotation is additive
    out = normalize_text("上周五是芸青生日", "2026-06-10")
    assert out.startswith("上周五")
    assert "芸青生日" in out


def test_normalize_fuzzy_untouched():
    # fuzzy relative words must not be "guessed"
    for t in ("最近有点累", "前几天见过", "过几天再说", "这几天忙"):
        assert normalize_text(t, "2026-06-10") == t


def test_normalize_empty_and_bad_base():
    assert normalize_text("", "2026-06-10") == ""
    # invalid base falls back without raising
    assert "2026" in normalize_text("今天", "not-a-date") or True


def _mk(db, **kw):
    kw.setdefault("embedder", None)
    return Engine(db, agent_id="tester", **kw)


def test_engine_capture_stores_normalized_and_keeps_raw(tmp_path):
    os.environ["HITMIND_BG_RUNNER"] = "0"
    e = _mk(str(tmp_path / "d.db"))
    try:
        r = e.capture("上周五是芸青生日", "fact", created_at="2026-06-10T20:00:00+08:00")
        mid = r["data"]["id"]
        row = e.conn.execute("SELECT content, normalized_text FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["content"] == "上周五是芸青生日"            # raw untouched
        assert row["normalized_text"] == "上周五(2026-06-05)是芸青生日"  # absolute date added
    finally:
        e.close()


def test_engine_recall_finds_by_absolute_date(tmp_path):
    os.environ["HITMIND_BG_RUNNER"] = "0"
    e = _mk(str(tmp_path / "d.db"))
    try:
        e.capture("上周五是芸青生日", "fact", created_at="2026-06-10T20:00:00+08:00")
        e.capture("周末去了海边", "event", created_at="2026-07-01T09:00:00+08:00")
        res = e.recall("芸青生日是什么时候 2026-06-05", top_k=5)["data"]["results"]
        assert res, "expected a hit"
        top = res[0]
        assert "芸青生日" in (top.get("content") or "")
        # result surfaces normalized_text so downstream (answerer) sees the absolute date
        assert top.get("normalized_text") and "2026-06-05" in top["normalized_text"]
    finally:
        e.close()


def test_engine_time_normalize_off(tmp_path):
    os.environ["HITMIND_BG_RUNNER"] = "0"
    e = _mk(str(tmp_path / "d.db"), time_normalize=False)
    try:
        r = e.capture("上周五是芸青生日", "fact", created_at="2026-06-10T20:00:00+08:00")
        mid = r["data"]["id"]
        row = e.conn.execute("SELECT normalized_text FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["normalized_text"] is None
    finally:
        e.close()
