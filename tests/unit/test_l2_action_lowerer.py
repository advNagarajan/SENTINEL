"""Unit tests for Layer 2 Action Lowerer."""
import pytest
from unittest.mock import AsyncMock

from layer2.tn3270 import TN3270ActionLowerer
from schemas.actions import ActionType, CanonicalActionIntent
from schemas.contracts import ContractValidationError
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.fixture
def sample_state():
    grid: list[str] = [" " * 80 for _ in range(24)]
    fields = {
        "userid": FieldEntry(label="userid", value="", row=10, col=20, length=8, protected=False, field_id="fld_userid"),
        "password": FieldEntry(label="password", value="", row=11, col=20, length=8, protected=False, field_id="fld_password"),
        "title_label": FieldEntry(label="title_label", value="LOGIN SCREEN", row=1, col=30, length=12, protected=True, field_id="fld_title_label"),
    }
    report = StabilityReport(is_stable=True, method="oia_byte", confidence=1.0, delta_value=0.0, iterations=1)
    state = RuntimeState(
        runtime_id="test_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="LOGIN SCREEN",
        fields=fields,
        status_line="",
        stability_report=report,
        screen_hash="a" * 64,
        generation=1,
    )
    return state


@pytest.mark.asyncio
async def test_action_lowerer_fill_field(sample_state):
    """Verify lower_and_execute translates FILL_FIELD and writes to driver."""
    mock_driver = AsyncMock()
    lowerer = TN3270ActionLowerer()

    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_FIELD,
        generation_token=sample_state.generation_token,
        generation=1,
        ticket_id="ticket_001",
        field_id="fld_userid",
        value="HERC01",
        action_id="ENTER",
    )

    result = await lowerer.lower_and_execute(intent, mock_driver, sample_state)
    assert result.success is True
    assert result.ticket_id == "ticket_001"
    assert mock_driver.write_raw.called
    written_bytes = mock_driver.write_raw.call_args[0][0]
    assert b"HERC01" not in written_bytes  # It should be EBCDIC encoded!
    assert "HERC01".encode("cp037") in written_bytes


@pytest.mark.asyncio
async def test_action_lowerer_trigger_action(sample_state):
    """Verify lower_and_execute translates TRIGGER_ACTION."""
    mock_driver = AsyncMock()
    lowerer = TN3270ActionLowerer()

    intent = CanonicalActionIntent(
        intent_type=ActionType.TRIGGER_ACTION,
        generation_token=sample_state.generation_token,
        generation=1,
        ticket_id="ticket_002",
        action_id="PF3",
    )

    result = await lowerer.lower_and_execute(intent, mock_driver, sample_state)
    assert result.success is True
    written_bytes = mock_driver.write_raw.call_args[0][0]
    assert written_bytes[0] == 0xF3  # PF3 AID byte


@pytest.mark.asyncio
async def test_action_lowerer_rejects_protected_field(sample_state):
    """Verify writing to protected field is rejected with error."""
    mock_driver = AsyncMock()
    lowerer = TN3270ActionLowerer()

    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_FIELD,
        generation_token=sample_state.generation_token,
        generation=1,
        ticket_id="ticket_003",
        field_id="fld_title_label",  # Protected field!
        value="HACK",
    )

    result = await lowerer.lower_and_execute(intent, mock_driver, sample_state)
    assert result.success is False
    assert result.error_message is not None
    assert "protected" in result.error_message
    assert not mock_driver.write_raw.called


@pytest.mark.asyncio
async def test_action_lowerer_rejects_stale_generation(sample_state):
    """Verify stale generation token is rejected."""
    mock_driver = AsyncMock()
    lowerer = TN3270ActionLowerer()

    intent = CanonicalActionIntent(
        intent_type=ActionType.TRIGGER_ACTION,
        generation_token="gen_999_deadbeef",  # Mismatched!
        generation=999,
        ticket_id="ticket_004",
        action_id="ENTER",
    )

    result = await lowerer.lower_and_execute(intent, mock_driver, sample_state)
    assert result.success is False
    assert result.error_message is not None
    assert "Stale action dispatch" in result.error_message
    assert not mock_driver.write_raw.called
