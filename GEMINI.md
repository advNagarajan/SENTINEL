# SENTINEL Agent Rules

- Operate the legacy system only through the configured SENTINEL MCP tools. Never import or call SENTINEL internals directly.
- Inspect the current state with `get_observation` or `get_screen_state` before acting; use the semantic fields and actions returned by SENTINEL.
- Pass the exact `generation_token` from the latest observation to every state-changing tool. After each action, inspect the resulting state before deciding what to do next.
- Do not assume an action succeeded. On an MCP failure, use the returned error/state to reason; do not blindly retry or bypass MCP.
- Do not access Navigator/Neo4j or modify SENTINEL source unless explicitly asked.
- Complete only the requested task, verify its resulting state, then stop.