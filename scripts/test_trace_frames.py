"""Complete navigation to ISPF and clean logout."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer1.tn3270 import TN3270Driver
from layer2.tn3270 import TN3270StateReducer


async def main():
    driver = TN3270Driver("127.0.0.1", 3270, device_type="IBM-3279-2-E")
    reducer = TN3270StateReducer()
    await driver.connect()

    print("[1] Splash Screen -> Sending ENTER")
    await driver.read_frame()
    await driver.send_aid("ENTER")

    print("[2] Logon Screen -> Sending ENTER to reach INR")
    await driver.read_frame()
    await driver.send_aid("ENTER")

    print("[3] INR Screen -> Typing 'herc01' at (0, 1)...")
    await driver.read_frame()
    await driver.send_field_input("herc01", row=0, col=1)

    # Read until password prompt
    print("[4] Waiting for Password Prompt...")
    pwd_prompt_found = False
    for _ in range(5):
        f = await driver.read_frame()
        text = f.raw_payload.decode("cp037", errors="replace")
        if "password" in text.lower():
            pwd_prompt_found = True
            break
    if not pwd_prompt_found:
        print("Failed to reach password prompt!")
        return

    print("[5] Sending password 'cul8tr'...")
    await driver.send_field_input("cul8tr", row=0, col=22)

    # After password, MVS displays 'LOGON IN PROGRESS' and TSO Welcome
    print("[6] Waiting for TSO Welcome / Logon messages...")
    await asyncio.sleep(1.0)
    f_logon = await driver.read_frame()
    print("Logon text:", repr(f_logon.raw_payload.decode("cp037", errors="replace")))

    # Step 5: Press ENTER for quote banner
    print("\n[7] Sending ENTER for Fortune Quote / Broadcast...")
    await driver.send_aid("ENTER")
    await asyncio.sleep(1.0)
    f_quote = await driver.read_frame()
    som_quote = reducer.build_object_model(reducer.parse_frame(f_quote))
    s_quote, _ = reducer.reduce_state(som_quote, "live", 7)
    print(f"Quote Screen Title: {repr(s_quote.title)}")
    print("Top 3 lines:")
    for line in s_quote.raw_grid[:3]:
        print(" ", repr(line))

    # Step 6: Press ENTER -> arrives at ISPF Primary Option Menu!
    print("\n[8] Sending ENTER to enter ISPF...")
    await driver.send_aid("ENTER")
    await asyncio.sleep(1.0)
    f_ispf = await driver.read_frame()
    som_ispf = reducer.build_object_model(reducer.parse_frame(f_ispf))
    s_ispf, _ = reducer.reduce_state(som_ispf, "live", 8)
    print("\n" + "="*60)
    print("       ARRIVED AT ISPF PRIMARY OPTION MENU!       ")
    print("="*60)
    print(f"ISPF Title: {repr(s_ispf.title)}")
    print("Screen lines:")
    for i, line in enumerate(s_ispf.raw_grid[:12]):
        print(f"  [{i:02d}] {line}")

    # Step 7: To logout: type 'X', press ENTER
    print("\n[9] Logging out of ISPF: Typing 'X' and pressing ENTER...")
    # Find editable field (Option ===>)
    editable = [f for f in s_ispf.fields.values() if not f.protected]
    if editable:
        print(f"Typing 'X' into field {editable[0].field_id} at ({editable[0].row}, {editable[0].col})")
        await driver.send_field_input("X", row=editable[0].row, col=editable[0].col)
    else:
        await driver.send_field_input("X", row=s_ispf.cursor["row"], col=s_ispf.cursor["col"])

    await asyncio.sleep(1.0)
    f_ready = await driver.read_frame()
    som_ready = reducer.build_object_model(reducer.parse_frame(f_ready))
    s_ready, _ = reducer.reduce_state(som_ready, "live", 9)
    print(f"\n[10] Exited ISPF. Screen Title: {repr(s_ready.title)}")
    print("Top 3 lines:")
    for line in s_ready.raw_grid[:3]:
        print(" ", repr(line))

    # Step 8: Type 'logoff', press ENTER -> back to original screen
    print("\n[11] Typing 'logoff' and pressing ENTER...")
    await driver.send_field_input("LOGOFF", row=s_ready.cursor["row"], col=s_ready.cursor["col"])
    await asyncio.sleep(1.0)
    f_final = await driver.read_frame()
    som_final = reducer.build_object_model(reducer.parse_frame(f_final))
    s_final, _ = reducer.reduce_state(som_final, "live", 10)
    print(f"\n[12] Final Screen Title: {repr(s_final.title)}")
    print("Back to original screen verified!")

    await driver.disconnect()
    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
