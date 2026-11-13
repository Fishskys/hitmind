"""Verify the Hermes adapter against the real host MemoryProvider ABC.

Puts the Hermes source tree on sys.path so ``import agent.memory_provider`` works
exactly like in the real host, then asserts the adapter subclasses it, implements
the full surface, binds db_path from hermes_home, and that register(ctx) calls
register_memory_provider. Skipped if the source tree isn't present.
"""

import os
import sys
from pathlib import Path

import pytest

_SRC = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes" / "hermes-agent"))
if (_SRC / "agent" / "memory_provider.py").exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _host_abc():
    try:
        from agent.memory_provider import MemoryProvider
        return MemoryProvider
    except Exception:
        return None


def test_adapter_matches_host_contract():
    ABC = _host_abc()
    if ABC is None:
        pytest.skip("Hermes source not available")

    # ensure the adapter module was bound against the real host ABC even if another
    # test imported it earlier when the host path wasn't set up
    import importlib

    import hitmind.adapters.hermes as hm
    hm = importlib.reload(hm)
    from hitmind.adapters.hermes import HitMindMemoryProvider

    assert issubclass(HitMindMemoryProvider, ABC)
    prov = HitMindMemoryProvider()
    assert prov.name == "hitmind"
    assert isinstance(prov.is_available(), bool)
    assert isinstance(prov.system_prompt_block(), str)
    schemas = prov.get_tool_schemas()
    assert isinstance(schemas, list) and len(schemas) == 9
    assert all({"name", "description", "parameters"} <= set(s) for s in schemas)

    import tempfile
    home = tempfile.mkdtemp()
    prov.initialize("sess-1", hermes_home=home, agent_context="primary")
    assert (Path(home) / "hitmind" / "data" / "hitmind.db").exists()
    prov.sync_turn("hello memory", "hi there", session_id="sess-1")
    out = prov.handle_tool_call("hitmind_stats", {})
    assert '"ok": true' in out
    prov.shutdown()


def test_register_calls_register_memory_provider():
    from hitmind.adapters.hermes import register

    captured = {}

    class Ctx:
        def register_memory_provider(self, p):
            captured["p"] = p

    register(Ctx())
    assert captured.get("p") is not None
    assert captured["p"].name == "hitmind"


def test_prefetch_returns_bare_context_without_fence():
    """prefetch() must return BARE bullet lines — no <memory-context> wrapper.

    The host owns the fence (build_memory_context_block) and strips a provider-added
    one, logging 'memory provider returned pre-wrapped context; stripped' every turn.
    We assert our output is NOT altered by the host sanitizer (the exact warning trigger).
    """
    import tempfile

    from hitmind.adapters.hermes import HitMindMemoryProvider

    home = tempfile.mkdtemp()
    prov = HitMindMemoryProvider()
    prov.initialize("sess-pf", hermes_home=home, agent_context="primary")
    prov.handle_tool_call("hitmind_remember",
                          {"content": "用户偏好深色主题用于夜间阅读", "kind": "persona", "title": "深色主题"})

    out = prov.prefetch("配色偏好", session_id="sess-pf")
    assert out, "prefetch should return the recalled memory"
    assert "<memory-context>" not in out and "</memory-context>" not in out
    assert "- [mem_" in out

    # If the host sanitizer is importable, prove it leaves our output untouched
    # (sanitized == raw  <=>  no 'pre-wrapped context; stripped' warning).
    try:
        from agent.memory_context import sanitize_context
    except Exception:
        prov.shutdown()
        pytest.skip("Hermes memory_context not importable")
    assert sanitize_context(out) == out
    prov.shutdown()
