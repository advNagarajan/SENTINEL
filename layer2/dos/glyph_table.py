"""CP437 Glyph Table and vectorized template matcher for VGA 9x16 text cells."""
from pathlib import Path
from typing import Optional
import numpy as np

from layer2.dos.constants import CP437_TO_UNICODE


class GlyphTable:
    """Manages 256 calibrated CP437 9x16 glyph templates and performs cell matching."""

    DEFAULT_DATA_PATH = Path(__file__).parent / "data" / "cp437_vga_9x16.npz"

    def __init__(self, data_path: Optional[Path] = None) -> None:
        self.data_path = data_path or self.DEFAULT_DATA_PATH
        if not self.data_path.exists():
            raise FileNotFoundError(f"Calibrated glyph table not found at {self.data_path}")

        loaded = np.load(self.data_path)
        self.templates: np.ndarray = loaded["templates"]  # Shape (256, 16, 9), uint8

        if self.templates.shape != (256, 16, 9):
            raise ValueError(f"Invalid template shape: {self.templates.shape}. Expected (256, 16, 9).")

        # Derive blank glyph codes directly from calibration data
        # Any code where all pixels are background (0 foreground pixels)
        self.blank_codes: list[int] = [
            code for code in range(256)
            if int(np.sum(self.templates[code])) == 0
        ]

        # Derive full block codes (all pixels = 1)
        self.full_block_codes: list[int] = [
            code for code in range(256)
            if int(np.sum(self.templates[code])) == (16 * 9)
        ]

    def get_template(self, code: int) -> np.ndarray:
        """Return 1-bit boolean/uint8 mask (16, 9) for CP437 code (0..255)."""
        if not (0 <= code < 256):
            raise ValueError(f"Invalid character code: {code}. Must be 0..255.")
        return self.templates[code]

    def match_mask(self, cell_mask: np.ndarray) -> tuple[int, str, int, float]:
        """Match a (16, 9) 1-bit cell mask against the 256 calibrated templates.
        
        Returns:
            (char_code, unicode_char, min_diff_pixels, confidence)
        """
        pixel_count = int(np.sum(cell_mask))

        # 1. Deterministic resolution for blank cells (0 foreground pixels)
        # Any cell with no foreground pixels canonically maps to ASCII space (0x20)
        if pixel_count == 0:
            return 0x20, " ", 0, 1.0

        # 2. Deterministic resolution for full block (144 foreground pixels)
        if pixel_count == 16 * 9:
            return 0xDB, "█", 0, 1.0

        # 3. Vectorized Hamming distance comparison across all 256 templates
        # Computes differing pixels between cell_mask and each template
        diffs = np.sum(self.templates != cell_mask, axis=(1, 2))
        best_code = int(np.argmin(diffs))
        min_err = int(diffs[best_code])

        confidence = max(0.0, 1.0 - (min_err / (16.0 * 9.0)))
        unicode_char = CP437_TO_UNICODE[best_code]

        return best_code, unicode_char, min_err, confidence
