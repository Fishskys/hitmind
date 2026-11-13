#!/usr/bin/env python3
"""Standalone wrapper around ``hitmind.io_cli`` (export/import memories as JSON).

Prefer the installed console script: ``hitmind-io export|import ...``.
This file exists so it also runs directly: ``uv run python scripts/hitmind_io.py ...``.
"""

import sys

from hitmind.io_cli import main

if __name__ == "__main__":
    sys.exit(main())
