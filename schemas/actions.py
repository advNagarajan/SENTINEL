"""Action schemas for downward execution propagation (L3 -> L2 -> L1)."""
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional


class ActionType(str, Enum):
    """Canonical action types supported across all environments."""
    FILL_FIELD = "fill_field"
    TRIGGER_ACTION = "trigger_action"
    FILL_AND_SUBMIT = "fill_and_submit"
    SEND_RAW_KEYS = "send_raw_keys"


@dataclass
class CanonicalActionIntent:
    """Standardized, environment-independent action intent passed from Layer 3 to Layer 2."""
    intent_type: ActionType
    generation_token: str
    generation: int
    ticket_id: str
    field_id: Optional[str] = None
    fields: dict[str, str] = field(default_factory=dict)
    value: Optional[str] = None
    action_id: Optional[str] = None
    raw_keys: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ActionResult:
    """Outcome of downward action execution returned by Layer 2 to Layer 3."""
    ticket_id: str
    success: bool
    generation_before: int
    generation_after: int
    screen_hash_before: str
    screen_hash_after: str
    execution_time_ms: float
    error_message: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
