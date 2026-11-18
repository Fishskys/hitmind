"""Host-agnostic JSON-RPC sidecar for HitMind (used by non-Hermes hosts, e.g. DSH).

Why a sidecar: HitMind's core is Python + SQLite, but hosts like DeepSeek Harness
(DSH) are Node/TypeScript and have no Python memory-provider hook. The TS plugin
spawns this daemon as a child process and speaks newline-delimited JSON (JSONL) over
stdio. The daemon owns the Engine; the plugin owns the host surface (tools, auto
injection, capture events). LLM work (offline extraction / topic finalize) is routed
BACK to the host via a reverse call (``host_llm_complete``) so HitMind never talks to
a provider directly and never holds an API key.

Process layout (inside the daemon):
  serve_engine  — handles tool calls + recall + note_activity (fast, interactive)
  worker_engine — runs the idle runner (chat-batch extraction, topic, review). A
                  separate Engine (own connection + lock) so a slow LLM extraction
                  never blocks an interactive recall. Both share the same SQLite file
                  (WAL + busy_timeout) and a shared DaemonTurnStore.

Wire protocol (one JSON object per line):
  host -> daemon request : {"id":"1","method":"<m>","params":{...}}
  daemon -> host response: {"id":"1","result":{...}}   or   {"id":"1","error":{"code","message"}}
  daemon -> host reverse : {"id":"r1","method":"host_llm_complete","params":{...}}
  host -> daemon reverse response: {"id":"r1","result":{"parsed":{...}}}

Methods:
  ping, get_schemas, tool{name,args}, recall_inject{query,top_k},
  append_turn{session_id,role,content}, note_turn{session_id,...},
  ingest_compaction{session_id,summary,mode}, run_pending{task?}, shutdown
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .adapters.hermes.llm import HostClusterLLM
from .adapters.hermes.tasks import BackgroundTaskRunner
from .core import models
from .core.engine import Engine, make_embedder_from_env, make_reranker_from_env
from .tools import _TOOL_SCHEMAS, dispatch

logger = logging.getLogger("hitmind.daemon")


# --------------------------------------------------------------------------- #
# Turn store: a StateDBSource-compatible view over tables in the HitMind DB.
# Lets the existing chat-batch pipeline (note_activity / process_pending) run
# without a Hermes state.db: capture appends turns here; the engine reads windows.
# --------------------------------------------------------------------------- #
class DaemonTurnStore:
    def __init__(self, db_path: str):
        import sqlite3
        from pathlib import Path as _Path
        self.ok = True
        # ensure the parent dir exists and tighten permissions (owner-only),
        # matching core.connect(): the memory DB is sensitive.
        try:
            from .core.db import prepare_db_path as _prepare_db_path
            _prepare_db_path(db_path)
        except Exception:
            try:
                _Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        # separate connection so its writes don't contend with the engines' locks
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            import os as _os
            _os.chmod(db_path, 0o600)
        except Exception:
            pass
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS host_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT,
                    content TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_host_messages_sess ON host_messages(session_id, id);
                CREATE TABLE IF NOT EXISTS host_summaries(
                    session_id TEXT PRIMARY KEY,
                    summary TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS host_compaction_seen(
                    compaction_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    revision INTEGER,
                    summary_len INTEGER,
                    created_at TEXT
                );
                """
            )

    def append(self, session_id: str, role: str, content: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO host_messages(session_id, role, content, active) VALUES(?,?,?,1)",
                (session_id, role or "user", content or ""),
            )
            return int(cur.lastrowid)

    def set_summary(self, session_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO host_summaries(session_id, summary, updated_at) VALUES(?,?,datetime('now')) "
                "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                (session_id, summary or ""),
            )

    # -- StateDBSource interface ------------------------------------------- #
    def latest_summary(self, session_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM host_summaries WHERE session_id=?", (session_id,)
            ).fetchone()
        s = (row["summary"] if row else "") or ""
        return s or None

    def rows_after(self, session_id: str, after_id: int = 0, limit: int = 400) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content FROM host_messages "
                "WHERE session_id=? AND id>? AND active=1 ORDER BY id LIMIT ?",
                (session_id, after_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_after(self, session_id: str, after_id: int = 0) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM host_messages WHERE session_id=? AND id>? AND active=1",
                (session_id, after_id),
            ).fetchone()
        return int(row["c"]) if row else 0

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # -- compaction dedupe (idempotent per compaction_id) ------------------ #
    def seen_compaction(self, compaction_id: str) -> bool:
        if not compaction_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM host_compaction_seen WHERE compaction_id=?", (compaction_id,)
            ).fetchone()
        return row is not None

    def mark_compaction(self, compaction_id: str, session_id: str, revision: int, summary_len: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO host_compaction_seen"
                "(compaction_id, session_id, revision, summary_len, created_at) "
                "VALUES(?,?,?,?,datetime('now'))",
                (compaction_id, session_id, revision, summary_len),
            )


