# SENTINEL Logging and Navigator End-to-End Playbook

Use this playbook to verify that Gemini CLI drives SENTINEL, SENTINEL emits
sanitized observability events to the logging service, and successful
screen-changing actions are recorded by Navigator in Neo4j.

This is a live test against the local mainframe demo. It adds event rows to the
regular logging volume and graph nodes/transitions to the regular Neo4j volume.
It does not clear either database. Run it only when those local services and
the mainframe are yours to use.

## What this verifies

```text
Gemini CLI
  -> SENTINEL MCP (get_observation / set_field_and_submit / trigger_action)
       -> logging producer -> logging service -> SQLite event database
       -> Navigator emitter -> Navigator API -> Neo4j
```

Logging and graph delivery are separate flows. A log entry does not prove a
graph transition was saved, and a graph edge does not prove an observability
event was persisted; verify both independently below.

## Safety notes

- Use the local demo mainframe configured by `configs/mainframe.toml`, not a
  production or shared system.
- This walkthrough only opens `SYS1.PROCLIB` in read-only Browse mode and exits
  the browse screens. It does not allocate, edit, delete, or update datasets.
- SENTINEL's Navigator emitter records submitted field values in Neo4j
  `values_seen`. The dataset name is a read-only target on the local demo
  mainframe, not synthetic data. Never enter passwords, credentials, personal
  information, or other sensitive values into actions being graphed.
- The default logging and Neo4j Compose volumes are persistent. Do not run
  `docker compose down -v`, delete volumes, or clear graph/event data as part
  of this check.
- If an expected screen or semantic field is absent, stop and inspect the
  observation. Do not guess field IDs, action tokens, or retry a state-changing
  action with an old `generation_token`.

## 1. Start the local services

Open PowerShell in the repository root. Start only the required services; this
avoids starting the unrelated FreeDOS container:

```powershell
docker compose up -d --build neo4j navigator logging-service mainframe
docker compose ps
```

Wait for the service health checks to pass, then check the HTTP endpoints:

```powershell
curl.exe --fail http://127.0.0.1:8200/health
curl.exe --fail http://127.0.0.1:8100/health
```

Expected results:

- Logging service returns HTTP 200 and `storage: available`.
- Navigator returns HTTP 200 and `neo4j: connected`.
- `mainframe` is running and its TN3270 port is available on `localhost:3270`.

If a port is already in use or a health check fails, stop and resolve that
environment issue before proceeding. Do not remove existing containers or
volumes to make the test pass.

## 2. Record pre-test counts

Capture the current logging and graph counts before starting Gemini. The
databases may already contain data; record the numbers rather than expecting
zero.

Logging event count:

```powershell
docker compose exec -T logging-service python -c "import sqlite3; db=sqlite3.connect('/var/lib/sentinel-logging/observability.sqlite3'); print(db.execute('SELECT COUNT(*) FROM events').fetchone()[0]); db.close()"
```

Navigator counts:

```powershell
curl.exe --fail http://127.0.0.1:8100/graph/stats
```

Save or note the returned logging event count and the Navigator
`total_screens`, `total_transitions`, and `total_traversals`. Do not reset
either database.

## 3. Start Gemini with the right SENTINEL configuration

Use a **second** PowerShell window, also rooted at this repository. The Gemini
MCP server configuration is in `.gemini/settings.json`; it launches
`scripts/run_sentinel_mcp.py`. Set the environment in this same window before
starting Gemini so the MCP process inherits it:

```powershell
$env:SENTINEL_CONFIG = "configs/mainframe.toml"
$env:SENTINEL_OBSERVABILITY_ENDPOINT = "http://127.0.0.1:8200/v1/events"
gemini
```

`SENTINEL_CONFIG` is important: without it, the launcher may select the
FreeDOS configuration when that file exists. Authenticate Gemini CLI using
your usual approved method; do not put API keys or mainframe credentials in a
Gemini prompt.

## 4. Use these Gemini CLI prompts

Send the prompts one at a time. SENTINEL's configured MCP tools are
`get_observation`, `get_screen_state`, `set_field_and_submit`, and
`trigger_action`. The read-only `view_graph` tool can also inspect a bounded
graph neighborhood; use it only after the screen-navigation prompts below.
Calling it is an MCP request and adds observability events to the logging
service, but it does not change the mainframe or graph.

### Prompt A — inspect, do not act

```text
Use the SENTINEL MCP get_observation tool to inspect the current local demo
mainframe screen. Do not change the screen. Report the screen title, available
semantic field IDs, available actions, and the current generation_token exactly
as returned. Do not infer missing values.
```

Check that the observation is the local ISPF demo and that the screen is ready.
If it is not, stop rather than navigating an unknown screen.

### Prompt B — enter the read-only Browse option

```text
Use get_observation first. Only if the current screen is the ISPF Primary
Option Menu and the observation exposes its option input field, use
set_field_and_submit to enter option 1 in that exact field. Pass the exact
generation_token from that latest observation. After the action, call
get_observation and report the resulting screen title and fields. If the
screen or field does not match, do nothing and tell me what you observed.
```

Confirm that the result is the Browse entry panel before continuing.

### Prompt C — open a read-only dataset

