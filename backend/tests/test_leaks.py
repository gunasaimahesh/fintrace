"""Nothing the model sees names the planted scenario: the old IDs are gone, and the tool schemas,
server instructions and system prompt are neutral."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from backend.app import agent
from backend.app import scenario as s
from backend.app.db import get_db
from backend.app.mcp_server import INSTRUCTIONS, build_server

pytestmark = pytest.mark.graph

HINTS = ("mule", "shell", "sdn", "vortex", "volkov", "baltic", "orion", "nova", "8821", "rohan", "priya", "waiver-77")


def test_renamed_ids_are_gone_from_the_graph(scratch_key):
    rows = get_db().select_graph(scratch_key).ro_query(
        "MATCH (n:Entity) WHERE n.id IN $old RETURN n.id", {"old": list(s.RENAMED)}).result_set
    assert rows == []


def test_tool_schemas_and_the_prompt_name_nothing_from_the_scenario(scratch_key):
    async def listed():
        async with Client(build_server(scratch_key)) as client:
            return (await client.list_tools()).tools

    visible = json.dumps([{"name": t.name, "description": t.description, "schema": t.input_schema}
                          for t in asyncio.run(listed())])
    visible = (visible + INSTRUCTIONS + agent.system_prompt("case-test", agent.MAX_STEPS)).lower()
    assert [word for word in (*s.RENAMED, *HINTS) if word in visible] == []
