"""Seed a temp DB from bench/corpus.jsonl, run bench/queries.jsonl across several
retrieval configs, print a comparison table.

Usage:  uv run python bench/run.py
Uses the 8091 embedder when HITMIND_EMBED_BASE_URL is set; else BM25-only.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.metrics import false_recall_rate, mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402
from hitmind.core.engine import Engine, make_embedder_from_env  # noqa: E402

HERE = Path(__file__).parent

CONFIGS = [
    {"name": "linear_fl.50_.6_.4", "cfg": {"fusion": "linear", "dense_floor": 0.50, "w_dense": 0.6, "w_bm25": 0.4}},
    {"name": "linear_fl.45_.6_.4", "cfg": {"fusion": "linear", "dense_floor": 0.45, "w_dense": 0.6, "w_bm25": 0.4}},
    {"name": "linear_fl.50_.7_.3", "cfg": {"fusion": "linear", "dense_floor": 0.50, "w_dense": 0.7, "w_bm25": 0.3}},
    {"name": "linear_fl.50+bm25ratio.1", "cfg": {"fusion": "linear", "dense_floor": 0.50, "bm25_floor_ratio": 0.1, "w_dense": 0.6, "w_bm25": 0.4}},
    {"name": "rrf_fl.50", "cfg": {"fusion": "rrf", "dense_floor": 0.50, "bm25_floor": 0.0}},
]


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(k: int = 5) -> list[dict]:
    docs = _load(HERE / "corpus.jsonl")
    queries = _load(HERE / "queries.jsonl")
    rows = []
    for spec in CONFIGS:
        tmp = Path(tempfile.mkdtemp()) / "bench.db"
        e = Engine(str(tmp), agent_id="bench", embedder=make_embedder_from_env(),
                   retrieval_config=spec["cfg"])
        id_map = {}
        for d in docs:
            r = e.remember(d["content"], d["kind"], title=d["title"])
            id_map[d["id"]] = r["data"]["id"]
        res_lists, rels = [], []
        for q in queries:
            res = e.recall(q["q"], top_k=k)["data"]["results"]
            res_lists.append([m["id"] for m in res])
            rels.append({id_map[x] for x in q["relevant"]})
        e.close()
        n = len(queries)
        rows.append({
            "config": spec["name"],
            "recall@k": round(sum(recall_at_k(res_lists[i], rels[i], k) for i in range(n)) / n, 4),
            "mrr@k": round(sum(mrr_at_k(res_lists[i], rels[i], k) for i in range(n)) / n, 4),
            "ndcg@k": round(sum(ndcg_at_k(res_lists[i], rels[i], k) for i in range(n)) / n, 4),
            "false_recall": round(false_recall_rate(res_lists, rels), 4),
        })
    return rows


if __name__ == "__main__":
    rows = run()
    emb = make_embedder_from_env()
    print(f"# embedder model={emb.model} ok={emb.ok}")
    keys = ["config", "recall@k", "mrr@k", "ndcg@k", "false_recall"]
    widths = {k: max(len(k), *[len(str(r[k])) for r in rows]) for k in keys}
    print(" | ".join(k.ljust(widths[k]) for k in keys))
    print("-+-".join("-" * widths[k] for k in keys))
    for r in rows:
        print(" | ".join(str(r[k]).ljust(widths[k]) for k in keys))
