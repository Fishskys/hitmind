"""End-to-end smoke: exercise the 11 verbs on a temp DB. Exit 0 on success."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hitmind.core.engine import Engine  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "smoke.db"
    e = Engine(str(tmp), agent_id="smoke")
    checks = []
    checks.append(e.stats()["ok"])
    r = e.remember("smoke test fact about tabs", "fact", title="smoke")
    checks.append(r["ok"] and r["data"]["action"] == "created")
    mid = r["data"]["id"]
    checks.append(e.get(mid)["ok"])
    checks.append(e.update(mid, content="smoke test fact about spaces")["ok"])
    checks.append(e.list()["ok"])
    checks.append(e.recall("tabs", top_k=3)["ok"])
    checks.append(e.reviews()["ok"])
    checks.append(e.forget(mid)["data"]["status"] == "deleted")
    e.close()
    ok = all(checks)
    print("acceptance_smoke:", "PASS" if ok else f"FAIL {checks}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
