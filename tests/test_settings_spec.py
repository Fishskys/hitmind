"""Guards for the single-source settings spec (hitmind/settings_spec.py).

These tests fail loudly if the tunable surface drifts between:
  * the Python spec (source of truth),
  * the DSH JS mirror (dsh-plugin/index.js: Config fields + CFG_ENV), and
  * what Engine actually resolves (env -> attribute).
No Hermes dependency: they read files and exercise the spec/Engine directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hitmind import settings_spec as spec

REPO = Path(__file__).resolve().parents[1]
INDEX_JS = REPO / "dsh-plugin" / "index.js"

# DSH-facing rename(s): the core key `recall_top_k` surfaces as `injectTopK` in the plugin.
RENAME = {"recall_top_k": "injectTopK"}
# Core keys intentionally NOT mirrored into the DSH plugin's Config/CFG_ENV: DSH is a
# single host with one global mode switch (`workMode`), so per-platform `work_platforms`
# is meaningless there and is omitted on purpose.
DSH_OMITTED = {"work_platforms"}


def _camel(key: str) -> str:
    parts = key.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _js_name(key: str) -> str:
    return RENAME.get(key, _camel(key))


def test_spec_keys_and_env_unique():
    keys = [s.key for s in spec.SETTINGS]
    envs = [s.env for s in spec.SETTINGS]
    assert len(keys) == len(set(keys)), "duplicate config key"
    assert len(envs) == len(set(envs)), "duplicate env name"
    for s in spec.SETTINGS:
        assert s.kind in (spec.K_TEXT, spec.K_NUMBER, spec.K_BOOL, spec.K_SELECT, spec.K_SECRET)
        assert s.env.startswith("HITMIND_")


def test_build_engine_kwargs_defaults():
    kw = spec.build_engine_kwargs(lambda key: None)  # all defaults
    assert kw["recall_top_k"] == 5
    assert kw["dense_floor"] == 0.5
    assert kw["rel_weight"] == 0.8
    assert kw["capacity_warn"] == 200000
    assert kw["cluster_cos_floor"] == 0.78
    assert kw["raw_ttl_days"] == 30
    assert kw["tz_hours"] is None  # 'auto'
    assert kw["kind_importance"] == {"persona": 1.0, "fact": 1.0, "event": 1.0, "procedure": 1.0}
    assert kw["half_life_days"] == {"persona": 180, "fact": 180, "event": 14, "procedure": 60}
    # ttl: 0 -> None (never expires)
    assert kw["ttl_days"] == {"persona": None, "fact": None, "event": 90, "procedure": None}


def test_previously_envless_knobs_now_honor_env(monkeypatch):
    """P4 regression: these knobs ignored HITMIND_* env before; they must not anymore."""
    monkeypatch.setenv("HITMIND_CLUSTER_COS_FLOOR", "0.66")
    monkeypatch.setenv("HITMIND_REL_WEIGHT", "0.7")
    monkeypatch.setenv("HITMIND_CAPACITY_WARN", "12345")
    monkeypatch.setenv("HITMIND_HALF_LIFE_EVENT_DAYS", "7")
    monkeypatch.setenv("HITMIND_TTL_FACT_DAYS", "33")
    monkeypatch.setenv("HITMIND_IMPORTANCE_FACT", "0.5")
    import tempfile

    from hitmind.core.engine import Engine

    e = Engine(tempfile.mktemp(suffix=".db"), agent_id="t")
    try:
        assert e.cluster_cos_floor == 0.66
        assert e.rel_weight == 0.7
        assert e.capacity_warn == 12345
        assert e.half_life_days["event"] == 7
        assert e.ttl_days["fact"] == 33
        assert e.kind_importance["fact"] == 0.5
    finally:
        e.conn.close()


@pytest.mark.skipif(not INDEX_JS.is_file(), reason="dsh-plugin/index.js not present")
def test_js_mirror_matches_spec():
    """dsh-plugin/index.js Config + CFG_ENV must mirror the spec exactly (no drift)."""
    js = INDEX_JS.read_text(encoding="utf-8")
    cfg = re.search(r"export const Config = Schema\.object\(\{(.+?)\n\}\)", js, re.S).group(1)
    cfg_keys = set(re.findall(r"^\s*([a-zA-Z]+):\s*Schema\.", cfg, re.M))
    env_block = re.search(r"const CFG_ENV = \[(.+?)\n\]", js, re.S).group(1)
    env_map = {m[0]: (m[1], m[2]) for m in re.findall(r"\['([a-zA-Z]+)',\s*'([A-Z_]+)',\s*'([sibf])'", env_block)}

    for s in spec.SETTINGS:
        if s.key in DSH_OMITTED:
            assert _js_name(s.key) not in cfg_keys, f"{s.key} should be omitted from DSH Config"
            continue
        jsn = _js_name(s.key)
        assert jsn in cfg_keys, f"spec key {s.key} -> {jsn} missing from DSH Config"
        assert jsn in env_map, f"spec key {s.key} -> {jsn} missing from CFG_ENV"
        assert env_map[jsn][0] == s.env, f"{jsn} env mismatch: {env_map[jsn][0]} != {s.env}"
        want = {
            "text": "s", "select": "s", "secret": "s", "bool": "b",
            "number": "i" if isinstance(s.default, int) and not isinstance(s.default, bool) else "f",
        }[s.kind]
        assert env_map[jsn][1] == want, f"{jsn} kind mismatch: {env_map[jsn][1]} != {want}"
