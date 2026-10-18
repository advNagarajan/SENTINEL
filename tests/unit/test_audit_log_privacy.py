"""Tests for redaction in the separate JSONL and structlog paths."""

import json
from types import SimpleNamespace

import structlog

from layer4.audit_log import AuditLogger


def test_audit_and_warning_logs_redact_sensitive_values(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(str(log_path))

    audit.log_event(
        SimpleNamespace(
            event_type=SimpleNamespace(value="input_typed"),
            runtime_id="runtime-test",
            message="typed_value=super-secret",
            timestamp=1.0,
            details={
                "typed_value": "super-secret",
                "screen_text": ["ACCOUNT PASSWORD"],
                "safe_detail": "preserved",
            },
        )
    )
    audit.log_action_completion(
        runtime_id="runtime-test",
        tool_name="trigger_action",
        action_started_at=1.0,
        action_completed_at=2.0,
        success=False,
        error="password=super-secret",
        message="screen_text=ACCOUNT PASSWORD",
    )
    structlog.get_logger("privacy-test").warning(
        "synthetic_warning",
        error="authorization: Bearer super-secret",
        safe_detail="preserved",
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    serialized = json.dumps(records, sort_keys=True)
    assert "super-secret" not in serialized
    assert "ACCOUNT PASSWORD" not in serialized
    assert "Bearer super-secret" not in serialized
    assert records[0]["details"]["typed_value"] == "[REDACTED]"
    assert records[0]["details"]["safe_detail"] == "preserved"
    assert records[2]["safe_detail"] == "preserved"


def test_audit_redaction_preserves_safe_diagnostic_text(tmp_path):
    log_path = tmp_path / "audit-safe.jsonl"
    audit = AuditLogger(str(log_path))

    audit.log_action_completion(
        runtime_id="runtime-test",
        tool_name="trigger_action",
        action_started_at=1.0,
        action_completed_at=2.0,
        success=False,
        error="RuntimeError",
        message="stale generation",
    )

    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["error"] == "RuntimeError"
    assert record["message"] == "stale generation"
