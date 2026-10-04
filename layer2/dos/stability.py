"""Stage 2d: DOS Stability Engine for FreeDOS text-mode determinism.

Implements hardened two-phase settling:
1. Reaction window:
   - Evaluates whether screen hash has changed from previous_state.screen_hash.
   - Waits for first screen hash change from baseline (upper-bounded by reaction_timeout_ms).
   - If previous_state is None or action causes no visual change, smoothly proceeds to quiescence.
2. Quiescence window:
   - Resets quiescent frames and timer whenever screen_hash changes.
   - Requires screen_hash to remain continuously identical for quiescence_ms (300ms).
   - Accumulates quiescent_frames exclusively from the settled screen state.
3. Slow command handling:
   - If screen keeps scrolling/updating past max_wait_ms (e.g. dir /s), returns 'settled_by_timeout'
     gracefully with is_stable=True without raising an unhandled exception.
"""
import asyncio
import hashlib
import time
from typing import Any, Optional
import numpy as np
import structlog

from layer2.base import StabilityEngine
from schemas.state import RuntimeState, ScreenType, StabilityReport

logger = structlog.get_logger(__name__)


class DOSStabilityEngine(StabilityEngine):
    """Two-phase settling engine for FreeDOS screen and cursor stability."""

    def __init__(
        self,
        poll_interval_ms: int = 50,
        reaction_timeout_ms: int = 750,
        quiescence_ms: int = 300,
        max_wait_ms: int = 6000,
    ) -> None:
        self.poll_interval_ms = poll_interval_ms
        self.reaction_timeout_ms = reaction_timeout_ms
        self.quiescence_ms = quiescence_ms
        self.max_wait_ms = max_wait_ms

    async def wait_until_stable(
        self,
        driver: Any,
        reducer: Any,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
    ) -> tuple[RuntimeState, StabilityReport]:
        """Poll driver and state reducer through two-phase settling until stability criteria are met."""
        start_time = time.time()
        prev_hash = previous_state.screen_hash if previous_state else ""
        iterations = 0

        # Helper to capture a frame and compute screen hash quickly
        async def fetch_frame_and_hash() -> tuple[np.ndarray, str]:
            nonlocal iterations
            iterations += 1
            frame = await driver.screenshot()
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, :3]

            reader_result = reducer.reader.read_frame(frame)
            grid_lines = list(reader_result.raw_grid)
            # Mask ticking clock on row 24 (cols 65-79) if present to prevent clock jitter
            if len(grid_lines) == 25:
                grid_lines[24] = grid_lines[24][:65] + (" " * 15)
            grid_blob = "\n".join(grid_lines).encode("utf-8")
            h = hashlib.sha256(grid_blob).hexdigest()
            return frame, h

        # -------------------------------------------------------------
        # Phase 1: Reaction Window
        # Wait for first change from prev_hash (if provided)
        # -------------------------------------------------------------
        current_frame, current_hash = await fetch_frame_and_hash()
        reaction_deadline = start_time + (self.reaction_timeout_ms / 1000.0)

        if prev_hash:
            while current_hash == prev_hash and time.time() < reaction_deadline:
                await asyncio.sleep(self.poll_interval_ms / 1000.0)
                current_frame, current_hash = await fetch_frame_and_hash()

        reaction_elapsed_ms = (time.time() - start_time) * 1000.0
        hash_reacted = (current_hash != prev_hash) if prev_hash else True

        logger.debug(
            "dos_reaction_window_finished",
            reacted=hash_reacted,
            elapsed_ms=reaction_elapsed_ms,
            iterations=iterations,
        )

        # -------------------------------------------------------------
        # Phase 2: Quiescence Window
        # Only accumulate frames from the stable hash state
        # -------------------------------------------------------------
        quiescence_start = time.time()
        stable_hash = current_hash
        quiescent_frames: list[np.ndarray] = [current_frame]
        max_deadline = start_time + (self.max_wait_ms / 1000.0)

        while True:
            await asyncio.sleep(self.poll_interval_ms / 1000.0)
            current_frame, current_hash = await fetch_frame_and_hash()

            now = time.time()
            if current_hash == stable_hash:
                # Same hash: append frame
                quiescent_frames.append(current_frame)
                if len(quiescent_frames) > 8:
                    quiescent_frames.pop(0)

                # Check if quiescence duration met
                if (now - quiescence_start) * 1000.0 >= self.quiescence_ms:
                    total_elapsed_ms = (now - start_time) * 1000.0
                    report = StabilityReport(
                        is_stable=True,
                        method="two_phase_quiescence",
                        confidence=1.0,
                        delta_value=0.0,
                        iterations=iterations,
                        details={
                            "reaction_elapsed_ms": reaction_elapsed_ms,
                            "hash_reacted": hash_reacted,
                            "total_elapsed_ms": total_elapsed_ms,
                            "quiescent_ms": (now - quiescence_start) * 1000.0,
                        },
                    )

                    # Ensure we have >= 5 frames of the settled screen for cursor blink detection
                    while len(quiescent_frames) < 5:
                        await asyncio.sleep(0.07)
                        extra_frame, extra_hash = await fetch_frame_and_hash()
                        if extra_hash == stable_hash:
                            quiescent_frames.append(extra_frame)
                        else:
                            # Unexpected change during final sampling
                            stable_hash = extra_hash
                            quiescence_start = time.time()
                            quiescent_frames = [extra_frame]
                            break

                    if len(quiescent_frames) >= 5:
                        state = reducer.build_state_from_frames(
                            frames=quiescent_frames,
                            runtime_id=runtime_id,
                            generation=generation,
                            stability_report=report,
                            previous_state=previous_state,
                        )
                        logger.info(
                            "dos_stability_settled",
                            method=report.method,
                            total_elapsed_ms=total_elapsed_ms,
                            iterations=iterations,
                        )
                        return state, report
            else:
                # Screen changed — reset quiescence timer and frames
                stable_hash = current_hash
                quiescence_start = now
                quiescent_frames = [current_frame]

            # Slow command timeout check
            if now >= max_deadline:
                total_elapsed_ms = (now - start_time) * 1000.0
                report = StabilityReport(
                    is_stable=True,
                    method="settled_by_timeout",
                    confidence=0.85,
                    delta_value=0.5,
                    iterations=iterations,
                    details={
                        "reaction_elapsed_ms": reaction_elapsed_ms,
                        "hash_reacted": hash_reacted,
                        "total_elapsed_ms": total_elapsed_ms,
                        "reason": f"Screen continuous change exceeded max_wait_ms ({self.max_wait_ms}ms)",
                    },
                )

                state = reducer.build_state_from_frames(
                    frames=quiescent_frames if quiescent_frames else [current_frame],
                    runtime_id=runtime_id,
                    generation=generation,
                    stability_report=report,
                    previous_state=previous_state,
                )
                logger.warning(
                    "dos_stability_timeout",
                    method=report.method,
                    total_elapsed_ms=total_elapsed_ms,
                    iterations=iterations,
                )
                return state, report
