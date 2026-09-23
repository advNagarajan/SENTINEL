"""Unit tests for Layer 3 to Layer 4 handoff contract and observation generation."""
import pytest
from layer3.gateway import AIToolGateway
from schemas.contracts import (
    ContractValidationError,
    L2toL3HandoffPayload,
    L3toL4HandoffPayload,
    validate_l3_to_l4_contract,
)
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.fixture
def sample_l3_l4_payload():
    """Build a valid sample L3toL4HandoffPayload."""
    return L3toL4HandoffPayload(
        generation=1,
        generation_token="gen_1_abcdef01",
        screen_title="ISPF PRIMARY OPTION MENU",
        status_message="READY",
        screen_text=[
            "ISPF PRIMARY OPTION MENU",
            "0  SETTINGS    Terminal and user parameters",
            "1  BROWSE      Display source data or listings",
            "2  EDIT        Create or change source data",
            "3  UTILITIES   Perform utility functions",
        ],
        editable_fields={
            "fld_option_===>": {
                "label": "Option ===>",
                "value": "",
                "current_value": "",
                "editable": True,
                "max_length": 24,
                "row": 1,
                "col": 14,
            }
        },
        available_tools=[
            {
                "type": "function",
                "function": {
                    "name": "set_field_and_submit",
                    "parameters": {
                        "properties": {
                            "generation_token": {
                                "enum": ["gen_1_abcdef01"],
                            }
                        }
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "trigger_action",
                    "parameters": {
                        "properties": {
                            "generation_token": {
                                "enum": ["gen_1_abcdef01"],
                            }
                        }
                    },
                },
            },
        ],
        is_stable=True,
        success=True,
    )


def test_validate_l3_to_l4_contract_valid(sample_l3_l4_payload):
    """Verify that a valid L3toL4HandoffPayload passes contract validation without error."""
    validate_l3_to_l4_contract(sample_l3_l4_payload)
    d = sample_l3_l4_payload.to_dict()
    assert d["success"] is True
    assert d["generation_token"] == "gen_1_abcdef01"
    assert "screen_text" in d
    assert len(d["screen_text"]) == 5
    assert "fld_option_===>" in d["editable_fields"]


def test_validate_l3_to_l4_contract_rejects_bad_token(sample_l3_l4_payload):
    """Verify that an invalid generation_token format is rejected."""
    sample_l3_l4_payload.generation_token = "invalid_token_format"
    with pytest.raises(ContractValidationError, match="Invalid generation_token"):
        validate_l3_to_l4_contract(sample_l3_l4_payload)


def test_validate_l3_to_l4_contract_rejects_empty_tools(sample_l3_l4_payload):
    """Verify that empty available_tools list is rejected."""
    sample_l3_l4_payload.available_tools = []
    with pytest.raises(ContractValidationError, match="available_tools must be a non-empty list"):
        validate_l3_to_l4_contract(sample_l3_l4_payload)


def test_validate_l3_to_l4_contract_rejects_token_mismatch_in_tool(sample_l3_l4_payload):
    """Verify that a tool with mismatched generation_token enum is rejected."""
    sample_l3_l4_payload.available_tools[0]["function"]["parameters"]["properties"]["generation_token"]["enum"] = ["gen_99_deadbeef"]
    with pytest.raises(ContractValidationError, match="does not match payload"):
        validate_l3_to_l4_contract(sample_l3_l4_payload)


def test_validate_l3_to_l4_contract_rejects_missing_field_tool_when_fields_editable(sample_l3_l4_payload):
    """Verify that if editable_fields exist, at least one field-filling tool must be present."""
    sample_l3_l4_payload.available_tools = [sample_l3_l4_payload.available_tools[1]]
    with pytest.raises(ContractValidationError, match="no field-filling tool was compiled"):
        validate_l3_to_l4_contract(sample_l3_l4_payload)


def test_gateway_get_observation_produces_valid_contract():
    """Verify that AIToolGateway.get_observation generates a contract-compliant payload with clean screen_text."""
    raw_grid = [" " * 80 for _ in range(24)]
    raw_grid[0] = "ISPF PRIMARY OPTION MENU".ljust(80)
    raw_grid[1] = "Option ===> ".ljust(80)
    raw_grid[3] = "1  BROWSE      Display source data or listings".ljust(80)
    raw_grid[4] = "2  EDIT        Create or change source data".ljust(80)
    raw_grid[5] = "3  UTILITIES   Perform utility functions".ljust(80)

    fields = {
        "opt": FieldEntry(label="Option ===>", value="", row=1, col=12, length=10, protected=False, field_id="fld_option_===>"),
        "title": FieldEntry(label="ISPF PRIMARY OPTION MENU", value="", row=0, col=0, length=24, protected=True, field_id="fld_title"),
    }
    report = StabilityReport(is_stable=True, method="oia_byte", confidence=1.0, delta_value=0.0, iterations=1)
    state = RuntimeState(
        runtime_id="node_test",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=raw_grid,
        title="ISPF PRIMARY OPTION MENU",
        fields=fields,
        status_line="READY",
        stability_report=report,
        screen_hash="a" * 64,
        generation=1,
    )
    l2_payload = L2toL3HandoffPayload(state=state)

    from unittest.mock import AsyncMock
    gateway = AIToolGateway(initial_payload=l2_payload, dispatcher=AsyncMock())

    observation = gateway.get_observation()
    assert isinstance(observation, L3toL4HandoffPayload)
    assert observation.screen_title == "ISPF PRIMARY OPTION MENU"
    assert "1  BROWSE" in observation.screen_text[2]
    assert "3  UTILITIES" in observation.screen_text[4]
    assert "fld_option_===>" in observation.editable_fields

    trigger_tool = next(t for t in observation.available_tools if t["function"]["name"] == "trigger_action")
    desc = trigger_tool["function"]["description"]
    assert "PF3" in desc and "Return to previous menu" in desc
