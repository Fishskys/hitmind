"""Lifecycle: kind-differentiated exponential freshness decay, default TTL,
capacity thresholds and expiry checks. Freshness/final are computed, never stored.

All knobs are module DEFAULTS that the Engine may override per-config (desktop panel):
``HALF_LIFE_DAYS``/``DEFAULT_TTL_DAYS``/``W_REL``/``CAPACITY_WARN`` are only fallbacks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

# Default display/logical timezone offset in hours (UTC+8 = Asia/Shanghai).
DEFAULT_TZ_HOURS = 8


def system_tz_hours() -> float:
    """Read the OS local timezone offset (hours) at call time — zero config, no deps.

    Uses ``datetime.now().astimezone()``, which consults the process TZ / the system
    zoneinfo (e.g. Asia/Shanghai -> +8.0). Falls back to +8 on any error.
    """
    try:
        off = datetime.now().astimezone().utcoffset()
        if off is not None:
            return off.total_seconds() / 3600.0
    except Exception:
        pass
    return float(DEFAULT_TZ_HOURS)


def _tzinfo(tz_hours: float | None = None) -> timezone:
    h = DEFAULT_TZ_HOURS if tz_hours is None else tz_hours
    return timezone(timedelta(hours=float(h)))


def now_tz(tz_hours: float | None = None) -> datetime:
    """Current time as a tz-aware datetime in the configured offset (default UTC+8)."""
    return datetime.now(_tzinfo(tz_hours))


def _iso_seconds(dt: datetime, tz_hours: float | None = None) -> str:
    """ISO-8601 with second precision, keeping the local offset (e.g. 2026-11-12T15:22:48+08:00)."""
    return dt.astimezone(_tzinfo(tz_hours)).isoformat(timespec="seconds")


# half-life in days per kind (initial values; calibrate with bench)
HALF_LIFE_DAYS = {"persona": 180, "fact": 180, "event": 14, "procedure": 60}
# default TTL in days per kind (None = never expires by default)
DEFAULT_TTL_DAYS = {"persona": None, "fact": None, "event": 90, "procedure": None}

# relevance weight in the final blended score (freshness gets 1 - rel - importance)
W_REL = 0.8
W_IMP = 0.1

# capacity: set generously; exceeding it only raises a warning, never deletes
CAPACITY_WARN = 200_000


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def _hl(hl_map, kind: str) -> float:
    src = hl_map if hl_map is not None else HALF_LIFE_DAYS
    v = src.get(kind, 180)
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 180.0
    return v if v > 0 else 180.0


def _ttl(ttl_map, kind: str):
    src = ttl_map if ttl_map is not None else DEFAULT_TTL_DAYS
    return src.get(kind)


def freshness(updated_at: str | None, kind: str, now: datetime | None = None,
              last_recalled_at: str | None = None, half_life_days: dict | None = None) -> float:
    """0..1 exponential decay: 0.5 ** (age_days / half_life[kind]).

    Decay baseline = max(updated_at, last_recalled_at): both an update and a recall
    "refresh" the clock (a memory that keeps being recalled stays fresh).
    ``half_life_days`` (kind->days) overrides the module default when provided.
    """
    now = now or datetime.now(UTC)
    ts = updated_at
    if last_recalled_at:
        a, b = _parse(updated_at), _parse(last_recalled_at)
        if b is not None and (a is None or b > a):
            ts = last_recalled_at
    dt = _parse(ts)
    if dt is None:
        return 0.5
    age_days = max((now - dt).total_seconds() / 86400.0, 0.0)
    hl = _hl(half_life_days, kind)
    return 0.5 ** (age_days / hl)


def refresh_expiry(kind: str, now: datetime | None = None, ttl_days: dict | None = None,
                   tz_hours: float | None = None) -> str | None:
    """New expiry when a time-limited memory is recalled/updated: now + kind TTL.

    Returns None for kinds without a default TTL (never expire), so there is nothing to
    refresh. ``ttl_days`` (kind->days|None) overrides the module default when provided.
    """
    now = now or now_tz(tz_hours)
    ttl = _ttl(ttl_days, kind)
    if ttl is None:
        return None
    return _iso_seconds(now + timedelta(days=ttl), tz_hours)


def final_score(rel_norm: float, importance: float, fresh: float,
                w_rel: float = W_REL, w_imp: float = W_IMP) -> float:
    """Blended score: w_rel*relevance + w_imp*importance + (remainder)*freshness."""
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 1.0
    fres_w = max(0.0, 1.0 - w_rel - w_imp)
    return w_rel * rel_norm + w_imp * importance + fres_w * fresh


def default_valid_until(kind: str, created_at: str | None, ttl_days: dict | None = None,
                        tz_hours: float | None = None) -> str | None:
    """Return an ISO expiry for kinds that have a default TTL, else None."""
    ttl = _ttl(ttl_days, kind)
    if ttl is None:
        return None
    base = _parse(created_at) or now_tz(tz_hours)
    return _iso_seconds(base + timedelta(days=ttl), tz_hours)


def is_expired(valid_until: str | None, now: datetime | None = None) -> bool:
    dt = _parse(valid_until)
    if dt is None:
        return False
    now = now or datetime.now(UTC)
    return dt <= now
