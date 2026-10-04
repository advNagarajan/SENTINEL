"""Live Oracle Verification for Milestone 5 (M5): Cursor Position, Shape, and Visibility.

Verifies:
1. Normal underline cursor at DOS shell prompt (BDA 0x450, 0x460=7, 0x461=6).
2. Block cursor in SETCUR.COM over letter 'Q' (BDA 0x450=(20, 10), 0x460=7, 0x461=0).
3. Hidden cursor via BDA start scanline bit 5 (0x20, CX=2000h).
4. Hidden cursor via off-screen position (row 25).
5. 100.00% character and color accuracy across all 2,000 cells (including cell under cursor).
"""
import asyncio
import os
import sys
from pathlib import Path
from typing import Any
import numpy as np
from PIL import Image

import asyncvnc

sys.path.append("/home/sivakuhan/Projects/FreeDos")
sys.path.append(str(Path(__file__).resolve().parents[2]))
from qmp_client import QMPClient
from layer2.dos.cursor import DOSCursorDetector, CursorObservation
from layer2.dos.screen_reader import DOSScreenReader
from layer2.dos.constants import CP437_TO_UNICODE
from tests.tools.verify_m4_oracle import get_qmp_vram_and_cursor, compare_screen


async def sample_vnc_frames(
    client: asyncvnc.Client, count: int = 7, interval: float = 0.09
) -> list[np.ndarray]:
    """Sample multiple video frames across the QEMU cursor blink cycle (267ms toggle / 533ms cycle)."""
    frames = []
    for _ in range(count):
        f = await client.screenshot()
        frames.append(f)
        await asyncio.sleep(interval)
    return frames


