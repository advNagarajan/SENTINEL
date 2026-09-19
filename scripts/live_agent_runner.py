"""Live Agent Runner & Validation CLI for Project SENTINEL.

Replicates Layer 4: boots the gateway, renders screen dashboards,
and accepts agent or human commands to validate downward action execution.
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Optional

# Ensure repository root is on Python module search path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness


async def check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if target host and port are actively listening."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def print_interactive_help() -> None:
    print("""
L4 Agent Command Interface:
  tools                                   -> Display compiled L3 AI tools catalog (JSON Schema)
  set <field_id> <value> [action=ENTER]   -> Execute set_field_and_submit tool
  act <action_id>                         -> Execute trigger_action tool (ENTER, CLEAR, PF1-PF24)
  inspect                                 -> Execute get_screen_state tool
  grid                                    -> Optional developer debug ASCII screen render
  quit / q                                -> Disconnect and exit
""")


async def interactive_loop(harness: LiveValidationHarness, debug_render: bool = False) -> None:
    """REPL allowing interactive execution of Layer 3 tools purely through tool invocations."""
    if not harness.gateway:
        print("\n[!] Error: Harness gateway is not connected or initialized.")
        return

    print_interactive_help()
    print("\n--- ACTIVE L3 TOOLS PRESENTED TO AGENT ---")
    print(harness.present_tools())
    if debug_render:
        harness.render_debug_grid()

    while True:
        try:
            user_input = input("\nL4-AGENT> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        parts = user_input.split()
        cmd = parts[0].lower()

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "help":
            print_interactive_help()
        elif cmd == "tools":
            print("\n--- ACTIVE L3 TOOLS PRESENTED TO AGENT ---")
            print(harness.present_tools())
        elif cmd == "grid":
            harness.render_debug_grid()
        elif cmd == "inspect":
            obs = await harness.step("get_screen_state", {})
            print(json.dumps(obs, indent=2))
        elif cmd == "act" and len(parts) >= 2:
            action_id = parts[1].upper()
            gateway = harness.gateway
            if not gateway:
                print("\n[!] Error: Harness is not connected.")
                break
            gen_token = gateway.get_active_payload().state.generation_token
            obs = await harness.step("trigger_action", {
                "generation_token": gen_token,
                "action_id": action_id,
            })
            print("\n--- AGENT OBSERVATION (TOOL RESPONSE) ---")
            print(json.dumps(obs, indent=2))
            if debug_render:
                harness.render_debug_grid()
        elif cmd == "set" and len(parts) >= 3:
            field_id = parts[1]
            action_key = "ENTER"
            val_tokens = parts[2:]
            if val_tokens and val_tokens[-1].upper().startswith("ACTION="):
                action_key = val_tokens[-1].split("=")[1].upper()
                val_tokens = val_tokens[:-1]
            value = " ".join(val_tokens)
            gateway = harness.gateway
            if not gateway:
                print("\n[!] Error: Harness is not connected.")
                break
            gen_token = gateway.get_active_payload().state.generation_token
            obs = await harness.step("set_field_and_submit", {
                "generation_token": gen_token,
                "field_id": field_id,
                "value": value,
                "action": action_key,
            })
            print("\n--- AGENT OBSERVATION (TOOL RESPONSE) ---")
            print(json.dumps(obs, indent=2))
            if debug_render:
                harness.render_debug_grid()
        else:
            print(f"Unknown command '{user_input}'. Type 'help' for syntax.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="SENTINEL Layer 4 Live Agent Validation Runner")
    parser.add_argument("--config", default="configs/mainframe.toml", help="Path to TOML config")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive L4 agent REPL")
    parser.add_argument("--mock", action="store_true", help="Run in mock simulation mode without a live emulator")
    parser.add_argument("--debug-render", action="store_true", help="Enable optional developer ASCII grid rendering")
    args = parser.parse_args()

    harness = LiveValidationHarness(config_path=args.config)

    if args.mock:
        print("\n[*] Starting L4 Agent Interface in SIMULATED MOCK MODE...")
        await harness.connect_mock()
        if args.interactive:
            await interactive_loop(harness, debug_render=args.debug_render)
        else:
            print("\n--- ACTIVE L3 TOOLS PRESENTED TO AGENT ---")
            print(harness.present_tools())
            if args.debug_render:
                harness.render_debug_grid()
            print("\n[+] Mock pipeline verified. Run with --interactive to execute tools.")
        await harness.disconnect()
        return

    from drivers.registry import DriverRegistry
    config = DriverRegistry.load_config(args.config)
    tn3270_cfg = config.get("tn3270", {})
    host = tn3270_cfg.get("host", "127.0.0.1")
    port = tn3270_cfg.get("port", 3270)

    print(f"Connecting to target environment at {host}:{port}...")
    is_live = await check_port(host, port)

    if not is_live:
        print(f"\n[!] Target {host}:{port} is currently OFFLINE.")
        print("    To run offline mock simulation, execute with --mock:")
        print("    python scripts/live_agent_runner.py --mock [--interactive]")
        print("    When Hercules TK5 is started, re-run without --mock to execute live.\n")
        return

    try:
        await harness.connect()
        print("\n[+] Successfully connected to Hercules TK5-MVS!")
        if args.interactive:
            await interactive_loop(harness, debug_render=args.debug_render)
        else:
            print("\n--- ACTIVE L3 TOOLS PRESENTED TO AGENT ---")
            print(harness.present_tools())
            if args.debug_render:
                harness.render_debug_grid()
            print("\nRun with --interactive to interactively invoke tools.")
    finally:
        await harness.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
