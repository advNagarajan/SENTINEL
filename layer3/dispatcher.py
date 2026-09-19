"""Layer 3 Action Dispatcher: Validates agent tool calls, creates CanonicalActionIntents,
and coordinates downward execution with Layer 2 ActionLowerer and post-action stability settling.
"""
import time
from typing import Any, Optional
import uuid
import structlog

from layer1.base import EnvironmentDriver
from layer2.base import ActionLowerer, StateReducer
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import ContractValidationError, L2toL3HandoffPayload, validate_l3_to_l2_action
from schemas.state import RuntimeState, ScreenDelta

logger = structlog.get_logger(__name__)


class ActionDispatcher:
    """Dispatches validated agent actions downward and manages post-action state settling."""

    def __init__(
        self,
        driver: EnvironmentDriver,
        reducer: StateReducer,
        action_lowerer: ActionLowerer,
        stability_engine: Any,
    ) -> None:
        self.driver = driver
        self.reducer = reducer
        self.action_lowerer = action_lowerer
        self.stability = stability_engine

    def create_intent(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        active_state: RuntimeState,
    ) -> CanonicalActionIntent:
        """Translate a validated tool invocation into a CanonicalActionIntent."""
        ticket_id = f"ticket_{uuid.uuid4().hex[:12]}"
        gen_token = arguments.get("generation_token", "")

        if tool_name == "set_field_and_submit":
            return CanonicalActionIntent(
                intent_type=ActionType.FILL_AND_SUBMIT,
                generation_token=gen_token,
                generation=active_state.generation,
                ticket_id=ticket_id,
                field_id=arguments.get("field_id"),
                value=arguments.get("value", ""),
                action_id=arguments.get("action", "ENTER"),
            )

        elif tool_name == "fill_form_and_submit":
            return CanonicalActionIntent(
                intent_type=ActionType.FILL_AND_SUBMIT,
                generation_token=gen_token,
                generation=active_state.generation,
                ticket_id=ticket_id,
                fields=arguments.get("fields", {}),
                action_id=arguments.get("action", "ENTER"),
            )

        elif tool_name == "trigger_action":
            return CanonicalActionIntent(
                intent_type=ActionType.TRIGGER_ACTION,
                generation_token=gen_token,
                generation=active_state.generation,
                ticket_id=ticket_id,
                action_id=arguments.get("action_id", "ENTER"),
            )

        else:
            raise ValueError(f"Unknown downward tool name: '{tool_name}'")

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        active_payload: L2toL3HandoffPayload,
    ) -> tuple[ActionResult, L2toL3HandoffPayload]:
        """Validate, execute action downward, and wait for environment stability."""
        active_state = active_payload.state

        # 1. Build CanonicalActionIntent
        intent = self.create_intent(tool_name, arguments, active_state)

        # 2. Strict contract validation before touching any driver or lowerer
        validate_l3_to_l2_action(intent, active_state)

        logger.info(
            "l3_dispatching_action",
            layer="layer3",
            tool_name=tool_name,
            ticket_id=intent.ticket_id,
            generation=active_state.generation,
        )

        # 3. Downward propagation: Lower action to L2 & L1
        action_result: ActionResult = await self.action_lowerer.lower_and_execute(
            intent=intent,
            driver=self.driver,
            active_state=active_state,
        )

        if not action_result.success:
            return action_result, active_payload

        # 4. Post-action settling via L2 Stability Engine
        next_generation = active_state.generation + 1
        new_state, report = await self.stability.wait_until_stable(
            driver=self.driver,
            reducer=self.reducer,
            runtime_id=self.driver.runtime_id,
            generation=next_generation,
            previous_state=active_state,
        )

        # 5. Build sealed L2 -> L3 handoff payload with ScreenDelta
        changed_fields: dict[str, dict[str, str]] = {}
        for name, field_entry in new_state.fields.items():
            prev_field = active_state.fields.get(name)
            if prev_field and prev_field.value != field_entry.value:
                changed_fields[name] = {"old": prev_field.value, "new": field_entry.value}
            elif not prev_field:
                changed_fields[name] = {"old": "", "new": field_entry.value}

        cursor_moved = (
            active_state.cursor["row"] != new_state.cursor["row"]
            or active_state.cursor["col"] != new_state.cursor["col"]
        )
        text_changed = (active_state.screen_hash != new_state.screen_hash)

        delta = ScreenDelta(
            generation_from=active_state.generation,
            generation_to=new_state.generation,
            screen_hash_from=active_state.screen_hash,
            screen_hash_to=new_state.screen_hash,
            changed_fields=changed_fields,
            cursor_moved=cursor_moved,
            cursor_from=(active_state.cursor["row"], active_state.cursor["col"]),
            cursor_to=(new_state.cursor["row"], new_state.cursor["col"]),
            text_changed=text_changed,
            timestamp=time.time(),
        )

        new_payload = L2toL3HandoffPayload(
            state=new_state,
            delta=delta,
        )

        # Record new screen hash on the action result
        action_result.screen_hash_after = new_state.screen_hash

        return action_result, new_payload
