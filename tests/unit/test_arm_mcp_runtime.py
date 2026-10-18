"""Offline tests for the Layer 4 ARM runtime adapter and action routing."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from layer2.arm.models import StabilityStatus
from layer4.arm_runtime import ARMRuntimeAdapter, ARMRuntimeStartupError
from schemas.pipeline import TransportFrame


class FakeARMDriver:
    runtime_id = "arm-test"

    def __init__(self) -> None:
        self.value = 41
        self.sequence = 0
        self.phase = "initial"
        self.connected = False
        self._transaction_lock = asyncio.Lock()

    @asynccontextmanager
    async def execution_transaction(self):
        async with self._transaction_lock:
            yield

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def run_to_breakpoint(self, location: str) -> None:
        assert location == "main"

    async def resolve_symbol(self, symbol: str) -> int:
        assert symbol == "counter"
        return 0x40000054

    async def find_store_instruction(self, address: int, symbol: str = "main") -> int:
        assert address == 0x40000054 and symbol == "main"
        return 0x4000002C

    async def run_to_address(self, address: int) -> None:
        assert address == 0x4000002C
        self.phase = "breakpoint"

    async def step_instruction(self) -> None:
        self.value = (self.value + 1) & 0xFFFFFFFF
        self.phase = "after"

    async def read_frame(self) -> TransportFrame:
        self.sequence += 1
        pc = {"initial": 0x40000020, "breakpoint": 0x4000002C, "after": 0x40000030}[self.phase]
        reason = "end-stepping-range" if self.phase == "after" else "breakpoint-hit"
        instruction = (
            {"status": "observed", "value": {"address": pc, "text": "str r2, [r3, #0]"}}
            if self.phase == "breakpoint"
            else {"status": "not_requested"}
        )
        payload = {
            "environment": "arm",
            "schema_version": "2.0",
            "identity": {
                "runtime_id": self.runtime_id,
                "architecture": {"status": "observed", "value": "armv7-a"},
                "cpu": {"status": "observed", "value": "cortex-a15"},
                "firmware": {
                    "path": {"status": "observed", "value": "local.elf"},
                    "sha256": {"status": "observed", "value": "a" * 64},
                },
            },
            "cpu": {
                "registers": {"status": "not_requested"},
                "pc": {"status": "observed", "value": pc},
                "sp": {"status": "not_requested"},
                "status_flags": {},
            },
            "execution": {
                "state": {"status": "observed", "value": "stopped"},
                "current_instruction": instruction,
                "stop_reason": {"status": "observed", "value": reason},
                "breakpoint_hit": {
                    "status": "observed", "value": {"breakpoint_id": "3"}
                },
            },
            "memory": {
                "status": "observed",
                "regions": [{
                    "request_id": "counter", "status": "observed", "size": 4,
                    "address": 0x40000054, "requested_address": None, "symbol": "counter",
                    "raw_bytes_hex": self.value.to_bytes(4, "little").hex(),
                    "decode": {"type": "uint32", "endianness": "little", "signed": False},
                }],
            },
            "debugging": {"breakpoints": {"status": "observed", "value": []}},
            "metadata": {
                "frame_seq": self.sequence, "timestamp": float(self.sequence),
                "capabilities": ["memory", "execution"],
                "completeness": {"requested": ["memory:counter"], "observed": ["memory:counter"],
                                 "not_requested": [], "unavailable": [], "failed": []},
                "errors": [],
            },
        }
        return TransportFrame(
            raw_payload=json.dumps(payload).encode(),
            frame_seq=self.sequence,
            timestamp=float(self.sequence),
        )


@pytest.mark.asyncio
async def test_arm_mcp_runtime_observation_then_verified_action() -> None:
    driver = FakeARMDriver()
    runtime = ARMRuntimeAdapter(driver)  # type: ignore[arg-type]
    await runtime.connect()

    observed = await runtime.observe("arm-test")
    assert observed["success"] is True
    assert observed["transport_frame"]["frame_seq"] == 2
    assert observed["state"]["decoded_memory"][0]["value"] == 41
    assert observed["state"]["stability"]["status"] == StabilityStatus.STABLE.value

    repeated = await runtime.observe("arm-test")
    assert repeated["success"] is True
    assert repeated["transport_frame"]["frame_seq"] == 3
    assert repeated["delta"]["memory_changes"] == {}

    result = await runtime.execute_action("arm-test", "increment_counter", "counter")
    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["observed_value"] == 42
    assert result["delta"]["memory_changes"]["counter"]["old_value"] == 41
    assert result["delta"]["memory_changes"]["counter"]["new_value"] == 42
    assert result["evidence"]["single_step_verified"] is True
    await runtime.disconnect()
    assert driver.connected is False


@pytest.mark.asyncio
async def test_arm_mcp_runtime_rejects_wrong_runtime_and_unsupported_intent() -> None:
    runtime = ARMRuntimeAdapter(FakeARMDriver())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown ARM runtime"):
        await runtime.observe("freedos_node_01")
    result = await runtime.execute_action("arm-test", "write_memory", "counter")
    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["execution_attempted"] is False

    malformed = await runtime.execute_action("arm-test", "increment_counter", "other_symbol")
    assert malformed["success"] is False
    assert malformed["error"] == "invalid_parameters"
    assert malformed["execution_attempted"] is False

    unavailable = await runtime.execute_action("arm-test", "increment_counter", "counter")
    assert unavailable["success"] is False
    assert unavailable["error"] == "runtime_unavailable"
    assert unavailable["execution_attempted"] is False


def test_arm_config_environment_overrides_are_shared_driver_settings(tmp_path, monkeypatch):
    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"test firmware")
    config = tmp_path / "arm.toml"
    config.write_text(f'[arm]\nfirmware = {json.dumps(str(firmware))}\n', encoding="utf-8")
    monkeypatch.setenv("SENTINEL_ARM_GDB", "mock-gdb")
    monkeypatch.setenv("SENTINEL_ARM_GDB_HOST", "localhost")
    monkeypatch.setenv("SENTINEL_ARM_GDB_PORT", "4321")
    monkeypatch.setattr("layer4.arm_runtime.shutil.which", lambda _name: "mock-gdb")

    adapter = ARMRuntimeAdapter.from_config(str(config))

    assert adapter.driver.client.gdb_path == "mock-gdb"
    assert adapter.driver.gdb_host == "localhost"
    assert adapter.driver.gdb_port == 4321


@pytest.mark.asyncio
async def test_connect_preserves_startup_and_disconnect_failures_and_can_retry_cleanup():
    driver = FakeARMDriver()
    startup_error = RuntimeError("breakpoint setup failed")
    cleanup_error = OSError("process close failed")
    driver.connect = AsyncMock(side_effect=startup_error)  # type: ignore[method-assign]
    driver.disconnect = AsyncMock(side_effect=[cleanup_error, None])  # type: ignore[method-assign]
    runtime = ARMRuntimeAdapter(driver, entry_breakpoint="")  # type: ignore[arg-type]

    with pytest.raises(ARMRuntimeStartupError) as caught:
        await runtime.connect()
    assert caught.value.startup_error is startup_error
    assert caught.value.cleanup_error is cleanup_error
    assert runtime._driver_needs_disconnect is True

    await runtime.disconnect()
    assert driver.disconnect.await_count == 2
    assert runtime._driver_needs_disconnect is False
    assert runtime._connected is False
