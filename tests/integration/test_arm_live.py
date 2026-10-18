"""Opt-in integration test for the ARM counter proof firmware in QEMU."""

import os
import shutil
import socket

import pytest

from layer1.arm import ARMDriver, ARMObservationConfig, MemoryDecodeConfig, MemoryRequest
from layer2.arm import ARMExecutionState, ARMStateAbstractor, ObservationStatus, StabilityStatus
from services.arm_demo import ARMEndToEnd, validate_counter_transition


@pytest.mark.asyncio
async def test_arm_l1_l2_increment_counter_end_to_end():
    if os.environ.get("SENTINEL_ARM_LIVE_TEST") != "1":
        pytest.skip("set SENTINEL_ARM_LIVE_TEST=1 to opt in; this test increments the QEMU counter")
    firmware = os.environ.get("SENTINEL_ARM_FIRMWARE", r"C:\sentinel-arm\firmware.elf")
    host = os.environ.get("SENTINEL_ARM_GDB_HOST", "127.0.0.1")
    port = int(os.environ.get("SENTINEL_ARM_GDB_PORT", "1234"))
    gdb_path = os.environ.get("SENTINEL_ARM_GDB", "arm-none-eabi-gdb")
    if not os.path.isfile(firmware):
        pytest.skip(f"ARM firmware not found: {firmware}")
    if not shutil.which(gdb_path):
        pytest.skip(f"GDB executable not found: {gdb_path}")
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        pytest.skip(f"QEMU GDB endpoint {host}:{port} is not reachable")

    config = ARMObservationConfig(
        include_registers=False,
        include_sp=False,
        status_registers=(),
        memory=(MemoryRequest("counter", 4, symbol="counter", decode=MemoryDecodeConfig("uint32", "little")),)
    )
    driver = ARMDriver(firmware, gdb_path=gdb_path, gdb_host=host, gdb_port=port, observation_config=config)
    flow = ARMEndToEnd(driver, ARMStateAbstractor())
    try:
        await flow.connect_at_main()
        before, _ = await flow.observe_boundary()
        after, delta = await flow.increment_and_observe()
        before_counter = next(item.value for item in before.decoded_memory if item.request_id == "counter")
        after_counter = next(item.value for item in after.decoded_memory if item.request_id == "counter")
        assert before_counter is not None
        assert after_counter == ((before_counter + 1) & 0xFFFFFFFF)
        assert delta is not None
        assert delta.memory_changes["counter"]["old_value"] == before_counter
        assert delta.memory_changes["counter"]["new_value"] == after_counter
        for state in (before, after):
            if state.execution_state.status == ObservationStatus.OBSERVED:
                assert state.execution_state.value == ARMExecutionState.STOPPED
                assert state.stability.status == StabilityStatus.STABLE
            else:
                assert state.stability.status == StabilityStatus.UNKNOWN
        validation = validate_counter_transition(before, after, delta)
        if (
            before.stability.status == StabilityStatus.STABLE
            and after.stability.status == StabilityStatus.STABLE
            and after.stop_reason.status == ObservationStatus.OBSERVED
        ):
            assert validation.valid
        else:
            assert not validation.valid
    finally:
        await flow.close()
