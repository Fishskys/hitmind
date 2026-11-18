"""Offline end-to-end test for the host-agnostic daemon (no network / no embedding).

Spawns ``python -m hitmind.daemon`` and speaks JSONL over stdio, auto-answering the
reverse ``host_llm_complete`` calls. Verifies: 11 tool schemas, remember/recall,
recall_inject format, chat-batch extraction + reverse LLM bridge, and compaction
episode ingest — all with NullEmbedder (BM25 path only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn():
    db = os.path.join(tempfile.mkdtemp(prefix="hitmind-daemon-test-"), "hitmind.db")
    env = dict(os.environ)
    # Force offline: no embedding endpoint -> NullEmbedder; rerank off.
    env.pop("HITMIND_EMBED_BASE_URL", None)
    env["HITMIND_RERANK_ENABLED"] = "0"
    env["HITMIND_SYNC_CONSOLIDATE_EVERY"] = "1"
    env["HITMIND_IDLE_CONSOLIDATE_SECONDS"] = "0"
    env["HITMIND_IDLE_POLL_SECONDS"] = "15"
    env["HITMIND_LOG_LEVEL"] = "ERROR"
    proc = subprocess.Popen(
        [sys.executable, "-m", "hitmind.daemon", "--db", db, "--agent-id", "dshtest"],
        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    return proc


class _Client:
    def __init__(self, proc):
        self.proc = proc
        self.pending: dict = {}
        self.lock = threading.Lock()
        self.llm_calls = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self._seq = 0

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("method") == "host_llm_complete":
                self.llm_calls += 1
                parsed = {"memories": [{"kind": "fact", "content": f"E2E 抽取记忆 #{self.llm_calls}"}],
                          "same": False, "update": False, "same_matter": False,
                          "topic": "", "corrections": []}
                self.proc.stdin.write(json.dumps({"id": msg["id"], "result": {"parsed": parsed}}) + "\n")
                self.proc.stdin.flush()
            else:
                with self.lock:
                    self.pending[msg.get("id")] = msg

    def _read_err(self):
        for _ in self.proc.stderr:
            pass

    def call(self, method, params=None, timeout=30):
        self._seq += 1
        rid = str(self._seq)
        self.proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                if rid in self.pending:
                    return self.pending.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(method)


def test_daemon_end_to_end():
    proc = _spawn()
    c = _Client(proc)
    try:
        r = c.call("ping")
        assert r["result"]["ok"] is True
        assert len(r["result"]["tools"]) == 11

        r = c.call("get_schemas")
        assert len(r["result"]["schemas"]) == 11

        r = c.call("tool", {"name": "hitmind_remember",
                            "args": {"content": "用户偏好深色主题用于夜间阅读", "kind": "persona"}})
        assert r["result"]["ok"] is True

        r = c.call("tool", {"name": "hitmind_recall", "args": {"query": "深色主题"}})
        assert r["result"]["ok"] is True
        assert len(r["result"]["data"]["results"]) >= 1

        r = c.call("recall_inject", {"query": "深色主题"})
        inj = r["result"]
        assert inj["ok"] and inj["count"] >= 1
        assert inj["lines"][0].startswith("[m_")
        assert "：(persona" not in inj["lines"][0]  # sanity: format uses "] (kind)[" shape
        assert "(persona)[@" in inj["lines"][0]

        # chat-batch extraction through the reverse LLM bridge
        sid = "sess-e2e"
        for i in range(2):
            c.call("append_turn", {"session_id": sid, "role": "user", "content": f"我喜欢中文，第{i}条"})
            c.call("append_turn", {"session_id": sid, "role": "assistant", "content": f"收到第{i}条"})
        c.call("note_turn", {"session_id": sid})
        c.call("run_pending", {"task": "chat_batch"}, timeout=40)
        r = c.call("tool", {"name": "hitmind_list", "args": {"limit": 20}})
        items = r["result"]["data"]["items"]
        assert any("E2E 抽取记忆" in (it.get("content") or "") for it in items)
        assert c.llm_calls >= 1

        r = c.call("ingest_compaction", {"session_id": sid,
                                         "summary": "[CONTEXT COMPACTION] 用户是程序员，偏好简洁。",
                                         "mode": "work", "compaction_id": "ck-1"}, timeout=40)
        assert r["result"]["ok"] is True
        assert "episode_id" in r["result"]["data"]
        assert r["result"]["data"].get("strategy") == "full"

        # dedupe: the same compactionId must NOT re-extract (idempotent)
        r2 = c.call("ingest_compaction", {"session_id": sid,
                                          "summary": "[CONTEXT COMPACTION] 用户是程序员，偏好简洁。",
                                          "mode": "work", "compaction_id": "ck-1"}, timeout=40)
        assert r2["result"]["data"].get("skipped") == "duplicate_compaction"

        r = c.call("tool", {"name": "hitmind_stats", "args": {}})
        assert r["result"]["ok"] is True
    finally:
        try:
            c.call("shutdown")
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
