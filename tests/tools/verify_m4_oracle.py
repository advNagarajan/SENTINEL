"""M4 Screen Reader Live Verification against QEMU FreeDOS and QMP pmemsave memory oracle."""
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional
import numpy as np
from PIL import Image

import asyncvnc

sys.path.append("/home/sivakuhan/Projects/FreeDos")
from qmp_client import QMPClient
from layer2.dos.constants import CP437_TO_UNICODE
from layer2.dos.screen_reader import DOSScreenReader


def get_qmp_vram_and_cursor() -> tuple[bytes, Optional[tuple[int, int]], dict[str, Any]]:
    """Read BDA video state (page offset 0x44E, cursor 0x450, cursor shape 0x460/0x461, active page 0x462)
    and dump the exact active video page VRAM via QMP pmemsave."""
    qmp = QMPClient()
    qmp.connect()

    bda_path = "/tmp/m4_bda.bin"
    vram_path = "/tmp/m4_vram.bin"

    # Dump 80 bytes of BDA starting at 0x440
    qmp.execute("pmemsave", {"val": 0x440, "size": 80, "filename": bda_path})
    with open(bda_path, "rb") as f:
        bda_data = f.read()

    # Active page offset from BDA 0x44E (offset 0x0E from 0x440)
    page_offset = bda_data[0x0E] | (bda_data[0x0F] << 8)
    active_page = bda_data[0x22]
    vram_base = 0xB8000 + page_offset

    # Cursor position from BDA 0x450 (offset 0x10 from 0x440)
    cursor_col = bda_data[0x10]
    cursor_row = bda_data[0x11]

    # Cursor scanlines from BDA 0x460/0x461 (offset 0x20/0x21 from 0x440)
    cursor_end = bda_data[0x20]
    cursor_start = bda_data[0x21]

    # Dump active page VRAM (4000 bytes for 80x25 text mode)
    qmp.execute("pmemsave", {"val": vram_base, "size": 4000, "filename": vram_path})
    qmp.close()

    with open(vram_path, "rb") as f:
        vram_data = f.read()

    # Check for hidden cursor:
    # 1. Cursor start scanline bit 5 set (0x20)
    # 2. Cursor parked off-screen (row >= 25 or col >= 80)
    is_hidden = bool(cursor_start & 0x20) or (cursor_row >= 25) or (cursor_col >= 80)
    cursor = None if is_hidden else (cursor_row, cursor_col)

    bda_info = {
        "page_offset": page_offset,
        "active_page": active_page,
        "vram_base": vram_base,
        "cursor_col": cursor_col,
        "cursor_row": cursor_row,
        "cursor_start": cursor_start,
        "cursor_end": cursor_end,
        "is_hidden": is_hidden,
    }

    return vram_data, cursor, bda_info


