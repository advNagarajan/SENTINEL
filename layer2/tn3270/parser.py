"""Stage 2a: TN3270 Stream Parser for decoding 3270 orders, WCC control bytes, and EBCDIC byte streams."""
from typing import Any, Optional
import structlog
from schemas.pipeline import Decoded3270Frame, TransportFrame

logger = structlog.get_logger(__name__)

# 3270 Write Commands
CMD_WRITE = 0xF1
CMD_ERASE_WRITE = 0xF5
CMD_ERASE_WRITE_ALT = 0x7E
CMD_WRITE_STRUCTURED_FIELD = 0xF3

# 3270 Orders
ORDER_SF = 0x1D    # Start Field
ORDER_SFE = 0x29   # Start Field Extended
ORDER_SBA = 0x11   # Set Buffer Address
ORDER_SA = 0x28    # Set Attribute
ORDER_IC = 0x13    # Insert Cursor
ORDER_RA = 0x3C    # Repeat to Address
ORDER_EUA = 0x12   # Erase Unprotected to Address
ORDER_PT = 0x05    # Program Tab

KNOWN_ORDERS = {ORDER_SF, ORDER_SFE, ORDER_SBA, ORDER_SA, ORDER_IC, ORDER_RA, ORDER_EUA, ORDER_PT}


class TN3270StreamParser:
    """Parses raw TN3270 TransportFrame byte payloads into structured Decoded3270Frames."""

    def parse_frame(self, frame: TransportFrame) -> Decoded3270Frame:
        raw = frame.raw_payload
        if not raw:
            return Decoded3270Frame(
                command=0,
                orders=[],
                raw_text_ebcdic=b"",
                oia_byte=b"\x00",
                cursor_address=0,
                wcc=None,
            )

        cmd = raw[0]
        pos = 1
        orders: list[dict[str, Any]] = []
        text_buf = bytearray()
        cursor_addr = 0
        oia_byte = b"\x00"
        wcc_byte: Optional[int] = None

        # Check for OIA byte header if present in TN3270E data header
        if len(raw) > 5 and raw[0] == 0x00 and raw[1] == 0x00:
            # TN3270E data header present
            oia_byte = bytes([raw[4]])
            pos = 5
            cmd = raw[pos] if pos < len(raw) else 0
            pos += 1

        # Check for WCC byte following Write / Erase-Write commands
        if cmd in (CMD_WRITE, CMD_ERASE_WRITE, CMD_ERASE_WRITE_ALT) and pos < len(raw):
            if raw[pos] not in KNOWN_ORDERS:
                wcc_byte = raw[pos]
                pos += 1

        while pos < len(raw):
            b = raw[pos]

            if b == ORDER_SF and pos + 1 < len(raw):
                attr_byte = raw[pos + 1]
                orders.append({"type": "SF", "attr": attr_byte, "offset": pos})
                text_buf.append(b)
                text_buf.append(attr_byte)
                pos += 2
            elif b == ORDER_SFE and pos + 1 < len(raw):
                count = raw[pos + 1]
                pairs = []
                idx = pos + 2
                for _ in range(count):
                    if idx + 1 < len(raw):
                        pairs.append((raw[idx], raw[idx + 1]))
                        idx += 2
                orders.append({"type": "SFE", "pairs": pairs, "offset": pos})
                pos = idx
            elif b == ORDER_SBA and pos + 2 < len(raw):
                b1, b2 = raw[pos + 1], raw[pos + 2]
                addr = ((b1 & 0x3F) << 6) | (b2 & 0x3F)
                orders.append({"type": "SBA", "address": addr, "offset": pos})
                pos += 3
            elif b == ORDER_IC:
                orders.append({"type": "IC", "offset": pos})
                cursor_addr = len(text_buf)
                pos += 1
            elif b == ORDER_SA and pos + 2 < len(raw):
                orders.append({"type": "SA", "attr_type": raw[pos + 1], "attr_val": raw[pos + 2], "offset": pos})
                pos += 3
            elif b == ORDER_RA and pos + 3 < len(raw):
                b1, b2, char_byte = raw[pos + 1], raw[pos + 2], raw[pos + 3]
                target_addr = ((b1 & 0x3F) << 6) | (b2 & 0x3F)
                orders.append({"type": "RA", "target_address": target_addr, "char": char_byte, "offset": pos})
                pos += 4
            elif b == ORDER_EUA and pos + 2 < len(raw):
                pos += 3
            else:
                text_buf.append(b)
                pos += 1

        return Decoded3270Frame(
            command=cmd,
            orders=orders,
            raw_text_ebcdic=bytes(text_buf),
            oia_byte=oia_byte,
            cursor_address=cursor_addr,
            wcc=wcc_byte,
            timestamp=frame.timestamp,
        )
