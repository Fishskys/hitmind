"""Machine-envelope filtering for autosave (adapted from Mnemosyne's approach).

Hermes injects synthetic messages into the user turn (cron preambles, gateway
interruption notices, context-compaction handoffs, background-process completions).
Autosave must not turn those into memories.

Two mechanisms, in order:
1. ``_MACHINE_ONLY`` — the whole turn is scaffolding with no human text → drop it
   (return None). This catches cron preambles that leak in when the host reports
   ``agent_context="primary"`` (e.g. gateway-scheduled runs), and gateway notices.
2. leading-envelope stripping — a machine notice wrapped *around* genuine human text
   → strip the wrapper and keep the human tail.
"""

from __future__ import annotations

import re

# whole-turn machine scaffolding: match at the start; these have no human tail.
_MACHINE_ONLY = [
    re.compile(r"^\[IMPORTANT:\s*You are running as a scheduled cron job", re.IGNORECASE),
    re.compile(r"^\[System\s+note:\s*The previous turn was interrupted by a gateway", re.IGNORECASE),
    re.compile(r"^\[System\s+note:\s*The user switched models", re.IGNORECASE),
    re.compile(r"^\[IMPORTANT:\s*Background process\b", re.IGNORECASE),
]

# leading machine-notice prefixes to strip when a human tail follows
_PREFIX_PATTERNS = [
    re.compile(r"^\[System\s+note:[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\[IMPORTANT:[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\[CONTEXT\s+COMPACTION\b[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\[CONTEXT\s+SUMMARY\]\s*:?\s*", re.IGNORECASE),
    re.compile(r"^\[PRIOR\s+CONTEXT\b[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\[ASYNC\s+[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\[Replying\s+to:\s*\"Cronjob\s+Response:[^\"]*\"\s*\]\s*", re.IGNORECASE),
]

# marker that ends a compaction summary and precedes the real user message
_COMPACTION_END = "--- END OF CONTEXT SUMMARY"


def sanitize_user_content(text: str | None) -> str | None:
    """Return cleaned human text, or None if the turn is purely machine-generated."""
    t = (text or "").strip()
    if not t:
        return ""
    # if a compaction summary, keep only the part after the end marker
    if _COMPACTION_END in t:
        t = t.split(_COMPACTION_END, 1)[1]
        t = t.split("---", 1)[-1] if t.startswith("---") else t
        t = t.strip()
        if not t:
            return None
    # whole-turn machine scaffolding → drop
    for pat in _MACHINE_ONLY:
        if pat.match(t):
            return None
    # strip leading machine envelopes repeatedly, keep human tail
    changed = True
    while changed:
        changed = False
        for pat in _PREFIX_PATTERNS:
            m = pat.match(t)
            if m:
                t = t[m.end():].strip()
                changed = True
                break
    if not t:
        return None
    return t