def compare_screen(
    reader_result: Any,
    vram_data: bytes,
    cursor: Optional[tuple[int, int]],
    blink_enabled: bool = False,
) -> dict[str, Any]:
    """Compare reader output against QMP VRAM dump with rigorous metrics and equivalence classes.
    
    Metric Definitions:
    - Evaluated Cells: Non-cursor cells where text is observable (total cells minus invisible text).
    - Invisible Text Cells: Cells where VRAM foreground equals background (fg == bg). These cells
      have zero contrast, are physically unobservable by any optical sensor, and are classified separately.
    - Exact Matches: Cells where VRAM unicode character byte equals the reader's character code.
    - Equivalence-Only Matches: Cells where VRAM byte was not identical (e.g. 0x00, 0xFF, solid 0xDB)
      but canonically resolved to ' ', or complement blocks (0xDC vs 0xDF).
    - Observable Foreground Match: Evaluated ONLY on cells with visible glyph pixels (non-blank cells).
      Blank cells have no visible foreground and are excluded.
    - Observable Background Match: Evaluated across all cells.
      When blink_enabled is True, attribute bit 7 is masked (blink bit: (attr >> 4) & 0x07).
      When blink_enabled is False, bit 7 is the 4th background bit ((attr >> 4) & 0x0F, colors 0-15).
      For solid 0xDB blocks and complement pairs, the observable background color is vram_fg.
    """
    rows = 25
    cols = 80
    cursor_pos = cursor

    total_cells = 0
    invisible_cells = 0
    exact_char_matches = 0
    equiv_only_matches = 0

    observable_fg_cells = 0
    fg_matches = 0

    observable_bg_cells = 0
    bg_matches = 0

    mismatches = []

    for r in range(rows):
        for c in range(cols):
            # Mask cursor cell from character and color comparisons (if not hidden)
            if cursor_pos is not None and (r, c) == cursor_pos:
                continue

            total_cells += 1
            offset = (r * cols + c) * 2
            vram_char_byte = vram_data[offset]
            vram_attr_byte = vram_data[offset + 1]

            # In standard VGA text attribute:
            # low nibble (bits 0-3): fg (0-15)
            # high nibble: if blink on, bits 4-6 are bg (0-7); if blink off, bits 4-7 are bg (0-15)
            vram_fg = vram_attr_byte & 0x0F
            if blink_enabled:
                vram_bg = (vram_attr_byte >> 4) & 0x07
            else:
                vram_bg = (vram_attr_byte >> 4) & 0x0F

            read_char = reader_result.raw_grid[r][c]
            read_fg = int(reader_result.color_grid.fg[r][c], 16)
            read_bg = int(reader_result.color_grid.bg[r][c], 16)

            # Check for invisible text (vram_fg == vram_bg)
            if vram_fg == vram_bg:
                invisible_cells += 1
                # When foreground equals background, the glyph has zero contrast and is
                # physically unobservable. The reader correctly decodes the cell as space
                # with bg = vram_bg. Classify as unobservable and verify observable background.
                observable_bg_cells += 1
                if read_bg == vram_bg:
                    bg_matches += 1
                continue

            vram_unicode = CP437_TO_UNICODE[vram_char_byte]

            # 1. Character comparison under equivalence
            is_exact = (read_char == vram_unicode)
            is_equiv_only = False
            is_complement = False

            # Equivalence Rule 1: Blank cells (0x00, 0xFF vs 0x20 space)
            if not is_exact and vram_char_byte in (0x00, 0xFF) and read_char == " ":
                is_equiv_only = True

            # Equivalence Rule 2: Solid single-color block (0xDB with any fg/bg reads as space ' ' with bg = vram_fg)
            elif not is_exact and vram_char_byte == 0xDB and read_char == " ":
                is_equiv_only = True

            # Equivalence Rule 3: Complement half-blocks (0xDC vs 0xDF)
            elif not is_exact and vram_char_byte == 0xDF and read_char == "▄" and read_fg == vram_bg and read_bg == vram_fg:
                is_equiv_only = True
                is_complement = True
            elif not is_exact and vram_char_byte == 0xDC and read_char == "▀" and read_fg == vram_bg and read_bg == vram_fg:
                is_equiv_only = True
                is_complement = True

            # Equivalence Rule 4: Complement side-blocks (0xDD vs 0xDE)
            elif not is_exact and vram_char_byte == 0xDE and read_char == "▌" and read_fg == vram_bg and read_bg == vram_fg:
                is_equiv_only = True
                is_complement = True
            elif not is_exact and vram_char_byte == 0xDD and read_char == "▐" and read_fg == vram_bg and read_bg == vram_fg:
                is_equiv_only = True
                is_complement = True

            if is_exact:
                exact_char_matches += 1
            elif is_equiv_only:
                equiv_only_matches += 1
            else:
                mismatches.append({
                    "row": r,
                    "col": c,
                    "vram_code": hex(vram_char_byte),
                    "vram_char": repr(vram_unicode),
                    "read_char": repr(read_char),
                    "vram_fg": vram_fg,
                    "read_fg": read_fg,
                    "vram_bg": vram_bg,
                    "read_bg": read_bg,
                })

            # 2. Background color comparison
            observable_bg_cells += 1
            if is_complement or vram_char_byte == 0xDB:
                expected_bg = vram_fg
            else:
                expected_bg = vram_bg

            if read_bg == expected_bg:
                bg_matches += 1

            # 3. Foreground color comparison (ONLY on observable non-blank cells)
            # On a space cell (' '), foreground is invisible and unobservable
            if read_char != " ":
                observable_fg_cells += 1
                expected_fg = vram_bg if is_complement else vram_fg
                if read_fg == expected_fg:
                    fg_matches += 1

    evaluated_cells = total_cells - invisible_cells
    char_matches = exact_char_matches + equiv_only_matches
    char_accuracy = (char_matches / evaluated_cells) * 100.0 if evaluated_cells else 100.0
    bg_accuracy = (bg_matches / observable_bg_cells) * 100.0 if observable_bg_cells else 100.0
    fg_accuracy = (fg_matches / observable_fg_cells) * 100.0 if observable_fg_cells else 100.0

    return {
        "total_cells": total_cells,
        "invisible_cells": invisible_cells,
        "evaluated_cells": evaluated_cells,
        "exact_char_matches": exact_char_matches,
        "equiv_only_matches": equiv_only_matches,
        "char_accuracy": char_accuracy,
        "observable_bg_cells": observable_bg_cells,
        "bg_matches": bg_matches,
        "bg_accuracy": bg_accuracy,
        "observable_fg_cells": observable_fg_cells,
        "fg_matches": fg_matches,
        "fg_accuracy": fg_accuracy,
        "cursor": cursor_pos,
        "mismatches": mismatches,
    }


