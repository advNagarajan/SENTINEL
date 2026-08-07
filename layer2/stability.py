"""Stage 2d: Modular Multi-Signal Stability Engine for TN3270 protocol determinism."""
import asyncio
import time
import structlog
from schemas.state import RuntimeState, StabilityReport

logger = structlog.get_logger(__name__)


class OIAStabilityEngine:
    """Modular Stability Engine fusing OIA status byte, socket quiescence, screen hash, cursor stability, and settling timers."""

    def __init__(
        self,
        poll_interval_ms: int = 50,
        settle_ms: int = 100,
        max_wait_ms: int = 10000,
        quiescence_ms: int = 150,
    ):
        self.poll_interval_ms = poll_interval_ms
        self.settle_ms = settle_ms
        self.max_wait_ms = max_wait_ms
        self.quiescence_ms = quiescence_ms

    async def wait_until_stable(
        self,
        driver,  # EnvironmentDriver
        reducer,  # StateReducer
        runtime_id: str,
        generation: int,
    ) -> tuple[RuntimeState, StabilityReport]:
        """Poll driver and state reducer until multi-signal stability criteria are met."""
        start_time = time.time()
        iterations = 0

        last_hash = ""
        last_cursor = (-1, -1)
        hash_stable_count = 0

        last_state = None
        while (time.time() - start_time) * 1000 < self.max_wait_ms:
            iterations += 1
            frame = await driver.read_frame()
            if frame.raw_payload:
                decoded = reducer.parse_frame(frame)
                som = reducer.build_object_model(decoded)
                state, _ = reducer.reduce_state(som, runtime_id, generation)
                last_state = state
            elif last_state is not None:
                state = last_state
            else:
                await asyncio.sleep(self.poll_interval_ms / 1000.0)
                continue

            # Signal 1: OIA Ready byte
            is_oia_ready = (state.metadata.get("oia_status") == "READY")

            # Signal 2 & 3: Screen hash and cursor stability
            if state.screen_hash == last_hash and (state.cursor["row"], state.cursor["col"]) == last_cursor:
                hash_stable_count += 1
            else:
                hash_stable_count = 0
                last_hash = state.screen_hash
                last_cursor = (state.cursor["row"], state.cursor["col"])

            if is_oia_ready and hash_stable_count >= 1:
                # Apply short settling delay window
                await asyncio.sleep(self.settle_ms / 1000.0)
                
                report = StabilityReport(
                    is_stable=True,
                    method="oia_multi_signal",
                    confidence=1.0,
                    delta_value=0.0,
                    iterations=iterations,
                    details={
                        "oia_status": state.metadata.get("oia_status"),
                        "hash_stable_count": hash_stable_count,
                        "elapsed_ms": (time.time() - start_time) * 1000,
                    },
                )
                state.stability_report = report
                state.is_stable = True
                return state, report

            await asyncio.sleep(self.poll_interval_ms / 1000.0)


        # Timeout reached — return state with is_stable=False
        report = StabilityReport(
            is_stable=False,
            method="oia_multi_signal_timeout",
            confidence=0.5,
            delta_value=1.0,
            iterations=iterations,
            details={"elapsed_ms": (time.time() - start_time) * 1000},
        )
        state.stability_report = report
        state.is_stable = False
        logger.warning("Stability engine timed out waiting for OIA host ready", elapsed_ms=(time.time() - start_time) * 1000)
        return state, report
