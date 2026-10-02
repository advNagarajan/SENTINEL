"""Controlled SENTINEL MCP server used by the stdio protocol integration test."""
from types import SimpleNamespace

from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from layer4.mcp_server import create_server
from layer4.orchestrator import RuntimeOrchestrator
from schemas.actions import ActionResult
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


def make_state(generation: int, title: str) -> RuntimeState:
    grid = [" " * 80 for _ in range(24)]
    grid[0] = title
    return RuntimeState(
        runtime_id="mcp-protocol-test",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title=title,
        fields={
            "command": FieldEntry("command", "", 20, 10, 40, False, "fld_command"),
            "account": FieldEntry("account", "", 20, 52, 12, False, "fld_account"),
        },
        status_line="READY",
        stability_report=StabilityReport(True, "test", 1.0, 0.0, 1),
        screen_hash=f"{generation:064x}",
        generation=generation,
        available_actions=["ENTER", "PF3"],
    )


class ControlledSession:
    async def connect(self) -> None:
        pass

    async def initialize(self) -> RuntimeState:
        return make_state(1, "MCP TEST MENU")

    async def teardown(self, _state) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class ControlledLowerer:
    async def lower_and_execute(self, intent, driver, active_state) -> ActionResult:
        del driver
        return ActionResult(
            ticket_id=intent.ticket_id,
            success=True,
            generation_before=active_state.generation,
            generation_after=active_state.generation + 1,
            screen_hash_before=active_state.screen_hash,
            screen_hash_after="",
            execution_time_ms=1.0,
        )


class ControlledStability:
    async def wait_until_stable(self, driver, reducer, runtime_id, generation, previous_state):
        del driver, reducer, previous_state
        state = make_state(generation, "MCP TEST RESULT")
        state.runtime_id = runtime_id
        return state, state.stability_report


class NullAudit:
    def log_action_start(self, **_fields) -> None:
        pass

    def log_action_completion(self, **_fields) -> None:
        pass


def make_runtime() -> RuntimeOrchestrator:
    driver = SimpleNamespace(runtime_id="mcp-protocol-test")
    reducer = object()
    stability = ControlledStability()
    lowerer = ControlledLowerer()
    dispatcher = ActionDispatcher(driver, reducer, lowerer, stability)
    pipeline = (driver, reducer, stability, lowerer, dispatcher, {})
    return RuntimeOrchestrator(
        pipeline_factory=lambda _path: pipeline,
        session_factory=lambda *_args: ControlledSession(),
        gateway_factory=AIToolGateway,
        audit_logger=NullAudit(),
    )


def main() -> None:
    create_server(orchestrator=make_runtime()).run(transport="stdio")


if __name__ == "__main__":
    main()