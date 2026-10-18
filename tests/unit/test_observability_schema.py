"""Unit tests for the sanitized observability event contract."""
import pytest
import schemas.observability as observability

from schemas.observability import (
    EventOutcome,
    EventType,
    EventValidationError,
    ObservabilityEvent,
    SCHEMA_VERSION,
    Severity,
    SourceLayer,
    is_safe_identifier,
)


FIXED_TIMESTAMP = "2026-10-18T12:00:00.123Z"


def make_event(**overrides) -> ObservabilityEvent:
    values = {
        "event_id": "evt-test-01",
        "event_type": EventType.ACTION_COMPLETED,
        "timestamp_utc": FIXED_TIMESTAMP,
        "severity": Severity.INFO,
        "source_layer": SourceLayer.LAYER4,
        "source_component": "RuntimeOrchestrator",
        "outcome": EventOutcome.SUCCESS,
        "correlation_id": "corr-test-01",
        "runtime_id": "runtime-test",
        "session_id": "session-test",
        "operation_id": "op-test-01",
        "ticket_id": "ticket-test-01",
        "generation_before": 1,
        "generation_after": 2,
        "duration_ms": 12.5,
        "metadata": {"tool_name": "trigger_action", "action_id": "ENTER"},
    }
    values.update(overrides)
    return ObservabilityEvent(**values)


def test_valid_event_serializes_with_versioned_taxonomy():
    event = make_event()

    payload = event.to_dict()

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["event_type"] == "action.completed"
    assert payload["source_layer"] == "layer4"
    assert payload["outcome"] == "success"
    assert payload["metadata"] == {"tool_name": "trigger_action", "action_id": "ENTER"}


def test_all_taxonomy_values_are_stable_strings():
    assert all(event_type.value.count(".") == 1 for event_type in EventType)
    assert all(isinstance(layer.value, str) for layer in SourceLayer)
    assert all(isinstance(severity.value, str) for severity in Severity)
    assert all(isinstance(outcome.value, str) for outcome in EventOutcome)


@pytest.mark.parametrize(
    "field",
    [
        "event_id",
        "event_type",
        "timestamp_utc",
        "severity",
        "source_layer",
        "source_component",
        "outcome",
        "correlation_id",
    ],
)
def test_missing_required_fields_are_rejected(field):
    values = {
        "event_id": "evt-test-01",
        "event_type": EventType.ACTION_COMPLETED,
        "timestamp_utc": FIXED_TIMESTAMP,
        "severity": Severity.INFO,
        "source_layer": SourceLayer.LAYER4,
        "source_component": "test",
        "outcome": EventOutcome.SUCCESS,
        "correlation_id": "corr-test-01",
    }
    del values[field]

    with pytest.raises(TypeError):
        ObservabilityEvent(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_type", "action.unknown"),
        ("severity", "NOTICE"),
        ("source_layer", "arbitrary"),
        ("outcome", "unknown"),
    ],
)
def test_invalid_enum_values_are_rejected(field, value):
    with pytest.raises(EventValidationError, match=field):
        make_event(**{field: value})


def test_invalid_schema_version_is_rejected():
    with pytest.raises(EventValidationError, match="schema_version"):
        make_event(schema_version="2.0")


@pytest.mark.parametrize("field", ["event_id", "correlation_id", "runtime_id"])
def test_invalid_identifiers_are_rejected(field):
    with pytest.raises(EventValidationError, match="identifier"):
        make_event(**{field: "contains spaces"})


@pytest.mark.parametrize(
    "field",
    ["session_id", "operation_id", "runtime_id", "ticket_id", "source_component"],
)
@pytest.mark.parametrize("value", ["session-secret", "runtime-password", "typed_value"])
def test_identifier_fields_reject_sensitive_markers(field, value):
    with pytest.raises(EventValidationError, match="sensitive marker"):
        make_event(**{field: value})


def test_identifier_policy_preserves_existing_reference_shapes():
    for field, value in (
        ("session_id", "session-test"),
        ("operation_id", "op-test-01"),
        ("runtime_id", "runtime-test"),
        ("ticket_id", "ticket-test-01"),
        ("source_component", "RuntimeOrchestrator"),
    ):
        assert is_safe_identifier(field, value) is True
        make_event(**{field: value})


