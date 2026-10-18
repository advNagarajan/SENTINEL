"""Thin ARM L1-to-L2 live demo composition for an already-running QEMU target."""

import argparse
import asyncio
from dataclasses import dataclass
import os
import shutil
import socket
from typing import TextIO
import sys

from layer1.arm import ARMDriver, ARMObservationConfig, MemoryDecodeConfig, MemoryRequest
from layer2.arm import (
    ARMActionMapper,
    ARMActionResult,
    ARMActionStatus,
    ARMState,
    ARMStateAbstractor,
    ARMStateDelta,
    ARMSemanticActionIntent,
)
from layer2.arm.models import ARMExecutionState, ObservationStatus
from layer2.arm.models import StabilityStatus


@dataclass(frozen=True)
class ARMTransitionValidation:
    valid: bool
    reasons: tuple[str, ...]


def validate_counter_transition(
    before: ARMState,
    after: ARMState,
    delta: ARMStateDelta | None,
    counter_request_id: str = "counter",
) -> ARMTransitionValidation:
    """Validate a real stopped-boundary counter increment against stable L2 states."""
    reasons: list[str] = []
    if before.stability.status != StabilityStatus.STABLE or before.stability.stable is not True:
        reasons.append("before-state lacks repeated stable-state evidence")
    if after.stability.status != StabilityStatus.STABLE or after.stability.stable is not True:
        reasons.append("after-state lacks repeated stable-state evidence")
    if after.execution_state.status != ObservationStatus.OBSERVED or after.execution_state.value != ARMExecutionState.STOPPED:
        reasons.append("after-state lacks GDB/MI stopped execution evidence")
    if after.stop_reason.status != ObservationStatus.OBSERVED:
        reasons.append("after-state lacks a GDB/MI stop reason at the post-action boundary")
    if delta is None:
        reasons.append("ARM L2 did not produce a state delta")
    else:
        change = delta.memory_changes.get(counter_request_id)
        if change is None:
            reasons.append(f"ARM L2 delta has no change for {counter_request_id!r}")
        elif (not isinstance(change.get("old_value"), int)
              or change.get("new_value") != ((change.get("old_value") + 1) & 0xFFFFFFFF)):
            reasons.append(f"{counter_request_id!r} semantic value did not increment by one")
    return ARMTransitionValidation(not reasons, tuple(reasons))


class ARMEndToEnd:
    """Compose ARM L1 observations with ARM L2 semantic state abstraction."""

    def __init__(self, driver: ARMDriver, abstractor: ARMStateAbstractor | None = None) -> None:
        self.driver = driver
        self.abstractor = abstractor or ARMStateAbstractor()
        self.action_mapper = ARMActionMapper(driver, self.abstractor)
        self.previous_state: ARMState | None = None
        self.last_action_result: ARMActionResult | None = None

    async def connect_at_main(self) -> None:
        await self.driver.connect()
        await self.driver.run_to_breakpoint("main")

    async def observe(self, delta_baseline: ARMState | None = None) -> tuple[ARMState, ARMStateDelta | None]:
        frame = await self.driver.read_frame()
        observation = self.abstractor.parse_observation(frame)
        state, delta = self.abstractor.abstract_state(
            observation,
            generation=(self.previous_state.generation + 1 if self.previous_state else 1),
            previous_state=self.previous_state if delta_baseline is None else delta_baseline,
        )
        self.previous_state = state
        return state, delta

    async def observe_boundary(self, max_samples: int | None = None) -> tuple[ARMState, ARMStateDelta | None]:
        """Take consecutive L1 samples until L2 confirms stability or its sample budget ends."""
        baseline = self.previous_state
        sample_limit = max_samples or self.abstractor.stability.policy.required_matching_samples
        state: ARMState | None = None
        delta: ARMStateDelta | None = None
        for sample_index in range(sample_limit):
            state, delta = await self.observe(delta_baseline=baseline)
            if state.stability.status == StabilityStatus.STABLE:
                break
            delay = self.abstractor.stability.policy.sampling_interval_seconds
            if delay and sample_index + 1 < sample_limit:
                await asyncio.sleep(delay)
        assert state is not None
        return state, delta

    async def increment_and_observe(self) -> tuple[ARMState, ARMStateDelta | None]:
        result = await self.perform_action(ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}))
        if not result.succeeded or result.after_state is None:
            raise RuntimeError(f"ARM semantic action {result.status.value}: {result.message}")
        return result.after_state, result.delta

    async def perform_action(self, intent: ARMSemanticActionIntent) -> ARMActionResult:
        if self.previous_state is None:
            result = ARMActionResult(
                ARMActionStatus.REJECTED,
                intent.action,
                "an observed ARMState is required before requesting an action",
            )
        else:
            result = await self.action_mapper.execute(intent, self.previous_state)
        self.last_action_result = result
        if result.after_state is not None:
            self.previous_state = result.after_state
        return result

    async def close(self) -> None:
        await self.driver.disconnect()


def _display_value(value: object, status: object) -> str:
    rendered = getattr(value, "value", value)
    return str(rendered) if getattr(status, "value", status) == "observed" else f"<{getattr(status, 'value', status)}>"


def _counter(state: ARMState, request_id: str) -> tuple[int | None, str | None]:
    raw = next((item for item in state.memory if item.request_id == request_id), None)
    semantic = next((item for item in state.decoded_memory if item.request_id == request_id), None)
    return (semantic.value if semantic else None, raw.raw_bytes_hex if raw else None)


