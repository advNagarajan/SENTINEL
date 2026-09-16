"""Layer 2 Action Lowerer translating CanonicalActionIntent into protocol-level driver actions."""
import time
from typing import Any, Optional
import structlog

from layer1.base import EnvironmentDriver
from layer1.tn3270.constants import AID_MAP
from layer1.tn3270.downward import TN3270DownwardWriter
from layer2.base import ActionLowerer
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import validate_l3_to_l2_action
from schemas.state import FieldEntry, RuntimeState

logger = structlog.get_logger(__name__)


class TN3270ActionLowerer(ActionLowerer):
    """Translates generic CanonicalActionIntent into TN3270 SBA buffer orders and AID transmissions."""

    def __init__(self) -> None:
        self.downward = TN3270DownwardWriter()

    def _resolve_field(self, field_id: str, state: RuntimeState) -> Optional[FieldEntry]:
        """Resolve a field by dictionary key or FieldEntry.field_id attribute."""
        if field_id in state.fields:
            return state.fields[field_id]
        for f in state.fields.values():
            if f.field_id == field_id:
                return f
        return None

    async def lower_and_execute(
        self,
        intent: CanonicalActionIntent,
        driver: EnvironmentDriver,
        active_state: RuntimeState,
    ) -> ActionResult:
        """Validate intent against active state and execute on the TN3270 driver."""
        start_time = time.time()

        try:
            # 1. Enforce strict downward contract
            validate_l3_to_l2_action(intent, active_state)

            logger.info(
                "l2_lowering_action",
                layer="layer2",
                ticket_id=intent.ticket_id,
                intent_type=intent.intent_type.value,
                generation=intent.generation,
            )

            if intent.intent_type == ActionType.TRIGGER_ACTION:
                aid_name = (intent.action_id or "ENTER").upper()
                cursor_row = active_state.cursor.get("row", 0)
                cursor_col = active_state.cursor.get("col", 0)
                packet = self.downward.build_aid_packet(aid_name, cursor_row=cursor_row, cursor_col=cursor_col)
                await driver.write_raw(packet)

            elif intent.intent_type == ActionType.FILL_FIELD:
                if not intent.field_id:
                    raise ValueError("FILL_FIELD requires a valid field_id.")
                target = self._resolve_field(intent.field_id, active_state)
                if not target:
                    raise ValueError(f"Field '{intent.field_id}' not found on active screen.")

                aid_name = (intent.action_id or "ENTER").upper()
                packet = self.downward.build_field_input_packet(
                    text=intent.value or "",
                    row=target.row,
                    col=target.col,
                    aid_name=aid_name,
                )
                await driver.write_raw(packet)

            elif intent.intent_type == ActionType.FILL_AND_SUBMIT:
                aid_name = (intent.action_id or "ENTER").upper()
                if intent.fields:
                    # Multi-field write
                    fields_data = []
                    for fid, val in intent.fields.items():
                        target = self._resolve_field(fid, active_state)
                        if not target:
                            raise ValueError(f"Field '{fid}' not found on active screen.")
                        fields_data.append((target.row, target.col, val))
                    packet = self.downward.build_multi_field_packet(
                        fields_data=fields_data,
                        aid_name=aid_name,
                        cursor_row=active_state.cursor.get("row", 0),
                        cursor_col=active_state.cursor.get("col", 0),
                    )
                    await driver.write_raw(packet)
                elif intent.field_id:
                    # Single-field write
                    target = self._resolve_field(intent.field_id, active_state)
                    if not target:
                        raise ValueError(f"Field '{intent.field_id}' not found on active screen.")
                    packet = self.downward.build_field_input_packet(
                        text=intent.value or "",
                        row=target.row,
                        col=target.col,
                        aid_name=aid_name,
                    )
                    await driver.write_raw(packet)
                else:
                    # Bare submit with no field modifications
                    packet = self.downward.build_aid_packet(
                        aid_name,
                        cursor_row=active_state.cursor.get("row", 0),
                        cursor_col=active_state.cursor.get("col", 0),
                    )
                    await driver.write_raw(packet)

            elif intent.intent_type == ActionType.SEND_RAW_KEYS:
                raw_bytes = (intent.raw_keys or "").encode("cp037")
                await driver.write_raw(raw_bytes)

            else:
                raise ValueError(f"Unsupported intent type: {intent.intent_type}")

            duration_ms = (time.time() - start_time) * 1000.0
            return ActionResult(
                ticket_id=intent.ticket_id,
                success=True,
                generation_before=active_state.generation,
                generation_after=active_state.generation + 1,
                screen_hash_before=active_state.screen_hash,
                screen_hash_after="",  # Will be populated after post-action stability
                execution_time_ms=duration_ms,
                details={"intent_type": intent.intent_type.value},
            )

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            logger.error("l2_action_execution_failed", error=str(exc), ticket_id=intent.ticket_id)
            return ActionResult(
                ticket_id=intent.ticket_id,
                success=False,
                generation_before=active_state.generation,
                generation_after=active_state.generation,
                screen_hash_before=active_state.screen_hash,
                screen_hash_after=active_state.screen_hash,
                execution_time_ms=duration_ms,
                error_message=str(exc),
            )
