"""Layer 3 AI Tool Gateway: Public interface providing tool catalog and executing agent actions."""
from typing import Any, Optional
import structlog

from layer3.compiler import ToolCompiler
from layer3.dispatcher import ActionDispatcher
from schemas.contracts import (
    ContractValidationError,
    L2toL3HandoffPayload,
    L3toL4HandoffPayload,
    validate_l3_to_l4_contract,
)

logger = structlog.get_logger(__name__)


class AIToolGateway:
    """High-level Gateway presented to LLMs or Layer 4 Agent orchestrator."""

    def __init__(
        self,
        initial_payload: L2toL3HandoffPayload,
        dispatcher: ActionDispatcher,
    ) -> None:
        self.active_payload = initial_payload
        self.dispatcher = dispatcher
        self.compiler = ToolCompiler()

    def get_tools(self) -> list[dict[str, Any]]:
        """Return dynamic JSON Schema tools compiled from the active screen state."""
        return self.compiler.compile_tools(self.active_payload)

    def get_active_payload(self) -> L2toL3HandoffPayload:
        """Get the current sealed L2toL3 handoff payload."""
        return self.active_payload

    def _build_l3_to_l4_payload(
        self,
        payload: L2toL3HandoffPayload,
        success: bool = True,
        error: Optional[str] = None,
        execution_time_ms: float = 0.0,
        generation_before: Optional[int] = None,
        generation_after: Optional[int] = None,
        ticket_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> L3toL4HandoffPayload:
        """Construct, sanitize, and validate an L3toL4HandoffPayload for the active state."""
        state = payload.state

        # 1. Extract sanitized screen text lines
        screen_lines: list[str] = []
        for row in state.raw_grid:
            clean_row = "".join(c if (ord(c) < 128 and c.isprintable()) else " " for c in row).rstrip()
            if clean_row.strip():
                screen_lines.append(clean_row)
        if not screen_lines:
            screen_lines = ["(Blank Screen)"]

        # 2. Extract editable fields summary and full fields
        editable_fields: dict[str, dict[str, Any]] = {}
        all_fields: dict[str, dict[str, Any]] = {}
        for f in state.fields.values():
            field_data = {
                "label": f.label,
                "value": f.value,
                "current_value": f.value,
                "editable": not f.protected,
                "max_length": f.length,
                "row": f.row,
                "col": f.col,
            }
            all_fields[f.field_id] = field_data
            if not f.protected:
                editable_fields[f.field_id] = field_data

        # 3. Status message
        status_msg = state.status_line or (
            f"OIA: {state.metadata.get('oia_status', 'READY')}" if state.metadata else "READY"
        )

        tools = self.compiler.compile_tools(payload)

        handoff = L3toL4HandoffPayload(
            generation=state.generation,
            generation_token=state.generation_token,
            screen_title=state.title,
            status_message=status_msg,
            screen_text=screen_lines,
            editable_fields=editable_fields,
            fields=all_fields,
            available_tools=tools,
            is_stable=state.is_stable,
            success=success,
            message=message or f"Screen state generation #{state.generation} ('{state.title or 'N/A'}').",
            changed_fields=payload.delta.changed_fields if payload.delta else {},
            error=error,
            execution_time_ms=execution_time_ms,
            generation_before=generation_before,
            generation_after=generation_after if generation_after is not None else state.generation,
            ticket_id=ticket_id,
            cursor=state.cursor,
        )

        # Enforce contract validation
        validate_l3_to_l4_contract(handoff)
        return handoff

    def get_observation(self) -> L3toL4HandoffPayload:
        """Return the validated L3toL4 observation payload for the current screen state."""
        return self._build_l3_to_l4_payload(self.active_payload, success=True)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute an agent tool invocation.
        
        If it's an inspection tool (get_screen_state), returns state immediately.
        If it's an action tool, dispatches downward to L2/L1, waits for stability, updates state,
        and returns the result.
        """
        # Inspection tool (does not modify state, purely returns semantic state and tools)
        if name == "get_screen_state":
            handoff = self.get_observation()
            return handoff.to_dict()

        # Downward action tool
        try:
            action_result, new_payload = await self.dispatcher.dispatch(
                tool_name=name,
                arguments=arguments,
                active_payload=self.active_payload,
            )

            # Update active payload
            self.active_payload = new_payload

            msg = (
                f"Successfully executed '{name}'. Target transitioned to generation #{new_payload.state.generation} "
                f"('{new_payload.state.title or 'N/A'}')."
            )

            handoff = self._build_l3_to_l4_payload(
                payload=new_payload,
                success=action_result.success,
                execution_time_ms=action_result.execution_time_ms,
                generation_before=action_result.generation_before,
                generation_after=action_result.generation_after,
                ticket_id=action_result.ticket_id,
                message=msg,
            )
            return handoff.to_dict()

        except ContractValidationError as cve:
            logger.warning("l3_contract_validation_failed", error=str(cve))
            return {
                "success": False,
                "error": "ContractValidationError",
                "message": str(cve),
                "generation_token": self.active_payload.state.generation_token,
                "available_tools": self.get_tools(),
            }
        except Exception as exc:
            logger.error("l3_tool_execution_failed", error=str(exc))
            return {
                "success": False,
                "error": exc.__class__.__name__,
                "message": str(exc),
                "generation_token": self.active_payload.state.generation_token,
                "available_tools": self.get_tools(),
            }
