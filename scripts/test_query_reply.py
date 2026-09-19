"""Experiment with 3270 Query Reply packets against Hercules TK5 to find the exact reply format."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer1.tn3270 import TN3270Driver
from layer1.tn3270.constants import TELNET_EOR, TELNET_IAC


async def test_reply(reply_payload: bytes, name: str):
    print(f"\n================ Testing: {name} ================")
    driver = TN3270Driver("127.0.0.1", 3270, device_type="IBM-3279-2-E")
    try:
        await driver.connect()
        # Screen 0: Splash -> ENTER
        await driver.read_frame()
        await driver.send_aid("ENTER")
        # Screen 1: Logon -> ENTER (to INR)
        await driver.read_frame()
        await driver.send_aid("ENTER")
        # Screen 2: INR -> type 'herc01'
        await driver.read_frame()
        await driver.send_field_input("herc01", row=0, col=1)
        
        f_resp = await driver.read_frame()
        f_query = await driver.read_frame()
        print(f"Received query frame: {len(f_query.raw_payload)} bytes ({f_query.raw_payload.hex()})")

        if f_query.raw_payload.startswith(b"\xf3"):
            print(f"Sending {name}...")
            packet = bytes([0x88]) + reply_payload + bytes([TELNET_IAC, TELNET_EOR])
            await driver.write_raw(packet)

            # Wait for response with up to 3 seconds timeout
            try:
                for i in range(5):
                    f_next = await asyncio.wait_for(driver.read_frame(), timeout=1.5)
                    if f_next.raw_payload:
                        text = f_next.raw_payload.decode("cp037", errors="replace")
                        print(f"SUCCESS! Received response (#{i+1}, {len(f_next.raw_payload)} bytes):")
                        print(f"  Hex: {f_next.raw_payload.hex()[:80]}")
                        print(f"  Text: {repr(text)}")
                        if "password" in text.lower() or "enter" in text.lower() or "ikj" in text.lower():
                            print(">>> FOUND PASSWORD PROMPT! <<<")
                            return True
            except asyncio.TimeoutError:
                print("Timed out waiting for response.")
    finally:
        await driver.disconnect()
    return False


async def main():
    # Attempt 1: Query Reply Summary (QCODE 0x80) listing Usable Area (0x81)
    # Length: 0x00 0x06, SFID: 0x81, QCODE: 0x80, list: [0x80, 0x81]
    # Followed by Query Reply Usable Area (QCODE 0x81): 80x24 (0x0050, 0x0018)
    summary_sf = bytes([0x00, 0x06, 0x81, 0x80, 0x80, 0x81])
    # Usable area: len 0x0017 (23 bytes), SFID 0x81, QCODE 0x81, flags...
    # Standard 3279-2 Usable Area structured field:
    usable_area_sf = bytes([
        0x00, 0x17,  # Length 23
        0x81,        # Query Reply SFID
        0x81,        # QCODE: Usable Area
        0x01, 0x00,  # 12/14-bit addressing
        0x00, 0x50,  # Width = 80
        0x00, 0x18,  # Height = 24
        0x00, 0x01,  # Units: inches or mm
        0x00, 0x00, 0x00, 0x00, # X, Y physical
        0x00, 0x50, 0x00, 0x18, # Width, height in points/chars
        0x00, 0x00   # Reserved
    ])

    # Test 1: Just Summary
    await test_reply(summary_sf, "Summary Only")

    # Test 2: Summary + Usable Area
    await test_reply(summary_sf + usable_area_sf, "Summary + Usable Area")

    # Test 3: Standard Null Query Reply (QCODE 0xFF)
    null_sf = bytes([0x00, 0x04, 0x81, 0xFF])
    await test_reply(null_sf, "Null Query Reply (0xFF)")


if __name__ == "__main__":
    asyncio.run(main())
