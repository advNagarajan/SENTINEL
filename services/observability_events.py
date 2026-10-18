"""Small, best-effort helpers for emitting sanitized lower-layer events."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping, Optional
import uuid
import structlog

from schemas.observability import (
    EventOutcome,
    EventType,
    ObservabilityEvent,
    Severity,
    SourceLayer,
    is_safe_identifier,
)
from services.observability_context import current_context

logger = structlog.get_logger(__name__)


def submit_layer_event(
    producer: Any,
    *,
    event_type: EventType,
    outcome: EventOutcome,
    source_layer: SourceLayer,
    source_component: str,
    ticket_id: Optional[str] = None,
    generation_before: Optional[int] = None,
    generation_after: Optional[int] = None,
    duration_ms: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Submit one validated event without allowing observability to affect execution."""
    context = current_context()
    if producer is None or context is None:
        return
    try:
        fields: dict[str, Any] = {
            "event_id": f"evt-{uuid.uuid4().hex}",
            "event_type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "severity": Severity.ERROR
            if outcome in {EventOutcome.FAILURE, EventOutcome.TIMEOUT, EventOutcome.CANCELLED}
            else Severity.INFO,
            "source_layer": source_layer,
            "source_component": source_component,
            "outcome": outcome,
            "correlation_id": context.correlation_id,
            "session_id": context.session_id,
            "operation_id": context.operation_id,
            "metadata": dict(metadata or {}),
        }
        for name, value in (
            ("ticket_id", ticket_id),
            ("generation_before", generation_before),
            ("generation_after", generation_after),
            ("duration_ms", duration_ms),
        ):
            if name in {"ticket_id"}:
                if value is not None and is_safe_identifier(name, value):
                    fields[name] = value
            elif name.startswith("generation"):
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    fields[name] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 86_400_000:
                fields[name] = value
        producer.submit(ObservabilityEvent(**fields))
    except Exception:
        logger.warning("layer_observability_submit_failed")
