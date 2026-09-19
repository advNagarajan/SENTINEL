"""Test to check screen parsing and fields after WCC fix."""
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

    # Step 0: Splash
    await driver.read_frame()
    # Step 1: ENTER -> Logon screen
    await driver.send_aid("ENTER")
    await driver.read_frame()
    # Step 1b: ENTER -> 'INPUT NOT RECOGNIZED'
    await driver.send_aid("ENTER")
    f_inr = await driver.read_frame()

    decoded = reducer.parse_frame(f_inr)
    som = reducer.build_object_model(decoded)
    s, _ = reducer.reduce_state(som, "test", 3)

    print("--- S3 State with WCC fix ---")
    print(f"Title: {repr(s.title)}")
    print(f"Decoded WCC: {hex(decoded.wcc) if decoded.wcc is not None else None}")
    print(f"Fields count: {len(s.fields)}")
    for fid, f in s.fields.items():
        print(f"  {fid}: prot={f.protected} at ({f.row},{f.col}) len={f.length} val={repr(f.value)}")

    await driver.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
