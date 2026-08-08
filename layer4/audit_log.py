"""Layer 4 Audit Logger & Multi-Layer Structured Logging System."""
import json
import logging
import os
import sys
import time
from typing import Any, Optional
import structlog


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
