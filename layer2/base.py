"""Abstract base class for State Reducers in Layer 2."""
from abc import ABC, abstractmethod
from typing import Optional
from schemas.pipeline import Decoded3270Frame, ScreenObjectModel, TransportFrame
from schemas.state import RuntimeState, ScreenDelta


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
