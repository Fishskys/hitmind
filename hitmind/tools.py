"""Host-agnostic tool definitions for HitMind.

Single source of truth for the 11 memory tools: their JSON schemas (``_TOOL_SCHEMAS``)
and their engine dispatch lambdas (``_TOOL_MAP``). Used by:

- the Hermes memory-provider adapter (``hitmind.adapters.hermes``), and
- the host-agnostic JSON-RPC daemon (``hitmind.daemon``) used by non-Hermes hosts (DSH).

Nothing here imports a host SDK, so it is safe to import from any adapter.
"""

from __future__ import annotations

from .core import models

_TOOL_SCHEMAS = [
    {"name": "hitmind_remember", "description": "Store a memory unit (persona/fact/event/procedure). Runs the write gate (dedupe/conflict/version).",
     "parameters": {"type": "object", "properties": {
         "content": {"type": "string"},
         "kind": {"type": "string", "enum": list(models.VALID_KINDS)},
         "title": {"type": "string"},
         "tags": {"type": "array", "items": {"type": "string"}},
         "scope": {"type": "string", "enum": list(models.VALID_SCOPES)},
         "importance": {"type": "number"},
     }, "required": ["content", "kind"]}},
    {"name": "hitmind_recall", "description": "Search memories (dense+BM25+RRF, relevance threshold). Returns top_k.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "top_k": {"type": "integer"},
         "kind": {"type": "array", "items": {"type": "string"}},
         "tags": {"type": "array", "items": {"type": "string"}},
     }, "required": ["query"]}},
    {"name": "hitmind_get", "description": "Fetch a memory by id (history=true for the version chain).",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "history": {"type": "boolean"}}, "required": ["id"]}},
    {"name": "hitmind_update", "description": "Versioned update (never overwrites; keeps up to version_keep history versions, default 5).",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}}, "required": ["id"]}},
    {"name": "hitmind_forget", "description": "Soft-delete (archive) a memory by id. No hard delete.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "hitmind_list", "description": "Browse memories with filters and pagination.",
     "parameters": {"type": "object", "properties": {"kind": {"type": "array", "items": {"type": "string"}}, "status": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}}},
    {"name": "hitmind_reviews", "description": "List memories pending review (status=inactive).",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "hitmind_review_resolve", "description": "Resolve an inactive memory: accept | reject | supersede.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "resolution": {"type": "string", "enum": ["accept", "reject", "supersede"]}}, "required": ["id", "resolution"]}},
    {"name": "hitmind_stats", "description": "Memory counts / health / embedding status.", "parameters": {"type": "object", "properties": {}}},
    {"name": "hitmind_export",
     "description": "Export memories as JSON (backup). Returns {memories:[...]}.",
     "parameters": {"type": "object", "properties": {
         "statuses": {"type": "array", "items": {"type": "string"}},
         "include_embeddings": {"type": "boolean"}}}},
    {"name": "hitmind_import",
     "description": "Import memories from JSON (validated). mode=merge|replace.",
     "parameters": {"type": "object", "properties": {
         "memories": {"type": "array", "items": {"type": "object"}},
         "mode": {"type": "string", "enum": ["merge", "replace"]}}, "required": ["memories"]}},
]

_TOOL_MAP = {
    "hitmind_remember": lambda e, a: e.remember(a["content"], a["kind"], title=a.get("title"), tags=a.get("tags"),
                                               scope=a.get("scope", "shared"), importance=a.get("importance", 0.5)),
    "hitmind_recall": lambda e, a: e.recall(a["query"], top_k=a.get("top_k", 5),
                                           filters={"kind": a.get("kind"), "tags": a.get("tags")}),
    "hitmind_get": lambda e, a: e.get(a["id"], history=a.get("history", False)),
    "hitmind_update": lambda e, a: e.update(a["id"], content=a.get("content"), title=a.get("title")),
    "hitmind_forget": lambda e, a: e.forget(a["id"]),
    "hitmind_list": lambda e, a: e.list(filters={"kind": a.get("kind"), "status": a.get("status")},
                                        limit=a.get("limit", 50), offset=a.get("offset", 0)),
    "hitmind_reviews": lambda e, a: e.reviews(limit=a.get("limit", 50)),
    "hitmind_review_resolve": lambda e, a: e.review_resolve(a["id"], a["resolution"]),
    "hitmind_stats": lambda e, a: e.stats(),
    "hitmind_export": lambda e, a: e.export_memories(
        statuses=a.get("statuses"), include_embeddings=a.get("include_embeddings", False)),
    "hitmind_import": lambda e, a: e.import_memories(a.get("memories"), mode=a.get("mode", "merge")),
}


def dispatch(engine, tool_name: str, args: dict | None) -> dict:
    """Run one tool against an Engine and return its envelope dict (never raises)."""
    fn = _TOOL_MAP.get(tool_name)
    if fn is None:
        return models.err(models.NOT_FOUND, f"unknown tool: {tool_name}")
    try:
        return fn(engine, args or {})
    except models.HitMindError as exc:
        return exc.to_envelope()
    except Exception as exc:  # noqa: BLE001 - fail-open envelope
        return models.err(models.INTERNAL, str(exc))
