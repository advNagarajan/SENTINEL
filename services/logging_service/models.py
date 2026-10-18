"""Configuration and limits for the isolated logging service."""
from dataclasses import dataclass
import math
from typing import Optional


DEFAULT_RETENTION_DAYS = 7
DEFAULT_RETENTION_INTERVAL_SECONDS = 3600.0
MIN_RETENTION_INTERVAL_SECONDS = 1.0
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 256 * 1024


def validate_retention_days(value: object) -> int:
    """Validate a non-negative retention duration without an upper bound."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("retention_days must be a non-negative integer")
    return value


def parse_retention_days(raw_value: Optional[str]) -> int:
    """Parse the retention setting without imposing an arbitrary upper bound."""
    if raw_value is None:
        return DEFAULT_RETENTION_DAYS
    if not isinstance(raw_value, str):
        raise ValueError("SENTINEL_LOG_RETENTION_DAYS must be a non-negative integer")
    if not raw_value.strip():
        raise ValueError("SENTINEL_LOG_RETENTION_DAYS must be a non-negative integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "SENTINEL_LOG_RETENTION_DAYS must be a non-negative integer"
        ) from exc
    try:
        return validate_retention_days(value)
    except ValueError as exc:
        raise ValueError(
            "SENTINEL_LOG_RETENTION_DAYS must be a non-negative integer"
        ) from exc


def validate_retention_interval_seconds(value: object) -> float:
    """Validate a finite cleanup interval that is not unreasonably small."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "retention_interval_seconds must be a finite number at least 1 second"
        )
    interval = float(value)
    if not math.isfinite(interval) or interval < MIN_RETENTION_INTERVAL_SECONDS:
        raise ValueError(
            "retention_interval_seconds must be a finite number at least 1 second"
        )
    return interval


def parse_retention_interval_seconds(raw_value: Optional[str]) -> float:
    """Parse the periodic cleanup interval without accepting busy-loop values."""
    if raw_value is None:
        return DEFAULT_RETENTION_INTERVAL_SECONDS
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(
            "SENTINEL_LOG_RETENTION_INTERVAL_SECONDS must be at least 1 second"
        )
    try:
        value = float(raw_value)
        return validate_retention_interval_seconds(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "SENTINEL_LOG_RETENTION_INTERVAL_SECONDS must be at least 1 second"
        ) from exc
@dataclass(frozen=True)
class ServiceLimits:
    """Bounded service settings kept separate from the event contract."""

    retention_days: int = DEFAULT_RETENTION_DAYS
    retention_interval_seconds: float = DEFAULT_RETENTION_INTERVAL_SECONDS
    max_batch_events: int = MAX_BATCH_EVENTS
    max_batch_bytes: int = MAX_BATCH_BYTES

    def __post_init__(self) -> None:
        validate_retention_days(self.retention_days)
        validate_retention_interval_seconds(self.retention_interval_seconds)
        for name, value in (
            ("max_batch_events", self.max_batch_events),
            ("max_batch_bytes", self.max_batch_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
