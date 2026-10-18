"""Controlled Stage 7D integration check for the isolated logging service."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Allow direct execution from the repository root or from the scripts directory.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.observability import (
    EventOutcome,
    EventType,
    ObservabilityEvent,
    Severity,
    SourceLayer,
)
from services.observability_producer import EventProducer, ProducerConfig


SERVICE = "logging-service"
ENDPOINT = os.environ.get(
    "SENTINEL_OBSERVABILITY_ENDPOINT",
    "http://127.0.0.1:8200/v1/events",
)
HEALTH_ENDPOINT = ENDPOINT.rsplit("/v1/events", 1)[0] + "/health"
DATABASE = "/var/lib/sentinel-logging/observability.sqlite3"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def service_running() -> bool:
    result = docker("ps", "--status", "running", "-q", SERVICE, check=False)
    return bool(result.stdout.strip())


def start_service() -> None:
    docker("up", "-d", SERVICE)


def stop_service() -> None:
    docker("stop", SERVICE, check=False)


def database_snapshot() -> dict[str, Any]:
    query = (
        "import json,sqlite3; "
        f"db=sqlite3.connect({DATABASE!r}); "
        "count=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]; "
        "oldest=db.execute('SELECT MIN(timestamp_utc) FROM events').fetchone()[0]; "
        "print(json.dumps({'count': count, 'oldest': oldest}))"
    )
    result = docker("exec", "-T", SERVICE, "python", "-c", query)
    return json.loads(result.stdout.strip().splitlines()[-1])


def records_for(event_ids: list[str]) -> dict[str, dict[str, Any]]:
    query = (
        "import json,sqlite3,sys; "
        f"db=sqlite3.connect({DATABASE!r}); "
        "ids=json.loads(sys.argv[1]); "
        "marks=','.join('?' for _ in ids); "
        "rows=db.execute('SELECT event_id,payload_json FROM events WHERE event_id IN ('+marks+')', ids); "
        "print(json.dumps(dict(rows)))"
    )
    result = docker("exec", "-T", SERVICE, "python", "-c", query, json.dumps(event_ids))
    raw = json.loads(result.stdout.strip().splitlines()[-1])
    return {event_id: json.loads(payload) for event_id, payload in raw.items()}


def event(event_id: str, sequence: int) -> ObservabilityEvent:
    return ObservabilityEvent(
        event_id=event_id,
        event_type=EventType.OBSERVATION_COMPLETED,
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        severity=Severity.INFO,
        source_layer=SourceLayer.SERVICE,
        source_component="stage7d.integration",
        outcome=EventOutcome.SUCCESS,
        correlation_id=f"stage7d-correlation-{sequence}",
        metadata={"tool_name": "stage7d_test", "batch_size": sequence},
    )


async def wait_for_health() -> None:
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(HEALTH_ENDPOINT)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise AssertionError("logging service did not become healthy within 30 seconds")


def producer_config() -> ProducerConfig:
    return ProducerConfig(
        endpoint=ENDPOINT,
        max_batch_events=10,
        flush_interval_seconds=0.05,
        connect_timeout_seconds=0.1,
        write_timeout_seconds=0.25,
        read_timeout_seconds=0.5,
        pool_timeout_seconds=0.1,
        max_retries=1,
        retry_delay_seconds=0.05,
        shutdown_timeout_seconds=0.25,
    )


async def deliver_with_producer(events: list[ObservabilityEvent]) -> Any:
    producer = EventProducer(producer_config())
    await producer.start()
    for item in events:
        assert producer.submit(item)
    stats = await producer.flush(timeout_seconds=3)
    await producer.close(timeout_seconds=0.5)
    return stats


async def run_checks() -> dict[str, Any]:
    await wait_for_health()

    initial = database_snapshot()
    if initial["count"]:
        raise RuntimeError(
            "refusing to write to the non-empty named volume; "
            "this guard protects existing records from retention side effects"
        )

    run_id = uuid.uuid4().hex
    batch_ids = [f"stage7d-{run_id}-batch-{index}" for index in range(1, 4)]
    duplicate_id = f"stage7d-{run_id}-duplicate"
    recovery_id = f"stage7d-{run_id}-recovery"
    outage_id = f"stage7d-{run_id}-outage"

    batch_events = [event(event_id, index) for index, event_id in enumerate(batch_ids, 1)]
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.post(
            ENDPOINT,
            json={"events": [item.to_dict() for item in batch_events]},
        )
    assert response.status_code == 200, response.text
    batch_result = response.json()
    assert batch_result["accepted"] == len(batch_ids)
    assert batch_result["duplicates"] == 0

    duplicate_event = event(duplicate_id, 4)
    async with httpx.AsyncClient(timeout=3) as client:
        first = await client.post(
            ENDPOINT, json={"events": [duplicate_event.to_dict()]}
        )
        second = await client.post(
            ENDPOINT, json={"events": [duplicate_event.to_dict()]}
        )
    assert first.status_code == 200 and first.json()["accepted"] == 1
    assert second.status_code == 200 and second.json()["duplicates"] == 1

    producer_ids = [f"stage7d-{run_id}-producer-{index}" for index in range(1, 4)]
    producer_events = [
        event(event_id, index + 4) for index, event_id in enumerate(producer_ids)
    ]
    producer_stats = await deliver_with_producer(producer_events)
    assert producer_stats.delivered_events == len(producer_ids)

    stored = records_for(batch_ids + [duplicate_id] + producer_ids)
    assert set(stored) == set(batch_ids + [duplicate_id] + producer_ids)
    serialized = json.dumps(stored, sort_keys=True).lower()
    for forbidden in ("password", "secret", "credential", "typed_value", "screen_text"):
        assert forbidden not in serialized

    stop_service()
    outage_started = time.monotonic()
    outage_stats = await deliver_with_producer([event(outage_id, 9)])
    outage_elapsed = time.monotonic() - outage_started
    assert outage_elapsed < 2
    assert outage_stats.retry_count <= 1
    assert outage_stats.failed_events >= 1

    start_service()
    await wait_for_health()
    recovery_stats = await deliver_with_producer([event(recovery_id, 10)])
    assert recovery_stats.delivered_events == 1
    assert set(records_for([recovery_id])) == {recovery_id}

    return {
        "batch_accepted": batch_result,
        "duplicate_second_submission": second.json(),
        "producer_delivered": producer_stats.delivered_events,
        "outage_elapsed_seconds": round(outage_elapsed, 3),
        "outage_failed_events": outage_stats.failed_events,
        "outage_retries": outage_stats.retry_count,
        "recovery_delivered": recovery_stats.delivered_events,
        "sensitive_strings_checked": [
            "password",
            "secret",
            "credential",
            "typed_value",
            "screen_text",
        ],
        "final_database": database_snapshot(),
    }


def main() -> int:
    if os.environ.get("SENTINEL_RUN_LOGGING_INTEGRATION") != "1":
        print("Set SENTINEL_RUN_LOGGING_INTEGRATION=1 to run the Docker integration check.")
        return 2

    was_running = service_running()
    try:
        start_service()
        summary = asyncio.run(run_checks())
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        if was_running:
            start_service()
        else:
            stop_service()


if __name__ == "__main__":
    sys.exit(main())
