"""End-to-End Simulation Test: Complete typing and submission flow across all layers.

Verifies: Agent Tool Call -> L3 Gateway -> L2 Lowerer -> L1 Packet Generation -> L2 Stability -> New State.
"""
import pytest
from unittest.mock import AsyncMock

from layer1.tn3270.constants import AID_MAP, ORDER_SBA, TELNET_EOR, TELNET_IAC, encode_buffer_address
from layer2.action_lowerer import TN3270ActionLowerer
from layer2.builder import ScreenObjectBuilder
from layer2.parser import TN3270StreamParser
from layer2.reducer import TN3270StateReducer
from layer2.stability import OIAStabilityEngine
from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from schemas.contracts import L2toL3HandoffPayload
from schemas.pipeline import TransportFrame


@pytest.mark.asyncio
async def test_end_to_end_field_typing_flow():
    """Verify that an agent can discover an editable field, type text, and have it correctly encoded to L1."""
    # 1. Setup Layer 1 & 2 components
    parser = TN3270StreamParser()
    builder = ScreenObjectBuilder(rows=24, cols=80)
    reducer = TN3270StateReducer(rows=24, cols=80)
    lowerer = TN3270ActionLowerer()
    stability = OIAStabilityEngine(poll_interval_ms=10, settle_ms=20, max_wait_ms=500, quiescence_ms=20)

    # 2. Simulate raw inbound 3270 stream for Generation 1 (Login Screen):
    # Erase/Write (0xF5) + Protected Label "LOGON ID:" at (10, 10) + Editable Field at (10, 20)
    label_ebcdic = "LOGON ID:".encode("cp037")
    input_ebcdic = "        ".encode("cp037")  # 8 spaces
    gen1_raw = bytes([0xF5, 0x1D, 0x28]) + label_ebcdic + bytes([0x1D, 0x00]) + input_ebcdic

    frame_gen1 = TransportFrame(raw_payload=gen1_raw)
    decoded_gen1 = parser.parse_frame(frame_gen1)
    som_gen1 = builder.build_object_model(decoded_gen1)
    state_gen1, _ = reducer.reduce_state(som_gen1, runtime_id="node_01", generation=1)

    initial_payload = L2toL3HandoffPayload(state=state_gen1)

    # Verify field extraction identified the editable field
    editable_fields = [f for f in state_gen1.fields.values() if not f.protected]
    assert len(editable_fields) >= 1
    target_field = editable_fields[0]
    target_field_id = target_field.field_id

    # 3. Setup Mock Driver for L1 capture
    mock_driver = AsyncMock()
    mock_driver.runtime_id = "node_01"

    # Preparation for Generation 2: once typed, server responds with Menu Screen
    gen2_raw = bytes([0xF5, 0x1D, 0x28]) + "WELCOME TO IBM CICS PRIMARY MENU".encode("cp037")
    frame_gen2 = TransportFrame(raw_payload=gen2_raw)
    empty_frame = TransportFrame(raw_payload=b"")

    # Driver read_frame sequence: gen2 stream followed by quiescence
    mock_driver.read_frame.side_effect = [
        frame_gen2,
        empty_frame,
        empty_frame,
        empty_frame,
        frame_gen2,  # For handoff payload delta extraction
    ]

    # 4. Initialize Layer 3 Gateway
    dispatcher = ActionDispatcher(
        driver=mock_driver,
        reducer=reducer,
        action_lowerer=lowerer,
        stability_engine=stability,
    )
    gateway = AIToolGateway(initial_payload=initial_payload, dispatcher=dispatcher)

    # Verify compiled tools expose the editable field ID
    tools = gateway.get_tools()
    submit_tool = next(t for t in tools if t["function"]["name"] == "set_field_and_submit")
    assert target_field_id in submit_tool["function"]["parameters"]["properties"]["field_id"]["enum"]

    # 5. AGENT EXECUTES TYPING ACTION: Type "HERC01" into the field and send ENTER
    agent_arguments = {
        "generation_token": state_gen1.generation_token,
        "field_id": target_field_id,
        "value": "HERC01",
        "action": "ENTER",
    }

    result = await gateway.execute_tool("set_field_and_submit", agent_arguments)

    # 6. VERIFY OUTCOME OF THE ACTION EXECUTION
    assert result["success"] is True
    assert result["generation_before"] == 1
    assert result["generation_after"] == 2
    assert "Successfully executed 'set_field_and_submit'" in result["message"]

    # 7. VERIFY EXACT 3270 PROTOCOL BYTES WRITTEN TO LAYER 1
    assert mock_driver.write_raw.called
    written_packet: bytes = mock_driver.write_raw.call_args[0][0]

    # Check Byte 0: AID byte for ENTER (0x7D)
    assert written_packet[0] == AID_MAP["ENTER"]

    # Check SBA order is present (0x11)
    assert ORDER_SBA in written_packet
    sba_index = written_packet.index(ORDER_SBA)

    # Check Target Buffer Address immediately following SBA
    expected_field_addr = encode_buffer_address(target_field.row, target_field.col)
    assert written_packet[sba_index + 1: sba_index + 3] == expected_field_addr

    # Check EBCDIC encoded text "HERC01" immediately follows SBA address
    text_slice = written_packet[sba_index + 3: sba_index + 3 + len("HERC01")]
    assert text_slice == "HERC01".encode("cp037")

    # Check packet ends with Telnet IAC EOR sequence
    assert written_packet[-2:] == bytes([TELNET_IAC, TELNET_EOR])

    # 8. VERIFY GATEWAY STATE TRANSITION
    updated_payload = gateway.get_active_payload()
    assert updated_payload.state.generation == 2
    assert "WELCOME TO IBM CICS" in updated_payload.state.raw_grid[0]
