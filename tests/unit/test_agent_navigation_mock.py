"""Unit test: Complete multi-turn autonomous agent menu navigation with mocked L1 driver."""
import pytest
from unittest.mock import AsyncMock

from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from layer2.tn3270.reducer import TN3270StateReducer
from layer2.tn3270.action_lowerer import TN3270ActionLowerer
from layer2.tn3270.stability import OIAStabilityEngine
from schemas.contracts import L2toL3HandoffPayload, L3toL4HandoffPayload, validate_l3_to_l4_contract
from schemas.pipeline import TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.fixture
def mock_pipeline():
    reducer = TN3270StateReducer(rows=24, cols=80)
    lowerer = TN3270ActionLowerer()
    stability = OIAStabilityEngine(poll_interval_ms=10, settle_ms=20, max_wait_ms=200, quiescence_ms=20)
    mock_driver = AsyncMock()
    mock_driver.runtime_id = "test_node"
    dispatcher = ActionDispatcher(driver=mock_driver, reducer=reducer, action_lowerer=lowerer, stability_engine=stability)
    return mock_driver, reducer, dispatcher


def build_ispf_state(gen: int, title: str, lines: list[str]) -> RuntimeState:
    raw_grid = [" " * 80 for _ in range(24)]
    raw_grid[0] = title.ljust(80)
    raw_grid[1] = "Option ===> ".ljust(80)
    for i, l in enumerate(lines):
        if i + 3 < 24:
            raw_grid[i + 3] = l.ljust(80)

    fields = {
        "opt": FieldEntry(label="Option ===>", value="", row=1, col=12, length=10, protected=False, field_id="fld_option_===>"),
        "title": FieldEntry(label=title, value="", row=0, col=0, length=len(title), protected=True, field_id="fld_title"),
    }
    report = StabilityReport(is_stable=True, method="oia_byte", confidence=1.0, delta_value=0.0, iterations=1)
    return RuntimeState(
        runtime_id="test_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=raw_grid,
        title=title,
        fields=fields,
        status_line="READY",
        stability_report=report,
        screen_hash=f"{gen}" * 64,
        generation=gen,
    )


@pytest.mark.asyncio
async def test_agent_multi_turn_navigation_contract(mock_pipeline):
    mock_driver, reducer, dispatcher = mock_pipeline

    # Screen 1: Primary Option Menu
    s1 = build_ispf_state(1, "ISPF/PDF PRIMARY OPTION MENU", [
        "0  SETTINGS    Terminal and user parameters",
        "1  BROWSE      Display source data or listings",
        "2  EDIT        Create or change source data",
        "3  UTILITIES   Perform utility functions",
    ])
    payload1 = L2toL3HandoffPayload(state=s1)
    gateway = AIToolGateway(initial_payload=payload1, dispatcher=dispatcher)

    # 1. Inspect Screen 1 observation
    obs1 = gateway.get_observation()
    validate_l3_to_l4_contract(obs1)
    obs1_d = obs1.to_dict()
    assert obs1_d["screen_title"] == "ISPF/PDF PRIMARY OPTION MENU"
    assert any("3  UTILITIES" in line for line in obs1_d["screen_text"])
    assert "fld_option_===>" in obs1_d["editable_fields"]
    assert obs1_d["generation_token"] == s1.generation_token

    # Setup mock transition to Screen 2 (UTILITY SELECTION MENU)
    s2 = build_ispf_state(2, "UTILITY SELECTION MENU", [
        "1  Library     Compress or expand data sets",
        "2  Data Set    Allocate, rename, or delete data sets",
        "3  Move/Copy   Move or copy members or data sets",
    ])
    payload2 = L2toL3HandoffPayload(state=s2)

    # Mock dispatcher returns (action_result, payload2)
    from schemas.actions import ActionResult
    res_step1 = ActionResult(ticket_id="t1", success=True, generation_before=1, generation_after=2, screen_hash_before=s1.screen_hash, screen_hash_after=s2.screen_hash, execution_time_ms=15.0)
    dispatcher.dispatch = AsyncMock(return_value=(res_step1, payload2))

    # 2. Agent Action 1: Select 3 (UTILITIES)
    obs2_d = await gateway.execute_tool("set_field_and_submit", {
        "generation_token": obs1_d["generation_token"],
        "field_id": "fld_option_===>",
        "value": "3",
        "action": "ENTER",
    })

    assert obs2_d["success"] is True
    assert obs2_d["generation"] == 2
    assert obs2_d["screen_title"] == "UTILITY SELECTION MENU"
    assert any("2  Data Set" in line for line in obs2_d["screen_text"])
    # Verify tools bound to gen 2
    submit_tool = next(t for t in obs2_d["available_tools"] if t["function"]["name"] == "set_field_and_submit")
    assert submit_tool["function"]["parameters"]["properties"]["generation_token"]["enum"] == [obs2_d["generation_token"]]

    # Setup mock transition to Screen 3 (DATA SET UTILITY)
    s3 = build_ispf_state(3, "DATA SET UTILITY - A", [
        "A  Allocate new data set",
        "R  Rename entire data set",
        "D  Delete entire data set",
    ])
    payload3 = L2toL3HandoffPayload(state=s3)
    res_step2 = ActionResult(ticket_id="t2", success=True, generation_before=2, generation_after=3, screen_hash_before=s2.screen_hash, screen_hash_after=s3.screen_hash, execution_time_ms=14.0)
    dispatcher.dispatch = AsyncMock(return_value=(res_step2, payload3))

    # 3. Agent Action 2: Select 2 (Data Set)
    obs3_d = await gateway.execute_tool("set_field_and_submit", {
        "generation_token": obs2_d["generation_token"],
        "field_id": "fld_option_===>",
        "value": "2",
        "action": "ENTER",
    })
    assert obs3_d["success"] is True
    assert obs3_d["generation"] == 3
    assert obs3_d["screen_title"] == "DATA SET UTILITY - A"

    # Setup mock transition to Screen 4 (Back to UTILITIES via PF3)
    s4 = build_ispf_state(4, "UTILITY SELECTION MENU", [
        "1  Library     Compress or expand data sets",
        "2  Data Set    Allocate, rename, or delete data sets",
        "3  Move/Copy   Move or copy members or data sets",
    ])
    payload4 = L2toL3HandoffPayload(state=s4)
    res_step3 = ActionResult(ticket_id="t3", success=True, generation_before=3, generation_after=4, screen_hash_before=s3.screen_hash, screen_hash_after=s4.screen_hash, execution_time_ms=10.0)
    dispatcher.dispatch = AsyncMock(return_value=(res_step3, payload4))

    # 4. Agent Action 3: Trigger PF3 to backtrack
    obs4_d = await gateway.execute_tool("trigger_action", {
        "generation_token": obs3_d["generation_token"],
        "action_id": "PF3",
    })
    assert obs4_d["success"] is True
    assert obs4_d["generation"] == 4
    assert obs4_d["screen_title"] == "UTILITY SELECTION MENU"
