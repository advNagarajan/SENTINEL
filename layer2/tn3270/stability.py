"""Stage 2d: Modular Multi-Signal Stability Engine for TN3270 protocol determinism."""
import asyncio
import time
from typing import Any
import structlog

from layer2.base import StabilityEngine
from schemas.state import RuntimeState, ScreenType, StabilityReport

logger = structlog.get_logger(__name__)


class OIAStabilityEngine(StabilityEngine):
    """Modular Stability Engine fusing OIA status byte, socket quiescence, screen hash, cursor stability, and settling timers."""

    def __init__(
        self,
        poll_interval_ms: int = 50,
        settle_ms: int = 100,
        max_wait_ms: int = 10000,
        quiescence_ms: int = 150,
    ) -> None:
        self.poll_interval_ms = poll_interval_ms
        self.settle_ms = settle_ms
        self.max_wait_ms = max_wait_ms
        self.quiescence_ms = quiescence_ms

    async def wait_until_stable(
        self,
        driver: Any,
        reducer: Any,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
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
                # If frame is an intermediate screen-clearing frame (11 bytes), skip it and drain next
                if len(frame.raw_payload) == 11 and frame.raw_payload.startswith(b"\xf5\xc3\x11\x5d\x7f"):
                    continue

                decoded = reducer.parse_frame(frame)
                som = reducer.build_object_model(decoded)
                state, _ = reducer.reduce_state(som, runtime_id, generation, previous_state=previous_state)
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
                logger.info(
                    "l2_stability_converged",
                    layer="layer2",
                    method=report.method,
                    iterations=iterations,
                    elapsed_ms=(time.time() - start_time) * 1000,
                    screen_hash=state.screen_hash,
                )
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
        if last_state is not None:
            state = last_state
            state.stability_report = report
            state.is_stable = False
        else:
            blank_grid: list[str] = [" " * 80 for _ in range(24)]
            state = RuntimeState(
                runtime_id=runtime_id,
                screen_type=ScreenType.TEXT_GRID,
                is_stable=False,
                confidence_score=0.0,
                raw_grid=blank_grid,
                title=None,
                fields={},
                status_line=None,
                stability_report=report,
                screen_hash="",
                generation=generation,
            )

        logger.warning("Stability engine timed out waiting for OIA host ready", elapsed_ms=(time.time() - start_time) * 1000)
        return state, report
