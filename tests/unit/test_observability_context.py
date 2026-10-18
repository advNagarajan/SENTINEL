"""Tests for MCP-scoped observability context and producer lifecycle."""
import asyncio
from types import SimpleNamespace

import pytest

from layer4.mcp_server import runtime_lifespan
from schemas.observability import EventOutcome, EventType, SourceLayer
from services.observability_context import current_context, new_context, operation_context


class RecordingProducer:
    def __init__(self, calls, fail_start=False, fail_close=False, submit_result=True, submit_error=False):
        self.calls = calls
        self.fail_start = fail_start
        self.fail_close = fail_close
        self.submit_result = submit_result
        self.submit_error = submit_error
        self.events = []

    async def start(self):
        self.calls.append(("producer.start", current_context()))
        if self.fail_start:
            raise RuntimeError("synthetic producer start failure")

    async def close(self, timeout_seconds=None):
        self.calls.append(("producer.close", current_context()))
        if self.fail_close:
            raise RuntimeError("synthetic producer close failure")

    def submit(self, event):
        if self.submit_error:
            raise RuntimeError("synthetic producer submit failure")
        self.events.append(event)
        return self.submit_result


class RecordingOrchestrator:
    def __init__(self, calls, fail_connect=False, fail_disconnect=False):
        self.calls = calls
        self.fail_connect = fail_connect
        self.fail_disconnect = fail_disconnect
        self._runtime_id = "runtime-test"

    def runtime_id(self):
        return self._runtime_id

    async def connect(self):
        self.calls.append(("runtime.connect", current_context()))
        if self.fail_connect:
            raise RuntimeError("synthetic runtime connect failure")

    async def disconnect(self):
        self.calls.append(("runtime.disconnect", current_context()))
        if self.fail_disconnect:
            raise RuntimeError("synthetic runtime disconnect failure")


@pytest.mark.asyncio
async def test_operation_context_isolated_across_concurrent_tasks():
    barrier = asyncio.Event()

    async def run_operation():
        with operation_context(session_id="session-shared"):
            before = current_context()
            barrier.set()
            await asyncio.sleep(0)
            after = current_context()
            return before, after

    first, second = await asyncio.gather(run_operation(), run_operation())

    assert first[0] == first[1]
    assert second[0] == second[1]
    assert first[0].session_id == second[0].session_id == "session-shared"
    assert first[0].correlation_id != second[0].correlation_id
    assert first[0].operation_id != second[0].operation_id
    assert current_context() is None


def test_exceptional_nested_contexts_restore_previous_context():
    with operation_context(session_id="outer-session") as outer:
        try:
            with operation_context(session_id="inner-session") as inner:
                assert current_context() == inner
                raise RuntimeError("synthetic operation failure")
        except RuntimeError:
            pass
        assert current_context() == outer
    assert current_context() is None


@pytest.mark.parametrize("field", ["session_id", "correlation_id", "operation_id"])
def test_context_rejects_sensitive_identifier_markers(field):
    with pytest.raises(ValueError, match=field):
        new_context(**{field: "runtime-password"})


@pytest.mark.asyncio
async def test_mcp_lifecycle_starts_producer_before_runtime_and_closes_after_disconnect():
    calls = []
    producer = RecordingProducer(calls)
    runtime = RecordingOrchestrator(calls)

    async with runtime_lifespan(
        SimpleNamespace(),
        runtime,
        producer,
        session_id="session-test",
    ):
        calls.append(("yield", current_context()))

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "yield",
        "runtime.disconnect",
        "producer.close",
    ]
    assert all(context.session_id == "session-test" for _name, context in calls)


@pytest.mark.asyncio
async def test_successful_lifecycle_events_are_valid_and_sanitized():
    calls = []
    producer = RecordingProducer(calls)
    runtime = RecordingOrchestrator(calls)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        pass

    assert [event.event_type for event in producer.events] == [
        EventType.RUNTIME_CONNECTING,
        EventType.RUNTIME_CONNECTED,
        EventType.SESSION_DISCONNECTING,
        EventType.SESSION_DISCONNECTED,
    ]
    assert [event.outcome for event in producer.events] == [
        EventOutcome.STARTED,
        EventOutcome.SUCCESS,
        EventOutcome.STARTED,
        EventOutcome.SUCCESS,
    ]
    assert all(event.source_layer is SourceLayer.LAYER4 for event in producer.events)
    assert all(event.session_id == "session-test" for event in producer.events)
    assert all(event.runtime_id == "runtime-test" for event in producer.events)
    assert all("password" not in event.to_dict() for event in producer.events)
    assert all("raw_screen" not in event.to_dict() for event in producer.events)


@pytest.mark.asyncio
async def test_connection_and_disconnection_failures_emit_safe_failure_events():
    calls = []
    producer = RecordingProducer(calls)
    runtime = RecordingOrchestrator(calls, fail_connect=True, fail_disconnect=True)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        pass

    assert [event.event_type for event in producer.events] == [
        EventType.RUNTIME_CONNECTING,
        EventType.RUNTIME_CONNECTION_FAILED,
        EventType.SESSION_DISCONNECTING,
        EventType.SESSION_DISCONNECTED,
    ]
    assert producer.events[1].metadata == {"error_category": "connection_failure"}
    assert producer.events[3].metadata == {"error_category": "transport_error"}


@pytest.mark.asyncio
async def test_producer_start_failure_does_not_block_runtime_or_cleanup():
    calls = []
    producer = RecordingProducer(calls, fail_start=True)
    runtime = RecordingOrchestrator(calls)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        calls.append(("yield", current_context()))

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "yield",
        "runtime.disconnect",
        "producer.close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("submit_error,submit_result", [(True, True), (False, False)])
async def test_producer_submission_failure_does_not_change_lifecycle(submit_error, submit_result):
    calls = []
    producer = RecordingProducer(
        calls,
        submit_result=submit_result,
        submit_error=submit_error,
    )
    runtime = RecordingOrchestrator(calls)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        pass

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "runtime.disconnect",
        "producer.close",
    ]


@pytest.mark.asyncio
async def test_runtime_disconnect_failure_does_not_prevent_producer_close():
    calls = []
    producer = RecordingProducer(calls, fail_close=True)
    runtime = RecordingOrchestrator(calls, fail_disconnect=True)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        pass

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "runtime.disconnect",
        "producer.close",
    ]


@pytest.mark.asyncio
async def test_lifespan_preserves_body_exception_through_cleanup_failures():
    calls = []
    producer = RecordingProducer(calls, fail_close=True)
    runtime = RecordingOrchestrator(calls, fail_disconnect=True)

    with pytest.raises(ValueError, match="original lifespan failure"):
        async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
            raise ValueError("original lifespan failure")

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "runtime.disconnect",
        "producer.close",
    ]


@pytest.mark.asyncio
async def test_hanging_producer_close_is_bounded_and_does_not_hang_lifespan():
    calls = []

    class HangingProducer(RecordingProducer):
        def __init__(self, calls):
            super().__init__(calls)
            self.config = SimpleNamespace(shutdown_timeout_seconds=0.01)

        async def close(self, timeout_seconds=None):
            self.calls.append(("producer.close", current_context()))
            await asyncio.Event().wait()

    producer = HangingProducer(calls)
    runtime = RecordingOrchestrator(calls)

    async with runtime_lifespan(SimpleNamespace(), runtime, producer, session_id="session-test"):
        pass

    assert [name for name, _context in calls] == [
        "producer.start",
        "runtime.connect",
        "runtime.disconnect",
        "producer.close",
    ]
