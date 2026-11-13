"""Embedder abstraction: HTTP client for the 8091 local-embed service + a null
fallback. Prefix is applied SERVER-SIDE (per models.yaml); the client only sends
``input_type`` (+ ``title`` for the document side). Never pre-prefix here.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol


class Embedder(Protocol):
    model: str
    dim: int | None
    ok: bool

    def embed_documents(self, texts: list[str], titles: list[str | None] | None = None) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class NullEmbedder:
    """Used when no embedding backend is configured/reachable → BM25-only."""

    model = "none"
    dim = None
    ok = False

    def embed_documents(self, texts, titles=None):
        return []

    def embed_query(self, text):
        return []


class HttpEmbedder:
    """OpenAI-compatible /v1/embeddings caller (local-embed).

    Failure guard: a single attempt is bounded by ``timeout``; after
    ``max_consecutive_failures`` consecutive failures the embedder TRIPS (``ok=False``)
    and stops issuing further HTTP calls until ``reset()`` — so a down/slow embed
    service can't cost every write a long timeout forever. Recall/BM25 auto-degrades
    meanwhile (embeddings become NULL). The idle runner resets it each pass.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 10.0,
                 max_consecutive_failures: int = 5):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.dim: int | None = None
        self.ok = True
        self._fails = 0

    def reset(self) -> None:
        """Re-open the breaker (called by the idle runner to probe recovery)."""
        self._fails = 0
        self.ok = True

    def _trip(self) -> None:
        self._fails += 1
        if self._fails >= self.max_consecutive_failures:
            self.ok = False

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=data,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _embed(self, inputs: list[str], input_type: str, titles: list | None = None) -> list[list[float]]:
        if not inputs or not self.ok:
            return []
        payload = {"model": self.model, "input": inputs, "input_type": input_type}
        if titles is not None and any(t for t in titles):
            # titles only meaningful for document/doc_prefix {title}
            payload["title"] = titles
        try:
            body = self._post(payload)
            vecs = [d["embedding"] for d in body["data"]]
        except Exception:
            self._trip()
            return []
        if vecs and self.dim is None:
            self.dim = len(vecs[0])
        if len(vecs) != len(inputs):
            self._trip()
            return []
        self._fails = 0  # success resets the consecutive-failure counter
        return vecs

    def embed_documents(self, texts, titles=None):
        return self._embed(texts, "document", titles)

    def embed_query(self, text):
        out = self._embed([text], "query")
        return out[0] if out else []


# ---------------------------------------------------------------------------
# Reranker (local cross-encoder, e.g. /rerank on the 8091 local-embed service)
# ---------------------------------------------------------------------------

class Reranker(Protocol):
    model: str
    ok: bool

    def rerank(self, query: str, documents: list[str], top_n: int | None = None
               ) -> list[tuple[int, float]]: ...


class NullReranker:
    """No rerank backend -> recall keeps its first-stage (fusion/final_score) order."""

    model = "none"
    ok = False

    def rerank(self, query, documents, top_n=None):
        return []


class HttpReranker:
    """Client for the local-embed ``POST /rerank`` cross-encoder endpoint.

    Request:  {"model", "query", "documents": [...], "top_n"?: N}
    Response: {"results": [{"index", "relevance_score"|"score"}...]}  (descending)

    ``base_url`` is the server ROOT (e.g. ``http://127.0.0.1:8091``); the rerank route
    is ``/rerank`` (NOT under ``/v1``). Scores are RAW cross-encoder logits (can be
    negative, unbounded) — used only for ORDERING a shortlist, never thresholded.

    Same failure breaker as the embedder: after ``max_consecutive_failures`` the reranker
    TRIPS (ok=False) and recall silently keeps first-stage order until ``reset()``.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 10.0,
                 max_consecutive_failures: int = 5):
        b = (base_url or "").rstrip("/")
        if b.endswith("/v1"):
            b = b[:-3]  # 8091 root: /v1/embeddings + /rerank both live here
        self.base_url = b
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.ok = True
        self._fails = 0

    def reset(self) -> None:
        self._fails = 0
        self.ok = True

    def _trip(self) -> None:
        self._fails += 1
        if self._fails >= self.max_consecutive_failures:
            self.ok = False

    def rerank(self, query: str, documents: list[str], top_n: int | None = None
               ) -> list[tuple[int, float]]:
        if not self.ok or not documents:
            return []
        payload: dict = {"model": self.model, "query": query, "documents": documents}
        if top_n:
            payload["top_n"] = int(top_n)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/rerank",
            data=data,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            out: list[tuple[int, float]] = []
            for r in body["results"]:
                idx = int(r["index"])
                score = r.get("relevance_score", r.get("score"))
                out.append((idx, float(score)))
        except Exception:
            self._trip()
            return []
        self._fails = 0  # success resets the breaker
        return out


def make_reranker(base_url: str | None = None, model: str | None = None, api_key: str = "") -> Reranker:
    """Build a reranker from explicit args or HITMIND_RERANK_* / HITMIND_EMBED_* env.
    Returns NullReranker when no base URL or model is available."""
    import os
    base = base_url or os.environ.get("HITMIND_RERANK_BASE_URL") or os.environ.get("HITMIND_EMBED_BASE_URL")
    model = model or os.environ.get("HITMIND_RERANK_MODEL")
    if not base or not model:
        return NullReranker()
    key = api_key or os.environ.get("HITMIND_RERANK_API_KEY", "") or os.environ.get("HITMIND_EMBED_API_KEY", "")
    return HttpReranker(base, model, key)
