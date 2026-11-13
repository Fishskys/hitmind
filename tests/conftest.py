"""Shared pytest fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Never start the idle background runner during unit tests (deterministic, no stray threads).
os.environ.setdefault("HITMIND_BG_RUNNER", "0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hitmind.core.engine import Engine


@pytest.fixture
def engine(tmp_path):
    e = Engine(str(tmp_path / "hitmind.db"), agent_id="tester")
    yield e
    e.close()