```text
Use get_observation to inspect the current Browse entry panel. Only if it
exposes a dataset-name input field, use set_field_and_submit with the exact
observed field ID and the read-only dataset name SYS1.PROCLIB on the local
demo mainframe.
Pass the exact generation_token from that latest observation. Do not edit or
submit any dataset contents. After the action, call get_observation and report
the resulting screen title only; do not reproduce dataset contents.
If the expected panel or field is absent, do nothing and stop.
```

Verify that a screen change occurred. That change is what should produce a
Navigator transition; an observation that leaves the screen unchanged may
produce no graph edge.

### Prompt D — exit the Browse view

```text
Use get_observation to inspect the current screen. If it is the read-only
SYS1.PROCLIB Browse view and PF3 is an available action, call trigger_action
with action_id PF3 and the exact generation_token from that observation.
Immediately call get_observation and report the resulting screen title.
Do not take any other action. If the screen or action is different, stop.
```

If the resulting screen is still a Browse panel and the current observation
offers PF3, repeat Prompt D once using the newly observed token. Never reuse
the earlier token.

### Prompt E — stop and summarize evidence

```text
Do not call any more state-changing tools. Summarize the SENTINEL observations
and actions you actually performed, including the screen titles before and
after each action. Do not claim that logging persistence or Neo4j storage was
verified; I will check those directly.
```

The Gemini summary is not the pass/fail evidence. Check both services directly
in the next section.

### Optional Prompt F — inspect the graph through MCP

Run this before quitting Gemini if you also want to verify the MCP-to-Navigator
query path:

```text
Call the read-only SENTINEL MCP view_graph tool with depth 1 and no screen_hash
so it uses the current screen. Summarize the returned screen titles and
transitions. Do not perform any other tool calls or change the mainframe.
```

This call produces MCP logging events. Include it in your event-count
comparison; it is a graph read, not a graph transition. The service and Neo4j
Browser checks below remain the direct verification of persisted data.

## 5. Verify the logging pipeline

Exit Gemini cleanly (use `/quit`) so the MCP server can flush and close its
event producer. In the first PowerShell window, check logging health again:

```powershell
curl.exe --fail http://127.0.0.1:8200/health
```

Compare the event count with the pre-test count from Step 2:

```powershell
docker compose exec -T logging-service python -c "import sqlite3; db=sqlite3.connect('/var/lib/sentinel-logging/observability.sqlite3'); print('count:',db.execute('SELECT COUNT(*) FROM events').fetchone()[0]); print('recent:',*db.execute('SELECT event_id,event_type,source_layer,source_component,timestamp_utc FROM events ORDER BY timestamp_utc DESC LIMIT 10'),sep='\n'); db.close()"
```

Pass evidence: the count increased during the Gemini session, and recent rows
include events corresponding to SENTINEL MCP activity. The query intentionally
does not print event payloads. Logging delivery is best-effort; if the count
does not increase, inspect the Gemini/MCP launch environment, producer
diagnostics, and logging-service logs:

```powershell
docker compose logs --tail 100 logging-service
```

## 6. Verify the Navigator graph

Check Navigator's totals again and compare them with the baseline:

```powershell
curl.exe --fail http://127.0.0.1:8100/graph/stats
```

After screen-changing Browse actions, expect new graph data or an increase in
existing traversal counts. A screen that has already been seen can be merged
with an existing node, so do not require `total_screens` to increase on every
run.

For a visual view, open **http://localhost:7474** and sign in with the Neo4j
credentials configured for your existing local database. The Compose default
is `neo4j` / `sentinel_graph`; an existing database may retain a password set
when it was first initialized. Run these read-only queries:

```cypher
MATCH (s:Screen)-[r:TRANSITION]->(t:Screen)
RETURN s, r, t
ORDER BY r.last_seen DESC
LIMIT 50
```

Inspect the transition records and their traversal counts:

```cypher
MATCH (s:Screen)-[r:TRANSITION]->(t:Screen)
RETURN s.title AS from_screen,
       r.action_signature AS action,
       r.traversal_count AS traversals,
       r.avg_latency_ms AS average_latency_ms,
       t.title AS to_screen,
       r.last_seen AS last_seen
ORDER BY r.last_seen DESC
LIMIT 20
```

If you see no new edge, check Navigator logs for `navigator_transition_emitted`
or `navigator_emit_failed`, check the Gemini-launched MCP server output, and
confirm `configs/mainframe.toml` has Navigator enabled at
`http://localhost:8100`. The emitter only records successful actions that
change the screen.

## 7. Finish and report

Record:

- Whether all four health checks passed (before and after the Gemini session).
- Logging event count before/after and the recent event types/components.
- Navigator totals before/after and the observed transition/action.
- Any errors or warnings from the MCP, logging, or Navigator output.

Optionally stop the local services while retaining their data:

```powershell
docker compose stop logging-service navigator neo4j mainframe
```

Do not use `docker compose down -v`; it deletes persistent data volumes.

## If you only want to test without adding to your regular data

Use the isolated synthetic integration check instead of the Gemini walkthrough:

```powershell
$env:SENTINEL_RUN_ISOLATED_INTEGRATION = "1"
python scripts/test_logging_graph_live.py
```

That check uses a unique temporary Compose project and synthetic data; its graph
and event database are removed after the run. It verifies the services and
their APIs, but it does not exercise Gemini CLI or a real mainframe session.
