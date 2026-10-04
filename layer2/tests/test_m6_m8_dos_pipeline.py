"""Unit tests for FreeDOS Layer 1/2/3 integration:
1. DOSActionLowerer lowering and contract enforcement
2. DOSStabilityEngine two-phase settling & timeout
3. DOSStateReducer contract compliance (validate_l2_to_l3_contract)
4. AIToolGateway tool compilation & execution (validate_l3_to_l4_contract)
"""
import asyncio
from typing import Any
import numpy as np
import pytest

from layer1.base import EnvironmentDriver
from layer2.dos.action_lowerer import DOSActionLowerer
from layer2.dos.reducer import DOS_AVAILABLE_ACTIONS, DOSStateReducer
from layer2.dos.stability import DOSStabilityEngine
from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from schemas.actions import ActionType, CanonicalActionIntent
from schemas.contracts import (
    ContractValidationError,
    L2toL3HandoffPayload,
    validate_l2_to_l3_contract,
    validate_l3_to_l4_contract,
)
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


class MockDOSDriver(EnvironmentDriver):
    """Mock DOSDriver for deterministic unit testing."""

    def __init__(self) -> None:
        self._runtime_id = "mock_freedos_node"
        self.typed_text: list[str] = []
        self.pressed_keys: list[str] = []
        self.frames_to_return: list[np.ndarray] = []

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def screenshot(self) -> np.ndarray:
        if self.frames_to_return:
            return self.frames_to_return.pop(0)
        # Default blank 720x400 frame
        return np.zeros((400, 720, 3), dtype=np.uint8)

    async def read_frame(self) -> Any:
        pass

    async def write_raw(self, data: bytes) -> None:
        pass

    async def freeze(self) -> None:
        pass

    async def unfreeze(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    def add_event_listener(self, listener: Any) -> None:
        pass

    async def type_text(self, text: str) -> None:
        self.typed_text.append(text)

    async def press_keys(self, keys: list[str]) -> None:
        self.pressed_keys.extend(keys)


def make_dummy_state(gen: int = 0) -> RuntimeState:
    raw_grid = [" " * 80 for _ in range(25)]
    raw_grid[24] = "C:\\>" + (" " * 76)
    return RuntimeState(
        runtime_id="mock_freedos_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=raw_grid,
        title="FreeDOS Command Prompt",
        fields={
            "cmd_prompt": FieldEntry(
                label="cmd_prompt",
                value="",
                row=24,
                col=4,
                length=76,
                protected=False,
                field_id="cmd_prompt",
            )
        },
        status_line="C:\\>",
        stability_report=StabilityReport(
            is_stable=True,
            method="two_phase_quiescence",
            confidence=1.0,
            delta_value=0.0,
            iterations=2,
        ),
        screen_hash="a" * 64,
        generation=gen,
        screen_size={"rows": 25, "cols": 80},
        available_actions=DOS_AVAILABLE_ACTIONS,
    )


@pytest.mark.asyncio
async def test_dos_action_lowerer_trigger_action() -> None:
    driver = MockDOSDriver()
    lowerer = DOSActionLowerer()
    state = make_dummy_state(0)

    intent = CanonicalActionIntent(
        intent_type=ActionType.TRIGGER_ACTION,
        generation_token=state.generation_token,
        generation=0,
        ticket_id="ticket_001",
        action_id="ALT+F",
    )

    result = await lowerer.lower_and_execute(intent, driver, state)
    assert result.success is True
    assert driver.pressed_keys == ["ALT+F"]


@pytest.mark.asyncio
async def test_dos_action_lowerer_fill_and_submit() -> None:
    driver = MockDOSDriver()
    lowerer = DOSActionLowerer()
    state = make_dummy_state(0)

    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_AND_SUBMIT,
        generation_token=state.generation_token,
        generation=0,
        ticket_id="ticket_002",
        field_id="cmd_prompt",
        value="dir",
        action_id="ENTER",
    )

    result = await lowerer.lower_and_execute(intent, driver, state)
    assert result.success is True
    assert driver.typed_text == ["dir"]
    assert driver.pressed_keys == ["ENTER"]


@pytest.mark.asyncio
async def test_dos_reducer_contract_validation() -> None:
    reducer = DOSStateReducer()
    frame = np.zeros((400, 720, 3), dtype=np.uint8)
    som = reducer.build_object_model(frame)

    state, delta = reducer.reduce_state(som, "test_node", 0)
    payload = L2toL3HandoffPayload(state=state, delta=delta)

    # Enforce strict contract
    validate_l2_to_l3_contract(payload)
    assert payload.state.screen_size == {"rows": 25, "cols": 80}
    assert len(payload.state.raw_grid) == 25
    assert len(payload.state.screen_hash) == 64
    assert "cmd_prompt" in payload.state.fields


@pytest.mark.asyncio
async def test_dos_gateway_e2e_mock() -> None:
    driver = MockDOSDriver()
    reducer = DOSStateReducer()
    lowerer = DOSActionLowerer()
    stability = DOSStabilityEngine(poll_interval_ms=10, quiescence_ms=50, max_wait_ms=500)

    initial_state = make_dummy_state(0)
    initial_payload = L2toL3HandoffPayload(state=initial_state)

    dispatcher = ActionDispatcher(
        driver=driver,
        reducer=reducer,
        action_lowerer=lowerer,
        stability_engine=stability,
    )
    gateway = AIToolGateway(initial_payload=initial_payload, dispatcher=dispatcher)

    # 1. Observation
    obs = gateway.get_observation()
    validate_l3_to_l4_contract(obs)
    assert obs.generation == 0
    assert "set_field_and_submit" in [t["function"]["name"] for t in obs.available_tools]

    # 2. Tool execution
    res = await gateway.execute_tool(
        name="set_field_and_submit",
        arguments={
            "generation_token": obs.generation_token,
            "field_id": "cmd_prompt",
            "value": "cls",
            "action": "ENTER",
        },
    )
    assert res["success"] is True
    assert driver.typed_text == ["cls"]
    assert driver.pressed_keys == ["ENTER"]
    assert res["generation"] == 1
