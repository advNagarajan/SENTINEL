"""Canonical RuntimeState, ScreenDelta, and ScreenSnapshot schema definitions."""
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional


class ScreenType(Enum):
    TEXT_GRID = "text_grid"    # TN3270, DOS, serial UART
    VISUAL = "visual"          # QEMU framebuffer (OCR)
    MEMORY = "memory"          # DOS symbolic memory
    REGISTER = "register"      # ARM GDB register state


@dataclass
class FieldEntry:
    """Canonical representation of an interactive or read-only screen field."""
    label: str
    value: str
    row: int
    col: int
    length: int
    protected: bool
    hidden: bool = False
    numeric: bool = False


@dataclass
class StabilityReport:
    """Summary report from the stability engine."""
    is_stable: bool
    method: str
    confidence: float
    delta_value: float
    iterations: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeState:
    """Canonical representation of target system state consumed by downstream layers."""
    runtime_id: str
    screen_type: ScreenType
    is_stable: bool
    confidence_score: float
    raw_grid: list[str]  # 80x24 text rows
    title: Optional[str]
    fields: dict[str, FieldEntry]
    status_line: Optional[str]
    stability_report: StabilityReport
    screen_hash: str
    version: str = "1.0"
    generation: int = 0
    timestamp: float = field(default_factory=time.time)
    cursor: dict[str, int] = field(default_factory=lambda: {"row": 0, "col": 0})
    screen_size: dict[str, int] = field(default_factory=lambda: {"rows": 24, "cols": 80})
    text_grid: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text_grid and self.raw_grid:
            self.text_grid = self.raw_grid


@dataclass
class ScreenDelta:
    """Diff computed between consecutive RuntimeStates (generation N to N+1)."""
    generation_from: int
    generation_to: int
    screen_hash_from: str
    screen_hash_to: str
    changed_fields: dict[str, dict[str, str]]  # field_name -> {"old": v1, "new": v2}
    cursor_moved: bool
    cursor_from: tuple[int, int]
    cursor_to: tuple[int, int]
    text_changed: bool
    timestamp: float = field(default_factory=time.time)


@dataclass
class ScreenSnapshot:
    """Complete snapshot of raw bytes, decoded frame, state, and metadata for offline debugging and fixtures."""
    snapshot_id: str
    runtime_id: str
    generation: int
    raw_bytes: bytes
    screen_hash: str
    state: RuntimeState
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
