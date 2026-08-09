"""Live integration test verifying TN3270 driver and state reducer navigation against Hercules TK5-MVS."""
import asyncio
import pytest

from drivers.registry import DriverRegistry
from layer4.audit_log import AuditLogger
from schemas.events import RuntimeEvent


@pytest.mark.asyncio
async def test_hercules_live_navigation():
    """Integration test connecting to live Hercules TK5-MVS, sending ENTER, and verifying state transition."""
    config_path = "configs/mainframe.toml"
    
    try:
        driver, reducer, stability, config = DriverRegistry.create_target(config_path)
    except Exception as e:
        pytest.skip(f"Target configuration load failed: {e}")

    tn3270_config = config.get("tn3270", {})
    host = tn3270_config.get("host", "127.0.0.1")
    port = tn3270_config.get("port", 3270)

    # Verify live server port 3270 is reachable before running live test
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
        writer.close()
        await writer.wait_closed()
    except Exception:
        pytest.skip(f"Hercules TK5-MVS mainframe port {port} on {host} not reachable. Skipping live integration test.")

    audit_logger = AuditLogger("logs/mainframe_audit.jsonl")

    def on_event(ev: RuntimeEvent) -> None:
        audit_logger.log_event(ev)

    driver.add_event_listener(on_event)

    try:
        await driver.connect()

        # Step 1: Initial Screen (Gen #1)
        state_1, report_1 = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=1,
        )
        audit_logger.log_state(state_1)
        assert state_1.is_stable is True
        assert state_1.generation == 1
        assert state_1.screen_hash != ""

        # Step 2: Inject ENTER key to trigger navigation
        await driver.send_aid("ENTER")

        # Step 3: Wait for stability (Gen #2)
        state_2, report_2 = await stability.wait_until_stable(
            driver=driver,
            reducer=reducer,
            runtime_id=driver.runtime_id,
            generation=2,
        )
        audit_logger.log_state(state_2)
        assert state_2.is_stable is True
        assert state_2.generation == 2
        assert state_2.screen_hash != state_1.screen_hash

    finally:
        await driver.disconnect()
