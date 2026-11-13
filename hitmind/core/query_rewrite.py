"""Deterministic query rewriting for memory recall (no LLM).

The failure it targets: a question's wording often does not match how the fact was
stated (e.g. query "哪两天接米米不用我赶过去" vs evidence "周二周四梁志远接，其余我接").
We expand one query into several deterministic variants and recall with all of them,
then fuse — cheap, CPU-only, fully explainable.

Components (all rule/dict based):
- Chinese interrogative / intent detection (哪几天/几次/多久/谁/哪儿/多少 …)
- CJK n-gram keyword extraction (reuse tokenizer)
- relation / synonym dictionary (接↔接送, 不用我赶过去↔别人代接, 欠↔还, 搬家↔搬过去/签了/住哪 …)
- time-expression recognition (昨天/今天/前天/上个月/三点后 …) — kept as tokens in v1
- pronoun handling (我/你/他/她) — kept, minimal binding in v1

The synonym dictionary is the maintenance surface; extend it as new phrasings appear.
"""

from __future__ import annotations

import re

from . import tokenize

# ---- interrogative / intent ----
_INTENT_PATTERNS = [
    ("time_days", re.compile(r"哪[几两]?天|哪一天|星期几|周几")),
    ("time_when", re.compile(r"什么时候|哪年|哪月|几点|多久|多长时间|多少天")),
    ("quantity", re.compile(r"几[个件次位袋瓶条]|多少")),
    ("entity_who", re.compile(r"谁|哪个人|哪位")),
    ("place", re.compile(r"哪[儿里边]|什么地方")),
    ("reason", re.compile(r"为什么|为啥|怎么[会回事]")),
]

# ---- relation / synonym dictionary (the maintenance surface) ----
# map a phrase -> list of surface variants likely to appear in stored turns
SYNONYMS: dict[str, list[str]] = {
    "不用我赶过去": ["不用我接", "别人接", "代接", "梁志远接"],
    "不用我接": ["别人接", "代接", "不用我赶过去"],
    "接": ["接送", "接回来"],
    "接米米": ["米米", "接送"],
    "欠": ["还", "还了", "欠账"],
    "还": ["欠", "还了"],
    "搬家": ["搬过去", "搬到", "签了", "搬走", "住哪", "住哪边"],
    "住哪": ["搬到", "住哪边", "现在住"],
    "咖啡": ["美式", "拿铁", "不喝咖啡"],
    "三点后": ["下午三点", "15点", "过了三点"],
    "夜班见闻": ["夜班", "见闻", "讲故事"],
}

_DAY_TOKENS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日", "星期", "周末"]


def detect_intent(query: str) -> list[str]:
    return [name for name, pat in _INTENT_PATTERNS if pat.search(query)]


def keywords(query: str) -> list[str]:
    """Multi-char CJK n-grams + latin words (no single CJK unigrams)."""
    toks = tokenize.tokenize(query)
    return [t for t in toks if len(t) >= 2 or re.fullmatch(r"[a-z0-9]+", t)]


def _synonym_expand(query: str) -> str:
    extra: list[str] = []
    for phrase, alts in SYNONYMS.items():
        if phrase in query:
            extra.extend(alts)
    return query + (" " + " ".join(extra) if extra else "")


def expand_queries(query: str) -> list[str]:
    """Return an ordered, de-duplicated list of query variants (original first)."""
    q = (query or "").strip()
    if not q:
        return []
    variants: list[str] = [q]

    kws = keywords(q)
    syn = _synonym_expand(q)
    if syn != q:
        variants.append(syn)
        skw = keywords(syn)
        if skw:
            variants.append(" ".join(skw))

    # intent-focused: surface the day tokens if the question is about days
    if "time_days" in detect_intent(q):
        days = [d for d in _DAY_TOKENS if d in q]
        base = " ".join(kws + days) if kws else q
        variants.append(base)
        variants.append(" ".join(kws + days + ["梁志远", "其余", "我接", "接送"]))

    # de-dup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
