"""Unit tests for Layer 2 State Reduction pipeline stages."""
import pytest
from layer2.builder import ScreenObjectBuilder
from layer2.parser import TN3270StreamParser
from layer2.reducer import TN3270StateReducer
from schemas.pipeline import Decoded3270Frame, TransportFrame
from schemas.state import ScreenType


def test_parser_and_builder_pipeline():
    """Verify raw stream parsing into ScreenObjectModel and canonical RuntimeState."""
    parser = TN3270StreamParser()
    builder = ScreenObjectBuilder(rows=24, cols=80)
    reducer = TN3270StateReducer(rows=24, cols=80)

    # Simulated TN3270 byte stream: Erase/Write (0xF5) + SF Order + text in EBCDIC
    # "IBM MAINFRAME" in EBCDIC cp037: b'\xc9\xc2\xd4\x40\xd4\xc1\xc9\xd5\xc6\xd2\xc1\xd4\xc5'
    ebcdic_text = "IBM MAINFRAME LOGIN".encode("cp037")
    raw_payload = bytes([0xF5, 0x1D, 0x20]) + ebcdic_text

    frame = TransportFrame(raw_payload=raw_payload, is_eod=True)
    decoded = parser.parse_frame(frame)

    assert decoded.command == 0xF5
    assert len(decoded.orders) == 1
    assert decoded.orders[0]["type"] == "SF"

    som = builder.build_object_model(decoded)
    assert som.rows == 24
    assert som.cols == 80
    assert "IBM MAINFRAME LOGIN" in "".join(som.grid_matrix[0])

    state, delta = reducer.reduce_state(som, runtime_id="test_node", generation=1)

    assert state.runtime_id == "test_node"
    assert state.screen_type == ScreenType.TEXT_GRID
    assert state.generation == 1
    assert state.screen_hash != ""
    assert len(state.screen_hash) == 64  # SHA-256 hex string length
    assert delta is None


def test_screen_delta_calculation():
    """Verify ScreenDelta generation when transitioning from State 1 to State 2."""
    reducer = TN3270StateReducer(rows=24, cols=80)

    # State 1
    ebcdic_text_1 = "BALANCE: 001250".encode("cp037")
    frame_1 = TransportFrame(raw_payload=bytes([0xF5, 0x1D, 0x20]) + ebcdic_text_1)
    decoded_1 = reducer.parse_frame(frame_1)
    som_1 = reducer.build_object_model(decoded_1)
    state_1, _ = reducer.reduce_state(som_1, runtime_id="test_node", generation=1)

    # State 2 (Updated Balance)
    ebcdic_text_2 = "BALANCE: 001500".encode("cp037")
    frame_2 = TransportFrame(raw_payload=bytes([0xF5, 0x1D, 0x20]) + ebcdic_text_2)
    decoded_2 = reducer.parse_frame(frame_2)
    som_2 = reducer.build_object_model(decoded_2)
    state_2, delta = reducer.reduce_state(som_2, runtime_id="test_node", generation=2, previous_state=state_1)

    assert delta is not None
    assert delta.generation_from == 1
    assert delta.generation_to == 2
    assert delta.text_changed is True
    assert delta.screen_hash_from != delta.screen_hash_to
