# SENTINEL

## Gemini CLI MCP Agent

Gemini CLI connects to SENTINEL only through the project-configured stdio MCP server:

```text
Gemini CLI -> MCP stdio -> SENTINEL MCP Server -> RuntimeOrchestrator -> L3/L2/L1
```

### Environment setup

From the repository root, create a virtual environment and install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
npm install --global @google/gemini-cli
```

On macOS or Linux, activate with `source .venv/bin/activate`; the repository-local launcher discovers the environment interpreter on either platform. The Gemini project configuration does not contain a user-specific path. It starts the launcher from the repository root, and the launcher resolves its own location before starting the MCP server.

### Start and inspect

From the repository root, run `gemini` to load `.gemini/settings.json`. Use `/mcp` inside Gemini CLI, or run `gemini mcp list` from the repository root, to inspect the SENTINEL connection and tool list. Approve/trust the project folder if Gemini CLI requests it. The server uses stdio and does not expose an HTTP URL.

For a direct server check, run `python scripts/run_sentinel_mcp.py`. To run the MCP protocol tests, install development dependencies with `python -m pip install -e ".[dev]"` and run `python -m pytest tests/integration/test_mcp_stdio.py tests/unit/test_mcp_launcher.py`.

State-changing Gemini smoke tests require a reachable, controlled TN3270 test target at the address configured in `configs/mainframe.toml`. Begin with observation only; use a disposable test account and data before trying semantic actions. Do not include credentials in prompts, logs, or test fixtures.

## ARM L1 → L2 live demo

The ARM demo connects to an already-running QEMU GDB endpoint. QEMU and GDB communicate through ARM L1; the resulting `TransportFrame` is passed to ARM L2, which decodes the selected counter bytes and computes the state delta. The demo never starts QEMU.

ARM stability means that consecutive semantic observations match while GDB/MI provides evidence that the target is stopped. A single sample is always insufficient. By default, ARM L2 requires two matching state hashes; timestamps, frame sequence, generation, and provenance do not affect that comparison. A changed state is `UNSTABLE`; missing execution evidence remains `UNKNOWN`. ARM L1 first asks structured MI thread information for the execution state. If QEMU's remote target has no thread entry, L1 uses the latest actual `*running` or `*stopped` async MI record, clearing old evidence before each continue or step. It does not infer stopped state from a successful Python call. Stop reason is retained from the `*stopped` record where GDB supplies one.

The live path establishes its boundaries by stopping at `main`, sampling the stopped state twice, executing the existing counter-store breakpoint/step action, and sampling the post-step state twice. L2 can report a validated transition only when both boundaries are stable, the post-action MI stop and reason are observed, and the delta shows the selected counter advanced by one. If the target does not provide those signals, the demo reports `UNKNOWN` or an unvalidated transition with the reason. QEMU's remote stub may omit thread information or stop-reason fields; the MI event fallback helps only when a corresponding async run/stop record was actually received.

### ARM through the existing MCP server

The existing screen tools continue through the configured IBM or DOS `RuntimeOrchestrator` and L3 gateway. ARM has a separate Layer 4 adapter because ARM L2 state cannot be represented by the screen-oriented `RuntimeState` or `L2toL3HandoffPayload`. The same MCP server registers `get_arm_observation` and `execute_arm_action`; those tools route only to ARM L1/L2 and never pass ARM state through the DOS or IBM dispatcher.

ARM MCP is opt-in. Copy or edit `configs/arm.toml`, set its local firmware path (or set `SENTINEL_ARM_FIRMWARE`), and keep QEMU configured as an already-running GDB endpoint. Start the existing MCP server with both runtime configs, for example in PowerShell:

```powershell
$env:SENTINEL_CONFIG = "configs/freedos.toml"
$env:SENTINEL_ARM_CONFIG = "configs/arm.toml"
python scripts/run_sentinel_mcp.py
```

To use Gemini CLI, launch `gemini` from that same shell so it inherits both config variables, then run `/mcp` to confirm SENTINEL exposes the two ARM tools. The project MCP transport remains stdio; other compatible clients should launch `scripts/run_sentinel_mcp.py` with the same environment variables.

The server does not launch QEMU. At MCP startup, the ARM adapter connects to the configured GDB endpoint and stops at `entry_breakpoint` (default `main`). If QEMU/GDB is unavailable, ARM MCP calls return structured runtime-unavailable errors; existing DOS tools retain their existing route. `get_arm_observation` requires the configured `runtime_id` and returns the raw L1 TransportFrame payload, normalized L1 observation, and L2 ARMState including decoded selected memory, completeness, stability, firmware identity, and hash. Subsequent observations include an L2 state delta when the prior and current firmware identities allow one.

`execute_arm_action` accepts exactly the configured runtime id, `action: "increment_counter"`, and the configured counter symbol (default `counter`). It does not accept debugger commands or arbitrary writes. Its response includes the mapper status, plan, before/after ARMState, ARMStateDelta, execution evidence, and cleanup diagnostics. Rejected/pre-execution requests report `execution_attempted: false`; uncertain actions are returned as uncertain and are never retried automatically. Example MCP arguments:

```json
{"runtime_id":"arm_qemu_01"}
```

```json
{"runtime_id":"arm_qemu_01","action":"increment_counter","symbol":"counter"}
```

The automated MCP stdio integration test uses a deterministic ARM adapter double to verify tool discovery, schemas, routing, structured results, and runtime separation. The offline adapter test composes the real ARM abstractor and action mapper with a mocked L1 driver. Both live ARM integration tests skip before opening a connection unless `SENTINEL_ARM_LIVE_TEST=1` is set. They require an already-running configured QEMU endpoint, do not start QEMU, increment the firmware counter, and leave the target at the post-step stop; use a controlled target whose counter may be changed. For example, in PowerShell, set `$env:SENTINEL_ARM_LIVE_TEST = "1"` before running `uv run pytest tests/integration/test_arm_mcp_live.py -v -s` or `uv run pytest tests/integration/test_arm_live.py -v -s`. These tests verify protocol-level integration, not a Gemini or other AI-agent session.

In VS Code, use three integrated terminals as needed:

1. **Terminal 1:** start a halted QEMU ARM target with its GDB server on port 1234: `qemu-system-arm -M virt -cpu cortex-a15 -S -gdb tcp::1234 -nographic`. ARM L1 loads the firmware through GDB.
2. **Terminal 2:** from the repository root, run `uv run python -m services.arm_demo`. Configure `SENTINEL_ARM_FIRMWARE` if the firmware is not at `C:\sentinel-arm\firmware.elf`; `SENTINEL_ARM_GDB_HOST`, `SENTINEL_ARM_GDB_PORT`, and `SENTINEL_ARM_GDB` can select the endpoint and cross-GDB executable. Expect two raw L1 counter byte strings, two L2 decoded values, stability/completeness details, and a delta. `transition: valid` appears only when all validation evidence is present.
3. **Optional terminal 3:** to run the live end-to-end integration test, set `$env:SENTINEL_ARM_LIVE_TEST = "1"` first, then run `uv run pytest tests/integration/test_arm_live.py -v -s`. The test increments the target counter and leaves QEMU stopped after the store. Without the opt-in it skips before connecting. The composition can be exercised without QEMU using `uv run pytest tests/unit/test_arm_demo.py -v`.
