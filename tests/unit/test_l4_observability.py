"""Tests for sanitized L4 observation lifecycle observability."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from layer4.orchestrator import RuntimeOrchestrator
from schemas.observability import EventOutcome, EventType
from services.observability_context import operation_context


class FakeProducer:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.events = []

    def submit(self, event):
        if self.error:
            raise self.error
        self.events.append(event)
        return self.result


class FakeSession:
    async def connect(self):
        pass

    async def initialize(self):
        return SimpleNamespace(runtime_id="runtime-observation")

    async def teardown(self, _state):
        pass

    async def disconnect(self):
        pass


class FakeGateway:
    def __init__(self, observation=None, screen_state=None, error=None, state=None):
        self.observation = observation or {"screen_text": ["SENSITIVE_SCREEN_TEXT"]}
        self.screen_state = screen_state or {"screen_text": ["SENSITIVE_SCREEN_TEXT"]}
        self.error = error
        self.active_payload = SimpleNamespace(state=state or SimpleNamespace(
            generation=3,
            fields={"field": object()},
            is_stable=True,
            screen_hash="a" * 64,
            stability_report=SimpleNamespace(iterations=2),
        ))

    def get_observation(self):
        if self.error:
            raise self.error
        return SimpleNamespace(to_dict=lambda: self.observation)

    async def execute_tool(self, name, arguments):
        if self.error:
            raise self.error
        return self.screen_state

    def get_active_payload(self):
        return self.active_payload


def make_orchestrator(gateway, producer):
    driver = SimpleNamespace(runtime_id="runtime-observation")
    pipeline = (driver, object(), object(), object(), object(), {})
    return RuntimeOrchestrator(
        pipeline_factory=lambda _path: pipeline,
        session_factory=lambda *_args: FakeSession(),
        gateway_factory=lambda _payload, _dispatcher: gateway,
        observability_producer=producer,
    )


@pytest.mark.asyncio
async def test_get_observation_emits_one_start_and_completion_with_safe_metadata():
    producer = FakeProducer()
    gateway = FakeGateway()
    runtime = make_orchestrator(gateway, producer)
    await runtime.connect()

    with operation_context(session_id="session-observation"):
        result = runtime.get_observation()

    assert result["screen_text"] == ["SENSITIVE_SCREEN_TEXT"]
    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_STARTED,
        EventType.OBSERVATION_COMPLETED,
    ]
    completed = producer.events[1]
    assert completed.outcome is EventOutcome.SUCCESS
    assert completed.runtime_id == "runtime-observation"
    assert completed.session_id == "session-observation"
    assert completed.metadata["generation"] == 3
    assert completed.metadata["field_count"] == 1
    assert completed.metadata["is_stable"] is True
    assert completed.metadata["iterations"] == 2
    assert completed.metadata["screen_hash"] == "a" * 64
    assert "SENSITIVE_SCREEN_TEXT" not in str([event.to_dict() for event in producer.events])


@pytest.mark.asyncio
async def test_get_screen_state_emits_one_pair_without_duplicates():
    producer = FakeProducer()
    gateway = FakeGateway(screen_state={"screen_text": ["SENSITIVE_SCREEN_TEXT"]})
    runtime = make_orchestrator(gateway, producer)
    await runtime.connect()

    with operation_context(session_id="session-screen"):
        result = await runtime.execute_tool("get_screen_state", {})

    assert result["screen_text"] == ["SENSITIVE_SCREEN_TEXT"]
    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_STARTED,
        EventType.OBSERVATION_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_observation_exception_emits_one_failure_and_reraises():
    producer = FakeProducer()
    gateway = FakeGateway(error=RuntimeError("raw observation failure"))
    runtime = make_orchestrator(gateway, producer)
    await runtime.connect()

    with operation_context(session_id="session-failure"):
        with pytest.raises(RuntimeError, match="raw observation failure"):
            runtime.get_observation()

    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_STARTED,
        EventType.OBSERVATION_COMPLETED,
    ]
    assert producer.events[1].outcome is EventOutcome.FAILURE
    assert producer.events[1].metadata["error_category"] == "unknown"
    assert "raw observation failure" not in str([event.to_dict() for event in producer.events])


@pytest.mark.asyncio
async def test_observation_event_construction_failure_does_not_change_success():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(), producer)
    await runtime.connect()

    with patch("layer4.orchestrator.ObservabilityEvent", side_effect=RuntimeError("synthetic event failure")):
        with operation_context(session_id="session-construction"):
            result = runtime.get_observation()

    assert result["screen_text"] == ["SENSITIVE_SCREEN_TEXT"]
    assert producer.events == []
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_observation_event_construction_failure_does_not_mask_observation_exception():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(error=RuntimeError("original observation failure")), producer)
    await runtime.connect()

    with patch("layer4.orchestrator.ObservabilityEvent", side_effect=RuntimeError("synthetic event failure")):
        with operation_context(session_id="session-construction"):
            with pytest.raises(RuntimeError, match="original observation failure"):
                runtime.get_observation()

    assert producer.events == []
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_get_screen_state_failure_emits_one_failure_terminal_event():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(error=RuntimeError("raw screen failure")), producer)
    await runtime.connect()

    with operation_context(session_id="session-screen-failure"):
        with pytest.raises(RuntimeError, match="raw screen failure"):
            await runtime.execute_tool("get_screen_state", {})

    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_STARTED,
        EventType.OBSERVATION_COMPLETED,
    ]
    failure = producer.events[1]
    assert failure.outcome is EventOutcome.FAILURE
    assert failure.metadata["error_category"] == "unknown"
    assert "raw screen failure" not in str(failure.to_dict())
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_missing_context_skips_observation_events():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(), producer)
    await runtime.connect()

    result = runtime.get_observation()

    assert result["screen_text"] == ["SENSITIVE_SCREEN_TEXT"]
    assert producer.events == []


@pytest.mark.asyncio
async def test_pre_readiness_observation_rejection_emits_one_sanitized_terminal_event():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(), producer)

    with operation_context(session_id="session-not-ready"):
        with pytest.raises(RuntimeError, match="not connected and ready"):
            runtime.get_observation()

    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_COMPLETED,
    ]
    failure = producer.events[0]
    assert failure.outcome is EventOutcome.FAILURE
    assert failure.metadata["error_category"] == "unknown"
    assert failure.session_id == "session-not-ready"
    assert "SENSITIVE" not in str(failure.to_dict())
    assert runtime.state.value == "disconnected"


@pytest.mark.asyncio
async def test_pre_readiness_screen_rejection_emits_one_terminal_event():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(), producer)

    with operation_context(session_id="session-screen-not-ready"):
        with pytest.raises(RuntimeError, match="not connected and ready"):
            await runtime.execute_tool("get_screen_state", {})

    assert [event.event_type for event in producer.events] == [
        EventType.OBSERVATION_COMPLETED,
    ]
    assert producer.events[0].outcome is EventOutcome.FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", [FakeProducer(result=False), FakeProducer(error=RuntimeError("producer failure"))])
async def test_producer_failure_does_not_change_observation_behavior(producer):
    runtime = make_orchestrator(FakeGateway(), producer)
    await runtime.connect()

    with operation_context(session_id="session-producer"):
        result = runtime.get_observation()

    assert result["screen_text"] == ["SENSITIVE_SCREEN_TEXT"]
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_invalid_optional_observation_metadata_is_omitted():
    producer = FakeProducer()
    state = SimpleNamespace(
        generation=-1,
        fields={},
        is_stable=False,
        screen_hash="not-a-hash",
        stability_report=SimpleNamespace(iterations=-1),
    )
    runtime = make_orchestrator(FakeGateway(state=state), producer)
    await runtime.connect()

    with operation_context(session_id="session-invalid"):
        runtime.get_observation()

    completed = producer.events[1]
    assert "generation" not in completed.metadata
    assert completed.metadata["field_count"] == 0
    assert completed.metadata["is_stable"] is False
    assert "iterations" not in completed.metadata
    assert "screen_hash" not in completed.metadata
