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
from layer2.dos.reducer import DOS_AVAILABLE_ACTIONS, DOSStateReducer, detect_dos_prompt
from layer2.dos.stability import DOSStabilityEngine, TIME_PATTERN
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

    # On blank screen without prompt, fields must be empty (confirming prompt detection)
    validate_l2_to_l3_contract(payload)
    assert payload.state.screen_size == {"rows": 25, "cols": 80}
    assert len(payload.state.raw_grid) == 25
    assert len(payload.state.screen_hash) == 64
    assert payload.state.fields == {}

    # When a real DOS prompt is present on screen
    som.grid_matrix[24] = list("C:\\>" + (" " * 76))
    som.cursor = (24, 4)
    state2, delta2 = reducer.reduce_state(som, "test_node", 1)
    payload2 = L2toL3HandoffPayload(state=state2, delta=delta2)
    validate_l2_to_l3_contract(payload2)
    assert "cmd_prompt" in payload2.state.fields
    assert payload2.state.fields["cmd_prompt"].row == 24
    assert payload2.state.fields["cmd_prompt"].col == 4


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


def test_detect_dos_prompt_anchoring_and_false_positives() -> None:
    """Verify detect_dos_prompt strictly anchors at line start and avoids false positives."""
    grid = [" " * 80 for _ in range(25)]

    # 1. Genuine prompts
    grid[24] = "C:\\>" + (" " * 76)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=4) == (24, 4, 76)

    grid[24] = "C:\\FDOS\\BIN>" + (" " * 68)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=12) == (24, 12, 68)

    grid[24] = "A:>" + (" " * 77)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=3) == (24, 3, 77)

    # 2. Program output containing prompt mid-line (must NOT match due to ^ anchoring)
    grid[24] = "Type exit to return to C:\\>" + (" " * 53)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=27) is None

    # 3. Leading spaces before prompt (must NOT match)
    grid[24] = "   C:\\>" + (" " * 73)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=7) is None

    # 4. Cursor positioned before prompt ends
    grid[24] = "C:\\>" + (" " * 76)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=2) is None

    # 5. Cursor positioned after user typed command (prompt is not ending at cursor)
    grid[24] = "C:\\>dir" + (" " * 73)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=7) is None

    # 6. Cursor row does not match prompt row
    assert detect_dos_prompt(grid, cur_r=20, cur_c=4) is None

    # 7. Non-drive prompt pattern (e.g. choice prompt without drive letter)
    grid[24] = "Selection [1-5]>" + (" " * 64)
    assert detect_dos_prompt(grid, cur_r=24, cur_c=16) is None


def test_clock_mask_heuristic_on_off() -> None:
    """Verify conditional status-bar clock masking on row 24 cols 65-79."""
    # 1. FreeDOS EDIT status bar with full time HH:MM:SS
    row_edit = "│<F1=Help> <Alt=Menu>                                            │ 15:43:21 │"
    clock_zone = row_edit[65:80]
    assert TIME_PATTERN.search(clock_zone) is not None
    masked = row_edit[:65] + (" " * 15)
    assert len(masked) == 80
    assert masked[65:80] == " " * 15

    # 2. Status bar with HH:MM
    row_edit_short = "│<F1=Help>                                                        │ 08:30 │   "
    assert TIME_PATTERN.search(row_edit_short[65:80]) is not None

    # 3. Normal command prompt (no clock) -> must NOT match
    row_prompt = "C:\\>                                                                            "
    assert TIME_PATTERN.search(row_prompt[65:80]) is None

    # 4. Numbers that are NOT a time format -> must NOT match
    row_numbers = "Showing 45 files found in directory listing 1004 bytes total                     "
    assert TIME_PATTERN.search(row_numbers[65:80]) is None

    # 5. Time pattern located elsewhere (cols 10-18) -> clock_zone (cols 65-80) must NOT match
    row_mid = "Logged at 12:30:00 by user process                                              "
    assert TIME_PATTERN.search(row_mid[65:80]) is None


@pytest.mark.asyncio
async def test_cursor_detection_from_reused_frames_and_fallback() -> None:
    """Verify cursor detection from reused quiescence frames with single-phase fallback."""
    reducer = DOSStateReducer()
    stability = DOSStabilityEngine(
        poll_interval_ms=10,
        reaction_timeout_ms=50,
        quiescence_ms=30,
        min_quiescent_polls=3,
    )

    # Helper to construct a frame with or without underline cursor at (row=24, col=4)
    def create_frame(cursor_on: bool) -> np.ndarray:
        f = np.zeros((400, 720, 3), dtype=np.uint8)
        # Put prompt C:\> on row 24
        # At row 24, col 4: underline cursor on scanlines 14..15
        if cursor_on:
            y0, y1 = 24 * 16, 25 * 16
            x0, x1 = 4 * 9, 5 * 9
            f[y0 + 14 : y1, x0:x1] = (255, 255, 255)
        return f

    # Case A: Reused frames already contain both blink phases (e.g. 2 ON, 1 OFF)
    driver_a = MockDOSDriver()
    # 1 reaction frame + 3 quiescence frames (toggle across frames)
    driver_a.frames_to_return = [
        create_frame(True),   # reaction frame
        create_frame(True),   # poll 1
        create_frame(False),  # poll 2 (OFF phase)
        create_frame(True),   # poll 3 (ON phase)
    ]
    state_a, rep_a = await stability.wait_until_stable(
        driver=driver_a,
        reducer=reducer,
        runtime_id="node_a",
        generation=0,
    )
    # Both phases were in reused frames: no extra screenshots needed
    assert rep_a.is_stable is True
    assert rep_a.details["cursor_sampling_ms"] == 0.0
    assert state_a.metadata.get("cursor_visible") is True
    assert state_a.cursor == {"row": 24, "col": 4}

    # Case B: Reused frames contain ONLY one phase (all ON), fallback captures extra frame
    driver_b = MockDOSDriver()
    driver_b.frames_to_return = [
        create_frame(True),  # reaction
        create_frame(True),  # poll 1
        create_frame(True),  # poll 2
        create_frame(True),  # poll 3 (all ON!)
        create_frame(False), # extra frame 1 (OFF phase provided!)
        create_frame(True),
    ]
    state_b, rep_b = await stability.wait_until_stable(
        driver=driver_b,
        reducer=reducer,
        runtime_id="node_b",
        generation=0,
    )
    assert rep_b.is_stable is True
    # Extra frames were queried because reused frames had only one phase
    assert rep_b.details["cursor_sampling_ms"] > 0.0
    assert state_b.metadata.get("cursor_visible") is True
    assert state_b.cursor == {"row": 24, "col": 4}

