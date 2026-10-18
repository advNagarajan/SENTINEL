import asyncio
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlsplit
import uuid
import structlog

from mcp.server.fastmcp import FastMCP

from layer4.orchestrator import RuntimeOrchestrator
from schemas.observability import (
    EventOutcome,
    EventType,
    is_safe_identifier,
    ObservabilityEvent,
    Severity,
    SourceLayer,
)
from services.observability_context import (
    bind_context,
    current_context,
    new_context,
    operation_context,
)
from services.observability_producer import DEFAULT_ENDPOINT, EventProducer, ProducerConfig

logger = structlog.get_logger(__name__)
OBSERVABILITY_ENDPOINT_ENV = "SENTINEL_OBSERVABILITY_ENDPOINT"


def resolve_observability_endpoint() -> str:
    """Resolve the producer endpoint without making observability a startup dependency."""
    configured = os.environ.get(OBSERVABILITY_ENDPOINT_ENV)
    if configured is None:
        return DEFAULT_ENDPOINT

    endpoint = configured.strip()
    parsed = urlsplit(endpoint)
    if not endpoint or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.warning("mcp_observability_endpoint_invalid_using_default")
        return DEFAULT_ENDPOINT
    return endpoint


def _build_default_producer() -> EventProducer:
    return EventProducer(ProducerConfig(endpoint=resolve_observability_endpoint()))


def _submit_lifecycle_event(
    producer: EventProducer,
    event_type: EventType,
    outcome: EventOutcome,
    runtime_id: Optional[str],
    metadata: Optional[dict[str, str]] = None,
) -> None:
    """Build and submit one sanitized lifecycle event without affecting runtime flow."""
    context = current_context()
    if context is None:
        return
    try:
        event = ObservabilityEvent(
            event_id=f"evt-{uuid.uuid4().hex}",
            event_type=event_type,
            timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            severity=Severity.INFO if outcome is not EventOutcome.FAILURE else Severity.ERROR,
            source_layer=SourceLayer.LAYER4,
            source_component="RuntimeOrchestrator",
            outcome=outcome,
            correlation_id=context.correlation_id,
            session_id=context.session_id,
            operation_id=context.operation_id,
            runtime_id=runtime_id,
            metadata=metadata or {},
        )
        producer.submit(event)
    except Exception:
        logger.warning("mcp_observability_submit_failed")


def _safe_runtime_id(orchestrator: RuntimeOrchestrator) -> Optional[str]:
    try:
        runtime_id = orchestrator.runtime_id()
        return runtime_id if is_safe_identifier("runtime_id", runtime_id) else None
    except Exception:
        return None


def _safe_error_category(error: Any) -> str:
    """Map an exception or structured error to the public safe category set."""
    text = str(error).lower()
    if "contractvalidation" in text or "contract validation" in text:
        return "contract_validation"
    if "timeout" in text:
        return "timeout"
    if "connection" in text or "transport" in text:
        return "transport_error"
    if "persistence" in text or "database" in text:
        return "persistence_failure"
    return "unknown"


def _duration_ms(started: float) -> int:
    return min(max(int((time.monotonic() - started) * 1000), 0), 86_400_000)


def _submit_mcp_event(
    producer: EventProducer,
    event_type: EventType,
    outcome: EventOutcome,
    tool_name: str,
    *,
    duration_ms: Optional[int] = None,
    error_category: Optional[str] = None,
) -> None:
    """Submit one sanitized MCP boundary event without affecting tool flow."""
    context = current_context()
    if context is None:
        return
    try:
        metadata: dict[str, Any] = {"tool_name": tool_name}
        if duration_ms is not None:
            metadata["duration_ms"] = duration_ms
        if error_category is not None:
            metadata["error_category"] = error_category
        event = ObservabilityEvent(
            event_id=f"evt-{uuid.uuid4().hex}",
            event_type=event_type,
            timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            severity=Severity.ERROR if outcome in {
                EventOutcome.FAILURE,
                EventOutcome.CANCELLED,
            } else Severity.INFO,
            source_layer=SourceLayer.MCP,
            source_component="MCPServer",
            outcome=outcome,
            correlation_id=context.correlation_id,
            session_id=context.session_id,
            operation_id=context.operation_id,
            metadata=metadata,
        )
        producer.submit(event)
    except Exception:
        # Instrumentation must never recurse into the tool boundary or alter it.
        logger.warning("mcp_boundary_observability_submit_failed")


