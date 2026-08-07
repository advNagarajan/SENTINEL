"""Live connection test script for Layer 1 & Layer 2 against Hercules TK5-MVS on 127.0.0.1:3270."""
import asyncio
import sys
import structlog
from layer1.tn3270 import TN3270Driver
from layer2.reducer import TN3270StateReducer
from schemas.events import RuntimeEvent

logger = structlog.get_logger(__name__)


def handle_event(event: RuntimeEvent) -> None:
    print(f"  [EVENT] {event.event_type.value.upper()} | {event.message}")


async def main() -> None:
    host = "127.0.0.1"
    port = 3270
    print("=== PROJECT SENTINEL — Live Mainframe Integration ===")
    print(f"Target: IBM Mainframe Hercules TK5-MVS ({host}:{port})")
    print("-" * 60)

    driver = TN3270Driver(host=host, port=port, runtime_id="hercules_tk5")
    driver.add_event_listener(handle_event)
    reducer = TN3270StateReducer(rows=24, cols=80)

    try:
        await driver.connect()
        print("\n[+] Reading initial TK5-MVS screen frame...")

        frame = await driver.read_frame()
        decoded = reducer.parse_frame(frame)
        som = reducer.build_object_model(decoded)
        state, _ = reducer.reduce_state(som, runtime_id="hercules_tk5", generation=1)

        print("\n=== MAINFRAME SCREEN REDUCTION COMPLETE ===")
        print(f"  Runtime ID:      {state.runtime_id}")
        print(f"  Generation:      {state.generation}")
        print(f"  Screen Hash:     {state.screen_hash}")
        print(f"  OIA Status:      {state.metadata.get('oia_status')}")
        print(f"  Cursor Position: Row {state.cursor['row']}, Col {state.cursor['col']}")
        print(f"  Detected Title:  {state.title}")
        print(f"  Fields Extracted:{len(state.fields)}")
        print("=" * 60)
        print("  RECONSTRUCTED 80x24 TEXT GRID:")
        print("=" * 60)
        for idx, row in enumerate(state.raw_grid):
            print(f"{idx:02d} | {row}")
        print("=" * 60)

    except Exception as e:
        print(f"[-] Error during live execution: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await driver.disconnect()
        print("\n[+] Test complete.")


if __name__ == "__main__":
    asyncio.run(main())
