"""Offline tests for the standalone SENTINEL observability producer."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from schemas.observability import EventOutcome, EventType, ObservabilityEvent, Severity, SourceLayer
from services.observability_producer import DEFAULT_ENDPOINT, EventProducer, ProducerConfig


FIXED_TIMESTAMP = "2026-10-18T12:00:00Z"


def make_event(event_id: str = "evt-1") -> ObservabilityEvent:
    return ObservabilityEvent(
        event_id=event_id,
        event_type=EventType.ACTION_COMPLETED,
        timestamp_utc=FIXED_TIMESTAMP,
        severity=Severity.INFO,
        source_layer=SourceLayer.LAYER4,
        source_component="producer-test",
        outcome=EventOutcome.SUCCESS,
        correlation_id="corr-1",
        metadata={"tool_name": "trigger_action"},
    )


def test_default_and_explicit_producer_endpoints_are_preserved():
    assert ProducerConfig().endpoint == DEFAULT_ENDPOINT
    assert ProducerConfig(endpoint="http://logging.internal:8200/v1/events").endpoint == (
        "http://logging.internal:8200/v1/events"
    )


def test_empty_explicit_endpoint_is_rejected():
    with pytest.raises(ValueError, match="endpoint"):
        ProducerConfig(endpoint="")


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeClient:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.outcomes = list(outcomes or [FakeResponse(200)])
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, endpoint: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"endpoint": endpoint, **kwargs})
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse(200)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client():
    client = FakeClient()
    return client, lambda _timeout: client


@pytest.mark.asyncio
async def test_successful_submission_and_delivery(fake_client):
    client, factory = fake_client
    producer = EventProducer(client_factory=factory)

    await producer.start()
    assert producer.submit(make_event()) is True
    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert stats.accepted_events == 1
    assert stats.delivered_events == 1
    assert stats.failed_events == 0
    assert len(client.calls) == 1
    assert json.loads(client.calls[0]["content"])["events"][0]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_submit_before_start_and_queue_overflow_are_non_blocking():
    client = FakeClient()
    producer = EventProducer(
        ProducerConfig(queue_size=1),
        client_factory=lambda _timeout: client,
    )

    assert producer.submit(make_event()) is False
    await producer.start()
    assert producer.submit(make_event("evt-2")) is True
    assert producer.submit(make_event("evt-3")) is False
    assert producer.submit(make_event("evt-4")) is False
    stats = producer.stats()
    await producer.close()

    assert stats.not_running_events == 1
    assert stats.queue_overflow_events == 2
    assert stats.accepted_events == 1
    assert stats.dropped_events == 3


@pytest.mark.asyncio
async def test_loss_snapshot_is_local_only_and_cannot_recurse_into_submission():
    client = FakeClient()
    producer = EventProducer(
        ProducerConfig(queue_size=1),
        client_factory=lambda _timeout: client,
    )

    assert producer.submit(make_event()) is False
    await producer.start()
    assert producer.submit(make_event("evt-2")) is True
    assert producer.submit(make_event("evt-3")) is False

    loss_snapshot = producer.loss_stats()
    assert loss_snapshot.not_running_events == 1
    assert loss_snapshot.queue_overflow_events == 1
    assert loss_snapshot.dropped_events == 2
    assert loss_snapshot is not producer.stats()

    await producer.close()
    assert loss_snapshot.dropped_events == 2


@pytest.mark.asyncio
async def test_event_count_batch_limit():
    client = FakeClient()
    producer = EventProducer(
        ProducerConfig(max_batch_events=2),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    for index in range(5):
        assert producer.submit(make_event(f"evt-{index}")) is True

    await producer.flush()
    await producer.close()

    batches = [json.loads(call["content"])["events"] for call in client.calls]
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert sum(len(batch) for batch in batches) == 5


@pytest.mark.asyncio
async def test_serialized_byte_batch_limit():
    first = make_event()
    batch_limit = EventProducer._batch_size([first.serialized_bytes()])
    client = FakeClient()
    producer = EventProducer(
        ProducerConfig(max_batch_bytes=batch_limit),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    producer.submit(first)
    producer.submit(make_event("evt-2"))

    await producer.flush()
    await producer.close()

    assert len(client.calls) == 2
    assert all(len(call["content"]) <= batch_limit for call in client.calls)


def test_batch_limits_cannot_exceed_logging_service_limits():
    with pytest.raises(ValueError, match="max_batch_events"):
        ProducerConfig(max_batch_events=51)
    with pytest.raises(ValueError, match="max_batch_bytes"):
        ProducerConfig(max_batch_bytes=200 * 1024 + 1)


@pytest.mark.asyncio
async def test_event_larger_than_configured_batch_limit_is_accounted_as_failed():
    event = make_event()
    client = FakeClient()
    producer = EventProducer(
        ProducerConfig(max_batch_bytes=EventProducer._batch_size([event.serialized_bytes()]) - 1),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    producer.submit(event)

    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert stats.failed_events == 1
    assert stats.delivered_events == 0
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_status_retries_once(status):
    client = FakeClient([FakeResponse(status), FakeResponse(200)])
    producer = EventProducer(client_factory=lambda _timeout: client)
    await producer.start()
    producer.submit(make_event())

    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert len(client.calls) == 2
    assert stats.retry_count == 1
    assert stats.delivered_events == 1
    assert stats.failed_events == 0


@pytest.mark.asyncio
async def test_non_retryable_client_error_fails_once():
    client = FakeClient([FakeResponse(400), FakeResponse(200)])
    producer = EventProducer(client_factory=lambda _timeout: client)
    await producer.start()
    producer.submit(make_event())

    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert len(client.calls) == 1
    assert stats.retry_count == 0
    assert stats.failed_events == 1
    assert stats.exhausted_retry_events == 0
    assert stats.delivered_events == 0


@pytest.mark.asyncio
async def test_permanently_failed_multi_event_batch_counts_each_event_once():
    client = FakeClient([FakeResponse(400)])
    producer = EventProducer(
        ProducerConfig(max_batch_events=2),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    producer.submit(make_event("evt-1"))
    producer.submit(make_event("evt-2"))

    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert len(client.calls) == 1
    assert stats.failed_events == 2
    assert stats.delivered_events == 0
    assert stats.dropped_events == 0
    assert stats.retry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic")])
async def test_transport_failures_retry_once_then_account_for_event(error):
    client = FakeClient([error, error])
    producer = EventProducer(client_factory=lambda _timeout: client)
    await producer.start()
    producer.submit(make_event())

    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert len(client.calls) == 2
    assert stats.retry_count == 1
    assert stats.failed_events == 1
    assert stats.exhausted_retry_events == 1
    assert stats.delivered_events == 0


@pytest.mark.asyncio
async def test_serialization_failure_does_not_crash_worker_or_block_later_events():
    client = FakeClient()
    producer = EventProducer(client_factory=lambda _timeout: client)
    event = make_event()
    later_event = make_event("evt-2")
    later_event_bytes = later_event.serialized_bytes()
    await producer.start()
    with patch.object(ObservabilityEvent, "serialized_bytes", side_effect=[RuntimeError("synthetic"), later_event_bytes]):
        producer.submit(event)
        producer.submit(later_event)
        await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert stats.failed_events == 1
    assert stats.delivered_events == 1
    assert json.loads(client.calls[0]["content"])["events"][0]["event_id"] == "evt-2"


@pytest.mark.asyncio
async def test_generic_delivery_exception_fails_batch_and_worker_delivers_later_event():
    client = FakeClient([RuntimeError("synthetic client failure"), FakeResponse(200)])
    producer = EventProducer(client_factory=lambda _timeout: client)
    await producer.start()

    producer.submit(make_event("evt-1"))
    await producer.flush()
    producer.submit(make_event("evt-2"))
    await producer.flush()
    stats = producer.stats()
    await producer.close()

    assert len(client.calls) == 2
    assert stats.failed_events == 1
    assert stats.delivered_events == 1
    assert stats.retry_count == 0


@pytest.mark.asyncio
async def test_flush_is_bounded_and_close_drops_stuck_work():
    blocked = asyncio.Event()

    class BlockingClient(FakeClient):
        async def post(self, endpoint: str, **kwargs: Any) -> FakeResponse:
            self.calls.append({"endpoint": endpoint, **kwargs})
            await blocked.wait()
            return FakeResponse(200)

    client = BlockingClient()
    producer = EventProducer(
        ProducerConfig(shutdown_timeout_seconds=0.01),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    producer.submit(make_event())
    await asyncio.sleep(0)

    stats = await producer.close(timeout_seconds=0.01)

    assert stats.dropped_events == 1
    assert client.closed is True


@pytest.mark.asyncio
async def test_pending_event_is_dropped_once_when_shutdown_cancels_stuck_batch():
    started = asyncio.Event()

    class BlockingClient(FakeClient):
        async def post(self, endpoint: str, **kwargs: Any) -> FakeResponse:
            self.calls.append({"endpoint": endpoint, **kwargs})
            started.set()
            await asyncio.Event().wait()
            return FakeResponse(200)

    first = make_event("evt-1")
    second = make_event("evt-2")
    batch_limit = EventProducer._batch_size([first.serialized_bytes()])
    client = BlockingClient()
    producer = EventProducer(
        ProducerConfig(max_batch_bytes=batch_limit, shutdown_timeout_seconds=0.01),
        client_factory=lambda _timeout: client,
    )
    await producer.start()
    producer.submit(first)
    producer.submit(second)
    await asyncio.wait_for(started.wait(), timeout=0.5)

    assert producer._pending_event is second
    stats = await producer.close(timeout_seconds=0.01)
    await asyncio.wait_for(producer._queue.join(), timeout=0.1)

    assert stats.dropped_events == 2
    assert stats.failed_events == 0
    assert client.closed is True


@pytest.mark.asyncio
async def test_http_timeout_configuration_is_passed_to_client_factory():
    captured = {}
    client = FakeClient()

    def factory(timeout):
        captured["timeout"] = timeout
        return client

    config = ProducerConfig(
        connect_timeout_seconds=0.11,
        write_timeout_seconds=0.22,
        read_timeout_seconds=0.33,
        pool_timeout_seconds=0.44,
    )
    producer = EventProducer(config, client_factory=factory)
    await producer.start()
    await producer.close()

    timeout = captured["timeout"]
    assert timeout.connect == 0.11
    assert timeout.write == 0.22
    assert timeout.read == 0.33
    assert timeout.pool == 0.44


@pytest.mark.asyncio
async def test_repeated_start_and_close_are_safe(fake_client):
    client, factory = fake_client
    producer = EventProducer(client_factory=factory)

    await producer.start()
    await producer.start()
    first_close = await producer.close()
    second_close = await producer.close()

    assert first_close == second_close
    assert client.closed is True


@pytest.mark.asyncio
async def test_only_observability_events_are_accepted(fake_client):
    _client, factory = fake_client
    producer = EventProducer(client_factory=factory)
    await producer.start()

    with pytest.raises(TypeError):
        producer.submit({"event_id": "raw"})
    with pytest.raises(TypeError):
        producer.submit("raw event")

    await producer.close()


@pytest.mark.asyncio
async def test_payload_uses_approved_safe_serialization(fake_client):
    client, factory = fake_client
    producer = EventProducer(client_factory=factory)
    await producer.start()
    producer.submit(make_event())
    await producer.flush()
    await producer.close()

    body = client.calls[0]["content"]
    assert b"password" not in body
    assert b"screen_text" not in body
    assert b"typed_value" not in body
    assert json.loads(body)["events"][0]["schema_version"] == "1.0"


@pytest.mark.asyncio
async def test_http_response_body_is_never_read_or_logged(fake_client):
    class SensitiveResponse(FakeResponse):
        @property
        def text(self) -> str:
            raise AssertionError("response body must not be read")

    client = FakeClient([SensitiveResponse(400)])
    producer = EventProducer(client_factory=lambda _timeout: client)
    await producer.start()
    producer.submit(make_event())

    stats = await producer.flush()
    await producer.close()

    assert stats.failed_events == 1