async def _run_instrumented_tool(
    *,
    producer: EventProducer,
    session_id: Optional[str],
    tool_name: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Run one registered tool inside one operation context and trace its boundary."""
    with operation_context(session_id=session_id):
        started = time.monotonic()
        _submit_mcp_event(
            producer,
            EventType.MCP_REQUEST,
            EventOutcome.STARTED,
            tool_name,
        )
        try:
            result = await operation()
        except asyncio.CancelledError:
            _submit_mcp_event(
                producer,
                EventType.MCP_RESPONSE,
                EventOutcome.CANCELLED,
                tool_name,
                duration_ms=_duration_ms(started),
                error_category="timeout",
            )
            raise
        except Exception as exc:
            _submit_mcp_event(
                producer,
                EventType.MCP_RESPONSE,
                EventOutcome.FAILURE,
                tool_name,
                duration_ms=_duration_ms(started),
                error_category=_safe_error_category(exc),
            )
            raise

        # A structured tool result is a failure only when it explicitly says so.
        structured_failure = isinstance(result, dict) and result.get("success") is False
        _submit_mcp_event(
            producer,
            EventType.MCP_RESPONSE,
            EventOutcome.FAILURE if structured_failure else EventOutcome.SUCCESS,
            tool_name,
            duration_ms=_duration_ms(started),
            error_category=(
                _safe_error_category(result.get("error"))
                if structured_failure and result.get("error") is not None
                else None
            ),
        )
        return result


@asynccontextmanager
async def runtime_lifespan(
    _server: FastMCP,
    orchestrator: RuntimeOrchestrator,
    producer: Optional[EventProducer] = None,
    session_id: Optional[str] = None,
) -> AsyncIterator[None]:
    """Connect the configured runtime for the MCP server lifetime."""
    # An explicitly supplied producer/configuration always wins over the environment.
    active_producer = producer if producer is not None else _build_default_producer()
    session_context = new_context(session_id=session_id)
    with bind_context(session_context):
        try:
            await active_producer.start()
        except Exception:
            logger.warning("mcp_observability_start_failed")
        with operation_context(session_id=session_context.session_id):
            _submit_lifecycle_event(
                active_producer,
                EventType.RUNTIME_CONNECTING,
                EventOutcome.STARTED,
                _safe_runtime_id(orchestrator),
            )
            try:
                await orchestrator.connect()
            except Exception as exc:
                _submit_lifecycle_event(
                    active_producer,
                    EventType.RUNTIME_CONNECTION_FAILED,
                    EventOutcome.FAILURE,
                    _safe_runtime_id(orchestrator),
                    {"error_category": "connection_failure"},
                )
                logger.warning("mcp_runtime_connection_failed", error=str(exc))
            else:
                _submit_lifecycle_event(
                    active_producer,
                    EventType.RUNTIME_CONNECTED,
                    EventOutcome.SUCCESS,
                    _safe_runtime_id(orchestrator),
                )
        try:
            yield
        finally:
            with operation_context(session_id=session_context.session_id):
                runtime_id = _safe_runtime_id(orchestrator)
                _submit_lifecycle_event(
                    active_producer,
                    EventType.SESSION_DISCONNECTING,
                    EventOutcome.STARTED,
                    runtime_id,
                )
                try:
                    await orchestrator.disconnect()
                except Exception:
                    _submit_lifecycle_event(
                        active_producer,
                        EventType.SESSION_DISCONNECTED,
                        EventOutcome.FAILURE,
                        runtime_id,
                        {"error_category": "transport_error"},
                    )
                else:
                    _submit_lifecycle_event(
                        active_producer,
                        EventType.SESSION_DISCONNECTED,
                        EventOutcome.SUCCESS,
                        runtime_id,
                    )
            try:
                shutdown_timeout = getattr(
                    getattr(active_producer, "config", None),
                    "shutdown_timeout_seconds",
                    2.0,
                )
                await asyncio.wait_for(
                    active_producer.close(timeout_seconds=shutdown_timeout),
                    timeout=shutdown_timeout,
                )
            except Exception:
                logger.warning("mcp_observability_close_failed")


def resolve_default_config() -> str:
    if os.environ.get("SENTINEL_CONFIG"):
        return os.environ["SENTINEL_CONFIG"]
    if Path("configs/freedos.toml").is_file():
        return "configs/freedos.toml"
    return "configs/mainframe.toml"


def create_server(
    config_path: Optional[str] = None,
    orchestrator: Optional[RuntimeOrchestrator] = None,
    producer: Optional[EventProducer] = None,
) -> FastMCP:
    """Create the MCP server and bind it to one Layer 4 runtime session."""
    active_config = config_path or resolve_default_config()
    # An explicitly supplied producer/configuration always wins over the environment.
    active_producer = producer if producer is not None else _build_default_producer()
    runtime = orchestrator or RuntimeOrchestrator(
        config_path=active_config,
        observability_producer=active_producer,
    )
    session_id: Optional[str] = None

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        nonlocal session_id
        session_id = new_context().session_id
        try:
            async with runtime_lifespan(server, runtime, active_producer, session_id):
                yield
        finally:
            session_id = None

    server = FastMCP(
        name="SENTINEL",
        instructions="Delegate runtime observation and semantic actions to SENTINEL Layer 4.",
        lifespan=lifespan,
    )

    @server.tool()
    async def get_screen_state() -> dict[str, Any]:
        """Return the current screen state through the Layer 4 gateway."""
        return await _run_instrumented_tool(
            producer=active_producer,
            session_id=session_id,
            tool_name="get_screen_state",
            operation=lambda: runtime.execute_tool("get_screen_state", {}),
        )

    @server.tool()
    async def get_observation() -> dict[str, Any]:
        """Return the current validated Layer 3-to-Layer 4 observation."""
        async def operation() -> dict[str, Any]:
            return runtime.get_observation()

        return await _run_instrumented_tool(
            producer=active_producer,
            session_id=session_id,
            tool_name="get_observation",
            operation=operation,
        )

    @server.tool()
    async def set_field_and_submit(
        generation_token: str,
        field_id: str,
        value: str,
        action: str = "ENTER",
    ) -> dict[str, Any]:
        """Delegate the existing single-field semantic tool to Layer 4."""
        return await _run_instrumented_tool(
            producer=active_producer,
            session_id=session_id,
            tool_name="set_field_and_submit",
            operation=lambda: runtime.execute_tool(
                "set_field_and_submit",
                {
                    "generation_token": generation_token,
                    "field_id": field_id,
                    "value": value,
                    "action": action,
                },
            )
        )

    @server.tool()
    async def fill_form_and_submit(
        generation_token: str,
        fields: dict[str, str],
        action: str = "ENTER",
    ) -> dict[str, Any]:
        """Delegate the existing multi-field semantic tool to Layer 4."""
        return await _run_instrumented_tool(
            producer=active_producer,
            session_id=session_id,
            tool_name="fill_form_and_submit",
            operation=lambda: runtime.execute_tool(
                "fill_form_and_submit",
                {
                    "generation_token": generation_token,
                    "fields": fields,
                    "action": action,
                },
            )
        )

    @server.tool()
    async def trigger_action(generation_token: str, action_id: str) -> dict[str, Any]:
        """Delegate the existing semantic action-key tool to Layer 4."""
        return await _run_instrumented_tool(
            producer=active_producer,
            session_id=session_id,
            tool_name="trigger_action",
            operation=lambda: runtime.execute_tool(
                "trigger_action",
                {"generation_token": generation_token, "action_id": action_id},
            )
        )

    return server


server = create_server()


if __name__ == "__main__":
    server.run(transport="stdio")
