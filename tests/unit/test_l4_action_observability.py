"""Tests for sanitized L4 action lifecycle observability."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class RecordingAudit:
    def __init__(self):
        self.starts = []
        self.completions = []

    def log_action_start(self, **fields):
        self.starts.append(fields)

    def log_action_completion(self, **fields):
        self.completions.append(fields)


class FakeSession:
    async def connect(self):
        pass

    async def initialize(self):
        return SimpleNamespace(runtime_id="runtime-test")

    async def teardown(self, _state):
        pass

    async def disconnect(self):
        pass


class FakeGateway:
    def __init__(self, result=None, error=None):
        self.result = result or {"success": True}
        self.error = error
        self.execute_calls = []
        self.active_payload = SimpleNamespace(
            state=SimpleNamespace(runtime_id="runtime-test", generation=1)
        )

    async def execute_tool(self, name, arguments):
        self.execute_calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result

    def get_active_payload(self):
        return self.active_payload


def make_orchestrator(gateway, producer, audit):
    driver = SimpleNamespace(runtime_id="runtime-test")
    pipeline = (driver, object(), object(), object(), object(), {})
    return RuntimeOrchestrator(
        pipeline_factory=lambda _path: pipeline,
        session_factory=lambda *_args: FakeSession(),
        gateway_factory=lambda _payload, _dispatcher: gateway,
        audit_logger=audit,
        observability_producer=producer,
    )


@pytest.mark.asyncio
async def test_successful_action_emits_start_and_completion():
    producer = FakeProducer()
    audit = RecordingAudit()
    gateway = FakeGateway(
        {
            "success": True,
            "ticket_id": "ticket-1",
            "generation_before": 1,
            "generation_after": 2,
            "execution_time_ms": 12.5,
        }
    )
    runtime = make_orchestrator(gateway, producer, audit)
    await runtime.connect()

    with operation_context(session_id="session-test"):
        sensitive_value = "SENSITIVE_TEST_VALUE_6A"
        result = await runtime.execute_tool(
            "trigger_action",
            {"action_id": "ENTER", "value": sensitive_value},
        )

    assert result["success"] is True
    assert [event.event_type for event in producer.events] == [
        EventType.ACTION_STARTED,
        EventType.ACTION_COMPLETED,
    ]
    completed = producer.events[1]
    assert completed.ticket_id == "ticket-1"
    assert completed.generation_before == 1
    assert completed.generation_after == 2
    assert completed.duration_ms == 12.5
    assert completed.session_id == "session-test"
    assert completed.runtime_id == "runtime-test"
    assert completed.correlation_id.startswith("corr-")
    assert completed.operation_id.startswith("op-")
    assert producer.events[0].generation_before == 1
    payloads = [event.to_dict() for event in producer.events]
    assert sensitive_value not in str(payloads)
    assert all(payload["metadata"] == {"tool_name": "trigger_action"} for payload in payloads)
    assert all("action_id" not in payload and "arguments" not in payload for payload in payloads)
    assert payloads[0]["outcome"] == "started"
    assert payloads[1]["outcome"] == "success"
    assert len(audit.starts) == 1
    assert len(audit.completions) == 1


@pytest.mark.asyncio
async def test_event_construction_failure_does_not_change_action_or_audit_behavior():
    producer = FakeProducer()
    audit = RecordingAudit()
    gateway = FakeGateway({"success": True, "ticket_id": "ticket-construction"})
    runtime = make_orchestrator(gateway, producer, audit)
    await runtime.connect()

    expected = {"success": True, "ticket_id": "ticket-construction"}
    with patch("layer4.orchestrator.ObservabilityEvent", side_effect=RuntimeError("synthetic event failure")):
        with operation_context(session_id="session-test"):
            result = await runtime.execute_tool("trigger_action", {})

    assert result == expected
    assert producer.events == []
    assert len(audit.starts) == 1
    assert len(audit.completions) == 1
    assert runtime.state.value == "ready"

    with operation_context(session_id="session-test"):
        assert await runtime.execute_tool("trigger_action", {}) == expected


@pytest.mark.asyncio
async def test_contract_rejection_emits_rejected_without_ticket():
    producer = FakeProducer()
    gateway = FakeGateway(
        {
            "success": False,
            "error": "ContractValidationError",
            "message": "raw sensitive detail must not be logged",
            "generation_before": 4,
            "generation_after": 4,
        }
    )
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-test"):
        result = await runtime.execute_tool("trigger_action", {})

    assert result["success"] is False
    assert [event.event_type for event in producer.events] == [
        EventType.ACTION_STARTED,
        EventType.ACTION_REJECTED,
    ]
    rejected = producer.events[1]
    assert rejected.ticket_id is None
    assert rejected.metadata == {
        "tool_name": "trigger_action",
        "error_category": "contract_validation",
    }
    assert "raw sensitive detail" not in str(rejected.to_dict())


@pytest.mark.asyncio
async def test_structured_failure_emits_one_failed_terminal_event():
    producer = FakeProducer()
    gateway = FakeGateway(
        {
            "success": False,
            "error": "TimeoutError",
            "ticket_id": "ticket-failed",
            "generation_before": 2,
            "generation_after": 2,
            "execution_time_ms": 4.0,
        }
    )
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-test"):
        await runtime.execute_tool("trigger_action", {})

    assert [event.event_type for event in producer.events] == [
        EventType.ACTION_STARTED,
        EventType.ACTION_FAILED,
    ]
    failed = producer.events[1]
    assert failed.metadata["error_category"] == "timeout"
    assert failed.ticket_id == "ticket-failed"
    assert failed.duration_ms == 4.0


@pytest.mark.asyncio
async def test_stability_timeout_is_visible_without_reclassifying_successful_input():
    producer = FakeProducer()
    gateway = FakeGateway({
        "success": True,
        "ticket_id": "ticket-timeout",
        "generation_before": 1,
        "generation_after": 2,
        "is_stable": False,
    })
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-timeout"):
        result = await runtime.execute_tool("trigger_action", {})

    assert result["success"] is True
    terminal = producer.events[1]
    assert terminal.event_type is EventType.ACTION_COMPLETED
    assert terminal.outcome is EventOutcome.SUCCESS
    assert terminal.metadata["is_stable"] is False


@pytest.mark.asyncio
async def test_action_cancellation_emits_terminal_cancelled_event_and_propagates():
    producer = FakeProducer()
    gateway = FakeGateway(error=asyncio.CancelledError())
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-cancel"):
        with pytest.raises(asyncio.CancelledError):
            await runtime.execute_tool("trigger_action", {})

    assert [event.event_type for event in producer.events] == [
        EventType.ACTION_STARTED,
        EventType.ACTION_FAILED,
    ]
    assert producer.events[1].outcome is EventOutcome.CANCELLED
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_missing_result_generation_uses_verified_start_only_without_fabrication():
    producer = FakeProducer()
    gateway = FakeGateway({"success": True, "ticket_id": "ticket-only"})
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-test"):
        result = await runtime.execute_tool("trigger_action", {})

    assert result == {"success": True, "ticket_id": "ticket-only"}
    started, completed = producer.events
    assert started.generation_before == 1
    assert completed.ticket_id == "ticket-only"
    assert completed.generation_before == 1
    assert completed.generation_after is None


@pytest.mark.asyncio
async def test_gateway_exception_emits_failure_and_preserves_exception():
    producer = FakeProducer()
    gateway = FakeGateway(error=RuntimeError("raw exception must not be logged"))
    audit = RecordingAudit()
    runtime = make_orchestrator(gateway, producer, audit)
    await runtime.connect()

    with operation_context(session_id="session-test"):
        with pytest.raises(RuntimeError, match="raw exception must not be logged"):
            await runtime.execute_tool("trigger_action", {})

    assert [event.event_type for event in producer.events] == [
        EventType.ACTION_STARTED,
        EventType.ACTION_FAILED,
    ]
    assert producer.events[1].metadata["error_category"] == "unknown"
    assert "raw exception" not in str(producer.events[1].to_dict())
    assert len(audit.completions) == 1
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", [FakeProducer(result=False), FakeProducer(error=RuntimeError("producer failure"))])
async def test_producer_failure_does_not_change_action_behavior(producer):
    gateway = FakeGateway({"success": True, "ticket_id": "ticket-ok"})
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-test"):
        result = await runtime.execute_tool("trigger_action", {})

    assert result["success"] is True
    assert runtime.state.value == "ready"


@pytest.mark.asyncio
async def test_invalid_optional_result_fields_are_omitted():
    producer = FakeProducer()
    gateway = FakeGateway(
        {
            "success": True,
            "ticket_id": "invalid ticket",
            "generation_before": -1,
            "generation_after": True,
            "execution_time_ms": float("inf"),
        }
    )
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    with operation_context(session_id="session-test"):
        await runtime.execute_tool("trigger_action", {})

    completed = producer.events[1]
    assert completed.ticket_id is None
    assert completed.generation_before is None
    assert completed.generation_after is None
    assert completed.duration_ms is None


@pytest.mark.asyncio
async def test_direct_l4_call_without_context_skips_events():
    producer = FakeProducer()
    gateway = FakeGateway({"success": True, "ticket_id": "ticket-direct"})
    runtime = make_orchestrator(gateway, producer, RecordingAudit())
    await runtime.connect()

    result = await runtime.execute_tool("trigger_action", {})

    assert result["success"] is True
    assert producer.events == []


@pytest.mark.asyncio
async def test_pre_readiness_action_rejection_emits_one_failure_and_preserves_exception():
    producer = FakeProducer()
    audit = RecordingAudit()
    runtime = make_orchestrator(FakeGateway(), producer, audit)

    with operation_context(session_id="session-action-not-ready"):
        with pytest.raises(RuntimeError, match="not connected and ready"):
            await runtime.execute_tool("trigger_action", {"value": "SENSITIVE_TEST_VALUE"})

    assert [event.event_type for event in producer.events] == [EventType.ACTION_FAILED]
    failure = producer.events[0]
    assert failure.outcome is EventOutcome.FAILURE
    assert failure.metadata == {
        "tool_name": "trigger_action",
        "error_category": "unknown",
    }
    assert failure.session_id == "session-action-not-ready"
    assert "SENSITIVE_TEST_VALUE" not in str(failure.to_dict())
    assert audit.starts == []
    assert audit.completions == []
    assert runtime.state.value == "disconnected"


@pytest.mark.asyncio
async def test_pre_readiness_action_without_context_preserves_existing_silence():
    producer = FakeProducer()
    runtime = make_orchestrator(FakeGateway(), producer, RecordingAudit())

    with pytest.raises(RuntimeError, match="not connected and ready"):
        await runtime.execute_tool("trigger_action", {})

    assert producer.events == []
