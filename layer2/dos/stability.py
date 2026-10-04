"""Stage 2d: DOS Stability Engine for FreeDOS text-mode determinism.

Implements hardened two-phase settling with zero-overhead cursor detection:
1. Reaction window:
   - Evaluates whether screen hash has changed from previous_state.screen_hash.
   - Waits for first screen hash change from baseline (upper-bounded by reaction_timeout_ms).
   - If previous_state is None or action causes no visual change, smoothly proceeds to quiescence.
2. Quiescence window:
   - Evaluates consecutive frames for screen_hash stability over quiescence_ms (300ms) and >= 3 polls.
   - Resets quiescence timer whenever a visual screen transition is observed.
   - Collects frames directly during quiescence polling, eliminating any need for extra post-settling screenshots.
3. Status-Bar Clock Masking Heuristic:
   - Mask is applied ONLY when columns 65-79 of row 24 match a time pattern (HH:MM[:SS]).
4. Slow command timeout handling:
   - If screen keeps scrolling past max_wait_ms (e.g. dir /s), returns is_stable=False with method="timeout"
     and timed_out=True gracefully without raising an unhandled exception.
"""
import asyncio
import hashlib
import re
import time
from typing import Any, Optional
import numpy as np
import structlog

from layer2.base import StabilityEngine
from schemas.state import RuntimeState, ScreenType, StabilityReport

logger = structlog.get_logger(__name__)

# Heuristic time pattern for status-bar clocks (HH:MM or HH:MM:SS)
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


