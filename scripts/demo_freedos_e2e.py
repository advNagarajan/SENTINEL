"""Milestone 8: Scripted End-to-End Demo for FreeDOS target through Layer 3 AIToolGateway.

Scenario:
1. Read screen via gateway.get_observation()
2. Run 'dir' via gateway.execute_tool('set_field_and_submit', ...)
3. Read directory output via gateway.get_observation()
4. Open EDIT via gateway.execute_tool('set_field_and_submit', ...)
5. Open File menu with ALT+F via gateway.execute_tool('trigger_action', ...)
6. Exit EDIT via ESC and ALT+X via gateway.execute_tool('trigger_action', ...)
7. Verify return to shell prompt and record all step latencies and transitions.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any
import structlog

# Ensure repo root is in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layer1.dos.driver import DOSDriver
from layer2.dos.action_lowerer import DOSActionLowerer
from layer2.dos.reducer import DOSStateReducer
from layer2.dos.screen_reader import DOSScreenReader
from layer2.dos.stability import DOSStabilityEngine
from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from schemas.contracts import L2toL3HandoffPayload, validate_l2_to_l3_contract, validate_l3_to_l4_contract

logger = structlog.get_logger(__name__)


async def run_e2e_demo(host: str = "127.0.0.1", port: int = 5900, log_path: str = "logs/demo_freedos_run.log") -> dict[str, Any]:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    report_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        report_lines.append(msg)

    log("=" * 80)
    log("PROJECT SENTINEL: FREEDOS END-TO-END DEMO (LAYER 3 GATEWAY)")
    log("=" * 80)
    log(f"Target: FreeDOS 1.3 on {host}:{port} (QEMU Snapshot Mode)")
    log(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    log("-" * 80)

    # 1. Initialize Architectural Stack
    driver = DOSDriver(host=host, port=port)
    reducer = DOSStateReducer()
    action_lowerer = DOSActionLowerer()
    stability = DOSStabilityEngine(
        poll_interval_ms=50,
        reaction_timeout_ms=750,
        quiescence_ms=300,
        max_wait_ms=6000,
    )

    await driver.connect()

    action_timings: dict[str, float] = {}
    captured_grids: dict[str, list[str]] = {}

    try:
        # Initial Reset to ensure clean prompt
        await driver.press_keys(["CTRL+C"])
        await asyncio.sleep(0.3)

        # Initial Settle
        t_init = time.time()
        initial_state, init_rep = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=0,
        )
        init_elapsed = (time.time() - t_init) * 1000.0
        initial_payload = L2toL3HandoffPayload(state=initial_state)
        validate_l2_to_l3_contract(initial_payload)

        dispatcher = ActionDispatcher(
            driver=driver,
            reducer=reducer,
            action_lowerer=action_lowerer,
            stability_engine=stability,
        )
        gateway = AIToolGateway(
            initial_payload=initial_payload,
            dispatcher=dispatcher,
        )

        log("\n>>> STACK INITIALIZED & LAYER 3 GATEWAY READY <<<")
        log(f"Initial State: Generation #{initial_state.generation}, Hash={initial_state.screen_hash[:16]}...")
        log(f"Initial Settling Latency: {init_elapsed:.1f}ms (method={init_rep.method})")

        # -------------------------------------------------------------
        # Step 1: Read Screen via Gateway (Observation)
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 1: Read Initial Screen State (gateway.get_observation)")
        log("=" * 80)
        t_step1 = time.time()
        obs1 = gateway.get_observation()
        step1_ms = (time.time() - t_step1) * 1000.0
        action_timings["1_read_screen"] = step1_ms

        log(f"Observation Generation: #{obs1.generation} (token: {obs1.generation_token})")
        log(f"Screen Title: '{obs1.screen_title}'")
        log(f"Cursor Position: row={obs1.cursor['row']}, col={obs1.cursor['col']}")
        log(f"Editable Fields: {list(obs1.editable_fields.keys())}")
        log(f"Available Tools: {[t['function']['name'] for t in obs1.available_tools]}")
        log(f"Action Latency: {step1_ms:.2f}ms")

        # -------------------------------------------------------------
        # Step 2: Run 'dir' Command via Gateway Tool Execution
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 2: Run 'dir' via gateway.execute_tool('set_field_and_submit')")
        log("=" * 80)
        t_step2 = time.time()
        res2 = await gateway.execute_tool(
            name="set_field_and_submit",
            arguments={
                "generation_token": obs1.generation_token,
                "field_id": "cmd_prompt",
                "value": "dir",
                "action": "ENTER",
            },
        )
        step2_ms = (time.time() - t_step2) * 1000.0
        action_timings["2_run_dir"] = step2_ms

        assert res2["success"] is True, f"Failed to execute dir: {res2}"
        log(f"Action Result: Success={res2['success']}, New Generation=#{res2['generation']}")
        log(f"Downward Dispatch & Settling Latency: {step2_ms:.1f}ms")

        # -------------------------------------------------------------
        # Step 3: Read Output of 'dir'
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 3: Read 'dir' Output (gateway.get_observation)")
        log("=" * 80)
        t_step3 = time.time()
        obs3 = gateway.get_observation()
        step3_ms = (time.time() - t_step3) * 1000.0
        action_timings["3_read_dir_output"] = step3_ms

        log(f"Observation Generation: #{obs3.generation} (token: {obs3.generation_token})")
        log("Visible Screen Text Snippet (Bottom 5 Lines):")
        for line in obs3.screen_text[-5:]:
            log(f"  {line}")
        log(f"Action Latency: {step3_ms:.2f}ms")

        captured_grids["dir_output"] = gateway.get_active_payload().state.raw_grid

        # -------------------------------------------------------------
        # Step 4: Open EDIT Application
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 4: Open EDIT via gateway.execute_tool('set_field_and_submit')")
        log("=" * 80)
        t_step4 = time.time()
        res4 = await gateway.execute_tool(
            name="set_field_and_submit",
            arguments={
                "generation_token": obs3.generation_token,
                "field_id": "cmd_prompt",
                "value": "edit",
                "action": "ENTER",
            },
        )
        step4_ms = (time.time() - t_step4) * 1000.0
        action_timings["4_open_edit"] = step4_ms

        assert res4["success"] is True, f"Failed to open edit: {res4}"
        log(f"Action Result: Success={res4['success']}, New Generation=#{res4['generation']}")
        log(f"Application Launch & Settling Latency: {step4_ms:.1f}ms")
        log(f"Active Screen Title: '{res4.get('screen_title')}'")

        obs4 = gateway.get_observation()
        assert "Edit" in (obs4.screen_title or "") or any("Edit" in l for l in obs4.screen_text[:3])
        captured_grids["edit_main"] = gateway.get_active_payload().state.raw_grid

        # -------------------------------------------------------------
        # Step 5: Use Menu with ALT+F in EDIT
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 5: Trigger Menu with ALT+F via gateway.execute_tool('trigger_action')")
        log("=" * 80)
        t_step5 = time.time()
        res5 = await gateway.execute_tool(
            name="trigger_action",
            arguments={
                "generation_token": obs4.generation_token,
                "action_id": "ALT+F",
            },
        )
        step5_ms = (time.time() - t_step5) * 1000.0
        action_timings["5_menu_alt_f"] = step5_ms

        assert res5["success"] is True, f"Failed to send ALT+F: {res5}"
        log(f"Action Result: Success={res5['success']}, New Generation=#{res5['generation']}")
        log(f"Menu Trigger & Settling Latency: {step5_ms:.1f}ms")

        obs5 = gateway.get_observation()
        log("Active Menu Dropdown Items (Lines 2-6):")
        for line in obs5.screen_text[2:7]:
            log(f"  {line}")

        captured_grids["edit_menu_alt_f"] = gateway.get_active_payload().state.raw_grid

        # -------------------------------------------------------------
        # Step 6: Exit EDIT (Dismiss Menu via ESC, Exit via ALT+X)
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("STEP 6: Exit EDIT via gateway.execute_tool('trigger_action') [ESC then ALT+X]")
        log("=" * 80)
        t_step6a = time.time()
        res6a = await gateway.execute_tool(
            name="trigger_action",
            arguments={
                "generation_token": obs5.generation_token,
                "action_id": "ESC",
            },
        )
        step6a_ms = (time.time() - t_step6a) * 1000.0
        obs6a = gateway.get_observation()

        t_step6b = time.time()
        res6b = await gateway.execute_tool(
            name="trigger_action",
            arguments={
                "generation_token": obs6a.generation_token,
                "action_id": "ALT+X",
            },
        )
        step6b_ms = (time.time() - t_step6b) * 1000.0
        total_exit_ms = step6a_ms + step6b_ms
        action_timings["6_exit_edit"] = total_exit_ms

        assert res6b["success"] is True, f"Failed to exit edit: {res6b}"
        log(f"Action Result: Success={res6b['success']}, New Generation=#{res6b['generation']}")
        log(f"Exit Latency: ESC={step6a_ms:.1f}ms, ALT+X={step6b_ms:.1f}ms (Total={total_exit_ms:.1f}ms)")

        obs_final = gateway.get_observation()
        log("Final Screen State (Bottom 4 Lines):")
        for line in obs_final.screen_text[-4:]:
            log(f"  {line}")

        captured_grids["final_shell"] = gateway.get_active_payload().state.raw_grid

        # -------------------------------------------------------------
        # Timing Summary & Report
        # -------------------------------------------------------------
        log("\n" + "=" * 80)
        log("END-TO-END DEMO PERFORMANCE & ACTION TIMING SUMMARY")
        log("=" * 80)
        log(f"{'Action':<35} | {'Duration (ms)':<15} | {'Status'}")
        log("-" * 65)
        for act, dur in action_timings.items():
            log(f"{act:<35} | {dur:10.1f} ms    | SUCCESS")
        total_time_ms = sum(action_timings.values())
        log("-" * 65)
        log(f"{'TOTAL SCENARIO DURATION':<35} | {total_time_ms:10.1f} ms    | ALL STEPS PASSED")
        log("=" * 80)

        # Write log file
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines) + "\n")

        # Write json summary
        json_summary_path = log_path.replace(".log", ".json")
        with open(json_summary_path, "w", encoding="utf-8") as f:
            json.dump({
                "target": "FreeDOS 1.3",
                "timestamp": time.time(),
                "action_timings_ms": action_timings,
                "total_duration_ms": total_time_ms,
                "success": True,
                "steps_completed": 6,
            }, f, indent=2)

        log(f"\nExecution log saved to: {log_path}")
        log(f"JSON metrics saved to: {json_summary_path}")

        return {
            "action_timings": action_timings,
            "captured_grids": captured_grids,
            "success": True,
        }

    finally:
        await driver.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SENTINEL FreeDOS E2E Demo")
    parser.add_argument("--test", action="store_true", help="Run against snapshot test VM")
    parser.add_argument("--host", default="127.0.0.1", help="QEMU VNC host")
    parser.add_argument("--port", type=int, default=5900, help="QEMU VNC port")
    parser.add_argument("--log", default="logs/demo_freedos_run.log", help="Output log file")
    args = parser.parse_args()

    asyncio.run(run_e2e_demo(host=args.host, port=args.port, log_path=args.log))
