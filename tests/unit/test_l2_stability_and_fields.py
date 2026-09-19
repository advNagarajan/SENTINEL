"""Unit tests for Layer 2 Stability Engine, field extraction, and multi-field lowering."""
import pytest
from unittest.mock import AsyncMock

from layer2.tn3270 import (
    OIAStabilityEngine,
    ScreenObjectBuilder,
    TN3270ActionLowerer,
    TN3270StateReducer,
    TN3270StreamParser,
)
from schemas.actions import ActionType, CanonicalActionIntent
from schemas.pipeline import TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.mark.asyncio
async def test_oia_stability_engine_quiescence():
    """Verify OIAStabilityEngine successfully detects stable state via hash and OIA ready signal."""
    stability = OIAStabilityEngine(
        poll_interval_ms=10,
        settle_ms=20,
        max_wait_ms=1000,
        quiescence_ms=30,
    )

    reducer = TN3270StateReducer(rows=24, cols=80)
    mock_driver = AsyncMock()
    mock_driver.runtime_id = "test_node"

    # Frame 1: Erase/Write + Ready OIA
    text = "WELCOME TO IBM CICS".encode("cp037")
    payload = bytes([0xF5, 0x1D, 0x20]) + text
    frame = TransportFrame(raw_payload=payload)
    empty_frame = TransportFrame(raw_payload=b"")

    # Driver returns the screen on first call, then empty frames (quiescent stream)
    mock_driver.read_frame.side_effect = [frame, empty_frame, empty_frame, empty_frame, empty_frame]

    state, report = await stability.wait_until_stable(
        driver=mock_driver,
        reducer=reducer,
        runtime_id="test_node",
        generation=1,
    )

    assert state.is_stable is True
    assert report.is_stable is True
    assert state.screen_hash != ""
    assert "WELCOME TO IBM CICS" in state.raw_grid[0]


def test_field_extraction_protected_vs_editable():
    """Verify 3270 attribute bytes correctly differentiate protected vs editable fields."""
    parser = TN3270StreamParser()
    builder = ScreenObjectBuilder(rows=24, cols=80)

    # 3270 stream with:
    # 1. Protected field (SF order 0x1D with attr 0x28, bit 0x20 set -> protected=True)
    # 2. Editable field (SF order 0x1D with attr 0x00, bit 0x20 not set -> protected=False)
    prot_label = "USER ID:".encode("cp037")
    edit_val = "________".encode("cp037")

    raw = bytes([0xF5, 0x1D, 0x28]) + prot_label + bytes([0x1D, 0x00]) + edit_val
    frame = TransportFrame(raw_payload=raw)

    decoded = parser.parse_frame(frame)
    som = builder.build_object_model(decoded)

    assert len(som.fields) == 2
    f_label = som.fields[0]
    f_input = som.fields[1]

    assert f_label.protected is True
    assert f_input.protected is False


@pytest.mark.asyncio
async def test_action_lowerer_multi_field_submit():
    """Verify TN3270ActionLowerer correctly handles multi-field form submission."""
    grid: list[str] = [" " * 80 for _ in range(24)]
    fields = {
        "userid": FieldEntry(label="userid", value="", row=10, col=20, length=8, protected=False, field_id="fld_userid"),
        "password": FieldEntry(label="password", value="", row=11, col=20, length=8, protected=False, field_id="fld_password"),
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
        screen_hash="f" * 64,
        generation=1,
    )

    mock_driver = AsyncMock()
    lowerer = TN3270ActionLowerer()

    intent = CanonicalActionIntent(
        intent_type=ActionType.FILL_AND_SUBMIT,
        generation_token=state.generation_token,
        generation=1,
        ticket_id="ticket_multi",
        fields={"fld_userid": "ADMIN", "fld_password": "SECRET"},
        action_id="ENTER",
    )

    result = await lowerer.lower_and_execute(intent, mock_driver, state)
    assert result.success is True
    assert mock_driver.write_raw.called
    written = mock_driver.write_raw.call_args[0][0]

    # Verify both strings were encoded into EBCDIC in the written payload
    assert "ADMIN".encode("cp037") in written
    assert "SECRET".encode("cp037") in written
