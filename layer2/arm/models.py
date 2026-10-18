"""Typed raw ARM observations and semantically interpreted ARM state models."""

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ObservationStatus(str, Enum):
    OBSERVED = "observed"
    NOT_REQUESTED = "not_requested"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ARMExecutionState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class StabilityStatus(str, Enum):
    UNKNOWN = "unknown"
    STABLE = "stable"
    UNSTABLE = "unstable"


@dataclass(frozen=True)
class CapturedValue(Generic[T]):
    status: ObservationStatus
    value: T | None = None
    raw_value: str | None = None
    error: dict[str, str] | None = None
    source: str | None = None


@dataclass(frozen=True)
class ARMRegister:
    number: int
    name: str | None
    observation: CapturedValue[int]


@dataclass(frozen=True)
class ARMInstruction:
    address: int
    text: str
    raw: str | None = None


@dataclass(frozen=True)
class ARMMemoryObservation:
    request_id: str
    status: ObservationStatus
    size: int
    address: int | None = None
    requested_address: int | None = None
    symbol: str | None = None
    raw_bytes_hex: str | None = None
    decode: dict[str, Any] | None = None
    error: dict[str, str] | None = None


@dataclass(frozen=True)
class ARMObservation:
    runtime_id: str
    environment: str
    schema_version: str
    architecture: CapturedValue[str]
    cpu: CapturedValue[str]
    firmware_path: CapturedValue[str]
    firmware_sha256: CapturedValue[str]
    registers: CapturedValue[tuple[ARMRegister, ...]]
    pc: CapturedValue[int]
    sp: CapturedValue[int]
    status_registers: dict[str, CapturedValue[int]]
    execution_state: CapturedValue[ARMExecutionState]
    current_instruction: CapturedValue[ARMInstruction]
    stop_reason: CapturedValue[str]
    breakpoint_hit: CapturedValue[dict[str, Any]]
    memory: tuple[ARMMemoryObservation, ...]
    breakpoints: CapturedValue[tuple[dict[str, Any], ...]]
    frame_seq: int
    timestamp: float
    capabilities: tuple[str, ...] = ()
    completeness: dict[str, tuple[str, ...]] = field(default_factory=dict)
    errors: tuple[dict[str, str], ...] = ()
    source_values: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ARMDecodedMemory:
    request_id: str
    address: int
    symbol: str | None
    size: int
    raw_bytes_hex: str
    value: int | None
    value_type: str | None
    endianness: str | None
    decode_status: ObservationStatus


@dataclass(frozen=True)
class ARMStability:
    status: StabilityStatus = StabilityStatus.UNKNOWN
    stable: bool | None = None
    method: str = "unavailable"
    reason: str = "A single observation does not establish stability"
    samples: int = 1


@dataclass(frozen=True)
class ARMState:
    runtime_id: str
    generation: int
    environment: str
    architecture: CapturedValue[str]
    cpu: CapturedValue[str]
    firmware_path: CapturedValue[str]
    firmware_sha256: CapturedValue[str]
    registers: CapturedValue[tuple[ARMRegister, ...]]
    pc: CapturedValue[int]
    sp: CapturedValue[int]
    status_registers: dict[str, CapturedValue[int]]
    execution_state: CapturedValue[ARMExecutionState]
    current_instruction: CapturedValue[ARMInstruction]
    stop_reason: CapturedValue[str]
    breakpoint_hit: CapturedValue[dict[str, Any]]
    memory: tuple[ARMMemoryObservation, ...]
    decoded_memory: tuple[ARMDecodedMemory, ...]
    breakpoints: CapturedValue[tuple[dict[str, Any], ...]]
    stability: ARMStability
    completeness: dict[str, tuple[str, ...]]
    errors: tuple[dict[str, str], ...]
    state_hash: str
    source_frame_seq: int
    timestamp: float
    schema_version: str
    capabilities: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ARMStateDelta:
    generation_from: int
    generation_to: int
    state_hash_from: str
    state_hash_to: str
    register_changes: dict[str, dict[str, int | None]]
    pc_change: dict[str, int] | None
    sp_change: dict[str, int] | None
    status_register_changes: dict[str, dict[str, int | None]]
    memory_changes: dict[str, dict[str, Any]]
    execution_state_change: dict[str, ARMExecutionState] | None
    stop_reason_change: dict[str, str] | None
    breakpoint_changes: dict[str, dict[str, Any]]


def _captured_semantic(value: CapturedValue[Any]) -> Any:
    raw = value.value if value.status == ObservationStatus.OBSERVED else None
    return _jsonable(raw)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def semantic_state_hash(state: ARMState) -> str:
    """Hash canonical observed state values, excluding time, generation, and provenance."""
    # Keep ARMRegister instances here: _captured_semantic serializes dataclasses
    # for JSON fields, while the canonical register projection needs attributes.
    registers = state.registers.value if state.registers.status == ObservationStatus.OBSERVED else ()
    breakpoints = _captured_semantic(state.breakpoints)
    canonical = {
        "environment": state.environment,
        "architecture": _captured_semantic(state.architecture),
        "cpu": _captured_semantic(state.cpu),
        "firmware_sha256": _captured_semantic(state.firmware_sha256),
        "firmware_path": _captured_semantic(state.firmware_path),
        "registers": [
            {
                "number": item.number,
                "name": item.name,
                "status": item.observation.status.value,
                "value": item.observation.value if item.observation.status == ObservationStatus.OBSERVED else None,
            }
            for item in registers or ()
        ],
        "pc": _captured_semantic(state.pc),
        "sp": _captured_semantic(state.sp),
        "status_registers": {key: _captured_semantic(value) for key, value in sorted(state.status_registers.items())},
        "execution_state": _captured_semantic(state.execution_state),
        "current_instruction": _captured_semantic(state.current_instruction),
        "stop_reason": _captured_semantic(state.stop_reason),
        "breakpoint_hit": _captured_semantic(state.breakpoint_hit),
        "memory": [
            {
                "request_id": item.request_id,
                "status": item.status.value,
                "address": item.address,
                "size": item.size,
                "raw_bytes_hex": item.raw_bytes_hex,
                "decode": item.decode,
            }
            for item in sorted(state.memory, key=lambda entry: entry.request_id)
        ],
        "breakpoints": _jsonable(breakpoints),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
