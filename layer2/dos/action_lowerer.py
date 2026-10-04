"""Stage 2b Action Lowerer: Translates CanonicalActionIntent into DOSDriver keyboard events."""
import time
from typing import Any, Optional
import structlog

from layer1.base import EnvironmentDriver
from layer2.base import ActionLowerer
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import validate_l3_to_l2_action
from schemas.state import FieldEntry, RuntimeState

logger = structlog.get_logger(__name__)


class DOSActionLowerer(ActionLowerer):
    """Translates generic CanonicalActionIntent into FreeDOS driver keyboard operations."""

    def __init__(self) -> None:
        pass

    async def lower_and_execute(
        self,
        intent: CanonicalActionIntent,
        driver: EnvironmentDriver,
        active_state: RuntimeState,
    ) -> ActionResult:
        """Validate intent against active state and execute on the DOS driver."""
        start_time = time.time()

        try:
            # 1. Enforce strict downward contract validation
            validate_l3_to_l2_action(intent, active_state)

            logger.info(
                "dos_lowering_action",
                layer="layer2",
                ticket_id=intent.ticket_id,
                intent_type=intent.intent_type.value,
                generation=intent.generation,
            )

            # Ensure driver provides DOS keyboard methods
            dos_driver: Any = driver

            if intent.intent_type == ActionType.TRIGGER_ACTION:
                aid_name = (intent.action_id or "ENTER").strip()
                await dos_driver.press_keys([aid_name])

            elif intent.intent_type == ActionType.FILL_FIELD:
                text = intent.value or ""
                if text:
                    await dos_driver.type_text(text)
                if intent.action_id:
                    await dos_driver.press_keys([intent.action_id])

            elif intent.intent_type == ActionType.FILL_AND_SUBMIT:
                aid_name = intent.action_id or "ENTER"
                if intent.fields:
                    # Multi-field write
                    for val in intent.fields.values():
                        if val:
                            await dos_driver.type_text(val)
                elif intent.value:
                    await dos_driver.type_text(intent.value)

                if aid_name:
                    await dos_driver.press_keys([aid_name])

            elif intent.intent_type == ActionType.SEND_RAW_KEYS:
                keys = (intent.raw_keys or "").split()
                if keys:
                    await dos_driver.press_keys(keys)

            else:
                raise ValueError(f"Unsupported intent type: {intent.intent_type}")

            duration_ms = (time.time() - start_time) * 1000.0
            return ActionResult(
                ticket_id=intent.ticket_id,
                success=True,
                generation_before=active_state.generation,
                generation_after=active_state.generation + 1,
                screen_hash_before=active_state.screen_hash,
                screen_hash_after="",  # Populated after post-action stability
                execution_time_ms=duration_ms,
                details={"intent_type": intent.intent_type.value},
            )

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            logger.error("dos_action_execution_failed", error=str(exc), ticket_id=intent.ticket_id)
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
