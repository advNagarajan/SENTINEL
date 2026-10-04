"""Stage 2c: State Reducer for producing canonical RuntimeState and ScreenDeltas for FreeDOS."""
import hashlib
import re
import time
from typing import Any, Optional
import numpy as np
import structlog

from layer2.base import StateReducer
from layer2.dos.cursor import CursorObservation, DOSCursorDetector
from layer2.dos.screen_reader import DOSScreenReader, ScreenReaderResult
from schemas.contracts import L2toL3HandoffPayload, validate_l2_to_l3_contract
from schemas.pipeline import Decoded3270Frame, ScreenObjectModel, TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenDelta, ScreenType, StabilityReport

logger = structlog.get_logger(__name__)

DOS_AVAILABLE_ACTIONS: list[str] = [
    "ENTER", "ESC", "TAB", "BACKSPACE", "SPACE",
    "UP", "DOWN", "LEFT", "RIGHT",
    "PAGE_UP", "PAGE_DOWN", "HOME", "END",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10",
    "ALT+F", "ALT+X", "ALT+E", "ALT+S", "ALT+O", "ALT+H",
    "CTRL+C",
]


def detect_dos_prompt(raw_grid: list[str], cur_r: int, cur_c: int) -> Optional[tuple[int, int, int]]:
    r"""Detect whether an active DOS command prompt is present on the cursor row.
    
    Strict requirement:
    - Anchored at line start: ^[A-Za-z]:\...>, preventing program output from creating a fake prompt.
    - Must match on the active cursor row (cur_r).
    - The prompt pattern (e.g. 'C:\>', 'C:\DOS>', 'A:\>') must end precisely at the cursor column (cur_c).
    
    Returns (row, col, length) if matched, else None.
    """
    if 0 <= cur_r < len(raw_grid) and 0 <= cur_c <= len(raw_grid[cur_r]):
        line = raw_grid[cur_r]
        prefix = line[:cur_c]
        m = re.match(r"^([A-Za-z]:(?:\\[^>]*)?>)$", prefix)
        if m and m.end(1) == cur_c:
            return cur_r, cur_c, max(1, 80 - cur_c)

    return None


