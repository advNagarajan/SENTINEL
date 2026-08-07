"""Unit tests for Layer 1 TN3270 Driver."""
import asyncio
import pytest
from layer1.tn3270 import AID_MAP, TN3270Driver, encode_buffer_address
from schemas.events import RuntimeEventType


def test_encode_buffer_address():
    """Verify 3270 12-bit buffer address encoding."""
    # (0, 0) -> 0 -> (0x40, 0x40)
    addr_0 = encode_buffer_address(0, 0)
    assert addr_0 == bytes([0x40, 0x40])

    # (0, 1) -> 1 -> (0x40, 0xC1)
    addr_1 = encode_buffer_address(0, 1)
    assert addr_1 == bytes([0x40, 0xC1])


def test_aid_map_completeness():
    """Verify AID_MAP contains all 24 PF keys, PA keys, Enter, Clear, and SysReq."""
    assert AID_MAP["ENTER"] == 0x7D
    assert AID_MAP["CLEAR"] == 0x6D
    assert AID_MAP["PA1"] == 0x6C
    
    # Check PF1-PF24
    for i in range(1, 25):
        key_name = f"PF{i}"
        assert key_name in AID_MAP, f"Missing {key_name} in AID_MAP"
    
    # Specific known IBM 3270 AID codes
    assert AID_MAP["PF1"] == 0xF1
    assert AID_MAP["PF9"] == 0xF9
    assert AID_MAP["PF10"] == 0x7A
    assert AID_MAP["PF12"] == 0x7C
    assert AID_MAP["PF13"] == 0xC1
    assert AID_MAP["PF21"] == 0xC9
    assert AID_MAP["PF22"] == 0x4A
    assert AID_MAP["PF24"] == 0x4C


@pytest.mark.asyncio
async def test_tn3270_driver_events():
    """Verify event listener registration and event emissions."""
    driver = TN3270Driver(host="127.0.0.1", port=3270)
    events = []

    driver.add_event_listener(lambda ev: events.append(ev))
    driver._emit_event(RuntimeEventType.CONNECTED, "Test Connected")

    assert len(events) == 1
    assert events[0].event_type == RuntimeEventType.CONNECTED
    assert events[0].message == "Test Connected"
    assert driver.runtime_id == "mainframe_node_01"
