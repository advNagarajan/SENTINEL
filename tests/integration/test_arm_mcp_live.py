"""Live MCP-to-ARM-QEMU test; requires an already-running configured target."""

import os
import socket
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from layer4.arm_runtime import ARMRuntimeAdapter

ROOT = Path(__file__).resolve().parents[2]


def _runtime_config() -> tuple[str, str, int, str, str, str]:
    config_path = os.environ.get("SENTINEL_ARM_CONFIG", "configs/arm.toml")
    path = Path(config_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        pytest.skip(f"ARM MCP configuration not found: {path}")
    try:
        adapter = ARMRuntimeAdapter.from_config(str(path))
    except ValueError as exc:
        message = str(exc)
        if "firmware file is unavailable" in message or "GDB executable is unavailable" in message:
            pytest.skip(message)
        raise
    driver = adapter.driver
    return (
        str(path),
        driver.gdb_host,
        driver.gdb_port,
        driver.client.gdb_path,
        driver.firmware_path,
        adapter.runtime_id,
    )


def _counter(state: dict) -> int:
    values = [
        item["value"] for item in state["decoded_memory"]
        if item["request_id"] == "counter" and item["decode_status"] == "observed"
    ]
    assert len(values) == 1, "ARM L2 state must contain one decoded counter"
    return values[0]


@pytest.mark.asyncio
async def test_live_arm_qemu_observation_and_action_cross_mcp_boundary() -> None:
    if os.environ.get("SENTINEL_ARM_LIVE_TEST") != "1":
        pytest.skip("set SENTINEL_ARM_LIVE_TEST=1 to opt in; this test increments the QEMU counter")
    config_path, host, port, gdb_path, _firmware, runtime_id = _runtime_config()
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        pytest.skip(f"configured QEMU GDB endpoint {host}:{port} is unavailable")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.mcp_arm_live_server"],
        cwd=str(ROOT),
        env={
            **os.environ,
            "SENTINEL_ARM_CONFIG": config_path,
            "SENTINEL_ARM_GDB": gdb_path,
            "SENTINEL_ARM_GDB_HOST": host,
            "SENTINEL_ARM_GDB_PORT": str(port),
        },
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"get_arm_observation", "execute_arm_action"} <= names

        before_response = await session.call_tool(
            "get_arm_observation", {"runtime_id": runtime_id}
        )
        before_result = before_response.structuredContent
        assert before_result["success"] is True, before_result
        before_state = before_result["state"]
        before_counter = _counter(before_state)
        assert before_state["environment"] == "arm"
        assert before_state["stability"]["status"] == "stable"
        assert before_state["source_frame_seq"] > 0

        action_response = await session.call_tool(
            "execute_arm_action",
            {
                "runtime_id": runtime_id,
                "action": "increment_counter",
                "symbol": "counter",
            },
        )
        action = action_response.structuredContent
        assert action["success"] is True, action
        assert action["status"] == "succeeded"
        assert action["execution_attempted"] is True
        assert action["observed_value"] == ((before_counter + 1) & 0xFFFFFFFF)
        assert action["evidence"]["breakpoint_verified"] is True
        assert action["evidence"]["single_step_verified"] is True
        assert action["after_state"]["decoded_memory"]
        assert action["delta"]["memory_changes"]["counter"]["new_value"] == action["observed_value"]

        after_response = await session.call_tool(
            "get_arm_observation", {"runtime_id": runtime_id}
        )
        after_result = after_response.structuredContent
        assert after_result["success"] is True, after_result
        assert _counter(after_result["state"]) == action["observed_value"]

