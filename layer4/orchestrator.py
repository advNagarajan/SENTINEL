"""Runtime-agnostic Layer 4 lifecycle and agent orchestration."""
import asyncio
from datetime import datetime, timezone
import math
import re
import time
from enum import Enum
from typing import Any, Callable, Optional
import structlog

from drivers.registry import DriverRegistry
from layer3.gateway import AIToolGateway
from layer4.audit_log import AuditLogger
from layer4.session import PipelineSession, RuntimeSession
from schemas.contracts import L2toL3HandoffPayload
from schemas.observability import (
    EventOutcome,
    EventType,
    EventValidationError,
    is_safe_identifier,
    MAX_DURATION_MS,
    ObservabilityEvent,
    Severity,
    SourceLayer,
)
from schemas.state import RuntimeState
from services.observability_context import current_context
from services.observability_producer import EventProducer

logger = structlog.get_logger(__name__)


class LifecycleState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    INITIALIZING = "initializing"
    READY = "ready"
    EXECUTING = "executing"
    ERROR = "error"
    DISCONNECTING = "disconnecting"


class RuntimeNotConnected(RuntimeError):
    """Raised when an agent operation requires a ready runtime."""


class InvalidRuntimeState(RuntimeError):
    """Raised when a lifecycle operation is invalid for the current state."""


