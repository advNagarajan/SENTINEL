"""Parse rich ARM L1 observations and produce semantic ARM states and deltas."""

import json
import re
from dataclasses import replace
from typing import Any

from schemas.pipeline import TransportFrame

from layer2.arm.models import (
    ARMDecodedMemory,
    ARMExecutionState,
    ARMInstruction,
    ARMMemoryObservation,
    ARMObservation,
    ARMRegister,
    ARMState,
    ARMStateDelta,
    ARMStability,
    CapturedValue,
    ObservationStatus,
    StabilityStatus,
    semantic_state_hash,
)
from layer2.arm.stability import ARMStabilityAssessment, ARMStabilityPolicy
from layer2.arm.validation import validate_arm_observation, validate_arm_state


def _status(value: Any) -> ObservationStatus:
    try:
        return ObservationStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unknown ARM observation status: {value!r}") from exc


def _captured(raw: Any, convert: Any = None) -> CapturedValue[Any]:
    if not isinstance(raw, dict) or "status" not in raw:
        raise ValueError("captured ARM values must be status objects")
    status = _status(raw["status"])
    value = raw.get("value")
    if status == ObservationStatus.OBSERVED and convert is not None:
        value = convert(value)
    error = raw.get("error")
    if error is not None and not isinstance(error, dict):
        error = {"message": str(error)}
    return CapturedValue(status, value, raw.get("raw_value"), error, raw.get("source"))


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric ARM value")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)(?=\s|$)", value)
        if match is None:
            raise ValueError(f"invalid numeric ARM value: {value!r}")
        parsed = int(match.group(1), 0)
    else:
        raise ValueError(f"invalid numeric ARM value: {value!r}")
    if parsed < 0:
        raise ValueError("ARM numeric values must be non-negative")
    return parsed


