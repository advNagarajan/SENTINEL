"""Full live test following the user's verified ISPF login and logout flow."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness


async def main():
    harness = LiveValidationHarness(config_path="configs/mainframe.toml")
    try:
        print("[Step 0] Connecting to live Hercules TK5...")
        await harness.connect()
        state = harness.gateway.get_active_payload().state
        print(f"Connected! Gen: #{state.generation}, Title: '{state.title}', Token: {state.generation_token}")
        harness.render_debug_grid()

        # Step 1: press ENTER on splash screen
        print("\n[Step 1] Sending ENTER on Splash Screen...")
        obs1 = await harness.step("trigger_action", {
            "generation_token": harness.gateway.get_active_payload().state.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 1 Result: {obs1.get('success')}")
        harness.render_debug_grid()

        # Step 1b: press ENTER on Logon screen to go to INPUT NOT RECOGNIZED
        state_logon = harness.gateway.get_active_payload().state
        if "logon" in (state_logon.raw_grid[11].lower() if len(state_logon.raw_grid) > 11 else ""):
            print("\n[Step 1b] Sending ENTER on Logon screen to transition to 'INPUT NOT RECOGNIZED'...")
            obs1b = await harness.step("trigger_action", {
                "generation_token": state_logon.generation_token,
                "action_id": "ENTER",
            })
            print(f"Step 1b Result: {obs1b.get('success')}")
            harness.render_debug_grid()

        # Step 2: type 'herc01' and press ENTER
        state2 = harness.gateway.get_active_payload().state
        editable2 = [f for f in state2.fields.values() if not f.protected]
        print(f"\n[Step 2] Typing 'herc01' into editable field: {[f.field_id for f in editable2]}...")
        target_fid = editable2[0].field_id if editable2 else "fld_input_not_recognized"
        obs2 = await harness.step("set_field_and_submit", {
            "generation_token": state2.generation_token,
            "field_id": target_fid,
            "value": "herc01",
            "action": "ENTER",
        })
        print(f"Step 2 Result: {obs2.get('success')}, Title now: '{harness.gateway.get_active_payload().state.title}'")
        harness.render_debug_grid()

        # Step 3: type password 'cul8tr' and press ENTER
        state3 = harness.gateway.get_active_payload().state
        editable3 = [f for f in state3.fields.values() if not f.protected]
        print(f"\n[Step 3] Typing password 'cul8tr' into editable field: {[f.field_id for f in editable3]}...")
        if editable3:
            pwd_fid = editable3[0].field_id
            obs3 = await harness.step("set_field_and_submit", {
                "generation_token": state3.generation_token,
                "field_id": pwd_fid,
                "value": "cul8tr",
                "action": "ENTER",
            })
        else:
            obs3 = await harness.step("trigger_action", {
                "generation_token": state3.generation_token,
                "action_id": "ENTER",
            })
        print(f"Step 3 Result: {obs3.get('success')}, Title now: '{harness.gateway.get_active_payload().state.title}'")
        harness.render_debug_grid()

        # Step 4: press ENTER on 'welcome to TSO'
        state4 = harness.gateway.get_active_payload().state
        print("\n[Step 4] Pressing ENTER on TSO welcome / quote...")
        obs4 = await harness.step("trigger_action", {
            "generation_token": state4.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 4 Result: {obs4.get('success')}, Title now: '{harness.gateway.get_active_payload().state.title}'")
        harness.render_debug_grid()

        # Step 5: press ENTER on quote
        state5 = harness.gateway.get_active_payload().state
        print("\n[Step 5] Pressing ENTER on quote banner...")
        obs5 = await harness.step("trigger_action", {
            "generation_token": state5.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 5 Result: {obs5.get('success')}, Title now: '{harness.gateway.get_active_payload().state.title}'")
        harness.render_debug_grid()

        print("\n==========================================")
        print("          ISPF VALIDATION CHECK           ")
        print("==========================================")
        final_state = harness.gateway.get_active_payload().state
        print(f"Generation: #{final_state.generation}")
        print(f"Screen Title: '{final_state.title}'")
        print(f"Fields extracted: {len(final_state.fields)}")
        print("\nAVAILABLE COMPILED L3 TOOLS:")
        print(harness.present_tools())

    finally:
        await harness.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
