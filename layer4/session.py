"""Session Bootstrapper for automated authentication and pre-flight terminal setup.

Enables agents to start directly at target entrypoints (such as ISPF Primary Option Menu
or TSO READY) without spending tokens and roundtrips on splash banners, credential prompts,
and broadcast fortune quotes on every task.
"""
import asyncio
from typing import Any, Optional, Protocol
import structlog

from layer1.base import EnvironmentDriver
from layer2.base import StateReducer, StabilityEngine
from schemas.state import RuntimeState

logger = structlog.get_logger(__name__)


class RuntimeSession(Protocol):
    """Runtime-independent lifecycle contract owned by Layer 4."""

    async def connect(self) -> None:
        """Connect the underlying runtime."""

    async def initialize(self) -> RuntimeState:
        """Acquire the initial state exposed to Layer 4."""

    async def teardown(self, active_state: Optional[RuntimeState]) -> None:
        """Release session-level resources before disconnecting."""

    async def disconnect(self) -> None:
        """Disconnect the underlying runtime."""


class PipelineSession:
    """Generic session adapter for an already-constructed L1-L2 pipeline."""

    def __init__(
        self,
        driver: EnvironmentDriver,
        reducer: StateReducer,
        stability: StabilityEngine,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        self.driver = driver
        self.reducer = reducer
        self.stability = stability
        self.config = config or {}
        session_cfg = self.config.get("session", {}) if isinstance(self.config, dict) else {}
        self.bootstrapper = SessionBootstrapper(session_cfg) if session_cfg else None

    async def connect(self) -> None:
        await self.driver.connect()

    async def initialize(self) -> RuntimeState:
        if self.bootstrapper and self.bootstrapper.auto_login and self.bootstrapper.entrypoint != "raw":
            state, _ = await self.bootstrapper.bootstrap(
                driver=self.driver,
                reducer=self.reducer,
                stability=self.stability,
            )
            return state

        state, _ = await self.stability.wait_until_stable(
            driver=self.driver,
            reducer=self.reducer,
            runtime_id=self.driver.runtime_id,
            generation=1,
        )
        return state

    async def teardown(self, active_state: Optional[RuntimeState]) -> None:
        if self.bootstrapper and self.bootstrapper.auto_logout and active_state:
            await self.bootstrapper.teardown(
                driver=self.driver,
                reducer=self.reducer,
                stability=self.stability,
                active_state=active_state,
            )

    async def disconnect(self) -> None:
        await self.driver.disconnect()


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

        # Dynamic State-Machine Bootstrap Loop
        max_bootstrap_steps = 15
        step_count = 0
        while step_count < max_bootstrap_steps:
            step_count += 1
            grid_text = " ".join(state.raw_grid).lower()
            title_text = (state.title or "").lower()

            # Target 1: ISPF entrypoint reached
            if self.entrypoint == "ispf" and ("ispf" in title_text or "ispf" in grid_text) and "option  ===>" in grid_text:
                logger.info("session_bootstrap_complete", entrypoint="ispf", title=state.title)
                break

            # Target 2: TSO READY entrypoint reached
            if self.entrypoint == "tso" and "ready" in grid_text and "ispf" not in title_text and "option  ===>" not in grid_text:
                logger.info("session_bootstrap_complete", entrypoint="tso", title=state.title)
                break

            # If already at ISPF but target is TSO, exit ISPF
            if self.entrypoint == "tso" and "option  ===>" in grid_text:
                logger.debug("bootstrap_exiting_ispf_to_tso")
                editable = [f for f in state.fields.values() if not f.protected]
                r = editable[0].row if editable else 1
                c = editable[0].col if editable else 14
                state = await step_input("X", row=r, col=c, gen=step_count)
                continue

            # State A: Blank / uninitialized screen -> Press ENTER to trigger Hercules splash
            if not any(row.strip() for row in state.raw_grid):
                logger.debug("bootstrap_wake_blank_screen")
                state = await step_aid("ENTER", gen=step_count)
                continue

            # State B: Splash Screen -> Press ENTER to reach Logon
            if "hercules" in grid_text or "turnkey" in grid_text:
                logger.debug("bootstrap_splash_screen")
                state = await step_aid("ENTER", gen=step_count)
                continue

            # State C: Logon Screen with 'logon ===>' -> Press ENTER to reach INR
            if "logon ===>" in grid_text and "input not recognized" not in grid_text:
                logger.debug("bootstrap_logon_screen")
                state = await step_aid("ENTER", gen=step_count)
                continue

            # State D: User in use / rejected -> Reconnect
            if "in use" in grid_text or "rejected" in grid_text:
                logger.debug("bootstrap_reconnect_user", user=self.user)
                editable = [f for f in state.fields.values() if not f.protected]
                r = editable[0].row if editable else 0
                c = editable[0].col if editable else 0
                state = await step_input(f"logon {self.user} reconnect", row=r, col=c, gen=step_count)
                continue

            # State E: 'INPUT NOT RECOGNIZED' or username prompt
            if "input not recognized" in grid_text or "enter logon" in grid_text or ("user" in grid_text and "password" not in grid_text):
                logger.debug("bootstrap_send_user", user=self.user)
                editable = [f for f in state.fields.values() if not f.protected]
                r = editable[0].row if editable else 0
                c = editable[0].col if editable else 0
                state = await step_input(self.user, row=r, col=c, gen=step_count)
                continue

            # State F: Password prompt
            if "password" in grid_text:
                logger.debug("bootstrap_send_password")
                editable = [f for f in state.fields.values() if not f.protected]
                r = editable[0].row if editable else 0
                c = editable[0].col if editable else 0
                state = await step_input(self.password, row=r, col=c, gen=step_count)
                continue

            # State G: Welcome / logon progress / broadcast / quote / *** prompt -> Press ENTER to dismiss
            if any(k in grid_text for k in ["welcome", "progress", "***", "broadcast", "quote", "ikj"]):
                logger.debug("bootstrap_dismiss_banner")
                state = await step_aid("ENTER", gen=step_count)
                continue

            # Default fallback: Send ENTER to advance
            logger.debug("bootstrap_advance_enter")
            state = await step_aid("ENTER", gen=step_count)

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
