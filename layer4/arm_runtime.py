"""Layer 4 lifecycle adapter for the independent ARM L1/L2 pipeline."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tomllib
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from layer1.arm import ARMDriver, ARMObservationConfig, MemoryDecodeConfig, MemoryRequest
from layer1.arm.gdb_client import (
    validate_gdb_executable,
    validate_gdb_host,
    validate_gdb_port,
)
from layer2.arm import (
    ARMActionMapper,
    ARMActionStatus,
    ARMSemanticActionIntent,
    ARMStabilityPolicy,
    ARMState,
    ARMStateAbstractor,
)
from layer2.arm.models import StabilityStatus


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


class ARMRuntimeStartupError(RuntimeError):
    """Retain both an ARM startup failure and a failed cleanup attempt."""

    def __init__(self, startup_error: BaseException, cleanup_error: BaseException) -> None:
        self.startup_error = startup_error
        self.cleanup_error = cleanup_error
        super().__init__(
            f"ARM runtime startup failed ({type(startup_error).__name__}: {startup_error}); "
            f"disconnect cleanup also failed ({type(cleanup_error).__name__}: {cleanup_error})"
        )


class ARMRuntimeAdapter:
    """Own the configured ARM runtime and compose L1 observations with ARM L2."""

    def __init__(
        self,
        driver: ARMDriver,
        *,
        abstractor: ARMStateAbstractor | None = None,
        entry_breakpoint: str = "main",
        counter_symbol: str = "counter",
        counter_request_id: str = "counter",
        matching_samples: int = 2,
    ) -> None:
        if matching_samples < 2:
            raise ValueError("ARM runtime requires at least two stability samples")
        self.driver = driver
        self.abstractor = abstractor or ARMStateAbstractor(runtime_id=driver.runtime_id)
        self.mapper = ARMActionMapper(
            driver,
            self.abstractor,
            counter_symbol=counter_symbol,
            counter_request_id=counter_request_id,
        )
        self.runtime_id = driver.runtime_id
        self.entry_breakpoint = entry_breakpoint
        self.matching_samples = matching_samples
        self._lock = asyncio.Lock()
        self._connected = False
        self._driver_needs_disconnect = False
        self._generation = 0
        self._state: ARMState | None = None

    @classmethod
    def from_config(cls, config_path: str) -> ARMRuntimeAdapter:
        with open(config_path, "rb") as config_file:
            config = tomllib.load(config_file)
        target = config.get("target", {})
        arm = config.get("arm", {})
        firmware = arm.get("firmware") or os.environ.get("SENTINEL_ARM_FIRMWARE")
        if not isinstance(firmware, str) or not firmware.strip():
            raise ValueError("ARM configuration must set arm.firmware or SENTINEL_ARM_FIRMWARE")
        firmware = os.path.expandvars(firmware)
        if not Path(firmware).is_file():
            raise ValueError(f"configured ARM firmware file is unavailable: {firmware}")
        gdb_path = validate_gdb_executable(
            os.environ.get("SENTINEL_ARM_GDB", arm.get("gdb", "arm-none-eabi-gdb"))
        )
        if not shutil.which(gdb_path) and not Path(gdb_path).is_file():
            raise ValueError(f"configured ARM GDB executable is unavailable: {gdb_path}")
        host = validate_gdb_host(os.environ.get("SENTINEL_ARM_GDB_HOST", arm.get("host", "127.0.0.1")))
        port_value: object = os.environ.get("SENTINEL_ARM_GDB_PORT", arm.get("port", 1234))
        if isinstance(port_value, str):
            if not re.fullmatch(r"[0-9]+", port_value):
                raise ValueError("SENTINEL_ARM_GDB_PORT must be an integer from 1 to 65535")
            port_value = int(port_value, 10)
        port = validate_gdb_port(port_value)
        memory_cfg = arm.get("counter", {})
        symbol = memory_cfg.get("symbol", "counter")
        request_id = memory_cfg.get("request_id", "counter")
        observation_config = ARMObservationConfig(
            include_registers=arm.get("include_registers", True),
            include_pc=True,
            include_sp=arm.get("include_sp", True),
            status_registers=tuple(arm.get("status_registers", ["cpsr"])),
            include_execution=True,
            include_current_instruction=True,
            include_breakpoints=True,
            memory=(
                MemoryRequest(
                    request_id=request_id,
                    symbol=symbol,
                    size=4,
                    decode=MemoryDecodeConfig("uint32", memory_cfg.get("endianness", "little")),
                ),
            ),
        )
        driver = ARMDriver(
            firmware_path=firmware,
            gdb_path=gdb_path,
            gdb_host=host,
            gdb_port=port,
            runtime_id=target.get("runtime_id", "arm_qemu_01"),
            observation_config=observation_config,
            architecture=arm.get("architecture", "arm"),
            cpu=arm.get("cpu", "cortex-a15"),
        )
        stability_cfg = config.get("stability", {})
        abstractor = ARMStateAbstractor(
            runtime_id=driver.runtime_id,
            stability_policy=ARMStabilityPolicy(
                required_matching_samples=stability_cfg.get("matching_samples", 2),
                require_stopped_execution=True,
                sampling_interval_seconds=stability_cfg.get("sampling_interval_seconds", 0.05),
            ),
        )
        return cls(
            driver,
            abstractor=abstractor,
            entry_breakpoint=arm.get("entry_breakpoint", "main"),
            counter_symbol=symbol,
            counter_request_id=request_id,
            matching_samples=stability_cfg.get("matching_samples", 2),
        )

    async def connect(self) -> None:
        async with self._lock:
            if self._connected:
                return
            if self._driver_needs_disconnect:
                # A prior failed cleanup left the driver in an unknown partial
                # state. Do not open another GDB process until it is closed.
                await self.driver.disconnect()
                self._driver_needs_disconnect = False
            # Mark the attempt before calling L1 so a partial connect can still
            # be cleaned up if the driver raises after opening resources.
            self._driver_needs_disconnect = True
            try:
                await self.driver.connect()
                if self.entry_breakpoint:
                    await self.driver.run_to_breakpoint(self.entry_breakpoint)
            except BaseException as startup_error:
                try:
                    await self.driver.disconnect()
                except BaseException as cleanup_error:
                    self._driver_needs_disconnect = True
                    if isinstance(startup_error, asyncio.CancelledError):
                        setattr(startup_error, "arm_cleanup_error", cleanup_error)
                        startup_error.add_note(f"ARM disconnect cleanup also failed: {cleanup_error}")
                        raise
                    if isinstance(cleanup_error, asyncio.CancelledError):
                        setattr(cleanup_error, "arm_startup_error", startup_error)
                        cleanup_error.add_note(f"ARM startup also failed: {startup_error}")
                        raise cleanup_error from startup_error
                    raise ARMRuntimeStartupError(startup_error, cleanup_error) from startup_error
                else:
                    self._driver_needs_disconnect = False
                raise
            self._driver_needs_disconnect = True
            self._connected = True

    async def disconnect(self) -> None:
        async with self._lock:
            if self._driver_needs_disconnect:
                try:
                    await self.driver.disconnect()
                except BaseException:
                    self._connected = False
                    self._state = None
                    raise
                else:
                    self._driver_needs_disconnect = False
                    self._connected = False
                    self._state = None

    async def observe(self, runtime_id: str) -> dict[str, Any]:
        async with self._lock:
            self._require_runtime(runtime_id)
            if not self._connected:
                return {
                    "success": False,
                    "error": "runtime_unavailable",
                    "message": "ARM runtime is not connected",
                    "execution_attempted": False,
                }
            state: ARMState | None = None
            observation = None
            delta = None
            baseline = self._state
            for sample in range(self.matching_samples):
                frame = await self.driver.read_frame()
                observation = self.abstractor.parse_observation(frame)
                self._generation += 1
                state, delta = self.abstractor.abstract_state(
                    observation, self._generation, previous_state=baseline
                )
                if state.stability.status == StabilityStatus.STABLE:
                    break
                if sample + 1 < self.matching_samples:
                    delay = self.abstractor.stability.policy.sampling_interval_seconds
                    if delay:
                        await asyncio.sleep(delay)
            assert state is not None and observation is not None
            self._state = state
            return {
                "success": True,
                "runtime_id": self.runtime_id,
                "transport_frame": {
                    "frame_seq": observation.frame_seq,
                    "timestamp": observation.timestamp,
                    "is_eod": frame.is_eod,
                    "payload": observation.source_values,
                },
                "observation": _jsonable(observation),
                "state": _jsonable(state),
                "delta": _jsonable(delta),
            }

    async def execute_action(self, runtime_id: str, action: str, symbol: str) -> dict[str, Any]:
        async with self._lock:
            self._require_runtime(runtime_id)
            if action != "increment_counter":
                return {
                    "success": False,
                    "status": ARMActionStatus.REJECTED.value,
                    "error": "unsupported_intent",
                    "message": f"Unsupported ARM semantic action: {action!r}",
                    "execution_attempted": False,
                }
            if not isinstance(symbol, str) or symbol != self.mapper.counter_symbol:
                return {
                    "success": False,
                    "status": ARMActionStatus.REJECTED.value,
                    "error": "invalid_parameters",
                    "message": f"Only configured counter symbol {self.mapper.counter_symbol!r} is supported",
                    "execution_attempted": False,
                }
            if not self._connected:
                return {
                    "success": False,
                    "error": "runtime_unavailable",
                    "message": "ARM runtime is not connected",
                    "execution_attempted": False,
                }
            if self._state is None:
                await self._observe_locked()
            assert self._state is not None
            result = await self.mapper.execute(
                ARMSemanticActionIntent(action, {"symbol": symbol}), self._state
            )
            if result.after_state is not None:
                self._state = result.after_state
                self._generation = max(self._generation, result.after_state.generation)
            return {"success": result.succeeded, **_jsonable(result)}

    async def _observe_locked(self) -> ARMState:
        state: ARMState | None = None
        baseline = self._state
        for sample in range(self.matching_samples):
            frame = await self.driver.read_frame()
            observation = self.abstractor.parse_observation(frame)
            self._generation += 1
            state, _ = self.abstractor.abstract_state(
                observation, self._generation, previous_state=baseline
            )
            if state.stability.status == StabilityStatus.STABLE:
                break
            if sample + 1 < self.matching_samples:
                delay = self.abstractor.stability.policy.sampling_interval_seconds
                if delay:
                    await asyncio.sleep(delay)
        assert state is not None
        self._state = state
        return state

    def _require_runtime(self, runtime_id: str) -> None:
        if not isinstance(runtime_id, str) or runtime_id != self.runtime_id:
            raise ValueError(f"unknown ARM runtime: {runtime_id!r}")
