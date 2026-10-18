"""stdio MCP server for an explicitly configured live ARM integration test."""

from layer4.arm_runtime import ARMRuntimeAdapter
from layer4.mcp_server import create_server


class UnusedScreenRuntime:
    """The live test exercises ARM tools only; this satisfies the shared server lifecycle."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def main() -> None:
    import os

    config_path = os.environ.get("SENTINEL_ARM_CONFIG", "configs/arm.toml")
    arm_runtime = ARMRuntimeAdapter.from_config(config_path)
    create_server(orchestrator=UnusedScreenRuntime(), arm_runtime=arm_runtime).run(transport="stdio")


if __name__ == "__main__":
    main()