class DOSStabilityEngine(StabilityEngine):
    """Two-phase settling engine for FreeDOS screen and cursor stability."""

    def __init__(
        self,
        poll_interval_ms: int = 50,
        reaction_timeout_ms: int = 750,
        quiescence_ms: int = 300,
        max_wait_ms: int = 5000,
        min_quiescent_polls: int = 3,
        enable_cursor_sampling: bool = True,
    ) -> None:
        self.poll_interval_ms = poll_interval_ms
        self.reaction_timeout_ms = reaction_timeout_ms
        self.quiescence_ms = quiescence_ms
        self.max_wait_ms = max_wait_ms
        self.min_quiescent_polls = min_quiescent_polls
        self.enable_cursor_sampling = enable_cursor_sampling

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

        # Helper to capture a frame and compute screen hash
        async def fetch_frame_and_hash() -> tuple[np.ndarray, str]:
            nonlocal iterations
            iterations += 1
            frame = await driver.screenshot()
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, :3]

            reader_result = reducer.reader.read_frame(frame)
            grid_lines = list(reader_result.raw_grid)
            # Heuristic: Mask clock on row 24 (cols 65-79) ONLY when it matches a valid time pattern
            if len(grid_lines) == 25:
                clock_zone = grid_lines[24][65:80]
                if TIME_PATTERN.search(clock_zone):
                    grid_lines[24] = grid_lines[24][:65] + (" " * 15)

            grid_blob = "\n".join(grid_lines).encode("utf-8")
            h = hashlib.sha256(grid_blob).hexdigest()
            return frame, h

        # -------------------------------------------------------------
        # Phase 1: Reaction Window
        # Wait for first change from prev_hash (if provided)
        # -------------------------------------------------------------
        t_react_start = time.time()
        current_frame, current_hash = await fetch_frame_and_hash()
        reaction_deadline = start_time + (self.reaction_timeout_ms / 1000.0)

        if prev_hash:
            while current_hash == prev_hash and time.time() < reaction_deadline:
                await asyncio.sleep(self.poll_interval_ms / 1000.0)
                current_frame, current_hash = await fetch_frame_and_hash()

        reaction_elapsed_ms = (time.time() - t_react_start) * 1000.0
        hash_reacted = (current_hash != prev_hash) if prev_hash else True

        logger.debug(
            "dos_reaction_window_finished",
            reacted=hash_reacted,
            elapsed_ms=reaction_elapsed_ms,
            iterations=iterations,
        )

        # -------------------------------------------------------------
        # Phase 2: Quiescence Window (accumulates frames for cursor detection)
        # -------------------------------------------------------------
        phase2_start = time.time()
        stable_window_start = phase2_start
        stable_hash = current_hash
        quiescent_frames: list[np.ndarray] = [current_frame]
        max_deadline = start_time + (self.max_wait_ms / 1000.0)
        timed_out = False

        while True:
            await asyncio.sleep(self.poll_interval_ms / 1000.0)
            current_frame, current_hash = await fetch_frame_and_hash()

            now = time.time()
            if current_hash == stable_hash:
                quiescent_frames.append(current_frame)
                if len(quiescent_frames) > 5:
                    quiescent_frames.pop(0)

                # Settle when duration is met AND we have collected >= min_quiescent_polls
                if (now - stable_window_start) * 1000.0 >= self.quiescence_ms and len(quiescent_frames) >= self.min_quiescent_polls:
                    break
            else:
                # Screen changed — reset quiescence timer and accumulated frames
                stable_hash = current_hash
                stable_window_start = now
                quiescent_frames = [current_frame]

            # Check for slow command timeout
            if now >= max_deadline:
                timed_out = True
                break

        quiescence_elapsed_ms = (time.time() - phase2_start) * 1000.0

        # -------------------------------------------------------------
        # Phase 3: Zero-overhead Cursor Sampling with Single-Phase Fallback
        # Use frames ALREADY captured during quiescence polling.
        # If only one cursor blink phase is seen, capture up to 4 extra frames
        # before reporting the cursor as unknown / hidden.
        # -------------------------------------------------------------
        cursor_frames = list(quiescent_frames[-self.min_quiescent_polls:] if len(quiescent_frames) >= self.min_quiescent_polls else quiescent_frames)
        cursor_sampling_ms = 0.0

        if self.enable_cursor_sampling and hasattr(reducer, "cursor_detector"):
            obs, _ = reducer.cursor_detector.detect_cursor_from_frames(cursor_frames)
            if not obs.visible:
                t_extra_start = time.time()
                for _ in range(4):
                    await asyncio.sleep(0.08)  # Spaced at 80ms to traverse the 267ms toggle
                    extra_frame = await driver.screenshot()
                    if extra_frame.ndim == 3 and extra_frame.shape[2] == 4:
                        extra_frame = extra_frame[:, :, :3]
                    cursor_frames.append(extra_frame)
                    obs, _ = reducer.cursor_detector.detect_cursor_from_frames(cursor_frames)
                    if obs.visible:
                        break
                cursor_sampling_ms = (time.time() - t_extra_start) * 1000.0

        settle_time_ms = (time.time() - start_time) * 1000.0

        # -------------------------------------------------------------
        # Phase 4: State Reduction & Report Building
        # -------------------------------------------------------------
        t_build_start = time.time()

        if timed_out:
            report = StabilityReport(
                is_stable=False,
                method="timeout",
                confidence=0.5,
                delta_value=1.0,
                iterations=iterations,
                details={
                    "timed_out": True,
                    "reaction_elapsed_ms": reaction_elapsed_ms,
                    "quiescence_elapsed_ms": quiescence_elapsed_ms,
                    "cursor_sampling_ms": cursor_sampling_ms,
                    "total_elapsed_ms": settle_time_ms,
                    "iterations": iterations,
                    "reason": f"Screen continuous change exceeded max_wait_ms ({self.max_wait_ms}ms)",
                },
            )
        else:
            report = StabilityReport(
                is_stable=True,
                method="two_phase_quiescence",
                confidence=1.0,
                delta_value=0.0,
                iterations=iterations,
                details={
                    "timed_out": False,
                    "reaction_elapsed_ms": reaction_elapsed_ms,
                    "quiescence_elapsed_ms": quiescence_elapsed_ms,
                    "cursor_sampling_ms": cursor_sampling_ms,
                    "total_elapsed_ms": settle_time_ms,
                    "iterations": iterations,
                },
            )

        state = reducer.build_state_from_frames(
            frames=cursor_frames,
            runtime_id=runtime_id,
            generation=generation,
            stability_report=report,
            previous_state=previous_state,
        )

        state_build_ms = (time.time() - t_build_start) * 1000.0
        report.details["state_build_ms"] = state_build_ms

        logger.info(
            "dos_stability_completed",
            is_stable=report.is_stable,
            method=report.method,
            total_elapsed_ms=settle_time_ms,
            iterations=iterations,
        )

        return state, report
