"""Test script for verifying keystroke injection and screen transition navigation on Hercules TK5-MVS."""
import asyncio
import sys
from drivers.registry import DriverRegistry
from layer4.audit_log import AuditLogger
from schemas.events import RuntimeEvent


def render_grid_box(title: str, state) -> None:
    cols = state.screen_size.get("cols", 80)
    print("\n" + "=" * (cols + 4))
    print(f"| {title:<{cols + 2}} |")
    print(f"| Gen: #{state.generation} | Hash: {state.screen_hash[:16]}... | Status: {'STABLE' if state.is_stable else 'BUSY'} |")
    print("=" * (cols + 4))
    for idx, row in enumerate(state.raw_grid[:12]):
        clean_row = "".join([c if (ord(c) < 128 or c.isprintable()) else " " for c in row])
        print(f"| {clean_row:<{cols}} |")
    print("=" * (cols + 4))


async def main() -> None:
    print("=== PROJECT SENTINEL — Keystroke Navigation & Screen Delta Test ===")
    config_path = "configs/mainframe.toml"
    driver, reducer, stability, config = DriverRegistry.create_target(config_path)
    audit_logger = AuditLogger("logs/mainframe_audit.jsonl")

    def on_event(ev: RuntimeEvent) -> None:
        print(f"  [EVENT] {ev.event_type.value.upper()}: {ev.message}")
        audit_logger.log_event(ev)

    driver.add_event_listener(on_event)

    try:
        await driver.connect()

        # Step 1: Initial Screen (Gen #1)
        print("\n[Step 1] Reading initial splash screen...")
        state_1, _ = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=1,
        )
        audit_logger.log_state(state_1)
        render_grid_box("INITIAL MAINFRAME SCREEN (Gen #1)", state_1)

        # Step 2: Inject ENTER key to navigate to next screen
        print("\n[Step 2] Injecting ENTER key to trigger mainframe navigation...")
        await driver.send_aid("ENTER")

        # Step 3: Wait for stability and capture next screen (Gen #2)
        state_2, _ = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=2,
        )
        audit_logger.log_state(state_2)
        render_grid_box("NEXT MAINFRAME SCREEN (Gen #2)", state_2)

        print("\n=== SCREEN TRANSITION DELTA REPORT ===")
        print(f"  Transition:       Gen #{state_1.generation} -> Gen #{state_2.generation}")
        print(f"  Screen Hash Prev: {state_1.screen_hash[:16]}...")
        print(f"  Screen Hash Next: {state_2.screen_hash[:16]}...")
        print(f"  Screen Changed:   {state_1.screen_hash != state_2.screen_hash}")
        print(f"  Fields Count:     {len(state_1.fields)} -> {len(state_2.fields)}")
        print("=" * 60)

    except Exception as e:
        print(f"[-] Navigation error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await driver.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
