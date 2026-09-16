"""Layer 4 Live Validation Harness & Agent Environment Stub.

Presents compiled Layer 3 tool schemas, renders real-time terminal views,
executes agent tool invocations, and audits downward action execution.
"""
import asyncio
import json
import time
from typing import Any, Optional
import structlog

from drivers.registry import DriverRegistry
from layer3.gateway import AIToolGateway
from layer4.audit_log import AuditLogger
from schemas.contracts import L2toL3HandoffPayload
from schemas.events import RuntimeEvent
from schemas.pipeline import TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport

logger = structlog.get_logger(__name__)


def format_screen_grid(state: RuntimeState) -> str:
    """Format an 80x24 character grid in a clean bordered frame."""
    cols = state.screen_size.get("cols", 80)
    border = "=" * (cols + 4)
    thin_line = "-" * (cols + 4)
    lines = [
        border,
        f"| SENTINEL L4 AGENT MONITOR | Runtime: {state.runtime_id:<20} | Generation: #{state.generation:<3} |",
        f"| Title: {state.title or 'N/A':<30} | Hash: {state.screen_hash[:16]}... | Status: {'STABLE' if state.is_stable else 'BUSY'} |",
        border,
    ]
    for row in state.raw_grid:
        clean_row = "".join([c if (ord(c) < 128 and c.isprintable()) else " " for c in row])
        lines.append(f"| {clean_row:<{cols}} |")

    lines.append(thin_line)
    lines.append(f"| Cursor: Row {state.cursor['row']:02d}, Col {state.cursor['col']:02d} | Token: {state.generation_token:<40} |")
    lines.append(border)
    return "\n".join(lines)


def format_fields_table(state: RuntimeState) -> str:
    """Format extracted fields with their field_ids and editable/protected status."""
    if not state.fields:
        return "  (No fields extracted on this screen)"

    header = (
        f"+-{'-'*20}-+-{'-'*20}-+-{'-'*20}-+-{'-'*8}-+-{'-'*10}-+\n"
        f"| {'FIELD ID':<20} | {'LABEL':<20} | {'CURRENT VALUE':<20} | {'ROW,COL':<8} | {'TYPE':<10} |\n"
        f"+-{'-'*20}-+-{'-'*20}-+-{'-'*20}-+-{'-'*8}-+-{'-'*10}-+"
    )
    rows = [header]
    for f in state.fields.values():
        val_clean = (f.value[:17] + "...") if len(f.value) > 20 else f.value
        field_type = "READ-ONLY" if f.protected else "EDITABLE"
        rows.append(
            f"| {f.field_id[:20]:<20} | {f.label[:20]:<20} | {val_clean:<20} | {f.row:02d},{f.col:02d}   | {field_type:<10} |"
        )
    rows.append(f"+-{'-'*20}-+-{'-'*20}-+-{'-'*20}-+-{'-'*8}-+-{'-'*10}-+")
    return "\n".join(rows)


def format_tools_catalog(tools: list[dict[str, Any]]) -> str:
    """Format compiled Layer 3 tools for human/agent review."""
    lines = ["Available Layer 3 Compiled AI Tools:"]
    for idx, t in enumerate(tools, 1):
        fn = t.get("function", {})
        lines.append(f"  {idx}. {fn.get('name')}: {fn.get('description')}")
        params = fn.get("parameters", {}).get("properties", {})
        param_list = [f"{k} ({v.get('type')})" for k, v in params.items()]
        lines.append(f"     Parameters: {', '.join(param_list)}")
    return "\n".join(lines)