class DOSStateReducer(StateReducer):
    """Full implementation of Layer 2 State Reducer pipeline for FreeDOS."""

    def __init__(self, rows: int = 25, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self.reader = DOSScreenReader()
        self.cursor_detector = DOSCursorDetector()

    def parse_frame(self, frame: TransportFrame) -> np.ndarray:
        """Decode raw frame payload into RGB numpy array (400, 720, 3)."""
        raw = frame.raw_payload
        arr = np.frombuffer(raw, dtype=np.uint8)
        if len(arr) == 400 * 720 * 3:
            return arr.reshape((400, 720, 3))
        elif len(arr) == 400 * 720 * 4:
            return arr.reshape((400, 720, 4))[:, :, :3]
        elif len(arr) == 400 * 640 * 3:
            return arr.reshape((400, 640, 3))
        else:
            raise ValueError(f"Unexpected frame byte length: {len(arr)}")

    def build_object_model(self, decoded: np.ndarray) -> ScreenObjectModel:
        """Run screen reader on frame to produce intermediate ScreenObjectModel."""
        result = self.reader.read_frame(decoded)
        grid_matrix = [list(r) for r in result.raw_grid]
        attr_matrix = [[{} for _ in range(self.cols)] for _ in range(self.rows)]
        return ScreenObjectModel(
            grid_matrix=grid_matrix,
            attribute_matrix=attr_matrix,
            fields=[],
            cursor=(0, 0),
            oia_status="READY",
            rows=self.rows,
            cols=self.cols,
            timestamp=time.time(),
        )

    def reduce_state(
        self,
        som: ScreenObjectModel,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
    ) -> tuple[RuntimeState, Optional[ScreenDelta]]:
        """Produce canonical RuntimeState and ScreenDelta from ScreenObjectModel."""
        raw_grid: list[str] = ["".join(row_chars) for row_chars in som.grid_matrix]
        grid_blob = "\n".join(raw_grid).encode("utf-8")
        screen_hash = hashlib.sha256(grid_blob).hexdigest()

        cur_r = max(0, min(som.cursor[0], self.rows - 1))
        cur_c = max(0, min(som.cursor[1], self.cols - 1))

        # Title inference
        title = None
        for line in raw_grid:
            stripped = line.strip()
            if stripped:
                title = stripped[:60]
                break
        if not title:
            title = "FreeDOS Command Prompt"

        status_line = raw_grid[-1].strip() if raw_grid[-1].strip() else None

        # Synthesize editable field ONLY if a genuine DOS prompt is detected
        prompt_info = detect_dos_prompt(raw_grid, cur_r, cur_c)
        fields_dict: dict[str, FieldEntry] = {}
        if prompt_info is not None:
            p_row, p_col, p_len = prompt_info
            fields_dict["cmd_prompt"] = FieldEntry(
                label="cmd_prompt",
                value="",
                row=p_row,
                col=p_col,
                length=p_len,
                protected=False,
                field_id="cmd_prompt",
            )

        stability_report = StabilityReport(
            is_stable=True,
            method="dos_reducer",
            confidence=1.0,
            delta_value=0.0,
            iterations=1,
        )

        state = RuntimeState(
            runtime_id=runtime_id,
            screen_type=ScreenType.TEXT_GRID,
            is_stable=True,
            confidence_score=1.0,
            raw_grid=raw_grid,
            title=title,
            fields=fields_dict,
            status_line=status_line,
            stability_report=stability_report,
            screen_hash=screen_hash,
            generation=generation,
            timestamp=som.timestamp,
            cursor={"row": cur_r, "col": cur_c},
            screen_size={"rows": self.rows, "cols": self.cols},
            text_grid=raw_grid,
            available_actions=DOS_AVAILABLE_ACTIONS,
            metadata={"dominant_bg": 0},
        )

        delta: Optional[ScreenDelta] = None
        if previous_state is not None:
            cursor_moved = (
                previous_state.cursor["row"] != state.cursor["row"]
                or previous_state.cursor["col"] != state.cursor["col"]
            )
            text_changed = (previous_state.screen_hash != state.screen_hash)
            delta = ScreenDelta(
                generation_from=previous_state.generation,
                generation_to=state.generation,
                screen_hash_from=previous_state.screen_hash,
                screen_hash_to=state.screen_hash,
                changed_fields={},
                cursor_moved=cursor_moved,
                cursor_from=(previous_state.cursor["row"], previous_state.cursor["col"]),
                cursor_to=(state.cursor["row"], state.cursor["col"]),
                text_changed=text_changed,
                timestamp=time.time(),
            )

        return state, delta

    def build_state_from_frames(
        self,
        frames: list[np.ndarray],
        runtime_id: str,
        generation: int,
        stability_report: StabilityReport,
        previous_state: Optional[RuntimeState] = None,
    ) -> RuntimeState:
        """Produce canonical RuntimeState fusing multi-frame cursor detection and screen reader."""
        if not frames:
            raise ValueError("No frames provided to build_state_from_frames.")

        # Detect cursor and off-phase frame
        obs, off_frame = self.cursor_detector.detect_cursor_from_frames(frames)
        reading_frame = off_frame if off_frame is not None else frames[-1]

        # Read text grid from off-phase frame
        reader_res = self.reader.read_frame(reading_frame)
        raw_grid = reader_res.raw_grid

        grid_blob = "\n".join(raw_grid).encode("utf-8")
        screen_hash = hashlib.sha256(grid_blob).hexdigest()

        # Determine cursor position (clamped to screen bounds [0..24, 0..79])
        if obs.visible and obs.row is not None and obs.col is not None:
            cur_r = max(0, min(obs.row, self.rows - 1))
            cur_c = max(0, min(obs.col, self.cols - 1))
        else:
            # If cursor is hidden or off-screen, clamp to bottom prompt or previous
            if previous_state and 0 <= previous_state.cursor["row"] < self.rows:
                cur_r = previous_state.cursor["row"]
                cur_c = previous_state.cursor["col"]
            else:
                cur_r = self.rows - 1
                cur_c = 0

        # Title inference
        title = None
        for line in raw_grid:
            stripped = line.strip()
            if stripped:
                title = stripped[:60]
                break
        if not title:
            title = "FreeDOS Command Prompt"

        status_line = raw_grid[-1].strip() if raw_grid[-1].strip() else None

        # Synthesize editable field ONLY if a genuine DOS prompt is detected
        prompt_info = detect_dos_prompt(raw_grid, cur_r, cur_c)
        fields_dict: dict[str, FieldEntry] = {}
        if prompt_info is not None:
            p_row, p_col, p_len = prompt_info
            fields_dict["cmd_prompt"] = FieldEntry(
                label="cmd_prompt",
                value="",
                row=p_row,
                col=p_col,
                length=p_len,
                protected=False,
                field_id="cmd_prompt",
            )

        state = RuntimeState(
            runtime_id=runtime_id,
            screen_type=ScreenType.TEXT_GRID,
            is_stable=stability_report.is_stable,
            confidence_score=reader_res.confidence_score,
            raw_grid=raw_grid,
            title=title,
            fields=fields_dict,
            status_line=status_line,
            stability_report=stability_report,
            screen_hash=screen_hash,
            generation=generation,
            timestamp=time.time(),
            cursor={"row": cur_r, "col": cur_c},
            screen_size={"rows": self.rows, "cols": self.cols},
            text_grid=raw_grid,
            available_actions=DOS_AVAILABLE_ACTIONS,
            metadata={
                "dominant_bg": reader_res.dominant_bg,
                "cursor_shape": obs.shape,
                "cursor_visible": obs.visible,
            },
        )

        return state
