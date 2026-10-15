"""The MCP server as Cursor or Claude Desktop launch it: a subprocess speaking stdio."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from backend.app import scenario as s
from backend.app.config import ROOT

pytestmark = pytest.mark.graph

TOOL_NAMES = {"resolve_entity", "recall_memory", "explore_neighborhood", "trace_money_flow", "path_between",
              "detect_ring", "find_brokers", "exposure_flow", "trace_governance", "freeze_impact", "request_action"}


def test_the_stdio_server_lists_the_eleven_tools_and_answers(scratch_key):
    params = StdioServerParameters(command=sys.executable, args=["-m", "backend.app.mcp_server"], cwd=str(ROOT),
                                   env={**os.environ, "FINTRACE_GRAPH": scratch_key})

    async def session():
        async with Client(params) as client:
            listed = await client.list_tools()
            return listed.tools, await client.call_tool("resolve_entity", {"entity_id": s.NOVAPAY})

    tools, result = asyncio.run(session())
    assert {tool.name for tool in tools} == TOOL_NAMES
    read_only = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint}
    assert TOOL_NAMES - read_only == {"find_brokers", "request_action"}
    assert result.structured_content["data"]["name"] == "NovaPay Digital Goods"