PipelineFactory = Callable[[str], tuple[Any, Any, Any, Any, Any, dict[str, Any]]]
SessionFactory = Callable[[Any, Any, Any, dict[str, Any]], RuntimeSession]
GatewayFactory = Callable[[L2toL3HandoffPayload, Any], AIToolGateway]
_SCREEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RuntimeOrchestrator:
    """Own one runtime session and delegate agent operations to Layer 3."""

    def __init__(
        self,
        config_path: str = "configs/mainframe.toml",
        pipeline_factory: PipelineFactory = DriverRegistry.create_gateway,
        session_factory: Optional[SessionFactory] = None,
        gateway_factory: GatewayFactory = AIToolGateway,
        audit_logger: Optional[AuditLogger] = None,
        observability_producer: Optional[EventProducer] = None,
    ) -> None:
        self.config_path = config_path
        self._pipeline_factory = pipeline_factory
        self._session_factory = session_factory
        self._gateway_factory = gateway_factory
        self._audit_logger = audit_logger or AuditLogger()
        self._observability_producer = observability_producer
        self.state = LifecycleState.DISCONNECTED

        self._session: Optional[RuntimeSession] = None
        self._gateway: Optional[AIToolGateway] = None
        self._active_state: Optional[RuntimeState] = None
        self._driver: Any = None
        self._navigator: Optional[Any] = None
        self._action_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Construct, connect, initialize, and expose the L3 gateway."""
        if self.state is not LifecycleState.DISCONNECTED:
            raise InvalidRuntimeState(f"Cannot connect from lifecycle state '{self.state.value}'.")

        self.state = LifecycleState.CONNECTING
        try:
            driver, reducer, stability, _lowerer, dispatcher, config = self._pipeline_factory(self.config_path)
            self._driver = driver
            for component in (_lowerer, dispatcher):
                bind_producer = getattr(component, "set_observability_producer", None)
                if callable(bind_producer):
                    bind_producer(self._observability_producer)
            self._session = (
                self._session_factory(driver, reducer, stability, config)
                if self._session_factory
                else PipelineSession(driver, reducer, stability, config)
            )

            await self._session.connect()
            self.state = LifecycleState.INITIALIZING
            self._active_state = await self._session.initialize()
            initial_payload = L2toL3HandoffPayload(state=self._active_state)
            self._gateway = self._gateway_factory(initial_payload, dispatcher)

            nav_cfg = config.get("navigator", {}) if isinstance(config, dict) else {}
            if self._navigator is None and nav_cfg.get("enabled", False):
                try:
                    from services.navigator.emitter import NavigatorEmitter
                    self._navigator = NavigatorEmitter(
                        base_url=nav_cfg.get("url", "http://localhost:8100"),
                        target_name=config.get("target", {}).get("name", "ibm_mainframe"),
                    )
                except Exception as exc:
                    logger.warning("navigator_init_failed", error=str(exc))

            self.state = LifecycleState.READY
        except Exception:
            self.state = LifecycleState.ERROR
            raise

    async def disconnect(self) -> None:
        """Teardown and disconnect; repeated calls while disconnected are safe."""
        async with self._action_lock:
            if self.state is LifecycleState.DISCONNECTED:
                return
            if self._session is None:
                self._clear_runtime()
                return

            self.state = LifecycleState.DISCONNECTING
            try:
                await self._session.teardown(self._active_state)
            finally:
                if self._navigator:
                    try:
                        await self._navigator.close()
                    except Exception:
                        pass
                    self._navigator = None
                try:
                    await self._session.disconnect()
                finally:
                    self._clear_runtime()

    def is_connected(self) -> bool:
        return self.state is LifecycleState.READY

    def runtime_id(self) -> Optional[str]:
        return getattr(self._driver, "runtime_id", None)

    def get_tools(self) -> list[dict[str, Any]]:
        gateway = self._require_ready_gateway()
        return gateway.get_tools()

    def get_observation(self) -> dict[str, Any]:
        started_at = time.perf_counter()
        try:
            gateway = self._require_ready_gateway()
        except Exception as exc:
            self._submit_observation_event(
                EventType.OBSERVATION_COMPLETED,
                EventOutcome.FAILURE,
                duration_ms=self._elapsed_ms(started_at),
                error_category=self._classify_exception(exc),
            )
            raise
        self._submit_observation_event(EventType.OBSERVATION_STARTED, EventOutcome.STARTED)
        try:
            result = gateway.get_observation().to_dict()
        except Exception as exc:
            self._submit_observation_event(
                EventType.OBSERVATION_COMPLETED,
                EventOutcome.FAILURE,
                duration_ms=self._elapsed_ms(started_at),
                error_category=self._classify_exception(exc),
            )
            raise
        self._submit_observation_event(
            EventType.OBSERVATION_COMPLETED,
            EventOutcome.SUCCESS,
            duration_ms=self._elapsed_ms(started_at),
        )
        return result

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_screen_state":
            started_at = time.perf_counter()
            try:
                gateway = self._require_ready_gateway()
            except Exception as exc:
                self._submit_observation_event(
                    EventType.OBSERVATION_COMPLETED,
                    EventOutcome.FAILURE,
                    duration_ms=self._elapsed_ms(started_at),
                    error_category=self._classify_exception(exc),
                )
                raise
            self._submit_observation_event(EventType.OBSERVATION_STARTED, EventOutcome.STARTED)
            try:
                result = await gateway.execute_tool(name, arguments)
            except Exception as exc:
                self._submit_observation_event(
                    EventType.OBSERVATION_COMPLETED,
                    EventOutcome.FAILURE,
                    duration_ms=self._elapsed_ms(started_at),
                    error_category=self._classify_exception(exc),
                )
                raise
            self._submit_observation_event(
                EventType.OBSERVATION_COMPLETED,
                EventOutcome.SUCCESS,
                duration_ms=self._elapsed_ms(started_at),
            )
            return result

        async with self._action_lock:
            try:
                gateway = self._require_ready_gateway()
            except Exception as exc:
                self._submit_action_event(
                    event_type=EventType.ACTION_FAILED,
                    outcome=EventOutcome.FAILURE,
                    tool_name=name,
                    runtime_id=self.runtime_id(),
                    error_category=self._classify_exception(exc),
                )
                raise
            self.state = LifecycleState.EXECUTING
            action_started_at = time.time()
            if self._navigator and name != "get_screen_state" and self._active_state:
                self._navigator.capture_pre_state(self._active_state)
            self._safe_audit(
                "log_action_start",
                runtime_id=self.runtime_id(),
                tool_name=name,
                timestamp=action_started_at,
            )
            starting_generation = self._active_generation(gateway)
            self._submit_action_event(
                event_type=EventType.ACTION_STARTED,
                outcome=EventOutcome.STARTED,
                tool_name=name,
                runtime_id=self.runtime_id(),
                generation_before=starting_generation,
            )
            try:
                result = await gateway.execute_tool(name, arguments)
                self._active_state = gateway.get_active_payload().state
                if self._navigator and name != "get_screen_state" and self._active_state:
                    latency = (time.time() - action_started_at) * 1000.0
                    try:
                        asyncio.create_task(
                            self._navigator.emit_transition(
                                new_state=self._active_state,
                                tool_name=name,
                                arguments=arguments,
                                runtime_id=self.runtime_id() or "unknown",
                                latency_ms=latency,
                            )
                        )
                    except Exception as nav_exc:
                        logger.warning("navigator_emission_failed", error=str(nav_exc))
                self._safe_audit_completion(
                    tool_name=name,
                    action_started_at=action_started_at,
                    result=result,
                )
                terminal_type, terminal_outcome, error_category = self._classify_action_result(result)
                self._submit_action_event(
                    event_type=terminal_type,
                    outcome=terminal_outcome,
                    tool_name=name,
                    runtime_id=self.runtime_id(),
                    result=result,
                    error_category=error_category,
                    generation_before=starting_generation,
                )
                return result
            except asyncio.CancelledError as exc:
                self._safe_audit_completion(
                    tool_name=name,
                    action_started_at=action_started_at,
                    exception=exc,
                )
                self._submit_action_event(
                    event_type=EventType.ACTION_FAILED,
                    outcome=EventOutcome.CANCELLED,
                    tool_name=name,
                    runtime_id=self.runtime_id(),
                    generation_before=starting_generation,
                )
                raise
            except Exception as exc:
                self._safe_audit_completion(
                    tool_name=name,
                    action_started_at=action_started_at,
                    exception=exc,
                )
                self._submit_action_event(
                    event_type=EventType.ACTION_FAILED,
                    outcome=EventOutcome.FAILURE,
                    tool_name=name,
                    runtime_id=self.runtime_id(),
                    error_category=self._classify_exception(exc),
                    generation_before=starting_generation,
                )
                raise
            finally:
                # Lower-layer failures are returned as structured results by the
                # gateway; gateway exceptions do not by themselves prove that
                # the underlying runtime is unusable.
                if self.state is LifecycleState.EXECUTING:
                    self.state = LifecycleState.READY

    def _submit_action_event(
        self,
        *,
        event_type: EventType,
        outcome: EventOutcome,
        tool_name: str,
        runtime_id: Optional[str],
        result: Optional[dict[str, Any]] = None,
        error_category: Optional[str] = None,
        generation_before: Optional[int] = None,
    ) -> None:
        producer = self._observability_producer
        context = current_context()
        if producer is None or context is None:
            return

        result = result or {}
        metadata: dict[str, Any] = {"tool_name": tool_name}
        if error_category:
            metadata["error_category"] = error_category
        if isinstance(result.get("is_stable"), bool):
            metadata["is_stable"] = result["is_stable"]

        event_fields: dict[str, Any] = {
            "event_id": f"evt_{time.time_ns()}",
            "event_type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "severity": Severity.ERROR
            if outcome in {EventOutcome.FAILURE, EventOutcome.CANCELLED}
            else Severity.INFO,
            "source_layer": SourceLayer.LAYER4,
            "source_component": "RuntimeOrchestrator",
            "outcome": outcome,
            "correlation_id": context.correlation_id,
            "session_id": context.session_id,
            "operation_id": context.operation_id,
            "runtime_id": self._safe_identifier(runtime_id),
            "metadata": metadata,
        }
        for field_name in ("ticket_id",):
            value = self._safe_identifier(result.get(field_name), field_name)
            if value is not None:
                event_fields[field_name] = value
        for field_name in ("generation_before", "generation_after"):
            candidate = result.get(field_name)
            if field_name == "generation_before" and candidate is None:
                candidate = generation_before
            value = self._safe_nonnegative_int(candidate)
            if value is not None:
                event_fields[field_name] = value
        duration = self._safe_duration(result.get("execution_time_ms"))
        if duration is not None:
            event_fields["duration_ms"] = duration

        try:
            producer.submit(ObservabilityEvent(**event_fields))
        except (EventValidationError, Exception):
            logger.warning("l4_action_observability_submit_failed")

    @staticmethod
    def _active_generation(gateway: AIToolGateway) -> Optional[int]:
        """Return the current L3 generation when it is safely available."""
        try:
            state = gateway.get_active_payload().state
            return RuntimeOrchestrator._safe_nonnegative_int(getattr(state, "generation", None))
        except Exception:
            return None

    def _submit_observation_event(
        self,
        event_type: EventType,
        outcome: EventOutcome,
        *,
        duration_ms: Optional[float] = None,
        error_category: Optional[str] = None,
    ) -> None:
        producer = self._observability_producer
        context = current_context()
        if producer is None or context is None:
            return

        metadata = self._observation_metadata()
        if error_category:
            metadata["error_category"] = error_category
        event_fields: dict[str, Any] = {
            "event_id": f"evt_{time.time_ns()}",
            "event_type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "severity": Severity.ERROR if outcome is EventOutcome.FAILURE else Severity.INFO,
            "source_layer": SourceLayer.LAYER4,
            "source_component": "RuntimeOrchestrator",
            "outcome": outcome,
            "correlation_id": context.correlation_id,
            "session_id": context.session_id,
            "operation_id": context.operation_id,
            "runtime_id": self._safe_identifier(self.runtime_id()),
            "metadata": metadata,
        }
        safe_duration = self._safe_duration(duration_ms)
        if safe_duration is not None:
            event_fields["duration_ms"] = safe_duration
        try:
            producer.submit(ObservabilityEvent(**event_fields))
        except Exception:
            logger.warning("l4_observation_observability_submit_failed")

    def _observation_metadata(self) -> dict[str, Any]:
        try:
            if self._gateway is None:
                return {}
            state = self._gateway.get_active_payload().state
            metadata: dict[str, Any] = {
                "generation": self._safe_nonnegative_int(state.generation),
                "field_count": len(state.fields),
                "is_stable": state.is_stable,
                "iterations": self._safe_nonnegative_int(state.stability_report.iterations),
            }
            if isinstance(state.screen_hash, str) and _SCREEN_HASH_PATTERN.fullmatch(state.screen_hash):
                metadata["screen_hash"] = state.screen_hash
            return {key: value for key, value in metadata.items() if value is not None}
        except Exception:
            return {}

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        elapsed = (time.perf_counter() - started_at) * 1000.0
        return elapsed if math.isfinite(elapsed) and elapsed >= 0 else 0.0

    @staticmethod
    def _classify_action_result(result: dict[str, Any]) -> tuple[EventType, EventOutcome, Optional[str]]:
        if result.get("success") is True:
            return EventType.ACTION_COMPLETED, EventOutcome.SUCCESS, None
        if result.get("error") == "ContractValidationError":
            return EventType.ACTION_REJECTED, EventOutcome.REJECTED, "contract_validation"
        return EventType.ACTION_FAILED, EventOutcome.FAILURE, RuntimeOrchestrator._safe_error_category(result.get("error"))

    @staticmethod
    def _classify_exception(exception: Exception) -> str:
        if isinstance(exception, (TimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(exception, (ConnectionError, OSError)):
            return "transport_error"
        return "unknown"

    @staticmethod
    def _safe_error_category(error: Any) -> str:
        if isinstance(error, str):
            lowered = error.lower()
            if "timeout" in lowered:
                return "timeout"
            if "connection" in lowered or "transport" in lowered:
                return "transport_error"
        return "unknown"

    @staticmethod
    def _safe_identifier(value: Any, field_name: str = "runtime_id") -> Optional[str]:
        if isinstance(value, str) and is_safe_identifier(field_name, value):
            return value
        return None

    @staticmethod
    def _safe_nonnegative_int(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @staticmethod
    def _safe_duration(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and 0 <= value <= MAX_DURATION_MS:
                return float(value)
        return None

    def _require_ready_gateway(self) -> AIToolGateway:
        if self.state is LifecycleState.DISCONNECTED:
            raise RuntimeNotConnected("RuntimeOrchestrator is not connected and ready.")
        if self.state is not LifecycleState.READY or self._gateway is None:
            raise InvalidRuntimeState(
                f"RuntimeOrchestrator is not ready (state: '{self.state.value}')."
            )
        return self._gateway

    def _safe_audit(self, method_name: str, **fields: Any) -> None:
        """Keep audit failures from changing action or lifecycle behavior."""
        try:
            getattr(self._audit_logger, method_name)(**fields)
        except Exception as exc:
            logger.warning("l4_audit_failed", method=method_name, error=str(exc))

    def _safe_audit_completion(
        self,
        tool_name: str,
        action_started_at: float,
        result: Optional[dict[str, Any]] = None,
        exception: Optional[Exception] = None,
    ) -> None:
        result = result or {}
        self._safe_audit(
            "log_action_completion",
            runtime_id=self.runtime_id(),
            tool_name=tool_name,
            action_started_at=action_started_at,
            action_completed_at=time.time(),
            success=bool(result.get("success", False)) if exception is None else False,
            ticket_id=result.get("ticket_id"),
            generation_before=result.get("generation_before"),
            generation_after=result.get("generation_after"),
            execution_time_ms=result.get("execution_time_ms"),
            error=(exception.__class__.__name__ if exception else result.get("error")),
            message=(str(exception) if exception else result.get("message")),
        )

    def _clear_runtime(self) -> None:
        self._session = None
        self._gateway = None
        self._active_state = None
        self._driver = None
        self.state = LifecycleState.DISCONNECTED
