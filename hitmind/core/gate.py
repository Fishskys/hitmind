"""Write gate: deterministic duplicate/conflict/version decision (no LLM).

One similarity ruler shared with retrieval (Dice over char bigrams), matching the
thresholds documented in docs/framework.md #5.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

REJECT_DICE = 0.90   # >= -> duplicate (do not create)
DUP_DICE = 0.82      # >= -> version (same fact, update)
TOPIC_DICE = 0.55    # >= -> candidate for conflict check
_NEG_RE = re.compile(r"不|没|别|无|never|not|no longer|doesn't|don't", re.IGNORECASE)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _bigrams(text: str) -> set[str]:
    t = normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def dice(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    return 2.0 * inter / (len(A) + len(B))


def _negated(text: str) -> bool:
    return bool(_NEG_RE.search(text or ""))


def classify(existing: list[dict], candidate: dict) -> dict:
    """existing: active rows of the same kind (dicts with id,root_id,title,content,dedup_hash).
    candidate: {title, content}.
    Returns {action, id?, root_id?, conflict_of?, reason?} with action in
    created | duplicate | versioned | inactive.
    """
    ch = content_hash(candidate.get("content", ""))
    cand_text = f"{candidate.get('title') or ''} {candidate.get('content') or ''}"

    # 1) exact duplicate (hash)
    for row in existing:
        if row.get("dedup_hash") == ch:
            return {"action": "duplicate", "id": row["id"], "reason": "hash"}

    best = None
    for row in existing:
        row_text = f"{row.get('title') or ''} {row.get('content') or ''}"
        s = dice(cand_text, row_text)
        if best is None or s > best[0]:
            best = (s, row)

    # 2) conflict: same title & different body, or similar & opposite polarity
    for row in existing:
        row_text = f"{row.get('title') or ''} {row.get('content') or ''}"
        same_title = (candidate.get("title") or "").strip() and \
            (candidate.get("title") or "").strip().lower() == (row.get("title") or "").strip().lower()
        s = dice(cand_text, row_text)
        if same_title and s < 0.95:
            return {"action": "inactive", "conflict_of": row["id"], "reason": "same_title"}
        if s >= TOPIC_DICE and _negated(candidate.get("content", "")) != _negated(row.get("content", "")):
            return {"action": "inactive", "conflict_of": row["id"], "reason": "negation"}

    # 3) near-duplicate -> duplicate / version
    if best is not None:
        s, row = best
        if s >= REJECT_DICE:
            return {"action": "duplicate", "id": row["id"], "reason": "near_dup"}
        if s >= DUP_DICE:
            return {"action": "versioned", "root_id": row["root_id"], "conflict_of": row["id"], "reason": "near_dup"}

    return {"action": "created"}
