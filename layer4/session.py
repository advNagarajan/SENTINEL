"""Session Bootstrapper for automated authentication and pre-flight terminal setup.

Enables agents to start directly at target entrypoints (such as ISPF Primary Option Menu
or TSO READY) without spending tokens and roundtrips on splash banners, credential prompts,
and broadcast fortune quotes on every task.
"""
import asyncio
from typing import Any, Optional
import structlog

from layer1.base import EnvironmentDriver
from layer2.base import StateReducer, StabilityEngine
from schemas.state import RuntimeState

logger = structlog.get_logger(__name__)


class SessionBootstrapper:
    """Pre-flight macro runner for authenticating and establishing terminal sessions."""

    def __init__(self, session_config: Optional[dict[str, Any]] = None) -> None:
        self.config = session_config or {}
        self.entrypoint: str = self.config.get("entrypoint", "ispf").lower()
        self.user: str = self.config.get("user", "herc01")
        self.password: str = self.config.get("password", "cul8tr")
        self.auto_login: bool = self.config.get("auto_login", True)
        self.auto_logout: bool = self.config.get("auto_logout", True)

    async def bootstrap(
        self,
        driver: EnvironmentDriver,
        reducer: StateReducer,
        stability: StabilityEngine,
    ) -> tuple[RuntimeState, Any]:
        """Execute pre-flight bootstrap sequence to reach the desired entrypoint."""
        runtime_id = getattr(driver, "runtime_id", "mainframe_node_01")

        # 0. Settle on initial splash screen
        state, report = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=runtime_id,
            generation=1,
        )

        if not self.auto_login or self.entrypoint == "raw":
            logger.info("session_bootstrap_skipped", entrypoint="raw")
            return state, report

        logger.info("session_bootstrap_starting", target_entrypoint=self.entrypoint, user=self.user)

        # Helper to execute action and settle
        async def step_aid(aid: str = "ENTER", gen: int = 1) -> RuntimeState:
            if hasattr(driver, "send_aid"):
                await driver.send_aid(aid)
            s, _ = await stability.wait_until_stable(driver=driver, reducer=reducer, runtime_id=runtime_id, generation=gen)
            return s

        async def step_input(text: str, row: int = 0, col: int = 0, aid: str = "ENTER", gen: int = 1) -> RuntimeState:
            if hasattr(driver, "send_field_input"):
                await driver.send_field_input(text=text, row=row, col=col, aid_name=aid)
            s, _ = await stability.wait_until_stable(driver=driver, reducer=reducer, runtime_id=runtime_id, generation=gen)
            return s

        # Step 1: Splash Screen -> Press ENTER to reach Logon or INR
        logger.debug("bootstrap_step_1_splash_enter")
        state = await step_aid("ENTER", gen=2)

        # Step 1b: If on Logon screen (e.g. 'Logon ===>'), press ENTER to reach INR
        grid_text = " ".join(state.raw_grid).lower()
        if "logon ===>" in grid_text and "input not recognized" not in grid_text:
            logger.debug("bootstrap_step_1b_logon_enter")
            state = await step_aid("ENTER", gen=3)
            grid_text = " ".join(state.raw_grid).lower()

        # Step 2: Submit username (or reconnect if session already exists)
        editable = [f for f in state.fields.values() if not f.protected]
        target_r = editable[0].row if editable else 0
        target_c = editable[0].col if editable else 0

        logger.debug("bootstrap_step_2_send_user", user=self.user, row=target_r, col=target_c)
        state = await step_input(self.user, row=target_r, col=target_c, gen=4)
        grid_text = " ".join(state.raw_grid).lower()

        # Step 2b: Check if user is in use or reconnect needed
        if "in use" in grid_text or "rejected" in grid_text or "enter logon or logoff" in grid_text:
            logger.info("bootstrap_reconnect_detected", user=self.user)
            editable_rec = [f for f in state.fields.values() if not f.protected]
            rec_r = editable_rec[0].row if editable_rec else 0
            rec_c = editable_rec[0].col if editable_rec else 0
            state = await step_input(f"logon {self.user} reconnect", row=rec_r, col=rec_c, gen=5)
            grid_text = " ".join(state.raw_grid).lower()

        # Step 3: Password prompt
        if "password" in grid_text:
            logger.debug("bootstrap_step_3_send_password")
            editable_pwd = [f for f in state.fields.values() if not f.protected]
            pwd_r = editable_pwd[0].row if editable_pwd else 0
            pwd_c = editable_pwd[0].col if editable_pwd else 0
            state = await step_input(self.password, row=pwd_r, col=pwd_c, gen=6)
            grid_text = " ".join(state.raw_grid).lower()

        # Step 4: Clear Welcome / Logon progress screen
        if "logon in progress" in grid_text or "welcome to the tso" in grid_text or "reconnect successful" in grid_text:
            logger.debug("bootstrap_step_4_clear_welcome")
            state = await step_aid("ENTER", gen=7)
            grid_text = " ".join(state.raw_grid).lower()

        # Step 5: Clear quote / broadcast messages / *** prompt
        if "***" in grid_text or "broadcast" in grid_text:
            logger.debug("bootstrap_step_5_clear_quote")
            state = await step_aid("ENTER", gen=8)
            grid_text = " ".join(state.raw_grid).lower()

        # Step 6: Handle target entrypoint
        if self.entrypoint == "ispf":
            # If not yet on ISPF, send one more ENTER in case a quote or multi-line broadcast is waiting
            if "ispf" not in (state.title or "").lower():
                state = await step_aid("ENTER", gen=9)
            logger.info("session_bootstrap_complete", entrypoint="ispf", title=state.title)

        elif self.entrypoint == "tso":
            # If landed in ISPF, exit to TSO READY prompt
            if "ispf" in (state.title or "").lower():
                editable_ispf = [f for f in state.fields.values() if not f.protected]
                opt_r = editable_ispf[0].row if editable_ispf else 1
                opt_c = editable_ispf[0].col if editable_ispf else 14
                state = await step_input("X", row=opt_r, col=opt_c, gen=9)
            logger.info("session_bootstrap_complete", entrypoint="tso", title=state.title)

        # Normalize bootstrapped state as Generation #1 for Layer 4 Agent
        state.generation = 1

        return state, report

    async def teardown(
        self,
        driver: EnvironmentDriver,
        reducer: StateReducer,
        stability: StabilityEngine,
        active_state: Optional[RuntimeState],
    ) -> None:
        """Gracefully exit applications and log off to leave terminal clean."""
        if not self.auto_logout or not active_state:
            return

        runtime_id = getattr(driver, "runtime_id", "mainframe_node_01")
        title_lower = (active_state.title or "").lower()
        grid_text = " ".join(active_state.raw_grid).lower()

        logger.info("session_teardown_starting")

        # 1. Exit ISPF if currently active
        if "ispf" in title_lower or "ispf" in grid_text:
            logger.debug("teardown_exiting_ispf")
            editable = [f for f in active_state.fields.values() if not f.protected]
            opt_r = editable[0].row if editable else 1
            opt_c = editable[0].col if editable else 14
            if hasattr(driver, "send_field_input"):
                await driver.send_field_input("X", row=opt_r, col=opt_c)
            active_state, _ = await stability.wait_until_stable(
                driver=driver, reducer=reducer, runtime_id=runtime_id, generation=998
            )

        # 2. Issue LOGOFF from TSO READY prompt
        if hasattr(driver, "send_field_input"):
            logger.debug("teardown_issuing_logoff")
            editable = [f for f in active_state.fields.values() if not f.protected]
            log_r = editable[0].row if editable else 0
            log_c = editable[0].col if editable else 0
            await driver.send_field_input("LOGOFF", row=log_r, col=log_c)
            await stability.wait_until_stable(
                driver=driver, reducer=reducer, runtime_id=runtime_id, generation=999
            )

        logger.info("session_teardown_complete")
