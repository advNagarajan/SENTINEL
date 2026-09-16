"""Downward / Egress writer and 3270 packet encoder for AID and field input."""
import asyncio
from typing import Callable, Optional
import structlog

from layer1.tn3270.constants import (
    AID_MAP,
    ORDER_SBA,
    TELNET_EOR,
    TELNET_IAC,
    encode_buffer_address,
)
from schemas.events import RuntimeEventType

logger = structlog.get_logger(__name__)


class TN3270DownwardWriter:
    """Encodes keystrokes, AID keys, and field modifications into 3270 data stream packets."""

    @staticmethod
    def build_aid_packet(aid_name: str, cursor_row: int = 0, cursor_col: int = 0) -> bytes:
        """Build 3270 AID packet: [AID] [Cursor Address (2 bytes)] [IAC EOR]."""
        aid_byte = AID_MAP.get(aid_name.upper())
        if aid_byte is None:
            raise ValueError(f"Unknown AID key: '{aid_name}'. Supported AID keys: {list(AID_MAP.keys())}")

        addr_bytes = encode_buffer_address(cursor_row, cursor_col)
        return bytes([aid_byte]) + addr_bytes + bytes([TELNET_IAC, TELNET_EOR])

    @staticmethod
    def build_field_input_packet(
        text: str,
        row: int,
        col: int,
        aid_name: str = "ENTER",
        cursor_row: Optional[int] = None,
        cursor_col: Optional[int] = None,
    ) -> bytes:
        """Build 3270 single field input packet:
        [AID] [Cursor Addr] [SBA (0x11)] [Field Addr] [EBCDIC Text] [IAC EOR]
        """
        aid_byte = AID_MAP.get(aid_name.upper(), 0x7D)
        cur_r = cursor_row if cursor_row is not None else row
        cur_c = cursor_col if cursor_col is not None else col
        cursor_addr = encode_buffer_address(cur_r, cur_c)
        field_addr = encode_buffer_address(row, col)
        ebcdic_text = text.encode("cp037")

        return (
            bytes([aid_byte])
            + cursor_addr
            + bytes([ORDER_SBA])
            + field_addr
            + ebcdic_text
            + bytes([TELNET_IAC, TELNET_EOR])
        )

    @staticmethod
    def build_multi_field_packet(
        fields_data: list[tuple[int, int, str]],
        aid_name: str = "ENTER",
        cursor_row: int = 0,
        cursor_col: int = 0,
    ) -> bytes:
        """Build 3270 multi-field input packet:
        [AID] [Cursor Addr] { [SBA (0x11)] [Field Addr] [EBCDIC Text] }... [IAC EOR]
        """
        aid_byte = AID_MAP.get(aid_name.upper(), 0x7D)
        cursor_addr = encode_buffer_address(cursor_row, cursor_col)

        packet = bytearray([aid_byte])
        packet.extend(cursor_addr)

        for row, col, val in fields_data:
            field_addr = encode_buffer_address(row, col)
            ebcdic_text = val.encode("cp037")
            packet.append(ORDER_SBA)
            packet.extend(field_addr)
            packet.extend(ebcdic_text)

        packet.extend([TELNET_IAC, TELNET_EOR])
        return bytes(packet)

    async def send_packet(
        self,
        writer: asyncio.StreamWriter,
        packet: bytes,
    ) -> None:
        """Inject bytes into host socket."""
        writer.write(packet)
        await writer.drain()