def _print_state(state: ARMState, label: str, stream: TextIO) -> None:
    counter, raw_bytes = _counter(state, "counter")
    pc = f"0x{state.pc.value:08x}" if state.pc.status.value == "observed" else f"<{state.pc.status.value}>"
    execution = _display_value(state.execution_state.value, state.execution_state.status)
    requested = len(state.completeness.get("requested", ()))
    observed = len(state.completeness.get("observed", ()))
    not_requested = len(state.completeness.get("not_requested", ()))
    unavailable = len(state.completeness.get("unavailable", ()))
    failed = len(state.completeness.get("failed", ()))
    print(f"\n[{label}] Runtime observation", file=stream)
    print(f"Frame: {state.source_frame_seq}", file=stream)
    print(f"PC: {pc}", file=stream)
    print(f"Execution: {execution}", file=stream)
    if state.stop_reason.status.value == "observed":
        print(f"Stop reason: {state.stop_reason.value}", file=stream)
    print(f"Counter raw bytes (L1): {raw_bytes or '<unavailable>'}", file=stream)
    print("\n[L2] Semantic abstraction", file=stream)
    print(f"Counter (L2 decoded): {counter if counter is not None else '<unavailable>'}", file=stream)
    print(f"State hash: {state.state_hash}", file=stream)
    print(
        f"Completeness: {observed}/{requested} observed; {unavailable} unavailable; "
        f"{failed} failed; {not_requested} not requested",
        file=stream,
    )
    print(f"Stability: {state.stability.status.value} ({state.stability.reason})", file=stream)


async def run_live_demo(
    firmware: str,
    host: str = "127.0.0.1",
    port: int = 1234,
    gdb_path: str = "arm-none-eabi-gdb",
    output: TextIO | None = None,
) -> tuple[ARMState, ARMState, ARMStateDelta]:
    """Run a real observe/increment/observe demonstration; never launches QEMU."""
    stream = output or sys.stdout
    config = ARMObservationConfig(
        include_registers=False,
        include_sp=False,
        status_registers=(),
        memory=(MemoryRequest("counter", 4, symbol="counter", decode=MemoryDecodeConfig("uint32", "little")),)
    )
    driver = ARMDriver(firmware, gdb_path=gdb_path, gdb_host=host, gdb_port=port, observation_config=config)
    flow = ARMEndToEnd(driver)
    print("=" * 58, file=stream)
    print("SENTINEL ARM L1 -> L2 LIVE DEMO", file=stream)
    print("=" * 58, file=stream)
    try:
        await flow.connect_at_main()
        before, _ = await flow.observe_boundary()
        _print_state(before, "L1", stream)
        intent = ARMSemanticActionIntent("increment_counter", {"symbol": "counter"})
        print(f"\n[SEMANTIC REQUEST] {intent.action} {dict(intent.parameters)}", file=stream)
        result = await flow.perform_action(intent)
        print(
            f"[L2] Mapping: {result.status.value} — {result.message}",
            file=stream,
        )
        if result.plan is not None:
            print(
                f"[L2] Resolved {result.plan.target_symbol} at 0x{result.plan.target_address:08x}; "
                f"store instruction at 0x{result.plan.store_instruction_address:08x}",
                file=stream,
            )
        if result.evidence is not None:
            print(
                f"[L1] Breakpoint verified and cleaned; stepped {result.evidence.instruction_text!r}; "
                f"PC after step: 0x{result.evidence.post_step_pc:08x}",
                file=stream,
            )
        if not result.succeeded or result.after_state is None:
            raise RuntimeError(f"ARM action did not succeed ({result.status.value}): {result.message}")
        after, delta = result.after_state, result.delta
        _print_state(after, "L1", stream)
        if delta is None:
            raise RuntimeError("ARM L2 did not produce a state delta")
        change = delta.memory_changes.get("counter")
        if change is None:
            raise RuntimeError("ARM L2 did not observe a counter change")
        print("\n[L2] STATE DELTA", file=stream)
        print(f"counter: {change['old_value']} -> {change['new_value']}", file=stream)
        print("changed: true", file=stream)
        validation = validate_counter_transition(before, after, delta)
        section = "VALIDATED STATE TRANSITION" if validation.valid else "STATE TRANSITION VALIDATION"
        print(f"\n[L2] {section}", file=stream)
        print(f"stable_before: {before.stability.stable is True}", file=stream)
        print(f"stable_after: {after.stability.stable is True}", file=stream)
        print(f"transition: {'valid' if validation.valid else 'not validated'}", file=stream)
        for reason in validation.reasons:
            print(f"reason: {reason}", file=stream)
        print("\n" + "=" * 58, file=stream)
        print("END-TO-END L1 -> L2: SUCCESS", file=stream)
        print("=" * 58, file=stream)
        return before, after, delta
    finally:
        await flow.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Demonstrate ARM L1 → L2 against an existing QEMU GDB endpoint")
    parser.add_argument("--firmware", default=os.environ.get("SENTINEL_ARM_FIRMWARE", r"C:\sentinel-arm\firmware.elf"))
    parser.add_argument("--host", default=os.environ.get("SENTINEL_ARM_GDB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SENTINEL_ARM_GDB_PORT", "1234")))
    parser.add_argument("--gdb", default=os.environ.get("SENTINEL_ARM_GDB", "arm-none-eabi-gdb"))
    args = parser.parse_args()
    if not os.path.isfile(args.firmware):
        parser.error(f"ARM firmware not found: {args.firmware}")
    if not shutil.which(args.gdb):
        parser.error(f"GDB executable not found: {args.gdb}")
    try:
        with socket.create_connection((args.host, args.port), timeout=1):
            pass
    except OSError as exc:
        parser.error(f"QEMU GDB endpoint {args.host}:{args.port} is unavailable: {exc}")
    asyncio.run(run_live_demo(args.firmware, args.host, args.port, args.gdb))


if __name__ == "__main__":
    main()