async def run_scenario(
    client: asyncvnc.Client,
    command_sequence: list[str],
    scenario_name: str,
    reader: DOSScreenReader,
) -> dict[str, Any]:
    """Execute command sequence, capture VNC frame and VRAM back-to-back, and verify."""
    print(f"\n=======================================================")
    print(f"Running Scenario: {scenario_name}")
    print(f"=======================================================")

    for cmd in command_sequence:
        if cmd.startswith("WAIT:"):
            sec = float(cmd.split(":")[1])
            await asyncio.sleep(sec)
        elif cmd == "ENTER":
            client.keyboard.press("Return")
            await client.drain()
            await asyncio.sleep(0.3)
        elif cmd == "ESCAPE":
            client.keyboard.press("Escape")
            await client.drain()
            await asyncio.sleep(0.3)
        elif cmd.startswith("COMBO:"):
            combo = cmd.split(":")[1]
            parts = combo.split("+")
            mods = parts[:-1]
            base = parts[-1]
            if "ALT" in mods:
                with client.keyboard.hold("Alt_L"):
                    client.keyboard.press(base.lower())
            elif "CTRL" in mods:
                with client.keyboard.hold("Control_L"):
                    client.keyboard.press(base.lower())
            await client.drain()
            await asyncio.sleep(0.5)
        else:
            # Literal string typing
            for ch in cmd:
                if ch == ":":
                    with client.keyboard.hold("Shift_L"):
                        client.keyboard.press(":")
                elif ch == "\\":
                    client.keyboard.press("\\")
                else:
                    client.keyboard.press(ch)
                await client.drain()
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)

    # Wait for screen to settle
    await asyncio.sleep(1.0)

    # Capture VNC frame and QMP memory dump back to back
    frame = await client.screenshot()
    vram_data, cursor, bda_info = get_qmp_vram_and_cursor()

    # Save PNG artifact
    os.makedirs("logs", exist_ok=True)
    png_path = f"logs/m4_scenario_{scenario_name.lower().replace(' ', '_')}.png"
    Image.fromarray(frame).save(png_path)
    print(f"Saved screenshot: {png_path}")

    # Copy to brain artifact directory for display
    brain_dir = Path("/home/sivakuhan/.gemini/antigravity-ide/brain/5fd96f92-58f1-4834-a051-b63d7695243b")
    if brain_dir.exists():
        Image.fromarray(frame).save(brain_dir / f"m4_scenario_{scenario_name.lower().replace(' ', '_')}.png")

    # Run screen reader
    result = reader.read_frame(frame)

    # Compare
    metrics = compare_screen(result, vram_data, cursor)
    print(f"Total Evaluated Cells (excluding cursor): {metrics['evaluated_cells']} (Invisible: {metrics['invisible_cells']})")
    print(f"Character Accuracy (Equivalence):       {metrics['char_accuracy']:.2f}% ({metrics['exact_char_matches'] + metrics['equiv_only_matches']}/{metrics['evaluated_cells']})")
    print(f"  - Exact CP437 Character Matches:      {metrics['exact_char_matches']}")
    print(f"  - Equivalence-Only Matches:           {metrics['equiv_only_matches']}")
    print(f"Background Colour Match (Observable):    {metrics['bg_accuracy']:.2f}% ({metrics['bg_matches']}/{metrics['observable_bg_cells']})")
    print(f"Foreground Colour Match (Observable):    {metrics['fg_accuracy']:.2f}% ({metrics['fg_matches']}/{metrics['observable_fg_cells']})")
    cursor_str = f"Row {cursor[0]}, Col {cursor[1]}" if cursor else "Hidden (off-screen / bit 5)"
    print(f"BIOS Cursor Status:                     {cursor_str}")
    print(f"Active Video Page:                      Page {bda_info['active_page']} (VRAM 0x{bda_info['vram_base']:05X})")

    if metrics["mismatches"]:
        print(f"Mismatches count: {len(metrics['mismatches'])}")
        print(f"First 3 mismatches: {metrics['mismatches'][:3]}")
    else:
        print("ALL CELLS MATCHED 100%!")

    return metrics


