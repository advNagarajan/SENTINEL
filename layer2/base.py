from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional
from schemas.contracts import L2toL3HandoffPayload, validate_l2_to_l3_contract
from schemas.pipeline import Decoded3270Frame, ScreenObjectModel, TransportFrame
from schemas.state import RuntimeState, ScreenDelta

if TYPE_CHECKING:
    from layer1.base import EnvironmentDriver
    from schemas.actions import ActionResult, CanonicalActionIntent


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