def test_component_identifier_has_explicit_shorter_limit():
    with pytest.raises(EventValidationError, match="valid identifier"):
        make_event(source_component="component-" + ("x" * 60))


def test_oversized_string_is_rejected():
    with pytest.raises(EventValidationError, match="exceeds"):
        make_event(source_component="x" * 257)


def test_screen_hash_requires_lowercase_sha256_hex():
    with pytest.raises(EventValidationError, match="screen_hash"):
        make_event(metadata={"screen_hash": "SECRET SCREEN CONTENT"})
    with pytest.raises(EventValidationError, match="screen_hash"):
        make_event(metadata={"screen_hash": "A" * 64})
    make_event(metadata={"screen_hash": "a" * 64})


def test_oversized_serialized_event_is_rejected_by_actual_json_bytes(monkeypatch):
    event = make_event(metadata={"tool_name": "trigger_action"})
    serialized_size = len(event.serialized_bytes())
    monkeypatch.setattr(observability, "MAX_EVENT_BYTES", serialized_size - 1)

    with pytest.raises(EventValidationError, match="serialized bytes"):
        event._validate_serialized_size()

    monkeypatch.setattr(observability, "MAX_EVENT_BYTES", serialized_size)
    event._validate_serialized_size()


def test_nested_metadata_is_rejected():
    with pytest.raises(EventValidationError, match="invalid type"):
        make_event(metadata={"tool_name": {"nested": "value"}})


def test_disallowed_metadata_is_rejected():
    with pytest.raises(EventValidationError, match="not allowlisted"):
        make_event(metadata={"arbitrary_key": "value"})


@pytest.mark.parametrize(
    "metadata",
    [
        {"password": "synthetic-secret"},
        {"typed_value": "synthetic-input"},
        {"raw_screen": "synthetic-screen"},
        {"exception": "Traceback (most recent call last)"},
        {"target_name": "password=synthetic-secret"},
        {"tool_name": "screen contents\nwith rows"},
    ],
)
def test_sensitive_payload_keys_and_content_are_rejected(metadata):
    with pytest.raises(EventValidationError):
        make_event(metadata=metadata)


def test_unsanitized_exception_detail_is_rejected():
    with pytest.raises(EventValidationError, match="unsanitized"):
        make_event(source_component="Traceback (most recent call last)")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-18T12:00:00",
        "2026-10-18T12:00:00+00:00",
        "2026-10-18T14:00:00+02:00",
        "2026-10-18T12:00:00UTC",
        "not-a-timestamp",
        "2026-02-30T12:00:00Z",
    ],
)
def test_timestamp_must_be_valid_explicit_utc(timestamp):
    with pytest.raises(EventValidationError, match="timestamp_utc"):
        make_event(timestamp_utc=timestamp)


def test_timestamp_accepts_fixed_utc_fractional_seconds():
    event = make_event(timestamp_utc=FIXED_TIMESTAMP)
    assert event.to_dict()["timestamp_utc"] == FIXED_TIMESTAMP


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_id", 1),
        ("session_id", 1),
        ("operation_id", 1),
        ("ticket_id", 1),
        ("generation_before", 1.5),
        ("generation_after", "2"),
        ("duration_ms", "12.5"),
        ("duration_ms", True),
    ],
)
def test_optional_field_types_are_rejected(field, value):
    with pytest.raises(EventValidationError):
        make_event(**{field: value})


@pytest.mark.parametrize("field", ["generation_before", "generation_after"])
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_generation_values_are_rejected(field, value):
    with pytest.raises(EventValidationError, match=field):
        make_event(**{field: value})


def test_boolean_numeric_metadata_is_rejected():
    with pytest.raises(EventValidationError, match="invalid type"):
        make_event(metadata={"batch_size": True})


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1.0, 86_400_001.0])
def test_invalid_duration_values_are_rejected(duration):
    with pytest.raises(EventValidationError, match="duration_ms"):
        make_event(duration_ms=duration)
