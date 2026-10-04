"""Builds RANDVRAM.COM and copies it to the tools floppy image for randomised stress testing."""
from pathlib import Path
import subprocess
import sys

sys.path.append("/home/sivakuhan/Projects/FreeDos")
from qmp_client import QMPClient


ASM_SOURCE = """
.code16
.intel_syntax noprefix
.text
_start:
    # Save seed in SI
    mov si, 12345

    # Hide cursor: off-screen (row 25) and disable scanline
    mov ah, 0x02
    xor bh, bh
    mov dh, 25
    xor dl, dl
    int 0x10

    mov ah, 0x01
    mov cx, 0x2000
    int 0x10

    # Default blink mode = 0 (bp=0: blink ON via BL=01, bp=1: blink OFF via BL=00)
    xor bp, bp

main_loop:
    # Set blink mode via INT 10h AX=1003h
    mov ax, 0x1003
    mov bx, bp
    xor bl, 0x01   # if bp=0 -> bl=1 (blink ON); if bp=1 -> bl=0 (blink OFF)
    int 0x10

    # Get active page offset from BDA 0040:004E
    mov ax, 0x0040
    mov es, ax
    mov di, es:[0x004E]

    # Target VRAM at 0xB800:DI
    mov ax, 0xB800
    mov es, ax

    mov cx, 2000
fill_cell:
    # LCG: SI = SI * 25173 + 13849
    mov ax, si
    mov dx, 25173
    mul dx
    add ax, 13849
    mov si, ax

    # Write AL (char) and AH (attr) to ES:[DI]
    stosw
    dec cx
    jnz fill_cell

    # Wait for keypress
    mov ah, 0
    int 0x16

    cmp al, 27       # ESC
    je exit_prog
    cmp al, 'q'
    je exit_prog
    cmp al, 'Q'
    je exit_prog

    cmp al, 'b'
    je toggle_blink
    cmp al, 'B'
    je toggle_blink

    # Any other key (e.g. Space) generates next screen with next seed
    jmp main_loop

toggle_blink:
    xor bp, 1
    jmp main_loop

exit_prog:
    # Restore normal underline cursor (scanlines 6 to 7)
    mov ah, 0x01
    mov cx, 0x0607
    int 0x10

    # Restore cursor position (0, 0)
    mov ah, 0x02
    xor bh, bh
    xor dx, dx
    int 0x10

    # Restore default blink ON
    mov ax, 0x1003
    mov bl, 0x01
    int 0x10

    # Clear screen (mode 3)
    mov ax, 0x0003
    int 0x10

    mov ax, 0x4C00
    int 0x21
"""


def build_randvram_com(com_path: Path) -> None:
    asm_path = Path("/tmp/randvram.s")
    obj_path = Path("/tmp/randvram.o")
    asm_path.write_text(ASM_SOURCE)
    subprocess.run(["as", "--32", "-o", str(obj_path), str(asm_path)], check=True)
    subprocess.run(["objcopy", "-O", "binary", "-j", ".text", str(obj_path), str(com_path)], check=True)
    print(f"Built {com_path} ({com_path.stat().st_size} bytes)")


def main() -> None:
    com_path = Path("/tmp/RANDVRAM.COM")
    img_path = Path("/tmp/tools.img")

    print("[RANDVRAM Builder] Assembling RANDVRAM.COM...")
    build_randvram_com(com_path)

    print("[RANDVRAM Builder] Copying to /tmp/tools.img...")
    subprocess.run(["mcopy", "-o", "-i", str(img_path), str(com_path), "::RANDVRAM.COM"], check=True)

    print("[RANDVRAM Builder] Remounting floppy in QEMU via QMP...")
    qmp = QMPClient()
    qmp.connect()
    qmp.execute("blockdev-change-medium", {"device": "floppy0", "filename": str(img_path), "format": "raw"})
    qmp.close()
    print("[RANDVRAM Builder] Ready!")


if __name__ == "__main__":
    main()
