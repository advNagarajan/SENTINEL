"""Opt-in live integration check for isolated logging and Navigator services.

The check uses a unique Docker Compose project, dynamically assigned loopback
ports, and project-scoped volumes. It never starts the mainframe or FreeDOS
containers and removes only its own project and volumes when finished.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.observability import (  # noqa: E402
    EventOutcome,
    EventType,
    ObservabilityEvent,
    Severity,
    SourceLayer,
)
from services.observability_producer import EventProducer, ProducerConfig  # noqa: E402


def compose(
    project: str,
    override: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(ROOT / "docker-compose.yml"),
            "--file",
            str(override),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"docker compose {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result


def host_port(project: str, override: Path, service: str, container_port: int) -> int:
    result = compose(project, override, "port", service, str(container_port))
    address = result.stdout.strip().splitlines()[-1]
    return int(address.rsplit(":", 1)[1])


async def wait_healthy(url: str, description: str) -> None:
    deadline = time.monotonic() + 90
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise AssertionError(f"{description} did not become healthy within 90 seconds")


def make_event(event_id: str, sequence: int) -> ObservabilityEvent:
    return ObservabilityEvent(
        event_id=event_id,
        event_type=EventType.OBSERVATION_COMPLETED,
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        severity=Severity.INFO,
        source_layer=SourceLayer.SERVICE,
        source_component="isolated.integration",
        outcome=EventOutcome.SUCCESS,
        correlation_id=f"isolated-correlation-{sequence}",
        metadata={"tool_name": "isolated_test", "batch_size": sequence},
    )


async def deliver(endpoint: str, events: list[ObservabilityEvent]) -> Any:
    producer = EventProducer(
        ProducerConfig(
            endpoint=endpoint,
            max_batch_events=10,
            flush_interval_seconds=0.05,
            connect_timeout_seconds=0.1,
            write_timeout_seconds=0.25,
            read_timeout_seconds=0.5,
            pool_timeout_seconds=0.1,
            max_retries=1,
            retry_delay_seconds=0.05,
            shutdown_timeout_seconds=0.5,
        )
    )
    await producer.start()
    for item in events:
        assert producer.submit(item), "producer rejected a test event"
    stats = await producer.flush(timeout_seconds=5)
    await producer.close(timeout_seconds=1)
    return stats


def stored_events(
    project: str,
    override: Path,
    event_ids: list[str],
) -> dict[str, dict[str, Any]]:
    query = (
        "import json,sqlite3,sys; "
        "db=sqlite3.connect('/var/lib/sentinel-logging/observability.sqlite3'); "
        "ids=json.loads(sys.argv[1]); "
        "marks=','.join('?' for _ in ids); "
        "rows=db.execute('SELECT event_id,payload_json FROM events WHERE event_id IN ('"
        "+marks+')', ids); "
        "print(json.dumps({key:json.loads(value) for key,value in rows}))"
    )
    result = compose(
        project,
        override,
        "exec",
        "-T",
        "logging-service",
        "python",
        "-c",
        query,
        json.dumps(event_ids),
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def logging_event_count(project: str, override: Path) -> int:
    query = (
        "import sqlite3; "
        "db=sqlite3.connect('/var/lib/sentinel-logging/observability.sqlite3'); "
        "print(db.execute('SELECT COUNT(*) FROM events').fetchone()[0])"
    )
    result = compose(
        project,
        override,
        "exec",
        "-T",
        "logging-service",
        "python",
        "-c",
        query,
    )
    return int(result.stdout.strip().splitlines()[-1])


def transition_payload(
    run_id: str,
    src_hash: str,
    src_structural_hash: str,
    tgt_hash: str,
    tgt_structural_hash: str,
    action_signature: str,
    *,
    src_title: str,
    tgt_title: str,
    value: str,
) -> dict[str, Any]:
    return {
        "src_screen_hash": src_hash,
        "src_structural_hash": src_structural_hash,
        "src_title": src_title,
        "src_field_ids": [],
        "tgt_screen_hash": tgt_hash,
        "tgt_structural_hash": tgt_structural_hash,
        "tgt_title": tgt_title,
        "tgt_field_ids": [],
        "tool_name": "isolated_test",
        "field_id": None,
        "action_id": "ENTER",
        "value": value,
        "action_signature": action_signature,
        "runtime_id": f"isolated-{run_id}",
        "target_name": "isolated_test_target",
        "latency_ms": 10.0,
    }


async def run_checks(
    project: str,
    override: Path,
    logging_endpoint: str,
    navigator_endpoint: str,
) -> dict[str, Any]:
    await wait_healthy(f"{logging_endpoint}/health", "logging service")
    await wait_healthy(f"{navigator_endpoint}/health", "Navigator")
    assert logging_event_count(project, override) == 0, (
        "isolated logging volume was not empty at test start"
    )

    run_id = uuid.uuid4().hex[:12]
    event_ids = [
        f"isolated-{run_id}-batch-{index}" for index in range(1, 3)
    ]
    duplicate_id = f"isolated-{run_id}-duplicate"
    producer_ids = [f"isolated-{run_id}-producer-{index}" for index in range(1, 3)]
    outage_id = f"isolated-{run_id}-outage"
    recovery_id = f"isolated-{run_id}-recovery"

    async with httpx.AsyncClient(timeout=5) as client:
        batch = await client.post(
            f"{logging_endpoint}/v1/events",
            json={
                "events": [
                    make_event(event_id, index).to_dict()
                    for index, event_id in enumerate(event_ids, 1)
                ]
            },
        )
        assert batch.status_code == 200, batch.text
        assert batch.json()["accepted"] == 2
        assert batch.json()["duplicates"] == 0

        duplicate = make_event(duplicate_id, 3).to_dict()
        first = await client.post(
            f"{logging_endpoint}/v1/events", json={"events": [duplicate]}
        )
        second = await client.post(
            f"{logging_endpoint}/v1/events", json={"events": [duplicate]}
        )
        assert first.status_code == 200 and first.json()["accepted"] == 1
        assert second.status_code == 200 and second.json()["duplicates"] == 1

    producer_stats = await deliver(
        f"{logging_endpoint}/v1/events",
        [make_event(event_id, index + 3) for index, event_id in enumerate(producer_ids)],
    )
    assert producer_stats.delivered_events == len(producer_ids)

    persisted = stored_events(
        project,
        override,
        event_ids + [duplicate_id] + producer_ids,
    )
    assert set(persisted) == set(event_ids + [duplicate_id] + producer_ids)
    serialized = json.dumps(persisted, sort_keys=True).lower()
    for sensitive_marker in ("password", "secret", "credential", "typed_value", "screen_text"):
        assert sensitive_marker not in serialized

    await asyncio.to_thread(compose, project, override, "stop", "logging-service")
    outage_started = time.monotonic()
    outage_stats = await deliver(
        f"{logging_endpoint}/v1/events",
        [make_event(outage_id, 6)],
    )
    outage_elapsed = time.monotonic() - outage_started
    assert outage_elapsed < 5
    assert outage_stats.failed_events == 1
    assert outage_stats.retry_count <= 1

    await asyncio.to_thread(compose, project, override, "start", "logging-service")
    await wait_healthy(f"{logging_endpoint}/health", "restarted logging service")
    retained = stored_events(
        project,
        override,
        event_ids + [duplicate_id] + producer_ids,
    )
    assert set(retained) == set(event_ids + [duplicate_id] + producer_ids)
    recovery_stats = await deliver(
        f"{logging_endpoint}/v1/events",
        [make_event(recovery_id, 7)],
    )
    assert recovery_stats.delivered_events == 1
    assert recovery_id in stored_events(project, override, [recovery_id])
    assert logging_event_count(project, override) == 6

    source = hashlib.sha256(f"{run_id}:source".encode()).hexdigest()
    source_structure = hashlib.sha256(f"{run_id}:source-structure".encode()).hexdigest()
    target_a = hashlib.sha256(f"{run_id}:target-a".encode()).hexdigest()
    target_a_structure = hashlib.sha256(f"{run_id}:target-a-structure".encode()).hexdigest()
    target_b = hashlib.sha256(f"{run_id}:target-b".encode()).hexdigest()
    target_b_structure = hashlib.sha256(f"{run_id}:target-b-structure".encode()).hexdigest()
    action_a = f"isolated-{run_id}-action-a"
    action_b = f"isolated-{run_id}-action-b"

    async with httpx.AsyncClient(timeout=10) as client:
        graph_before = await client.post(
            f"{navigator_endpoint}/graph/query",
            json={"query": "MATCH (s:Screen) RETURN count(s) AS screens"},
        )
        assert graph_before.status_code == 200, graph_before.text
        assert graph_before.json()["rows"][0]["screens"] == 0, (
            "isolated graph volume was not empty at test start"
        )

        first_transition = transition_payload(
            run_id, source, source_structure, target_a, target_a_structure, action_a,
            src_title="Synthetic test source",
            tgt_title="Synthetic test target A",
            value="SENTINEL_SYNTHETIC_VALUE_A",
        )
        first = await client.post(f"{navigator_endpoint}/transitions", json=first_transition)
        assert first.status_code == 201, first.text
        assert first.json()["traversal_count"] == 1

        repeated_transition = {
            **first_transition,
            "value": "SENTINEL_SYNTHETIC_VALUE_B",
        }
        repeated = await client.post(
            f"{navigator_endpoint}/transitions", json=repeated_transition
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["traversal_count"] == 2

        branch = transition_payload(
            run_id, source, source_structure, target_b, target_b_structure, action_b,
            src_title="Synthetic test source",
            tgt_title="Synthetic test target B",
            value="SENTINEL_SYNTHETIC_VALUE_C",
        )
        branched = await client.post(f"{navigator_endpoint}/transitions", json=branch)
        assert branched.status_code == 201, branched.text
        assert branched.json()["traversal_count"] == 1

        predictions = await client.get(f"{navigator_endpoint}/predict/{source}")
        assert predictions.status_code == 200, predictions.text
        prediction_body = predictions.json()
        assert prediction_body["total_outgoing"] == 3
        by_action = {item["action_signature"]: item for item in prediction_body["transitions"]}
        assert by_action[action_a]["traversal_count"] == 2
        assert math.isclose(by_action[action_a]["probability"], 2 / 3, abs_tol=0.0001)
        assert math.isclose(by_action[action_b]["probability"], 1 / 3, abs_tol=0.0001)
        assert set(by_action[action_a]["values_seen"]) == {
            "SENTINEL_SYNTHETIC_VALUE_A",
            "SENTINEL_SYNTHETIC_VALUE_B",
        }

        neighbors = await client.get(
            f"{navigator_endpoint}/graph/neighbors/{source}", params={"depth": 2}
        )
        assert neighbors.status_code == 200, neighbors.text
        assert len(neighbors.json()["edges"]) == 2

        path = await client.get(
            f"{navigator_endpoint}/graph/paths",
            params={"from_hash": source, "to_hash": target_a},
        )
        assert path.status_code == 200, path.text
        assert path.json()["found"] is True
        assert path.json()["path_length"] == 1

        missing_path = await client.get(
            f"{navigator_endpoint}/graph/paths",
            params={
                "from_hash": target_a,
                "to_hash": hashlib.sha256(f"{run_id}:missing".encode()).hexdigest(),
            },
        )
        assert missing_path.status_code == 200, missing_path.text
        assert missing_path.json()["found"] is False

        search = await client.get(
            f"{navigator_endpoint}/graph/screens",
            params={"query": "Synthetic test target A"},
        )
        assert search.status_code == 200, search.text
        assert search.json()["total_found"] == 1

        stats = await client.get(f"{navigator_endpoint}/graph/stats")
        assert stats.status_code == 200, stats.text
        assert stats.json()["total_screens"] == 3
        assert stats.json()["total_transitions"] == 2
        assert stats.json()["total_traversals"] == 3

        rejected_query = await client.post(
            f"{navigator_endpoint}/graph/query",
            json={"query": "MATCH (n) DETACH DELETE n"},
        )
        assert rejected_query.status_code == 400

    return {
        "logging_batch_accepted": batch.json()["accepted"],
        "logging_duplicate_count": second.json()["duplicates"],
        "producer_delivered": producer_stats.delivered_events,
        "producer_outage_failures": outage_stats.failed_events,
        "producer_outage_retries": outage_stats.retry_count,
        "producer_recovery_delivered": recovery_stats.delivered_events,
        "navigator_screens": stats.json()["total_screens"],
        "navigator_transitions": stats.json()["total_transitions"],
        "navigator_traversals": stats.json()["total_traversals"],
        "isolated_project": project,
    }


def main() -> int:
    if os.environ.get("SENTINEL_RUN_ISOLATED_INTEGRATION") != "1":
        print(
            "Set SENTINEL_RUN_ISOLATED_INTEGRATION=1 to run the isolated Docker integration check."
        )
        return 2

    project = f"sentinel-it-{uuid.uuid4().hex[:12]}"
    print(f"Starting isolated Docker project {project}")
    with tempfile.TemporaryDirectory(prefix=f"{project}-") as temp_dir:
        override = Path(temp_dir) / "ports.override.yml"
        override.write_text(
            "services:\n"
            "  neo4j:\n"
            "    ports:\n"
            '      - "127.0.0.1::7474"\n'
            '      - "127.0.0.1::7687"\n'
            "  navigator:\n"
            "    ports:\n"
            '      - "127.0.0.1::8100"\n'
            "  logging-service:\n"
            "    ports:\n"
            '      - "127.0.0.1::8200"\n',
            encoding="utf-8",
        )

        try:
            compose(
                project,
                override,
                "up",
                "-d",
                "--build",
                "neo4j",
                "navigator",
                "logging-service",
            )
            logging_port = host_port(project, override, "logging-service", 8200)
            navigator_port = host_port(project, override, "navigator", 8100)
            result = asyncio.run(
                run_checks(
                    project,
                    override,
                    f"http://127.0.0.1:{logging_port}",
                    f"http://127.0.0.1:{navigator_port}",
                )
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except BaseException:
            diagnostics = compose(
                project,
                override,
                "logs",
                "--no-color",
                "--tail",
                "100",
                check=False,
            )
            print(diagnostics.stdout, file=sys.stderr)
            print(diagnostics.stderr, file=sys.stderr)
            raise
        finally:
            cleanup = compose(
                project,
                override,
                "down",
                "--volumes",
                "--remove-orphans",
                check=False,
            )
            if cleanup.returncode:
                raise RuntimeError(
                    f"failed to remove isolated Docker project {project}:\n{cleanup.stderr}"
                )


if __name__ == "__main__":
    sys.exit(main())
