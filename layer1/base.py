"""Abstract base class for all Layer 1 environment drivers."""
from abc import ABC, abstractmethod
from typing import Callable, Optional
from schemas.events import RuntimeEvent
from schemas.pipeline import TransportFrame


class EnvironmentDriver(ABC):
    """Abstract interface for low-level environment connection, clock control, and framed I/O."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the target environment."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly close connection."""
        pass

    @abstractmethod
    async def freeze(self) -> None:
        """Halt execution / buffer incoming stream."""
        pass

    @abstractmethod
    async def unfreeze(self) -> None:
        """Resume execution / release buffer."""
        pass

    @abstractmethod
    async def read_frame(self) -> TransportFrame:
        """Return the next framed protocol record from the target."""
        pass

    @abstractmethod
    async def write_raw(self, data: bytes) -> None:
        """Inject raw command/keystroke bytes into the target."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if connection is alive and healthy."""
        pass

    @abstractmethod
    def add_event_listener(self, listener: Callable[[RuntimeEvent], None]) -> None:
        """Register a callback for runtime events emitted by the driver."""
        pass
