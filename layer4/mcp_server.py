import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
import structlog

from mcp.server.fastmcp import FastMCP

from layer4.orchestrator import RuntimeOrchestrator

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def runtime_lifespan(
    _server: FastMCP,
    orchestrator: RuntimeOrchestrator,
) -> AsyncIterator[None]:
    """Connect the configured runtime for the MCP server lifetime."""
    try:
        await orchestrator.connect()
    except Exception as exc:
        logger.warning("mcp_runtime_connection_failed", error=str(exc))
    try:
        yield
    finally:
        try:
            await orchestrator.disconnect()
        except Exception:
            pass


def resolve_default_config() -> str:
    if os.environ.get("SENTINEL_CONFIG"):
        return os.environ["SENTINEL_CONFIG"]
    if Path("configs/freedos.toml").is_file():
        return "configs/freedos.toml"
    return "configs/mainframe.toml"


def create_server(
    config_path: Optional[str] = None,
    orchestrator: Optional[RuntimeOrchestrator] = None,
) -> FastMCP:
    """Create the MCP server and bind it to one Layer 4 runtime session."""
    active_config = config_path or resolve_default_config()
    runtime = orchestrator or RuntimeOrchestrator(config_path=active_config)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        async with runtime_lifespan(server, runtime):
            yield

    server = FastMCP(
        name="SENTINEL",
        instructions="Delegate runtime observation and semantic actions to SENTINEL Layer 4.",
        lifespan=lifespan,
    )

    @server.tool()
    async def get_screen_state() -> dict[str, Any]:
        """Return the current screen state through the Layer 4 gateway."""
        return await runtime.execute_tool("get_screen_state", {})

    @server.tool()
    async def get_observation() -> dict[str, Any]:
        """Return the current validated Layer 3-to-Layer 4 observation."""
        return runtime.get_observation()

    @server.tool()
    async def set_field_and_submit(
        generation_token: str,
        field_id: str,
        value: str,
        action: str = "ENTER",
    ) -> dict[str, Any]:
        """Delegate the existing single-field semantic tool to Layer 4."""
        return await runtime.execute_tool(
            "set_field_and_submit",
            {
                "generation_token": generation_token,
                "field_id": field_id,
                "value": value,
                "action": action,
            },
        )

    @server.tool()
    async def fill_form_and_submit(
        generation_token: str,
        fields: dict[str, str],
        action: str = "ENTER",
    ) -> dict[str, Any]:
        """Delegate the existing multi-field semantic tool to Layer 4."""
        return await runtime.execute_tool(
            "fill_form_and_submit",
            {
                "generation_token": generation_token,
                "fields": fields,
                "action": action,
            },
        )

    @server.tool()
    async def trigger_action(generation_token: str, action_id: str) -> dict[str, Any]:
        """Delegate the existing semantic action-key tool to Layer 4."""
        return await runtime.execute_tool(
            "trigger_action",
            {"generation_token": generation_token, "action_id": action_id},
        )

    return server


server = create_server()


if __name__ == "__main__":
    server.run(transport="stdio")