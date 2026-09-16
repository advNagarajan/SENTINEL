"""Unit tests for restructured Layer 1 TN3270 upward and downward modules."""
import pytest
from layer1.tn3270.constants import (
    AID_MAP,
    ORDER_SBA,
    TELNET_EOR,
    TELNET_IAC,
    decode_buffer_address,
    encode_buffer_address,
)
from layer1.tn3270.downward import TN3270DownwardWriter
from layer1.tn3270.upward import TN3270UpwardReader


def test_encode_and_decode_buffer_address():
    """Verify 12-bit 3270 buffer address encoding and decoding roundtrip."""
    for row in [0, 5, 10, 23]:
        for col in [0, 20, 50, 79]:
            addr_bytes = encode_buffer_address(row, col)
            assert len(addr_bytes) == 2
            dec_r, dec_c = decode_buffer_address(addr_bytes[0], addr_bytes[1])
            assert dec_r == row
            assert dec_c == col


def test_downward_build_aid_packet():
    """Verify build_aid_packet generates valid 3270 stream with AID, cursor, and IAC EOR."""
    packet = TN3270DownwardWriter.build_aid_packet("ENTER", cursor_row=0, cursor_col=0)
    assert packet[0] == AID_MAP["ENTER"]  # 0x7D
    # Cursor at (0, 0)
    expected_addr = encode_buffer_address(0, 0)
    assert packet[1:3] == expected_addr
    # IAC EOR terminator
    assert packet[3:5] == bytes([TELNET_IAC, TELNET_EOR])

    # PF3 test
    pf3_packet = TN3270DownwardWriter.build_aid_packet("PF3", cursor_row=12, cursor_col=40)
    assert pf3_packet[0] == AID_MAP["PF3"]  # 0xF3
    assert pf3_packet[1:3] == encode_buffer_address(12, 40)
    assert pf3_packet[-2:] == bytes([TELNET_IAC, TELNET_EOR])


def test_downward_build_aid_packet_invalid():
    """Verify invalid AID key raises ValueError."""
    with pytest.raises(ValueError, match="Unknown AID key"):
        TN3270DownwardWriter.build_aid_packet("NONEXISTENT_KEY")


def test_downward_build_field_input_packet():
    """Verify build_field_input_packet correctly formats SBA, EBCDIC text, and AID."""
    text = "HERC01"
    row, col = 10, 20
    packet = TN3270DownwardWriter.build_field_input_packet(text, row, col, aid_name="ENTER")

    # [AID=0x7D, Cursor(2 bytes), SBA=0x11, TargetAddr(2 bytes), EBCDIC, IAC, EOR]
    assert packet[0] == 0x7D
    cursor_addr = encode_buffer_address(row, col)
    assert packet[1:3] == cursor_addr
    assert packet[3] == ORDER_SBA
    assert packet[4:6] == cursor_addr

    # Check EBCDIC text matches
    ebcdic_slice = packet[6:6 + len(text)]
    assert ebcdic_slice.decode("cp037") == text

    # Check IAC EOR termination
    assert packet[-2:] == bytes([TELNET_IAC, TELNET_EOR])


def test_downward_build_multi_field_packet():
    """Verify multi-field packet packs multiple SBA sequences."""
    fields_data = [
        (10, 20, "USER01"),
        (11, 20, "SECRET"),
    ]
    packet = TN3270DownwardWriter.build_multi_field_packet(fields_data, aid_name="ENTER", cursor_row=10, cursor_col=20)
    assert packet[0] == 0x7D
    assert packet.count(ORDER_SBA) == 2
    assert packet[-2:] == bytes([TELNET_IAC, TELNET_EOR])
