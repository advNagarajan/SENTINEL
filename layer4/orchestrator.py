"""Runtime-agnostic Layer 4 lifecycle and agent orchestration."""
import asyncio
import time
from enum import Enum
from typing import Any, Callable, Optional
import structlog

from drivers.registry import DriverRegistry
from layer3.gateway import AIToolGateway
from layer4.audit_log import AuditLogger
from layer4.session import PipelineSession, RuntimeSession
from schemas.contracts import L2toL3HandoffPayload
from schemas.state import RuntimeState

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


class RuntimeOrchestrator:
    """Own one runtime session and delegate agent operations to Layer 3."""

    def __init__(
        self,
        config_path: str = "configs/mainframe.toml",
        pipeline_factory: PipelineFactory = DriverRegistry.create_gateway,
        session_factory: Optional[SessionFactory] = None,
        gateway_factory: GatewayFactory = AIToolGateway,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.config_path = config_path
        self._pipeline_factory = pipeline_factory
        self._session_factory = session_factory
        self._gateway_factory = gateway_factory
        self._audit_logger = audit_logger or AuditLogger()
        self.state = LifecycleState.DISCONNECTED

        self._session: Optional[RuntimeSession] = None
        self._gateway: Optional[AIToolGateway] = None
        self._active_state: Optional[RuntimeState] = None
        self._driver: Any = None
        self._action_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Construct, connect, initialize, and expose the L3 gateway."""
        if self.state is not LifecycleState.DISCONNECTED:
            raise InvalidRuntimeState(f"Cannot connect from lifecycle state '{self.state.value}'.")

        self.state = LifecycleState.CONNECTING
        try:
            driver, reducer, stability, _lowerer, dispatcher, config = self._pipeline_factory(self.config_path)
            self._driver = driver
            self._session = (
                self._session_factory(driver, reducer, stability, config)
                if self._session_factory
                else PipelineSession(driver, reducer, stability)
            )

            await self._session.connect()
            self.state = LifecycleState.INITIALIZING
            self._active_state = await self._session.initialize()
            initial_payload = L2toL3HandoffPayload(state=self._active_state)
            self._gateway = self._gateway_factory(initial_payload, dispatcher)
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
        gateway = self._require_ready_gateway()
        return gateway.get_observation().to_dict()

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_screen_state":
            gateway = self._require_ready_gateway()
            return await gateway.execute_tool(name, arguments)

        async with self._action_lock:
            gateway = self._require_ready_gateway()
            self.state = LifecycleState.EXECUTING
            action_started_at = time.time()
            self._safe_audit(
                "log_action_start",
                runtime_id=self.runtime_id(),
                tool_name=name,
                timestamp=action_started_at,
            )
            try:
                result = await gateway.execute_tool(name, arguments)
                self._active_state = gateway.get_active_payload().state
                self._safe_audit_completion(
                    tool_name=name,
                    action_started_at=action_started_at,
                    result=result,
                )
                return result
            except Exception as exc:
                self._safe_audit_completion(
                    tool_name=name,
                    action_started_at=action_started_at,
                    exception=exc,
                )
                raise
            finally:
                # Lower-layer failures are returned as structured results by the
                # gateway; gateway exceptions do not by themselves prove that
                # the underlying runtime is unusable.
                if self.state is LifecycleState.EXECUTING:
                    self.state = LifecycleState.READY

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