"""hitmind-io console entry: export/import HitMind memories as JSON (offline).

Not exposed to the model context (no agent tool); invoked from the CLI / scripts.
See ``scripts/hitmind_io.py`` for the standalone wrapper.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_db() -> str:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(Path(home) / "hitmind" / "data" / "hitmind.db")


def _engine(db: str):
    from hitmind.core.engine import Engine
    return Engine(db, embedder=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("hitmind-io")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="export memories to JSON")
    ex.add_argument("--db", default=_default_db())
    ex.add_argument("--out", default="-", help="output file (default: stdout)")
    ex.add_argument("--statuses", default="active,inactive,superseded,expired")
    ex.add_argument("--embeddings", action="store_true")
    ex.add_argument("--limit", type=int, default=None)

    im = sub.add_parser("import", help="import memories from JSON")
    im.add_argument("--db", default=_default_db())
    im.add_argument("--in", dest="infile", required=True)
    im.add_argument("--mode", choices=["merge", "replace"], default="merge")
    im.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    e = _engine(args.db)
    try:
        if args.cmd == "export":
            statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
            res = e.export_memories(statuses=statuses, include_embeddings=args.embeddings,
                                    limit=args.limit)["data"]
            payload = json.dumps(res, ensure_ascii=False, indent=2)
            if args.out == "-":
                print(payload)
            else:
                Path(args.out).write_text(payload, encoding="utf-8")
                print(f"exported {res['count']} memories -> {args.out}", file=sys.stderr)
            return 0
        raw = Path(args.infile).read_text(encoding="utf-8")
        obj = json.loads(raw)
        items = obj.get("memories") if isinstance(obj, dict) else obj
        if not isinstance(items, list):
            print('error: JSON must be a list or {"memories": [...]}', file=sys.stderr)
            return 2
        if args.dry_run:
            import shutil
            import tempfile
            tmp = str(Path(tempfile.mkdtemp()) / "dry.db")
            shutil.copy2(args.db, tmp)
            de = _engine(tmp)
            r = de.import_memories(items, mode=args.mode)
            de.close()
        else:
            r = e.import_memories(items, mode=args.mode)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok", True) else 1
    finally:
        e.close()


if __name__ == "__main__":
    sys.exit(main())