class LiveValidationHarness:
    """Interactive validation harness acting as Layer 4 for end-to-end action execution."""

    def __init__(self, config_path: str = "configs/mainframe.toml", log_file: str = "logs/live_validation.jsonl"):
        self.config_path = config_path
        self.log_file = log_file
        self.audit_logger = AuditLogger(log_file=self.log_file)
        self.driver = None
        self.reducer = None
        self.stability = None
        self.lowerer = None
        self.dispatcher = None
        self.config = None
        self.gateway: Optional[AIToolGateway] = None
        self.connected = False

    async def connect(self) -> None:
        """Initialize L1-L2-L3 pipeline and connect to target environment."""
        (
            self.driver,
            self.reducer,
            self.stability,
            self.lowerer,
            self.dispatcher,
            self.config,
        ) = DriverRegistry.create_gateway(self.config_path)

        def on_event(ev: RuntimeEvent) -> None:
            self.audit_logger.log_event(ev)

        self.driver.add_event_listener(on_event)

        logger.info("harness_connecting_target", host=self.driver.host, port=self.driver.port)
        await self.driver.connect()
        self.connected = True

        # Settle on initial screen
        state_1, _ = await self.stability.wait_until_stable(
            driver=self.driver,
            reducer=self.reducer,
            runtime_id=self.driver.runtime_id,
            generation=1,
        )

        initial_payload = L2toL3HandoffPayload(state=state_1)
        self.gateway = AIToolGateway(initial_payload=initial_payload, dispatcher=self.dispatcher)
        self.audit_logger.log_state(state_1)

    async def connect_mock(self) -> None:
        """Initialize mock L1-L2-L3 pipeline for offline testing without a live emulator."""
        from unittest.mock import AsyncMock
        from layer2.action_lowerer import TN3270ActionLowerer
        from layer2.builder import ScreenObjectBuilder
        from layer2.parser import TN3270StreamParser
        from layer2.reducer import TN3270StateReducer
        from layer2.stability import OIAStabilityEngine
        from layer3.dispatcher import ActionDispatcher

        self.reducer = TN3270StateReducer(rows=24, cols=80)
        self.lowerer = TN3270ActionLowerer()
        self.stability = OIAStabilityEngine(poll_interval_ms=10, settle_ms=20, max_wait_ms=500, quiescence_ms=20)

        # Mock driver
        self.driver = AsyncMock()
        self.driver.runtime_id = "mock_mainframe_01"

        parser = TN3270StreamParser()
        builder = ScreenObjectBuilder(rows=24, cols=80)

        # Build simulated CICS login screen
        label_ebcdic = "CICS LOGON USERID:".encode("cp037")
        input_ebcdic = "        ".encode("cp037")
        gen1_raw = bytes([0xF5, 0x1D, 0x28]) + label_ebcdic + bytes([0x1D, 0x00]) + input_ebcdic
        decoded = parser.parse_frame(TransportFrame(raw_payload=gen1_raw))
        som = builder.build_object_model(decoded)
        state_1, _ = self.reducer.reduce_state(som, runtime_id="mock_mainframe_01", generation=1)

        # Responses for subsequent steps
        gen2_raw = bytes([0xF5, 0x1D, 0x28]) + "WELCOME TO IBM CICS PRIMARY APPLICATION MENU".encode("cp037")
        frame_gen2 = TransportFrame(raw_payload=gen2_raw)
        empty_frame = TransportFrame(raw_payload=b"")
        self.driver.read_frame.side_effect = [
            frame_gen2, empty_frame, empty_frame, frame_gen2,
            frame_gen2, empty_frame, empty_frame, frame_gen2,
        ]

        self.dispatcher = ActionDispatcher(
            driver=self.driver,
            reducer=self.reducer,
            action_lowerer=self.lowerer,
            stability_engine=self.stability,
        )

        initial_payload = L2toL3HandoffPayload(state=state_1)
        self.gateway = AIToolGateway(initial_payload=initial_payload, dispatcher=self.dispatcher)
        self.connected = True
        self.audit_logger.log_state(state_1)

    async def disconnect(self) -> None:
        """Close connection cleanly."""
        if self.driver and self.connected and hasattr(self.driver, "disconnect"):
            await self.driver.disconnect()
        self.connected = False

    def get_tools(self) -> list[dict[str, Any]]:
        """Return compiled Layer 3 tool schemas for the agent."""
        if not self.gateway:
            return []
        return self.gateway.get_tools()

    def present_tools(self) -> str:
        """Present current tool definitions to the agent as formatted JSON Schema."""
        tools = self.get_tools()
        return json.dumps(tools, indent=2)

    def render_debug_grid(self) -> None:
        """Optional developer visual inspection helper (only called on explicit request)."""
        if not self.gateway:
            print("Gateway not initialized.")
            return
        state = self.gateway.get_active_payload().state
        print("\n" + format_screen_grid(state))
        print("\nEXTRACTED FIELDS:")
        print(format_fields_table(state) + "\n")

    async def step(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Agent action execution endpoint: dispatches tool call and returns structured observation JSON."""
        if not self.gateway:
            raise RuntimeError("Harness is not connected.")

        start_time = time.time()
        result = await self.gateway.execute_tool(tool_name, arguments)
        duration_ms = (time.time() - start_time) * 1000.0

        if result.get("success"):
            self.audit_logger.log_state(self.gateway.get_active_payload().state)

        result["latency_ms"] = round(duration_ms, 2)
        return result

    async def execute_step(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Alias for step() with structured logging."""
        return await self.step(tool_name, arguments)

    async def run_scenario(self, scenario_steps: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Run a predefined sequence of agent tool calls."""
        results = []
        for idx, (tool_name, args) in enumerate(scenario_steps, 1):
            # Automatically populate active generation token if omitted
            if "generation_token" not in args and self.gateway:
                args["generation_token"] = self.gateway.get_active_payload().state.generation_token
            res = await self.step(tool_name, args)
            results.append(res)
            if not res.get("success"):
                break
        return results
