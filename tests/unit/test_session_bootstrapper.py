"""Unit tests for SessionBootstrapper."""
import pytest
from unittest.mock import AsyncMock

from layer4.session import SessionBootstrapper
from schemas.state import RuntimeState, ScreenType, StabilityReport


@pytest.mark.asyncio
async def test_session_bootstrapper_raw_entrypoint():
    """Verify bootstrapper returns raw cold screen when entrypoint is 'raw'."""
    bootstrapper = SessionBootstrapper({"entrypoint": "raw", "auto_login": True})
    driver = AsyncMock()
    reducer = AsyncMock()
    stability = AsyncMock()

    grid = [" " * 80 for _ in range(24)]
    report = StabilityReport(is_stable=True, method="mock", confidence=1.0, delta_value=0.0, iterations=1)
    cold_state = RuntimeState(
        runtime_id="test_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="Splash",
        fields={},
        status_line="",
        stability_report=report,
        screen_hash="a" * 64,
        generation=1,
    )

    stability.wait_until_stable.return_value = (cold_state, report)

    state, rep = await bootstrapper.bootstrap(driver, reducer, stability)
    assert state.title == "Splash"
    assert driver.send_aid.call_count == 0
    assert driver.send_field_input.call_count == 0


@pytest.mark.asyncio
async def test_session_bootstrapper_disabled():
    """Verify bootstrapper does nothing when auto_login is False."""
    bootstrapper = SessionBootstrapper({"auto_login": False, "entrypoint": "ispf"})
    driver = AsyncMock()
    reducer = AsyncMock()
    stability = AsyncMock()

    grid = [" " * 80 for _ in range(24)]
    report = StabilityReport(is_stable=True, method="mock", confidence=1.0, delta_value=0.0, iterations=1)
    cold_state = RuntimeState(
        runtime_id="test_node",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=grid,
        title="Cold",
        fields={},
        status_line="",
        stability_report=report,
        screen_hash="b" * 64,
        generation=1,
    )
    stability.wait_until_stable.return_value = (cold_state, report)

    state, _ = await bootstrapper.bootstrap(driver, reducer, stability)
    assert state.title == "Cold"
    assert driver.send_aid.call_count == 0
