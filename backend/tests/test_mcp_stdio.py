"""The ``penny mcp`` surface: toolset passthrough + rendered instructions."""

from __future__ import annotations

from penny.mcp_server import _instructions, _ToolIndex
from penny.plugins.amazon import build_amazon_toolset
from penny.tools.registry import build_toolset


async def test_tools_pass_through_verbatim(isolated_db):
    index = _ToolIndex([build_toolset(), build_amazon_toolset()])
    tools = {t.name: t for t in await index.list_mcp_tools()}
    # Identity is preserved: the same names the web agent sees, with schemas.
    for expected in ("run_sql", "sync_transactions", "sync_status", "generate_chart"):
        assert expected in tools
        assert isinstance(tools[expected].inputSchema, dict)


async def test_unknown_tool_is_a_tool_error_not_a_crash(isolated_db):
    index = _ToolIndex([build_toolset()])
    result = await index.call("no_such_tool", {})
    assert result.isError


def test_instructions_render_dynamic_blocks(isolated_db, isolated_workspace):
    from penny.db import get_db

    get_db().create_schema()
    text = _instructions()
    # The dynamic runtime context made it in (placeholders resolved).
    assert "{{CURRENT_DATE}}" not in text
    assert "sync_status" in text