# --------------------------------------------------------------------------- #
# Reverse LLM bridge: HostClusterLLM -> TS plugin -> host ctx.llm.
# --------------------------------------------------------------------------- #
class HostLLMClient:
    """Implements the duck-typed ``plugin_llm`` that HostClusterLLM expects:
    ``complete_structured(instructions, input, json_mode, task?) -> obj.parsed``.

    Every call issues a reverse RPC to the TS host and blocks (bounded) for the
    reply. ``ok`` stays True as long as the transport exists; HostClusterLLM handles
    failures by returning None (its callers keep cursors / retry).
    """

    def __init__(self, send_reverse, timeout_s: float = 120.0):
        self._send_reverse = send_reverse  # callable(params:dict, timeout:float) -> dict
        self.ok = True
        self.timeout_s = timeout_s

    def complete_structured(self, *, instructions: str, input: list, json_mode: bool = True,
                            task: str | None = None, **kwargs: Any):
        params = {"instructions": instructions, "input": input,
                  "json_mode": bool(json_mode), "task": task}
        try:
            reply = self._send_reverse(params, self.timeout_s)
        except Exception as exc:  # transport timeout / host error
            logger.debug("host_llm_complete failed: %s", exc)
            raise
        parsed = reply.get("parsed") if isinstance(reply, dict) else None
        return SimpleNamespace(parsed=parsed, usage=(reply or {}).get("usage"))


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #
def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _build_engine(db_path: str, agent_id: str, *, with_runner_tasks: bool) -> Engine:
    # Tunables from the single source of truth; daemon runs bare (no config.json),
    # so every value comes from HITMIND_* env (the plugin bridges all fields there) or the spec default.
    import os as _os

    from .settings_spec import BY_KEY as _SPEC_BY_KEY
    from .settings_spec import build_engine_kwargs

    def _resolve(key: str):
        st = _SPEC_BY_KEY[key]
        raw = _os.environ.get(st.env)
        return raw if raw is not None else st.default

    engine = Engine(
        db_path,
        agent_id=agent_id,
        embedder=make_embedder_from_env(),
        reranker=make_reranker_from_env(),
        **build_engine_kwargs(_resolve),
    )
    if with_runner_tasks:
        engine.register_pending_task("chat_batch", engine.process_pending)
        engine.register_pending_task("topic", engine.run_topic_task)
        engine.register_pending_task("review", engine.run_review_task)
    return engine


