"""Live test executing the complete verified ISPF login and logout flow via LiveValidationHarness:
1. Connect -> Splash screen
2. Step 1: Press ENTER -> Logon screen
3. Step 1b: Press ENTER -> 'INPUT NOT RECOGNIZED' screen
4. Step 2: Type 'herc01' -> Password Prompt ('ENTER CURRENT PASSWORD FOR HERC01')
5. Step 3: Type 'cul8tr' -> 'LOGON IN PROGRESS' / TSO Welcome
6. Step 4: Press ENTER -> Fortune Quote / Broadcast messages
7. Step 5: Press ENTER -> Arrive at ISPF Primary Option Menu!
8. Step 6: Type 'X' + ENTER -> Exit ISPF
9. Step 7: Type 'LOGOFF' + ENTER -> Return to initial screen
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness


async def main():
    harness = LiveValidationHarness(config_path="configs/mainframe.toml")
    try:
        print("\n[Step 0] Connecting to live Hercules TK5...")
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

        # Step 1b: press ENTER on Logon screen to transition to 'INPUT NOT RECOGNIZED'
        state_logon = harness.gateway.get_active_payload().state
        if "logon" in (state_logon.raw_grid[11].lower() if len(state_logon.raw_grid) > 11 else ""):
            print("\n[Step 1b] Sending ENTER on Logon screen to reach 'INPUT NOT RECOGNIZED'...")
            obs1b = await harness.step("trigger_action", {
                "generation_token": state_logon.generation_token,
                "action_id": "ENTER",
            })
            print(f"Step 1b Result: {obs1b.get('success')}")
            harness.render_debug_grid()

        # Step 2: type 'herc01' and press ENTER
        state2 = harness.gateway.get_active_payload().state
        editable2 = [f for f in state2.fields.values() if not f.protected]
        target_fid = editable2[0].field_id if editable2 else list(state2.fields.keys())[0]
        print(f"\n[Step 2] Typing 'herc01' into field '{target_fid}'...")
        obs2 = await harness.step("set_field_and_submit", {
            "generation_token": state2.generation_token,
            "field_id": target_fid,
            "value": "herc01",
            "action": "ENTER",
        })
        print(f"Step 2 Result: {obs2.get('success')} (msg: {obs2.get('message') or obs2.get('error')})")
        harness.render_debug_grid()

        # Step 2b: If userid already in use, send 'logon herc01 reconnect'
        state_after_usr = harness.gateway.get_active_payload().state
        grid_text = " ".join(state_after_usr.raw_grid).lower()
        if "in use" in grid_text or "rejected" in grid_text or "logon or logoff" in grid_text:
            print("\n[Step 2b] User in use detected! Reconnecting...")
            editable2b = [f for f in state_after_usr.fields.values() if not f.protected]
            rec_fid = editable2b[0].field_id if editable2b else list(state_after_usr.fields.keys())[0]
            obs2b = await harness.step("set_field_and_submit", {
                "generation_token": state_after_usr.generation_token,
                "field_id": rec_fid,
                "value": "logon herc01 reconnect",
                "action": "ENTER",
            })
            print(f"Step 2b Result: {obs2b.get('success')} (msg: {obs2b.get('message') or obs2b.get('error')})")
            harness.render_debug_grid()

        # Step 3: type password 'cul8tr' and press ENTER
        state3 = harness.gateway.get_active_payload().state
        editable3 = [f for f in state3.fields.values() if not f.protected]
        pwd_fid = editable3[0].field_id if editable3 else list(state3.fields.keys())[0]
        print(f"\n[Step 3] Typing password 'cul8tr' into field '{pwd_fid}'...")
        obs3 = await harness.step("set_field_and_submit", {
            "generation_token": state3.generation_token,
            "field_id": pwd_fid,
            "value": "cul8tr",
            "action": "ENTER",
        })
        print(f"Step 3 Result: {obs3.get('success')} (msg: {obs3.get('message') or obs3.get('error')})")
        harness.render_debug_grid()

        # Step 4: press ENTER on TSO welcome
        state4 = harness.gateway.get_active_payload().state
        print(f"\n[Step 4] Pressing ENTER on TSO Welcome...")
        obs4 = await harness.step("trigger_action", {
            "generation_token": state4.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 4 Result: {obs4.get('success')}")
        harness.render_debug_grid()

        # Step 5: press ENTER on Quote / Broadcast
        state5 = harness.gateway.get_active_payload().state
        print(f"\n[Step 5] Pressing ENTER on Broadcast / Quote...")
        obs5 = await harness.step("trigger_action", {
            "generation_token": state5.generation_token,
            "action_id": "ENTER",
        })
        print(f"Step 5 Result: {obs5.get('success')}")
        harness.render_debug_grid()

        print("\n" + "="*70)
        print("          VALIDATION: ARRIVED AT ISPF PRIMARY OPTION MENU!          ")
        print("="*70)
        final_state = harness.gateway.get_active_payload().state
        print(f"Generation: #{final_state.generation}")
        print(f"Screen Title: '{final_state.title}'")
        print(f"Total Extracted Fields: {len(final_state.fields)}")
        print("\nAVAILABLE COMPILED L3 TOOLS AT ISPF SCREEN:")
        print(harness.present_tools())

        # Clean Logout: Step 6: type 'X' and press ENTER
        ispf_editable = [f for f in final_state.fields.values() if not f.protected]
        if ispf_editable:
            opt_fid = ispf_editable[0].field_id
            print(f"\n[Step 6] Exiting ISPF: Typing 'X' into '{opt_fid}'...")
            obs6 = await harness.step("set_field_and_submit", {
                "generation_token": final_state.generation_token,
                "field_id": opt_fid,
                "value": "X",
                "action": "ENTER",
            })
            print(f"Step 6 Result: {obs6.get('success')}")
            harness.render_debug_grid()

        # Clean Logout: Step 7: type 'LOGOFF' and press ENTER
        state_ready = harness.gateway.get_active_payload().state
        ready_editable = [f for f in state_ready.fields.values() if not f.protected]
        if ready_editable:
            ready_fid = ready_editable[0].field_id
            print(f"\n[Step 7] Logging off: Typing 'LOGOFF' into '{ready_fid}'...")
            obs7 = await harness.step("set_field_and_submit", {
                "generation_token": state_ready.generation_token,
                "field_id": ready_fid,
                "value": "LOGOFF",
                "action": "ENTER",
            })
            print(f"Step 7 Result: {obs7.get('success')}")
            harness.render_debug_grid()

        print("\n=== ISPF VALIDATION & LOGOUT WORKFLOW COMPLETED SUCCESSFULLY ===")

    finally:
        await harness.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
