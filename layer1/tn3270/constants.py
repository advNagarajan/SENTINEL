"""Constants, AID mappings, and buffer address encoding for IBM 3270 protocol."""
from typing import Optional

# Telnet Protocol Commands
TELNET_IAC = 0xFF
TELNET_DONT = 0xFE
TELNET_DO = 0xFD
TELNET_WONT = 0xFC
TELNET_WILL = 0xFB
TELNET_SB = 0xFA
TELNET_SE = 0xF0
TELNET_EOR = 0xEF

# Telnet Options
TELNET_OPT_BINARY = 0
TELNET_OPT_EOR = 25
TELNET_OPT_TERMINAL_TYPE = 24
TELNET_OPT_TN3270E = 40

# 3270 Orders
ORDER_SF = 0x1D   # Start Field
ORDER_SBA = 0x11  # Set Buffer Address
ORDER_IC = 0x13   # Insert Cursor
ORDER_PT = 0x05   # Program Tab
ORDER_RA = 0x3C   # Repeat to Address
ORDER_EUA = 0x12  # Erase Unprotected to Address

# Exact 3270 AID Key Table (IBM 3270 Data Stream Programmer's Reference)
AID_MAP: dict[str, int] = {
    "ENTER": 0x7D,
    "CLEAR": 0x6D,
    "PA1": 0x6C,
    "PA2": 0x6E,
    "PA3": 0x6B,
    "SYSREQ": 0xF0,
    "PF1": 0xF1,
    "PF2": 0xF2,
    "PF3": 0xF3,
    "PF4": 0xF4,
    "PF5": 0xF5,
    "PF6": 0xF6,
    "PF7": 0xF7,
    "PF8": 0xF8,
    "PF9": 0xF9,
    "PF10": 0x7A,
    "PF11": 0x7B,
    "PF12": 0x7C,
    "PF13": 0xC1,
    "PF14": 0xC2,
    "PF15": 0xC3,
    "PF16": 0xC4,
    "PF17": 0xC5,
    "PF18": 0xC6,
    "PF19": 0xC7,
    "PF20": 0xC8,
    "PF21": 0xC9,
    "PF22": 0x4A,
    "PF23": 0x4B,
    "PF24": 0x4C,
}

# Inverse AID map (byte -> name)
INV_AID_MAP: dict[int, str] = {v: k for k, v in AID_MAP.items()}

# 3270 12-bit Address Code Array
ADDR_CODE = [
    0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F,
    0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F,
    0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F,
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F,
]


def encode_buffer_address(row: int, col: int, cols: int = 80) -> bytes:
    """Encode (row, col) 0-indexed position into 2-byte 3270 buffer address."""
    addr = row * cols + col
    byte1 = ADDR_CODE[(addr >> 6) & 0x3F]
    byte2 = ADDR_CODE[addr & 0x3F]
    return bytes([byte1, byte2])


def decode_buffer_address(byte1: int, byte2: int, cols: int = 80) -> tuple[int, int]:
    """Decode 2-byte 3270 buffer address into (row, col) 0-indexed position."""
    b1 = byte1 & 0x3F
    b2 = byte2 & 0x3F
    addr = (b1 << 6) | b2
    return addr // cols, addr % cols
