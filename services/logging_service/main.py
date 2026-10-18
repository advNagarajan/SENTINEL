"""FastAPI application for isolated SENTINEL event ingestion."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from schemas.observability import (
    EventOutcome,
    EventType,
    EventValidationError,
    ObservabilityEvent,
    Severity,
    SourceLayer,
)
from services.logging_service.models import (
    MAX_BATCH_BYTES,
    ServiceLimits,
    parse_retention_days,
    parse_retention_interval_seconds,
)
from services.logging_service.storage import SQLiteEventStorage


logger = logging.getLogger("sentinel.logging_service")
DEFAULT_DATABASE_PATH = "logs/observability.sqlite3"
EVENTS_PATH = "/v1/events"
MAX_FUTURE_TIMESTAMP_SKEW = timedelta(minutes=5)
RETENTION_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class RequestBodyLimitMiddleware:
    """Reject oversized event requests while receiving bounded body chunks."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != EVENTS_PATH:
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._send_rejection(send)
            return

        buffered_messages: list[dict[str, Any]] = []
        received_bytes = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                buffered_messages.append(message)
                break

            body = message.get("body", b"")
            received_bytes += len(body)
            if received_bytes > self.max_bytes:
                await self._send_rejection(send)
                return
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal message_index
            if message_index >= len(buffered_messages):
                return {"type": "http.disconnect"}
            message = buffered_messages[message_index]
            message_index += 1
            return message

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _content_length(scope: dict[str, Any]) -> Optional[int]:
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    async def _send_rejection(self, send: Any) -> None:
        body = b'{"detail":"event batch exceeds size limit"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _event_from_dict(data: Any) -> ObservabilityEvent:
    if not isinstance(data, dict):
        raise EventValidationError("event must be an object")
    try:
        normalized = dict(data)
        normalized["event_type"] = EventType(normalized["event_type"])
        normalized["severity"] = Severity(normalized["severity"])
        normalized["source_layer"] = SourceLayer(normalized["source_layer"])
        normalized["outcome"] = EventOutcome(normalized["outcome"])
        return ObservabilityEvent(**normalized)
    except (KeyError, TypeError, ValueError, EventValidationError) as exc:
        raise EventValidationError("event failed schema validation") from exc


def _validate_event_timestamp_window(event: ObservabilityEvent) -> None:
    """Reject future-dated events beyond bounded host clock skew.

    Older timestamps retain the existing retention behavior: they may be
    accepted and then removed by the normal retention pass after ingestion.
    """
    parsed = datetime.fromisoformat(event.timestamp_utc[:-1] + "+00:00")
    if parsed > datetime.now(timezone.utc) + MAX_FUTURE_TIMESTAMP_SKEW:
        raise EventValidationError("event timestamp is beyond the allowed future skew")


async def _retention_loop(
    storage: SQLiteEventStorage,
    interval_seconds: float,
    state: dict[str, Optional[asyncio.Future[Any]]],
) -> None:
    """Run serialized, best-effort cleanup until the lifespan is cancelled."""
    while True:
        await asyncio.sleep(interval_seconds)
        loop = asyncio.get_running_loop()
        cleanup_future = loop.run_in_executor(None, storage.purge_expired)
        state["future"] = cleanup_future
        try:
            await asyncio.shield(cleanup_future)
        except asyncio.CancelledError:
            # The shield keeps the SQLite operation alive until it finishes, so
            # shutdown can defer closing storage instead of closing it beneath
            # an active cleanup.
            raise
        except Exception:
            logger.error("event_retention_cleanup_failed")
        finally:
            if cleanup_future.done():
                state["future"] = None


def _close_storage_after(future: asyncio.Future[Any], storage: SQLiteEventStorage) -> None:
    """Close storage after a deferred executor cleanup has stopped using it."""
    try:
        future.result()
    except BaseException:
        pass
    storage.close()


def create_app(
    storage: Optional[SQLiteEventStorage] = None,
    limits: Optional[ServiceLimits] = None,
) -> FastAPI:
    active_limits = limits or ServiceLimits(
        retention_days=parse_retention_days(os.getenv("SENTINEL_LOG_RETENTION_DAYS")),
        retention_interval_seconds=parse_retention_interval_seconds(
            os.getenv("SENTINEL_LOG_RETENTION_INTERVAL_SECONDS")
        ),
    )
    active_storage = storage or SQLiteEventStorage(
        path=os.getenv("SENTINEL_LOG_DATABASE", DEFAULT_DATABASE_PATH),
        retention_days=active_limits.retention_days,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        active_storage.open()
        cleanup_state: dict[str, Optional[asyncio.Future[Any]]] = {"future": None}
        cleanup_task = asyncio.create_task(
            _retention_loop(
                active_storage,
                active_limits.retention_interval_seconds,
                cleanup_state,
            )
        )
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await asyncio.wait_for(
                    cleanup_task,
                    timeout=RETENTION_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.error("event_retention_cleanup_shutdown_timeout")

            pending_cleanup = cleanup_state["future"]
            if pending_cleanup is not None and not pending_cleanup.done():
                pending_cleanup.add_done_callback(
                    lambda future: _close_storage_after(future, active_storage)
                )
            elif cleanup_task.done() or cleanup_task.cancelled():
                active_storage.close()
            else:
                cleanup_task.add_done_callback(
                    lambda _task: active_storage.close()
                )

    app = FastAPI(
        title="SENTINEL Logging Service",
        description="Sanitized structured event ingestion for SENTINEL",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=min(MAX_BATCH_BYTES, active_limits.max_batch_bytes),
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        if not active_storage.is_healthy():
            return _error(503, "logging storage unavailable")
        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "storage": "available",
                "retention_days": active_limits.retention_days,
            },
        )

    @app.post("/v1/events")
    async def ingest_events(request: Request) -> JSONResponse:
        raw_body = await request.body()
        if len(raw_body) > min(MAX_BATCH_BYTES, active_limits.max_batch_bytes):
            return _error(413, "event batch exceeds size limit")
        try:
            body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(400, "request body must be valid JSON")
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            return _error(400, "request must contain an events array")
        events_data = body["events"]
        if not events_data or len(events_data) > active_limits.max_batch_events:
            return _error(413, "event batch count exceeds limit")

        try:
            events = [_event_from_dict(item) for item in events_data]
            for event in events:
                _validate_event_timestamp_window(event)
            accepted, duplicates = active_storage.persist_events(events)
        except EventValidationError:
            return _error(400, "one or more events failed schema validation")
        except Exception:
            logger.error("event_persistence_failed")
            return _error(503, "logging storage unavailable")

        try:
            active_storage.purge_expired()
        except Exception:
            # Persistence has already committed. Retention cleanup is
            # operationally important but must not turn a durable write into a
            # retryable response. Do not expose exception text or payloads.
            logger.error("event_retention_cleanup_failed")

        return JSONResponse(
            status_code=200,
            content={
                "accepted": accepted,
                "duplicates": duplicates,
                "total": len(events),
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)