async def main() -> None:
    reader = DOSScreenReader()

    async with asyncvnc.connect("127.0.0.1", 5900) as client:
        # Clear prompt with cls
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

        results: dict[str, dict[str, Any]] = {}

        # Scenario 1: Clean Shell prompt
        results["Boot_Prompt"] = await run_scenario(client, [], "Boot_Prompt", reader)
        assert results["Boot_Prompt"]["char_accuracy"] >= 99.9

        # Scenario 2: DIR output
        results["DIR_Output"] = await run_scenario(client, ["dir c:\\", "ENTER", "WAIT:1.0"], "DIR_Output", reader)
        assert results["DIR_Output"]["char_accuracy"] >= 99.9

        # Scenario 3: MEM output
        results["MEM_Output"] = await run_scenario(client, ["mem", "ENTER", "WAIT:1.0"], "MEM_Output", reader)
        assert results["MEM_Output"]["char_accuracy"] >= 99.9

        # Scenario 4: FreeDOS HELP Output (Box drawing, scrollbars, shaded blocks)
        results["HELP_Output"] = await run_scenario(client, ["help", "ENTER", "WAIT:1.5"], "HELP_Output", reader)
        assert results["HELP_Output"]["char_accuracy"] >= 99.9
        # Exit HELP with Alt+X
        with client.keyboard.hold("Alt_L"):
            client.keyboard.press("x")
        await client.drain()
        await asyncio.sleep(0.5)

        # Scenario 5: FreeDOS EDIT Main Screen (Blue background, double box drawing, menus, clock)
        results["EDIT_Main"] = await run_scenario(client, ["edit", "ENTER", "WAIT:1.5"], "EDIT_Main", reader)
        assert results["EDIT_Main"]["char_accuracy"] >= 99.9

        # Scenario 6: FreeDOS EDIT File Menu (Dropdown menu, single box lines, selection bar, shadow)
        results["EDIT_Menu"] = await run_scenario(client, ["COMBO:ALT+F", "WAIT:1.0"], "EDIT_Menu", reader)
        assert results["EDIT_Menu"]["char_accuracy"] >= 99.9

        # Exit EDIT cleanly
        client.keyboard.press("Escape")
        await client.drain()
        await asyncio.sleep(0.3)
        with client.keyboard.hold("Alt_L"):
            client.keyboard.press("x")
        await client.drain()
        await asyncio.sleep(0.5)

        print("\n" + "=" * 90)
        print("M4 ACCEPTANCE SUMMARY: ORACLE COMPARISON ACROSS 6 REAL DOS SCENARIOS")
        print("=" * 90)
        print("| Scenario | Evaluated Cells | Invisible Text | Exact Matches | Equiv-Only | Char Accuracy | Observable FG Match | Observable BG Match |")
        print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for sc_name, m in results.items():
            tot = m["evaluated_cells"]
            inv = m["invisible_cells"]
            ex = m["exact_char_matches"]
            eq = m["equiv_only_matches"]
            ca = m["char_accuracy"]
            fg_str = f"{m['fg_accuracy']:.2f}% ({m['fg_matches']}/{m['observable_fg_cells']})"
            bg_str = f"{m['bg_accuracy']:.2f}% ({m['bg_matches']}/{m['observable_bg_cells']})"
            print(f"| {sc_name} | {tot} | {inv} | {ex} | {eq} | {ca:.2f}% | {fg_str} | {bg_str} |")
        print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
