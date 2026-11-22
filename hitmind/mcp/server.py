"""Minimal MCP server for HitMind.

Uses the official ``mcp`` SDK 2.x (``MCPServer``, pinned ``mcp>=2.0.0,<3``;
first stable 2.0.0 released 2026-07-28) over stdio by default.
Exposes 11 memory tools (same set as the Hermes plugin: 9 core + export/import).
``--selftest`` runs a core round-trip without the SDK.

Tools are declared with explicit signatures so the SDK can generate correct
JSON schemas (a **kwargs catch-all would break arg validation).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from ..core import models
from ..core.engine import Engine, make_embedder_from_env
from ..logging_setup import MCP, get_logger
from ..logging_setup import configure as configure_logging

logger = get_logger(MCP)


def _db_path(arg: str | None) -> str:
    if arg:
        return arg
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(Path(home) / "hitmind" / "data" / "hitmind.db")


def _envelope(engine: Engine, fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except models.HitMindError as exc:
        return json.dumps(exc.to_envelope(), ensure_ascii=False)
    except Exception as exc:  # fail-open shape; adapters surface INTERNAL
        return json.dumps(models.err(models.INTERNAL, str(exc)), ensure_ascii=False)


def build_server(engine: Engine):
    """Build an MCPServer (mcp 2.x) with 11 explicitly-typed tools."""
    from mcp.server import MCPServer

    app = MCPServer("hitmind")

    @app.tool()
    def hitmind_remember(content: str, kind: str, title: Optional[str] = None,
                         tags: Optional[list[str]] = None, scope: str = "shared",
                         importance: Optional[float] = None) -> str:
        """Store a memory unit (persona/fact/event/procedure). Runs the write gate."""
        return _envelope(engine, engine.remember, content, kind, title=title,
                         tags=tags, scope=scope, importance=importance)

    @app.tool()
    def hitmind_recall(query: str, top_k: int = 5, kind: Optional[list[str]] = None,
                       tags: Optional[list[str]] = None) -> str:
        """Search memories (dense+BM25+RRF, relevance threshold). Returns top_k."""
        return _envelope(engine, engine.recall, query, top_k=top_k,
                         filters={"kind": kind, "tags": tags})

    @app.tool()
    def hitmind_get(id: str, history: bool = False) -> str:
        """Fetch a memory by id (history=true returns the version chain)."""
        return _envelope(engine, engine.get, id, history=history)

    @app.tool()
    def hitmind_update(id: str, content: Optional[str] = None, title: Optional[str] = None) -> str:
        """Versioned update (never overwrites; keeps up to version_keep history versions, default 5)."""
        return _envelope(engine, engine.update, id, content=content, title=title)

    @app.tool()
    def hitmind_forget(id: str) -> str:
        """Soft-delete (archive) a memory by id. No hard delete."""
        return _envelope(engine, engine.forget, id)

    @app.tool()
    def hitmind_list(kind: Optional[list[str]] = None, status: Optional[str] = None,
                     limit: int = 50, offset: int = 0) -> str:
        """Browse memories with filters and pagination."""
        return _envelope(engine, engine.list, filters={"kind": kind, "status": status},
                         limit=limit, offset=offset)

    @app.tool()
    def hitmind_reviews(limit: int = 50) -> str:
        """List memories pending review (status=inactive)."""
        return _envelope(engine, engine.reviews, limit=limit)

    @app.tool()
    def hitmind_review_resolve(id: str, resolution: str) -> str:
        """Resolve an inactive memory: accept | reject | supersede."""
        return _envelope(engine, engine.review_resolve, id, resolution)

    @app.tool()
    def hitmind_stats() -> str:
        """Memory counts / health / embedding status."""
        return _envelope(engine, engine.stats)

    @app.tool()
    def hitmind_export(statuses: Optional[list[str]] = None,
                       include_embeddings: bool = False) -> str:
        """Export memories as JSON (backup). Returns {memories:[...]}."""
        return _envelope(engine, engine.export_memories,
                         statuses=statuses, include_embeddings=include_embeddings)

    @app.tool()
    def hitmind_import(memories: list[dict], mode: str = "merge") -> str:
        """Import memories from JSON (validated). mode=merge|replace."""
        return _envelope(engine, engine.import_memories, memories, mode=mode)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("hitmind-mcp")
    ap.add_argument("--db", default=None)
    ap.add_argument("--agent-id", default=os.environ.get("HITMIND_AGENT_ID", "mcp"))
    ap.add_argument("--selftest", action="store_true", help="run a quick core round-trip and exit")
    args = ap.parse_args(argv)

    db_path = _db_path(args.db)
    # Standalone MCP server: stdout is the MCP protocol channel, so logs go to a local
    # rotating file beside the DB (NOT stdout). stderr mirror kept off to avoid noise.
    configure_logging(level=None, db_path=db_path, stderr=False, component=MCP)
    logger.info("hitmind mcp server starting (agent=%s)", args.agent_id)

    engine = Engine(db_path, agent_id=args.agent_id, embedder=make_embedder_from_env())

    if args.selftest:
        r = engine.remember("HitMind selftest memory about paper towels", "fact", title="selftest")
        print("selftest remember:", json.dumps(r, ensure_ascii=False))
        print("selftest recall:", json.dumps(engine.recall("paper towels", top_k=3), ensure_ascii=False)[:300])
        engine.close()
        return 0

    app = build_server(engine)
    try:
        app.run()  # stdio by default
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
