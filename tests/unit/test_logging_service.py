"""Offline tests for the isolated SENTINEL logging service."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from schemas.observability import EventOutcome, EventType, Severity, SourceLayer
from services.logging_service import main as logging_main
from services.logging_service.main import create_app
from services.logging_service.models import (
    MAX_BATCH_BYTES,
    ServiceLimits,
    parse_retention_days,
    parse_retention_interval_seconds,
)
from services.logging_service.storage import SQLiteEventStorage


FIXED_TIMESTAMP = "2026-10-18T12:00:00Z"
FIXED_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def event_payload(event_id: str = "evt-1", timestamp: str = FIXED_TIMESTAMP) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": EventType.ACTION_COMPLETED.value,
        "timestamp_utc": timestamp,
        "severity": Severity.INFO.value,
        "source_layer": SourceLayer.LAYER4.value,
        "source_component": "test-service",
        "outcome": EventOutcome.SUCCESS.value,
        "correlation_id": "corr-1",
        "metadata": {"tool_name": "trigger_action"},
    }


@pytest.fixture
def service(tmp_path):
    storage = SQLiteEventStorage(
        str(tmp_path / "events.sqlite3"),
        now_factory=lambda: FIXED_NOW,
    )
    app = create_app(storage=storage)
    with TestClient(app) as client:
        yield client, storage


def test_health_reports_storage_and_retention(service):
    client, _storage = service

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "storage": "available",
        "retention_days": 7,
    }


def test_health_runs_read_only_integrity_and_schema_checks(service, monkeypatch):
    _client, storage = service
    statements = []

    class Cursor:
        def __init__(self, statement):
            self.statement = statement

        def fetchone(self):
            if self.statement.startswith("PRAGMA quick_check"):
                return ("ok",)
            return (1,)

    class Connection:
        def execute(self, statement):
            statements.append(statement)
            return Cursor(statement)

    monkeypatch.setattr(storage, "_require_connection", lambda: Connection())

    assert storage.is_healthy() is True
    assert statements == [
        "PRAGMA quick_check(1)",
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'",
    ]


def test_health_rejects_failed_read_only_integrity_check(service, monkeypatch):
    _client, storage = service

    class Cursor:
        def fetchone(self):
            return ("not an ok result",)

    class Connection:
        def execute(self, _statement):
            return Cursor()

    monkeypatch.setattr(storage, "_require_connection", lambda: Connection())

    assert storage.is_healthy() is False


@pytest.mark.parametrize(
    "raw_value,expected",
    [(None, 7), ("0", 0), ("3650", 3650)],
)
def test_retention_configuration_parser_accepts_valid_values_without_upper_bound(raw_value, expected):
    assert parse_retention_days(raw_value) == expected


@pytest.mark.parametrize("raw_value", ["", "   ", "seven", "-1"])
def test_retention_configuration_parser_rejects_invalid_values(raw_value):
    with pytest.raises(ValueError, match="SENTINEL_LOG_RETENTION_DAYS"):
        parse_retention_days(raw_value)


def test_invalid_retention_environment_fails_startup_validation(monkeypatch):
    monkeypatch.setenv("SENTINEL_LOG_RETENTION_DAYS", "not-an-integer")

    with pytest.raises(ValueError, match="SENTINEL_LOG_RETENTION_DAYS"):
        create_app()


@pytest.mark.parametrize(
    "raw_value,expected",
    [(None, 3600.0), ("1", 1.0), ("2.5", 2.5)],
)
def test_retention_interval_parser_accepts_valid_values(raw_value, expected):
    assert parse_retention_interval_seconds(raw_value) == expected


@pytest.mark.parametrize("raw_value", ["", " ", "zero", "0", "0.5", "-1", "nan", "inf"])
def test_retention_interval_parser_rejects_invalid_or_busy_loop_values(raw_value):
    with pytest.raises(ValueError, match="SENTINEL_LOG_RETENTION_INTERVAL_SECONDS"):
        parse_retention_interval_seconds(raw_value)


def test_invalid_retention_interval_environment_fails_startup_validation(monkeypatch):
    monkeypatch.setenv("SENTINEL_LOG_RETENTION_INTERVAL_SECONDS", "0")

    with pytest.raises(ValueError, match="SENTINEL_LOG_RETENTION_INTERVAL_SECONDS"):
        create_app()


def test_service_limits_default_retention_interval_is_one_hour():
    assert ServiceLimits().retention_interval_seconds == 3600.0


@pytest.mark.asyncio
async def test_periodic_cleanup_runs_without_ingestion_and_does_not_purge_on_startup(
    tmp_path, monkeypatch
):
    storage = SQLiteEventStorage(
        str(tmp_path / "events.sqlite3"),
        now_factory=lambda: FIXED_NOW,
    )
    storage.open()
    from services.logging_service.main import _event_from_dict

    storage.persist_events([_event_from_dict(event_payload(timestamp="2020-01-01T00:00:00Z"))])
    storage.close()
    app = create_app(storage=storage, limits=ServiceLimits(retention_interval_seconds=1.0))
    original_sleep = asyncio.sleep
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    async def controlled_sleep(delay):
        assert delay == 1.0
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr(logging_main.asyncio, "sleep", controlled_sleep)
    async with app.router.lifespan_context(app):
        assert storage.count() == 1
        await original_sleep(0)
        await sleep_started.wait()
        assert storage.count() == 1
        release_sleep.set()
        for _ in range(20):
            await original_sleep(0)
            if storage.count() == 0:
                break
        assert storage.count() == 0


@pytest.mark.asyncio
async def test_periodic_cleanup_failure_retries_and_logs_sanitized_diagnostic(
    tmp_path, monkeypatch, caplog
):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    app = create_app(storage=storage, limits=ServiceLimits(retention_interval_seconds=1.0))
    original_sleep = asyncio.sleep
    sleep_count = 0
    release = asyncio.Event()
    calls = 0

    def fail_cleanup():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise sqlite3.OperationalError("secret sqlite payload")

    monkeypatch.setattr(storage, "purge_expired", fail_cleanup)

    async def controlled_sleep(delay):
        nonlocal sleep_count
        assert delay == 1.0
        sleep_count += 1
        if sleep_count >= 3:
            release.set()
        await original_sleep(0)

    monkeypatch.setattr(logging_main.asyncio, "sleep", controlled_sleep)
    with caplog.at_level("ERROR", logger="sentinel.logging_service"):
        async with app.router.lifespan_context(app):
            await release.wait()
            for _ in range(20):
                await original_sleep(0)
                if calls >= 3:
                    break

    assert calls >= 3
    assert "event_retention_cleanup_failed" in caplog.text
    assert "secret sqlite payload" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_cancels_cleanup_before_closing_storage(tmp_path, monkeypatch):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    app = create_app(storage=storage, limits=ServiceLimits(retention_interval_seconds=1.0))
    original_sleep = asyncio.sleep
    started = asyncio.Event()

    async def controlled_sleep(_delay):
        started.set()
        await original_sleep(0)
        await asyncio.Future()

    monkeypatch.setattr(logging_main.asyncio, "sleep", controlled_sleep)
    async with app.router.lifespan_context(app):
        await started.wait()

    assert storage._connection is None


@pytest.mark.asyncio
async def test_cleanup_uses_same_storage_lock_as_ingestion(tmp_path):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    storage.open()
    try:
        storage._lock.acquire()
        cleanup = asyncio.create_task(asyncio.to_thread(storage.purge_expired))
        await asyncio.sleep(0)
        assert cleanup.done() is False
        storage._lock.release()
        await cleanup
    finally:
        if storage._lock.locked():
            storage._lock.release()
        storage.close()


@pytest.mark.asyncio
async def test_stuck_cleanup_defers_storage_close_until_executor_finishes(tmp_path, monkeypatch):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    app = create_app(storage=storage, limits=ServiceLimits(retention_interval_seconds=1.0))
    original_sleep = asyncio.sleep
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    first_sleep = True

    def stuck_cleanup():
        cleanup_started.set()
        release_cleanup.wait(timeout=5)

    async def controlled_sleep(_delay):
        nonlocal first_sleep
        if first_sleep:
            first_sleep = False
            return
        await asyncio.Future()

    monkeypatch.setattr(storage, "purge_expired", stuck_cleanup)
    monkeypatch.setattr(logging_main.asyncio, "sleep", controlled_sleep)

    async with app.router.lifespan_context(app):
        await asyncio.to_thread(cleanup_started.wait)

    assert storage._connection is not None
    release_cleanup.set()
    await original_sleep(0.05)
    assert storage._connection is None


def test_successful_ingestion_persists_sanitized_event(service):
    client, storage = service

    response = client.post("/v1/events", json={"events": [event_payload()]})

    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0, "total": 1}
    assert storage.count() == 1
    assert storage.get_payload("evt-1")["event_id"] == "evt-1"


def test_future_dated_event_beyond_clock_skew_is_rejected(service):
    client, storage = service
    future = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat().replace(
        "+00:00", "Z"
    )

    response = client.post("/v1/events", json={"events": [event_payload(timestamp=future)]})

    assert response.status_code == 400
    assert response.json() == {"detail": "one or more events failed schema validation"}
    assert storage.count() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"events": "not-a-list"},
        {"events": [{}]},
        {"events": [{**event_payload(), "metadata": {"raw_screen": "synthetic"}}]},
        {"events": [{**event_payload(), "metadata": {"tool_name": {"nested": True}}}]},
    ],
)
def test_malformed_or_invalid_events_are_rejected_without_payload_echo(service, body):
    client, _storage = service

    response = client.post("/v1/events", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "one or more events failed schema validation"} or response.json() == {
        "detail": "request must contain an events array"
    }
    assert "synthetic" not in response.text


def test_invalid_json_is_rejected(service):
    client, _storage = service

    response = client.post("/v1/events", content=b"{not-json")

    assert response.status_code == 400
    assert response.json() == {"detail": "request body must be valid JSON"}


def test_oversized_serialized_request_body_is_rejected_before_persistence(service):
    client, storage = service
    body = b"{" + (b"x" * MAX_BATCH_BYTES)

    response = client.post("/v1/events", content=body)

    assert response.status_code == 413
    assert response.json() == {"detail": "event batch exceeds size limit"}
    assert storage.count() == 0


def test_oversized_body_without_content_length_is_rejected_before_app_processing(service):
    _client, storage = service
    app = create_app(storage=storage)
    chunks = [b"{" + (b"x" * MAX_BATCH_BYTES), b"x"]

    status, response_body = asyncio.run(_call_asgi_without_content_length(app, chunks))

    assert status == 413
    assert response_body == b'{"detail":"event batch exceeds size limit"}'
    assert storage.count() == 0


def test_valid_multi_chunk_request_is_forwarded_once_and_persisted_once(service):
    _client, storage = service
    app = create_app(storage=storage)
    body = json.dumps({"events": [event_payload()]}, separators=(",", ":")).encode("utf-8")
    split_points = (len(body) // 3, 2 * len(body) // 3)
    chunks = [body[: split_points[0]], body[split_points[0] : split_points[1]], body[split_points[1] :]]

    status, response_body = asyncio.run(_call_asgi_without_content_length(app, chunks))

    assert status == 200
    assert json.loads(response_body) == {"accepted": 1, "duplicates": 0, "total": 1}
    assert storage.count() == 1
    assert storage.get_payload("evt-1")["event_id"] == "evt-1"


def test_client_disconnect_before_body_completion_emits_no_response_or_persistence(service):
    _client, storage = service
    app = create_app(storage=storage)
    messages = [
        {"type": "http.request", "body": b'{"events":[', "more_body": True},
        {"type": "http.disconnect"},
    ]

    sent = asyncio.run(_call_asgi_messages(app, messages))

    assert sent == []
    assert storage.count() == 0


def test_oversized_batch_is_rejected(service):
    client, _storage = service
    body = {"events": [event_payload(f"evt-{index}") for index in range(101)]}

    response = client.post("/v1/events", json=body)

    assert response.status_code == 413
    assert response.json() == {"detail": "event batch count exceeds limit"}


def test_duplicate_event_ids_are_idempotently_ignored(service):
    client, storage = service
    body = {"events": [event_payload()]}

    first = client.post("/v1/events", json=body)
    second = client.post("/v1/events", json=body)

    assert first.json() == {"accepted": 1, "duplicates": 0, "total": 1}
    assert second.json() == {"accepted": 0, "duplicates": 1, "total": 1}
    assert storage.count() == 1


def test_concurrent_distinct_events_are_each_persisted_once(tmp_path):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    storage.open()
    app = create_app(storage=storage)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(
                executor.map(
                    lambda index: _post_without_lifespan(app, {"events": [event_payload(f"evt-{index}")]}),
                    range(20),
                )
            )
        assert all(response.status_code == 200 for response in responses)
        assert storage.count() == 20
    finally:
        storage.close()


def test_concurrent_duplicate_events_are_deterministically_deduplicated(tmp_path):
    storage = SQLiteEventStorage(str(tmp_path / "events.sqlite3"))
    storage.open()
    app = create_app(storage=storage)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(
                executor.map(
                    lambda _index: _post_without_lifespan(app, {"events": [event_payload()]}),
                    range(20),
                )
            )
        assert all(response.status_code == 200 for response in responses)
        assert storage.count() == 1
        assert sum(response.json()["accepted"] for response in responses) == 1
        assert sum(response.json()["duplicates"] for response in responses) == 19
    finally:
        storage.close()


def test_sqlite_data_survives_storage_reopen(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    first = SQLiteEventStorage(path)
    first.open()
    from services.logging_service.main import _event_from_dict

    first.persist_events([_event_from_dict(event_payload())])
    first.close()

    second = SQLiteEventStorage(path)
    second.open()
    try:
        assert second.count() == 1
        assert second.get_payload("evt-1")["event_type"] == "action.completed"
    finally:
        second.close()


def test_sqlite_online_backup_restore_preserves_records_and_idempotency(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"
    from services.logging_service.main import _event_from_dict

    source = SQLiteEventStorage(str(source_path))
    source.open()
    source_events = [
        _event_from_dict(event_payload(f"backup-{index}")) for index in range(1, 4)
    ]
    try:
        accepted, duplicates = source.persist_events(source_events)
        assert (accepted, duplicates) == (3, 0)
        expected_fields = {
            event.event_id: (event.event_type.value, event.source_component, event.correlation_id)
            for event in source_events
        }
        assert source.count() == 3
        source_snapshot = {
            event_id: source.get_payload(event_id) for event_id in expected_fields
        }
    finally:
        source.close()

    with sqlite3.connect(source_path) as source_connection, sqlite3.connect(
        backup_path
    ) as backup_connection:
        source_connection.backup(backup_connection)

    source_reopened = SQLiteEventStorage(str(source_path))
    source_reopened.open()
    try:
        assert source_reopened.count() == 3
        assert {
            event_id: source_reopened.get_payload(event_id)
            for event_id in expected_fields
        } == source_snapshot
    finally:
        source_reopened.close()

    with sqlite3.connect(backup_path) as backup_connection:
        assert backup_connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    backup_storage = SQLiteEventStorage(str(backup_path))
    backup_storage.open()
    try:
        assert backup_storage.count() == 3
        for event_id, (event_type, source_component, correlation_id) in expected_fields.items():
            payload = backup_storage.get_payload(event_id)
            assert payload is not None
            assert payload["event_type"] == event_type
            assert payload["source_component"] == source_component
            assert payload["correlation_id"] == correlation_id

        duplicate_event = _event_from_dict(event_payload("backup-2"))
        assert backup_storage.persist_events([duplicate_event]) == (0, 1)
        assert backup_storage.count() == 3
    finally:
        backup_storage.close()

    with sqlite3.connect(backup_path) as backup_connection, sqlite3.connect(
        restored_path
    ) as restored_connection:
        backup_connection.backup(restored_connection)

    restored_storage = SQLiteEventStorage(str(restored_path))
    restored_storage.open()
    try:
        assert restored_storage.is_healthy() is True
        assert restored_storage.count() == 3
        assert restored_storage.get_payload("backup-1") == source_snapshot["backup-1"]
    finally:
        restored_storage.close()


def test_sqlite_backup_integrity_check_rejects_incomplete_file(tmp_path):
    invalid_backup = tmp_path / "incomplete.sqlite3"
    invalid_backup.write_bytes(b"not a complete sqlite database")

    with sqlite3.connect(invalid_backup) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("PRAGMA quick_check").fetchone()


def test_startup_does_not_delete_expired_data_until_retention_runs(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    old_timestamp = "2020-01-01T00:00:00Z"
    storage = SQLiteEventStorage(path, retention_days=7)
    storage.open()
    from services.logging_service.main import _event_from_dict

    storage.persist_events([_event_from_dict(event_payload(timestamp=old_timestamp))])
    storage.close()

    reopened = SQLiteEventStorage(path, retention_days=7)
    reopened.open()
    try:
        assert reopened.count() == 1
        assert reopened.purge_expired(datetime(2026, 10, 18, tzinfo=timezone.utc)) == 1
        assert reopened.count() == 0
    finally:
        reopened.close()


def test_retention_is_applied_after_ingestion(service):
    client, storage = service
    old = event_payload(timestamp="2020-01-01T00:00:00Z")
    current = event_payload(event_id="evt-current")

    response = client.post("/v1/events", json={"events": [old, current]})

    assert response.status_code == 200
    assert storage.count() == 1
    assert storage.get_payload("evt-current") is not None


def test_persistence_failure_returns_bounded_error(service, monkeypatch):
    client, storage = service

    def fail(_events):
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(storage, "persist_events", fail)
    response = client.post("/v1/events", json={"events": [event_payload()]})

    assert response.status_code == 503
    assert response.json() == {"detail": "logging storage unavailable"}
    assert "synthetic database failure" not in response.text
    assert storage.count() == 0


def test_cleanup_failure_returns_success_after_persistence_and_hides_diagnostics(service, monkeypatch, caplog):
    client, storage = service

    def fail_cleanup():
        raise sqlite3.OperationalError("synthetic cleanup database failure")

    monkeypatch.setattr(storage, "purge_expired", fail_cleanup)
    with caplog.at_level("ERROR", logger="sentinel.logging_service"):
        response = client.post("/v1/events", json={"events": [event_payload()]})

    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0, "total": 1}
    assert storage.count() == 1
    assert "synthetic cleanup database failure" not in caplog.text
    assert "event_retention_cleanup_failed" in caplog.text


def test_cleanup_failure_then_duplicate_retry_reports_duplicate_without_new_row(service, monkeypatch):
    client, storage = service
    original_cleanup = storage.purge_expired
    calls = 0

    def fail_once_then_cleanup():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("synthetic cleanup failure")
        return original_cleanup()

    monkeypatch.setattr(storage, "purge_expired", fail_once_then_cleanup)
    body = {"events": [event_payload()]}

    first = client.post("/v1/events", json=body)
    second = client.post("/v1/events", json=body)

    assert first.status_code == 200
    assert first.json() == {"accepted": 1, "duplicates": 0, "total": 1}
    assert second.status_code == 200
    assert second.json() == {"accepted": 0, "duplicates": 1, "total": 1}
    assert storage.count() == 1


def test_unhealthy_storage_returns_bounded_health_failure(service, monkeypatch):
    client, storage = service
    monkeypatch.setattr(storage, "is_healthy", lambda: False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "logging storage unavailable"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_batch_events", True),
        ("max_batch_events", 1.5),
        ("max_batch_events", "100"),
        ("max_batch_bytes", False),
        ("max_batch_bytes", 1024.0),
        ("max_batch_bytes", "1024"),
    ],
)
def test_batch_limits_require_integer_values(field, value):
    with pytest.raises(ValueError, match=field):
        ServiceLimits(**{field: value})


def test_batch_limits_accept_positive_integers():
    limits = ServiceLimits(max_batch_events=10, max_batch_bytes=1024)

    assert limits.max_batch_events == 10
    assert limits.max_batch_bytes == 1024


async def _call_asgi_without_content_length(app, chunks):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent = await _call_asgi_messages(app, messages)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return status, response_body


async def _call_asgi_messages(app, messages):
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/events",
        "raw_path": b"/v1/events",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return sent


def _post_without_lifespan(app, body):
    client = TestClient(app)
    try:
        return client.post("/v1/events", json=body)
    finally:
        client.close()
