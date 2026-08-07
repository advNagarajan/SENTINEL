"""Structured Audit Logger for writing machine-readable JSON logs to disk."""
import json
import os
import time
from typing import Any, Optional
import structlog

from schemas.events import RuntimeEvent
from schemas.state import RuntimeState


class AuditLogger:
    """Emits structured JSON events to logs/ directory for auditing, metrics, and debugging."""

    def __init__(self, log_file: str = "logs/mainframe_audit.jsonl"):
        self.log_file = log_file
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        self._setup_structlog()

    def _setup_structlog(self) -> None:
        """Configure structlog to log to file."""
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(),
        )

    def log_event(self, event: RuntimeEvent) -> None:
        """Log a RuntimeEvent to JSONL file."""
        data = {
            "type": "SENTINEL_EVENT",
            "event_type": event.event_type.value,
            "runtime_id": event.runtime_id,
            "message": event.message,
            "timestamp": event.timestamp,
            "details": event.details,
        }
        self._append_json(data)

    def log_state(self, state: RuntimeState) -> None:
        """Log a RuntimeState observation to JSONL file."""
        data = {
            "type": "SENTINEL_STATE_OBSERVED",
            "runtime_id": state.runtime_id,
            "generation": state.generation,
            "screen_hash": state.screen_hash,
            "is_stable": state.is_stable,
            "title": state.title,
            "fields_count": len(state.fields),
            "timestamp": state.timestamp,
        }
        self._append_json(data)

    def _append_json(self, data: dict[str, Any]) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data) + "\n")
        except Exception as e:
            print(f"Failed to write audit log: {e}")
