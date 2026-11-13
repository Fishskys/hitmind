"""Two-stage rerank: engine reorders a shortlist via an injected reranker; fail-open."""

import tempfile

from hitmind.core.embedder import HttpReranker, NullReranker, make_reranker
from hitmind.core.engine import Engine


def _eng(**kw):
    kw.setdefault("embedder", None)
    return Engine(str(tempfile.mkdtemp()) + "/d.db", agent_id="t", **kw)


class _ReverseReranker:
    """Fake: rank documents in REVERSE input order (to prove it changes ordering)."""

    model = "fake-rr"
    ok = True

    def __init__(self):
        self.calls = 0

    def rerank(self, query, documents, top_n=None):
        self.calls += 1
        n = len(documents)
        return [(i, float(n - i)) for i in range(n)]  # earlier docs -> lower score


class _Boom:
    model = "boom"
    ok = True

    def rerank(self, query, documents, top_n=None):
        raise RuntimeError("down")


def _seed(e):
    ids = []
    for i in range(6):
        ids.append(e.capture(f"咖啡记录{i}", "fact")["data"]["id"])
    return ids


def test_null_reranker_keeps_order():
    e = _eng()  # default NullReranker
    _seed(e)
    r = e.recall("咖啡记录", top_k=5)["data"]["results"]
    assert r and all("rerank" not in m for m in r)  # no rerank annotation
    e.close()


def test_reranker_reorders_shortlist():
    rr = _ReverseReranker()
    e = _eng(reranker=rr, rerank_pool=6)
    _seed(e)
    assert e.rerank_enabled is True
    r = e.recall("咖啡记录", top_k=5)["data"]["results"]
    assert rr.calls == 1
    # reranked docs carry a 'rerank' score; the first has the highest rerank score
    assert all("rerank" in m for m in r)
    scores = [m["rerank"] for m in r]
    assert scores == sorted(scores, reverse=True)
    e.close()


def test_reranker_failure_fails_open():
    e = _eng(reranker=_Boom(), rerank_pool=6)
    _seed(e)
    r = e.recall("咖啡记录", top_k=5)["data"]["results"]  # must NOT raise
    assert r
    assert all("rerank" not in m for m in r)  # unchanged first-stage order
    e.close()


def test_reranker_disabled():
    rr = _ReverseReranker()
    e = _eng(reranker=rr, rerank_enabled=False)
    _seed(e)
    r = e.recall("咖啡记录", top_k=5)["data"]["results"]
    assert rr.calls == 0 and all("rerank" not in m for m in r)
    e.close()


def test_make_reranker_env_gating():
    import os
    os.environ.pop("HITMIND_RERANK_MODEL", None)
    os.environ.pop("HITMIND_RERANK_BASE_URL", None)
    assert isinstance(make_reranker(None, None, ""), NullReranker)  # no model/base -> null
    rr = make_reranker("http://127.0.0.1:8091/v1", "some/model", "")
    assert isinstance(rr, HttpReranker)
    assert rr.base_url == "http://127.0.0.1:8091"  # /v1 stripped -> root
