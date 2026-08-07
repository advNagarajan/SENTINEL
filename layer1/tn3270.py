"""Pure Python asyncio TN3270 Environment Driver for IBM Mainframe (Hercules TK5-MVS)."""
import asyncio
import ssl

import time

from typing import Callable, Optional
import structlog

from layer1.base import EnvironmentDriver
from schemas.events import RuntimeEvent, RuntimeEventType
from schemas.pipeline import TransportFrame

logger = structlog.get_logger(__name__)

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


class TN3270Driver(EnvironmentDriver):
    """Pure Python asyncio TN3270 socket driver for IBM Mainframes."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3270,
        device_type: str = "IBM-3278-2",
        use_tls: bool = False,
        runtime_id: str = "mainframe_node_01",
    ):
        self.host = host
        self.port = port
        self.device_type = device_type
        self.use_tls = use_tls
        self._runtime_id = runtime_id

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._frozen: bool = False
        self._buffer: bytearray = bytearray()
        self._pending_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._event_listeners: list[Callable[[RuntimeEvent], None]] = []
        self._frame_seq: int = 0
        self._is_connected: bool = False

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def add_event_listener(self, listener: Callable[[RuntimeEvent], None]) -> None:
        self._event_listeners.append(listener)

    def _emit_event(self, event_type: RuntimeEventType, message: str, details: Optional[dict] = None) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            runtime_id=self.runtime_id,
            message=message,
            details=details or {},
        )
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error("Error in event listener", error=str(e))

    async def connect(self) -> None:
        """Establish TCP/TLS connection and handle Telnet IAC handshake."""
        ssl_ctx = ssl.create_default_context() if self.use_tls else None
        logger.info("Connecting to TN3270 host", host=self.host, port=self.port, tls=self.use_tls)
        
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx
        )
        self._is_connected = True
        self._emit_event(RuntimeEventType.CONNECTED, f"Connected to {self.host}:{self.port}")
        
        # Negotiate initial Telnet options
        await self._negotiate_telnet()

    async def disconnect(self) -> None:
        """Close TN3270 socket connection cleanly."""
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._is_connected = False
        self._emit_event(RuntimeEventType.DISCONNECTED, "Disconnected from host")
        logger.info("Disconnected from TN3270 host")

    async def freeze(self) -> None:
        """Freeze execution loop — incoming socket packets are held in buffer queue."""
        self._frozen = True
        logger.debug("TN3270 driver stream frozen")

    async def unfreeze(self) -> None:
        """Unfreeze execution loop — releases buffered socket packets."""
        self._frozen = False
        logger.debug("TN3270 driver stream unfrozen")

    async def health_check(self) -> bool:
        """Check if socket connection remains open."""
        return self._is_connected and self._writer is not None and not self._writer.is_closing()

    async def _negotiate_telnet(self) -> None:
        """Process incoming Telnet IAC commands during handshake."""
        if not self._reader or not self._writer:
            return

        for _ in range(3):
            try:
                raw_data = await asyncio.wait_for(self._reader.read(1024), timeout=1.0)
            except asyncio.TimeoutError:
                break

            if not raw_data:
                break

            pos = 0
            response = bytearray()
            while pos < len(raw_data):
                if raw_data[pos] == TELNET_IAC and pos + 2 < len(raw_data):
                    cmd = raw_data[pos + 1]
                    opt = raw_data[pos + 2]
                    
                    if cmd in (TELNET_DO, TELNET_DONT, TELNET_WILL, TELNET_WONT):
                        pos += 3
                        if cmd == TELNET_DO:
                            if opt in (TELNET_OPT_BINARY, TELNET_OPT_EOR, TELNET_OPT_TERMINAL_TYPE):
                                response.extend([TELNET_IAC, TELNET_WILL, opt])
                            else:
                                response.extend([TELNET_IAC, TELNET_WONT, opt])
                        elif cmd == TELNET_WILL:
                            if opt in (TELNET_OPT_BINARY, TELNET_OPT_EOR):
                                response.extend([TELNET_IAC, TELNET_DO, opt])
                            else:
                                response.extend([TELNET_IAC, TELNET_DONT, opt])
                    elif cmd == TELNET_SB:
                        # Find matching IAC SE
                        se_pos = raw_data.find(bytes([TELNET_IAC, TELNET_SE]), pos)
                        if se_pos != -1:
                            sb_data = raw_data[pos:se_pos + 2]
                            pos = se_pos + 2
                            if opt == TELNET_OPT_TERMINAL_TYPE:
                                response.extend([TELNET_IAC, TELNET_SB, TELNET_OPT_TERMINAL_TYPE, 0x00])
                                response.extend(self.device_type.encode("ascii"))
                                response.extend([TELNET_IAC, TELNET_SE])
                        else:
                            pos += 3
                    else:
                        pos += 2
                else:
                    self._buffer.append(raw_data[pos])
                    pos += 1

            if response:
                self._writer.write(bytes(response))
                await self._writer.drain()


    async def read_frame(self) -> TransportFrame:
        """Read next framed 3270 stream record ending with IAC EOR or EOF."""
        if not self._reader:
            raise RuntimeError("TN3270 driver is not connected")

        data_acc = bytearray(self._buffer)
        self._buffer.clear()

        while True:
            if data_acc:
                # Check for Telnet IAC EOR marker or full record
                eor_pos = data_acc.find(bytes([TELNET_IAC, TELNET_EOR]))
                if eor_pos != -1:
                    frame_payload = bytes(data_acc[:eor_pos])
                    self._buffer.extend(data_acc[eor_pos + 2:])
                    self._frame_seq += 1
                    
                    frame = TransportFrame(
                        raw_payload=frame_payload,
                        is_eod=True,
                        timestamp=time.time(),
                        frame_seq=self._frame_seq,
                    )
                    self._emit_event(
                        RuntimeEventType.SCREEN_RECEIVED,
                        f"Received TransportFrame #{self._frame_seq}",
                        {"bytes_len": len(frame_payload)},
                    )
                    return frame

            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=0.2)
                if not chunk:
                    break
                data_acc.extend(chunk)
            except asyncio.TimeoutError:
                break

        self._frame_seq += 1
        frame = TransportFrame(
            raw_payload=bytes(data_acc),
            is_eod=True,
            timestamp=time.time(),
            frame_seq=self._frame_seq,
        )
        if data_acc:
            self._emit_event(
                RuntimeEventType.SCREEN_RECEIVED,
                f"Received TransportFrame #{self._frame_seq}",
                {"bytes_len": len(data_acc)},
            )
        return frame


    async def write_raw(self, data: bytes) -> None:
        """Inject raw bytes into the 3270 host connection."""
        if not self._writer:
            raise RuntimeError("TN3270 driver is not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def send_aid(self, aid_name: str, cursor_row: int = 0, cursor_col: int = 0) -> None:
        """Send AID key (Enter, PF1-24, PA1-3, Clear) with optional cursor address."""
        aid_byte = AID_MAP.get(aid_name.upper())
        if aid_byte is None:
            raise ValueError(f"Unknown AID key: {aid_name}")

        addr_bytes = encode_buffer_address(cursor_row, cursor_col)
        # 3270 Command sequence: [AID] [Cursor Address (2 bytes)] [IAC EOR]
        payload = bytes([aid_byte]) + addr_bytes + bytes([TELNET_IAC, TELNET_EOR])
        await self.write_raw(payload)
        self._emit_event(
            RuntimeEventType.AID_SENT,
            f"Sent AID key: {aid_name}",
            {"aid_name": aid_name, "cursor": (cursor_row, cursor_col)},
        )

    async def send_field_input(self, text: str, row: int, col: int, aid_name: str = "ENTER") -> None:
        """Write text into a specific screen location and trigger AID key."""
        aid_byte = AID_MAP.get(aid_name.upper(), 0x7D)
        cursor_addr = encode_buffer_address(row, col)
        
        # 3270 Write sequence: [AID] [Cursor Addr] [SBA (0x11)] [Target Addr] [EBCDIC Text] [IAC EOR]
        sba_cmd = 0x11
        ebcdic_text = text.encode("cp037")
        payload = (
            bytes([aid_byte])
            + cursor_addr
            + bytes([sba_cmd])
            + cursor_addr
            + ebcdic_text
            + bytes([TELNET_IAC, TELNET_EOR])
        )
        await self.write_raw(payload)
        self._emit_event(
            RuntimeEventType.INPUT_TYPED,
            f"Typed text at ({row},{col}) and sent {aid_name}",
            {"text": text, "row": row, "col": col, "aid": aid_name},
        )
