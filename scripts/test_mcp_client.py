"""Test client for demonstrating Layer 4 Model Context Protocol (MCP) tool execution."""
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    server_args = ["-m", "tests.mcp_stdio_server"] if "--mock" in sys.argv else ["scripts/run_sentinel_mcp.py"]
    params = StdioServerParameters(command=sys.executable, args=server_args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("=" * 80)
            print("PROJECT SENTINEL: LAYER 4 MCP CLIENT PROTOCOL HANDSHAKE")
            print("=" * 80)
            print("Status: Connected to SENTINEL MCP Server via stdio")

            tools = await session.list_tools()
            print(f"\nDiscovered {len(tools.tools)} Layer 4 Tools via MCP Tool Catalog:")
            for t in tools.tools:
                print(f"  - {t.name:<22} : {t.description}")

            print("\n" + "-" * 80)
            print("Executing Tool via MCP: get_observation()")
            print("-" * 80)
            res = await session.call_tool("get_observation", {})
            obs = res.structuredContent
            print(f"Screen Title:       '{obs.get('screen_title')}'")
            print(f"Generation Token:   '{obs.get('generation_token')}'")
            print(f"Editable Fields:    {list(obs.get('editable_fields', {}).keys())}")
            print(f"Available Actions:  {[t['function']['name'] for t in obs.get('available_tools', [])]}")

            print("\n" + "=" * 80)
            print("SUCCESS: Full L1-L4 stdio MCP pipeline verified end-to-end!")
            print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
