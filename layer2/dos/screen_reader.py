"""Stage 2a Screen Reader: Translates raw VGA framebuffer pixels into 80x25 text grid and ColorGrid."""
from dataclasses import dataclass, field
from typing import Any, Optional
import numpy as np

from layer2.dos.constants import CP437_TO_UNICODE, VGA_PALETTE_RGB
from layer2.dos.glyph_table import GlyphTable


@dataclass
class ColorGrid:
    """Per-cell rendered VGA text color attributes recovered from pixels.
    
    Attributes:
        fg: Rows of hex strings '0'-'f' representing foreground palette indices.
            For blank cells (0 foreground pixels), fg equals bg by definition.
        bg: Rows of hex strings '0'-'f' representing background palette indices.
        blink: Optional rows of binary strings '0'/'1' populated when cell toggling
               is observed across multiple frames.
    """
    fg: list[str]
    bg: list[str]
    blink: Optional[list[str]] = None


@dataclass
class ScreenReaderResult:
    """Result of screen reader perception on a single framebuffer frame."""
    raw_grid: list[str]
    color_grid: ColorGrid
    confidence_score: float
    screen_size: dict[str, int]
    dominant_bg: int
    cell_errors: np.ndarray  # Shape (rows, cols), differing pixels per cell
    is_graphics: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class DOSScreenReader:
    """Decodes VGA text-mode video frames into canonical character grids and colour matrices."""

    def __init__(self, glyph_table: Optional[GlyphTable] = None) -> None:
        self.glyph_table = glyph_table or GlyphTable()
        self.palette = np.array(VGA_PALETTE_RGB, dtype=np.int32)  # (16, 3)

        # Complement pairs derived from calibration data (exact photographic inverses)
        self.complement_pairs: list[tuple[int, int]] = [
            (0x00, 0xDB),  # NUL <-> Full Block
            (0x20, 0xDB),  # Space <-> Full Block
            (0xDC, 0xDF),  # Lower half block <-> Upper half block
            (0xDD, 0xDE),  # Left half block <-> Right half block
            (0xDB, 0xFF),  # Full Block <-> Non-breaking space
        ]

    def _map_pixel_to_vga(self, rgb: np.ndarray) -> int:
        """Map RGB pixel (3,) to nearest 0-15 VGA palette index via Euclidean distance."""
        diff = self.palette - rgb
        dist = np.sum(diff * diff, axis=1)
        return int(np.argmin(dist))

    def _detect_dominant_background(self, frame: np.ndarray, cell_h: int = 16, cell_w: int = 9) -> int:
        """Sample corner pixels of all cells to determine the screen's dominant background color."""
        rows = frame.shape[0] // cell_h
        cols = frame.shape[1] // cell_w
        # Sample top-left pixel (0, 0) of every cell
        sampled_corners = frame[0::cell_h, 0::cell_w][:rows, :cols].reshape(-1, 3)
        # Vectorized mapping to VGA palette
        # Broadcast difference: (N, 1, 3) - (1, 16, 3) -> (N, 16)
        diff = sampled_corners[:, np.newaxis, :] - self.palette[np.newaxis, :, :]
        dist = np.sum(diff * diff, axis=2)
        vga_indices = np.argmin(dist, axis=1)
        # Most frequent index
        counts = np.bincount(vga_indices, minlength=16)
        return int(np.argmax(counts))

    def read_frame(self, frame_rgba_or_rgb: np.ndarray) -> ScreenReaderResult:
        """Process a raw video frame array into text grid and ColorGrid.
        
        Supports 720x400 (80x25 with 9x16 cells). Unknown or graphics resolutions
        return low confidence rather than raising an unhandled exception.
        """
        # Ensure RGB shape (H, W, 3)
        if frame_rgba_or_rgb.ndim == 3 and frame_rgba_or_rgb.shape[2] == 4:
            frame = frame_rgba_or_rgb[:, :, :3]
        else:
            frame = frame_rgba_or_rgb

        h, w = frame.shape[:2]

        # Geometry validation
        if (w, h) == (720, 400):
            rows, cols = 25, 80
            cell_w, cell_h = 9, 16
        elif (w, h) == (640, 400):
            rows, cols = 25, 80
            cell_w, cell_h = 8, 16
        else:
            # Recoverable graphics / unknown geometry state
            blank_grid = [" " * 80 for _ in range(25)]
            color_grid = ColorGrid(fg=["0" * 80 for _ in range(25)], bg=["0" * 80 for _ in range(25)])
            return ScreenReaderResult(
                raw_grid=blank_grid,
                color_grid=color_grid,
                confidence_score=0.1,
                screen_size={"rows": 25, "cols": 80},
                dominant_bg=0,
                cell_errors=np.full((25, 80), 144, dtype=int),
                is_graphics=True,
                metadata={"reason": f"Unsupported or graphics frame geometry: {w}x{h}"},
            )

        dominant_bg = self._detect_dominant_background(frame, cell_h, cell_w)

        raw_grid: list[str] = []
        fg_rows: list[str] = []
        bg_rows: list[str] = []
        cell_errors = np.zeros((rows, cols), dtype=int)
        total_confidence = 0.0

        for r in range(rows):
            line_chars: list[str] = []
            line_fg: list[str] = []
            line_bg: list[str] = []
            y0 = r * cell_h
            y1 = y0 + cell_h

            for c in range(cols):
                x0 = c * cell_w
                x1 = x0 + cell_w
                cell = frame[y0:y1, x0:x1]  # (cell_h, cell_w, 3)

                pixels = cell.reshape(-1, 3)
                unique_colors, counts = np.unique(pixels, axis=0, return_counts=True)

                if len(unique_colors) == 1:
                    # Case A: Solid single-colour cell
                    # Deterministic rule: canonically resolves to Space (0x20) with fg == bg
                    vga_idx = self._map_pixel_to_vga(unique_colors[0])
                    hex_color = f"{vga_idx:x}"
                    line_chars.append(" ")
                    line_fg.append(hex_color)
                    line_bg.append(hex_color)
                    total_confidence += 1.0

                elif len(unique_colors) == 2:
                    # Case B: Standard 2-colour text cell
                    # Evaluate both candidate assignments to handle visual complement pairs
                    col_a = unique_colors[0]
                    col_b = unique_colors[1]
                    vga_a = self._map_pixel_to_vga(col_a)
                    vga_b = self._map_pixel_to_vga(col_b)

                    # Candidate 1: col_a is bg, col_b is fg
                    mask_1 = (np.any(cell != col_a, axis=-1)).astype(np.uint8)
                    code_1, ch_1, err_1, conf_1 = self.glyph_table.match_mask(mask_1)

                    # Candidate 2: col_b is bg, col_a is fg
                    mask_2 = (np.any(cell != col_b, axis=-1)).astype(np.uint8)
                    code_2, ch_2, err_2, conf_2 = self.glyph_table.match_mask(mask_2)

                    # Selection & Tie-breaking
                    if err_1 < err_2:
                        best_code, best_ch, best_err, best_conf = code_1, ch_1, err_1, conf_1
                        bg_idx, fg_idx = vga_a, vga_b
                    elif err_2 < err_1:
                        best_code, best_ch, best_err, best_conf = code_2, ch_2, err_2, conf_2
                        bg_idx, fg_idx = vga_b, vga_a
                    else:
                        # Tie: Perfect match or identical error (e.g. 0xDC vs 0xDF, 0xDD vs 0xDE)
                        # Fixed local tie-breaker by character code only: ensures cell decoding is 100%
                        # local, independent of other cells, and does not shift grid hash on screen changes
                        if code_1 <= code_2:
                            best_code, best_ch, best_err, best_conf = code_1, ch_1, err_1, conf_1
                            bg_idx, fg_idx = vga_a, vga_b
                        else:
                            best_code, best_ch, best_err, best_conf = code_2, ch_2, err_2, conf_2
                            bg_idx, fg_idx = vga_b, vga_a

                    line_chars.append(best_ch)
                    line_bg.append(f"{bg_idx:x}")
                    line_fg.append(f"{fg_idx:x}")
                    cell_errors[r, c] = best_err
                    total_confidence += best_conf

                else:
                    # Case C: > 2 colours (anti-aliasing, cursor border, or noise)
                    # Use top two most frequent colors
                    sorted_indices = np.argsort(-counts)
                    col_bg = unique_colors[sorted_indices[0]]
                    col_fg = unique_colors[sorted_indices[1]]
                    vga_bg = self._map_pixel_to_vga(col_bg)
                    vga_fg = self._map_pixel_to_vga(col_fg)

                    # Build 1-bit mask by assigning each pixel to nearer of the two top colors
                    diff_bg = np.sum((cell - col_bg) ** 2, axis=-1)
                    diff_fg = np.sum((cell - col_fg) ** 2, axis=-1)
                    mask = (diff_fg < diff_bg).astype(np.uint8)

                    code, ch, err, conf = self.glyph_table.match_mask(mask)
                    line_chars.append(ch)
                    line_bg.append(f"{vga_bg:x}")
                    line_fg.append(f"{vga_fg:x}")
                    cell_errors[r, c] = err
                    total_confidence += conf

            raw_grid.append("".join(line_chars))
            fg_rows.append("".join(line_fg))
            bg_rows.append("".join(line_bg))

        mean_confidence = total_confidence / (rows * cols)
        color_grid = ColorGrid(fg=fg_rows, bg=bg_rows)

        return ScreenReaderResult(
            raw_grid=raw_grid,
            color_grid=color_grid,
            confidence_score=mean_confidence,
            screen_size={"rows": rows, "cols": cols},
            dominant_bg=dominant_bg,
            cell_errors=cell_errors,
            is_graphics=False,
        )
