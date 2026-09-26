"""Minimal MCP adapter smoke tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from layer4.mcp_server import create_server


@pytest.fixture
def orchestrator():
    runtime = MagicMock()
    runtime.connect = AsyncMock()
    runtime.disconnect = AsyncMock()
    runtime.get_observation.return_value = {
        "generation": 1,
        "generation_token": "gen_1_test",
        "is_stable": True,
    }
    runtime.execute_tool = AsyncMock(return_value={"success": True, "generation": 2})
    runtime.state = SimpleNamespace(value="ready")
    return runtime


@pytest.mark.asyncio
async def test_mcp_tool_call_delegates_to_runtime_orchestrator(orchestrator):
    server = create_server(orchestrator=orchestrator)

    _content, structured = await server.call_tool("trigger_action", {
        "generation_token": "gen_1_test",
        "action_id": "ENTER",
    })

    assert structured == {"success": True, "generation": 2}
    orchestrator.execute_tool.assert_awaited_once_with(
        "trigger_action",
        {"generation_token": "gen_1_test", "action_id": "ENTER"},
    )


@pytest.mark.asyncio
async def test_mcp_read_tools_delegate_to_runtime(orchestrator):
    server = create_server(orchestrator=orchestrator)

    _content, structured = await server.call_tool("get_observation", {})
    assert structured == {
        "generation": 1,
        "generation_token": "gen_1_test",
        "is_stable": True,
    }
    _content, structured = await server.call_tool("get_screen_state", {})
    assert structured == {
        "success": True,
        "generation": 2,
    }
    orchestrator.execute_tool.assert_awaited_once_with("get_screen_state", {})