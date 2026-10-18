"""Context-local correlation identifiers for SENTINEL operations."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import uuid
from typing import Iterator, Optional

from schemas.observability import is_safe_identifier


@dataclass(frozen=True)
class ObservabilityContext:
    """Identifiers scoped to one session and one operation."""

    correlation_id: str
    session_id: str
    operation_id: str


_current_context: ContextVar[Optional[ObservabilityContext]] = ContextVar(
    "sentinel_observability_context",
    default=None,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def new_context(
    *,
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    operation_id: Optional[str] = None,
) -> ObservabilityContext:
    """Create a context with generated IDs for omitted values."""
    for name, value in (
        ("session_id", session_id),
        ("correlation_id", correlation_id),
        ("operation_id", operation_id),
    ):
        if value is not None and not is_safe_identifier(name, value):
            raise ValueError(f"{name} is not a safe observability identifier")
    return ObservabilityContext(
        correlation_id=correlation_id or _new_id("corr"),
        session_id=session_id or _new_id("session"),
        operation_id=operation_id or _new_id("op"),
    )


def current_context() -> Optional[ObservabilityContext]:
    """Return the context for the current async/task-local execution flow."""
    return _current_context.get()


@contextmanager
def bind_context(context: ObservabilityContext) -> Iterator[ObservabilityContext]:
    """Temporarily bind a context and reliably restore the previous value."""
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


@contextmanager
def operation_context(*, session_id: Optional[str] = None) -> Iterator[ObservabilityContext]:
    """Bind a fresh correlation and operation ID, preserving the session ID."""
    parent = current_context()
    context = new_context(session_id=session_id or (parent.session_id if parent else None))
    with bind_context(context):
        yield context
