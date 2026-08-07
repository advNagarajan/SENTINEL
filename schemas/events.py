"""Runtime event definitions for logging, metrics, and debugging."""
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional


class RuntimeEventType(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    SCREEN_RECEIVED = "screen_received"
    HOST_BUSY = "host_busy"
    SCREEN_STABLE = "screen_stable"
    AID_SENT = "aid_sent"
    INPUT_TYPED = "input_typed"
    ERROR_ENCOUNTERED = "error_encountered"


@dataclass
class RuntimeEvent:
    event_type: RuntimeEventType
    runtime_id: str
    message: str
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)
