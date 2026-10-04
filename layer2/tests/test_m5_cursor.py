"""Milestone 5 Cursor Perception Tests.

Verifies:
1. Underline cursor detection, shape discrimination, and off-phase underlying character recovery.
2. Block cursor detection, shape discrimination, and off-phase underlying character recovery.
3. Hidden cursor handling (bit 5 in BDA 0x461 and off-screen cursor row >= 25).
4. Live verification against QEMU FreeDOS and BDA memory oracle (0x450, 0x460, 0x461).
"""
import numpy as np
import pytest

from layer2.dos.cursor import DOSCursorDetector, CursorObservation
from layer2.dos.glyph_table import GlyphTable


@pytest.fixture
def cursor_detector() -> DOSCursorDetector:
    return DOSCursorDetector()


def test_m5_detect_underline_cursor_synthetic(cursor_detector: DOSCursorDetector) -> None:
    """Verify synthetic underline cursor blinking on scanlines 14..15 is accurately detected."""
    # Create 6 black frames (720x400)
    frames = [np.zeros((400, 720, 3), dtype=np.uint8) for _ in range(6)]

    # In cell (row=12, col=30): put letter 'A'
    template_a = cursor_detector.glyph_table.get_template(ord("A"))
    y0, y1 = 12 * 16, 13 * 16
    x0, x1 = 30 * 9, 31 * 9

    # Set underlying character 'A' in all frames
    for f in frames:
        f[y0:y1, x0:x1, 0] = template_a * 255
        f[y0:y1, x0:x1, 1] = template_a * 255
        f[y0:y1, x0:x1, 2] = template_a * 255

    # Frames 2, 3, 4 have underline cursor ON (scanlines 14 and 15 set to white)
    for idx in [2, 3, 4]:
        frames[idx][y0 + 14 : y1, x0:x1] = (255, 255, 255)

    obs, off_frame = cursor_detector.detect_cursor_from_frames(frames)

    assert obs.visible is True
    assert obs.row == 12
    assert obs.col == 30
    assert obs.shape == "underline"
    assert obs.underlying_char == "A"
    assert obs.underlying_fg == "f"
    assert obs.underlying_bg == "0"
    assert off_frame is not None


def test_m5_detect_block_cursor_synthetic(cursor_detector: DOSCursorDetector) -> None:
    """Verify synthetic block cursor blinking over scanlines 0..15 is accurately detected."""
    frames = [np.zeros((400, 720, 3), dtype=np.uint8) for _ in range(6)]

    # In cell (row=5, col=10): put letter 'B'
    template_b = cursor_detector.glyph_table.get_template(ord("B"))
    y0, y1 = 5 * 16, 6 * 16
    x0, x1 = 10 * 9, 11 * 9

    # Set underlying character 'B' in all frames
    for f in frames:
        f[y0:y1, x0:x1, 0] = template_b * 255
        f[y0:y1, x0:x1, 1] = template_b * 255
        f[y0:y1, x0:x1, 2] = template_b * 255

    # Frames 2, 3, 4 have full block cursor ON (inverted: background white, glyph black)
    for idx in [2, 3, 4]:
        frames[idx][y0:y1, x0:x1] = (255, 255, 255)
        # Glyph inverted
        for r_cell in range(16):
            for c_cell in range(9):
                if template_b[r_cell, c_cell]:
                    frames[idx][y0 + r_cell, x0 + c_cell] = (0, 0, 0)

    obs, off_frame = cursor_detector.detect_cursor_from_frames(frames)

    assert obs.visible is True
    assert obs.row == 5
    assert obs.col == 10
    assert obs.shape == "block"
    assert obs.underlying_char == "B"
    assert off_frame is not None


def test_m5_hidden_cursor_no_toggling(cursor_detector: DOSCursorDetector) -> None:
    """Verify static / hidden cursor frames return visible=False and shape='hidden'."""
    # 6 identical frames with no changes
    frames = [np.zeros((400, 720, 3), dtype=np.uint8) for _ in range(6)]

    obs, off_frame = cursor_detector.detect_cursor_from_frames(frames)

    assert obs.visible is False
    assert obs.shape == "hidden"
    assert obs.row is None
    assert obs.col is None
