"""Reusable capture-policy gate (host-agnostic, stdlib only).

The decision "should this turn become a memory candidate?" is the same rule for
every host: filter by role set, per-role minimum length, and skipped contexts.
Before this module the logic lived only inside the Hermes adapter, so the DSH
daemon recorded turns unconditionally and the corresponding config knobs were
inert. Both hosts now share this one function.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable

# Machine-generated scaffolding that must never become a "user" memory even when it
# arrives on the user role (compaction summaries / envelope preambles the host injects).
_COMPACTION_END = "<!-- END COMPACTION -->"
_MACHINE_ONLY = (
    re.compile(r"^\[CONTEXT (?:COMPACTION|SUMMARY)\]", re.IGNORECASE),
    re.compile(r"^<[^>]+>\s*$"),
)
# Leading envelope wrappers to peel before measuring the human tail.
_PREFIX_PATTERNS = (
    re.compile(r"^\[[A-Z][^\]]{0,40}\]\s*"),          # [SYSTEM] / [CONTEXT ...]
    re.compile(r"^<[A-Za-z][^>]{0,40}>\s*"),           # <system> …
    re.compile(r"^---+\s*", re.MULTILINE),
)


def sanitize_user_content(text: str | None) -> str | None:
    """Return cleaned human text, or None if the turn is purely machine-generated."""
    t = (text or "").strip()
    if not t:
        return ""
    if _COMPACTION_END in t:
        t = t.split(_COMPACTION_END, 1)[1]
        t = t.split("---", 1)[-1] if t.startswith("---") else t
        t = t.strip()
        if not t:
            return None
    for pat in _MACHINE_ONLY:
        if pat.match(t):
            return None
    changed = True
    while changed:
        changed = False
        for pat in _PREFIX_PATTERNS:
            m = pat.match(t)
            if m:
                t = t[m.end():].strip()
                changed = True
                break
    return t or None


def parse_roles(raw: str | Iterable[str] | None) -> set[str]:
    """Parse a ``sync_roles`` value ("user" / "user,assistant") into a role set."""
    if raw is None:
        return {"user"}
    if not isinstance(raw, str):
        raw = ",".join(str(x) for x in raw)
    roles = {r.strip().lower() for r in raw.split(",") if r.strip()}
    return roles & {"user", "assistant"} or {"user"}


def parse_contexts(raw: str | Iterable[str] | None) -> set[str]:
    """Parse a comma-separated ``skip_contexts`` value into a lowercase set."""
    if raw is None:
        return set()
    if not isinstance(raw, str):
        raw = ",".join(str(x) for x in raw)
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def should_capture(
    role: str,
    content: str,
    *,
    roles: set[str],
    user_min: int,
    assistant_min: int,
    context: str | None = None,
    skip_contexts: set[str] | None = None,
    sanitizer: "Callable[[str | None], str | None] | None" = None,
) -> tuple[bool, str]:
    """Decide whether a turn is captured, returning (capture?, cleaned_text).

    Rules (identical to the historical Hermes behaviour):
      * the role must be in ``roles`` (default {"user"});
      * a skipped context drops the turn entirely;
      * user turns are sanitised against machine scaffolding, then must exceed
        ``user_min`` characters; assistant turns must exceed ``assistant_min``.

    ``sanitizer`` lets a host supply its own machine-envelope filter (Hermes has a
    richer one); when omitted, this module's generic ``sanitize_user_content`` runs.
    """
    role = (role or "").strip().lower()
    if role not in roles:
        return False, ""
    if context is not None and skip_contexts:
        if str(context).strip().lower() in skip_contexts:
            return False, ""
    if role == "user":
        clean = (sanitizer or sanitize_user_content)(content)
        if not clean or len(clean) <= int(user_min):
            return False, ""
        return True, clean
    # assistant (and any other captured role): raw text, past the assistant threshold.
    clean = (content or "").strip()
    if not clean or len(clean) <= int(assistant_min):
        return False, ""
    return True, clean
