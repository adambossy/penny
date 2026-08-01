"""``penny mcp`` — the stdio MCP server exposing Penny's toolsets to harnesses.

The local-first surface for Claude Code / Codex / any MCP client: the same
toolsets the web chat uses (``build_toolset`` + the Amazon plugin), passed
through verbatim — names, descriptions, JSON schemas — plus the dynamic
system-prompt blocks (date, schema, taxonomy, memory) rendered fresh into the
MCP ``initialize`` instructions. Static identity/behavior belongs to the
agents-doc the plugin ships (``.claude-plugin``), not here.

Distinct from the deleted sandbox-facing capability-token server: this is a
plain stdio process on the user's machine — the OS user is the auth boundary,
exactly like the web UI.
"""

from __future__ import annotations

import json
from typing import Any
import uuid

from agent_harness.core.tools import ToolCall
from agent_harness.core.toolsets import Toolset
import mcp.types as mcp_types


class _ToolIndex:
    """Lazily-built name→(toolset, tool) map over the given toolsets."""

    def __init__(self, toolsets: list[Toolset]) -> None:
        self._toolsets = toolsets
        self._index: dict[str, tuple[Toolset, Any]] | None = None

    async def _ensure(self) -> dict[str, tuple[Toolset, Any]]:
        if self._index is None:
            index: dict[str, tuple[Toolset, Any]] = {}
            for ts in self._toolsets:
                for tool in await ts.list_tools(None):
                    index[tool.name] = (ts, tool)
            self._index = index
        return self._index

    async def list_mcp_tools(self) -> list[mcp_types.Tool]:
        index = await self._ensure()
        return [
            mcp_types.Tool(
                name=t.name,
                description=t.description or "",
                inputSchema=t.schema or {"type": "object", "properties": {}},
            )
            for _, t in index.values()
        ]

    async def call(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        index = await self._ensure()
        entry = index.get(name)
        if entry is None:
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=f"unknown tool: {name}")
                ],
                isError=True,
            )
        toolset, _ = entry
        call = ToolCall(
            id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments or {}
        )
        result = await toolset.call_tool(None, call)
        return _to_mcp_result(result)


def _to_mcp_result(result: Any) -> mcp_types.CallToolResult:
    """Convert a harness ``ToolResult`` to an MCP ``CallToolResult``.

    Mirrors ``bridge._serialize_tool_output``: structured content rides
    ``structuredContent`` verbatim (+ a JSON text block for legacy readers);
    otherwise the content blocks' text is forwarded.
    """
    if result.error:
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=str(result.error))],
            isError=True,
        )
    if result.structured_content is not None:
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps(result.structured_content, default=str)
                )
            ],
            structuredContent=result.structured_content
            if isinstance(result.structured_content, dict)
            else {"result": result.structured_content},
        )
    text = "\n".join(getattr(b, "text", "") for b in result.content)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)]
    )


def _instructions() -> str:
    """The dynamic prompt blocks, rendered fresh for MCP ``initialize``.

    The full system prompt (identity, behavior, workflows) is the harness's
    business via the plugin agents-doc; ``initialize`` carries only what must
    be CURRENT: date, schema, taxonomy, memory, and the freshness contract.
    """
    from penny.agent_factory import render_system_prompt

    rendered = render_system_prompt()
    return (
        "Penny — local personal-finance tools over the user's own database.\n"
        "Call sync_status before analysis when data freshness matters; if the "
        "daemon is not running, suggest `penny daemon start`.\n\n" + rendered
    )


def run_stdio() -> None:
    """Serve the toolsets over stdio (blocks until the client disconnects)."""
    import asyncio

    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from penny.bootstrap import bootstrap
    from penny.plugins.amazon import build_amazon_toolset
    from penny.tools.registry import build_toolset

    bootstrap()
    index = _ToolIndex([build_toolset(), build_amazon_toolset()])
    server: Server = Server("penny", instructions=_instructions())

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return await index.list_mcp_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await index.call(name, arguments)

    async def _main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_main())
