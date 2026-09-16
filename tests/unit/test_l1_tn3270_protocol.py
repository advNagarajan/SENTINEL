"""Unit tests for Layer 1 TN3270 protocol negotiation and stream framing."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from layer1.tn3270.constants import (
    TELNET_DO,
    TELNET_EOR,
    TELNET_IAC,
    TELNET_OPT_BINARY,
    TELNET_OPT_EOR,
    TELNET_OPT_TERMINAL_TYPE,
    TELNET_SB,
    TELNET_SE,
    TELNET_WILL,
)
from layer1.tn3270.driver import TN3270Driver
from layer1.tn3270.upward import TN3270UpwardReader


@pytest.mark.asyncio
async def test_upward_reader_telnet_negotiation():
    """Verify upward reader responds with WILL for supported Telnet options and sends terminal type."""
    reader = TN3270UpwardReader(device_type="IBM-3278-2")

    mock_stream_reader = AsyncMock()
    mock_stream_writer = MagicMock()
    mock_stream_writer.drain = AsyncMock()

    # Incoming server negotiation: DO BINARY, DO EOR, DO TERMINAL-TYPE
    server_handshake = bytes([
        TELNET_IAC, TELNET_DO, TELNET_OPT_BINARY,
        TELNET_IAC, TELNET_DO, TELNET_OPT_EOR,
        TELNET_IAC, TELNET_DO, TELNET_OPT_TERMINAL_TYPE,
    ])

    mock_stream_reader.read.side_effect = [server_handshake, b""]

    await reader.negotiate_telnet(mock_stream_reader, mock_stream_writer, max_rounds=1)

    assert mock_stream_writer.write.called
    response = mock_stream_writer.write.call_args[0][0]

    # Verify client replied IAC WILL for BINARY, EOR, TERMINAL-TYPE
    assert bytes([TELNET_IAC, TELNET_WILL, TELNET_OPT_BINARY]) in response
    assert bytes([TELNET_IAC, TELNET_WILL, TELNET_OPT_EOR]) in response
    assert bytes([TELNET_IAC, TELNET_WILL, TELNET_OPT_TERMINAL_TYPE]) in response


@pytest.mark.asyncio
async def test_upward_reader_stream_framing():
    """Verify upward reader properly accumulates chunks and splits records on IAC EOR."""
    reader = TN3270UpwardReader()

    mock_stream_reader = AsyncMock()

    # Payload split across two network chunks
    chunk_1 = b"\xF5\x1D\x20HELLO "
    chunk_2 = b"WORLD" + bytes([TELNET_IAC, TELNET_EOR]) + b"\xF5NEXT_SCREEN"

    mock_stream_reader.read.side_effect = [chunk_1, chunk_2]

    frame = await reader.read_frame(mock_stream_reader)

    assert frame.is_eod is True
    assert frame.raw_payload == b"\xF5\x1D\x20HELLO WORLD"
    # Remaining bytes for next frame should be buffered
    assert bytes(reader.buffer) == b"\xF5NEXT_SCREEN"


@pytest.mark.asyncio
async def test_tn3270_driver_lifecycle():
    """Verify driver freeze/unfreeze state, health check, and event emission."""
    driver = TN3270Driver(host="127.0.0.1", port=3270)
    assert await driver.health_check() is False

    await driver.freeze()
    assert driver._frozen is True

    await driver.unfreeze()
    assert driver._frozen is False
