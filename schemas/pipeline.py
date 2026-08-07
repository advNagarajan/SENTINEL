"""Data contracts for internal Layer 1 to Layer 2 pipeline stages."""
from dataclasses import dataclass, field
import time
from typing import Any, Optional


@dataclass
class TransportFrame:
    """Framed raw protocol data record emitted by Layer 1 environment driver."""
    raw_payload: bytes
    is_eod: bool = True
    timestamp: float = field(default_factory=time.time)
    frame_seq: int = 0


@dataclass
class Decoded3270Frame:
    """Output of 3270 order stream parser (Stage 2a)."""
    command: int
    orders: list[dict[str, Any]]
    raw_text_ebcdic: bytes
    oia_byte: bytes
    cursor_address: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Field3270:
    """3270 Field Object representation in ScreenObjectModel (Stage 2b)."""
    start_row: int
    start_col: int
    end_row: int
    end_col: int
    length: int
    protected: bool
    numeric: bool
    hidden: bool
    intense: bool
    value: str
    label: Optional[str] = None


@dataclass
class ScreenObjectModel:
    """Semantic UI object model representing a decoded screen grid and field objects (Stage 2b)."""
    grid_matrix: list[list[str]]
    attribute_matrix: list[list[dict[str, Any]]]
    fields: list[Field3270]
    cursor: tuple[int, int]  # (row, col)
    oia_status: str          # "BUSY" (0xF1) | "READY" (0x00)
    rows: int = 24
    cols: int = 80
    timestamp: float = field(default_factory=time.time)
