"""TN3270 Protocol Environment Driver orchestrating upward reader and downward writer."""
import asyncio
import ssl
import time
from typing import Any, Callable, Optional
import structlog

from layer1.base import EnvironmentDriver
from layer1.tn3270.constants import AID_MAP, encode_buffer_address
from layer1.tn3270.downward import TN3270DownwardWriter
from layer1.tn3270.upward import TN3270UpwardReader
from schemas.events import RuntimeEvent, RuntimeEventType
from schemas.pipeline import TransportFrame

logger = structlog.get_logger(__name__)


class TN3270Driver(EnvironmentDriver):
    """Pure Python asyncio TN3270 socket driver for IBM Mainframes."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3270,
        device_type: str = "IBM-3279-2-E",
        use_tls: bool = False,
        runtime_id: str = "mainframe_node_01",
    ) -> None:
        self.host = host
        self.port = port
        self.device_type = device_type
        self.use_tls = use_tls
        self._runtime_id = runtime_id

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._is_connected: bool = False
        self._frozen: bool = False

        self._event_listeners: list[Callable[[RuntimeEvent], None]] = []

        # Upward and Downward components
        self.upward = TN3270UpwardReader(device_type=device_type)
        self.downward = TN3270DownwardWriter()

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def add_event_listener(self, listener: Callable[[RuntimeEvent], None]) -> None:
        self._event_listeners.append(listener)

    def _emit_event(self, event_type: RuntimeEventType, message: str, details: Optional[dict[str, Any]] = None) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            runtime_id=self._runtime_id,
            timestamp=time.time(),
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
        logger.info("l1_connecting", layer="layer1", host=self.host, port=self.port, tls=self.use_tls)

        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx
        )
        self._is_connected = True
        logger.info("l1_connected", layer="layer1", host=self.host, port=self.port)
        self._emit_event(RuntimeEventType.CONNECTED, f"Connected to {self.host}:{self.port}")

        # Negotiate initial Telnet options via upward reader
        await self.upward.negotiate_telnet(self._reader, self._writer)

    async def disconnect(self) -> None:
        """Close TN3270 socket connection cleanly."""
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._is_connected = False
        logger.info("l1_disconnected", layer="layer1", host=self.host, port=self.port)
        self._emit_event(RuntimeEventType.DISCONNECTED, "Disconnected from host")

    async def freeze(self) -> None:
        self._frozen = True
        logger.debug("l1_stream_frozen", layer="layer1")

    async def unfreeze(self) -> None:
        self._frozen = False
        logger.debug("l1_stream_unfrozen", layer="layer1")

    async def health_check(self) -> bool:
        return self._is_connected and self._writer is not None and not self._writer.is_closing()

    async def read_frame(self) -> TransportFrame:
        """Read next framed 3270 stream record via upward reader."""
        if not self._reader:
            raise RuntimeError("TN3270 driver is not connected")
        return await self.upward.read_frame(self._reader, self._emit_event)

    async def write_raw(self, data: bytes) -> None:
        """Inject raw bytes into the 3270 host connection."""
        if not self._writer:
            raise RuntimeError("TN3270 driver is not connected")
        await self.downward.send_packet(self._writer, data)

    async def send_aid(self, aid_name: str, cursor_row: int = 0, cursor_col: int = 0) -> None:
        """Send AID key (Enter, PF1-24, PA1-3, Clear) with optional cursor address."""
        packet = self.downward.build_aid_packet(aid_name, cursor_row=cursor_row, cursor_col=cursor_col)
        await self.write_raw(packet)
        aid_byte = AID_MAP.get(aid_name.upper(), 0x00)
        logger.info("l1_aid_injected", layer="layer1", aid_name=aid_name, aid_code=hex(aid_byte), cursor=(cursor_row, cursor_col))
        self._emit_event(
            RuntimeEventType.AID_SENT,
            f"Sent AID key: {aid_name}",
            {"aid_name": aid_name, "cursor": (cursor_row, cursor_col)},
        )

    async def send_field_input(self, text: str, row: int, col: int, aid_name: str = "ENTER") -> None:
        """Write text into a specific screen location and trigger AID key."""
        packet = self.downward.build_field_input_packet(text, row, col, aid_name=aid_name)
        await self.write_raw(packet)
        logger.info("l1_field_input_injected", layer="layer1", text=text, row=row, col=col, aid_name=aid_name)
        self._emit_event(
            RuntimeEventType.INPUT_TYPED,
            f"Typed text at ({row},{col}) and sent {aid_name}",
            {"text": text, "row": row, "col": col, "aid": aid_name},
        )
