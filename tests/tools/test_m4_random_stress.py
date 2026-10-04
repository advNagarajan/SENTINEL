"""Automated Randomised VRAM Stress Test for Milestone 4 (M4).

Fills text VRAM with thousands of pseudo-random characters (0x00-0xFF) and attributes (0x00-0xFF)
over multiple seeds with high-intensity backgrounds and blink mode, verifying that:
1. All observable glyphs match exactly or through mathematical equivalence (100.00% accuracy).
2. Invisible text (fg == bg) is classified as unobservable and accounted for separately.
3. Observable foreground and background colors match ground truth VRAM 100.00%.
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
from layer2.dos.constants import CP437_TO_UNICODE
from layer2.dos.screen_reader import DOSScreenReader
from scripts.build_randvram import main as build_and_mount_randvram
from tests.tools.verify_m4_oracle import get_qmp_vram_and_cursor, compare_screen


async def run_random_stress_test(num_seeds: int = 5) -> dict[str, Any]:
    print("=" * 80)
    print(f"RUNNING M4 RANDOMISED VRAM STRESS TEST ({num_seeds} SEEDS)")
    print("=" * 80)

    # Ensure RANDVRAM.COM is built and mounted
    build_and_mount_randvram()

    reader = DOSScreenReader()

    async with asyncvnc.connect("127.0.0.1", 5900) as client:
        # Clear prompt
        with client.keyboard.hold("Control_L"):
            client.keyboard.press("c")
        await client.drain()
        await asyncio.sleep(0.3)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.3)

        # Launch A:\RANDVRAM.COM
        client.keyboard.write("a")
        with client.keyboard.hold("Shift_L"):
            client.keyboard.press(":")
        client.keyboard.write("\\randvram.com")
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(1.0)

        # Toggle to High-Intensity Background mode (blink OFF) to test bright backgrounds 8-15
        client.keyboard.press("b")
        await client.drain()
        await asyncio.sleep(0.4)

        cumulative_eval = 0
        cumulative_exact = 0
        cumulative_equiv = 0
        cumulative_invisible = 0
        cumulative_fg_matches = 0
        cumulative_fg_cells = 0
        cumulative_bg_matches = 0
        cumulative_bg_cells = 0

        for seed_idx in range(num_seeds):
            if seed_idx > 0:
                client.keyboard.press("space")
                await client.drain()
                await asyncio.sleep(0.4)

            frame = await client.screenshot()
            vram_data, cursor, bda_info = get_qmp_vram_and_cursor()

            # Save sample screenshot
            os.makedirs("logs", exist_ok=True)
            if seed_idx == 0:
                Image.fromarray(frame).save("logs/m4_random_stress_sample.png")
                brain_dir = Path("/home/sivakuhan/.gemini/antigravity-ide/brain/5fd96f92-58f1-4834-a051-b63d7695243b")
                if brain_dir.exists():
                    Image.fromarray(frame).save(brain_dir / "m4_random_stress_sample.png")

            result = reader.read_frame(frame)
            metrics = compare_screen(result, vram_data, cursor, blink_enabled=False)

            assert metrics["char_accuracy"] == 100.0, f"Mismatches found in seed {seed_idx+1}: {metrics['mismatches']}"
            assert metrics["fg_accuracy"] == 100.0
            assert metrics["bg_accuracy"] == 100.0

            cumulative_eval += metrics["evaluated_cells"]
            cumulative_exact += metrics["exact_char_matches"]
            cumulative_equiv += metrics["equiv_only_matches"]
            cumulative_invisible += metrics["invisible_cells"]
            cumulative_fg_matches += metrics["fg_matches"]
            cumulative_fg_cells += metrics["observable_fg_cells"]
            cumulative_bg_matches += metrics["bg_matches"]
            cumulative_bg_cells += metrics["observable_bg_cells"]

            print(f"Seed {seed_idx+1:2d}: Evaluated={metrics['evaluated_cells']}, "
                  f"Invisible={metrics['invisible_cells']}, Exact={metrics['exact_char_matches']}, "
                  f"Equiv={metrics['equiv_only_matches']}, CharAcc={metrics['char_accuracy']:.2f}%, "
                  f"FGAcc={metrics['fg_accuracy']:.2f}%, BGAcc={metrics['bg_accuracy']:.2f}%")

        # Exit RANDVRAM cleanly
        client.keyboard.press("Escape")
        await client.drain()
        await asyncio.sleep(0.5)

        total_tested = cumulative_eval + cumulative_invisible
        summary = {
            "num_seeds": num_seeds,
            "total_cells_generated": total_tested,
            "evaluated_cells": cumulative_eval,
            "invisible_cells": cumulative_invisible,
            "exact_matches": cumulative_exact,
            "equiv_matches": cumulative_equiv,
            "char_accuracy": (cumulative_exact + cumulative_equiv) / cumulative_eval * 100.0,
            "fg_accuracy": (cumulative_fg_matches / cumulative_fg_cells) * 100.0,
            "bg_accuracy": (cumulative_bg_matches / cumulative_bg_cells) * 100.0,
        }

        print("\n" + "=" * 80)
        print("RANDOMISED STRESS TEST SUMMARY:")
        print(f"  Total Cells Generated:  {total_tested}")
        print(f"  Invisible Text Cells:   {cumulative_invisible} ({cumulative_invisible/total_tested*100:.2f}%)")
        print(f"  Evaluated Observable:   {cumulative_eval}")
        print(f"  Exact CP437 Matches:    {cumulative_exact}")
        print(f"  Equivalence Matches:    {cumulative_equiv}")
        print(f"  Character Accuracy:     {summary['char_accuracy']:.2f}%")
        print(f"  Foreground Accuracy:    {summary['fg_accuracy']:.2f}%")
        print(f"  Background Accuracy:    {summary['bg_accuracy']:.2f}%")
        print("=" * 80)
        return summary


if __name__ == "__main__":
    asyncio.run(run_random_stress_test(num_seeds=5))