class Daemon:
    def __init__(self, db_path: str, agent_id: str = "dsh"):
        self.db_path = db_path
        self.agent_id = agent_id
        self._out_lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._rev_seq = 0
        self._stop = threading.Event()

        # shared turn store (StateDBSource-compatible) over the hitmind db
        self.store = DaemonTurnStore(db_path)

        # capture policy (same as the plugin's; defensive backstop). reads HITMIND_* env.
        from .capture_policy import parse_contexts as _pc
        from .capture_policy import parse_roles as _pr
        self._sync_roles = _pr(os.environ.get("HITMIND_SYNC_ROLES", "user"))
        self._skip_contexts = _pc(os.environ.get("HITMIND_SKIP_CONTEXTS", ""))
        try:
            self._user_min = int(os.environ.get("HITMIND_SYNC_USER_MIN", "5"))
        except (TypeError, ValueError):
            self._user_min = 5
        try:
            self._assistant_min = int(os.environ.get("HITMIND_SYNC_ASSISTANT_MIN", "10"))
        except (TypeError, ValueError):
            self._assistant_min = 10

        # interactive engine (tools/recall) + worker engine (idle extraction)
        self.serve = _build_engine(db_path, agent_id, with_runner_tasks=False)
        self.worker = _build_engine(db_path, agent_id, with_runner_tasks=True)
        self.serve.state_source = self.store
        self.worker.state_source = self.store

        # reverse LLM bridge wired into BOTH engines' offline paths
        try:
            _llm_timeout = float(os.environ.get("HITMIND_LLM_TIMEOUT_S", "120"))
        except (TypeError, ValueError):
            _llm_timeout = 120.0
        self.host_llm = HostLLMClient(self._reverse_llm, timeout_s=_llm_timeout)
        self.serve._llm = HostClusterLLM(self.host_llm)
        self.worker._llm = HostClusterLLM(self.host_llm)
        for e in (self.serve, self.worker):
            if e._llm is not None:
                e._llm.memory_max_chars = e.memory_max_chars

        # idle runner drives chat-batch / topic / review on the worker engine.
        # Gated like Hermes: only when bg_runner is on AND idle consolidation is enabled.
        self._auto_runner = os.environ.get("HITMIND_BG_RUNNER", "1") not in ("0", "false", "False", "")
        self._idle_enabled = int(getattr(self.worker, "idle_consolidate_seconds", 0) or 0) > 0
        self.runner = None
        if self._auto_runner and self._idle_enabled:
            self.runner = BackgroundTaskRunner(self.worker,
                                               poll_seconds=int(self.worker.idle_poll_seconds))

    # -- wire I/O ----------------------------------------------------------- #
    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _respond(self, rid, result=None, error=None) -> None:
        msg = {"id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self._write(msg)

    def _reverse_llm(self, params: dict, timeout: float) -> dict:
        with self._pending_lock:
            self._rev_seq += 1
            rid = f"r{self._rev_seq}"
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._write({"id": rid, "method": "host_llm_complete", "params": params})
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"host_llm_complete timed out after {timeout}s")
        with self._pending_lock:
            self._pending.pop(rid, None)
        if slot["error"] is not None:
            raise RuntimeError(str(slot["error"]))
        return slot["result"] or {}

    def _resolve_reverse(self, msg: dict) -> None:
        rid = msg.get("id")
        with self._pending_lock:
            slot = self._pending.get(rid)
            if slot is None:
                return
            slot["result"] = msg.get("result")
            slot["error"] = msg.get("error")
            slot["event"].set()

    # -- request handlers --------------------------------------------------- #
    def handle(self, method: str, params: dict) -> dict:
        params = params or {}
        if method == "ping":
            return {"ok": True, "version": _version(), "agent_id": self.agent_id,
                    "tools": [s["name"] for s in _TOOL_SCHEMAS]}
        if method == "get_schemas":
            return {"schemas": _TOOL_SCHEMAS}
        if method == "tool":
            env = dispatch(self.serve, params.get("name"), params.get("args") or {})
            return env
        if method == "recall_inject":
            return self._recall_inject(params)
        if method == "append_turn":
            from .capture_policy import should_capture as _sc
            role = params.get("role", "user")
            ok, clean = _sc(
                role, params.get("content", ""),
                roles=self._sync_roles, user_min=self._user_min,
                assistant_min=self._assistant_min, context=params.get("context"),
                skip_contexts=self._skip_contexts, sanitizer=lambda x: x,
            )
            if not ok:
                return {"ok": True, "skipped": "policy"}
            mid = self.store.append(params["session_id"], role, clean)
            return {"ok": True, "id": mid}
        if method == "note_turn":
            return self.serve.note_activity(params["session_id"])
        if method == "ingest_compaction":
            return self._ingest_compaction(params)
        if method == "run_pending":
            return self.worker.run_pending_pass(params.get("task"))
        if method == "shutdown":
            self._stop.set()
            return {"ok": True}
        return models.err(models.NOT_FOUND, f"unknown method: {method}")

    def _ingest_compaction(self, params: dict) -> dict:
        """DSH/Native compaction capture — route-1 orchestration (does NOT change core).

        DSH's compaction summary is a *folded checkpoint* (merges older summaries, drops
        stale facts, covers only the shadowed earlier range). Running the core's
        ``ingest_compaction`` delta ('new vs previous summary') on it is wrong: it would
        re-extract facts that stayed true and miss rewritten ones. So we orchestrate the
        core's existing atomic methods directly:

          1) dedupe by ``compactionId`` (idempotent: repeated events don't double-extract);
          2) ``add_episode`` stores the (folded) summary verbatim (one revisioned row/session);
          3) ``extract_from_summary(summary, prev_summary=None)`` -> FULL extraction (no delta);
          4) ``remember`` each durable memory, linked to the episode.

        Only the daemon's own dedupe table is new; ``hitmind.core`` is untouched.
        """
        sid = (params.get("session_id") or "").strip()
        summary = params.get("summary") or ""
        mode = params.get("mode", "work")
        compaction_id = str(params.get("compaction_id") or "").strip()
        if not sid or not (summary or "").strip():
            return models.ok({"skipped": "missing_session_or_summary"})
        if compaction_id and self.store.seen_compaction(compaction_id):
            return models.ok({"skipped": "duplicate_compaction", "compaction_id": compaction_id})

        self.store.set_summary(sid, summary)
        eng = self.worker
        ep = eng.add_episode(summary, session_id=sid, mode=mode, trigger="compaction")["data"]
        eid = ep["id"]
        rev = ep.get("revision")

        if getattr(eng._llm, "ok", False) is not True:
            if compaction_id:
                self.store.mark_compaction(compaction_id, sid, rev, len(summary))
            return models.ok({"episode_id": eid, "revision": rev, "memories": 0,
                              "skipped": "no_llm", "compaction_id": compaction_id})

        # FULL extraction: prev_summary=None skips the delta compare (DSH summaries fold).
        verdict = eng._llm.extract_from_summary(new_summary=summary, prev_summary=None)
        if not isinstance(verdict, dict):
            # do NOT mark seen -> a retry can re-extract this same compaction
            return models.ok({"episode_id": eid, "revision": rev, "memories": 0,
                              "skipped": "llm_failed", "compaction_id": compaction_id})

        created = 0
        for mem in (verdict.get("memories") or []):
            content = (mem.get("content") or "").strip()
            kind = (mem.get("kind") or "fact").strip()
            if not content or kind not in models.VALID_KINDS:
                continue
            eng.remember(content, kind, title=(mem.get("title") or None),
                         source="consolidate", episode_id=eid)
            created += 1
        if compaction_id:
            self.store.mark_compaction(compaction_id, sid, rev, len(summary))
        return models.ok({"episode_id": eid, "revision": rev, "memories": created,
                          "strategy": "full", "compaction_id": compaction_id, "mode": mode})

    def _recall_inject(self, params: dict) -> dict:
        query = (params.get("query") or "").strip()
        if not query:
            return {"ok": True, "count": 0, "lines": []}
        try:
            res = self.serve.recall(query, top_k=int(params.get("top_k") or self.serve.recall_top_k))
        except Exception as exc:  # fail-open: never break a turn
            logger.warning("recall_inject failed: %s", exc)
            return {"ok": False, "count": 0, "lines": [], "error": str(exc)}
        items = (res.get("data") or {}).get("results") or []
        lines = []
        for m in items:
            mid = m.get("id")
            if not mid:
                continue
            kind = m.get("kind") or ""
            created = (m.get("created_at") or "")[:10]
            content = (m.get("content") or "").replace("\n", " ").strip()
            lines.append(f"[{mid}] ({kind})[@{created}]：{content}".strip())
        return {"ok": True, "count": len(lines), "lines": lines}

    # -- main loop ---------------------------------------------------------- #
    def serve_forever(self) -> int:
        if self.runner is not None:
            self.runner.start()
        logger.info("hitmind daemon ready (db=%s agent=%s)", self.db_path, self.agent_id)
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception as exc:
                    logger.warning("bad json line: %s", exc)
                    continue
                if "method" in msg and msg.get("method"):
                    threading.Thread(target=self._dispatch, args=(msg,), daemon=True).start()
                else:
                    self._resolve_reverse(msg)
                if self._stop.is_set():
                    break
        finally:
            self._shutdown()
        return 0

    def _dispatch(self, msg: dict) -> None:
        rid = msg.get("id")
        try:
            result = self.handle(msg.get("method"), msg.get("params") or {})
            # result of most handlers is already an envelope {ok:..}; wrap as-is
            self._respond(rid, result=result)
        except models.HitMindError as exc:
            self._respond(rid, error={"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler error")
            self._respond(rid, error={"code": "internal", "message": str(exc)})

    def _shutdown(self) -> None:
        try:
            if self.runner is not None:
                self.runner.stop()
        except Exception:
            pass
        for e in (self.serve, self.worker):
            try:
                e.close()
            except Exception:
                pass
        try:
            self.store.close()
        except Exception:
            pass


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0"


def _db_path(arg: str | None) -> str:
    if arg:
        return arg
    # Explicit override wins (rare). Otherwise a stable per-host default so the user
    # never configures a path: DSH keeps its memory under ~/.dsh.
    explicit = os.environ.get("HITMIND_DB")
    if explicit:
        return explicit
    home = os.environ.get("HITMIND_HOME") or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return str(Path(home) / "hitmind" / "data" / "hitmind.db")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("hitmind-daemon")
    ap.add_argument("--db", default=None)
    ap.add_argument("--agent-id", default=os.environ.get("HITMIND_AGENT_ID", "dsh"))
    ap.add_argument("--log-level", default=os.environ.get("HITMIND_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daemon = Daemon(_db_path(args.db), agent_id=args.agent_id)
    return daemon.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
