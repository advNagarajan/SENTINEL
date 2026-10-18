"""Unit tests for Layer 3 downward contract enforcement, error handling, and tool validation."""
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock

from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import ContractValidationError, L2toL3HandoffPayload, validate_l3_to_l2_action
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.fixture
def active_payload():
    grid: list[str] = [" " * 80 for _ in range(24)]
    fields = {
        "user_id": FieldEntry(label="user_id", value="", row=10, col=20, length=8, protected=False, field_id="fld_user_id"),
        "protected_banner": FieldEntry(label="protected_banner", value="BANNER", row=0, col=0, length=10, protected=True, field_id="fld_banner"),
    }
    report = StabilityReport(is_stable=True, method="oia_byte", confidence=1.0, delta_value=0.0, iterations=1)
    state = RuntimeState(
        runtime_id="test_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="SYSTEM SCREEN",
        fields=fields,
        status_line="",
        stability_report=report,
        screen_hash="e" * 64,
        generation=1,
    )
    return L2toL3HandoffPayload(state=state)


def test_contract_rejects_value_length_overflow(active_payload):
    """Verify validate_l3_to_l2_action rejects input text longer than field length."""
    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_FIELD,
        generation_token=active_payload.state.generation_token,
        generation=1,
        ticket_id="ticket_overflow",
        field_id="fld_user_id",
        value="WAY_TOO_LONG_USER_ID_OVER_8_CHARS",
        action_id="ENTER",
    )

    with pytest.raises(ContractValidationError, match="Value length .* exceeds maximum length"):
        validate_l3_to_l2_action(intent, active_payload.state)


def test_contract_rejects_nonexistent_field(active_payload):
    """Verify validate_l3_to_l2_action rejects non-existent field_id."""
    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_FIELD,
        generation_token=active_payload.state.generation_token,
        generation=1,
        ticket_id="ticket_nonexistent",
        field_id="fld_does_not_exist",
        value="TEST",
        action_id="ENTER",
    )

    with pytest.raises(ContractValidationError, match="Target field_id 'fld_does_not_exist' not found"):
        validate_l3_to_l2_action(intent, active_payload.state)


@pytest.mark.asyncio
async def test_gateway_handles_validation_error_gracefully(active_payload):
    """Verify AIToolGateway returns structured failure when contract validation fails."""
    mock_dispatcher = AsyncMock()
    mock_dispatcher.dispatch.side_effect = ContractValidationError("Simulated contract failure: field protected")

    gateway = AIToolGateway(initial_payload=active_payload, dispatcher=mock_dispatcher)

    res = await gateway.execute_tool("set_field_and_submit", {
        "generation_token": active_payload.state.generation_token,
        "field_id": "fld_banner",
        "value": "ATTEMPT",
        "action": "ENTER",
    })

    assert res["success"] is False
    assert res["error"] == "ContractValidationError"
    assert "Simulated contract failure" in res["message"]


@pytest.mark.asyncio
async def test_real_dispatcher_validation_failure_preserves_ticket_correlation(active_payload):
    driver = AsyncMock()
    lowerer = AsyncMock()
    stability = AsyncMock()
    dispatcher = ActionDispatcher(
        driver=driver,
        reducer=object(),
        action_lowerer=lowerer,
        stability_engine=stability,
    )
    gateway = AIToolGateway(initial_payload=active_payload, dispatcher=dispatcher)

    result = await gateway.execute_tool(
        "set_field_and_submit",
        {
            "generation_token": active_payload.state.generation_token,
            "field_id": "fld_banner",
            "value": "ATTEMPT",
            "action": "ENTER",
        },
    )

    assert result["success"] is False
    assert result["ticket_id"].startswith("ticket_")
    assert result["generation_before"] == active_payload.state.generation
    lowerer.lower_and_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stability_exception_preserves_ticket_correlation(active_payload):
    driver = SimpleNamespace(runtime_id="test_node")
    lowerer = AsyncMock()
    lowerer.lower_and_execute.return_value = ActionResult(
        ticket_id="ticket-stability-error",
        success=True,
        generation_before=1,
        generation_after=2,
        screen_hash_before=active_payload.state.screen_hash,
        screen_hash_after="",
        execution_time_ms=1.0,
    )
    stability = AsyncMock()
    stability.wait_until_stable.side_effect = RuntimeError("synthetic stability failure")
    dispatcher = ActionDispatcher(
        driver=driver,
        reducer=object(),
        action_lowerer=lowerer,
        stability_engine=stability,
    )
    gateway = AIToolGateway(initial_payload=active_payload, dispatcher=dispatcher)

    result = await gateway.execute_tool(
        "trigger_action",
        {"generation_token": active_payload.state.generation_token, "action_id": "ENTER"},
    )

    assert result["success"] is False
    assert result["ticket_id"].startswith("ticket_")
    assert result["generation_before"] == 1
    assert "generation_after" not in result


def test_dispatcher_rejects_unknown_tool(active_payload):
    """Verify ActionDispatcher raises ValueError on invalid tool name."""
    mock_driver = AsyncMock()
    mock_reducer = AsyncMock()
    mock_lowerer = AsyncMock()
    mock_stability = AsyncMock()

    dispatcher = ActionDispatcher(
        driver=mock_driver,
        reducer=mock_reducer,
        action_lowerer=mock_lowerer,
        stability_engine=mock_stability,
    )

    with pytest.raises(ValueError, match="Unknown downward tool name"):
        dispatcher.create_intent("invalid_tool_name", {}, active_payload.state)
