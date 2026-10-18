"""Stage 2b Action Lowerer: Translates CanonicalActionIntent into DOSDriver keyboard events."""
import asyncio
import time
from typing import Any, Optional
import structlog

from layer1.base import EnvironmentDriver
from layer2.base import ActionLowerer
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import validate_l3_to_l2_action
from schemas.state import FieldEntry, RuntimeState
from schemas.observability import EventOutcome, EventType, SourceLayer
from services.observability_events import submit_layer_event

logger = structlog.get_logger(__name__)


class DOSActionLowerer(ActionLowerer):
    """Translates generic CanonicalActionIntent into FreeDOS driver keyboard operations."""

    def __init__(self, observability_producer: Any = None) -> None:
        self._observability_producer = observability_producer

    def set_observability_producer(self, producer: Any) -> None:
        self._observability_producer = producer

    async def lower_and_execute(
        self,
        intent: CanonicalActionIntent,
        driver: EnvironmentDriver,
        active_state: RuntimeState,
    ) -> ActionResult:
        """Validate intent against active state and execute on the DOS driver."""
        start_time = time.time()
        input_attempted = False

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
                input_attempted = True
                await dos_driver.press_keys([aid_name])

            elif intent.intent_type == ActionType.FILL_FIELD:
                text = intent.value or ""
                if text:
                    input_attempted = True
                    await dos_driver.type_text(text)
                if intent.action_id:
                    input_attempted = True
                    await dos_driver.press_keys([intent.action_id])

            elif intent.intent_type == ActionType.FILL_AND_SUBMIT:
                aid_name = intent.action_id or "ENTER"
                if intent.fields:
                    # Multi-field write
                    for val in intent.fields.values():
                        if val:
                            input_attempted = True
                            await dos_driver.type_text(val)
                elif intent.value:
                    input_attempted = True
                    await dos_driver.type_text(intent.value)

                if aid_name:
                    input_attempted = True
                    await dos_driver.press_keys([aid_name])

            elif intent.intent_type == ActionType.SEND_RAW_KEYS:
                keys = (intent.raw_keys or "").split()
                if keys:
                    input_attempted = True
                    await dos_driver.press_keys(keys)

            else:
                raise ValueError(f"Unsupported intent type: {intent.intent_type}")

            duration_ms = (time.time() - start_time) * 1000.0
            self._submit_driver_event(intent, active_state, dos_driver.__class__.__name__, EventOutcome.SUCCESS, duration_ms)
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

        except asyncio.CancelledError:
            duration_ms = (time.time() - start_time) * 1000.0
            if input_attempted:
                self._submit_driver_event(intent, active_state, dos_driver.__class__.__name__, EventOutcome.CANCELLED, duration_ms)
            raise
        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            if input_attempted:
                self._submit_driver_event(intent, active_state, dos_driver.__class__.__name__, EventOutcome.FAILURE, duration_ms)
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

    def _submit_driver_event(
        self,
        intent: CanonicalActionIntent,
        active_state: RuntimeState,
        driver_component: str,
        outcome: EventOutcome,
        duration_ms: float,
    ) -> None:
        submit_layer_event(
            self._observability_producer,
            event_type=EventType.DRIVER_INPUT_SENT,
            outcome=outcome,
            source_layer=SourceLayer.LAYER1,
            source_component=driver_component,
            ticket_id=intent.ticket_id,
            generation_before=active_state.generation,
            duration_ms=duration_ms,
            metadata={"action_id": intent.intent_type.value},
        )
