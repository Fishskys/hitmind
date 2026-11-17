"""Cross-adapter tool contract: mcp/server.py must stay in sync with hitmind/tools.py.

The 11 tools are declared in two physical places by necessity:
  - hitmind/tools.py        : _TOOL_SCHEMAS (host-agnostic source of truth; Hermes + daemon)
  - hitmind/mcp/server.py   : @app.tool() functions (MCP SDK requires typed signatures)

This test fails if they drift, so "改工具只改 tools.py" is enforced: forgetting to update
the MCP surface will break CI. We compare the STABLE contract only — tool names, each
tool's parameter NAMES, and the required set — not SDK-generated presentation details
(default/title/anyOf-null wrappers).
"""

from __future__ import annotations

import asyncio

from hitmind.mcp.server import build_server
from hitmind.tools import _TOOL_SCHEMAS


def _mcp_schemas() -> dict[str, dict]:
    app = build_server(object())  # engine is not touched by list_tools()
    tools = asyncio.run(app.list_tools())
    return {t.name: t.input_schema for t in tools}


def test_mcp_matches_tools_py():
    mcp = _mcp_schemas()
    src = {s["name"]: s["parameters"] for s in _TOOL_SCHEMAS}

    assert len(src) == 11, f"expected 11 tools in tools.py, got {len(src)}"
    assert set(mcp) == set(src), f"tool name mismatch: only-mcp={set(mcp) - set(src)} only-tools.py={set(src) - set(mcp)}"

    for name, src_params in src.items():
        mcp_props = set((mcp[name].get("properties") or {}).keys())
        src_props = set((src_params.get("properties") or {}).keys())
        assert mcp_props == src_props, f"{name}: parameter names differ (mcp={mcp_props} tools.py={src_props})"

        mcp_req = set(mcp[name].get("required") or [])
        src_req = set(src_params.get("required") or [])
        assert mcp_req == src_req, f"{name}: required set differs (mcp={mcp_req} tools.py={src_req})"
