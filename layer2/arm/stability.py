"""Conservative ARM stability assessment from repeated semantic states and MI evidence."""

from dataclasses import dataclass

from layer2.arm.models import ARMExecutionState, ARMStability, ARMState, ObservationStatus, StabilityStatus


@dataclass(frozen=True)
class ARMStabilityPolicy:
    """Criteria required before repeated ARM observations can be called stable."""

    required_matching_samples: int = 2
    require_stopped_execution: bool = True
    sampling_interval_seconds: float = 0.05

    def __post_init__(self) -> None:
        if self.required_matching_samples < 2:
            raise ValueError("ARM stability requires at least two matching samples")
        if self.sampling_interval_seconds < 0:
            raise ValueError("sampling interval must be non-negative")


class ARMStabilityAssessment:
    """Track consecutive semantic hashes, requiring stopped-state evidence by default."""

    def __init__(self, policy: ARMStabilityPolicy | None = None) -> None:
        self.policy = policy or ARMStabilityPolicy()
        self._candidate_hash: str | None = None
        self._matching_samples = 0
        self._last_frame_seq: int | None = None

    def assess(self, state: ARMState) -> ARMStability:
        if self._last_frame_seq is not None and state.source_frame_seq <= self._last_frame_seq:
            self._reset()
            return ARMStability(
                status=StabilityStatus.UNSTABLE,
                stable=False,
                method="stale_frame_sequence",
                reason="Frame sequence must strictly increase between stability samples",
                samples=1,
            )
        self._last_frame_seq = state.source_frame_seq
        execution = state.execution_state
        if execution.status != ObservationStatus.OBSERVED:
            self._reset()
            return ARMStability(
                status=StabilityStatus.UNKNOWN,
                stable=None,
                method="missing_execution_evidence",
                reason="GDB/MI did not provide reliable running/stopped evidence; stability cannot be established",
                samples=1,
            )
        if execution.value == ARMExecutionState.RUNNING:
            self._reset()
            return ARMStability(
                status=StabilityStatus.UNSTABLE,
                stable=False,
                method="execution_state",
                reason="GDB/MI reports that the target is running",
                samples=1,
            )
        if self.policy.require_stopped_execution and execution.value != ARMExecutionState.STOPPED:
            self._reset()
            return ARMStability(
                status=StabilityStatus.UNKNOWN,
                stable=None,
                method="missing_stopped_evidence",
                reason="The stability policy requires observed stopped-state evidence",
                samples=1,
            )

        if state.state_hash == self._candidate_hash:
            self._matching_samples += 1
        else:
            changed = self._candidate_hash is not None
            self._candidate_hash = state.state_hash
            self._matching_samples = 1
            if changed:
                return ARMStability(
                    status=StabilityStatus.UNSTABLE,
                    stable=False,
                    method="semantic_state_changed",
                    reason="Relevant semantic ARM values changed between consecutive samples",
                    samples=1,
                )

        if self._matching_samples >= self.policy.required_matching_samples:
            return ARMStability(
                status=StabilityStatus.STABLE,
                stable=True,
                method="repeated_semantic_state_while_stopped",
                reason="Consecutive semantic state hashes match and GDB/MI reports the target stopped",
                samples=self._matching_samples,
            )
        return ARMStability(
            status=StabilityStatus.UNKNOWN,
            stable=None,
            method="insufficient_matching_samples",
            reason=(
                f"Need {self.policy.required_matching_samples} consecutive matching semantic samples; "
                f"have {self._matching_samples}"
            ),
            samples=self._matching_samples,
        )

    def _reset(self) -> None:
        self._candidate_hash = None
        self._matching_samples = 0
