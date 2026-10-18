# SENTINEL logging service deployment

The logging service is an isolated development deployment. It is published only
on the host loopback interface and is not an authenticated or TLS-protected
public endpoint.

## Start the logging service only

From the repository root:

```powershell
docker compose up -d --build logging-service
```

Check its storage-backed health endpoint:

```powershell
curl.exe http://127.0.0.1:8200/health
```

The host-launched MCP process can send observability events to this container
with:

```powershell
$env:SENTINEL_OBSERVABILITY_ENDPOINT = "http://127.0.0.1:8200/v1/events"
python scripts/run_sentinel_mcp.py
```

The producer remains best-effort; stopping the logging service does not become
a runtime startup dependency.

Retention cleanup runs in the logging-service lifespan every hour by default,
including when no events are ingested. Configure the interval with
`SENTINEL_LOG_RETENTION_INTERVAL_SECONDS`; it must be a finite number of at
least one second. Cleanup remains best-effort and logs only a fixed operational
diagnostic when it fails. A successful ingestion response confirms persistence,
not successful retention cleanup.

The cleanup loop is owned by the single service process. Running multiple
workers or replicas would create multiple cleanup loops and requires a separate
coordination design review.

## Response and health contract

`POST /v1/events` returns success after the event batch is committed to SQLite.
The response confirms persistence, not successful retention cleanup. If the
post-commit retention pass fails, the service keeps the success response and
writes only a safe operational diagnostic without SQLite exception text or
event payloads. A later ingestion request retries the cleanup pass.

`GET /health` performs read-only SQLite readiness checks: `PRAGMA
quick_check(1)` and verification that the `events` table exists. It does not
prove that a future write will succeed, that disk space is sufficient, or that
retention cleanup can run.

## Persistence and recreation

The SQLite database is stored in the named `sentinel_logging_data` volume at
`/var/lib/sentinel-logging/observability.sqlite3` inside the container. Stop or
recreate the service without `-v` to retain the volume:

```powershell
docker compose stop logging-service
docker compose up -d --build logging-service
```

Do not use `docker compose down -v` when the retained database is required.

## Safe backup and restore

Create backups while the service is running but quiescent. The supported method
is SQLite's online backup API, writing to a separate temporary database before
copying the backup out of the container. This does not alter the named volume:

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
docker compose exec -T logging-service python -c "import sqlite3; src=sqlite3.connect('/var/lib/sentinel-logging/observability.sqlite3'); dst=sqlite3.connect('/tmp/observability-backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker compose cp logging-service:/tmp/observability-backup.sqlite3 .\backups\observability-backup.sqlite3
```

Validate a backup before restore:

```powershell
python -c "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); result=db.execute('PRAGMA quick_check').fetchone()[0]; db.close(); raise SystemExit(0 if result == 'ok' else result)" .\backups\observability-backup.sqlite3
```

Before restoring, use SQLite's online backup API to copy the verified backup
into a separate temporary destination and validate that destination as well.
Restore only during a planned outage: stop `logging-service`, preserve a
recoverable copy of the active database, and replace the active database file
only while the service is stopped. Do not delete or recreate the named Docker
volume, and never use `docker compose down -v` when retaining logs. Start the
service again only after the restored database is in place and verify
`/health` plus a synthetic ingestion check.

The repository contains an isolated temporary-database test covering the
online backup API, `PRAGMA quick_check`, record preservation, duplicate-event
idempotency, and restoration into a separate destination. It does not perform
remote backups, encryption, scheduled backups, or disaster-recovery service
automation.
