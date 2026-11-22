"""Engine: the 11-verb facade over the SQLite store (host-agnostic)."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from ..logging_setup import CORE, get_logger
from . import lifecycle as lifecycle_mod
from . import models, query_rewrite, retrieval, timenorm, tokenize
from . import topic as topic_mod
from .db import connect, read_tx, write_tx
from .embedder import Embedder, HttpEmbedder, NullEmbedder, NullReranker, Reranker
from .gate import classify, content_hash
from .lifecycle import default_valid_until, final_score, freshness, refresh_expiry
from .vec import pack_vec

logger = get_logger(CORE)

_B32 = "0123456789abcdefghjkmnpqrstvwxyz"

DEFAULT_KIND_IMPORTANCE = {"persona": 1.0, "fact": 1.0, "event": 1.0, "procedure": 1.0}


def _merge_kind_importance(overrides: dict | None) -> dict:
    """Per-kind default importance blended over DEFAULT_KIND_IMPORTANCE (clamped 0..1)."""
    out = dict(DEFAULT_KIND_IMPORTANCE)
    for k, v in (overrides or {}).items():
        if k in out:
            try:
                out[k] = min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                pass
    return out


def _now(tz_hours: float = lifecycle_mod.DEFAULT_TZ_HOURS) -> str:
    """Current logical timestamp: second-precision, 'T'-separated, configured offset
    (default +08:00), e.g. ``2026-11-12T15:22:48+08:00``."""
    return lifecycle_mod._iso_seconds(lifecycle_mod.now_tz(tz_hours), tz_hours)


def _new_id() -> str:
    """Short opaque id: ``m_`` + 8 base32 chars (~40 bits)."""
    return "m_" + "".join(secrets.choice(_B32) for _ in range(8))


def _row_to_memory(r: Any) -> dict:
    return {
        "id": r["id"], "root_id": r["root_id"], "version": r["version"],
        "agent_id": r["agent_id"], "scope": r["scope"], "kind": r["kind"],
        "tags": json.loads(r["tags"] or "[]"), "title": r["title"], "content": r["content"],
        "importance": r["importance"],
        "source": r["source"], "status": r["status"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "last_recalled_at": r["last_recalled_at"], "recalled_times": r["recalled_times"],
        "valid_from": r["valid_from"], "valid_until": r["valid_until"],
        "superseded_by": r["superseded_by"], "conflict_of": r["conflict_of"],
        "topic": r["topic"],
        "pending_since": r["pending_since"],
        "cluster_id": r["cluster_id"],
        "cluster_stats": r["cluster_stats"],
        "episode_id": r["episode_id"],
        "normalized_text": (r["normalized_text"] if "normalized_text" in r.keys() else None),
    }


def _locked(method):
    """Serialize a public Engine method on the engine-level RLock (reentrant, so
    nested calls such as remember->_embed or recall->write_tx are safe)."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Engine:
    @staticmethod
    def _resolve_tz_hours(explicit) -> float:
        """Resolve the stored-timestamp timezone offset (hours).

        Priority: explicit numeric arg > ``HITMIND_TZ_HOURS`` env > OS local timezone.
        ``None`` / ``"auto"`` / ``""`` means "read the system timezone" (zero-config).
        """
        candidates = [explicit, os.environ.get("HITMIND_TZ_HOURS")]
        for c in candidates:
            if c is None:
                continue
            s = str(c).strip().lower()
            if s in ("", "auto"):
                continue
            try:
                return lifecycle_mod.parse_tz_hours(s)  # supports 5.5 / "5:30" / "-3:30"
            except ValueError:
                continue
        return lifecycle_mod.system_tz_hours()

    def __init__(self, db_path: str, *, agent_id: str = "hermes", embedder: Optional[Embedder] = None,
                 retrieval_config: Optional[dict] = None,
                 reranker: Optional[Reranker] = None,
                 raw_ttl_days: int | None = None,
                 cluster_min_size: int | None = None,
                 consolidate_min_working: int | None = None,
                 consolidate_every_turns: int | None = None,
                 topic_expand_cap: int | None = None,
                 topic_expand_hits: int | None = None,
                 recall_top_k: int | None = None,
                 dense_floor: float | None = None,
                 rerank_enabled: bool | None = None,
                 rerank_pool: int | None = None,
                 memory_max_chars: int | None = None,
                 version_keep: int | None = None,
                 review_trigger_min: int | None = None,
                 idle_poll_seconds: int | None = None,
                 kind_importance: dict | None = None,
                 episode_ttl_days: int | None = None,
                 idle_consolidate_seconds: int | None = None,
                 chat_cooldown_seconds: int | None = None,
                 cluster_cos_floor: float | None = None,
                 cluster_scan_window: int | None = None,
                 topic_max_members: int | None = None,
                 topic_min_cluster: int | None = None,
                 consolidate_llm_enabled: bool | None = None,
                 half_life_days: dict | None = None,
                 ttl_days: dict | None = None,
                 rel_weight: float | None = None,
                 importance_weight: float | None = None,
                 capacity_warn: int | None = None,
                 time_normalize: bool | None = None,
                 tz_hours: float | None = None):
        self.db_path = db_path
        self.agent_id = agent_id
        self.embedder: Embedder = embedder or NullEmbedder()
        self.reranker: Reranker = reranker or NullReranker()
        self.retrieval_config = dict(retrieval_config) if retrieval_config else {}
        # TTL (days) for un-consolidated auto-capture rows: None disables the sweep
        # (benchmarks / chronological corpus ingestion need every turn kept verbatim).
        if raw_ttl_days is None:
            raw_ttl_days = int(os.environ.get("HITMIND_RAW_TTL_DAYS", "0")) or None
        self.raw_ttl_days = raw_ttl_days
        # ---- the ONLY 4 tunables exposed (everything else is a code constant) ----
        def _cfg(explicit, env_key, default):
            if explicit is not None:
                return int(explicit)
            return int(os.environ.get(env_key, str(default)))
        self.cluster_min_size = _cfg(cluster_min_size, "HITMIND_CLUSTER_MIN_SIZE", 10)
        self.consolidate_min_working = _cfg(consolidate_min_working, "HITMIND_CONSOLIDATE_MIN_WORKING", 50)
        self.consolidate_every_turns = _cfg(consolidate_every_turns, "HITMIND_SYNC_CONSOLIDATE_EVERY", 10)
        self.topic_expand_cap = _cfg(topic_expand_cap, "HITMIND_TOPIC_EXPAND_CAP", 3)
        self.topic_expand_hits = _cfg(topic_expand_hits, "HITMIND_TOPIC_EXPAND_HITS", 3)
        self.recall_top_k = _cfg(recall_top_k, "HITMIND_RECALL_TOP_K", 5)
        self.memory_max_chars = _cfg(memory_max_chars, "HITMIND_MEMORY_MAX_CHARS", 1024)

        def _flag(explicit, env_key, default):
            if explicit is not None:
                return bool(explicit)
            return os.environ.get(env_key, "1" if default else "0") not in ("0", "false", "False", "")
        # write-time deterministic relative-date normalization (store normalized_text;
        # use it for embedding + FTS so "上周五" also indexes as an absolute date).
        self.time_normalize = _flag(time_normalize, "HITMIND_TIME_NORMALIZE", True)
        self.version_keep = _cfg(version_keep, "HITMIND_VERSION_KEEP", 5)
        self.review_trigger_min = _cfg(review_trigger_min, "HITMIND_REVIEW_TRIGGER_MIN", topic_mod.REVIEW_TRIGGER_MIN)
        # background runner wake/poll interval (seconds); the daemon thread ticks this often.
        self.idle_poll_seconds = max(15, _cfg(idle_poll_seconds, "HITMIND_IDLE_POLL_SECONDS", 60))
        # env fallbacks keep bare-env and spec-driven hosts consistent with the single source.
        def _env_num(env_key, default, cast=int):
            try:
                return cast(os.environ.get(env_key, str(default)))
            except (TypeError, ValueError):
                return default
        # per-kind default importance (0..1) applied when a write doesn't specify one
        if kind_importance is None:
            kind_importance = {
                k: _env_num(f"HITMIND_IMPORTANCE_{k.upper()}", DEFAULT_KIND_IMPORTANCE[k], float)
                for k in DEFAULT_KIND_IMPORTANCE
            }
        self.kind_importance = _merge_kind_importance(kind_importance)
        # dense cosine floor (absolute): drop candidates whose cosine < this before fusion.
        # Resolves to retrieval.DEFAULT_CONFIG["dense_floor"] (0.50) when not provided.
        _df = (float(dense_floor) if dense_floor is not None
               else float(os.environ.get("HITMIND_DENSE_FLOOR", str(retrieval.DENSE_FLOOR))))
        self.retrieval_config.setdefault("dense_floor", _df)
        # two-stage rerank: use the reranker to reorder a shortlist before top_k. Enabled by
        # default; inert (no-op) unless an actual reranker is wired in. rerank_pool is how many
        # first-stage candidates the cross-encoder reorders.
        self.rerank_enabled = (bool(rerank_enabled) if rerank_enabled is not None
                               else os.environ.get("HITMIND_RERANK_ENABLED", "1") not in ("0", "false", "False", ""))
        self.rerank_pool = max(1, int(rerank_pool) if rerank_pool is not None
                               else int(os.environ.get("HITMIND_RERANK_POOL", "30")))
        self.episode_ttl_days = _cfg(episode_ttl_days, "HITMIND_EPISODE_TTL_DAYS", 90)
        # idle threshold (seconds) before background tasks run for a session; 0 disables
        self.idle_consolidate_seconds = _cfg(idle_consolidate_seconds, "HITMIND_IDLE_CONSOLIDATE_SECONDS", 300)
        # chat-batch cooldown after too many consecutive extraction failures: keep the
        # cursor (LOSE NOTHING) and retry after this many seconds (default 20 min).
        self.chat_cooldown_seconds = _cfg(chat_cooldown_seconds, "HITMIND_CHAT_COOLDOWN_SECONDS", 1200)
        # ---- clustering knobs (were topic.py constants; now configurable; None -> env -> constant)
        def _cfg_or(explicit, env_key, default):
            if explicit is not None:
                return explicit
            return _env_num(env_key, default, float if isinstance(default, float) else int)
        self.cluster_cos_floor = float(_cfg_or(cluster_cos_floor, "HITMIND_CLUSTER_COS_FLOOR", topic_mod.PRE_CLUSTER_COS_FLOOR))
        self.cluster_scan_window = int(_cfg_or(cluster_scan_window, "HITMIND_CLUSTER_SCAN_WINDOW", topic_mod.PRE_CLUSTER_SCAN_WINDOW))
        # topic_max_members = single-cluster cap ("一个 topic 最多几条记忆"). Default 10.
        self.topic_max_members = int(_cfg_or(topic_max_members, "HITMIND_TOPIC_MAX_MEMBERS", 10))
        self.topic_min_cluster = int(_cfg_or(topic_min_cluster, "HITMIND_TOPIC_MIN_CLUSTER", topic_mod.TOPIC_MIN_CLUSTER))
        self.consolidate_llm_enabled = (
            bool(consolidate_llm_enabled) if consolidate_llm_enabled is not None
            else (os.environ.get("HITMIND_CONSOLIDATE_LLM_ENABLED", "1" if topic_mod.CONSOLIDATE_LLM_ENABLED else "0")
                  not in ("0", "false", "False", "")))
        # ---- lifecycle knobs (were lifecycle.py module defaults) ----
        def _env_dict(prefix, base, cast):
            out = dict(base)
            for k in out:
                raw = os.environ.get(f"{prefix}_{k.upper()}_DAYS")
                if raw is not None:
                    try:
                        out[k] = cast(raw)
                    except (TypeError, ValueError):
                        pass
            return out
        self.half_life_days = half_life_days if isinstance(half_life_days, dict) else _env_dict("HITMIND_HALF_LIFE", lifecycle_mod.HALF_LIFE_DAYS, int)
        self.ttl_days = ttl_days if isinstance(ttl_days, dict) else _env_dict("HITMIND_TTL", lifecycle_mod.DEFAULT_TTL_DAYS, int)
        self.rel_weight = float(_cfg_or(rel_weight, "HITMIND_REL_WEIGHT", lifecycle_mod.W_REL))
        self.importance_weight = float(_cfg_or(importance_weight, "HITMIND_IMPORTANCE_WEIGHT", lifecycle_mod.W_IMP))
        self.capacity_warn = int(_cfg_or(capacity_warn, "HITMIND_CAPACITY_WARN", lifecycle_mod.CAPACITY_WARN))
        # timezone offset (hours) for the logical/display timestamps written to the store.
        # Resolution: explicit numeric arg > HITMIND_TZ_HOURS env > OS local timezone (auto).
        # A value of "auto"/None/"" means "read the system tz" — zero config by default.
        # Stored timestamps are second-precision, 'T'-separated, carrying this offset
        # (e.g. ...T15:22:48+08:00).
        self.tz_hours = self._resolve_tz_hours(tz_hours)
        # idle/background tasks (chat batch, topic aggregation, ...). Registered by the
        # adapter; run by an idle runner. Never on the hot path.
        self._pending_tasks: dict[str, Any] = {}
        self._lock = threading.RLock()
        self.conn = connect(db_path)
        # lazily-created offline LLM finalizer (never used on the write path)
        self._llm = None
        # L0 read-only source over Hermes state.db (set by the host adapter)
        self.state_source = None

    # -- helpers ------------------------------------------------------------
    def _scope_sql(self) -> tuple[str, list]:
        return "(scope='shared' OR (scope='private' AND agent_id=?))", [self.agent_id]

    def _embed_text(self, text: str, title: str | None = None) -> bytes | None:
        """Embed `text` (the normalized content when enabled, else the raw content),
        passing `title` as document metadata exactly like the original path — so the
        vector is comparable to the un-normalized baseline (only the text differs)."""
        if not getattr(self.embedder, "ok", False):
            return None
        vecs = self.embedder.embed_documents([text], [title])
        if not vecs:
            return None
        return pack_vec(vecs[0])

    def _embed_doc(self, title: str | None, content: str) -> bytes | None:
        # unchanged baseline semantics: embed content, pass title as metadata
        return self._embed_text(content, title)

    def _normalize(self, title: str | None, content: str, base_iso: str | None) -> str | None:
        """Write-time relative-date normalization: return content with unambiguous
        relative dates annotated with their absolute date (original words kept), or
        None when disabled. Purely deterministic, zero LLM."""
        if not getattr(self, "time_normalize", False):
            return None
        try:
            norm = timenorm.normalize_text(content or "", base_iso)
        except Exception:
            return None
        return norm if norm and norm != (content or "") else None

    def _embed_query(self, query: str):
        if not getattr(self.embedder, "ok", False):
            return None
        v = self.embedder.embed_query(query)
        return [float(x) for x in v] if v else None

    def _cap_chars(self, content: str) -> str:
        """Enforce the configurable per-memory length limit (default 1024 chars; 0=off)."""
        limit = int(getattr(self, "memory_max_chars", 0) or 0)
        if limit > 0 and content and len(content) > limit:
            return content[:limit].rstrip()
        return content

    def _insert_with_id_retry(self, build):
        """Run a write that inserts one new memory row, retrying on an id collision.

        A plain ``INSERT`` into ``memories`` raises ``IntegrityError`` (PRIMARY KEY)
        when the freshly generated short id already exists. Because the write runs
        inside ``write_tx`` (which fully ROLLBACKs on any exception), an existing
        memory is never overwritten by a collision — the failed insert leaves the
        store untouched. Here we catch just that PRIMARY-key collision, generate a
        fresh id, and retry a bounded number of times; any other error propagates.
        """
        for attempt in range(8):
            try:
                return build(_new_id())
            except sqlite3.IntegrityError as exc:
                if "unique" not in str(exc).lower() and "primary key" not in str(exc).lower():
                    raise
                logger.warning("id collision on insert (attempt %d), regenerating id", attempt + 1)
        raise models.HitMindError(models.INTERNAL, "could not allocate a unique memory id")

    # -- ① capture ----------------------------------------------------------
    @_locked
    def capture(self, content: str, kind: str, *, title: str | None = None,
                tags: list[str] | None = None, scope: str = "shared",
                importance: float | None = None, source: str = "hook",
                created_at: str | None = None,
                topic: str | None = None,
                skip_if_duplicate: bool = False) -> dict:
        """Raw capture: insert a memory WITHOUT running the write gate.

        Used to ingest a chronological corpus verbatim (benchmarks / hook capture),
        where every turn is its own memory and dedup/versioning must NOT apply.
        Always records an exact-content dedup_hash.

        ``skip_if_duplicate=True`` (used by the per-turn autosave hook) makes an
        exact-in-scope duplicate a no-op that returns the existing id instead of
        inserting a second copy — this is the Core-level dedup gate (reusable by the
        MCP ``remember`` path too), independent of which host produced the turn.
        """
        if kind not in models.VALID_KINDS:
            raise models.HitMindError(models.INVALID_ARGUMENT, f"bad kind: {kind}")
        if not (content or "").strip():
            raise models.HitMindError(models.INVALID_ARGUMENT, "empty content")
        content = self._cap_chars(content.strip())  # enforce configurable length limit
        if importance is None:
            importance = self.kind_importance.get(kind, 0.5)  # per-kind default importance
        now = created_at or _now(self.tz_hours)
        h = content_hash(content)
        if skip_if_duplicate:
            scopewhere, scopeparams = self._scope_sql()
            dup = self.conn.execute(
                f"SELECT id FROM memories WHERE dedup_hash=? AND {scopewhere} "
                "AND status IN ('active','inactive') LIMIT 1",
                [h, *scopeparams],
            ).fetchone()
            if dup is not None:
                logger.debug("capture kind=%s -> duplicate id=%s", kind, dup["id"])
                return models.ok({"id": dup["id"], "duplicate": True})
        norm = self._normalize(title, content, now)
        emb = self._embed_text(norm or content, title)
        # topic is LLM-finalized; generation leaves it empty. cluster_id also empty
        # until pre-clustering / direct clustering runs (here, right after insert).
        tp = topic
        # un-consolidated rows get a pending_since so an eventual TTL sweep can find them
        pending_since = None if scope == "shared" and source == "manual" else now

        def _build(mid):
            with write_tx(self.conn) as cur:
                self._insert(cur, mid, mid, 1, scope, kind, tags or [], title, content,
                             importance, source, "active",
                             h, None, now, None,
                             default_valid_until(kind, now, self.ttl_days, tz_hours=self.tz_hours), emb, tp,
                             pending_since, None, topic_mod.UNCLUSTERED, normalized_text=norm)
                self._assign_cluster(cur, mid, emb, now)
            return mid

        mid = self._insert_with_id_retry(_build)
        logger.debug("capture kind=%s source=%s id=%s", kind, source, mid)
        return models.ok({"id": mid})

    # -- ② remember ---------------------------------------------------------
    @_locked
    def remember(self, content: str, kind: str, *, title: str | None = None,
                 tags: list[str] | None = None, scope: str = "shared",
                 importance: float | None = None, source: str = "manual",
                 valid_from: str | None = None, valid_until: str | None = None,
                 episode_id: str | None = None) -> dict:
        if kind not in models.VALID_KINDS:
            raise models.HitMindError(models.INVALID_ARGUMENT, f"bad kind: {kind}")
        if scope not in models.VALID_SCOPES:
            raise models.HitMindError(models.INVALID_ARGUMENT, f"bad scope: {scope}")
        if not (content or "").strip():
            raise models.HitMindError(models.INVALID_ARGUMENT, "empty content")
        content = self._cap_chars(content.strip())  # enforce configurable length limit
        if importance is None:
            importance = self.kind_importance.get(kind, 0.5)  # per-kind default importance
        tags = tags or []
        cand = {"title": title, "content": content}
        now = _now(self.tz_hours)
        valid_until = valid_until or default_valid_until(kind, now, self.ttl_days, tz_hours=self.tz_hours)
        h = content_hash(content)

        # pre-check (outside lock) to avoid useless embedding work
        with read_tx(self.conn) as cur:
            scopewhere, scopeparams = self._scope_sql()
            rows = cur.execute(
                f"SELECT * FROM memories WHERE status='active' AND kind=? AND {scopewhere}",
                [kind, *scopeparams],
            ).fetchall()
        existing = [dict(r) for r in rows]
        pre = classify(existing, cand)
        if pre["action"] == "duplicate":
            logger.debug("remember kind=%s -> duplicate id=%s", kind, pre["id"])
            return models.ok({"action": "duplicate", "id": pre["id"], "root_id": None, "version": None, "status": "active"})

        norm = self._normalize(title, content, now)
        emb = self._embed_text(norm or content, title)

        with write_tx(self.conn) as cur:
            scopewhere, scopeparams = self._scope_sql()
            rows = cur.execute(
                f"SELECT * FROM memories WHERE status='active' AND kind=? AND {scopewhere}",
                [kind, *scopeparams],
            ).fetchall()
            existing = [dict(r) for r in rows]
            decision = classify(existing, cand)  # lock-in re-check
            action = decision["action"]

            if action == "duplicate":
                return models.ok({"action": "duplicate", "id": decision["id"], "root_id": None, "version": None, "status": "active"})

            def _build(mid):
                if action in ("versioned", "inactive"):
                    # Defer versioning to the async LLM review. Store the new item as
                    # `inactive` and record the earlier candidate it resembles/conflicts with
                    # (conflict_of). Nothing is superseded here; the review task decides
                    # later whether it's really the same matter and should bump the version.
                    root_id = mid
                    version = 1
                    status = "inactive"
                    conflict_of = decision.get("conflict_of")
                    act = "inactive"  # report what actually happened (deferred to review)
                else:  # created
                    root_id = mid
                    version = 1
                    status = "active"
                    conflict_of = None
                    act = action

                self._insert(cur, mid, root_id, version, scope, kind, tags, title, content,
                             importance, source, status, h, conflict_of,
                             now, valid_from, valid_until, emb, cluster_stats=topic_mod.UNCLUSTERED,
                             episode_id=episode_id, normalized_text=norm)
                self._assign_cluster(cur, mid, emb, now)
                return act, mid, root_id, version, status, conflict_of

            action, mid, root_id, version, status, conflict_of = self._insert_with_id_retry(_build)
            warn = self._capacity_warn(cur)
        logger.debug("remember kind=%s action=%s id=%s version=%s status=%s",
                     kind, action, mid, version, status)
        if warn:
            logger.warning("capacity: %s", warn)
        return models.ok({"action": action, "id": mid, "root_id": root_id, "version": version,
                          "status": status, "conflict_of": conflict_of, "warning": warn})

    def _insert(self, cur, mid, root_id, version, scope, kind, tags, title, content,
                importance, source, status, h, conflict_of,
                now, valid_from, valid_until, emb,
                topic=None, pending_since=None, cluster_id=None, cluster_stats=None,
                episode_id=None, normalized_text=None):
        # Index the normalized text (relative dates annotated with absolute dates) when
        # present, so BM25/FTS match "6月5日" as well as the original "上周五". The raw
        # `content` is still stored verbatim in its own column (never mutated).
        index_text = normalized_text or content
        stext = tokenize.search_text(f"{title or ''} {index_text}")
        cur.execute(
            """INSERT INTO memories
            (id, root_id, version, agent_id, scope, kind, tags, title, content, search_text,
             importance, source, status, dedup_hash, superseded_by,
             conflict_of, created_at, updated_at, last_recalled_at, recalled_times,
             valid_from, valid_until, embedding, topic, pending_since,
             cluster_id, cluster_stats, episode_id, normalized_text)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,0,?,?,?,?,?,?,?,?,?)""",
            (mid, root_id, version, self.agent_id, scope, kind, json.dumps(tags), title, content, stext,
             importance, source, status, h, conflict_of, now, now,
             valid_from, valid_until, emb, topic, pending_since,
             cluster_id, cluster_stats, episode_id, normalized_text),
        )
        cur.execute("INSERT INTO memories_fts(rowid, search_text) VALUES(last_insert_rowid(), ?)", (stext,))

    def _assign_cluster(self, cur, mid: str, emb: bytes | None, now: str) -> None:
        """Pre-cluster a freshly inserted memory (single-link, conservative, capped).

        NO union-find: look at the K most-recent candidates, find the single most
        similar one above the cosine floor, and attach — nothing cascades, so a corpus
        can never collapse into one cluster. A cluster never grows past
        ``topic_max_members`` (an oversized candidate starts a new cluster instead).

        Two outcomes:
        - attach to a FINALIZED cluster (already ``clustered`` with a topic) -> the new
          memory INHERITS that topic and is ``clustered`` immediately (NO re-finalize;
          keeps a growing matter stable instead of churning).
        - otherwise -> ``pre_clustered`` (cluster_id set, topic empty). Queueing for LLM
          finalize is done lazily by ``run_topic_task``'s pull-based scan, NOT here, so
          the write path stays zero-LLM and zero-queue-write.
        """
        rows = cur.execute(
            "SELECT cluster_id, embedding FROM memories "
            "WHERE status='active' AND id<>? AND cluster_id IS NOT NULL "
            "ORDER BY rowid DESC LIMIT ?",
            (mid, self.cluster_scan_window),
        ).fetchall()
        best_cid: str | None = None
        best_sim = 0.0
        for r in rows:
            sim = topic_mod.cosine(emb, r["embedding"])
            if sim >= self.cluster_cos_floor and sim > best_sim:
                best_sim = sim
                best_cid = r["cluster_id"]
        if best_cid is not None:
            size = cur.execute(
                "SELECT COUNT(*) c FROM memories WHERE cluster_id=? AND status='active'",
                (best_cid,),
            ).fetchone()["c"]
            if size >= self.topic_max_members:
                best_cid = None  # refuse to grow past the cap -> start a new cluster
        if best_cid is None:
            # brand-new LONE memory: honest state = unclustered (not a real group yet).
            # keep cluster_id=itself so a future similar memory can find & join it.
            cur.execute(
                "UPDATE memories SET cluster_id=?, cluster_stats=? WHERE id=?",
                (mid, topic_mod.UNCLUSTERED, mid),
            )
            return
        # attaching to an existing cluster: inherit its topic if already finalized
        fin = cur.execute(
            "SELECT topic FROM memories WHERE cluster_id=? AND status='active' "
            "AND cluster_stats=? AND topic IS NOT NULL LIMIT 1",
            (best_cid, topic_mod.CLUSTERED),
        ).fetchone()
        if fin is not None:
            cur.execute(
                "UPDATE memories SET cluster_id=?, cluster_stats=?, topic=? WHERE id=?",
                (best_cid, topic_mod.CLUSTERED, fin["topic"], mid),
            )
        else:
            cur.execute(
                "UPDATE memories SET cluster_id=?, cluster_stats=? WHERE id=?",
                (best_cid, topic_mod.PRE_CLUSTERED, mid),
            )
            # the group is now >=2 -> promote the whole candidate group (incl. the lone
            # seed that was 'unclustered') to pre_clustered, so it reads as one real group
            cur.execute(
                "UPDATE memories SET cluster_stats=? "
                "WHERE cluster_id=? AND status='active' AND cluster_stats<>?",
                (topic_mod.PRE_CLUSTERED, best_cid, topic_mod.CLUSTERED),
            )

    def _record_processed(self, cur, cluster_id: str, now: str, status: str = "done",
                          fails: int | None = None) -> None:
        """Remember a cluster was finalized (done) / dropped so the pull-based idle scan
        does not immediately re-pick it. ``done`` rows keep out of the scan until the
        cluster grows (scan condition ``size > last_size`` re-admits it only then).
        ``fails`` (if given) preserves the failure count for observability (abandon).

        - no row       -> INSERT
        - existing row -> set status, refresh size, set fails
        """
        size = cur.execute(
            "SELECT COUNT(*) c FROM memories WHERE cluster_id=? AND status='active'",
            (cluster_id,),
        ).fetchone()["c"]
        f = 0 if fails is None else fails
        row = cur.execute(
            "SELECT 1 FROM topic_tasks WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO topic_tasks(cluster_id, size, cached_members, status, fails, "
                "created_at, updated_at) VALUES(?,?,NULL,?,?,?,?)",
                (cluster_id, size, status, f, now, now),
            )
        else:
            cur.execute(
                "UPDATE topic_tasks SET status=?, size=?, fails=?, updated_at=? "
                "WHERE cluster_id=?",
                (status, size, f, now, cluster_id),
            )

    # -- topic task queue (durable; idle runner processes it) ---------------
    def _topic_task_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM topic_tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    @_locked
    def run_topic_task(self) -> dict:
        """Idle task: pull-based topic finalization, decoupled thresholds.

        TRIGGER = backlog of real candidate groups: since ``cluster_stats`` semantics
        were tightened, ``pre_clustered`` now means EXACTLY "a real group of >=2 members
        awaiting finalize" (lone memories are ``unclustered``). So the trigger is simply
        ``COUNT(*) WHERE cluster_stats='pre_clustered' >= cluster_min_size`` (default 10,
        configurable) — a clean, precise backlog signal with no singleton noise.

        When triggered, pick up to ``CONSOLIDATE_BATCH_MAX`` eligible clusters (each is
        >= TOPIC_MIN_CLUSTER by construction) and LLM-finalize each. A cluster is skipped
        if the ledger says: pending retry after a failure, or already dropped at its
        current size (dropped clusters DO get another chance once they grow).

        - success            -> finalize_cluster (+corrections) + task 'done'
        - same_matter=false  -> DISSOLVE group into lone 'unclustered' singletons + 'dropped'
        - LLM failure        -> keep, fails++ (abandon to 'dropped' after QUEUE_TASK_MAX_FAILS)
        No LLM keeps everything untouched (llm_missing).
        """
        if not self.consolidate_llm_enabled:
            return models.ok({"skipped": "llm_disabled"})
        backlog = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' AND cluster_stats=?",
            (topic_mod.PRE_CLUSTERED,),
        ).fetchone()["c"]
        if backlog < self.cluster_min_size:
            return models.ok({"skipped": "below_global_backlog", "backlog": backlog})
        if getattr(self._llm, "ok", False) is not True:
            return models.ok({"skipped": "llm_missing", "backlog": backlog})
        # candidate groups == pre_clustered groups (guaranteed >=2 members by semantics)
        groups = self.conn.execute(
            "SELECT cluster_id AS cid, COUNT(*) AS n FROM memories "
            "WHERE status='active' AND cluster_stats=? AND cluster_id IS NOT NULL "
            "GROUP BY cluster_id HAVING n >= ? ORDER BY n DESC",
            (topic_mod.PRE_CLUSTERED, self.topic_min_cluster),
        ).fetchall()
        ledger = {
            r["cluster_id"]: (r["status"], int(r["size"] or 0), int(r["fails"] or 0))
            for r in self.conn.execute("SELECT cluster_id, status, size, fails FROM topic_tasks")
        }
        cids: list[str] = []
        for g in groups:
            st, sz, _fl = ledger.get(g["cid"], (None, 0, 0))
            if st == "dropped" and g["n"] <= sz:
                continue  # already dropped at this size; only retry if it grew
            cids.append(g["cid"])
            if len(cids) >= topic_mod.CONSOLIDATE_BATCH_MAX:
                break
        finalized = rejected = failed = 0
        now = _now(self.tz_hours)
        for cid in cids:
            members = self.conn.execute(
                "SELECT id, content FROM memories WHERE cluster_id=? AND status='active' "
                "ORDER BY rowid",
                (cid,),
            ).fetchall()
            texts = [m["content"] for m in members]
            if len(texts) < self.topic_min_cluster:
                with write_tx(self.conn) as cur:
                    self._record_processed(cur, cid, now, status="done")
                continue
            verdict = self._llm.finalize(texts)
            if not verdict:
                st, _sz, prev_fails = ledger.get(cid, (None, 0, 0))
                fails = prev_fails + 1
                with write_tx(self.conn) as cur:
                    if fails >= topic_mod.QUEUE_TASK_MAX_FAILS:
                        self._record_processed(cur, cid, now, status="dropped", fails=fails)
                    else:
                        row = cur.execute(
                            "SELECT 1 FROM topic_tasks WHERE cluster_id=?", (cid,)
                        ).fetchone()
                        if row is None:
                            cur.execute(
                                "INSERT INTO topic_tasks(cluster_id,size,cached_members,"
                                "status,fails,created_at,updated_at) "
                                "VALUES(?,?,NULL,'queued',?,?,?)",
                                (cid, len(texts), fails, now, now),
                            )
                        else:
                            cur.execute(
                                "UPDATE topic_tasks SET status='queued', fails=?, "
                                "updated_at=? WHERE cluster_id=?",
                                (fails, now, cid),
                            )
                failed += 1
                continue
            if verdict.get("same_matter") and verdict.get("topic"):
                self.finalize_cluster(
                    cid, verdict["topic"],
                    members=[{"id": m["id"], "content": m["content"]} for m in members],
                    corrections=verdict.get("corrections"),
                )
                with write_tx(self.conn) as cur:
                    self._record_processed(cur, cid, now, status="done")
                finalized += 1
            else:
                # not one matter -> dissolve the false group into honest lone singletons
                with write_tx(self.conn) as cur:
                    cur.execute(
                        "UPDATE memories SET cluster_stats=?, cluster_id=id "
                        "WHERE cluster_id=? AND status='active'",
                        (topic_mod.UNCLUSTERED, cid),
                    )
                    self._record_processed(cur, cid, now, status="dropped")
                rejected += 1
        remaining = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' AND cluster_stats=?",
            (topic_mod.PRE_CLUSTERED,),
        ).fetchone()["c"]
        if cids:
            logger.info("topic pass processed=%d finalized=%d rejected=%d failed=%d backlog=%d remaining=%d",
                        len(cids), finalized, rejected, failed, backlog, remaining)
        return models.ok({"processed": len(cids), "finalized": finalized,
                          "rejected": rejected, "failed": failed,
                          "backlog": backlog, "remaining_backlog": remaining})

    def queue_stats(self) -> dict:
        return models.ok({"topic_tasks": self._topic_task_counts(),
                          "llm_enabled": self.consolidate_llm_enabled})

    def _prune_history(self, cur, root_id: str, keep: int | None = None):
        keep = int(self.version_keep if keep is None else keep)
        rows = cur.execute(
            "SELECT id FROM memories WHERE root_id=? AND status<>'active' ORDER BY version DESC",
            (root_id,),
        ).fetchall()
        for r in rows[max(0, keep):]:
            cur.execute("UPDATE memories SET status='deleted' WHERE id=?", (r["id"],))

    # -- ⑧b review task (idle LLM: resolve inactive → version / corroborate / conflict) --
    def _review_backlog(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='inactive'"
        ).fetchone()["c"]

    @_locked
    def run_review_task(self) -> dict:
        """Idle task: when the inactive backlog exceeds ``review_trigger_min`` (default >3),
        LLM-review each candidate against its ``conflict_of`` prior:
          - same + update   -> bump a NEW version (prior superseded, prune history), active
          - same + no update-> corroboration: just activate the candidate (no new version)
          - not same        -> activate the candidate independently (it's a distinct memory)
          - no prior / LLM fail -> leave inactive for another pass / manual review
        Bounded per pass (REVIEW_BATCH_MAX) and never on the write hot path.
        """
        from . import topic as _t
        backlog = self._review_backlog()
        if backlog <= self.review_trigger_min:
            return models.ok({"skipped": "below_threshold", "backlog": backlog})
        if getattr(self._llm, "ok", False) is not True or not hasattr(self._llm, "review_verdict"):
            return models.ok({"skipped": "llm_missing", "backlog": backlog})
        rows = self.conn.execute(
            "SELECT id, kind, root_id, conflict_of, title, content FROM memories "
            "WHERE status='inactive' ORDER BY created_at LIMIT ?",
            (_t.REVIEW_BATCH_MAX,),
        ).fetchall()
        versioned = activated = failed = 0
        for r in rows:
            cand = r["content"] or ""
            prior_id = r["conflict_of"]
            prior = None
            if prior_id:
                pr = self.conn.execute(
                    "SELECT id, root_id, status, title, content FROM memories WHERE id=?", (prior_id,)
                ).fetchone()
                if pr is not None and pr["status"] == "active":
                    prior = pr
            if prior is None:
                # nothing live to compare -> independent memory; activate as-is
                with write_tx(self.conn) as cur:
                    cur.execute("UPDATE memories SET status='active', updated_at=? WHERE id=? AND status='inactive'",
                                (_now(self.tz_hours), r["id"]))
                activated += 1
                continue
            verdict = self._llm.review_verdict(cand, prior["content"] or "")
            if not isinstance(verdict, dict):
                failed += 1  # leave inactive, retry next pass
                continue
            now = _now(self.tz_hours)
            if verdict.get("same") and verdict.get("update"):
                # new version of the prior's root
                rid = prior["root_id"] or prior["id"]
                with write_tx(self.conn) as cur:
                    prev = cur.execute(
                        "SELECT id, version FROM memories WHERE root_id=? ORDER BY version DESC LIMIT 1", (rid,)
                    ).fetchone()
                    newver = (prev["version"] + 1) if prev else 2
                    if prev:
                        cur.execute("UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                                    (r["id"], prev["id"]))
                    cur.execute(
                        "UPDATE memories SET status='active', root_id=?, version=?, conflict_of=NULL, updated_at=? "
                        "WHERE id=? AND status='inactive'",
                        (rid, newver, now, r["id"]))
                    self._prune_history(cur, rid)
                versioned += 1
            elif verdict.get("same"):
                # corroboration: activate the candidate, leave prior untouched
                with write_tx(self.conn) as cur:
                    cur.execute("UPDATE memories SET status='active', conflict_of=NULL, updated_at=? "
                                "WHERE id=? AND status='inactive'", (now, r["id"]))
                activated += 1
            else:
                # distinct memory after all: activate independently
                with write_tx(self.conn) as cur:
                    cur.execute("UPDATE memories SET status='active', conflict_of=NULL, updated_at=? "
                                "WHERE id=? AND status='inactive'", (now, r["id"]))
                activated += 1
        logger.info("review pass processed=%d versioned=%d activated=%d failed=%d backlog=%d",
                    len(rows), versioned, activated, failed, backlog)
        return models.ok({"backlog": backlog, "processed": len(rows), "versioned": versioned,
                          "activated": activated, "failed": failed,
                          "remaining_inactive": self._review_backlog()})

    def _capacity_warn(self, cur) -> Optional[str]:
        n = cur.execute("SELECT COUNT(*) AS c FROM memories WHERE status='active'").fetchone()["c"]
        if n > self.capacity_warn:
            return f"active memories {n} exceed soft cap {self.capacity_warn} (warning only, nothing deleted)"
        return None

    def _expire_due(self) -> None:
        """Best-effort lazy transition of due rows to status=expired (no delete).

        Two sweeps:
        1. valid_until <= now → expired (kind default TTL).
        2. un-consolidated auto-capture rows older than ``raw_ttl_days`` → expired.
           This bounds growth of per-turn raw capture without ever hard-deleting
           (explicit forget() is the only delete path). Benchmarks disable it.
        """
        try:
            with write_tx(self.conn) as cur:
                now = _now(self.tz_hours)
                cur.execute(
                    "UPDATE memories SET status='expired', updated_at=? "
                    "WHERE status='active' AND valid_until IS NOT NULL AND valid_until <= ?",
                    (now, now),
                )
                if self.raw_ttl_days:
                    cutoff = lifecycle_mod._iso_seconds(
                        lifecycle_mod.now_tz(self.tz_hours) - timedelta(days=self.raw_ttl_days), self.tz_hours)
                    cur.execute(
                        "UPDATE memories SET status='expired', updated_at=? "
                        "WHERE status='active' AND pending_since IS NOT NULL AND pending_since <= ?",
                        (now, cutoff),
                    )
                # L1 episodes: TTL expiry is a PHYSICAL DELETE (they are a derivable
                # cache from state.db). Clear the dangling episode_id on their memories.
                cur.execute(
                    "DELETE FROM episodes WHERE valid_until IS NOT NULL AND valid_until <= ?",
                    (now,),
                )
                cur.execute(
                    "UPDATE memories SET episode_id=NULL WHERE episode_id IS NOT NULL "
                    "AND episode_id NOT IN (SELECT id FROM episodes)"
                )
        except Exception:
            pass

    # -- episodes (L1 layer: ONE row per session) --------------------------
    @_locked
    def add_episode(self, summary: str, *, session_id: str, mode: str = "chat",
                    trigger: str = "compaction", scope: str = "shared",
                    status: str = "active", ttl_days: int | None = None,
                    summary_kind: str = "compaction",
                    message_id_range: list[int] | None = None,
                    allow_empty: bool = False) -> dict:
        """Upsert the episode for ``session_id`` (one episode per session).

        - new session  -> INSERT, revision=1, created_at=updated_at=now
        - same session -> UPDATE summary, revision+1, updated_at=now
          (work: the Hermes cumulative summary replaces the stored one in-place;
           chat: a window batch updates the message range and may re-use the row)

        ``summary`` is the Hermes compaction summary (work) or "" for a chat window.
        ``valid_until`` = updated_at + episode_ttl_days; expiry physically deletes it.
        """
        if not (session_id or "").strip():
            raise models.HitMindError(models.INVALID_ARGUMENT, "episode needs session_id")
        if not allow_empty and not (summary or "").strip():
            raise models.HitMindError(models.INVALID_ARGUMENT, "empty episode summary")
        days = self.episode_ttl_days if ttl_days is None else int(ttl_days)
        now = _now(self.tz_hours)
        valid_until = lifecycle_mod._iso_seconds(
            lifecycle_mod.now_tz(self.tz_hours) + timedelta(days=days), self.tz_hours) if days else None
        rng = json.dumps(message_id_range) if message_id_range else None
        prev = self.last_episode(session_id)
        with write_tx(self.conn) as cur:
            if prev is None:
                eid = "ep_" + _new_id()[2:]
                cur.execute(
                    """INSERT INTO episodes(id, agent_id, scope, session_id, mode, trigger,
                       summary, summary_kind, message_id_range, revision, status,
                       created_at, updated_at, valid_until)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, self.agent_id, scope, session_id, mode, trigger,
                     (summary or "").strip(), summary_kind, rng, 1, status, now, now, valid_until),
                )
                rev = 1
            else:
                eid = prev["id"]
                rev = int(prev["revision"]) + 1
                cur.execute(
                    "UPDATE episodes SET summary=?, summary_kind=?, message_id_range=?, "
                    "revision=?, mode=?, trigger=?, status=?, updated_at=?, valid_until=? "
                    "WHERE id=?",
                    ((summary or "").strip(), summary_kind, rng, rev, mode, trigger,
                     status, now, valid_until, eid),
                )
        return models.ok({"id": eid, "revision": rev, "valid_until": valid_until})

    @_locked
    def get_episode(self, id: str) -> dict:
        row = self.conn.execute("SELECT * FROM episodes WHERE id=?", (id,)).fetchone()
        if row is None:
            raise models.HitMindError(models.NOT_FOUND, f"episode not found: {id}")
        return models.ok({"episode": dict(row)})

    @_locked
    def last_episode(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE session_id=? LIMIT 1", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    @_locked
    def list_episodes(self, *, limit: int = 50, session_id: str | None = None) -> dict:
        self._expire_due()
        q = "SELECT * FROM episodes"
        p: list = []
        if session_id:
            q += " WHERE session_id=?"
            p.append(session_id)
        q += " ORDER BY updated_at DESC LIMIT ?"
        p.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(q, p).fetchall()
        return models.ok({"items": [dict(r) for r in rows], "count": len(rows)})

    @_locked
    def ingest_compaction(self, session_id: str, summary: str, *, mode: str = "work") -> dict:
        """Compaction-driven capture (one episode per session, revisioned).

        1) upsert the episode: store the Hermes cumulative ``summary`` verbatim,
           bump ``revision`` (same row per session_id);
        2) compare to the SAME session's previous summary:
           - first compaction (revision was 0/none) -> extract memories directly;
           - otherwise ONLY what changed vs the previous summary;
        3) LLM returns 0..k memories; topic clustering groups repeats.
        """
        if not (session_id or "").strip() or not (summary or "").strip():
            return models.ok({"skipped": "missing_session_or_summary"})
        prev = self.last_episode(session_id)
        prev_summary = prev["summary"] if prev else None
        ep = self.add_episode(summary, session_id=session_id, mode=mode, trigger="compaction")["data"]
        eid = ep["id"]
        if getattr(self._llm, "ok", False) is not True:
            logger.debug("ingest_compaction session=%s skipped=no_llm", session_id)
            return models.ok({"episode_id": eid, "revision": ep["revision"],
                              "memories": 0, "skipped": "no_llm"})
        verdict = self._llm.extract_from_summary(new_summary=summary, prev_summary=prev_summary)
        if not isinstance(verdict, dict):
            logger.warning("ingest_compaction session=%s skipped=llm_failed", session_id)
            return models.ok({"episode_id": eid, "revision": ep["revision"],
                              "memories": 0, "skipped": "llm_failed"})
        created = 0
        for mem in (verdict.get("memories") or []):
            content = (mem.get("content") or "").strip()
            kind = (mem.get("kind") or "fact").strip()
            if not content or kind not in models.VALID_KINDS:
                continue
            self.remember(content, kind, title=(mem.get("title") or None),
                          source="consolidate", episode_id=eid)
            created += 1
        logger.info("ingest_compaction session=%s revision=%s first=%s mode=%s memories=%d",
                    session_id, ep["revision"], prev_summary is None, mode, created)
        return models.ok({"episode_id": eid, "revision": ep["revision"], "memories": created,
                          "first": prev_summary is None, "mode": mode})

    # -- chat batch pipeline (cursor-based; busy submit vs idle process) ----
    def session_state(self, session_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row:
            return dict(row)
        return {"session_id": session_id, "cursor_id": 0, "last_activity": None,
                "pending_batch": 0, "batch_start": 0, "fails": 0, "updated_at": None}

    def _touch_session(self, session_id: str, *, last_activity: str | None = None,
                       pending_batch: int | None = None, batch_start: int | None = None,
                       cursor_id: int | None = None, fails: int | None = None) -> None:
        now = _now(self.tz_hours)
        cur = self.session_state(session_id)
        with write_tx(self.conn) as c:
            c.execute(
                "INSERT INTO sessions(session_id, cursor_id, last_activity, pending_batch, "
                "batch_start, fails, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "cursor_id=excluded.cursor_id, last_activity=excluded.last_activity, "
                "pending_batch=excluded.pending_batch, batch_start=excluded.batch_start, "
                "fails=excluded.fails, updated_at=excluded.updated_at",
                (session_id,
                 cur["cursor_id"] if cursor_id is None else cursor_id,
                 cur["last_activity"] if last_activity is None else last_activity,
                 cur["pending_batch"] if pending_batch is None else pending_batch,
                 cur["batch_start"] if batch_start is None else batch_start,
                 cur["fails"] if fails is None else fails,
                 now),
            )

    @_locked
    def note_activity(self, session_id: str) -> dict:
        """Mark a (non-skip) chat turn: bump the activity clock and, once the configured
        number of turns has accumulated past the cursor, flag a PENDING batch.

        Key design: this ONLY creates a pending batch — it never calls the LLM. The idle
        processor does the (expensive) extraction off the hot path.
        """
        if self.state_source is None or not session_id:
            logger.debug("note_activity skip: state_source=%s session=%r", self.state_source is not None, session_id)
            return models.ok({"skipped": "no_source"})
        st = self.session_state(session_id)
        now = _now(self.tz_hours)
        if not st.get("pending_batch"):
            cursor = int(st.get("cursor_id") or 0)
            # Threshold is in TURNS (user messages), not raw transcript rows: a single turn
            # fans out into many assistant/tool rows, so counting rows flags a batch after ~1
            # turn. Prefer count_turns_after; fall back to count_after for custom/legacy sources.
            counter = getattr(self.state_source, "count_turns_after", None)
            unprocessed = counter(session_id, cursor) if callable(counter) \
                else self.state_source.count_after(session_id, cursor)
            threshold = 2 * int(self.consolidate_every_turns)
            logger.debug("note_activity sid=%s cursor=%s unprocessed=%d threshold=%d pending=%s",
                         session_id, cursor, unprocessed, threshold, unprocessed >= threshold)
            if unprocessed >= threshold:
                self._touch_session(session_id, last_activity=now, pending_batch=1,
                                    batch_start=cursor)
                logger.info("chat batch due sid=%s unprocessed=%d threshold=%d cursor=%s",
                            session_id, unprocessed, threshold, cursor)
                return models.ok({"pending": True, "unprocessed": unprocessed})
        self._touch_session(session_id, last_activity=now)
        pending = bool(self.session_state(session_id).get("pending_batch"))
        # surface the review backlog so the runner also runs the review task when due
        review_due = self._review_backlog() > self.review_trigger_min
        return models.ok({"pending": pending, "review_due": review_due})

    @_locked
    def process_pending(self) -> dict:
        """Idle-time handler for chat batches (called by the runner).

        For each session with ``pending_batch=1`` read the cursor window and extract
        memories via the host LLM. Cursor semantics guarantee no-gap/no-dup:
          - extraction SUCCESS (even 0 memories) -> commit cursor to last msg, clear pending;
          - extraction FAILURE / no-LLM -> keep cursor, leave pending, bump fails (retry).
        A still-busy session (recent activity) is deferred to the next idle window.
        """
        rows = self.conn.execute(
            "SELECT session_id FROM sessions WHERE pending_batch=1"
        ).fetchall()
        done, deferred, failed, cooling = 0, 0, 0, 0
        for r in rows:
            sid = r["session_id"]
            st = self.session_state(sid)
            if not self._is_idle(st.get("last_activity")):
                deferred += 1
                continue
            res = self.process_chat_batch(sid)
            data = res.get("data") or {}
            if data.get("committed"):
                done += 1
            elif data.get("skipped") == "cooldown":
                cooling += 1
            elif data.get("skipped") in ("no_llm", "llm_failed", "empty"):
                if data.get("skipped") != "empty":
                    failed += 1
            else:
                deferred += 1
        if rows:  # only log when there was actually a batch to consider (avoid idle spam)
            logger.info("chat_batch pass sessions=%d committed=%d deferred=%d failed=%d cooling=%d",
                        len(rows), done, deferred, failed, cooling)
        return models.ok({"committed": done, "deferred": deferred, "failed": failed,
                          "cooling": cooling, "sessions": len(rows)})

    @_locked
    def process_chat_batch(self, session_id: str) -> dict:
        """Extract one chat batch for a session and commit the cursor (or not)."""
        if self.state_source is None:
            logger.warning("chat_batch skip sid=%s reason=no_source (state_source missing)", session_id)
            return models.ok({"skipped": "no_source"})
        st = self.session_state(session_id)
        if self._in_cooldown(st):
            # in cooldown after repeated failures: keep cursor/messages, retry later
            logger.info("chat_batch skip sid=%s reason=cooldown until=%s fails=%s",
                        session_id, int(st.get("batch_start") or 0), st.get("fails"))
            return models.ok({"skipped": "cooldown", "committed": False,
                              "cooldown_until": int(st.get("batch_start") or 0)})
        cursor = int(st.get("cursor_id") or 0)
        msgs = self.state_source.rows_after(session_id, cursor, limit=400)
        nonempty = [m for m in msgs if (m.get("content") or "").strip()]
        if not nonempty:
            logger.info("chat_batch skip sid=%s reason=empty cursor=%s rows=%d",
                        session_id, cursor, len(msgs))
            self._touch_session(session_id, pending_batch=0)
            return models.ok({"skipped": "empty", "committed": True})
        msgs = nonempty
        last_id = int(msgs[-1]["id"])
        if getattr(self._llm, "ok", False) is not True:
            logger.warning("chat_batch skip sid=%s reason=no_llm (LLM unavailable) msgs=%d",
                           session_id, len(msgs))
            gave_up = self._chat_batch_fail(st, session_id, reason="no_llm")
            if gave_up is not None:
                return gave_up
            return models.ok({"skipped": "no_llm", "committed": False})
        verdict = self._llm.extract_from_messages(
            [{"role": m.get("role") or "user", "content": m.get("content") or ""} for m in msgs])
        if not isinstance(verdict, dict):
            # extraction FAILED: keep cursor + pending, count a retry (bounded via cooldown)
            logger.warning("chat_batch skip sid=%s reason=llm_failed msgs=%d verdict_type=%s",
                           session_id, len(msgs), type(verdict).__name__)
            gave_up = self._chat_batch_fail(st, session_id, reason="llm_failed")
            if gave_up is not None:
                return gave_up
            return models.ok({"skipped": "llm_failed", "committed": False})
        first_id = int(msgs[0]["id"])
        ep = self.add_episode("", session_id=session_id, mode="chat", trigger="chat_batch",
                              summary_kind="window", message_id_range=[first_id, last_id],
                              allow_empty=True)["data"]
        created = 0
        for mem in (verdict.get("memories") or []):
            content = (mem.get("content") or "").strip()
            kind = (mem.get("kind") or "fact").strip()
            if not content or kind not in models.VALID_KINDS:
                continue
            self.remember(content, kind, title=(mem.get("title") or None),
                          source="consolidate", episode_id=ep["id"])
            created += 1
        # SUCCESS (even 0 memories): commit cursor, clear pending, reset fails
        self._touch_session(session_id, cursor_id=last_id, pending_batch=0, fails=0)
        logger.info("chat_batch committed session=%s range=[%d,%d] memories=%d",
                    session_id, first_id, last_id, created)
        return models.ok({"committed": True, "memories": created, "episode_id": ep["id"],
                          "cursor_id": last_id, "range": [first_id, last_id]})

    def _chat_batch_fail(self, st: dict, session_id: str, reason: str = "llm_failed") -> dict | None:
        """Count a chat-batch failure; after CHAT_BATCH_MAX_FAILS enter a COOLDOWN.

        Crucially we NEVER advance the cursor here (that would drop messages). The window
        + cursor are kept intact; we only stop hammering the LLM: set a cooldown deadline
        (epoch seconds in ``batch_start``), keep ``pending_batch=1``, reset the retry
        count so that after the cooldown a full set of fresh attempts is allowed.
        Returns an envelope when it just failed (still retrying), or the cooldown one.
        """
        fails = int(st.get("fails") or 0) + 1
        if fails >= topic_mod.CHAT_BATCH_MAX_FAILS:
            until = int(time.time()) + max(0, self.chat_cooldown_seconds)
            self._touch_session(session_id, batch_start=until, pending_batch=1, fails=0)
            return models.ok({"skipped": "cooldown", "committed": False,
                              "reason": reason, "cooldown_until": until, "fails": fails})
        self._touch_session(session_id, fails=fails)
        return None

    def _in_cooldown(self, st: dict) -> bool:
        until = int(st.get("batch_start") or 0)
        return until > int(time.time())

    def _is_idle(self, last_activity: str | None) -> bool:
        if not last_activity:
            return True
        try:
            dt = datetime.fromisoformat(str(last_activity).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (lifecycle_mod.now_tz(self.tz_hours) - dt).total_seconds() >= self.idle_consolidate_seconds
        except Exception:
            return True

    @_locked
    def run_pending_pass(self, task: str | None = None) -> dict:
        """Run the registered background tasks now (idle runner calls this). Returns a
        per-task summary so the runner can log it."""
        out = {}
        for name, fn in self._pending_tasks.items():
            if task is not None and name != task:
                continue
            try:
                out[name] = fn().get("data")
            except Exception as exc:  # noqa: BLE001 - one task must not sink the pass
                logger.warning("pending task %s failed: %s", name, exc)
                out[name] = {"error": str(exc)}
        return models.ok({"tasks": out})

    def register_pending_task(self, name: str, fn) -> None:
        """Register an extensible idle/background task (chat batch, topic aggregation,
        and any future ones). Each fn takes no args and returns a models.ok envelope."""
        self._pending_tasks[name] = fn

    @_locked
    def flush_session(self, session_id: str) -> dict:
        """On a real session boundary: process a pending chat batch even if not idle
        (so a conversation's tail is never stranded). No-op if nothing is pending."""
        st = self.session_state(session_id)
        if st.get("pending_batch"):
            return self.process_chat_batch(session_id)
        return models.ok({"skipped": "no_pending"})

    # -- ③ update (versioned) ----------------------------------------------
    @_locked
    def update(self, id: str, *, content=None, title=None, importance=None,
               tags=None, valid_until=None) -> dict:
        with read_tx(self.conn) as cur:
            row = cur.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if row is None:
            raise models.HitMindError(models.NOT_FOUND, f"memory not found: {id}")
        if row["scope"] == "private" and row["agent_id"] != self.agent_id:
            raise models.HitMindError(models.FORBIDDEN, "private memory of another agent")
        base = _row_to_memory(row)
        new_content = content if content is not None else base["content"]
        new_title = title if title is not None else base["title"]
        # dedup guard: refuse to turn this memory into an exact copy of another
        dup = self.conn.execute(
            "SELECT id FROM memories WHERE status='active' AND dedup_hash=? AND root_id<>?",
            (content_hash(new_content), base["root_id"]),
        ).fetchone()
        if dup is not None:
            raise models.HitMindError(models.INVALID_ARGUMENT, "updated content duplicates an existing memory",
                                     {"id": dup["id"]})
        emb = self._embed_doc(new_title, new_content)
        now = _now(self.tz_hours)
        norm = self._normalize(new_title, new_content, now)
        if norm:
            emb = self._embed_text(norm, new_title)
        def _build(new_id):
            with write_tx(self.conn) as cur:
                prev = cur.execute("SELECT MAX(version) AS v FROM memories WHERE root_id=?", (base["root_id"],)).fetchone()
                version = (prev["v"] or 1) + 1
                new_tags = tags if tags is not None else base["tags"]
                # update also refreshes the clock: new version is active & new (freshness=1),
                # and for time-limited kinds push valid_until to now + TTL.
                if valid_until is not None:
                    new_vu = valid_until
                else:
                    _re = refresh_expiry(base["kind"], ttl_days=self.ttl_days, tz_hours=self.tz_hours)
                    new_vu = _re if _re is not None else base["valid_until"]
                self._insert(cur, new_id, base["root_id"], version, base["scope"], base["kind"],
                             new_tags, new_title, new_content,
                             importance if importance is not None else base["importance"],
                             base["source"], "active",
                             content_hash(new_content), None, now,
                             base["valid_from"], new_vu, emb, normalized_text=norm)
                cur.execute("UPDATE memories SET status='superseded', superseded_by=? WHERE id=?", (new_id, id))
                self._prune_history(cur, base["root_id"])
            return new_id, version

        new_id, version = self._insert_with_id_retry(_build)
        return models.ok({"action": "versioned", "id": new_id, "root_id": base["root_id"], "version": version})

    # -- ④ forget (soft delete) --------------------------------------------
    @_locked
    def forget(self, id: str) -> dict:
        with write_tx(self.conn) as cur:
            row = cur.execute("SELECT id, scope, agent_id FROM memories WHERE id=?", (id,)).fetchone()
            if row is None:
                raise models.HitMindError(models.NOT_FOUND, f"memory not found: {id}")
            if row["scope"] == "private" and row["agent_id"] != self.agent_id:
                raise models.HitMindError(models.FORBIDDEN, "private memory of another agent")
            cur.execute("UPDATE memories SET status='deleted', updated_at=? WHERE id=?", (_now(self.tz_hours), id))
        return models.ok({"id": id, "status": "deleted"})

    # -- ⑤ recall -----------------------------------------------------------
    def _rerank_stage(self, query: str, results: list) -> list:
        """Two-stage rerank: if a real reranker is wired and enabled, reorder a shortlist
        with the cross-encoder before the top_k cut. Fail-open: no reranker / disabled /
        empty response / any error -> return `results` unchanged (first-stage order)."""
        rk = getattr(self, "reranker", None)
        if not (getattr(self, "rerank_enabled", True) and rk is not None and getattr(rk, "ok", False)):
            return results
        if len(results) <= 1:
            return results
        try:
            pool = results[: max(1, int(self.rerank_pool))]
            docs = []
            for _f, m in pool:
                t = m.get("title")
                c = m.get("normalized_text") or m.get("content") or ""
                docs.append(f"{t}：{c}" if t else c)
            pairs = rk.rerank(query, docs)
        except Exception:
            return results
        if not pairs:
            return results  # endpoint failed/tripped -> keep first-stage order
        score_by_idx = {i: s for i, s in pairs if 0 <= i < len(pool)}
        order = sorted(range(len(pool)), key=lambda i: score_by_idx.get(i, float("-inf")), reverse=True)
        reranked = []
        for i in order:
            _f, m = pool[i]
            m["rerank"] = round(score_by_idx.get(i, 0.0), 4)
            reranked.append((_f, m))
        return reranked + results[len(pool):]

    @_locked
    def recall(self, query: str, *, top_k: int | None = None, filters: Optional[dict] = None) -> dict:
        filters = filters or {}
        top_k = int(top_k if top_k is not None else self.recall_top_k)
        self._expire_due()
        scopewhere, scopeparams = self._scope_sql()
        clauses = ["status='active'", scopewhere]
        params: list[Any] = [*scopeparams]
        # point-in-time validity: hide rows whose valid_until <= as-of time
        as_of = filters.get("valid_at") or _now(self.tz_hours)
        clauses.append("(valid_until IS NULL OR valid_until > ?)")
        params.append(as_of)
        if filters.get("kind"):
            clauses.append("kind IN (%s)" % ",".join("?" * len(filters["kind"])))
            params.extend(filters["kind"])
        if filters.get("scope"):
            clauses.append("scope=?")
            params.append(filters["scope"])
        if filters.get("root_id"):
            clauses.append("root_id=?")
            params.append(filters["root_id"])
        if filters.get("updated_after"):
            clauses.append("updated_at>=?")
            params.append(filters["updated_after"])
        where_sql = " AND ".join(clauses)

        qv = self._embed_query(query)
        cfg = self.retrieval_config or {}
        variants = query_rewrite.expand_queries(query) if cfg.get("query_rewrite", True) else [query]
        if len(variants) <= 1:
            raw = retrieval.search(self.conn, query, query_vec=qv, where_sql=where_sql,
                                   params=params, limit=50, embedder_ok=getattr(self.embedder, "ok", False),
                                   config=self.retrieval_config)
        else:
            # multi-query: retrieve per variant, then MERGE candidates by BEST rank
            # (min-rank union). Summing RRF dilutes a variant that pins the answer at
            # #1 when other variants miss it; the union keeps the best position.
            best_rank: dict[str, int] = {}
            for v in variants:
                vq = qv if v == query else self._embed_query(v)
                r = retrieval.search(self.conn, v, query_vec=vq, where_sql=where_sql,
                                     params=params, limit=50, embedder_ok=getattr(self.embedder, "ok", False),
                                     config=self.retrieval_config)
                for rank, (mid, _sc) in enumerate(r, start=1):
                    if mid not in best_rank or rank < best_rank[mid]:
                        best_rank[mid] = rank
            if not best_rank:
                raw = []
            else:
                ordered = sorted(best_rank.items(), key=lambda kv: kv[1])
                mx = len(ordered)
                raw = [(mid, {"rel_norm": 1.0 - (rank - 1) / mx, "score": 1.0 / rank})
                       for mid, rank in ordered]
        # tag filter in python (json array)
        want_tags = set(filters.get("tags") or [])
        results = []
        now_iso = _now(self.tz_hours)
        for mid, sc in raw:
            r = self.conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            if r is None:
                continue
            m = _row_to_memory(r)
            if want_tags and not (want_tags & set(m["tags"])):
                continue
            f = freshness(m["updated_at"], m["kind"], last_recalled_at=m["last_recalled_at"],
                          half_life_days=self.half_life_days)
            fin = final_score(sc["rel_norm"], m.get("importance", 1.0), f,
                              self.rel_weight, self.importance_weight)
            sc = {**sc, "freshness": round(f, 4), "final": round(fin, 4)}
            m["scores"] = sc
            results.append((fin, m))
        results.sort(key=lambda x: x[0], reverse=True)
        # two-stage rerank (no-op unless a real reranker is wired + enabled)
        results = self._rerank_stage(query, results)
        results = results[:top_k]

        # attach L1 episode context to each hit (the "what was going on" anchor)
        _epcache: dict[str, str] = {}
        for _f, m in results:
            eid = m.get("episode_id")
            if not eid:
                continue
            if eid not in _epcache:
                row = self.conn.execute("SELECT summary FROM episodes WHERE id=?", (eid,)).fetchone()
                _epcache[eid] = row["summary"] if row else ""
            if _epcache[eid]:
                m["episode_summary"] = _epcache[eid]

            # ---- topic expansion ----
        # Expand ONLY the topics of the first `topic_expand_hits` HIT memories (fixed by the
        # pre-expansion RRF ordering). Each expanded topic contributes AT MOST `topic_expand_cap`
        # memories TOTAL (the hit already in the result counts toward that cap), newest-first.
        # Dedupe against what we already have; after dedupe we do NOT backfill to refill the cap.
        # => with hits=3, cap=3: at most 3 topics x 3 = 9 topic memories (max +6 over the 10 base).
        if filters.get("expand_topic", True) and results:
            cap = int(filters.get("topic_expand_cap", self.topic_expand_cap))
            hits_cap = int(filters.get("topic_expand_hits", self.topic_expand_hits))
            hit_ids = {m["id"] for _f, m in results}
            base_topic_counts: dict[str, int] = {}
            for _f, m in results:
                tp = m.get("topic")
                if tp:
                    base_topic_counts[tp] = base_topic_counts.get(tp, 0) + 1
            # Topics of the FIRST `hits_cap` hit memories (fixed pre-expansion RRF prefix),
            # de-duplicated so a topic appearing more than once is expanded only once.
            expand_topics: list[str] = []
            _seen: set[str] = set()
            for _f, m in list(results)[:hits_cap]:
                tp = m.get("topic")
                if tp and tp not in _seen:
                    _seen.add(tp)
                    expand_topics.append(tp)
            for tp in expand_topics:
                quota = cap - base_topic_counts.get(tp, 0)  # remaining after hits already present
                if quota <= 0:
                    continue
                extra = self.conn.execute(
                    "SELECT * FROM memories WHERE topic=? AND status='active' "
                    "AND cluster_stats=? AND (valid_until IS NULL OR valid_until > ?) "
                    "ORDER BY updated_at DESC",   # newest first
                    (tp, topic_mod.CLUSTERED, _now(self.tz_hours)),
                ).fetchall()
                added = 0
                for r in extra:
                    if added >= quota:
                        break
                    if r["id"] in hit_ids:
                        continue
                    if r["scope"] == "private" and r["agent_id"] != self.agent_id:
                        continue
                    em = _row_to_memory(r)
                    em["scores"] = {"topic_expansion": tp, "final": None}
                    results.append((0.0, em))
                    hit_ids.add(r["id"])
                    added += 1

        # record recall activity (best-effort, short write tx).
        # Recall refreshes the decay clock (via last_recalled_at) AND, for kinds with
        # a default TTL, pushes valid_until out to now + TTL (a recalled memory is
        # "in use" and should not expire on the old schedule).
        if results:
            try:
                with write_tx(self.conn) as cur:
                    for _f, m in results:
                        new_vu = refresh_expiry(m["kind"], ttl_days=self.ttl_days, tz_hours=self.tz_hours)
                        if new_vu is not None:
                            cur.execute(
                                "UPDATE memories SET last_recalled_at=?, "
                                "recalled_times=recalled_times+1, valid_until=? WHERE id=?",
                                (now_iso, new_vu, m["id"]),
                            )
                        else:
                            cur.execute(
                                "UPDATE memories SET last_recalled_at=?, "
                                "recalled_times=recalled_times+1 WHERE id=?",
                                (now_iso, m["id"]),
                            )
            except Exception:
                pass
        logger.debug("recall top_k=%s hits=%s expand=%s", top_k, len(results),
                     bool(filters.get("expand_topic", True)))
        return models.ok({"results": [m for _f, m in results], "count": len(results)})

    # -- ⑥ get --------------------------------------------------------------
    @_locked
    def get(self, id: str, *, history: bool = False) -> dict:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if row is None:
            raise models.HitMindError(models.NOT_FOUND, f"memory not found: {id}")
        root = row["root_id"]
        if row["scope"] == "private" and row["agent_id"] != self.agent_id:
            raise models.HitMindError(models.FORBIDDEN, "private memory of another agent")
        if history:
            rows = self.conn.execute("SELECT * FROM memories WHERE root_id=? ORDER BY version", (root,)).fetchall()
            return models.ok({"versions": [_row_to_memory(r) for r in rows]})
        return models.ok({"memory": _row_to_memory(row)})

    # -- ⑦ list -------------------------------------------------------------
    @_locked
    def list(self, *, filters: Optional[dict] = None, limit: int = 50, offset: int = 0) -> dict:
        filters = filters or {}
        self._expire_due()
        scopewhere, scopeparams = self._scope_sql()
        clauses = [scopewhere]
        params: list[Any] = [*scopeparams]
        status = filters.get("status")
        if status:
            clauses.append("status=?")
            params.append(status)
        else:
            clauses.append("status<>'deleted'")
        if filters.get("kind"):
            clauses.append("kind IN (%s)" % ",".join("?" * len(filters["kind"])))
            params.extend(filters["kind"])
        where_sql = " AND ".join(clauses)
        limit = max(1, min(int(limit), 500))
        want_tags = set(filters.get("tags") or [])
        if want_tags:
            allrows = self.conn.execute(
                f"SELECT * FROM memories WHERE {where_sql} ORDER BY updated_at DESC", params
            ).fetchall()
            items = [r for r in allrows if want_tags & set(json.loads(r["tags"] or "[]"))]
            total = len(items)
            page = items[offset: offset + limit]
            nxt = offset + limit if offset + limit < total else None
            return models.ok({"items": [_row_to_memory(r) for r in page], "total": total, "next_offset": nxt})
        total = self.conn.execute(f"SELECT COUNT(*) AS c FROM memories WHERE {where_sql}", params).fetchone()["c"]
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE {where_sql} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        nxt = offset + limit if offset + limit < total else None
        return models.ok({"items": [_row_to_memory(r) for r in rows], "total": total, "next_offset": nxt})

    # -- ⑧ reviews ----------------------------------------------------------
    @_locked
    def reviews(self, *, limit: int = 50) -> dict:
        scopewhere, scopeparams = self._scope_sql()
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE status='inactive' AND {scopewhere} ORDER BY created_at DESC LIMIT ?",
            [*scopeparams, limit],
        ).fetchall()
        return models.ok({"items": [_row_to_memory(r) for r in rows], "count": len(rows)})

    # -- ⑨ review_resolve ---------------------------------------------------
    @_locked
    def review_resolve(self, id: str, resolution: str) -> dict:
        if resolution not in ("accept", "reject", "supersede"):
            raise models.HitMindError(models.INVALID_ARGUMENT, f"bad resolution: {resolution}")
        with write_tx(self.conn) as cur:
            row = cur.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
            if row is None:
                raise models.HitMindError(models.NOT_FOUND, f"memory not found: {id}")
            if row["status"] != "inactive":
                raise models.HitMindError(models.INVALID_ARGUMENT, "only inactive memories can be resolved")
            now = _now(self.tz_hours)
            if resolution == "accept":
                cur.execute("UPDATE memories SET status='active', updated_at=? WHERE id=?", (now, id))
                return models.ok({"id": id, "status": "active"})
            if resolution == "reject":
                cur.execute("UPDATE memories SET status='deleted', updated_at=? WHERE id=?", (now, id))
                return models.ok({"id": id, "status": "deleted"})
            # supersede: this inactive becomes active, the conflicting old one superseded
            target = row["conflict_of"]
            if target:
                cur.execute("UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                            (id, now, target))
            cur.execute("UPDATE memories SET status='active', updated_at=?, conflict_of=NULL WHERE id=?", (now, id))
            return models.ok({"id": id, "status": "active"})

    # -- ⑩ stats ------------------------------------------------------------
    @_locked
    def stats(self) -> dict:
        self._expire_due()
        cur = self.conn.cursor()
        by_status = {r["status"]: r["c"] for r in cur.execute("SELECT status, COUNT(*) c FROM memories GROUP BY status")}
        by_kind = {r["kind"]: r["c"] for r in cur.execute("SELECT kind, COUNT(*) c FROM memories GROUP BY kind")}
        try:
            db_bytes = os.path.getsize(self.db_path)
        except Exception:
            db_bytes = None
        return models.ok({
            "counts": {"by_status": by_status, "by_kind": by_kind},
            "db_bytes": db_bytes,
            "embedding": {"ok": getattr(self.embedder, "ok", False),
                          "model": getattr(self.embedder, "model", "none"),
                          "dim": getattr(self.embedder, "dim", None)},
            "degraded": not getattr(self.embedder, "ok", False),
        })

    # -- ⑪ consolidate (candidate-cluster verification queue) ---------------
    # -- ⑪ consolidate / finalize (LLM topic finalization) ------------------
    @_locked
    def consolidate(self, *, dry_run: bool = True) -> dict:
        """Report which pre-clustered candidate clusters are due for LLM finalization.

        Qualifying = active members >= ``TOPIC_MIN_CLUSTER`` (default 2), still
        ``pre_clustered`` (topic empty, so single-member clusters are NOT counted),
        capped at ``CONSOLIDATE_BATCH_MAX`` clusters. The actual work is done by the
        idle ``run_topic_task`` (pull-based); this is a read-only report.
        """
        min_working = self.consolidate_min_working
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' AND pending_since IS NOT NULL"
        ).fetchone()["c"]
        rows = self.conn.execute(
            "SELECT cluster_id, COUNT(*) c FROM memories "
            "WHERE status='active' AND cluster_id IS NOT NULL AND topic IS NULL "
            "AND (cluster_stats IS NULL OR cluster_stats=? ) "
            "GROUP BY cluster_id HAVING c >= ? ORDER BY c DESC LIMIT ?",
            (topic_mod.PRE_CLUSTERED, self.topic_min_cluster,
             topic_mod.CONSOLIDATE_BATCH_MAX),
        ).fetchall()
        clusters = [r["cluster_id"] for r in rows]
        if dry_run or not self.consolidate_llm_enabled:
            return models.ok({"dry_run": True, "working_pending": pending,
                              "min_working": min_working, "qualifying_clusters": len(clusters),
                              "llm_enabled": self.consolidate_llm_enabled})
        if pending < min_working and not clusters:
            return models.ok({"skipped": "below_threshold", "working_pending": pending,
                              "min_working": min_working,
                              "llm_enabled": self.consolidate_llm_enabled})
        return models.ok({"qualifying_clusters": len(clusters), "working_pending": pending,
                          "llm_enabled": self.consolidate_llm_enabled})

    @_locked
    def finalize_cluster(self, cluster_id: str, topic: str,
                         members: list[dict] | None = None,
                         corrections: list[dict] | None = None) -> dict:
        """Write the LLM-decided ``topic`` to every active member of a cluster, and
        APPLY the LLM's per-member ``corrections`` (which member is a correction/update
        of an earlier one).

        - ``topic`` is the ONLY symbolic label per matter; all active members share it
          and state flips to ``clustered`` (that is what lets recall expand same-matter).
        - ``members`` = ``[{id, content}]`` in the SAME order the LLM saw them; used to
          map a correction's 0-based ``index`` back to a real memory id.
        - ``corrections`` = ``[{index, note}]``. For each, the referenced memory is the
          newer/correct statement: its earlier false ``content_hash`` duplicate is marked
          ``superseded`` (no delete) and the corrected memory gets a prepended
          ``[更正] note`` so the correction survives recall/consolidation.
        Returns ``{"cluster_id","topic","members","corrections_applied"}``.
        """
        if not (topic or "").strip():
            raise models.HitMindError(models.INVALID_ARGUMENT, "empty topic")
        applied = 0
        with write_tx(self.conn) as cur:
            rows = cur.execute(
                "SELECT id FROM memories WHERE cluster_id=? AND status='active'", (cluster_id,)
            ).fetchall()
            if not rows:
                raise models.HitMindError(models.NOT_FOUND, f"no active memories in cluster {cluster_id}")
            cur.execute(
                "UPDATE memories SET topic=?, cluster_stats=?, updated_at=? "
                "WHERE cluster_id=? AND status='active'",
                (topic.strip(), topic_mod.CLUSTERED, _now(self.tz_hours), cluster_id),
            )
            # apply LLM corrections (index -> members[index]['id'])
            if members and corrections:
                by_index = {i: m.get("id") for i, m in enumerate(members)}
                for c in corrections:
                    if not isinstance(c, dict):
                        continue
                    try:
                        idx = int(c.get("index"))
                    except (TypeError, ValueError):
                        continue
                    target = by_index.get(idx)
                    notes = str(c.get("note") or "").strip()
                    if not target:
                        continue
                    row = cur.execute(
                        "SELECT content, dedup_hash, status FROM memories WHERE id=?", (target,)
                    ).fetchone()
                    if row is None or row["status"] != "active":
                        continue
                    # 1) explicitly supersede the OLD memory the LLM says this corrects
                    old = None
                    try:
                        old_idx = c.get("corrects")
                        if old_idx is not None:
                            old = by_index.get(int(old_idx))
                    except (TypeError, ValueError):
                        old = None
                    if old and old != target:
                        cur.execute(
                            "UPDATE memories SET status='superseded', updated_at=? "
                            "WHERE id=? AND status='active'",
                            (_now(self.tz_hours), old),
                        )
                    else:
                        # fallback: supersede an active same-text duplicate (not self)
                        cur.execute(
                            "UPDATE memories SET status='superseded', updated_at=? "
                            "WHERE dedup_hash=? AND status='active' AND id<>? AND cluster_id=?",
                            (_now(self.tz_hours), row["dedup_hash"], target, cluster_id),
                        )
                    # 2) stamp the correction note onto the newer memory (survives recall)
                    newer = row["content"]
                    if not newer.startswith("[更正]"):
                        stamped = (f"[更正] {notes} {newer}".strip() if notes else f"[更正] {newer}")
                        cur.execute(
                            "UPDATE memories SET content=?, search_text=? WHERE id=?",
                            (stamped, tokenize.search_text(stamped), target),
                        )
                    applied += 1
        return models.ok({"cluster_id": cluster_id, "topic": topic.strip(),
                          "members": len(rows), "corrections_applied": applied})

    @_locked
    def run_finalize_pass(self, *, limit: int | None = None) -> dict:
        """Backward-compatible alias: finalize queued topic tasks now. The durable queue
        (``topic_tasks``) is the source of truth; this simply runs the same idle task."""
        return self.run_topic_task()

    # -- export / import (JSON backup/restore; not an agent tool by default) --
    _EXPORT_FIELDS = ("id", "kind", "title", "content", "tags", "scope", "importance",
                      "status", "source", "topic", "cluster_stats", "version", "root_id",
                      "created_at", "updated_at", "valid_from", "valid_until")

    @_locked
    def export_memories(self, *, statuses: list[str] | None = None,
                        include_embeddings: bool = False, limit: int | None = None) -> dict:
        """Dump memories as plain dicts (JSON-serialisable). Default: all statuses."""
        use = statuses or ["active", "inactive", "superseded", "expired"]
        q = ("SELECT * FROM memories WHERE status IN (%s) ORDER BY created_at"
             % ",".join("?" * len(use)))
        params: list = list(use)
        if limit:
            q += " LIMIT ?"
            params.append(int(limit))
        out = []
        for r in self.conn.execute(q, params):
            row = {k: r[k] for k in self._EXPORT_FIELDS if k in r.keys()}
            if isinstance(row.get("tags"), str):
                row["tags"] = json.loads(row["tags"] or "[]")
            if include_embeddings and r["embedding"] is not None:
                from .vec import unpack_vec
                row["embedding"] = unpack_vec(r["embedding"])
            out.append(row)
        return models.ok({"count": len(out), "memories": out})

    @_locked
    def import_memories(self, items: list, *, mode: str = "merge") -> dict:
        """Import memories from exported dicts, with validation.

        mode='merge'   -> skip items whose id already exists (idempotent re-import)
        mode='replace' -> hard-delete ALL existing memories first, then insert
        Validation is all-or-nothing: any invalid item aborts the whole import (nothing written).
        Returns {"imported","skipped_existing","invalid":[...],"mode"}.
        """
        if mode not in ("merge", "replace"):
            raise models.HitMindError(models.INVALID_ARGUMENT, f"bad mode: {mode}")
        if not isinstance(items, list):
            raise models.HitMindError(models.INVALID_ARGUMENT, "items must be a list")
        valid_kinds, valid_scopes = set(models.VALID_KINDS), set(models.VALID_SCOPES)
        errors: list = []
        clean: list[dict] = []
        seen_ids: set = set()
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                errors.append({"index": i, "error": "not an object"})
                continue
            content = it.get("content")
            kind = it.get("kind")
            if not isinstance(content, str) or not content.strip():
                errors.append({"index": i, "error": "content empty/non-string"})
                continue
            if kind not in valid_kinds:
                errors.append({"index": i, "error": f"bad kind {kind!r}"})
                continue
            if len(content) > 100_000:  # sanity bound (raw dump, not the live cap)
                errors.append({"index": i, "error": "content too large"})
                continue
            mid = it.get("id")
            if mid is not None:
                if not isinstance(mid, str) or not mid:
                    errors.append({"index": i, "error": "bad id"})
                    continue
                if mid in seen_ids:
                    errors.append({"index": i, "error": f"duplicate id in file {mid}"})
                    continue
                seen_ids.add(mid)
            scope = it.get("scope") or "shared"
            if scope not in valid_scopes:
                errors.append({"index": i, "error": f"bad scope {scope!r}"})
                continue
            imp = it.get("importance")
            if imp is not None:
                try:
                    imp = min(1.0, max(0.0, float(imp)))
                except (TypeError, ValueError):
                    errors.append({"index": i, "error": "bad importance"})
                    continue
            tags = it.get("tags") or []
            if not isinstance(tags, list):
                errors.append({"index": i, "error": "bad tags"})
                continue
            clean.append({"index": i, "id": mid, "kind": kind, "title": it.get("title"),
                          "content": content.strip(), "tags": [str(t) for t in tags],
                          "scope": scope, "importance": imp, "source": it.get("source") or "import",
                          "status": it.get("status") or "active",
                          "valid_from": it.get("valid_from"), "valid_until": it.get("valid_until"),
                          "cluster_stats": it.get("cluster_stats") or topic_mod.UNCLUSTERED})
        if errors:
            return models.err(models.INVALID_ARGUMENT, "import validation failed", {"invalid": errors})
        imported = skipped = 0
        with write_tx(self.conn) as cur:
            if mode == "replace":
                cur.execute("DELETE FROM memories_fts")
                cur.execute("DELETE FROM memories")
            for it in clean:
                explicit = bool(it["id"])
                mid = it["id"] or _new_id()
                exists = cur.execute("SELECT 1 FROM memories WHERE id=?", (mid,)).fetchone()
                while exists and not explicit:
                    mid = _new_id()  # auto-generated id collided: pick another, never drop the import
                    exists = cur.execute("SELECT 1 FROM memories WHERE id=?", (mid,)).fetchone()
                if exists:
                    skipped += 1
                    continue
                now = _now(self.tz_hours)
                cur.execute(
                    "INSERT INTO memories (id, root_id, version, agent_id, scope, kind, tags, title, "
                    "content, search_text, importance, source, status, dedup_hash, superseded_by, "
                    "conflict_of, created_at, updated_at, last_recalled_at, recalled_times, "
                    "valid_from, valid_until, embedding, topic, pending_since, cluster_id, "
                    "cluster_stats, episode_id) VALUES "
                    "(?,?,1,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,NULL,0,?,?,NULL,NULL,NULL,NULL,?,NULL)",
                    (mid, mid, self.agent_id, it["scope"], it["kind"], json.dumps(it["tags"]),
                     it["title"], it["content"], tokenize.search_text(f"{it['title'] or ''} {it['content']}"),
                     it["importance"] if it["importance"] is not None else self.kind_importance.get(it["kind"], 0.5),
                     it["source"], it["status"], content_hash(it["content"]), now, now,
                     it["valid_from"], it["valid_until"], it["cluster_stats"]),
                )
                cur.execute("INSERT INTO memories_fts(rowid, search_text) VALUES(last_insert_rowid(), ?)",
                            (tokenize.search_text(f"{it['title'] or ''} {it['content']}"),))
                imported += 1
        return models.ok({"mode": mode, "imported": imported, "skipped_existing": skipped,
                          "invalid": []})

    # -- utility ------------------------------------------------------------
    @_locked
    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.conn.close()


def make_embedder_from_env() -> Embedder:
    base = os.environ.get("HITMIND_EMBED_BASE_URL")
    if not base:
        return NullEmbedder()
    model = os.environ.get("HITMIND_EMBED_MODEL", "embeddinggemma-2")
    key = os.environ.get("HITMIND_EMBED_API_KEY", "")
    return HttpEmbedder(base, model, key)


def make_reranker_from_env() -> Reranker:
    """Build a reranker from HITMIND_RERANK_* / HITMIND_EMBED_* env (benchmark/scripts).
    Defaults to NullReranker unless HITMIND_RERANK_MODEL (or explicit) is present."""
    from .embedder import make_reranker
    return make_reranker()
