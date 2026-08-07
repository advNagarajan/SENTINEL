"""Main CLI entrypoint for Project SENTINEL — run any registered environment driver."""
import argparse
import asyncio
import sys
from typing import Optional
import structlog

from drivers.registry import DriverRegistry
from schemas.events import RuntimeEvent

# Ensure Windows stdout uses UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = structlog.get_logger(__name__)


def render_screen_view(state, config: dict) -> None:
    """Render reconstructed 80x24 terminal screen grid in a clean formatted box."""
    cols = state.screen_size.get("cols", 80)
    border_line = "=" * (cols + 4)
    thin_line = "-" * (cols + 4)

    print("\n" + border_line)
    print(f"| SENTINEL RUNTIME MONITOR | ID: {state.runtime_id} | Target: {config.get('target', {}).get('name', 'UNKNOWN')}")
    print(f"| Gen: #{state.generation:<3} | Hash: {state.screen_hash[:16]}... | Status: {'STABLE' if state.is_stable else 'BUSY'} | OIA: {state.metadata.get('oia_status', 'N/A')}")
    print(border_line)

    for idx, row in enumerate(state.raw_grid):
        # Sanitize any non-printable or cp1252 incompatible characters
        clean_row = "".join([c if (ord(c) < 128 or c.isprintable()) else " " for c in row])
        print(f"| {clean_row:<{cols}} |")

    print(thin_line)
    clean_title = "".join([c if (ord(c) < 128 or c.isprintable()) else " " for c in (state.title or "N/A")])
    print(f"| Title: {clean_title:<65} |")
    print(f"| Cursor: Row {state.cursor['row']:02d}, Col {state.cursor['col']:02d} | Fields Extracted: {len(state.fields):<25} |")
    print(border_line + "\n")


def render_fields_summary(state) -> None:
    """Render summary table of extracted screen fields."""
    if not state.fields:
        return
    print("+" + "-" * 78 + "+")
    print("| EXTRACTED FIELDS SUMMARY" + " " * 54 + "|")
    print("+" + "-" * 25 + "+" + "-" * 30 + "+" + "-" * 10 + "+" + "-" * 10 + "+")
    print(f"| {'FIELD LABEL':<23} | {'VALUE':<28} | {'ROW,COL':<8} | {'TYPE':<8} |")
    print("+" + "-" * 25 + "+" + "-" * 30 + "+" + "-" * 10 + "+" + "-" * 10 + "+")
    for name, f in list(state.fields.items())[:15]:  # Show first 15 fields
        clean_val = "".join([c if (ord(c) < 128 or c.isprintable()) else " " for c in f.value])
        val_str = (clean_val[:25] + "...") if len(clean_val) > 28 else clean_val
        field_type = "READ-ONLY" if f.protected else "INPUT"
        clean_name = "".join([c if (ord(c) < 128 or c.isprintable()) else " " for c in name])
        print(f"| {clean_name[:23]:<23} | {val_str:<28} | {f.row:02d},{f.col:02d}   | {field_type:<8} |")
    print("+" + "-" * 25 + "+" + "-" * 30 + "+" + "-" * 10 + "+" + "-" * 10 + "+")


async def main_loop(config_path: str, interactive: bool) -> None:
    driver, reducer, stability, config = DriverRegistry.create_target(config_path)

    events_log: list[str] = []

    def on_event(ev: RuntimeEvent) -> None:
        events_log.append(f"[{ev.event_type.value.upper()}] {ev.message}")

    driver.add_event_listener(on_event)

    try:
        await driver.connect()
        generation = 1

        state, report = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=generation,
        )

        render_screen_view(state, config)
        render_fields_summary(state)

        if interactive and hasattr(driver, "send_aid"):
            print("\n[Interactive Mode Commands: type 'ENTER', 'PF3', 'CLEAR', or 'Q' to quit]")
            while True:
                try:
                    user_cmd = input("SENTINEL> ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    break
                if user_cmd in ("Q", "QUIT", "EXIT"):
                    break
                elif user_cmd in ("ENTER", "PF1", "PF3", "PF7", "PF8", "CLEAR", "PA1"):
                    generation += 1
                    print(f"Sending AID key: {user_cmd}...")
                    await driver.send_aid(user_cmd)
                    state, report = await stability.wait_until_stable(
                        driver=driver,
                        reducer=reducer,
                        runtime_id=driver.runtime_id,
                        generation=generation,
                    )
                    render_screen_view(state, config)
                    render_fields_summary(state)
                else:
                    print("Unknown command. Type ENTER, PF3, CLEAR, or Q.")

    except Exception as e:
        logger.error("Error running SENTINEL driver", error=str(e))
        import traceback
        traceback.print_exc()
    finally:
        await driver.disconnect()


def cli() -> None:
    parser = argparse.ArgumentParser(description="SENTINEL Gateway Driver Runner")
    parser.add_argument(
        "--config",
        default="configs/mainframe.toml",
        help="Path to target TOML configuration file (default: configs/mainframe.toml)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enable interactive command mode",
    )
    args = parser.parse_args()
    asyncio.run(main_loop(args.config, args.interactive))


if __name__ == "__main__":
    cli()
