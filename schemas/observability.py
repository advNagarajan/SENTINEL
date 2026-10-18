"""Versioned, sanitized events for SENTINEL observability delivery."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json
import math
import re
from typing import Mapping, Optional, Union


class EventValidationError(ValueError):
    """Raised when an observability event violates the public event contract."""


class EventType(str, Enum):
    SERVICE_STARTED = "service.started"
    SERVICE_HEALTH = "service.health"
    MCP_REQUEST = "mcp.request"
    MCP_RESPONSE = "mcp.response"
    RUNTIME_CONNECTING = "runtime.connecting"
    RUNTIME_CONNECTED = "runtime.connected"
    RUNTIME_CONNECTION_FAILED = "runtime.connection_failed"
    SESSION_INITIALIZED = "session.initialized"
    SESSION_DISCONNECTING = "session.disconnecting"
    SESSION_DISCONNECTED = "session.disconnected"
    OBSERVATION_STARTED = "observation.started"
    OBSERVATION_COMPLETED = "observation.completed"
    ACTION_STARTED = "action.started"
    ACTION_INTENT_CREATED = "action.intent_created"
    ACTION_REJECTED = "action.rejected"
    ACTION_LOWERING = "action.lowering"
    DRIVER_INPUT_SENT = "driver.input_sent"
    STABILITY_STARTED = "stability.started"
    STABILITY_COMPLETED = "stability.completed"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
    LOGGING_EVENT_DROPPED = "logging.event_dropped"


class SourceLayer(str, Enum):
    MCP = "mcp"
    LAYER1 = "layer1"
    LAYER2 = "layer2"
    LAYER3 = "layer3"
    LAYER4 = "layer4"
    SERVICE = "service"


class Severity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class EventOutcome(str, Enum):
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    DROPPED = "dropped"
    CANCELLED = "cancelled"


MetadataValue = Union[str, int, float, bool, None]

SCHEMA_VERSION = "1.0"
MAX_EVENT_BYTES = 2048
MAX_STRING_LENGTH = 256
MAX_METADATA_KEYS = 32
MAX_DURATION_MS = 86_400_000.0

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMPONENT_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")
_SENSITIVE_IDENTIFIER_PATTERN = re.compile(
    r"(?:^|[._:-])(?:password|passwd|secret|credential|token|typed[_-]?value|"
    r"screen[_-]?(?:text|content|grid)|raw[_-]?(?:screen|grid)|api[_-]?key|"
    r"authorization|bearer)(?:$|[._:-])",
    re.IGNORECASE,
)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_UNSANITIZED_DETAIL_PATTERN = re.compile(
    r"(?:password|passwd|secret|credential|token)\s*[:=]|"
    r"traceback\s*\(most recent call last\)|"
    r"-----BEGIN [^-]+-----",
    re.IGNORECASE,
)
ERROR_CATEGORIES = frozenset(
    {
        "connection_failure",
        "contract_validation",
        "persistence_failure",
        "timeout",
        "transport_error",
        "unknown",
    }
)
STABILITY_METHODS = frozenset(
    {
        "dos_reducer",
        "oia_multi_signal",
        "oia_multi_signal_timeout",
        "timeout",
        "two_phase_quiescence",
        "unknown",
    }
)

# Values are intentionally scalar and explicitly named. Raw payloads cannot be
# added by callers without changing this contract and its tests.
METADATA_TYPES: dict[str, tuple[type, ...]] = {
    "action_id": (str,),
    "batch_size": (int,),
    "dropped_count": (int,),
    "duration_ms": (int, float),
    "error_category": (str,),
    "field_count": (int,),
    "field_id": (str,),
    "generation": (int,),
    "is_stable": (bool,),
    "iterations": (int,),
    "screen_hash": (str,),
    "source_event_id": (str,),
    "stability_method": (str,),
    "status_code": (int,),
    "target_name": (str,),
    "tool_name": (str,),
    "value_length": (int,),
}

# Identifier-shaped values are references, not free-form payloads. They may
# contain only bounded ASCII reference characters and must not use common
# credential or raw-content markers. Component names have a shorter limit so
# they remain stable diagnostic labels; the other identifiers preserve the
# existing 128-character compatibility limit.
_IDENTIFIER_POLICIES: dict[str, tuple[re.Pattern[str], int]] = {
    "source_component": (_COMPONENT_IDENTIFIER_PATTERN, 64),
    "event_id": (_IDENTIFIER_PATTERN, 128),
    "correlation_id": (_IDENTIFIER_PATTERN, 128),
    "session_id": (_IDENTIFIER_PATTERN, 128),
    "operation_id": (_IDENTIFIER_PATTERN, 128),
    "runtime_id": (_IDENTIFIER_PATTERN, 128),
    "ticket_id": (_IDENTIFIER_PATTERN, 128),
}


def _validate_string(name: str, value: str, *, required: bool = True) -> None:
    if not isinstance(value, str):
        raise EventValidationError(f"{name} must be a string.")
    if required and not value:
        raise EventValidationError(f"{name} must not be empty.")
    if len(value) > MAX_STRING_LENGTH:
        raise EventValidationError(f"{name} exceeds {MAX_STRING_LENGTH} characters.")
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise EventValidationError(f"{name} contains control characters.")
    if _UNSANITIZED_DETAIL_PATTERN.search(value):
        raise EventValidationError(f"{name} contains unsanitized sensitive details.")


def _validate_identifier(name: str, value: Optional[str]) -> None:
    if value is None:
        return
    _validate_string(name, value)
    pattern, max_length = _IDENTIFIER_POLICIES.get(name, (_IDENTIFIER_PATTERN, 128))
    if len(value) > max_length or not pattern.fullmatch(value):
        raise EventValidationError(f"{name} is not a valid identifier.")
    if _SENSITIVE_IDENTIFIER_PATTERN.search(value):
        raise EventValidationError(f"{name} contains a sensitive marker.")


def is_safe_identifier(name: str, value: Optional[str]) -> bool:
    """Return whether an optional identifier is safe for event persistence."""
    if value is None:
        return False
    try:
        _validate_identifier(name, value)
    except EventValidationError:
        return False
    return True


def _validate_metadata(metadata: Mapping[str, MetadataValue]) -> dict[str, MetadataValue]:
    if not isinstance(metadata, Mapping):
        raise EventValidationError("metadata must be a mapping.")
    if len(metadata) > MAX_METADATA_KEYS:
        raise EventValidationError(f"metadata may contain at most {MAX_METADATA_KEYS} keys.")

    sanitized: dict[str, MetadataValue] = {}
    for key, value in metadata.items():
        if key not in METADATA_TYPES:
            raise EventValidationError(f"metadata key '{key}' is not allowlisted.")
        if value is not None and type(value) not in METADATA_TYPES[key]:
            raise EventValidationError(f"metadata value for '{key}' has an invalid type.")
        if isinstance(value, str):
            _validate_string(f"metadata['{key}']", value)
            if key in {"action_id", "field_id", "source_event_id", "tool_name"}:
                _validate_identifier(f"metadata['{key}']", value)
            elif key == "screen_hash" and not _HASH_PATTERN.fullmatch(value):
                raise EventValidationError("metadata['screen_hash'] must be lowercase SHA-256 hex.")
            elif key == "error_category" and value not in ERROR_CATEGORIES:
                raise EventValidationError("metadata['error_category'] is not supported.")
            elif key == "stability_method" and value not in STABILITY_METHODS:
                raise EventValidationError("metadata['stability_method'] is not supported.")
        elif isinstance(value, int) and not isinstance(value, bool) and value < 0:
            raise EventValidationError(f"metadata value for '{key}' must not be negative.")
        elif isinstance(value, int) and not isinstance(value, bool) and key == "status_code" and not 100 <= value <= 599:
            raise EventValidationError("metadata['status_code'] must be between 100 and 599.")
        elif isinstance(value, float) and not math.isfinite(value):
            raise EventValidationError(f"metadata value for '{key}' must be finite.")
        sanitized[key] = value
    return sanitized


@dataclass(frozen=True)
class ObservabilityEvent:
    """Sanitized versioned event envelope accepted by the logging boundary."""

    event_id: str
    event_type: EventType
    timestamp_utc: str
    severity: Severity
    source_layer: SourceLayer
    source_component: str
    outcome: EventOutcome
    correlation_id: str
    runtime_id: Optional[str] = None
    session_id: Optional[str] = None
    operation_id: Optional[str] = None
    ticket_id: Optional[str] = None
    generation_before: Optional[int] = None
    generation_after: Optional[int] = None
    duration_ms: Optional[float] = None
    metadata: Mapping[str, MetadataValue] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EventValidationError(f"schema_version must be '{SCHEMA_VERSION}'.")

        for name in ("event_id", "source_component", "correlation_id"):
            _validate_string(name, getattr(self, name))
        for name in ("event_id", "source_component", "correlation_id"):
            _validate_identifier(name, getattr(self, name))
        for name in ("runtime_id", "session_id", "operation_id", "ticket_id"):
            _validate_identifier(name, getattr(self, name))

        for name, enum_type in (
            ("event_type", EventType),
            ("severity", Severity),
            ("source_layer", SourceLayer),
            ("outcome", EventOutcome),
        ):
            if not isinstance(getattr(self, name), enum_type):
                raise EventValidationError(f"{name} must be a {enum_type.__name__} value.")

        _validate_timestamp(self.timestamp_utc)
        _validate_generation("generation_before", self.generation_before)
        _validate_generation("generation_after", self.generation_after)
        if self.duration_ms is not None:
            if not isinstance(self.duration_ms, (int, float)) or isinstance(self.duration_ms, bool):
                raise EventValidationError("duration_ms must be numeric.")
            if not math.isfinite(self.duration_ms) or not 0 <= self.duration_ms <= MAX_DURATION_MS:
                raise EventValidationError("duration_ms is outside the allowed range.")

        sanitized_metadata = _validate_metadata(self.metadata)
        object.__setattr__(self, "metadata", sanitized_metadata)
        self._validate_serialized_size()

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-compatible event representation."""
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp_utc": self.timestamp_utc,
            "severity": self.severity.value,
            "source_layer": self.source_layer.value,
            "source_component": self.source_component,
            "outcome": self.outcome.value,
            "correlation_id": self.correlation_id,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "ticket_id": self.ticket_id,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata),
        }

    def _validate_serialized_size(self) -> None:
        if len(self.serialized_bytes()) > MAX_EVENT_BYTES:
            raise EventValidationError(f"event exceeds {MAX_EVENT_BYTES} serialized bytes.")

    def serialized_bytes(self) -> bytes:
        """Return the exact compact UTF-8 JSON representation used for sizing."""
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _validate_timestamp(value: str) -> None:
    _validate_string("timestamp_utc", value)
    if not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise EventValidationError("timestamp_utc must be an ISO-8601 UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EventValidationError("timestamp_utc must be an ISO-8601 UTC timestamp.") from exc
    if parsed.utcoffset() != timedelta(0):
        raise EventValidationError("timestamp_utc must use UTC.")


def _validate_generation(name: str, value: Optional[int]) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise EventValidationError(f"{name} must be a non-negative integer.")
