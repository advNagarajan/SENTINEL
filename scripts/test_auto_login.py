"""Test automated session bootstrapping directly to ISPF and clean teardown."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness


async def main():
    print("=== Testing Automated Session Bootstrapping ===")
    harness = LiveValidationHarness(config_path="configs/mainframe.toml")

    try:
        print("\n[1] Calling harness.connect() with auto_login = True...")
        await harness.connect()

        state = harness.gateway.get_active_payload().state
        print("\n" + "=" * 60)
        print("          BOOTSTRAP SUCCESSFUL! INITIAL AGENT STATE:          ")
        print("=" * 60)
        print(f"Generation: #{state.generation}")
        print(f"Title: {repr(state.title)}")
        print(f"Token: {state.generation_token}")
        print(f"Extracted Fields Count: {len(state.fields)}")
        harness.render_debug_grid()

        # Invariant checks:
        assert state.generation == 1, f"Expected Generation #1, got #{state.generation}"
        assert "ispf" in (state.title or "").lower(), f"Expected ISPF screen title, got '{state.title}'"
        assert any(not f.protected for f in state.fields.values()), "Expected at least one editable field at ISPF"

        print("\nAVAILABLE COMPILED TOOLS FOR AGENT AT GEN #1:")
        print(harness.present_tools())

        print("\n[2] Performing an agent action: typing '1' (BROWSE) into Option ===>...")
        editable = [f for f in state.fields.values() if not f.protected]
        opt_fid = editable[0].field_id
        obs = await harness.step("set_field_and_submit", {
            "generation_token": state.generation_token,
            "field_id": opt_fid,
            "value": "1",
            "action": "ENTER",
        })
        print(f"Agent Action Result: {obs.get('success')}")
        harness.render_debug_grid()

        # Return to ISPF main menu via PF3 (or END)
        print("\n[3] Returning to ISPF Primary Option Menu via PF3...")
        obs_back = await harness.step("trigger_action", {
            "generation_token": harness.gateway.get_active_payload().state.generation_token,
            "action_id": "PF3",
        })
        print(f"Return Result: {obs_back.get('success')}")
        harness.render_debug_grid()

    finally:
        print("\n[4] Calling harness.disconnect() with auto_logout = True...")
        await harness.disconnect()
        print("Disconnected cleanly and session logged off!")

    print("\n=== All auto_login bootstrap tests PASSED! ===")


if __name__ == "__main__":
    asyncio.run(main())