class ARMStateAbstractor:
    """Convert ARM L1 TransportFrames into validated ARM observations and states."""

    def __init__(
        self,
        runtime_id: str | None = None,
        stability_policy: ARMStabilityPolicy | None = None,
    ) -> None:
        self.runtime_id = runtime_id
        self.stability = ARMStabilityAssessment(stability_policy)
        self._last_frame_seq: int | None = None

    def parse_observation(self, frame: TransportFrame) -> ARMObservation:
        try:
            raw = json.loads(frame.raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"ARM TransportFrame contains malformed JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("ARM TransportFrame JSON root must be an object")
        if raw.get("environment") != "arm":
            raise ValueError(f"Expected ARM observation environment, received {raw.get('environment')!r}")
        identity = raw.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("ARM observation identity must be an object")
        runtime_id = identity.get("runtime_id", self.runtime_id)
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("ARM observation must include runtime_id")
        cpu = raw.get("cpu")
        execution = raw.get("execution")
        debugging = raw.get("debugging")
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("ARM observation metadata must be an object")
        payload_seq = metadata.get("frame_seq")
        if (not isinstance(frame.frame_seq, int) or isinstance(frame.frame_seq, bool)
                or not isinstance(payload_seq, int) or isinstance(payload_seq, bool)
                or frame.frame_seq < 0 or payload_seq != frame.frame_seq):
            raise ValueError("TransportFrame and ARM payload frame sequences must be matching non-negative integers")
        if self._last_frame_seq is not None and frame.frame_seq <= self._last_frame_seq:
            raise ValueError("ARM TransportFrame sequence is stale, duplicated, or out of order")
        memory_root = raw.get("memory")
        if not all(isinstance(item, dict) for item in (cpu, execution, debugging, metadata, memory_root)):
            raise ValueError("ARM observation cpu/execution/debugging/memory/metadata sections must be objects")

        registers_raw = cpu.get("registers", {"status": "not_requested"})
        registers_status = _status(registers_raw.get("status"))
        registers: list[ARMRegister] = []
        if registers_status == ObservationStatus.OBSERVED:
            for item in registers_raw.get("values", []):
                number = item.get("number")
                if not isinstance(number, int) or isinstance(number, bool):
                    raise ValueError("register number must be an integer")
                status = _status(item.get("status", "observed"))
                source = item.get("raw_value")
                try:
                    value = _integer(item.get("value")) if status == ObservationStatus.OBSERVED else None
                    error = item.get("error")
                except ValueError as exc:
                    status, value, error = ObservationStatus.FAILED, None, {"type": "ValueError", "message": str(exc)}
                registers.append(
                    ARMRegister(number, item.get("name"), CapturedValue(status, value, source, error, "GDB register value"))
                )
        registers_value = CapturedValue(
            registers_status,
            tuple(sorted(registers, key=lambda register: register.number)) if registers_status == ObservationStatus.OBSERVED else None,
            error=registers_raw.get("error"),
        )

        def integer_capture(section: dict[str, Any], key: str) -> CapturedValue[int]:
            value = _captured(section.get(key, {"status": "not_requested"}), _integer)
            return value

        status_registers = {
            str(name): _captured(value, _integer)
            for name, value in cpu.get("status_flags", {}).items()
        }
        execution_raw = execution.get("state", {"status": "not_requested"})
        execution_state = _captured(
            execution_raw,
            lambda value: ARMExecutionState(value),
        )
        instruction_raw = execution.get("current_instruction", {"status": "not_requested"})
        instruction_capture = _captured(
            instruction_raw,
            lambda value: ARMInstruction(_integer(value["address"]), str(value["text"]), value.get("raw")),
        )
        breakpoint_hit = _captured(execution.get("breakpoint_hit", {"status": "not_requested"}))

        memories: list[ARMMemoryObservation] = []
        regions = memory_root.get("regions", [])
        for item in regions:
            memories.append(
                ARMMemoryObservation(
                    request_id=str(item.get("request_id", "")),
                    status=_status(item.get("status")),
                    size=item.get("size"),
                    address=_integer(item["address"]) if item.get("address") is not None else None,
                    requested_address=_integer(item["requested_address"]) if item.get("requested_address") is not None else None,
                    symbol=item.get("symbol"),
                    raw_bytes_hex=item.get("raw_bytes_hex"),
                    decode=item.get("decode"),
                    error=item.get("error"),
                )
            )

        breakpoint_raw = debugging.get("breakpoints", {"status": "not_requested"})
        breakpoint_status = _status(breakpoint_raw.get("status"))
        breakpoint_values = tuple(breakpoint_raw.get("value", breakpoint_raw.get("values", []))) if breakpoint_status == ObservationStatus.OBSERVED else None
        stop_raw = execution.get("stop_reason", {"status": "not_requested"})
        stop_capture = _captured(stop_raw, lambda value: str(value))
        firmware = identity.get("firmware", {})
        frame_seq = metadata.get("frame_seq", frame.frame_seq)
        timestamp = metadata.get("timestamp", frame.timestamp)
        observation = ARMObservation(
            runtime_id=runtime_id,
            environment="arm",
            schema_version=str(raw.get("schema_version", "2.0")),
            architecture=_captured(identity.get("architecture", {"status": "unavailable"}), str),
            cpu=_captured(identity.get("cpu", {"status": "unavailable"}), str),
            firmware_path=_captured(firmware.get("path", {"status": "unavailable"}), str),
            firmware_sha256=_captured(firmware.get("sha256", {"status": "unavailable"}), str),
            registers=registers_value,
            pc=integer_capture(cpu, "pc"),
            sp=integer_capture(cpu, "sp"),
            status_registers=status_registers,
            execution_state=execution_state,
            current_instruction=instruction_capture,
            stop_reason=stop_capture,
            breakpoint_hit=breakpoint_hit,
            memory=tuple(memories),
            breakpoints=CapturedValue(breakpoint_status, breakpoint_values, error=breakpoint_raw.get("error")),
            frame_seq=frame_seq,
            timestamp=float(timestamp),
            capabilities=tuple(metadata.get("capabilities", [])),
            completeness={key: tuple(value) for key, value in metadata.get("completeness", {}).items()},
            errors=tuple(metadata.get("errors", [])),
            source_values=raw,
        )
        validate_arm_observation(observation)
        self._last_frame_seq = frame.frame_seq
        return observation

    def abstract_state(
        self,
        observation: ARMObservation,
        generation: int,
        previous_state: ARMState | None = None,
    ) -> tuple[ARMState, ARMStateDelta | None]:
        validate_arm_observation(observation)
        if generation < 0:
            raise ValueError("generation must be non-negative")
        decoded_memory = tuple(self._decode_memory(item) for item in observation.memory)
        state = ARMState(
            runtime_id=observation.runtime_id,
            generation=generation,
            environment=observation.environment,
            architecture=observation.architecture,
            cpu=observation.cpu,
            firmware_path=observation.firmware_path,
            firmware_sha256=observation.firmware_sha256,
            registers=observation.registers,
            pc=observation.pc,
            sp=observation.sp,
            status_registers=observation.status_registers,
            execution_state=observation.execution_state,
            current_instruction=observation.current_instruction,
            stop_reason=observation.stop_reason,
            breakpoint_hit=observation.breakpoint_hit,
            memory=observation.memory,
            decoded_memory=decoded_memory,
            breakpoints=observation.breakpoints,
            stability=ARMStability(),
            completeness=observation.completeness,
            errors=observation.errors,
            state_hash="",
            source_frame_seq=observation.frame_seq,
            timestamp=observation.timestamp,
            schema_version=observation.schema_version,
            capabilities=observation.capabilities,
            provenance={"source": "ARM L1 TransportFrame", "frame_seq": observation.frame_seq},
        )
        state = ARMState(**{**state.__dict__, "state_hash": semantic_state_hash(state)})
        state = replace(state, stability=self.stability.assess(state))
        validate_arm_state(state)
        delta = self._build_delta(previous_state, state) if previous_state is not None else None
        return state, delta

    @staticmethod
    def _decode_memory(memory: ARMMemoryObservation) -> ARMDecodedMemory:
        if memory.status != ObservationStatus.OBSERVED or memory.raw_bytes_hex is None:
            return ARMDecodedMemory(memory.request_id, memory.address or 0, memory.symbol, memory.size, memory.raw_bytes_hex or "", None, None, None, memory.status)
        decode = memory.decode or {}
        if not isinstance(decode, dict):
            return ARMDecodedMemory(memory.request_id, memory.address or 0, memory.symbol, memory.size, memory.raw_bytes_hex, None, None, None, ObservationStatus.FAILED)
        value_type = decode.get("type")
        endianness = decode.get("endianness")
        if not value_type or not endianness:
            return ARMDecodedMemory(memory.request_id, memory.address or 0, memory.symbol, memory.size, memory.raw_bytes_hex, None, value_type, endianness, ObservationStatus.NOT_REQUESTED)
        raw = bytes.fromhex(memory.raw_bytes_hex)
        type_match = re.fullmatch(r"(u?int)(8|16|32|64)", value_type) if isinstance(value_type, str) else None
        expected_size = int(type_match.group(2)) // 8 if type_match else None
        expected_signed = type_match is not None and type_match.group(1) == "int"
        signed = decode.get("signed", expected_signed)
        if (expected_size != memory.size or endianness not in {"little", "big"}
                or not isinstance(signed, bool) or signed != expected_signed):
            return ARMDecodedMemory(memory.request_id, memory.address or 0, memory.symbol, memory.size, memory.raw_bytes_hex, None, value_type, endianness, ObservationStatus.FAILED)
        value = int.from_bytes(raw, endianness, signed=signed)
        return ARMDecodedMemory(memory.request_id, memory.address or 0, memory.symbol, memory.size, memory.raw_bytes_hex, value, value_type, endianness, ObservationStatus.OBSERVED)

    @staticmethod
    def _value(captured: CapturedValue[Any]) -> Any:
        return captured.value if captured.status == ObservationStatus.OBSERVED else None

    @classmethod
    def _build_delta(cls, previous: ARMState, current: ARMState) -> ARMStateDelta:
        if previous.runtime_id != current.runtime_id:
            raise ValueError("cannot build ARMStateDelta across different runtimes")
        for label, old, new in (("firmware SHA-256", previous.firmware_sha256, current.firmware_sha256),):
            if old.status != ObservationStatus.OBSERVED or new.status != ObservationStatus.OBSERVED or old.value != new.value:
                raise ValueError(f"cannot build ARMStateDelta without matching observed {label}")
        reg_changes: dict[str, dict[str, int | None]] = {}
        old_regs = {reg.name or f"reg_{reg.number}": reg.observation for reg in (previous.registers.value or ())}
        new_regs = {reg.name or f"reg_{reg.number}": reg.observation for reg in (current.registers.value or ())}
        for name in old_regs.keys() & new_regs.keys():
            old, new = old_regs[name], new_regs[name]
            if old.status == new.status == ObservationStatus.OBSERVED and old.value != new.value:
                reg_changes[name] = {"old": old.value, "new": new.value}
        status_changes = {}
        for name in previous.status_registers.keys() & current.status_registers.keys():
            old, new = previous.status_registers[name], current.status_registers[name]
            if old.status == new.status == ObservationStatus.OBSERVED and old.value != new.value:
                status_changes[name] = {"old": old.value, "new": new.value}
        old_mem = {m.request_id: m for m in previous.memory}
        new_mem = {m.request_id: m for m in current.memory}
        memory_changes = {}
        for key in old_mem.keys() & new_mem.keys():
            old, new = old_mem[key], new_mem[key]
            if old.status == new.status == ObservationStatus.OBSERVED and old.raw_bytes_hex != new.raw_bytes_hex:
                old_value = next((v.value for v in previous.decoded_memory if v.request_id == key and v.decode_status == ObservationStatus.OBSERVED), None)
                new_value = next((v.value for v in current.decoded_memory if v.request_id == key and v.decode_status == ObservationStatus.OBSERVED), None)
                memory_changes[key] = {"old_raw_bytes_hex": old.raw_bytes_hex, "new_raw_bytes_hex": new.raw_bytes_hex, "old_value": old_value, "new_value": new_value}
        old_bp = {str(b.get("id")): b for b in (previous.breakpoints.value or ())}
        new_bp = {str(b.get("id")): b for b in (current.breakpoints.value or ())}
        bp_changes = {}
        if previous.breakpoints.status == current.breakpoints.status == ObservationStatus.OBSERVED:
            for key in old_bp.keys() | new_bp.keys():
                if old_bp.get(key) != new_bp.get(key):
                    bp_changes[key] = {"old": old_bp.get(key), "new": new_bp.get(key)}
        def captured_change(old: CapturedValue[Any], new: CapturedValue[Any]) -> dict[str, Any] | None:
            if old.status == new.status == ObservationStatus.OBSERVED and old.value != new.value:
                return {"old": old.value, "new": new.value}
            return None

        return ARMStateDelta(
            previous.generation, current.generation, previous.state_hash, current.state_hash,
            reg_changes,
            captured_change(previous.pc, current.pc),
            captured_change(previous.sp, current.sp),
            status_changes, memory_changes,
            captured_change(previous.execution_state, current.execution_state),
            captured_change(previous.stop_reason, current.stop_reason), bp_changes,
        )
