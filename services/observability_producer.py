"""Bounded, best-effort producer for SENTINEL observability events."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
from typing import Any, Callable, Optional

import httpx

from schemas.observability import ObservabilityEvent


DEFAULT_ENDPOINT = "http://localhost:8200/v1/events"
DEFAULT_QUEUE_SIZE = 1000
DEFAULT_MAX_BATCH_EVENTS = 50
DEFAULT_MAX_BATCH_BYTES = 200 * 1024
DEFAULT_FLUSH_INTERVAL_SECONDS = 0.25
DEFAULT_CONNECT_TIMEOUT_SECONDS = 0.1
DEFAULT_WRITE_TIMEOUT_SECONDS = 0.25
DEFAULT_READ_TIMEOUT_SECONDS = 0.5
DEFAULT_POOL_TIMEOUT_SECONDS = 0.1
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_DELAY_SECONDS = 0.05
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ProducerConfig:
    """Bounded producer and HTTP delivery settings."""

    endpoint: str = DEFAULT_ENDPOINT
    queue_size: int = DEFAULT_QUEUE_SIZE
    max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES
    flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise ValueError("endpoint must be a non-empty string")
        for name in ("queue_size", "max_batch_events", "max_batch_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_batch_events > DEFAULT_MAX_BATCH_EVENTS:
            raise ValueError(f"max_batch_events must not exceed {DEFAULT_MAX_BATCH_EVENTS}")
        if self.max_batch_bytes > DEFAULT_MAX_BATCH_BYTES:
            raise ValueError(f"max_batch_bytes must not exceed {DEFAULT_MAX_BATCH_BYTES}")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        for name in (
            "flush_interval_seconds",
            "connect_timeout_seconds",
            "write_timeout_seconds",
            "read_timeout_seconds",
            "pool_timeout_seconds",
            "retry_delay_seconds",
            "shutdown_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")


@dataclass
class ProducerStats:
    """Local delivery counters; these are never submitted as events.

    These counters are intentionally local-only. Reading them cannot enqueue,
    transmit, or recursively generate another observability event.
    """

    accepted_events: int = 0
    delivered_events: int = 0
    failed_events: int = 0
    dropped_events: int = 0
    retry_count: int = 0
    exhausted_retry_events: int = 0
    queue_overflow_events: int = 0
    not_running_events: int = 0

    def snapshot(self) -> "ProducerStats":
        return replace(self)


ClientFactory = Callable[[httpx.Timeout], Any]


class EventProducer:
    """Single-worker, bounded, best-effort HTTP event producer."""

    def __init__(
        self,
        config: Optional[ProducerConfig] = None,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self.config = config or ProducerConfig()
        self._client_factory = client_factory or self._default_client_factory
        self._queue: asyncio.Queue[ObservabilityEvent] = asyncio.Queue(self.config.queue_size)
        self._worker: Optional[asyncio.Task[None]] = None
        self._client: Any = None
        self._pending_event: Optional[ObservabilityEvent] = None
        self._active_batch: list[ObservabilityEvent] = []
        self._accepting = False
        self._closed = False
        self._stats = ProducerStats()

    async def start(self) -> None:
        """Start the single background worker; repeated starts are harmless."""
        if self._closed:
            raise RuntimeError("EventProducer is closed")
        if self._worker is not None and not self._worker.done():
            return
        timeout = httpx.Timeout(
            timeout=self.config.read_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
            write=self.config.write_timeout_seconds,
            read=self.config.read_timeout_seconds,
            pool=self.config.pool_timeout_seconds,
        )
        self._client = self._client_factory(timeout)
        self._accepting = True
        self._worker = asyncio.create_task(self._run(), name="sentinel-observability-producer")

    def submit(self, event: ObservabilityEvent) -> bool:
        """Enqueue a validated event without waiting or performing network I/O."""
        if not isinstance(event, ObservabilityEvent):
            raise TypeError("submit accepts only ObservabilityEvent instances")
        if not self._accepting:
            self._stats.not_running_events += 1
            self._stats.dropped_events += 1
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats.queue_overflow_events += 1
            self._stats.dropped_events += 1
            return False
        self._stats.accepted_events += 1
        return True

    async def flush(self, timeout_seconds: Optional[float] = None) -> ProducerStats:
        """Wait for queued and in-flight events up to a bounded timeout."""
        timeout = self._timeout_or_default(timeout_seconds, self.config.shutdown_timeout_seconds)
        if self._worker is None or self._worker.done():
            return self.stats()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            return self.stats()
        return self.stats()

    async def close(self, timeout_seconds: Optional[float] = None) -> ProducerStats:
        """Stop acceptance and drain or drop remaining work within a deadline."""
        if self._closed:
            return self.stats()
        self._accepting = False
        timeout = self._timeout_or_default(timeout_seconds, self.config.shutdown_timeout_seconds)
        try:
            if self._worker is not None and not self._worker.done():
                try:
                    await asyncio.wait_for(self._queue.join(), timeout=timeout)
                except asyncio.TimeoutError:
                    self._worker.cancel()
                finally:
                    await self._stop_worker()
                    self._drop_remaining()
        finally:
            await self._close_client()
            self._closed = True
        return self.stats()

    def stats(self) -> ProducerStats:
        return self._stats.snapshot()

    def loss_stats(self) -> ProducerStats:
        """Return a local snapshot of producer loss counters.

        This is diagnostic visibility only. It does not report through the
        producer, so it remains available without network access and cannot
        recursively create more observability events.
        """
        return self._stats.snapshot()

    async def _run(self) -> None:
        while True:
            first = await self._next_event()
            if first is None:
                continue
            batch, payloads = self._build_batch(first)
            if not batch:
                continue
            self._active_batch = batch
            try:
                delivered, exhausted_retries = await self._deliver(payloads)
                if delivered:
                    self._stats.delivered_events += len(batch)
                else:
                    self._stats.failed_events += len(batch)
                    if exhausted_retries:
                        self._stats.exhausted_retry_events += len(batch)
            except asyncio.CancelledError:
                self._stats.dropped_events += len(batch)
                raise
            finally:
                for _event in batch:
                    self._queue.task_done()
                self._active_batch = []

    async def _next_event(self) -> Optional[ObservabilityEvent]:
        if self._pending_event is not None:
            event = self._pending_event
            self._pending_event = None
            return event
        try:
            return await asyncio.wait_for(
                self._queue.get(),
                timeout=self.config.flush_interval_seconds,
            )
        except asyncio.TimeoutError:
            return None

    def _build_batch(
        self,
        first: ObservabilityEvent,
    ) -> tuple[list[ObservabilityEvent], list[bytes]]:
        batch: list[ObservabilityEvent] = []
        payloads: list[bytes] = []
        total_bytes = len(b'{"events":[]}')
        candidate = self._serialize_event(first)
        if candidate is None:
            self._queue.task_done()
            return [], []
        if self._batch_size([candidate]) > self.config.max_batch_bytes:
            self._stats.failed_events += 1
            self._queue.task_done()
            return [], []
        batch.append(first)
        payloads.append(candidate)
        total_bytes = self._batch_size(payloads)

        while len(batch) < self.config.max_batch_events:
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            serialized = self._serialize_event(event)
            if serialized is None:
                self._queue.task_done()
                continue
            candidate_payloads = payloads + [serialized]
            if self._batch_size(candidate_payloads) > self.config.max_batch_bytes:
                self._pending_event = event
                break
            batch.append(event)
            payloads.append(serialized)
            total_bytes = self._batch_size(payloads)
        del total_bytes
        return batch, payloads

    @staticmethod
    def _batch_size(payloads: list[bytes]) -> int:
        return len(b'{"events":[') + sum(len(payload) for payload in payloads) + max(0, len(payloads) - 1) + len(b"]}")

    def _serialize_event(self, event: ObservabilityEvent) -> Optional[bytes]:
        try:
            payload = event.serialized_bytes()
            json.loads(payload)
            return payload
        except Exception:
            self._stats.failed_events += 1
            return None

    async def _deliver(self, payloads: list[bytes]) -> tuple[bool, bool]:
        body = b'{"events":[' + b",".join(payloads) + b"]}"
        attempts = 0
        while True:
            try:
                response = await self._client.post(
                    self.config.endpoint,
                    content=body,
                    headers={"content-type": "application/json"},
                )
                status = int(response.status_code)
                if 200 <= status < 300:
                    return True, False
                retryable = status == 429 or status >= 500
            except (httpx.TimeoutException, httpx.TransportError):
                retryable = True
            except Exception:
                retryable = False

            if not retryable or attempts >= self.config.max_retries:
                return False, retryable
            attempts += 1
            self._stats.retry_count += 1
            if self.config.retry_delay_seconds:
                await asyncio.sleep(self.config.retry_delay_seconds)

    async def _stop_worker(self) -> None:
        if self._worker is None or self._worker.done():
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass

    def _drop_remaining(self) -> None:
        if self._pending_event is not None:
            self._stats.dropped_events += 1
            self._queue.task_done()
            self._pending_event = None
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._stats.dropped_events += 1
            self._queue.task_done()

    async def _close_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        finally:
            self._client = None

    @staticmethod
    def _timeout_or_default(value: Optional[float], default: float) -> float:
        timeout = default if value is None else value
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        return float(timeout)

    @staticmethod
    def _default_client_factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout)
