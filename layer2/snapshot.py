"""Stage 2e: Snapshot Manager for capturing, storing, and replaying ScreenSnapshots."""
import json
import os
import time
from typing import Any, Optional
import structlog

from schemas.pipeline import Decoded3270Frame, TransportFrame
from schemas.state import RuntimeState, ScreenSnapshot

logger = structlog.get_logger(__name__)


class SnapshotManager:
    """Manages recording and replaying of ScreenSnapshots for unit test fixtures and regression analysis."""

    def __init__(self, snapshot_dir: str = "logs/snapshots"):
        self.snapshot_dir = snapshot_dir
        os.makedirs(self.snapshot_dir, exist_ok=True)

    def capture_snapshot(
        self,
        frame: TransportFrame,
        state: RuntimeState,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScreenSnapshot:
        snapshot_id = f"snap_{state.runtime_id}_gen{state.generation}_{int(time.time() * 1000)}"
        snapshot = ScreenSnapshot(
            snapshot_id=snapshot_id,
            runtime_id=state.runtime_id,
            generation=state.generation,
            raw_bytes=frame.raw_payload,
            screen_hash=state.screen_hash,
            state=state,
            metadata=metadata or {},
            timestamp=time.time(),
        )

        # Persist snapshot metadata and raw bytes
        file_path = os.path.join(self.snapshot_dir, f"{snapshot_id}.json")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                snap_dict = {
                    "snapshot_id": snapshot.snapshot_id,
                    "runtime_id": snapshot.runtime_id,
                    "generation": snapshot.generation,
                    "raw_bytes_hex": frame.raw_payload.hex(),
                    "screen_hash": snapshot.screen_hash,
                    "title": state.title,
                    "fields_count": len(state.fields),
                    "timestamp": snapshot.timestamp,
                    "raw_grid": state.raw_grid,
                }
                json.dump(snap_dict, f, indent=2)
            logger.info("Saved ScreenSnapshot", snapshot_id=snapshot_id, path=file_path)
        except Exception as e:
            logger.error("Failed to save ScreenSnapshot", error=str(e))

        return snapshot
