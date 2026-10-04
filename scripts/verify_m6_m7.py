"""Quick M6/M7 Verification Script:
1. type_text & press_keys validation (including ALT+F in EDIT).
2. Two-phase settle detection on fast commands (cls, edit, menu navigation).
3. Settle detection on a slow command (dir /s) verifying graceful completion / timeout.
"""
import asyncio
import time
import structlog
from layer1.dos.driver import DOSDriver
from layer2.dos.reducer import DOSStateReducer
from layer2.dos.stability import DOSStabilityEngine
from schemas.contracts import L2toL3HandoffPayload, validate_l2_to_l3_contract

logger = structlog.get_logger(__name__)


async def main() -> None:
    print("=" * 80)
    print("STARTING M6/M7 CONFIRMATION SUITE")
    print("=" * 80)

    driver = DOSDriver()
    reducer = DOSStateReducer()
    stability = DOSStabilityEngine(
        poll_interval_ms=50,
        reaction_timeout_ms=750,
        quiescence_ms=250,
        max_wait_ms=5000,
    )

    await driver.connect()

    try:
        # Step 0: Initial Screen Settle
        print("\n--- Step 0: Initial Screen Capture & Settle ---")
        t0 = time.time()
        initial_state, rep0 = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=0,
        )
        elapsed0 = (time.time() - t0) * 1000.0
        print(f"Initial State: gen={initial_state.generation}, hash={initial_state.screen_hash[:16]}..., is_stable={initial_state.is_stable}")
        print(f"Stability: method={rep0.method}, iterations={rep0.iterations}, time={elapsed0:.1f}ms")
        payload0 = L2toL3HandoffPayload(state=initial_state)
        validate_l2_to_l3_contract(payload0)
        print("L2->L3 Contract Validation: PASSED")

        # Step 1: type_text & press_keys (cls)
        print("\n--- Step 1: M6 type_text & press_keys ('cls') ---")
        t1 = time.time()
        await driver.type_text("cls")
        await driver.press_keys(["ENTER"])
        type_elapsed = (time.time() - t1) * 1000.0
        print(f"Actuation time (type_text + ENTER): {type_elapsed:.1f}ms")

        # M7 Settle after cls
        t_settle = time.time()
        state1, rep1 = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=1,
            previous_state=initial_state,
        )
        settle_elapsed = (time.time() - t_settle) * 1000.0
        print(f"Settled: hash={state1.screen_hash[:16]}..., method={rep1.method}, iterations={rep1.iterations}, time={settle_elapsed:.1f}ms")
        assert state1.is_stable is True

        # Step 2: Open EDIT and trigger ALT+F
        print("\n--- Step 2: M6/M7 EDIT Application & ALT+F Menu Combo ---")
        t2 = time.time()
        await driver.type_text("edit")
        await driver.press_keys(["ENTER"])

        state_edit, rep_edit = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=2,
            previous_state=state1,
        )
        t_edit_total = (time.time() - t2) * 1000.0
        print(f"EDIT Launched & Settled in {t_edit_total:.1f}ms (method={rep_edit.method})")
        print(f"EDIT Title Row 0: {state_edit.raw_grid[0].strip()}")
        assert "FreeDOS Edit" in state_edit.raw_grid[0] or "Edit" in state_edit.raw_grid[1]

        # Trigger ALT+F
        print("\nSending ALT+F combo...")
        t_altf = time.time()
        await driver.press_keys(["ALT+F"])
        state_menu, rep_menu = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=3,
            previous_state=state_edit,
        )
        t_altf_total = (time.time() - t_altf) * 1000.0
        print(f"ALT+F Settled in {t_altf_total:.1f}ms (method={rep_menu.method})")
        print("Menu Dropdown Lines (Rows 2-5):")
        for r in range(2, 6):
            print(f"  {r}: {state_menu.raw_grid[r][:50]}")
        assert "New" in state_menu.raw_grid[3] or "Open" in state_menu.raw_grid[4]
        print("ALT+F in EDIT: CONFIRMED SUCCESSFUL")

        # Exit EDIT: ESC then ALT+X
        print("\nExiting EDIT via ESC and ALT+X...")
        await driver.press_keys(["ESC"])
        await asyncio.sleep(0.15)
        await driver.press_keys(["ALT+X"])
        state_prompt, rep_exit = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=4,
            previous_state=state_menu,
        )
        print(f"Exited back to prompt (hash={state_prompt.screen_hash[:16]}..., method={rep_exit.method})")
        assert "C:\\>" in "\n".join(state_prompt.raw_grid)

        # Step 3: Slow Command Settle Detection (dir /s)
        print("\n--- Step 3: M7 Settle Detection on Slow Command ('dir /s') ---")
        t3 = time.time()
        await driver.type_text("dir /s")
        await driver.press_keys(["ENTER"])
        type_dir_elapsed = (time.time() - t3) * 1000.0
        print(f"Started 'dir /s' command in {type_dir_elapsed:.1f}ms. Waiting for settling...")

        t_dir_settle = time.time()
        state_dir, rep_dir = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=5,
            previous_state=state_prompt,
        )
        dir_settle_elapsed = (time.time() - t_dir_settle) * 1000.0
        print(f"dir /s Settled Result:")
        print(f"  is_stable: {state_dir.is_stable}")
        print(f"  method: {rep_dir.method}")
        print(f"  iterations: {rep_dir.iterations}")
        print(f"  elapsed_ms: {dir_settle_elapsed:.1f}ms")
        print(f"  details: {rep_dir.details}")
        print(f"  screen bottom 4 rows:")
        for r in range(21, 25):
            print(f"    {r}: {state_dir.raw_grid[r].strip()}")

        assert state_dir.is_stable is True
        print("\nM6/M7 CONFIRMATION PASSED WITH FLYING COLORS!")

    finally:
        await driver.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
