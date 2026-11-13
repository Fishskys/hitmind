"""Data types, error codes and the JSON envelope used across the Core API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- error codes -----------------------------------------------------------
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
FORBIDDEN = "FORBIDDEN"
DB_BUSY = "DB_BUSY"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
INTERNAL = "INTERNAL"


class HitMindError(Exception):
    """Typed core error; adapters convert it to a JSON error envelope."""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_envelope(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "detail": self.detail}}


def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def err(code: str, message: str, detail: Any = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "detail": detail}}


# --- value objects ---------------------------------------------------------
VALID_KINDS = ("persona", "fact", "event", "procedure")
VALID_STATUS = ("active", "inactive", "superseded", "expired", "deleted")
VALID_SCOPES = ("shared", "private")


@dataclass
class Memory:
    id: str
    root_id: str
    version: int
    agent_id: str
    scope: str
    kind: str
    tags: list[str] = field(default_factory=list)
    title: str | None = None
    content: str = ""
    importance: float = 0.5
    source: str = "manual"
    status: str = "active"
    created_at: str | None = None
    updated_at: str | None = None
    last_recalled_at: str | None = None
    recalled_times: int = 0
    valid_from: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None
    conflict_of: str | None = None

    def title_card(self) -> dict:
        """Compact view used for automatic prompt injection."""
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "tags": self.tags,
            "updated_at": self.updated_at,
        }
