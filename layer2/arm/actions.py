"""Deterministic ARM semantic action mapping, execution, and verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from contextlib import AbstractAsyncContextManager
import re
from typing import Any, Mapping, Protocol

from schemas.pipeline import TransportFrame

from layer2.arm.abstractor import ARMStateAbstractor
from layer2.arm.models import (
    ARMExecutionState,
    ARMState,
    ARMStateDelta,
    CapturedValue,
    ObservationStatus,
    StabilityStatus,
)


_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ARMActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    EXECUTION_FAILED = "execution_failed"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    UNSTABLE = "unstable"
    POSTCONDITION_FAILED = "postcondition_failed"
    EXECUTION_UNCERTAIN = "execution_uncertain"


class ARMActionMappingError(ValueError):
    """A rejected intent or a precondition/evidence failure during mapping."""

    def __init__(self, status: ARMActionStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ARMSemanticActionIntent:
    """Caller-facing semantic intent; parameters never contain GDB commands."""

    action: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ARMExpectedPostcondition:
    request_id: str
    before_value: int
    expected_value: int
    value_type: str = "uint32"


@dataclass(frozen=True)
class ARMActionPlan:
    action: str
    target_symbol: str
    target_address: int
    store_instruction_address: int
    source_generation: int
    source_state_hash: str
    runtime_id: str
    firmware_sha256: str
    postcondition: ARMExpectedPostcondition


@dataclass(frozen=True)
class ARMExecutionEvidence:
    breakpoint_verified: bool
    breakpoint_cleanup_verified: bool
    breakpoint_stop_reason: str
    breakpoint_id: str | None
    instruction_address: int
    instruction_text: str
    single_step_verified: bool
    post_step_pc: int
    post_step_stop_reason: str | None


@dataclass(frozen=True)
class ARMActionResult:
    status: ARMActionStatus
    action: str
    message: str
    plan: ARMActionPlan | None = None
    before_state: ARMState | None = None
    after_state: ARMState | None = None
    delta: ARMStateDelta | None = None
    evidence: ARMExecutionEvidence | None = None
    observed_value: int | None = None
    execution_attempted: bool = False
    execution_may_have_occurred: bool = False
    cleanup_errors: tuple[str, ...] = ()
    primary_error: str | None = None
    breakpoint_insertion_ambiguous: bool = False

    def __post_init__(self) -> None:
        if self.status == ARMActionStatus.SUCCEEDED:
            if not (self.plan and self.before_state and self.after_state and self.delta and self.evidence):
                raise ValueError("successful ARM action result requires plan, before/after states, delta, and evidence")
            if not (self.execution_attempted and self.execution_may_have_occurred and self.observed_value is not None):
                raise ValueError("successful ARM action result requires execution and postcondition evidence")
            if not (self.evidence.breakpoint_verified and self.evidence.breakpoint_cleanup_verified
                    and self.evidence.single_step_verified):
                raise ValueError("successful ARM action result requires verified breakpoint cleanup and single-step evidence")
            self._validate_success_evidence()
        if self.status == ARMActionStatus.EXECUTION_UNCERTAIN and not (
            self.execution_attempted and self.execution_may_have_occurred
        ):
            raise ValueError("indeterminate ARM action result must record a possibly executed attempt")

    @property
    def succeeded(self) -> bool:
        return self.status == ARMActionStatus.SUCCEEDED

    def _validate_success_evidence(self) -> None:
        from layer2.arm.validation import validate_arm_state

        assert self.plan is not None and self.before_state is not None and self.after_state is not None
        assert self.delta is not None and self.evidence is not None and self.observed_value is not None
        plan, before, after, delta, evidence = self.plan, self.before_state, self.after_state, self.delta, self.evidence
        for state in (before, after):
            try:
                validate_arm_state(state)
            except ValueError as exc:
                raise ValueError(f"successful action contains an invalid ARM state: {exc}") from exc
        if self.action != plan.action or plan.action != "increment_counter":
            raise ValueError("successful action and plan actions must match the supported counter increment")
        if before.generation != plan.source_generation or before.state_hash != plan.source_state_hash:
            raise ValueError("successful action plan must be bound to its before state")
        if after.generation <= before.generation:
            raise ValueError("successful action after-state generation must advance")
        if after.source_frame_seq <= before.source_frame_seq:
            raise ValueError("successful action after-state frame sequence must advance")
        if before.runtime_id != after.runtime_id or before.runtime_id != plan.runtime_id:
            raise ValueError("successful action states must match the plan runtime identity")
        if (before.stability.status != StabilityStatus.STABLE or before.stability.stable is not True
                or after.stability.status != StabilityStatus.STABLE or after.stability.stable is not True):
            raise ValueError("successful action requires stable before/after state evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", plan.firmware_sha256):
            raise ValueError("successful action plan requires a verified firmware SHA-256")
        for state in (before, after):
            fingerprint = state.firmware_sha256
            if fingerprint.status != ObservationStatus.OBSERVED or fingerprint.value != plan.firmware_sha256:
                raise ValueError("successful action states must match the observed plan firmware fingerprint")
        if (plan.postcondition.value_type != "uint32"
                or not 0 <= plan.postcondition.before_value <= 0xFFFFFFFF
                or plan.postcondition.expected_value != ((plan.postcondition.before_value + 1) & 0xFFFFFFFF)):
            raise ValueError("successful action plan must declare a valid modulo-uint32 increment")
        before_memory = [item for item in before.memory if item.request_id == plan.postcondition.request_id]
        after_memory = [item for item in after.memory if item.request_id == plan.postcondition.request_id]
        before_decoded = [item for item in before.decoded_memory if item.request_id == plan.postcondition.request_id]
        after_decoded = [item for item in after.decoded_memory if item.request_id == plan.postcondition.request_id]
        if not all(len(items) == 1 for items in (before_memory, after_memory, before_decoded, after_decoded)):
            raise ValueError("successful action states must contain exactly one before/after counter observation")
        old_memory, new_memory = before_memory[0], after_memory[0]
        old_value, new_value = before_decoded[0], after_decoded[0]
        if (old_memory.symbol != plan.target_symbol or new_memory.symbol != plan.target_symbol
                or old_memory.address != plan.target_address or new_memory.address != plan.target_address
                or old_value.decode_status != ObservationStatus.OBSERVED
                or new_value.decode_status != ObservationStatus.OBSERVED
                or old_value.value_type != "uint32" or new_value.value_type != "uint32"
                or old_value.value != plan.postcondition.before_value
                or new_value.value != plan.postcondition.expected_value
                or self.observed_value != plan.postcondition.expected_value):
            raise ValueError("successful action counter values must match the plan and observed postcondition")
        if (delta.generation_from != before.generation or delta.generation_to != after.generation
                or delta.state_hash_from != before.state_hash or delta.state_hash_to != after.state_hash):
            raise ValueError("successful action delta generations and hashes must match the before/after states")
        memory_change = delta.memory_changes.get(plan.postcondition.request_id)
        if not isinstance(memory_change, dict) or any((
            memory_change.get("old_value") != old_value.value,
            memory_change.get("new_value") != new_value.value,
            memory_change.get("old_raw_bytes_hex") != old_memory.raw_bytes_hex,
            memory_change.get("new_raw_bytes_hex") != new_memory.raw_bytes_hex,
        )):
            raise ValueError("successful action delta must verify the raw and decoded counter transition")
        if (before.execution_state.status != ObservationStatus.OBSERVED
                or before.execution_state.value != ARMExecutionState.STOPPED
                or after.execution_state.status != ObservationStatus.OBSERVED
                or after.execution_state.value != ARMExecutionState.STOPPED):
            raise ValueError("successful action requires stopped before/after execution states")
        if (not evidence.breakpoint_id or evidence.breakpoint_stop_reason != "breakpoint-hit"
                or evidence.instruction_address != plan.store_instruction_address
                or not re.match(r"\s*str(?:\.\w+)?\b", evidence.instruction_text, re.IGNORECASE)):
            raise ValueError("successful action breakpoint evidence must match the planned store instruction")
        if (after.pc.status != ObservationStatus.OBSERVED
                or evidence.post_step_pc != after.pc.value
                or evidence.post_step_pc == plan.store_instruction_address):
            raise ValueError("successful action post-step PC evidence must match the after state and advance")
        if (after.stop_reason.status != ObservationStatus.OBSERVED
                or after.stop_reason.value != "end-stepping-range"
                or evidence.post_step_stop_reason != after.stop_reason.value):
            raise ValueError("successful action stop-reason evidence must match the after state")


class ARMActionRuntime(Protocol):
    """Narrow runtime capability contract; implementation details stay in L1."""

    async def resolve_symbol(self, symbol: str) -> int: ...

    async def find_store_instruction(self, counter_address: int, symbol: str = "main") -> int: ...

    async def run_to_breakpoint(self, location: str) -> None: ...

    async def run_to_address(self, address: int) -> None: ...

    async def step_instruction(self) -> None: ...

    async def read_frame(self) -> TransportFrame: ...

    def execution_transaction(self) -> AbstractAsyncContextManager[None]: ...


class ARMActionMapper:
    """Map only the configured counter increment intent to a verified ARM step."""

    def __init__(
        self,
        runtime: ARMActionRuntime,
        abstractor: ARMStateAbstractor,
        *,
        counter_symbol: str = "counter",
        counter_request_id: str = "counter",
        function_symbol: str = "main",
        max_observation_samples: int = 3,
    ) -> None:
        for label, value in (("counter_symbol", counter_symbol), ("function_symbol", function_symbol)):
            if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
                raise ValueError(f"{label} must be a simple firmware symbol identifier")
        if not isinstance(counter_request_id, str) or not counter_request_id:
            raise ValueError("counter_request_id must be non-empty")
        if max_observation_samples < abstractor.stability.policy.required_matching_samples:
            raise ValueError("max_observation_samples must allow the configured stability sample count")
        self.runtime = runtime
        self.abstractor = abstractor
        self.counter_symbol = counter_symbol
        self.counter_request_id = counter_request_id
        self.function_symbol = function_symbol
        self.max_observation_samples = max_observation_samples

    async def map_action(self, intent: ARMSemanticActionIntent, state: ARMState) -> ARMActionPlan:
        """Validate semantic intent and resolve a constrained, state-bound plan."""
        async with self.runtime.execution_transaction():
            return await self._map_action(intent, state)

    async def _map_action(self, intent: ARMSemanticActionIntent, state: ARMState) -> ARMActionPlan:
        if not isinstance(intent, ARMSemanticActionIntent):
            raise ValueError("intent must be ARMSemanticActionIntent")
        if intent.action != "increment_counter":
            raise ValueError(f"unsupported ARM semantic action: {intent.action!r}")
        if not isinstance(intent.parameters, Mapping) or set(intent.parameters) != {"symbol"}:
            raise ValueError("increment_counter requires exactly one parameter: symbol")
        if intent.parameters.get("symbol") != self.counter_symbol:
            raise ValueError(f"increment_counter only supports configured symbol {self.counter_symbol!r}")
        if not isinstance(state, ARMState):
            raise ValueError("current state must be ARMState")
        from layer2.arm.validation import validate_arm_state
        try:
            validate_arm_state(state)
        except ValueError as exc:
            raise ARMActionMappingError(ARMActionStatus.REJECTED, f"ARM state validation failed: {exc}") from exc
        if state.stability.status != StabilityStatus.STABLE or state.stability.stable is not True:
            raise ARMActionMappingError(ARMActionStatus.UNSTABLE, "increment_counter requires a stable current ARM state")
        if state.execution_state.status != ObservationStatus.OBSERVED:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "execution state evidence is unavailable")
        if state.execution_state.value != ARMExecutionState.STOPPED:
            raise ValueError("increment_counter requires stopped execution")
        if not state.runtime_id.strip():
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "runtime identity is unavailable")
        firmware = state.firmware_sha256
        if firmware.status != ObservationStatus.OBSERVED or not isinstance(firmware.value, str) or not re.fullmatch(r"[0-9a-f]{64}", firmware.value):
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "verified firmware SHA-256 is unavailable")

        matches = [item for item in state.memory if item.request_id == self.counter_request_id]
        decoded = [item for item in state.decoded_memory if item.request_id == self.counter_request_id]
        if len(matches) != 1 or len(decoded) != 1:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "configured counter observation is missing or ambiguous")
        memory, semantic = matches[0], decoded[0]
        if memory.status != ObservationStatus.OBSERVED or semantic.decode_status != ObservationStatus.OBSERVED:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "configured counter value is unavailable or failed decoding")
        if memory.symbol != self.counter_symbol or memory.address is None:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "configured counter observation has missing or ambiguous symbol metadata")
        if memory.size != 4 or semantic.value_type != "uint32" or semantic.value is None:
            raise ValueError("configured counter must be observed and decoded as uint32")
        if state.pc.status != ObservationStatus.OBSERVED:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "program counter evidence is unavailable")
        if not 0 <= semantic.value <= 0xFFFFFFFF:
            raise ValueError("configured counter value is outside uint32 range")

        try:
            resolved_address = await self.runtime.resolve_symbol(self.counter_symbol)
        except Exception as exc:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, f"configured counter symbol could not be resolved: {exc}") from exc
        if resolved_address != memory.address:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "configured counter symbol address conflicts with the observed memory address")
        try:
            store_address = await self.runtime.find_store_instruction(resolved_address, self.function_symbol)
        except Exception as exc:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, f"counter store instruction is missing or ambiguous: {exc}") from exc
        if isinstance(store_address, bool) or not isinstance(store_address, int) or not 0 <= store_address <= 0xFFFFFFFF:
            raise ARMActionMappingError(ARMActionStatus.EVIDENCE_UNAVAILABLE, "L1 returned an invalid counter store instruction address")

        expected = (semantic.value + 1) & 0xFFFFFFFF
        return ARMActionPlan(
            action="increment_counter",
            target_symbol=self.counter_symbol,
            target_address=resolved_address,
            store_instruction_address=store_address,
            source_generation=state.generation,
            source_state_hash=state.state_hash,
            runtime_id=state.runtime_id,
            firmware_sha256=firmware.value,
            postcondition=ARMExpectedPostcondition(self.counter_request_id, semantic.value, expected),
        )

    async def execute(self, intent: ARMSemanticActionIntent, state: ARMState) -> ARMActionResult:
        """Map once, execute once, capture fresh evidence, and verify without retry."""
        async with self.runtime.execution_transaction():
            return await self._execute_locked(intent, state)

    async def _execute_locked(self, intent: ARMSemanticActionIntent, state: ARMState) -> ARMActionResult:
        try:
            # The whole execute path already owns the runtime transaction.
            plan = await self._map_action(intent, state)
        except ARMActionMappingError as exc:
            return ARMActionResult(exc.status, getattr(intent, "action", "<invalid>"), str(exc), before_state=state if isinstance(state, ARMState) else None)
        except ValueError as exc:
            return ARMActionResult(ARMActionStatus.REJECTED, getattr(intent, "action", "<invalid>"), str(exc), before_state=state if isinstance(state, ARMState) else None)
        try:
            fresh_frame = await self.runtime.read_frame()
            fresh_observation = self.abstractor.parse_observation(fresh_frame)
            if fresh_observation.runtime_id != plan.runtime_id:
                return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action,
                                       "fresh observation runtime identity does not match the action plan", plan, state)
            if (fresh_observation.firmware_sha256.status != ObservationStatus.OBSERVED
                    or fresh_observation.firmware_sha256.value != plan.firmware_sha256):
                return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action,
                                       "fresh observation firmware fingerprint is unavailable or mismatched", plan, state)
            fresh_state, _ = self.abstractor.abstract_state(
                fresh_observation,
                state.generation + 1,
                previous_state=state,
            )
        except Exception as exc:
            return ARMActionResult(
                ARMActionStatus.EVIDENCE_UNAVAILABLE,
                plan.action,
                f"pre-execution state revalidation failed: {exc}",
                plan,
                state,
            )
        if fresh_state.state_hash != plan.source_state_hash:
            return ARMActionResult(
                ARMActionStatus.EVIDENCE_UNAVAILABLE,
                plan.action,
                "ARM semantic state changed while resolving the action plan",
                plan,
                state,
            )
        if fresh_state.runtime_id != plan.runtime_id:
            return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action,
                                   "fresh observation runtime identity does not match the action plan", plan, state)
        if (fresh_state.firmware_sha256.status != ObservationStatus.OBSERVED
                or fresh_state.firmware_sha256.value != plan.firmware_sha256):
            return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action,
                                   "fresh observation firmware fingerprint is unavailable or mismatched", plan, state)
        if fresh_state.stability.status != StabilityStatus.STABLE or fresh_state.stability.stable is not True:
            return ARMActionResult(
                ARMActionStatus.UNSTABLE,
                plan.action,
                "fresh pre-execution observation does not confirm stable state",
                plan,
                state,
            )
        try:
            # L1 validates the numeric location, confirms the breakpoint stop/PC, and
            # removes the temporary breakpoint before returning.
            await self.runtime.run_to_address(plan.store_instruction_address)
        except Exception as exc:
            cleanup_errors = tuple(getattr(exc, "cleanup_errors", ())) or tuple(
                note for note in getattr(exc, "__notes__", ()) if "cleanup" in note.lower() or "ambiguous" in note.lower()
            )
            primary_error = getattr(exc, "primary_error", None)
            ambiguous_insertion = bool(getattr(exc, "breakpoint_insertion_ambiguous", False))
            details = f"breakpoint execution outcome is uncertain: {exc}"
            if cleanup_errors and not any(item in details for item in cleanup_errors):
                details += "; cleanup: " + "; ".join(cleanup_errors)
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action,
                                   details,
                                   plan, state, execution_attempted=True, execution_may_have_occurred=True,
                                   cleanup_errors=cleanup_errors,
                                   primary_error=str(primary_error) if primary_error is not None else None,
                                   breakpoint_insertion_ambiguous=ambiguous_insertion)

        try:
            breakpoint_frame = await self.runtime.read_frame()
            breakpoint_observation = self.abstractor.parse_observation(breakpoint_frame)
            breakpoint_state, _ = self.abstractor.abstract_state(
                breakpoint_observation, state.generation + 1, previous_state=None
            )
        except Exception as exc:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, f"breakpoint was reached but follow-up evidence failed: {exc}", plan, state,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if breakpoint_state.pc.status != ObservationStatus.OBSERVED or breakpoint_state.current_instruction.status != ObservationStatus.OBSERVED:
            return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action, "breakpoint PC or instruction evidence is unavailable", plan, state)
        instruction = breakpoint_state.current_instruction.value
        if breakpoint_state.pc.value != plan.store_instruction_address or instruction.address != plan.store_instruction_address:
            return ARMActionResult(ARMActionStatus.EXECUTION_FAILED, plan.action, "target did not stop at the planned store instruction", plan, state)
        if not re.match(r"\s*str(?:\.\w+)?\b", instruction.text, re.IGNORECASE):
            return ARMActionResult(ARMActionStatus.EXECUTION_FAILED, plan.action, "planned instruction is not a store", plan, state)
        if breakpoint_state.stop_reason.status != ObservationStatus.OBSERVED:
            return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action, "breakpoint stop reason is unavailable", plan, state)
        if breakpoint_state.stop_reason.value != "breakpoint-hit":
            return ARMActionResult(ARMActionStatus.EXECUTION_FAILED, plan.action, "breakpoint frame does not confirm a breakpoint hit", plan, state)
        hit_value = breakpoint_state.breakpoint_hit.value if breakpoint_state.breakpoint_hit.status == ObservationStatus.OBSERVED else {}
        breakpoint_id = hit_value.get("breakpoint_id") if isinstance(hit_value, dict) else None
        if not breakpoint_id:
            return ARMActionResult(ARMActionStatus.EVIDENCE_UNAVAILABLE, plan.action,
                                   "breakpoint hit ID evidence is unavailable; refusing to step", plan, state)
        try:
            # L1 requires a normal end-stepping-range stop. An exception here is an
            # uncertain execution outcome; the action is deliberately never retried.
            await self.runtime.step_instruction()
        except Exception as exc:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, f"single-step outcome is uncertain: {exc}", plan, state,
                                   execution_attempted=True, execution_may_have_occurred=True)

        try:
            after_state, delta = await self._capture_stable_state(state)
        except Exception as exc:
            evidence = ARMExecutionEvidence(True, True, "breakpoint-hit", breakpoint_id, instruction.address, instruction.text, True, -1, None)
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, f"post-action observation failed after stepping: {exc}", plan, state, evidence=evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        evidence = ARMExecutionEvidence(
            True,
            True,
            "breakpoint-hit",
            breakpoint_id,
            instruction.address,
            instruction.text,
            True,
            after_state.pc.value if after_state.pc.status == ObservationStatus.OBSERVED else -1,
            after_state.stop_reason.value if after_state.stop_reason.status == ObservationStatus.OBSERVED else None,
        )
        if after_state.pc.status != ObservationStatus.OBSERVED:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "post-step PC evidence is unavailable", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if after_state.pc.value == plan.store_instruction_address:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "single-step did not provide evidence that execution advanced", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if after_state.stop_reason.status != ObservationStatus.OBSERVED:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "post-step stop reason is unavailable", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if after_state.stop_reason.value != "end-stepping-range":
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "post-step observation does not confirm a normal single-step stop", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if after_state.stability.status != StabilityStatus.STABLE or after_state.stability.stable is not True:
            return ARMActionResult(ARMActionStatus.UNSTABLE, plan.action, "post-action ARM state did not reach stable evidence", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        if after_state.execution_state.status != ObservationStatus.OBSERVED or after_state.execution_state.value != ARMExecutionState.STOPPED:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "post-action execution state is not confirmed stopped", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)

        after_values = [item for item in after_state.decoded_memory if item.request_id == plan.postcondition.request_id]
        if len(after_values) != 1 or after_values[0].decode_status != ObservationStatus.OBSERVED or after_values[0].value is None:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "post-action counter value is unavailable", plan, state, after_state, delta, evidence,
                                   execution_attempted=True, execution_may_have_occurred=True)
        observed_value = after_values[0].value
        if observed_value != plan.postcondition.expected_value:
            return ARMActionResult(ARMActionStatus.POSTCONDITION_FAILED, plan.action, f"expected counter {plan.postcondition.expected_value}, observed {observed_value}", plan, state, after_state, delta, evidence, observed_value,
                                   execution_attempted=True, execution_may_have_occurred=True)
        change = delta.memory_changes.get(plan.postcondition.request_id) if delta is not None else None
        if change is None or change.get("old_value") != plan.postcondition.before_value or change.get("new_value") != observed_value:
            return ARMActionResult(ARMActionStatus.EXECUTION_UNCERTAIN, plan.action, "ARMStateDelta does not verify the expected counter transition", plan, state, after_state, delta, evidence, observed_value,
                                   execution_attempted=True, execution_may_have_occurred=True)
        return ARMActionResult(ARMActionStatus.SUCCEEDED, plan.action, "counter increment and semantic postcondition verified", plan, state, after_state, delta, evidence, observed_value,
                               execution_attempted=True, execution_may_have_occurred=True)

    async def _capture_stable_state(self, baseline: ARMState) -> tuple[ARMState, ARMStateDelta | None]:
        state: ARMState | None = None
        delta: ARMStateDelta | None = None
        for index in range(self.max_observation_samples):
            frame = await self.runtime.read_frame()
            observation = self.abstractor.parse_observation(frame)
            state, delta = self.abstractor.abstract_state(
                observation,
                (baseline.generation + index + 2),
                previous_state=baseline,
            )
            if state.stability.status == StabilityStatus.STABLE:
                return state, delta
            delay = self.abstractor.stability.policy.sampling_interval_seconds
            if delay and index + 1 < self.max_observation_samples:
                await asyncio.sleep(delay)
        assert state is not None
        return state, delta

