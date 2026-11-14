"""Deterministic relative-date normalization (pure stdlib, no LLM).

Given a piece of text and a base timestamp (the logical time a memory was written),
rewrite unambiguous relative-date expressions to also carry their absolute date,
keeping the original words. Example:

    "上周五一起吃饭"  (base 2026-06-10)  ->  "上周五(2026-06-05)一起吃饭"

Only unambiguous mappings are rewritten (今天/昨天/前天/明天/后天/大后天, 上周X/这周X/下周X,
周X, 上周/这周/下周, 上/这个/下个月, 去年/今年/明年, N天|周|月|小时 前/后). Fuzzy words
(最近/前几天/过几天/这几天) are left untouched. Never raises; on any parse failure returns text unchanged.

Design notes:
- The insertion is "(YYYY-MM-DD)" (or "(YYYY-MM)" / "(YYYY)" / "(a~b)" for ranges), which contains
  no CJK relative-date words, so a SINGLE regex pass is safe and cannot re-match inserted text.
- Original words are preserved -> no information loss; disabling the feature = identity.
"""

from __future__ import annotations

import datetime as _dt
import re

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEK_PREFIX = r"(?:上上周|上上星期|上周|上星期|这周|这星期|本周|本星期|下周|下星期)"
_WEEKDAY = r"[一二三四五六日天]"
_OFFSET_NUM = r"\d+"

# Order matters: longest / most-specific alternatives first so the single pass matches greedily.
# The first two alternatives are SKIP-only (resolver returns None -> text left unchanged):
#   (1) recurring "每(个/逢)?周X[至周X]" -> every-weekday, not a single date
#   (2) BARE weekday range "周X至周X / 周X到周X" (both endpoints bare) -> a weekly span,
#       not a single date. Deliberately strict: prefixed forms (本周X至本周X / 上周X至... /
#       下周X至...) do NOT match here and are still annotated per endpoint.
_TOKEN = re.compile(
    "|".join(
        [
            r"每(?:个|逢)?(?:周|星期)" + _WEEKDAY + r"(?:(?:至|到)(?:周|星期)" + _WEEKDAY + r")?",
            r"(?:周|星期)" + _WEEKDAY + r"(?:至|到)(?:周|星期)" + _WEEKDAY,
            r"大后天", r"大前天", r"后天", r"前天", r"明天", r"今天", r"今日", r"昨天", r"昨日",
            _WEEK_PREFIX + _WEEKDAY,          # 上周五 / 下周二 / 这周日 ...
            r"(?:周|星期)" + _WEEKDAY,          # 周五 / 星期天（默认本周）
            _WEEK_PREFIX,                        # 上周 / 下周 / 这周（区间）
            r"上个月", r"上月", r"这个月", r"本月", r"下个月", r"下月",
            r"去年", r"今年", r"明年",
            _OFFSET_NUM + r"(?:个月|星期|小时|天|日|周|月)(?:以后|以前|之后|之前|前|后)",
        ]
    )
)


def _base_date(base_iso: str | None) -> _dt.date:
    if not base_iso:
        return _dt.date.today()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(base_iso))
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return _dt.datetime.fromisoformat(str(base_iso)).date()
    except Exception:
        return _dt.date.today()


