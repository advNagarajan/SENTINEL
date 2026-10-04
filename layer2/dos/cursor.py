"""Milestone 5: FreeDOS Hardware Cursor Perception, Shape, and Visibility Detector.

Detects VGA text-mode hardware cursor position, shape (underline vs block), visibility,
and recovers the obscured underlying character using off-phase multi-frame sampling.
"""
from dataclasses import dataclass
from typing import Any, Optional
import numpy as np

from layer2.dos.constants import VGA_PALETTE_RGB
from layer2.dos.glyph_table import GlyphTable


@dataclass
class CursorObservation:
    """Perceived hardware cursor state from video frames."""
    row: Optional[int] = None
    col: Optional[int] = None
    visible: bool = False
    shape: str = "hidden"  # "underline", "block", "half_block", "hidden"
    underlying_char: str = " "
    underlying_fg: str = "7"
    underlying_bg: str = "0"
    is_blinking: bool = False
    confidence: float = 1.0


class DOSCursorDetector:
    """Detects VGA text-mode hardware cursor position, shape, visibility, and underlying character.
    
    QEMU VGA hardware cursor toggles every 267 ms (~533 ms full cycle).
    By sampling frames at ~80-100 ms intervals over a >= 600 ms window:
    1. Detect the toggling cell (row, col) whose pixels invert/toggle on the hardware blink timer.
    2. Discriminate shape between 'underline' (bottom scanlines 14-15) and 'block' (most scanlines).
    3. Reconstruct the underlying character and color attributes from the cursor 'OFF' phase frame.
    4. Detect hidden cursor if no cell toggles, or if cursor position is off-screen (row >= 25).
    """

    def __init__(self, glyph_table: Optional[GlyphTable] = None) -> None:
        self.glyph_table = glyph_table or GlyphTable()
        self.palette = np.array(VGA_PALETTE_RGB, dtype=np.int32)

    def _map_pixel_to_vga(self, rgb: np.ndarray) -> int:
        diff = self.palette - rgb
        dist = np.sum(diff * diff, axis=1)
        return int(np.argmin(dist))

    def detect_cursor_from_frames(
        self,
        frames: list[np.ndarray],
        cell_h: int = 16,
        cell_w: int = 9,
    ) -> tuple[CursorObservation, Optional[np.ndarray]]:
        """Analyze a sequence of video frames to identify cursor position, shape, and off-phase frame.
        
        Returns:
            (CursorObservation, best_off_phase_frame)
            best_off_phase_frame is the frame where the cursor is OFF, suitable for reading
            the cell under the cursor without occlusion.
        """
        if not frames:
            return CursorObservation(visible=False, shape="hidden"), None

        # If only 1 frame provided, no temporal blink detection possible
        if len(frames) == 1:
            return CursorObservation(visible=False, shape="hidden"), frames[0]

        # Convert frames to RGB (H, W, 3)
        rgb_frames = []
        for f in frames:
            if f.ndim == 3 and f.shape[2] == 4:
                rgb_frames.append(f[:, :, :3])
            else:
                rgb_frames.append(f)

        h, w = rgb_frames[0].shape[:2]
        rows = h // cell_h
        cols = w // cell_w

        # Compute cell-by-cell temporal variation across frames
        # Shape: (N, rows, cols, cell_h, cell_w, 3)
        stacked = np.array(rgb_frames)  # (N, H, W, 3)
        N = len(stacked)

        # Reshape into cells: (N, rows, cell_h, cols, cell_w, 3) -> (N, rows, cols, cell_h, cell_w, 3)
        cells = (
            stacked.reshape(N, rows, cell_h, cols, cell_w, 3)
            .transpose(0, 1, 3, 2, 4, 5)
        )

        # Variance of each cell across time: sum over (N, cell_h, cell_w, 3)
        cell_diffs = np.sum(np.abs(np.diff(cells.astype(np.int32), axis=0)), axis=(0, 3, 4, 5))  # (rows, cols)

        # Find cells with significant temporal toggling
        toggling_indices = np.argwhere(cell_diffs > 500)

        if len(toggling_indices) == 0:
            # No blinking cursor detected (cursor is hidden, off-screen, or blink disabled)
            return CursorObservation(visible=False, shape="hidden"), rgb_frames[0]

        # In typical screens, only 1 cell is the cursor. If multiple cells toggle (e.g. text blink),
        # prioritize the cell whose bottom scanlines toggle on the hardware cursor pattern.
        best_candidate = None
        best_shape = "underline"
        best_toggling_scanlines = []
        best_off_frame_idx = 0

        for r, c in toggling_indices:
            cell_seq = cells[:, r, c]  # (N, cell_h, cell_w, 3)
            # Find which scanlines (0..15) differ across time
            scanline_diffs = np.sum(np.abs(np.diff(cell_seq.astype(np.int32), axis=0)), axis=(0, 2, 3))
            toggling_scanlines = np.where(scanline_diffs > 50)[0]

            if len(toggling_scanlines) == 0:
                continue

            # Classify shape:
            # Underline: toggling is strictly in bottom lines (lines 13, 14, 15)
            # Block: toggling spans >= 6 scanlines
            if np.all(toggling_scanlines >= 13):
                candidate_shape = "underline"
            elif len(toggling_scanlines) >= 6:
                candidate_shape = "block"
            elif np.all(toggling_scanlines >= 8):
                candidate_shape = "half_block"
            else:
                candidate_shape = "underline"

            # Prefer underline or block cursor
            best_candidate = (int(r), int(c))
            best_shape = candidate_shape
            best_toggling_scanlines = toggling_scanlines
            break

        if best_candidate is None:
            return CursorObservation(visible=False, shape="hidden"), rgb_frames[0]

        cur_r, cur_c = best_candidate

        # Find the OFF-phase frame for the cursor cell
        # In underline cursor: the OFF frame has FEWER foreground pixels on scanlines 14-15
        # In block cursor: the OFF frame has higher match confidence / lower error against CP437 font
        candidate_cell_seq = cells[:, cur_r, cur_c]  # (N, cell_h, cell_w, 3)

        off_idx = 0
        best_err = 999
        recovered_char = " "
        recovered_fg = "7"
        recovered_bg = "0"

        for idx in range(N):
            cell_frame = candidate_cell_seq[idx]  # (cell_h, cell_w, 3)
            unique_cols = np.unique(cell_frame.reshape(-1, 3), axis=0)

            if len(unique_cols) == 1:
                vga_bg = self._map_pixel_to_vga(unique_cols[0])
                if 0 <= best_err:
                    best_err = 0
                    off_idx = idx
                    recovered_char = " "
                    recovered_fg = f"{vga_bg:x}"
                    recovered_bg = f"{vga_bg:x}"

            elif len(unique_cols) == 2:
                # Test both color assignments
                col_a = unique_cols[0]
                col_b = unique_cols[1]
                vga_a = self._map_pixel_to_vga(col_a)
                vga_b = self._map_pixel_to_vga(col_b)

                # Assignment 1: col_a is bg, col_b is fg
                mask_1 = (np.any(cell_frame != col_a, axis=-1)).astype(np.uint8)
                code_1, ch_1, err_1, _ = self.glyph_table.match_mask(mask_1)

                # Assignment 2: col_b is bg, col_a is fg
                mask_2 = (np.any(cell_frame != col_b, axis=-1)).astype(np.uint8)
                code_2, ch_2, err_2, _ = self.glyph_table.match_mask(mask_2)

                cand_err = min(err_1, err_2)
                if cand_err < best_err:
                    best_err = cand_err
                    off_idx = idx
                    if err_1 <= err_2:
                        recovered_char = ch_1
                        recovered_bg = f"{vga_a:x}"
                        recovered_fg = f"{vga_b:x}"
                    else:
                        recovered_char = ch_2
                        recovered_bg = f"{vga_b:x}"
                        recovered_fg = f"{vga_a:x}"
            else:
                # > 2 colors: use top 2
                pass

        obs = CursorObservation(
            row=cur_r,
            col=cur_c,
            visible=True,
            shape=best_shape,
            underlying_char=recovered_char,
            underlying_fg=recovered_fg,
            underlying_bg=recovered_bg,
            is_blinking=True,
            confidence=1.0,
        )

        return obs, rgb_frames[off_idx]
