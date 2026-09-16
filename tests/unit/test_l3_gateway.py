"""Unit tests for Layer 3 AI Tool Gateway and Tool Compiler."""
import pytest
from unittest.mock import AsyncMock

from layer3.compiler import ToolCompiler
from layer3.dispatcher import ActionDispatcher
from layer3.gateway import AIToolGateway
from schemas.contracts import L2toL3HandoffPayload
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


@pytest.fixture
def mainframe_payload():
    grid = [" " * 80 for _ in range(24)]
    fields = {
        "command": FieldEntry(label="command", value="", row=20, col=10, length=40, protected=False, field_id="fld_command"),
        "header": FieldEntry(label="header", value="MVS SYSTEM CONSOLE", row=0, col=20, length=20, protected=True, field_id="fld_header"),
    }
    report = StabilityReport(is_stable=True, method="oia_byte", confidence=1.0, delta_value=0.0, iterations=1)
    state = RuntimeState(
        runtime_id="mainframe_01",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="MVS SYSTEM CONSOLE",
        fields=fields,
        status_line="",
        stability_report=report,
        screen_hash="b" * 64,
        generation=1,
        available_actions=["ENTER", "PF1", "PF3", "CLEAR"],
    )
    return L2toL3HandoffPayload(state=state)


@pytest.fixture
def freedos_payload():
    """Simulated FreeDOS 80x25 command prompt state to verify L3 is 100% environment-independent."""
    grid = [" " * 80 for _ in range(25)]
    grid[0] = "FreeDOS Kernel version 1.3"
    grid[2] = "C:\\>"
    fields = {
        "dos_prompt": FieldEntry(label="dos_prompt", value="dir", row=2, col=5, length=60, protected=False, field_id="fld_dos_prompt"),
    }
    report = StabilityReport(is_stable=True, method="cursor_settle", confidence=1.0, delta_value=0.0, iterations=1)
    state = RuntimeState(
        runtime_id="qemu_freedos_01",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="FreeDOS Prompt",
        screen_size={"rows": 25, "cols": 80},
        fields=fields,
        status_line="",
        stability_report=report,
        screen_hash="c" * 64,
        generation=1,
        available_actions=["ENTER", "ESCAPE", "TAB", "F1", "F3"],
    )
    return L2toL3HandoffPayload(state=state)


def test_tool_compiler_mainframe(mainframe_payload):
    """Verify ToolCompiler produces valid JSON Schema tools for Mainframe."""
    tools = ToolCompiler.compile_tools(mainframe_payload)
    tool_names = [t["function"]["name"] for t in tools]

    assert "set_field_and_submit" in tool_names
    assert "trigger_action" in tool_names
    assert "fill_form_and_submit" in tool_names
    assert "get_screen_state" in tool_names

    # Check field_id enum includes fld_command
    submit_tool = next(t for t in tools if t["function"]["name"] == "set_field_and_submit")
    props = submit_tool["function"]["parameters"]["properties"]
    assert "fld_command" in props["field_id"]["enum"]
    assert "fld_header" not in props["field_id"]["enum"]  # Protected field excluded from editable enum!
    assert props["generation_token"]["enum"] == [mainframe_payload.state.generation_token]


def test_tool_compiler_freedos_zero_code_changes(freedos_payload):
    """Verify ToolCompiler works identically on FreeDOS state with zero code changes."""
    tools = ToolCompiler.compile_tools(freedos_payload)
    tool_names = [t["function"]["name"] for t in tools]

    assert "set_field_and_submit" in tool_names
    submit_tool = next(t for t in tools if t["function"]["name"] == "set_field_and_submit")
    props = submit_tool["function"]["parameters"]["properties"]
    assert "fld_dos_prompt" in props["field_id"]["enum"]
    assert "ESCAPE" in props["action"]["enum"]
    assert "TAB" in props["action"]["enum"]
    assert props["generation_token"]["enum"] == [freedos_payload.state.generation_token]


@pytest.mark.asyncio
async def test_ai_tool_gateway_get_state(mainframe_payload):
    """Verify get_screen_state inspection tool in gateway."""
    mock_dispatcher = AsyncMock()
    gateway = AIToolGateway(initial_payload=mainframe_payload, dispatcher=mock_dispatcher)

    res = await gateway.execute_tool("get_screen_state", {"include_raw_grid": False})
    assert res["success"] is True
    assert res["title"] == "MVS SYSTEM CONSOLE"
    assert "fld_command" in res["fields"]
    assert res["fields"]["fld_command"]["editable"] is True
    assert res["fields"]["fld_header"]["editable"] is False


@pytest.mark.asyncio
async def test_ai_tool_gateway_execute_action_success(mainframe_payload):
    """Verify gateway executes action and updates active state."""
    mock_dispatcher = AsyncMock()
    gateway = AIToolGateway(initial_payload=mainframe_payload, dispatcher=mock_dispatcher)

    from schemas.actions import ActionResult
    action_res = ActionResult(
        ticket_id="ticket_123",
        success=True,
        generation_before=1,
        generation_after=2,
        screen_hash_before="b" * 64,
        screen_hash_after="d" * 64,
        execution_time_ms=12.5,
    )

    # Next state
    next_state = RuntimeState(
        runtime_id="mainframe_01",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=[" " * 80 for _ in range(24)],
        title="PRIMARY MENU",
        fields={},
        status_line="",
        stability_report=mainframe_payload.state.stability_report,
        screen_hash="d" * 64,
        generation=2,
    )
    next_payload = L2toL3HandoffPayload(state=next_state)

    mock_dispatcher.dispatch.return_value = (action_res, next_payload)

    exec_result = await gateway.execute_tool("set_field_and_submit", {
        "generation_token": mainframe_payload.state.generation_token,
        "field_id": "fld_command",
        "value": "LOGON",
        "action": "ENTER",
    })

    assert exec_result["success"] is True
    assert exec_result["generation_after"] == 2
    assert exec_result["screen_title"] == "PRIMARY MENU"
    assert gateway.get_active_payload().state.generation == 2
