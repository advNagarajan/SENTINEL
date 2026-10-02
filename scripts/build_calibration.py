"""Builds CALIB.COM, mounts it via QMP into a FAT floppy image, captures VNC, and creates cp437_vga_9x16.npz."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import numpy as np
from PIL import Image

import asyncvnc
sys.path.append("/home/sivakuhan/Projects/FreeDos")
from qmp_client import QMPClient


ASM_SOURCE = """
.code16
.intel_syntax noprefix
.text
_start:
    mov ax, 0xB800
    mov es, ax
    xor di, di
    mov cx, 2000
    mov ax, 0x0720
    rep stosw

    mov di, 672
    xor bl, bl
    mov bh, 16
row_loop:
    mov cl, 16
col_loop:
    mov al, bl
    mov ah, 0x07
    stosw
    mov ax, 0x0720
    stosw
    inc bl
    dec cl
    jnz col_loop
    add di, 96
    dec bh
    jnz row_loop

    mov ah, 0
    int 0x16

    mov ax, 0x4C00
    int 0x21
"""


def build_calib_com(com_path: Path) -> None:
    asm_path = Path("/tmp/calib_gen.s")
    obj_path = Path("/tmp/calib_gen.o")
    asm_path.write_text(ASM_SOURCE)
    subprocess.run(["as", "--32", "-o", str(obj_path), str(asm_path)], check=True)
    subprocess.run(["objcopy", "-O", "binary", "-j", ".text", str(obj_path), str(com_path)], check=True)


def build_floppy_image(com_path: Path, img_path: Path) -> None:
    if img_path.exists():
        img_path.unlink()
    subprocess.run(["mformat", "-C", "-f", "1440", "-i", str(img_path), "::"], check=True)
    subprocess.run(["mcopy", "-i", str(img_path), str(com_path), "::CALIB.COM"], check=True)


async def capture_screen(vnc_host: str = "127.0.0.1", vnc_port: int = 5900) -> np.ndarray:
    async with asyncvnc.connect(vnc_host, vnc_port) as client:
        # Launch A:\CALIB.COM
        client.keyboard.write("a")
        with client.keyboard.hold("Shift_L"):
            client.keyboard.press(":")
        client.keyboard.write("\\calib.com")
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(1.0)

        # Capture calibration frame
        frame = await client.screenshot()

        # Send return to exit CALIB.COM
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.5)

        return frame


def extract_templates_and_save(frame: np.ndarray, npz_out: Path) -> None:
    templates = np.zeros((256, 16, 9), dtype=np.uint8)
    for code in range(256):
        r = code // 16
        c = code % 16
        R = 4 + r
        C = 16 + c * 2
        y0 = R * 16
        y1 = y0 + 16
        x0 = C * 9
        x1 = x0 + 9
        cell = frame[y0:y1, x0:x1]
        fg_mask = (cell[:, :, 0] > 50).astype(np.uint8)
        templates[code] = fg_mask

    npz_out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_out, templates=templates)
    print(f"Saved {len(templates)} templates to {npz_out}")


def main() -> None:
    com_path = Path("/tmp/CALIB.COM")
    img_path = Path("/tmp/tools.img")
    npz_path = Path("layer2/dos/data/cp437_vga_9x16.npz")

    print("[M3 Builder] Assembling CALIB.COM...")
    build_calib_com(com_path)

    print("[M3 Builder] Building 1.44M floppy image with mtools...")
    build_floppy_image(com_path, img_path)

    print("[M3 Builder] Mounting floppy via QMP blockdev-change-medium...")
    qmp = QMPClient()
    qmp.connect()
    qmp.execute("blockdev-change-medium", {"device": "floppy0", "filename": str(img_path), "format": "raw"})
    qmp.close()

    print("[M3 Builder] Running CALIB.COM and capturing VNC frame...")
    frame = asyncio.run(capture_screen())

    print("[M3 Builder] Slicing and saving templates...")
    extract_templates_and_save(frame, npz_path)
    print("[M3 Builder] Completed successfully!")


if __name__ == "__main__":
    main()
