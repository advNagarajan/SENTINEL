"""Strict Handoff Contracts between Architectural Layers (L1->L2 and L2->L3)."""
from dataclasses import dataclass, field
import hashlib
import re
import time
from typing import Any, Optional
from schemas.events import RuntimeEvent
from schemas.pipeline import TransportFrame
from schemas.state import RuntimeState, ScreenDelta, ScreenSnapshot


class ContractValidationError(Exception):
    """Raised when an inter-layer handoff contract violates mandatory invariants."""
    pass


@dataclass
class L2toL3HandoffPayload:
    """The explicit, sealed contract payload passed from Layer 2 to Layer 3 on every turn."""
    state: RuntimeState
    delta: Optional[ScreenDelta] = None
    events: list[RuntimeEvent] = field(default_factory=list)
    snapshot: Optional[ScreenSnapshot] = None
    timestamp: float = field(default_factory=time.time)


def validate_l2_to_l3_contract(payload: L2toL3HandoffPayload) -> None:
    """Strict contract validator ensuring RuntimeState satisfies all Layer 3 compiler requirements.
    
    Invariants checked:
    1. state.runtime_id must be non-empty string.
    2. state.confidence_score must be between 0.0 and 1.0.
    3. state.screen_hash must be a valid 64-character SHA-256 hexadecimal string.
    4. state.text_grid / raw_grid must match state.screen_size['rows'].
    5. Every FieldEntry in state.fields must have row/col within grid bounds and length > 0.
    6. state.stability_report must have is_stable boolean matching state.is_stable.
    """
    state = payload.state

    if not state.runtime_id or not isinstance(state.runtime_id, str):
        raise ContractValidationError("RuntimeState.runtime_id must be a non-empty string.")

    if not (0.0 <= state.confidence_score <= 1.0):
        raise ContractValidationError(f"Invalid confidence_score: {state.confidence_score}. Must be in [0.0, 1.0].")

    if not state.screen_hash or len(state.screen_hash) != 64 or not re.match(r"^[0-9a-fA-F]{64}$", state.screen_hash):
        raise ContractValidationError(f"Invalid screen_hash: '{state.screen_hash}'. Must be a valid 64-char SHA-256 hex string.")

    expected_rows = state.screen_size.get("rows", 24)
    expected_cols = state.screen_size.get("cols", 80)

    if len(state.raw_grid) != expected_rows:
        raise ContractValidationError(f"raw_grid height {len(state.raw_grid)} does not match screen_size rows {expected_rows}.")

    for idx, row_str in enumerate(state.raw_grid):
        if len(row_str) != expected_cols:
            raise ContractValidationError(f"Row {idx} width {len(row_str)} does not match screen_size cols {expected_cols}.")

    for key, f in state.fields.items():
        if not (0 <= f.row < expected_rows):
            raise ContractValidationError(f"Field '{key}' row {f.row} out of bounds [0, {expected_rows-1}].")
        if not (0 <= f.col < expected_cols):
            raise ContractValidationError(f"Field '{key}' col {f.col} out of bounds [0, {expected_cols-1}].")
        if f.length <= 0:
            raise ContractValidationError(f"Field '{key}' length must be > 0.")

    if state.cursor["row"] < 0 or state.cursor["row"] >= expected_rows or state.cursor["col"] < 0 or state.cursor["col"] >= expected_cols:
        raise ContractValidationError(f"Cursor position {state.cursor} out of screen bounds ({expected_rows}x{expected_cols}).")

    if state.stability_report.is_stable != state.is_stable:
        raise ContractValidationError("Mismatch between stability_report.is_stable and state.is_stable.")


def validate_l3_to_l2_action(intent: Any, active_state: RuntimeState) -> None:
    """Strict contract validator ensuring downward CanonicalActionIntent is valid for active state.
    
    Invariants checked:
    1. Intent generation token and generation number must strictly match active RuntimeState.
    2. Any targeted field_id must exist in active_state and must NOT be protected.
    3. Input value length cannot exceed target field length.
    4. Action ID must be non-empty if triggering an action.
    """
    from schemas.actions import ActionType

    if not intent.ticket_id:
        raise ContractValidationError("Action intent must include a non-empty ticket_id.")

    if intent.generation != active_state.generation or intent.generation_token != active_state.generation_token:
        raise ContractValidationError(
            f"Stale action dispatch: intent token '{intent.generation_token}' (gen {intent.generation}) "
            f"does not match active state token '{active_state.generation_token}' (gen {active_state.generation})."
        )

    # Helper to find field by key or field_id
    def find_field(fid: str):
        if fid in active_state.fields:
            return active_state.fields[fid]
        for f in active_state.fields.values():
            if f.field_id == fid:
                return f
        return None

    if intent.intent_type in (ActionType.FILL_FIELD, ActionType.FILL_AND_SUBMIT):
        if intent.field_id:
            target = find_field(intent.field_id)
            if not target:
                raise ContractValidationError(f"Target field_id '{intent.field_id}' not found on active screen.")
            if target.protected:
                raise ContractValidationError(f"Target field '{intent.field_id}' is protected (read-only).")
            if intent.value is not None and len(intent.value) > target.length:
                raise ContractValidationError(
                    f"Value length ({len(intent.value)}) exceeds maximum length ({target.length}) for field '{intent.field_id}'."
                )

        if intent.fields:
            for fid, val in intent.fields.items():
                target = find_field(fid)
                if not target:
                    raise ContractValidationError(f"Target field '{fid}' not found in screen fields.")
                if target.protected:
                    raise ContractValidationError(f"Target field '{fid}' is protected (read-only).")
                if len(val) > target.length:
                    raise ContractValidationError(
                        f"Value length ({len(val)}) exceeds maximum length ({target.length}) for field '{fid}'."
                    )

    if intent.intent_type in (ActionType.TRIGGER_ACTION, ActionType.FILL_AND_SUBMIT):
        if not intent.action_id or not isinstance(intent.action_id, str):
            raise ContractValidationError("Action intent requires a valid non-empty action_id string.")
