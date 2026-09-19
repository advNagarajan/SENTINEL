"""Live test executing the exact ISPF login sequence:
1. press enter
2. type 'herc01', press enter
3. type 'cul8tr', press enter
4. press enter
-> Arrive at ISPF.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness


async def main():
    harness = LiveValidationHarness(config_path="configs/mainframe.toml")
    try:
        print("[1/5] Connecting to live Hercules TK5...")
        await harness.connect()
        state = harness.gateway.get_active_payload().state
        print(f"Connected! Gen: #{state.generation}, Title: '{state.title}', Token: {state.generation_token}")
        harness.render_debug_grid()

        # Step 1: press enter
        print("\n[2/5] Executing Step 1: trigger_action ENTER...")
        obs1 = await harness.step("trigger_action", {
            "generation_token": harness.gateway.get_active_payload().state.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 1 Result: {obs1.get('success')}, Message: {obs1.get('message')}")
        harness.render_debug_grid()

        # Step 2: type 'herc01', press enter
        state2 = harness.gateway.get_active_payload().state
        editable2 = [f for f in state2.fields.values() if not f.protected]
        print(f"\n[3/5] Executing Step 2: typing 'herc01' into editable fields: {[f.field_id for f in editable2]}...")
        if editable2:
            target_fid = editable2[0].field_id
            obs2 = await harness.step("set_field_and_submit", {
                "generation_token": state2.generation_token,
                "field_id": target_fid,
                "value": "herc01",
                "action": "ENTER",
            })
        else:
            obs2 = await harness.step("trigger_action", {
                "generation_token": state2.generation_token,
                "action_id": "ENTER",
            })
        print(f"Step 2 Result: {obs2.get('success')}, Message: {obs2.get('message')}")
        harness.render_debug_grid()

        # Step 3: type 'cul8tr', press enter
        state3 = harness.gateway.get_active_payload().state
        editable3 = [f for f in state3.fields.values() if not f.protected]
        print(f"\n[4/5] Executing Step 3: typing 'cul8tr' into editable fields: {[f.field_id for f in editable3]}...")
        if editable3:
            target_fid = editable3[0].field_id
            obs3 = await harness.step("set_field_and_submit", {
                "generation_token": state3.generation_token,
                "field_id": target_fid,
                "value": "cul8tr",
                "action": "ENTER",
            })
        else:
            obs3 = await harness.step("trigger_action", {
                "generation_token": state3.generation_token,
                "action_id": "ENTER",
            })
        print(f"Step 3 Result: {obs3.get('success')}, Message: {obs3.get('message')}")
        harness.render_debug_grid()

        # Step 4: press enter
        state4 = harness.gateway.get_active_payload().state
        print(f"\n[5/5] Executing Step 4: trigger_action ENTER...")
        obs4 = await harness.step("trigger_action", {
            "generation_token": state4.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 4 Result: {obs4.get('success')}, Message: {obs4.get('message')}")
        harness.render_debug_grid()

        print("\n--- FINAL STATE REACHED ---")
        final_state = harness.gateway.get_active_payload().state
        print(f"Generation: #{final_state.generation}, Title: '{final_state.title}'")
        print("\nAVAILABLE TOOLS AT FINAL SCREEN:")
        print(harness.present_tools())

    finally:
        await harness.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
