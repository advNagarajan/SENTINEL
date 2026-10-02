"""Protocol-level tests for the SENTINEL MCP stdio boundary."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {
    "get_screen_state",
    "get_observation",
    "set_field_and_submit",
    "fill_form_and_submit",
    "trigger_action",
}


def test_production_server_process_starts_without_client() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "layer4.mcp_server"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        asyncio.run(asyncio.sleep(0.5))
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


@pytest.mark.asyncio
async def test_stdio_discovery_observation_generation_and_action() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.mcp_stdio_server"],
        cwd=str(PROJECT_ROOT),
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        discovered = await session.list_tools()
        assert {tool.name for tool in discovered.tools} == EXPECTED_TOOLS

        observed = await session.call_tool("get_observation", {})
        observation = observed.structuredContent
        assert observation["screen_title"] == "MCP TEST MENU"
        generation_token = observation["generation_token"]
        assert generation_token

        stale = await session.call_tool(
            "trigger_action",
            {"generation_token": "not-the-current-token", "action_id": "ENTER"},
        )
        assert stale.structuredContent["success"] is False
        assert stale.structuredContent["error"] == "ContractValidationError"

        action = await session.call_tool(
            "set_field_and_submit",
            {
                "generation_token": generation_token,
                "field_id": "fld_command",
                "value": "MENU",
                "action": "ENTER",
            },
        )
        assert action.structuredContent["success"] is True, action.structuredContent
        assert action.structuredContent["generation"] == 2

        after_action = await session.call_tool("get_observation", {})
        assert after_action.structuredContent["screen_title"] == "MCP TEST RESULT"
        assert after_action.structuredContent["generation_token"] != generation_token

        multi_field = await session.call_tool(
            "fill_form_and_submit",
            {
                "generation_token": after_action.structuredContent["generation_token"],
                "fields": {"fld_command": "MENU", "fld_account": "TEST01"},
                "action": "ENTER",
            },
        )
        assert multi_field.structuredContent["success"] is True

        current = await session.call_tool("get_observation", {})
        trigger = await session.call_tool(
            "trigger_action",
            {
                "generation_token": current.structuredContent["generation_token"],
                "action_id": "PF3",
            },
        )
        assert trigger.structuredContent["success"] is True