"""Retrieval: BM25 (FTS5) + dense (cosine), fused with **lower-bound normalized
weighted scoring** (default), then the kind-differentiated freshness blend
(final = 0.9*rel + 0.1*fresh).

Why not plain RRF: RRF ignores score magnitude, so a single-branch high-confidence
hit (e.g. dense cosine 0.72, no BM25) loses to a two-branch mediocre hit. We instead
subtract a per-branch noise floor and normalize, so each branch contributes evidence
only above its floor:
  dense:  ndn = (cos - dense_floor) / (1 - dense_floor)
  bm25:   nbm = (bm  - bm25_floor) / (max_bm - bm25_floor)
  rel  =  w_dense*ndn + w_bm25*nbm
"""

from __future__ import annotations

import math

from . import tokenize
from .vec import unpack_vec

RRF_K = 60
# embeddinggemma-2 cosine noise floor (~0.5 for unrelated Chinese text). Configurable.
DENSE_FLOOR = 0.50
# BM25 magnitude floor. bm25() magnitude is NOT on a stable absolute scale (it swings
# from ~1e-6 on a 2-doc corpus to ~15 on a 300-doc one), so an absolute floor is
# fragile; the robust noise gate is a *relative* floor (fraction of this query's top
# bm25). Both are offered; defaults keep everything (0.0) unless tuned.
BM25_FLOOR = 0.0
BM25_FLOOR_RATIO = 0.0


def _bm25(conn, query: str, where_sql: str, params: list, limit: int):
    toks = tokenize.tokenize(query)
    if not toks:
        return {}
    multi = [t for t in toks if len(t) >= 2]
    use = multi or toks
    match = " OR ".join(f'"{t}"' for t in use)
    sql = (
        "SELECT m.id AS id, bm25(memories_fts) AS score FROM memories_fts "
        "JOIN memories m ON m.rowid = memories_fts.rowid "
        f"WHERE memories_fts MATCH ? AND {where_sql} ORDER BY score LIMIT ?"
    )
    try:
        rows = conn.execute(sql, [match, *params, limit]).fetchall()
    except Exception:
        return {}
    return {r["id"]: -float(r["score"]) for r in rows}


def _dense(conn, query_vec, where_sql: str, params: list, limit: int):
    if query_vec is None or len(query_vec) == 0:
        return {}
    q = [float(x) for x in query_vec]
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    sql = f"SELECT id, embedding FROM memories WHERE {where_sql} AND embedding IS NOT NULL"
    scored = []
    for r in conn.execute(sql, params).fetchall():
        v = unpack_vec(r["embedding"])
        if not v or len(v) != len(q):
            continue
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        dot = sum(a * b for a, b in zip(q, v))
        scored.append((r["id"], float(dot / (qn * vn))))
    scored.sort(key=lambda x: x[1], reverse=True)
    return dict(scored[:limit])


def _rrf(rank_lists: list[dict], k: int = RRF_K) -> dict:
    fused: dict[str, float] = {}
    for lst in rank_lists:
        ranked = sorted(lst.items(), key=lambda kv: kv[1], reverse=True)
        for rank, (mid, _s) in enumerate(ranked, start=1):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (k + rank)
    return fused


DEFAULT_CONFIG = {
    "fusion": "rrf",         # "rrf" (default) | "linear" (floors+normalized weighted)
    "dense_floor": DENSE_FLOOR,
    "bm25_floor": BM25_FLOOR,
    "bm25_floor_ratio": BM25_FLOOR_RATIO,
    "w_dense": 0.6,
    "w_bm25": 0.4,
    "rel_ratio": 0.0,
    "rrf_k": RRF_K,
}


def _linear_floors(bm: dict, dn: dict, cfg: dict) -> dict:
    """Lower-bound normalized weighted fusion."""
    df = cfg["dense_floor"]
    bf = cfg.get("bm25_floor", 0.0)
    ndn = {k: max(0.0, (v - df) / (1.0 - df)) for k, v in dn.items() if v >= df}
    # bm25 noise gate: drop absolute-below-floor and below a fraction of this query's max
    bm_eff = {k: v for k, v in bm.items() if v >= bf}
    mb = max(bm_eff.values()) if bm_eff else 0.0
    r = cfg.get("bm25_floor_ratio", 0.0)
    if r > 0 and mb > 0:
        bm_eff = {k: v for k, v in bm_eff.items() if v >= mb * r}
        mb = max(bm_eff.values()) if bm_eff else 0.0
    nbm = {k: ((v - bf) / (mb - bf)) if mb > bf else (1.0 if mb > 0 else 0.0) for k, v in bm_eff.items()}
    wd, wb = cfg["w_dense"], cfg["w_bm25"]
    fused = {}
    for mid in set(ndn) | set(nbm):
        fused[mid] = wd * ndn.get(mid, 0.0) + wb * nbm.get(mid, 0.0)
    return {k: v for k, v in fused.items() if v > 0.0}


def search(conn, query: str, *, query_vec, where_sql: str, params: list,
           limit: int = 50, embedder_ok: bool = True, config: dict | None = None
           ) -> list[tuple[str, dict]]:
    """Return [(id, scores_dict)] ordered by relevance (rel_norm in [0,1])."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    bm = _bm25(conn, query, where_sql, params, limit * 3)
    dn = _dense(conn, query_vec, where_sql, params, limit * 3) if embedder_ok else {}

    # optional relative pruning on dense
    if dn and cfg.get("rel_ratio", 0.0) > 0:
        top = max(dn.values())
        dn = {k: v for k, v in dn.items() if v >= top * cfg["rel_ratio"]}

    if cfg["fusion"] == "rrf":
        # legacy: rank-based; apply dense floor + bm25 floor first
        dn = {k: v for k, v in dn.items() if v >= cfg["dense_floor"]}
        bm = {k: v for k, v in bm.items() if v >= cfg["bm25_floor"]}
        fused = _rrf([bm, dn], cfg["rrf_k"])
    else:
        fused = _linear_floors(bm, dn, cfg)

    if not fused:
        return []
    max_r = max(fused.values()) or 1.0
    out = []
    for mid, raw in fused.items():
        out.append((mid, {"dense": dn.get(mid), "bm25": bm.get(mid),
                          "score": raw, "rel_norm": raw / max_r}))
    out.sort(key=lambda x: x[1]["rel_norm"], reverse=True)
    return out
