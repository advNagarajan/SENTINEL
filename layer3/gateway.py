"""Layer 3 AI Tool Gateway: Public interface providing tool catalog and executing agent actions."""
from typing import Any, Optional
import structlog

from layer3.compiler import ToolCompiler
from layer3.dispatcher import ActionDispatcher
from schemas.contracts import ContractValidationError, L2toL3HandoffPayload

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

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute an agent tool invocation.
        
        If it's an inspection tool (get_screen_state), returns state immediately.
        If it's an action tool, dispatches downward to L2/L1, waits for stability, updates state,
        and returns the result.
        """
        # Inspection tool (does not modify state, purely returns semantic state and tools)
        if name == "get_screen_state":
            state = self.active_payload.state
            fields_summary = {
                f.field_id: {
                    "label": f.label,
                    "value": f.value,
                    "editable": not f.protected,
                    "row": f.row,
                    "col": f.col,
                }
                for f in state.fields.values()
            }
            return {
                "success": True,
                "generation": state.generation,
                "generation_token": state.generation_token,
                "title": state.title,
                "cursor": state.cursor,
                "is_stable": state.is_stable,
                "fields": fields_summary,
                "available_tools": self.get_tools(),
            }

        # Downward action tool
        try:
            action_result, new_payload = await self.dispatcher.dispatch(
                tool_name=name,
                arguments=arguments,
                active_payload=self.active_payload,
            )

            # Update active payload
            self.active_payload = new_payload
            new_state = new_payload.state

            return {
                "success": action_result.success,
                "ticket_id": action_result.ticket_id,
                "execution_time_ms": action_result.execution_time_ms,
                "generation_before": action_result.generation_before,
                "generation_after": action_result.generation_after,
                "generation_token": new_state.generation_token,
                "screen_title": new_state.title,
                "is_stable": new_state.is_stable,
                "changed_fields": new_payload.delta.changed_fields if new_payload.delta else {},
                "message": (
                    f"Successfully executed '{name}'. Target transitioned to generation #{new_state.generation} "
                    f"('{new_state.title or 'N/A'}')."
                ),
                "available_tools": self.get_tools(),
            }

        except ContractValidationError as cve:
            logger.warning("l3_contract_validation_failed", error=str(cve))
            return {
                "success": False,
                "error": "ContractValidationError",
                "message": str(cve),
                "generation_token": self.active_payload.state.generation_token,
            }
        except Exception as exc:
            logger.error("l3_tool_execution_failed", error=str(exc))
            return {
                "success": False,
                "error": exc.__class__.__name__,
                "message": str(exc),
                "generation_token": self.active_payload.state.generation_token,
            }
