"""Extensible idle/background task runner (Hermes adapter side).

A single daemon thread wakes every ``poll_seconds`` (and on an explicit ``wake()``)
and calls ``Engine.run_pending_pass()``, which runs every task registered on the
engine. Tasks are pluggable — today: chat batch extraction and topic aggregation;
future tasks just call ``engine.register_pending_task(name, fn)``.

Design constraints:
- OFF the hot path: the turn hook only flags a pending batch; extraction happens here.
- One serial runner so LLM/SQLite work never stampedes across sessions.
- All work goes through the Engine (single writer under its RLock).
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# One idle pass at a time per process, even if several provider instances each start a
# runner (multiple cached agents/sessions). Cheap insurance against double-processing a
# batch; the DB cursor + write gate are the real correctness backstop.
_PASS_LOCK = threading.Lock()


class BackgroundTaskRunner:
    def __init__(self, engine, *, poll_seconds: int = 60) -> None:
        self._engine = engine
        self._poll = max(15, int(poll_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="hitmind-idle-runner", daemon=True)
        self._thread.start()
        logger.debug("hitmind idle runner started (poll=%ss)", self._poll)

    def wake(self) -> None:
        """Signal the runner to run a pass soon (e.g. a batch just became pending)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # sleep up to poll_seconds, or wake early on a signal
            self._wake.wait(timeout=self._poll)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                if not _PASS_LOCK.acquire(blocking=False):
                    continue  # another runner is mid-pass; skip this tick
                try:
                    # re-probe a possibly-tripped embedder once per pass (it re-trips
                    # cheaply if the service is still down; nothing on the hot path).
                    reset = getattr(getattr(self._engine, "embedder", None), "reset", None)
                    if callable(reset):
                        try:
                            reset()
                        except Exception:  # noqa: BLE001
                            pass
                    res = self._engine.run_pending_pass()
                finally:
                    _PASS_LOCK.release()
                data = (res or {}).get("data") or {}
                # log only when a task actually did something
                for name, r in (data.get("tasks") or {}).items():
                    if isinstance(r, dict) and (
                        r.get("committed") or r.get("finalized") or r.get("error")
                    ):
                        logger.info("hitmind idle task %s: %s", name, r)
            except Exception as exc:  # noqa: BLE001 - a pass must never kill the thread
                logger.warning("hitmind idle pass failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
