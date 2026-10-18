"""Focused tests for lower-layer driver and stability observability."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from layer2.tn3270.action_lowerer import TN3270ActionLowerer
from layer3.dispatcher import ActionDispatcher
from schemas.actions import ActionResult, ActionType, CanonicalActionIntent
from schemas.contracts import L2toL3HandoffPayload
from schemas.observability import EventOutcome, EventType
from schemas.state import RuntimeState, ScreenType, StabilityReport
from services.observability_context import operation_context


class RecordingProducer:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def submit(self, event):
        if self.fail:
            raise RuntimeError("synthetic producer failure")
        self.events.append(event)
        return True


def make_state(generation=1):
    return RuntimeState(
        runtime_id="node-test",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=[" " * 80 for _ in range(24)],
        title="TEST",
        fields={},
        status_line="",
        stability_report=StabilityReport(
            is_stable=True,
            method="oia_multi_signal",
            confidence=1.0,
            delta_value=0.0,
            iterations=1,
        ),
        screen_hash="a" * 64,
        generation=generation,
    )


def make_intent(state):
    return CanonicalActionIntent(
        intent_type=ActionType.TRIGGER_ACTION,
        generation_token=state.generation_token,
        generation=state.generation,
        ticket_id="ticket-observe",
        action_id="ENTER",
    )


@pytest.mark.asyncio
async def test_driver_input_event_is_emitted_after_success_without_sensitive_values():
    producer = RecordingProducer()
    lowerer = TN3270ActionLowerer(producer)
    state = make_state()
    driver = AsyncMock()

    with operation_context(session_id="session-l2"):
        result = await lowerer.lower_and_execute(make_intent(state), driver, state)

    assert result.success is True
    assert [event.event_type for event in producer.events] == [EventType.DRIVER_INPUT_SENT]
    event = producer.events[0]
    assert event.outcome is EventOutcome.SUCCESS
    assert event.ticket_id == "ticket-observe"
    assert event.generation_before == 1
    assert event.generation_after is None
    assert event.metadata == {"action_id": "trigger_action"}
    assert event.session_id == "session-l2"
    assert "password" not in str(event.to_dict())


@pytest.mark.asyncio
async def test_driver_failure_event_is_emitted_only_after_actual_input_attempt():
    producer = RecordingProducer()
    lowerer = TN3270ActionLowerer(producer)
    state = make_state()
    driver = AsyncMock()
    driver.write_raw.side_effect = TimeoutError("secret input detail")

    with operation_context(session_id="session-l2"):
        result = await lowerer.lower_and_execute(make_intent(state), driver, state)

    assert result.success is False
    assert producer.events[0].outcome is EventOutcome.FAILURE
    assert "secret input detail" not in str(producer.events[0].to_dict())


@pytest.mark.asyncio
async def test_stability_events_share_ticket_generation_and_classify_timeout():
    producer = RecordingProducer()
    state = make_state()
    next_state = make_state(generation=2)

    class Lowerer:
        async def lower_and_execute(self, intent, driver, active_state):
            return ActionResult(
                ticket_id=intent.ticket_id,
                success=True,
                generation_before=active_state.generation,
                generation_after=active_state.generation + 1,
                screen_hash_before=active_state.screen_hash,
                screen_hash_after="",
                execution_time_ms=1.0,
            )

    class Stability:
        async def wait_until_stable(self, **_kwargs):
            return next_state, StabilityReport(
                is_stable=False,
                method="oia_multi_signal_timeout",
                confidence=0.5,
                delta_value=1.0,
                iterations=3,
            )

    dispatcher = ActionDispatcher(
        driver=SimpleNamespace(runtime_id="node-test"),
        reducer=object(),
        action_lowerer=Lowerer(),
        stability_engine=Stability(),
        observability_producer=producer,
    )

    with operation_context(session_id="session-stability"):
        result, _payload = await dispatcher.dispatch(
            "trigger_action",
            {"generation_token": state.generation_token, "action_id": "ENTER"},
            L2toL3HandoffPayload(state=state),
        )

    assert result.success is True
    assert [event.event_type for event in producer.events] == [
        EventType.STABILITY_STARTED,
        EventType.STABILITY_COMPLETED,
    ]
    started, completed = producer.events
    assert started.ticket_id == completed.ticket_id == result.ticket_id
    assert started.generation_before == completed.generation_before == 1
    assert started.generation_after == completed.generation_after == 2
    assert completed.outcome is EventOutcome.TIMEOUT
    assert completed.metadata == {
        "stability_method": "oia_multi_signal_timeout",
        "is_stable": False,
        "iterations": 3,
    }


@pytest.mark.asyncio
async def test_lower_layer_producer_failure_does_not_change_execution():
    producer = RecordingProducer(fail=True)
    lowerer = TN3270ActionLowerer(producer)
    state = make_state()
    driver = AsyncMock()

    with operation_context(session_id="session-producer"):
        result = await lowerer.lower_and_execute(make_intent(state), driver, state)

    assert result.success is True
    driver.write_raw.assert_awaited_once()
    assert producer.events == []
