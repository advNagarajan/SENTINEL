"""Layer 1 FreeDOS VNC Environment Driver."""
import asyncio
import time
from typing import Any, Callable, Optional
import numpy as np
import structlog

import asyncvnc
from layer1.base import EnvironmentDriver
from layer2.dos.constants import DOS_KEYMAP, KEY_ALIASES, normalize_key_name, parse_key_combo
from schemas.events import RuntimeEvent, RuntimeEventType
from schemas.pipeline import TransportFrame

logger = structlog.get_logger(__name__)

# Shifted character to base key mapping for US keyboard layout
SHIFTED_SYMBOL_MAP: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", ":": ";", "\"": "'",
    "<": ",", ">": ".", "?": "/", "|": "\\", "~": "`",
}


class DOSDriver(EnvironmentDriver):
    """Asyncio VNC driver for FreeDOS running in QEMU."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5900,
        runtime_id: str = "freedos_node_01",
        inter_key_delay_ms: int = 35,
        modifier_delay_ms: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self._runtime_id = runtime_id
        self.inter_key_delay_ms = inter_key_delay_ms
        self.modifier_delay_ms = modifier_delay_ms

        self._cm: Any = None
        self._client: Optional[asyncvnc.Client] = None
        self._is_connected: bool = False
        self._frozen: bool = False
        self._event_listeners: list[Callable[[RuntimeEvent], None]] = []
        self._last_frame_raw: Optional[np.ndarray] = None
        self._frame_seq: int = 0

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
        """Establish VNC connection to QEMU."""
        logger.info("l1_dos_connecting", layer="layer1", host=self.host, port=self.port)
        self._cm = asyncvnc.connect(self.host, self.port)
        self._client = await self._cm.__aenter__()
        self._is_connected = True
        logger.info("l1_dos_connected", layer="layer1", host=self.host, port=self.port)
        self._emit_event(RuntimeEventType.CONNECTED, f"Connected to FreeDOS VNC at {self.host}:{self.port}")

    async def disconnect(self) -> None:
        """Close VNC connection cleanly."""
        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm = None
            self._client = None
        self._is_connected = False
        logger.info("l1_dos_disconnected", layer="layer1", host=self.host, port=self.port)
        self._emit_event(RuntimeEventType.DISCONNECTED, "Disconnected from FreeDOS VNC")

    async def freeze(self) -> None:
        self._frozen = True

    async def unfreeze(self) -> None:
        self._frozen = False

    async def health_check(self) -> bool:
        return self._is_connected and self._client is not None

    async def screenshot(self) -> np.ndarray:
        """Capture current video framebuffer array (H, W, 3)."""
        if not self._client:
            raise ConnectionError("DOSDriver is not connected to VNC.")
        frame = await self._client.screenshot()
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[:, :, :3]
        self._last_frame_raw = frame
        return frame

    async def read_frame(self) -> TransportFrame:
        """Capture video frame and return as packaged TransportFrame."""
        frame = await self.screenshot()
        self._frame_seq += 1
        return TransportFrame(
            raw_payload=frame.tobytes(),
            is_eod=True,
            timestamp=time.time(),
            frame_seq=self._frame_seq,
        )

    async def write_raw(self, data: bytes) -> None:
        """Write raw keystroke characters."""
        text = data.decode("latin-1", errors="replace")
        await self.type_text(text)

    async def type_text(self, text: str) -> None:
        """Type literal ASCII text with pacing and proper shift state handling."""
        if not self._client:
            raise ConnectionError("DOSDriver is not connected to VNC.")

        # Rejection of forbidden characters
        for ch in text:
            if ch in ("\n", "\r", "\t") or ord(ch) < 32 or ord(ch) > 126:
                raise ValueError(
                    f"type_text only accepts printable ASCII (32-126). "
                    f"Got disallowed character code {ord(ch)} ({repr(ch)}). "
                    f"For submitting commands, use trigger_action('ENTER') or press_keys(['ENTER'])."
                )

        for ch in text:
            if ch in SHIFTED_SYMBOL_MAP:
                base_char = SHIFTED_SYMBOL_MAP[ch]
                with self._client.keyboard.hold("Shift_L"):
                    await asyncio.sleep(self.modifier_delay_ms / 1000.0)
                    self._client.keyboard.press(base_char)
                    await self._client.drain()
            elif "A" <= ch <= "Z":
                with self._client.keyboard.hold("Shift_L"):
                    await asyncio.sleep(self.modifier_delay_ms / 1000.0)
                    self._client.keyboard.press(ch.lower())
                    await self._client.drain()
            else:
                self._client.keyboard.press(ch)
                await self._client.drain()

            await asyncio.sleep(self.inter_key_delay_ms / 1000.0)

    async def press_keys(self, keys: list[str]) -> None:
        """Press special keys or modifier combos (e.g. ['ALT+F', 'ENTER', 'ESC'])."""
        if not self._client:
            raise ConnectionError("DOSDriver is not connected to VNC.")

        for key_spec in keys:
            mods, base = parse_key_combo(key_spec)

            # Determine key name for base key in asyncvnc
            base_key = base.lower() if len(base) == 1 else base

            if not mods:
                self._client.keyboard.press(base_key)
                await self._client.drain()
            elif len(mods) == 1:
                with self._client.keyboard.hold(mods[0]):
                    await asyncio.sleep(self.modifier_delay_ms / 1000.0)
                    self._client.keyboard.press(base_key)
                    await self._client.drain()
            elif len(mods) == 2:
                with self._client.keyboard.hold(mods[0]):
                    with self._client.keyboard.hold(mods[1]):
                        await asyncio.sleep(self.modifier_delay_ms / 1000.0)
                        self._client.keyboard.press(base_key)
                        await self._client.drain()

            await asyncio.sleep(self.inter_key_delay_ms / 1000.0)

    async def send_aid(self, aid_name: str, cursor_row: int = 0, cursor_col: int = 0) -> None:
        """Send an action key or combo (e.g. 'ENTER', 'ESC', 'ALT+F')."""
        await self.press_keys([aid_name])

    async def send_field_input(self, text: str, row: int, col: int, aid_name: str = "ENTER") -> None:
        """Type text and trigger an action key."""
        if text:
            await self.type_text(text)
        if aid_name:
            await self.send_aid(aid_name)
