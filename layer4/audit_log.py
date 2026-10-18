"""Layer 4 Audit Logger & Multi-Layer Structured Logging System."""
import json
import logging
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import Any, Optional
import structlog


_REDACTED = "[REDACTED]"
_SENSITIVE_LOG_KEY_PATTERN = re.compile(
    r"(?:password|passwd|secret|credential|token|typed[_-]?value|screen[_-]?(?:text|content|grid)|"
    r"raw[_-]?(?:screen|grid|bytes)|input(?:[_-]?value)?|field[_-]?value|authorization|api[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_LOG_TEXT_PATTERN = re.compile(
    r"\b(?:password|passwd|secret|credential|token|typed[_ -]?(?:value|input)|"
    r"screen[_ -]?(?:text|content|grid)|raw[_ -]?(?:screen|grid|bytes)|"
    r"authorization|api[_ -]?key)\b",
    re.IGNORECASE,
)


def _sanitize_log_value(value: Any, key: Optional[str] = None) -> Any:
    """Recursively redact known sensitive log fields without changing structure."""
    if key and _SENSITIVE_LOG_KEY_PATTERN.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_log_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_log_value(item) for item in value]
    if isinstance(value, bytes):
        return _REDACTED
    if isinstance(value, str) and _SENSITIVE_LOG_TEXT_PATTERN.search(value):
        return _REDACTED
    return value


def redact_sensitive_log_fields(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor applied to audit and runtime warning/error records."""
    return _sanitize_log_value(event_dict)


class FileAndConsoleHandler(logging.Handler):
    """Logging handler that formats logs as JSON lines for files and clean text for console."""

    def __init__(self, log_file: str):
        super().__init__()
        self.log_file = log_file
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_entry = self.format(record)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception:
            self.handleError(record)


def setup_logging(log_file: str = "logs/mainframe_audit.jsonl", log_level: str = "INFO") -> None:
    """Configure structlog globally across Layer 1, Layer 2, Layer 3, and Layer 4."""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Configure root stdlib logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.handlers.clear()

    file_handler = FileAndConsoleHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_sensitive_log_fields,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class AuditLogger:
    """Layer 4 Audit Logger for high-level event and state observation persistence."""

    def __init__(self, log_file: str = "logs/mainframe_audit.jsonl"):
        self.log_file = log_file
        setup_logging(self.log_file)
        self.logger = structlog.get_logger("audit")

    def log_event(self, event: Any) -> None:
        """Log a RuntimeEvent to JSONL file."""
        self.logger.info(
            "sentinel_event",
            layer="layer1",
            event_type=event.event_type.value,
            runtime_id=event.runtime_id,
            message=event.message,
            timestamp=event.timestamp,
            details=event.details,
        )

    def log_state(self, state: Any) -> None:
        """Log a RuntimeState observation to JSONL file."""
        self.logger.info(
            "sentinel_state_observed",
            layer="layer2",
            runtime_id=state.runtime_id,
            generation=state.generation,
            screen_hash=state.screen_hash,
            is_stable=state.is_stable,
            title=state.title,
            fields_count=len(state.fields),
            timestamp=state.timestamp,
        )

    def log_action_start(
        self,
        runtime_id: Optional[str],
        tool_name: str,
        timestamp: float,
    ) -> None:
        """Log the start of an agent-requested state-changing action."""
        self.logger.info(
            "sentinel_action_started",
            layer="layer4",
            runtime_id=runtime_id,
            tool_name=tool_name,
            action_started_at=timestamp,
        )

    def log_action_completion(
        self,
        runtime_id: Optional[str],
        tool_name: str,
        action_started_at: float,
        action_completed_at: float,
        success: bool,
        ticket_id: Optional[str] = None,
        generation_before: Optional[int] = None,
        generation_after: Optional[int] = None,
        execution_time_ms: Optional[float] = None,
        error: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """Log completion or failure metadata for an agent action."""
        self.logger.info(
            "sentinel_action_completed",
            layer="layer4",
            runtime_id=runtime_id,
            tool_name=tool_name,
            action_started_at=action_started_at,
            action_completed_at=action_completed_at,
            success=success,
            ticket_id=ticket_id,
            generation_before=generation_before,
            generation_after=generation_after,
            execution_time_ms=execution_time_ms,
            error=error,
            message=message,
        )
