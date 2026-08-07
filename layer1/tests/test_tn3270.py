"""Unit tests for Layer 1 TN3270 Driver."""
import asyncio
import pytest
from layer1.tn3270 import TN3270Driver, encode_buffer_address
from schemas.events import RuntimeEventType


def test_encode_buffer_address():
    """Verify 3270 12-bit buffer address encoding."""
    # (0, 0) -> 0 -> (0x40, 0x40)
    addr_0 = encode_buffer_address(0, 0)
    assert addr_0 == bytes([0x40, 0x40])

    # (0, 1) -> 1 -> (0x40, 0xC1)
    addr_1 = encode_buffer_address(0, 1)
    assert addr_1 == bytes([0x40, 0xC1])


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
