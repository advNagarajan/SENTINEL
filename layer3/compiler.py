"""Layer 3 Tool Compiler: Compiles canonical RuntimeState into JSON Schema AI tool definitions.

Strictly environment-independent: relies solely on RuntimeState contracts.
"""
from typing import Any, Optional
from schemas.contracts import L2toL3HandoffPayload
from schemas.state import RuntimeState


class ToolCompiler:
    """Dynamically compiles environment-agnostic JSON Schema tool definitions for LLMs."""

    @staticmethod
    def compile_tools(payload: L2toL3HandoffPayload) -> list[dict[str, Any]]:
        """Generate OpenAI/Anthropic compatible function schemas bound to the current state."""
        state: RuntimeState = payload.state
        gen_token = state.generation_token
        
        # Find all unprotected (editable) fields
        editable_fields = [
            f.field_id for f in state.fields.values()
            if not f.protected
        ]
        
        # If no editable fields found by field_id, collect dict keys of unprotected fields
        if not editable_fields:
            editable_fields = [
                k for k, f in state.fields.items()
                if not f.protected
            ]
            
        actions = state.available_actions or ["ENTER", "CLEAR", "CANCEL", "SUBMIT"]

        tools: list[dict[str, Any]] = []

        # 1. Single Field Fill & Submit Tool (available if editable fields exist)
        if editable_fields:
            tools.append({
                "type": "function",
                "function": {
                    "name": "set_field_and_submit",
                    "description": (
                        f"Enter text into an editable screen field and trigger a submission key on the target screen. "
                        f"Screen title: '{state.title or 'Unknown'}'. Current generation: #{state.generation}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "generation_token": {
                                "type": "string",
                                "description": f"Mandatory generation token for active state validation. Must be '{gen_token}'.",
                                "enum": [gen_token],
                            },
                            "field_id": {
                                "type": "string",
                                "description": "Identifier of the target field to enter text into.",
                                "enum": editable_fields,
                            },
                            "value": {
                                "type": "string",
                                "description": "Text value to enter into the field.",
                            },
                            "action": {
                                "type": "string",
                                "description": "Submission action or key to trigger after typing.",
                                "enum": actions,
                                "default": "ENTER" if "ENTER" in actions else actions[0],
                            },
                        },
                        "required": ["generation_token", "field_id", "value"],
                    },
                },
            })

            # 2. Multi-Field Form Fill & Submit Tool
            tools.append({
                "type": "function",
                "function": {
                    "name": "fill_form_and_submit",
                    "description": (
                        "Populate multiple fields simultaneously on the active screen before triggering an action."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "generation_token": {
                                "type": "string",
                                "description": f"Mandatory generation token. Must be '{gen_token}'.",
                                "enum": [gen_token],
                            },
                            "fields": {
                                "type": "object",
                                "description": (
                                    f"Key-value dictionary where keys are field IDs ({', '.join(editable_fields[:8])}...) "
                                    f"and values are the text strings to enter."
                                ),
                            },
                            "action": {
                                "type": "string",
                                "description": "Action key to submit after filling fields.",
                                "enum": actions,
                                "default": "ENTER" if "ENTER" in actions else actions[0],
                            },
                        },
                        "required": ["generation_token", "fields"],
                    },
                },
            })

        # 3. Action Key Trigger Tool (always available for navigation/PF keys)
        tools.append({
            "type": "function",
            "function": {
                "name": "trigger_action",
                "description": (
                    f"Send an action or navigation key (e.g. ENTER, CLEAR, PF1-PF12, ESCAPE) to the target environment."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "generation_token": {
                            "type": "string",
                            "description": f"Mandatory generation token. Must be '{gen_token}'.",
                            "enum": [gen_token],
                        },
                        "action_id": {
                            "type": "string",
                            "description": "The specific action or key to trigger.",
                            "enum": actions,
                        },
                    },
                    "required": ["generation_token", "action_id"],
                },
            },
        })

        # 4. State Inspection Tool
        tools.append({
            "type": "function",
            "function": {
                "name": "get_screen_state",
                "description": "Inspect the active screen grid, cursor position, and extracted fields without modifying anything.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_raw_grid": {
                            "type": "boolean",
                            "description": "If true, includes full ASCII 2D text grid in the response.",
                            "default": False,
                        },
                    },
                },
            },
        })

        return tools
