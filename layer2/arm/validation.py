"""Validation rules for the ARM observation and state contracts."""

import re

from layer2.arm.models import (
    ARMObservation,
    ARMState,
    CapturedValue,
    ObservationStatus,
    StabilityStatus,
    semantic_state_hash,
)


class ARMValidationError(ValueError):
    """Raised when an ARM observation or semantic state violates its contract."""


def _validate_captured(value: CapturedValue[object], field: str) -> None:
    if not isinstance(value, CapturedValue):
        raise ARMValidationError(f"{field} must be a CapturedValue")
    if value.status == ObservationStatus.OBSERVED and value.value is None:
        raise ARMValidationError(f"{field} observed status requires a value")
    if value.status != ObservationStatus.OBSERVED and value.value is not None:
        raise ARMValidationError(f"{field} non-observed status must not contain a value")
    if value.status == ObservationStatus.FAILED and not value.error:
        raise ARMValidationError(f"{field} failed status requires error information")


def validate_arm_observation(observation: ARMObservation) -> None:
    if not isinstance(observation, ARMObservation):
        raise ARMValidationError("observation must be ARMObservation")
    if not observation.runtime_id.strip():
        raise ARMValidationError("runtime_id must be non-empty")
    if observation.environment != "arm":
        raise ARMValidationError("environment must be 'arm'")
    if not observation.schema_version:
        raise ARMValidationError("schema_version must be non-empty")
    if (not isinstance(observation.frame_seq, int) or isinstance(observation.frame_seq, bool)
            or observation.frame_seq < 0 or observation.timestamp < 0):
        raise ARMValidationError("frame_seq and timestamp must be non-negative")
    for name in ("architecture", "cpu", "firmware_path", "firmware_sha256", "registers", "pc", "sp", "execution_state", "current_instruction", "stop_reason", "breakpoint_hit", "breakpoints"):
        _validate_captured(getattr(observation, name), name)
    registers = observation.registers.value or ()
    numbers = [item.number for item in registers]
    if len(numbers) != len(set(numbers)):
        raise ARMValidationError("register numbers must be unique")
    for register in registers:
        _validate_captured(register.observation, f"register {register.number}")
    for name, value in observation.status_registers.items():
        _validate_captured(value, f"status register {name}")
    for memory in observation.memory:
        if not memory.request_id or memory.size <= 0:
            raise ARMValidationError("memory request_id and positive size are required")
        if memory.status == ObservationStatus.OBSERVED:
            if memory.address is None or memory.raw_bytes_hex is None:
                raise ARMValidationError(f"observed memory {memory.request_id} requires address and bytes")
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", memory.raw_bytes_hex):
                raise ARMValidationError(f"memory {memory.request_id} raw bytes must be hexadecimal bytes")
            if len(memory.raw_bytes_hex) // 2 != memory.size:
                raise ARMValidationError(f"memory {memory.request_id} byte count does not match size")
        if memory.status == ObservationStatus.FAILED and not memory.error:
            raise ARMValidationError(f"failed memory {memory.request_id} requires error information")


def validate_arm_state(state: ARMState) -> None:
    if not state.runtime_id.strip() or state.generation < 0:
        raise ARMValidationError("state requires runtime_id and non-negative generation")
    if (not isinstance(state.source_frame_seq, int) or isinstance(state.source_frame_seq, bool)
            or state.source_frame_seq < 0):
        raise ARMValidationError("state requires a non-negative integer source frame sequence")
    if not re.fullmatch(r"[0-9a-f]{64}", state.state_hash):
        raise ARMValidationError("state_hash must be a 64-character lowercase SHA-256 digest")
    for name in ("architecture", "cpu", "firmware_path", "firmware_sha256", "registers", "pc", "sp", "execution_state", "current_instruction", "stop_reason", "breakpoint_hit", "breakpoints"):
        _validate_captured(getattr(state, name), name)
    if state.stability.status == StabilityStatus.UNKNOWN and state.stability.stable is not None:
        raise ARMValidationError("unknown stability must not claim a stable boolean")
    if state.stability.status == StabilityStatus.STABLE:
        if state.stability.stable is not True or state.stability.samples < 2:
            raise ARMValidationError("stable state requires a true result from repeated samples")
        if state.execution_state.status != ObservationStatus.OBSERVED or state.execution_state.value.value != "stopped":
            raise ARMValidationError("stable state requires observed stopped execution evidence")
    if state.stability.status == StabilityStatus.UNSTABLE and state.stability.stable is not False:
        raise ARMValidationError("unstable state must explicitly report stable=False")
    if state.state_hash != semantic_state_hash(state):
        raise ARMValidationError("state_hash does not match canonical semantic state")
    request_ids = [item.request_id for item in state.memory]
    if len(request_ids) != len(set(request_ids)):
        raise ARMValidationError("memory request IDs must be unique")
    decoded_by_id = {item.request_id: item for item in state.decoded_memory}
    if len(decoded_by_id) != len(state.decoded_memory) or set(decoded_by_id) != set(request_ids):
        raise ARMValidationError("decoded memory must correspond one-to-one with raw memory observations")
    for memory in state.memory:
        decoded = decoded_by_id[memory.request_id]
        expected_value = None
        expected_status = memory.status
        value_type = None
        endianness = None
        raw_hex = memory.raw_bytes_hex or ""
        address = memory.address if memory.address is not None else 0
        if memory.status == ObservationStatus.OBSERVED and memory.raw_bytes_hex is not None:
            decode = memory.decode or {}
            if not isinstance(decode, dict):
                raise ARMValidationError(f"memory {memory.request_id} decoding metadata must be an object")
            value_type = decode.get("type")
            endianness = decode.get("endianness")
            if value_type and endianness:
                match = re.fullmatch(r"(u?int)(8|16|32|64)", value_type) if isinstance(value_type, str) else None
                expected_size = int(match.group(2)) // 8 if match else None
                expected_signed = match is not None and match.group(1) == "int"
                signed = decode.get("signed", expected_signed)
                if (expected_size != memory.size or endianness not in {"little", "big"}
                        or not isinstance(signed, bool) or signed != expected_signed):
                    expected_status = ObservationStatus.FAILED
                else:
                    try:
                        expected_value = int.from_bytes(
                            bytes.fromhex(memory.raw_bytes_hex), str(endianness),
                            signed=signed,
                        )
                        expected_status = ObservationStatus.OBSERVED
                    except (ValueError, TypeError):
                        expected_status = ObservationStatus.FAILED
            else:
                expected_status = ObservationStatus.NOT_REQUESTED
        expected = (memory.request_id, address, memory.symbol, memory.size, raw_hex,
                    expected_value, value_type, endianness, expected_status)
        actual = (decoded.request_id, decoded.address, decoded.symbol, decoded.size, decoded.raw_bytes_hex,
                  decoded.value, decoded.value_type, decoded.endianness, decoded.decode_status)
        if actual != expected:
            raise ARMValidationError(f"decoded memory {memory.request_id} is inconsistent with raw bytes and decoding metadata")
