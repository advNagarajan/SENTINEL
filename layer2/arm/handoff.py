"""ARM-specific L2 output envelope; intentionally separate from IBM L2→L3."""

from dataclasses import dataclass, field
import time

from layer2.arm.models import ARMState, ARMStateDelta


@dataclass(frozen=True)
class ARMStateHandoff:
    """Typed ARM L2 output for a future ARM-aware L3 boundary."""

    state: ARMState
    delta: ARMStateDelta | None = None
    timestamp: float = field(default_factory=time.time)
