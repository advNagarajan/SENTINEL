"""Live Validation Test: Multi-turn autonomous agent menu navigation via strict L3 to L4 contracts."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer4.harness import LiveValidationHarness
from schemas.contracts import validate_l3_to_l4_contract, L3toL4HandoffPayload


async def main():
    print("=" * 70)
    print("   LIVE AGENT MENU NAVIGATION & STRICT L3->L4 CONTRACT VALIDATION   ")
    print("=" * 70)

    harness = LiveValidationHarness(config_path="configs/mainframe.toml")

    try:
        print("\n[Step 0] Bootstrapping live session into ISPF...")
        await harness.connect()

        # -------------------------------------------------------------------------
        # Turn 1: Screen 1 (ISPF Primary Option Menu)
        # -------------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("TURN 1: Initial Observation (Screen 1 - ISPF Primary Option Menu)")
        print("-" * 60)
        obs_gen1 = harness.get_agent_observation()
        
        print(f"Success: {obs_gen1['success']}")
        print(f"Generation: #{obs_gen1['generation']} | Token: {obs_gen1['generation_token']}")
        print(f"Screen Title: {repr(obs_gen1['screen_title'])}")
        print(f"Status Message: {repr(obs_gen1['status_message'])}")
        print("\nVisible Screen Text (First 10 lines):")
        for line in obs_gen1["screen_text"][:10]:
            print(f"  {line}")

        # Assertions on Turn 1 observation
        assert obs_gen1["success"] is True
        assert obs_gen1["generation"] == 1
        assert "ispf" in (obs_gen1["screen_title"] or "").lower()
        assert len(obs_gen1["screen_text"]) >= 5
        assert any("utilities" in line.lower() for line in obs_gen1["screen_text"]), (
            "Expected 'UTILITIES' option visible in screen_text"
        )
        assert "fld_option_===>" in obs_gen1["editable_fields"]

        # Check compiled tools bound to gen 1
        tool_names = [t["function"]["name"] for t in obs_gen1["available_tools"]]
        print(f"\nAvailable Compiled Tools: {tool_names}")
        assert "set_field_and_submit" in tool_names
        assert "trigger_action" in tool_names

        # -------------------------------------------------------------------------
        # Turn 2: Agent selects Option 3 (UTILITIES)
        # -------------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("TURN 2: Agent Action -> set_field_and_submit('fld_option_===>', '3', 'ENTER')")
        print("-" * 60)
        obs_gen2 = await harness.step("set_field_and_submit", {
            "generation_token": obs_gen1["generation_token"],
            "field_id": "fld_option_===>",
            "value": "3",
            "action": "ENTER",
        })

        print(f"Success: {obs_gen2['success']}")
        print(f"Generation: #{obs_gen2['generation']} | Token: {obs_gen2['generation_token']}")
        print(f"Screen Title: {repr(obs_gen2['screen_title'])}")
        print("\nVisible Screen Text on Screen 2:")
        for line in obs_gen2["screen_text"][:10]:
            print(f"  {line}")

        # Assertions on Screen 2 (UTILITY SELECTION MENU)
        assert obs_gen2["success"] is True
        assert obs_gen2["generation"] == 2
        assert "utilit" in (obs_gen2["screen_title"] or "").lower()
        assert any("data set" in line.lower() for line in obs_gen2["screen_text"]), (
            "Expected 'Data Set' utility option visible in screen_text"
        )
        # Verify tools are now re-bound to gen 2 token
        submit_tool = next(t for t in obs_gen2["available_tools"] if t["function"]["name"] == "set_field_and_submit")
        assert submit_tool["function"]["parameters"]["properties"]["generation_token"]["enum"] == [obs_gen2["generation_token"]]

        # -------------------------------------------------------------------------
        # Turn 3: Agent selects Option 2 (Data Set Utility)
        # -------------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("TURN 3: Agent Action -> set_field_and_submit('fld_option_===>', '2', 'ENTER')")
        print("-" * 60)
        obs_gen3 = await harness.step("set_field_and_submit", {
            "generation_token": obs_gen2["generation_token"],
            "field_id": "fld_option_===>",
            "value": "2",
            "action": "ENTER",
        })

        print(f"Success: {obs_gen3['success']}")
        print(f"Generation: #{obs_gen3['generation']} | Token: {obs_gen3['generation_token']}")
        print(f"Screen Title: {repr(obs_gen3['screen_title'])}")
        print("\nVisible Screen Text on Screen 3:")
        for line in obs_gen3["screen_text"][:10]:
            print(f"  {line}")

        assert obs_gen3["success"] is True
        assert obs_gen3["generation"] == 3
        assert "data set" in (obs_gen3["screen_title"] or "").lower()

        # -------------------------------------------------------------------------
        # Turn 4: Agent Navigates Back via PF3 (Return to Utility Menu)
        # -------------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("TURN 4: Agent Action -> trigger_action('PF3') to return to Utilities Menu")
        print("-" * 60)
        obs_gen4 = await harness.step("trigger_action", {
            "generation_token": obs_gen3["generation_token"],
            "action_id": "PF3",
        })

        print(f"Success: {obs_gen4['success']}")
        print(f"Generation: #{obs_gen4['generation']} | Token: {obs_gen4['generation_token']}")
        print(f"Screen Title: {repr(obs_gen4['screen_title'])}")
        assert obs_gen4["success"] is True
        assert obs_gen4["generation"] == 4
        assert "utilit" in (obs_gen4["screen_title"] or "").lower()

        # -------------------------------------------------------------------------
        # Turn 5: Agent Navigates Back via PF3 (Return to ISPF Primary Menu)
        # -------------------------------------------------------------------------
        print("\n" + "-" * 60)
        print("TURN 5: Agent Action -> trigger_action('PF3') to return to Primary Menu")
        print("-" * 60)
        obs_gen5 = await harness.step("trigger_action", {
            "generation_token": obs_gen4["generation_token"],
            "action_id": "PF3",
        })

        print(f"Success: {obs_gen5['success']}")
        print(f"Generation: #{obs_gen5['generation']} | Token: {obs_gen5['generation_token']}")
        print(f"Screen Title: {repr(obs_gen5['screen_title'])}")
        assert obs_gen5["success"] is True
        assert obs_gen5["generation"] == 5
        assert "ispf" in (obs_gen5["screen_title"] or "").lower()

        print("\n" + "=" * 70)
        print("   ALL 5 AGENT MENU NAVIGATION TURNS COMPLETED SUCCESSFULLY!   ")
        print("=" * 70)

    finally:
        print("\n[Teardown] Disconnecting cleanly and logging off...")
        await harness.disconnect()
        print("Disconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
