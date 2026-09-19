"""Layer 4 Live Validation Harness & Agent Environment Stub.

Presents compiled Layer 3 tool schemas, renders real-time terminal views,
executes agent tool invocations, and audits downward action execution.
Optionally emits state transitions to the Navigator microservice for graph construction.
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
from services.navigator.emitter import NavigatorEmitter

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

    def __init__(
        self,
        config_path: str = "configs/mainframe.toml",
        log_file: str = "logs/live_validation.jsonl",
        navigator_url: Optional[str] = "http://localhost:8100",
        target_name: str = "unknown",
    ):
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

        # Navigator microservice integration (fire-and-forget, optional)
        self.navigator: Optional[NavigatorEmitter] = None
        if navigator_url:
            self.navigator = NavigatorEmitter(base_url=navigator_url, target_name=target_name)

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
        from layer2.tn3270 import (
            OIAStabilityEngine,
            ScreenObjectBuilder,
            TN3270ActionLowerer,
            TN3270StateReducer,
            TN3270StreamParser,
        )
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
        if self.navigator:
            await self.navigator.close()
        if self.driver and self.connected and hasattr(self.driver, "disconnect"):
            await self.driver.disconnect()
        self.connected = False

    @property
    def is_connected(self) -> bool:
        """Check if harness is connected with an active gateway."""
        return self.connected and self.gateway is not None

    @property
    def active_payload(self) -> Optional[L2toL3HandoffPayload]:
        """Return the current active L2-to-L3 handoff payload if connected."""
        if not self.gateway:
            return None
        return self.gateway.get_active_payload()

    @property
    def active_state(self) -> Optional[RuntimeState]:
        """Return the current runtime screen state if connected."""
        payload = self.active_payload
        return payload.state if payload else None

    @property
    def generation_token(self) -> Optional[str]:
        """Return the active generation token if connected."""
        state = self.active_state
        return state.generation_token if state else None

    def get_tools(self) -> list[dict[str, Any]]:
        """Return compiled Layer 3 tool schemas + navigator prediction tool for the agent."""
        if not self.gateway:
            return []
        tools = self.gateway.get_tools()

        # Inject unified navigator graph inspection tool alongside L3 tools
        if self.navigator:
            state = self.gateway.get_active_payload().state
            tools.append({
                "type": "function",
                "function": {
                    "name": "view_navigation_graph",
                    "description": (
                        "Inspect the screen navigation graph to discover reachable screens, plan paths, "
                        "search known screens, view local topology, or execute custom graph queries. "
                        "Modes: 'predict' (outgoing transitions & probabilities from current screen), "
                        "'neighbors' (N-hop local map around a screen), "
                        "'path' (shortest navigation sequence to a target screen), "
                        "'search' (find screens by title keyword or field name), "
                        "'stats' (global summary of graph screens and routes), "
                        "'cypher' (arbitrary read-only Cypher query)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["predict", "neighbors", "path", "search", "stats", "cypher"],
                                "description": "Inspection mode for viewing the navigation graph.",
                            },
                            "screen_hash": {
                                "type": "string",
                                "description": (
                                    f"Source screen hash for 'predict' or 'neighbors' mode. "
                                    f"Defaults to active screen: '{state.screen_hash}'"
                                ),
                            },
                            "to_hash": {
                                "type": "string",
                                "description": "Target destination screen hash (required for 'path' mode).",
                            },
                            "from_hash": {
                                "type": "string",
                                "description": (
                                    f"Starting screen hash for 'path' mode. "
                                    f"Defaults to active screen: '{state.screen_hash}'"
                                ),
                            },
                            "depth": {
                                "type": "integer",
                                "description": "Exploration hop depth for 'neighbors' mode (1-5, default 1).",
                            },
                            "query": {
                                "type": "string",
                                "description": "Search keyword for screen title or field names (for 'search' mode).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of results to return (for 'search' mode, default 10).",
                            },
                            "cypher": {
                                "type": "string",
                                "description": "Read-only Cypher query (for 'cypher' mode, e.g. 'MATCH (s:Screen) RETURN s.title, s.visit_count LIMIT 5').",
                            },
                        },
                        "required": ["mode"],
                    },
                },
            })

        return tools

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
        """Agent action execution endpoint: dispatches tool call and returns structured observation JSON.

        For state-changing actions, captures the pre-action screen state and emits a
        transition event to the Navigator microservice after successful execution.
        Graph inspection tools (view_navigation_graph, predict_navigation) are intercepted
        here and handled by the navigator client without modifying terminal state.
        """
        if not self.gateway:
            raise RuntimeError("Harness is not connected.")

        # Intercept navigator graph inspection tool — handled entirely in L4
        if tool_name == "view_navigation_graph":
            if not self.navigator:
                return {
                    "success": False,
                    "error": "NavigatorDisabled",
                    "message": "Navigation graph service is not configured.",
                }

            current_hash = self.gateway.get_active_payload().state.screen_hash
            mode = arguments.get("mode", "predict")

            try:
                if mode == "predict":
                    target_hash = arguments.get("screen_hash") or current_hash
                    res = await self.navigator.get_predictions(target_hash)
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    return {"success": True, "mode": "predict", "result": res}

                elif mode == "neighbors":
                    target_hash = arguments.get("screen_hash") or current_hash
                    depth = int(arguments.get("depth", 1))
                    res = await self.navigator.get_neighbors(target_hash, depth=depth)
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    return {"success": True, "mode": "neighbors", "result": res}

                elif mode == "path":
                    to_hash = arguments.get("to_hash")
                    if not to_hash:
                        return {"success": False, "error": "MissingArgument", "message": "'to_hash' is required for 'path' mode."}
                    from_hash = arguments.get("from_hash") or current_hash
                    res = await self.navigator.find_path(from_hash, to_hash)
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    return {"success": True, "mode": "path", "result": res}

                elif mode == "search":
                    query = arguments.get("query")
                    limit = int(arguments.get("limit", 10))
                    res = await self.navigator.search_screens(query=query, limit=limit)
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    return {"success": True, "mode": "search", "result": res}

                elif mode == "stats":
                    res = await self.navigator.get_stats()
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    return {"success": True, "mode": "stats", "result": res}

                elif mode == "cypher":
                    cypher = arguments.get("cypher")
                    if not cypher:
                        return {"success": False, "error": "MissingArgument", "message": "'cypher' query is required for 'cypher' mode."}
                    params = arguments.get("parameters")
                    res = await self.navigator.query_cypher(cypher, parameters=params)
                    if res is None:
                        return {"success": False, "error": "NavigatorUnavailable", "message": "Navigation service unreachable."}
                    if "error" in res:
                        return {"success": False, "error": "CypherError", "message": res["error"]}
                    return {"success": True, "mode": "cypher", "result": res}

                else:
                    return {
                        "success": False,
                        "error": "InvalidMode",
                        "message": f"Unknown mode '{mode}'. Choose from: predict, neighbors, path, search, stats, cypher.",
                    }
            except Exception as e:
                logger.warning("navigator_tool_execution_failed", error=str(e))
                return {"success": False, "error": "NavigatorError", "message": str(e)}

        # Legacy predict_navigation interception for backward compatibility
        if tool_name == "predict_navigation" and self.navigator:
            screen_hash = arguments.get("screen_hash") or self.gateway.get_active_payload().state.screen_hash
            prediction = await self.navigator.get_predictions(screen_hash)
            if prediction is not None:
                return {"success": True, "predictions": prediction}
            else:
                return {
                    "success": False,
                    "error": "NavigatorUnavailable",
                    "message": "Navigation graph service is not reachable. Proceed without predictions.",
                }

        # Capture pre-action state for navigator transition tracking (exclude read-only inspection tools)
        read_only_tools = {"get_screen_state", "view_navigation_graph", "predict_navigation"}
        if self.navigator and tool_name not in read_only_tools:
            self.navigator.capture_pre_state(self.gateway.get_active_payload().state)

        start_time = time.time()
        result = await self.gateway.execute_tool(tool_name, arguments)
        duration_ms = (time.time() - start_time) * 1000.0

        if result.get("success"):
            new_state = self.gateway.get_active_payload().state
            self.audit_logger.log_state(new_state)

            # Emit transition to navigator (fire-and-forget, errors are logged not raised)
            if self.navigator and tool_name not in read_only_tools:

                try:
                    runtime_id = getattr(self.driver, "runtime_id", "unknown") if self.driver else "unknown"
                    await self.navigator.emit_transition(
                        new_state=new_state,
                        tool_name=tool_name,
                        arguments=arguments,
                        runtime_id=runtime_id,
                        latency_ms=duration_ms,
                    )
                except Exception as e:
                    logger.warning("navigator_emission_error", error=str(e))

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
