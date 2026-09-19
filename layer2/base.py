import json
import os
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional
import structlog

from schemas.contracts import L2toL3HandoffPayload, validate_l2_to_l3_contract
from schemas.pipeline import Decoded3270Frame, ScreenObjectModel, TransportFrame
from schemas.state import RuntimeState, ScreenDelta, ScreenSnapshot, StabilityReport

if TYPE_CHECKING:
    from layer1.base import EnvironmentDriver
    from schemas.actions import ActionResult, CanonicalActionIntent

logger = structlog.get_logger(__name__)


class StateReducer(ABC):
    """Abstract interface for turning protocol transport frames into canonical RuntimeState."""

    @abstractmethod
    def parse_frame(self, frame: TransportFrame) -> Decoded3270Frame:
        """Stage 2a: Decode raw transport frame into structured orders and text."""
        pass

    @abstractmethod
    def build_object_model(self, decoded: Decoded3270Frame) -> ScreenObjectModel:
        """Stage 2b: Build semantic ScreenObjectModel (grid, field objects, cursor)."""
        pass

    @abstractmethod
    def reduce_state(
        self,
        som: ScreenObjectModel,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
    ) -> tuple[RuntimeState, Optional[ScreenDelta]]:
        """Stage 2c: Produce canonical RuntimeState and compute ScreenDelta against previous state."""
        pass

    def reduce_payload(
        self,
        som: ScreenObjectModel,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
        validate: bool = True,
    ) -> L2toL3HandoffPayload:
        """Stage 2c Sealed Handoff: Produce sealed L2toL3HandoffPayload and enforce contract validation."""
        state, delta = self.reduce_state(som, runtime_id, generation, previous_state)
        payload = L2toL3HandoffPayload(state=state, delta=delta)
        if validate:
            validate_l2_to_l3_contract(payload)
        return payload


class ActionLowerer(ABC):
    """Abstract interface for translating environment-agnostic CanonicalActionIntent into driver-specific execution."""

    @abstractmethod
    async def lower_and_execute(
        self,
        intent: Any,
        driver: Any,
        active_state: RuntimeState,
    ) -> Any:
        """Translate abstract action intent into protocol-level primitives and execute on driver."""
        pass


class StabilityEngine(ABC):
    """Abstract interface for environment settling and stability detection."""

    @abstractmethod
    async def wait_until_stable(
        self,
        driver: Any,
        reducer: Any,
        runtime_id: str,
        generation: int,
        previous_state: Optional[RuntimeState] = None,
    ) -> tuple[RuntimeState, StabilityReport]:
        """Poll driver and state reducer until stability criteria are met."""
        pass


class SnapshotManager:
    """Manages recording and replaying of ScreenSnapshots for unit test fixtures and regression analysis."""

    def __init__(self, snapshot_dir: str = "logs/snapshots") -> None:
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
