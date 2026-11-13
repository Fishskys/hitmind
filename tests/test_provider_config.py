"""Memory-provider config panel: value precedence (config.json > env > default) + tolerance."""

import json
import tempfile
from pathlib import Path

from hitmind.adapters.hermes import HitMindMemoryProvider


def _prov(home: str) -> HitMindMemoryProvider:
    p = HitMindMemoryProvider()
    p._native_cfg = p._read_native_config(home)
    return p


def test_precedence_config_over_env_over_default(monkeypatch):
    home = tempfile.mkdtemp()
    d = Path(home) / "hitmind"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"cluster_min_size": 25, "idle_consolidate_seconds": 0}), encoding="utf-8")
    monkeypatch.setenv("HITMIND_CLUSTER_MIN_SIZE", "99")      # config should win
    monkeypatch.setenv("HITMIND_TOPIC_EXPAND_CAP", "12")      # only env -> env wins over default
    p = _prov(home)
    assert p._config_value("cluster_min_size", "HITMIND_CLUSTER_MIN_SIZE", 10, int) == 25
    assert p._config_value("idle_consolidate_seconds", "HITMIND_IDLE_CONSOLIDATE_SECONDS", 300, int) == 0
    assert p._config_value("topic_expand_cap", "HITMIND_TOPIC_EXPAND_CAP", 8, int) == 12
    assert p._config_value("consolidate_every_turns", "HITMIND_SYNC_CONSOLIDATE_EVERY", 10, int) == 10


def test_missing_and_bad_values_fall_back(monkeypatch):
    home = tempfile.mkdtemp()
    p = _prov(home)                                            # no config.json -> {}
    monkeypatch.setenv("HITMIND_CLUSTER_MIN_SIZE", "not-an-int")  # bad env cast -> default
    assert p._config_value("cluster_min_size", "HITMIND_CLUSTER_MIN_SIZE", 10, int) == 10
    assert p._config_value("topic_expand_cap", "HITMIND_TOPIC_EXPAND_CAP", 8, int) == 8


def test_bool_coercion(monkeypatch):
    home = tempfile.mkdtemp()
    d = Path(home) / "hitmind"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"bg_runner": False}), encoding="utf-8")
    p = _prov(home)
    assert p._config_bool("bg_runner", "HITMIND_BG_RUNNER", True) is False
    monkeypatch.setenv("HITMIND_BG_RUNNER", "0")
    p2 = _prov(tempfile.mkdtemp())
    assert p2._config_bool("bg_runner", "HITMIND_BG_RUNNER", True) is False