def _monday(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def _fmt(d: _dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _month_span(base: _dt.date, delta_months: int) -> tuple[_dt.date, _dt.date]:
    y, m = base.year, base.month + delta_months
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    first = _dt.date(y, m, 1)
    nxt = _dt.date(y + (m // 12), (m % 12) + 1, 1)
    return first, nxt - _dt.timedelta(days=1)


def _week_segment(base: _dt.date, token: str) -> tuple[_dt.date, _dt.date]:
    mon = _monday(base)
    if token.startswith("上上"):
        mon = mon - _dt.timedelta(days=14)
    elif token.startswith("上"):
        mon = mon - _dt.timedelta(days=7)
    elif token.startswith(("下",)):
        mon = mon + _dt.timedelta(days=7)
    # 这周/本周/这星期/本星期 -> current week
    return mon, mon + _dt.timedelta(days=6)


def _resolve(token: str, base: _dt.date) -> str | None:
    """Return the absolute-date annotation for a matched token, or None to leave unchanged."""
    # Skip-only matches: recurring "每周X" and bare "周X至周X/到" ranges are NOT single dates.
    if "至" in token or "到" in token or "每" in token:
        return None
    # simple day offsets
    simple = {
        "大后天": 3, "后天": 2, "明天": 1, "今天": 0, "今日": 0,
        "昨天": -1, "昨日": -1, "前天": -2, "大前天": -3,
    }
    if token in simple:
        return "(" + _fmt(base + _dt.timedelta(days=simple[token])) + ")"

    # weekday within a week-prefix (上周五 / 下周二 / 这周日 ...)
    m = re.match(_WEEK_PREFIX + "(" + _WEEKDAY + ")$", token)
    if m:
        prefix = token[: len(token) - 1]
        x = _CN_NUM[m.group(1)]
        mon, _sun = _week_segment(base, prefix)
        return "(" + _fmt(mon + _dt.timedelta(days=x - 1)) + ")"

    # bare 周X / 星期X -> THIS WEEK (Monday-based), same day-of-week in the base's week.
    # e.g. "周一" -> the Monday of the week containing the base date. Consistent regardless
    # of which weekday the base falls on (avoids the old "nearest day" flipping past/future
    # and giving two different dates for the same '周一' said on consecutive days).
    m = re.match(r"(?:周|星期)(" + _WEEKDAY + ")$", token)
    if m:
        x = _CN_NUM[m.group(1)]
        return "(" + _fmt(_monday(base) + _dt.timedelta(days=x - 1)) + ")"

    # week segment 上周/下周/这周 (range)
    if re.fullmatch(_WEEK_PREFIX, token):
        a, b = _week_segment(base, token)
        return f"({_fmt(a)}~{_fmt(b)})"

    # months
    if token in ("上个月", "上月"):
        a, b = _month_span(base, -1)
        return f"({_fmt(a)}~{_fmt(b)})"
    if token in ("这个月", "本月"):
        a, b = _month_span(base, 0)
        return f"({_fmt(a)}~{_fmt(b)})"
    if token in ("下个月", "下月"):
        a, b = _month_span(base, 1)
        return f"({_fmt(a)}~{_fmt(b)})"

    # years
    if token == "去年":
        return f"({base.year - 1})"
    if token == "今年":
        return f"({base.year})"
    if token == "明年":
        return f"({base.year + 1})"

    # N unit 前/后
    m = re.match(r"(\d+)(个月|星期|小时|天|日|周|月)(以后|以前|之后|之前|前|后)$", token)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        direction = m.group(3)
        sign = 1 if direction in ("后", "以后", "之后") else -1
        if unit in ("天", "日"):
            return "(" + _fmt(base + _dt.timedelta(days=sign * n)) + ")"
        if unit in ("周", "星期"):
            return "(" + _fmt(base + _dt.timedelta(weeks=sign * n)) + ")"
        if unit in ("个月", "月"):
            a, _b = _month_span(base, sign * n)
            return "(" + _fmt(a) + ")"  # same day-of-month target month, first-of-month as anchor
        if unit == "小时":
            return None  # hour-level relative offset: skip (not a calendar date)

    return None


def normalize_text(text: str, base_iso: str | None) -> str:
    """Rewrite unambiguous relative dates in ``text`` to carry absolute dates. Idempotent-ish:
    inserted ``(YYYY-...)`` carries no CJK tokens, so it is never re-matched."""
    if not text:
        return text
    base = _base_date(base_iso)

    def _sub(m: "re.Match[str]") -> str:
        tok = m.group(0)
        try:
            ann = _resolve(tok, base)
        except Exception:
            return tok
        return tok + ann if ann else tok

    try:
        return _TOKEN.sub(_sub, text)
    except Exception:
        return text
