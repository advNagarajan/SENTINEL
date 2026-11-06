# SENTINEL Agent Rules

- Do NOT search, read, or inspect local workspace repository files (do not call FindFiles, ReadFile, Grep, or directory listings). The target legacy system is NOT in the local filesystem; it is running live inside Hercules MVS.
- Start every task immediately by calling the MCP tool `get_observation()`.
- Operate the legacy system only through the configured SENTINEL MCP tools (`get_observation`, `set_field_and_submit`, `fill_form_and_submit`, `trigger_action`). Never import or call SENTINEL internals directly.
- Inspect the current state with `get_observation` or `get_screen_state` before acting; use the semantic fields and actions returned by SENTINEL.
- Pass the exact `generation_token` from the latest observation to every state-changing tool. After each action, inspect the resulting state before deciding what to do next.
- Do not assume an action succeeded. On an MCP failure, use the returned error/state to reason; do not blindly retry or bypass MCP.
- Do not access Navigator/Neo4j or modify SENTINEL source unless explicitly asked.
- Complete only the requested task, verify its resulting state, then stop.