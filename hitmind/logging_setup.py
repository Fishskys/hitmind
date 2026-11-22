"""Unified logging configuration for HitMind (zero third-party dependencies).

Design contract (two duties, kept strictly separate):

  1. ``configure()`` — built-in, host-agnostic *fallback*, used ONLY when the host
     does not provide its own logging system (standalone MCP / stdio daemon / CLI).
     Installs one ``RotatingFileHandler`` (local disk, we're a standalone process)
     and, on request, a stderr handler. Never writes to stdout (stdout is the MCP /
     JSONL protocol channel).

  2. ``get_logger(component)`` — every module logs through this. It resolves handlers
     from the process's ROOT logger, so whichever way the host wired things (or our
     own ``configure()``) the message lands in the same place:

       * host-native logging (``install_host_logging``) -> records flow into the
         host's root handlers AND are mirrored into one local rotating file
         (self-owned backup);
       * built-in fallback (``configure``) -> into our rotating file (+ stderr).

Both paths emit the SAME strict format via ``HitMindFormatter``::

    2026-11-22 16:30:00.123 [hitmind-core] [INFO] 内容
    <asctime ms>          [<component>] [<LEVEL>] <message>

Privacy rule (hard): NEVER log memory bodies, raw conversation, episode summaries,
LLM prompts, or API keys. Log structure/skips/errors/counts only. The sanitization
helper here is only a last-ditch safety net (it has no knowledge of most bodies —
modules must simply not pass them). It DOES redact obvious API keys and URL
credentials in case an exception string carries them.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Format & sanitization
# --------------------------------------------------------------------------- #
LOG_FORMAT = "%(asctime)s [%(component)s] [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class HitMindFormatter(logging.Formatter):
    """Render ``ts [component] [LEVEL] message`` with millisecond precision.

    The ``component`` attribute is injected by ``get_logger``. When a bare
    ``logging.Logger`` (code that used ``logging.getLogger`` directly) emits a
    record, ``component`` is absent — fall back to a guess derived from the
    logger name so the line still parses.
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "component") or not getattr(record, "component"):
            name = record.name or ""
            if name.startswith("hitmind"):
                tail = name.split(".")[-1]
                record.component = f"hitmind-{tail or 'core'}"
            else:
                record.component = "hitmind" if not name else name
        return super().format(record)

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        """``YYYY-MM-DD HH:MM:SS.mmm`` — stdlib default drops sub-second precision."""
        import time as _time
        ct = _time.localtime(record.created)
        base = _time.strftime(datefmt or self.datefmt or "%Y-%m-%d %H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}"


# Obvious secret patterns (only trip on clearly malformed/leaked values).
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key['\"\s:=]+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(sk-)[A-Za-z0-9\-]{16,}"),
    re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"),  # url user:pass@
)
MAX_LOG_MSG = 4000


def sanitize(text: str) -> str:
    """Best-effort redaction + length cap. Never a substitute for not logging
    sensitive values in the first place; a safety net for unexpected strings."""
    if text is None:
        return ""
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
    if len(out) > MAX_LOG_MSG:
        out = out[:MAX_LOG_MSG] + f"…(+{len(out) - MAX_LOG_MSG} chars)"
    return out


class _SanitizingFormatter(HitMindFormatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        return sanitize(line)


def normalize_level(value) -> str:
    """Return a valid stdlib level name; tolerate bad input (fallback INFO)."""
    if value is None:
        return "INFO"
    name = str(value).strip().upper()
    return name if name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"


def level_no(value) -> int:
    return getattr(logging, normalize_level(value), logging.INFO)


# --------------------------------------------------------------------------- #
# Logger factory
# --------------------------------------------------------------------------- #
class _ComponentLogger(logging.LoggerAdapter):
    """Prefixed logger. ``get_logger('hitmind-core')`` -> every record carries
    ``component='hitmind-core'`` and, when present, Propagate=False is NOT set —
    records still propagate to the root handlers (that is where output lives)."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra") or {}
        # never let a caller clobber the component tag accidentally
        extra.setdefault("component", self.extra.get("component"))
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter:
    """Component-tagged logger (e.g. 'hitmind-core', 'hitmind-hermes',
    'hitmind-mcp', 'hitmind-dsh'). Handlers/level come from the root logger,
    configured by configure() or the host."""
    base = logging.getLogger("hitmind")
    return _ComponentLogger(base, {"component": component})


# Convenience component names (the tag half inside the brackets).
CORE = "hitmind-core"
HERMES = "hitmind-hermes"
MCP = "hitmind-mcp"
DSH = "hitmind-dsh"
DAEMON = "hitmind-daemon"


# --------------------------------------------------------------------------- #
# Built-in fallback configuration (standalone processes only)
# --------------------------------------------------------------------------- #
_HITMIND_HANDLER_ATTR = "_hitmind_handler"


def default_log_dir(db_path: Optional[str]) -> Path:
    """Logs live beside the DB: ``<dir(db)>/../logs`` -> e.g.
    ``$HITMIND_HOME/hitmind/logs``. Falls back to the system temp dir."""
    try:
        if db_path:
            return Path(db_path).expanduser().resolve().parent.parent / "logs"
    except Exception:  # pragma: no cover - defensive
        pass
    env = os.environ.get("HITMIND_LOG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hitmind" / "logs"


def _resolve_log_file(log_file: Optional[str], db_path: Optional[str]) -> Path:
    """``log_file`` may be 'stderr', a path (+.log), or 'auto'/''/None (default dir).
    Env ``HITMIND_LOG_FILE`` participates (daemon/host) when the arg is None."""
    if log_file is None:
        log_file = os.environ.get("HITMIND_LOG_FILE")
    val = (log_file or "").strip()
    if val.lower() in ("", "auto", "default"):
        return default_log_dir(db_path) / "hitmind.log"
    if val.lower() in ("stderr", "-", "off", "none"):
        return Path(val)  # sentinel; caller interprets
    p = Path(val).expanduser()
    if p.is_dir():
        return p / "hitmind.log"
    return p


def _make_rotating_handler(path: Path, level: int) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        filename=str(path),
        maxBytes=int(os.environ.get("HITMIND_LOG_MAX_BYTES", str(5 * 1024 * 1024))),
        backupCount=int(os.environ.get("HITMIND_LOG_BACKUPS", "5")),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(_SanitizingFormatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def configure(*, level=None, log_file: Optional[str] = None,
              db_path: Optional[str] = None, component: str = "hitmind",
              stderr: bool = True, force: bool = False) -> Optional[Path]:
    """Install HitMind's OWN fallback logging. Idempotent (safe to call twice).

    Only for standalone processes without a host logging system (MCP server,
    stdio daemon, CLI). Returns the log file path used, or None if stderr-only.

    - ``level``: default ``HITMIND_LOG_LEVEL`` then INFO.
    - ``log_file``: 'stderr' -> no file; else path/auto (see ``_resolve_log_file``).
    - Attachment is on the ROOT logger so every ``getLogger('hitmind...')`` record
      and third-party lib logs at/above level flow through the same handler.
    """
    lvl = level_no(level if level is not None else os.environ.get("HITMIND_LOG_LEVEL", "INFO"))
    root = logging.getLogger()
    root.setLevel(lvl)

    target = _resolve_log_file(log_file, db_path)
    if str(target).lower() in ("stderr", "-", "off", "none"):
        target_path = None
    else:
        target_path = target

    already = getattr(root, _HITMIND_HANDLER_ATTR, None)
    if already is not None and not force:
        # Re-configure level only; handlers persist.
        if target_path is not None and getattr(already, "baseFilename", None) != str(target_path):
            _detach_hitmind_handlers(root)
        else:
            return target_path

    _detach_hitmind_handlers(root)
    handlers: list[logging.Handler] = []
    if target_path is not None:
        fh = _make_rotating_handler(target_path, lvl)
        handlers.append(fh)
    if stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(lvl)
        sh.setFormatter(_SanitizingFormatter(LOG_FORMAT, DATE_FORMAT))
        handlers.append(sh)
    for h in handlers:
        setattr(h, _HITMIND_HANDLER_ATTR, True)
        root.addHandler(h)
    # Remember the primary file handler for idempotency.
    if target_path is not None:
        setattr(root, _HITMIND_HANDLER_ATTR, handlers[0])
    elif handlers:
        setattr(root, _HITMIND_HANDLER_ATTR, handlers[0])
    return target_path


def _detach_hitmind_handlers(root: logging.Logger) -> None:
    for h in list(root.handlers):
        if getattr(h, _HITMIND_HANDLER_ATTR, False):
            try:
                root.removeHandler(h)
            except Exception:  # pragma: no cover
                pass
    if getattr(root, _HITMIND_HANDLER_ATTR, None) is not None:
        try:
            delattr(root, _HITMIND_HANDLER_ATTR)
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# Host-native integration (preferred path when a host logger exists)
# --------------------------------------------------------------------------- #
def use_host_logging(*, level=None, component: str = "hitmind") -> None:
    """Clean host integration: let the host's OWN logging pipeline handle our records.

    Sets the ``hitmind`` logger's level (so DEBUG/INFO record creation is gated),
    enables propagation, and ensures NO HitMind-owned handlers linger on either the
    ``hitmind`` logger or the root. Records then propagate to the host's existing root
    handlers (e.g. Hermes' ``agent.log``) in the host's own format.

    This NEVER touches the root logger's level and NEVER adds handlers to root — the
    key difference from ``configure()`` / ``install_host_logging()``: we do not mutate
    global logging state. Use this when the host already has a working root pipeline
    (Hermes gateway) but does not expose a concrete logger object to forward to.
    """
    lvl = level_no(level if level is not None else os.environ.get("HITMIND_LOG_LEVEL", "INFO"))
    hlog = logging.getLogger("hitmind")
    for h in list(hlog.handlers):
        hlog.removeHandler(h)
    hlog.setLevel(lvl)
    hlog.propagate = True
    # Drop any handlers a prior `configure()` may have attached to root, so we don't
    # double-emit into a stale file/stderr; we rely on the host's handlers instead.
    _detach_hitmind_handlers(logging.getLogger())

