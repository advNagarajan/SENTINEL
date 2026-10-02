"""Automated verification of full printable ASCII range (0x20 - 0x7E) over VNC against QMP VRAM oracle."""
import asyncio
import os
import sys
import asyncvnc

# Add FreeDos path for qmp_client
sys.path.append("/home/sivakuhan/Projects/FreeDos")
from qmp_client import QMPClient


# Complete US QWERTY Layout Shift Table
# Maps characters requiring Shift to their keysym name
US_SHIFTED_MAP = {
    "~": ("asciitilde", True),
    "!": ("exclam", True),
    "@": ("at", True),
    "#": ("numbersign", True),
    "$": ("dollar", True),
    "%": ("percent", True),
    "^": ("asciicircum", True),
    "&": ("ampersand", True),
    "*": ("asterisk", True),
    "(": ("parenleft", True),
    ")": ("parenright", True),
    "_": ("underscore", True),
    "+": ("plus", True),
    "{": ("braceleft", True),
    "}": ("braceright", True),
    "|": ("bar", True),
    ":": ("colon", True),
    '"': ("quotedbl", True),
    "<": ("less", True),
    ">": ("greater", True),
    "?": ("question", True),
}

for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    US_SHIFTED_MAP[ch] = (ch, True)


async def send_char(client: asyncvnc.Client, ch: str) -> None:
    """Send single character with shift handling according to US keyboard layout."""
    if ch in US_SHIFTED_MAP:
        keysym, _ = US_SHIFTED_MAP[ch]
        with client.keyboard.hold("Shift_L"):
            client.keyboard.press(keysym)
    else:
        client.keyboard.press(ch)
    await client.drain()
    await asyncio.sleep(0.02)


async def type_string(client: asyncvnc.Client, text: str) -> None:
    for ch in text:
        await send_char(client, ch)


def dump_vram_text() -> list[str]:
    """Use QMP pmemsave oracle to read physical text VRAM at 0xB8000."""
    qmp = QMPClient()
    qmp.connect()
    tmp_path = "/tmp/vram_test.bin"
    qmp.execute("pmemsave", {"val": 0xB8000, "size": 4000, "filename": tmp_path})
    qmp.close()

    with open(tmp_path, "rb") as f:
        data = f.read()

    lines: list[str] = []
    for r in range(25):
        row_chars = []
        for c in range(80):
            byte_val = data[(r * 80 + c) * 2]
            row_chars.append(chr(byte_val))
        lines.append("".join(row_chars))
    return lines


async def run_ascii_suite() -> bool:
    print("[ASCII Test] Connecting to FreeDOS over VNC at 127.0.0.1:5900...")
    async with asyncvnc.connect("127.0.0.1", 5900) as client:
        # 1. Clear prompt line with Ctrl+C and Return
        with client.keyboard.hold("Control_L"):
            client.keyboard.press("c")
        await client.drain()
        await asyncio.sleep(0.2)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.5)

        # Line 1: Letters & Numbers (0x30-0x39, 0x41-0x5A, 0x61-0x7A)
        line1 = "rem ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789"
        print(f"[ASCII Test] Sending Line 1: {line1}")
        await type_string(client, line1)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.8)

        # Line 2: All US Symbols (shifted & unshifted)
        # unshifted: ` - = [ ] \ ; ' , . /
        # shifted:   ~ ! @ # $ % ^ & * ( ) _ + { } | : " < > ?
        line2 = "rem `~!@#$%^&*()_+{}|:\"<>?-=[]\\;',./"
        print(f"[ASCII Test] Sending Line 2: {line2}")
        await type_string(client, line2)
        client.keyboard.press("Return")
        await client.drain()
        await asyncio.sleep(0.8)

        # Capture PNG of final screen
        from PIL import Image
        frame = await client.screenshot()
        os.makedirs("logs", exist_ok=True)
        Image.fromarray(frame).save("logs/m2_ascii_suite.png")
        print("[ASCII Test] Saved screenshot to logs/m2_ascii_suite.png")

    # 3. Verify against QMP VRAM dump
    vram_lines = dump_vram_text()
    print("\n--- Recent VRAM Lines Dump ---")
    for r_idx, v_line in enumerate(vram_lines[-7:]):
        print(f"Row {18 + r_idx:02d}: {v_line.rstrip()}")

    found1 = any(line1 in l for l in vram_lines)
    found2 = any(line2 in l for l in vram_lines)

    print("\n--- Verification Results ---")
    print(f"Alphanumeric range verified: {found1}")
    print(f"Full Symbols range verified:  {found2}")

    all_matched = found1 and found2
    if all_matched:
        print("\nSUCCESS: All 95 printable ASCII characters (0x20 - 0x7E) matched byte-for-byte in guest VRAM!")
    else:
        print("\nFAILURE: Mismatch between sent characters and VRAM dump!")
    return all_matched


if __name__ == "__main__":
    success = asyncio.run(run_ascii_suite())
    sys.exit(0 if success else 1)
