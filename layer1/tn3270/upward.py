"""Upward / Ingress reader and Telnet state machine for TN3270 stream records."""
import asyncio
import time
from typing import Callable, Optional
import structlog

from layer1.tn3270.constants import (
    TELNET_DO,
    TELNET_DONT,
    TELNET_EOR,
    TELNET_IAC,
    TELNET_OPT_BINARY,
    TELNET_OPT_EOR,
    TELNET_OPT_TERMINAL_TYPE,
    TELNET_SB,
    TELNET_SE,
    TELNET_WILL,
    TELNET_WONT,
)
from schemas.events import RuntimeEventType
from schemas.pipeline import TransportFrame

logger = structlog.get_logger(__name__)


class TN3270UpwardReader:
    """Handles Telnet option negotiations, stream buffering, and EOR framing into TransportFrames."""

    def __init__(self, device_type: str = "IBM-3278-2"):
        self.device_type = device_type
        self.buffer = bytearray()
        self.frame_seq = 0

    async def negotiate_telnet(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_rounds: int = 3,
        timeout: float = 1.0,
    ) -> None:
        """Process incoming Telnet IAC commands during handshake."""
        for _ in range(max_rounds):
            try:
                raw_data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
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
                        se_pos = raw_data.find(bytes([TELNET_IAC, TELNET_SE]), pos)
                        if se_pos != -1:
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
                    self.buffer.append(raw_data[pos])
                    pos += 1

            if response:
                writer.write(bytes(response))
                await writer.drain()

    async def read_frame(
        self,
        reader: asyncio.StreamReader,
        emit_event: Optional[Callable[[RuntimeEventType, str, dict], None]] = None,
    ) -> TransportFrame:
        """Read next framed 3270 stream record ending with IAC EOR or EOF."""
        data_acc = bytearray(self.buffer)
        self.buffer.clear()

        while True:
            if data_acc:
                eor_pos = data_acc.find(bytes([TELNET_IAC, TELNET_EOR]))
                if eor_pos != -1:
                    frame_payload = bytes(data_acc[:eor_pos])
                    self.buffer.extend(data_acc[eor_pos + 2:])
                    self.frame_seq += 1

                    frame = TransportFrame(
                        raw_payload=frame_payload,
                        is_eod=True,
                        timestamp=time.time(),
                        frame_seq=self.frame_seq,
                    )
                    logger.info("l1_frame_received", layer="layer1", frame_seq=self.frame_seq, bytes_len=len(frame_payload))
                    if emit_event:
                        emit_event(
                            RuntimeEventType.SCREEN_RECEIVED,
                            f"Received TransportFrame #{self.frame_seq}",
                            {"bytes_len": len(frame_payload)},
                        )
                    return frame

            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.2)
                if not chunk:
                    break
                data_acc.extend(chunk)
            except asyncio.TimeoutError:
                break

        self.frame_seq += 1
        frame = TransportFrame(
            raw_payload=bytes(data_acc),
            is_eod=True,
            timestamp=time.time(),
            frame_seq=self.frame_seq,
        )
        if data_acc:
            logger.info("l1_frame_received", layer="layer1", frame_seq=self.frame_seq, bytes_len=len(data_acc))
            if emit_event:
                emit_event(
                    RuntimeEventType.SCREEN_RECEIVED,
                    f"Received TransportFrame #{self.frame_seq}",
                    {"bytes_len": len(data_acc)},
                )
        return frame