async def main() -> None:
    detector = DOSCursorDetector()
    reader = DOSScreenReader()

    print("=" * 80)
    print("MILESTONE 5 (M5) LIVE ORACLE VERIFICATION: CURSOR POSITION & SHAPE")
    print("=" * 80)

    async with asyncvnc.connect("127.0.0.1", 5900) as client:
        # Clear prompt
        with client.keyboard.hold("Control_L"):
            client.keyboard.press("c")
        await client.drain()
        await asyncio.sleep(0.3)
        for ch in "cls":
            client.keyboard.press(ch)
            await client.drain()
            await asyncio.sleep(0.02)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # Scenario 1: Normal Underline Cursor at Shell Prompt
        # -------------------------------------------------------------
        print("\n--- Scenario 1: Normal Underline Cursor (Shell Prompt) ---")
        frames = await sample_vnc_frames(client)
        vram, cursor_pos, bda = get_qmp_vram_and_cursor()

        obs, off_frame = detector.detect_cursor_from_frames(frames)
        print(f"Perceived Cursor: pos=({obs.row}, {obs.col}), visible={obs.visible}, shape={obs.shape}")
        print(f"BDA Ground Truth: pos=({bda['cursor_row']}, {bda['cursor_col']}), start=0x{bda['cursor_start']:02X}, end=0x{bda['cursor_end']:02X}")

        assert obs.visible is True
        assert obs.row == bda["cursor_row"]
        assert obs.col == bda["cursor_col"]
        assert obs.shape == "underline"
        print("SCENARIO 1 PASSED: Underline cursor position and shape match BDA exactly!")

        # -------------------------------------------------------------
        # Scenario 2: Live Block Cursor over character 'Q'
        # -------------------------------------------------------------
        print("\n--- Scenario 2: Block Cursor (SETCUR.COM over letter 'Q') ---")
        client.keyboard.write("a")
        with client.keyboard.hold("Shift_L"):
            client.keyboard.press(":")
        client.keyboard.write("\\setcur.com")
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.8)

        client.keyboard.press("b")
        await client.drain()
        await asyncio.sleep(0.3)

        frames = await sample_vnc_frames(client)
        vram, cursor_pos, bda = get_qmp_vram_and_cursor()

        obs, off_frame = detector.detect_cursor_from_frames(frames)
        print(f"Perceived Cursor: pos=({obs.row}, {obs.col}), visible={obs.visible}, shape={obs.shape}, underlying={repr(obs.underlying_char)}")
        print(f"BDA Ground Truth: pos=({bda['cursor_row']}, {bda['cursor_col']}), start=0x{bda['cursor_start']:02X}, end=0x{bda['cursor_end']:02X}")

        assert obs.visible is True
        assert obs.row == 10 and obs.col == 20
        assert obs.shape == "block"
        assert obs.underlying_char == "Q"
        assert obs.underlying_fg == "f" and obs.underlying_bg == "1"
        print("SCENARIO 2 PASSED: Block cursor position, shape, and underlying 'Q' recovered with 100% accuracy!")

        # -------------------------------------------------------------
        # Scenario 3: Hidden Cursor via BDA Bit 5
        # -------------------------------------------------------------
        print("\n--- Scenario 3: Hidden Cursor via Bit 5 (CX=2000h) ---")
        client.keyboard.press("h")
        await client.drain()
        await asyncio.sleep(0.3)

        frames = await sample_vnc_frames(client)
        vram, cursor_pos, bda = get_qmp_vram_and_cursor()

        obs, off_frame = detector.detect_cursor_from_frames(frames)
        print(f"Perceived Cursor: visible={obs.visible}, shape={obs.shape}")
        print(f"BDA Ground Truth: start=0x{bda['cursor_start']:02X} (bit 5={bool(bda['cursor_start'] & 0x20)})")

        assert obs.visible is False
        assert obs.shape == "hidden"
        assert bda["is_hidden"] is True
        print("SCENARIO 3 PASSED: Cursor hidden via bit 5 correctly recognized!")

        # -------------------------------------------------------------
        # Scenario 4: Hidden Cursor via Off-Screen Position (Row 25)
        # -------------------------------------------------------------
        print("\n--- Scenario 4: Hidden Cursor via Off-Screen Position (Row 25) ---")
        client.keyboard.press("o")
        await client.drain()
        await asyncio.sleep(0.3)

        frames = await sample_vnc_frames(client)
        vram, cursor_pos, bda = get_qmp_vram_and_cursor()

        obs, off_frame = detector.detect_cursor_from_frames(frames)
        print(f"Perceived Cursor: visible={obs.visible}, shape={obs.shape}")
        print(f"BDA Ground Truth: pos=({bda['cursor_row']}, {bda['cursor_col']}) (off-screen={bda['cursor_row'] >= 25})")

        assert obs.visible is False
        assert obs.shape == "hidden"
        assert bda["is_hidden"] is True
        print("SCENARIO 4 PASSED: Cursor parked off-screen correctly recognized as hidden!")

        # Exit SETCUR.COM
        client.keyboard.press("Escape")
        await client.drain()
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # Scenario 5: Full 2,000-Cell Screen Reading Coverage
        # -------------------------------------------------------------
        print("\n--- Scenario 5: Full 2,000-Cell Reading Coverage (Including Cursor Cell) ---")
        # Go to shell prompt with DIR output
        for ch in "dir c:\\":
            client.keyboard.press(ch)
            await client.drain()
            await asyncio.sleep(0.02)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(1.0)

        frames = await sample_vnc_frames(client)
        vram, cursor_pos, bda = get_qmp_vram_and_cursor()
        obs, off_frame = detector.detect_cursor_from_frames(frames)

        # Read the entire screen from the off-phase frame (cursor cell is unoccluded!)
        res = reader.read_frame(off_frame)

        # In compare_screen, pass cursor=None so ALL 2,000 cells are evaluated!
        metrics = compare_screen(res, vram, cursor=None)

        print(f"Total Evaluated Cells:    {metrics['evaluated_cells']} / 2000 (100% cell coverage!)")
        print(f"Character Accuracy:       {metrics['char_accuracy']:.2f}% ({metrics['exact_char_matches']}/{metrics['evaluated_cells']})")
        print(f"Observable FG Accuracy:   {metrics['fg_accuracy']:.2f}% ({metrics['fg_matches']}/{metrics['observable_fg_cells']})")
        print(f"Observable BG Accuracy:   {metrics['bg_accuracy']:.2f}% ({metrics['bg_matches']}/{metrics['observable_bg_cells']})")

        assert metrics["evaluated_cells"] == 2000
        assert metrics["char_accuracy"] == 100.0
        assert metrics["fg_accuracy"] == 100.0
        assert metrics["bg_accuracy"] == 100.0
        print("SCENARIO 5 PASSED: 100.00% Character and Color Accuracy across ALL 2,000 cells!")

        print("\n" + "=" * 80)
        print("ALL 5 M5 CURSOR SCENARIOS PASSED WITH 100% ORACLE CONFORMANCE!")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
