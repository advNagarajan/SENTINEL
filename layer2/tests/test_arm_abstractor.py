"""Tests for rich ARM L1 observation parsing and independent ARM L2 semantics."""

import json
from itertools import count

import pytest

from layer2.arm import ARMStateAbstractor, ObservationStatus, StabilityStatus
from layer2.arm.validation import ARMValidationError
from schemas.pipeline import TransportFrame


def observed(value: object, **extra: object) -> dict[str, object]:
    return {"status": "observed", "value": value, **extra}


def sample_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "environment": "arm",
        "schema_version": "2.0",
        "identity": {
            "runtime_id": "arm-test",
            "architecture": observed("armv7-a", source="configuration"),
            "cpu": observed("cortex-a15", source="configuration"),
            "firmware": {"path": observed("firmware.elf"), "sha256": observed("a" * 64)},
        },
        "cpu": {
            "registers": {
                "status": "observed",
                "values": [
                    {"number": 0, "name": "r0", "status": "observed", "raw_value": "0x0000002a", "value": 42},
                    {"number": 13, "name": "sp", "status": "observed", "raw_value": "0x40001000", "value": 0x40001000},
                    {"number": 15, "name": "pc", "status": "observed", "raw_value": "0x4000002c", "value": 0x4000002C},
                ],
            },
            "pc": observed(0x4000002C, raw_value="0x4000002c"),
            "sp": observed(0x40001000, raw_value="0x40001000"),
            "status_flags": {"cpsr": observed(0x600001D3, raw_value="0x600001d3")},
        },
        "execution": {
            "state": observed("stopped"),
            "current_instruction": observed({"address": 0x4000002C, "text": "str r2, [r3, #0]", "raw": "mi-insn"}),
            "stop_reason": observed("breakpoint-hit"),
            "breakpoint_hit": observed({"breakpoint_id": "1"}),
        },
        "memory": {
            "status": "observed",
            "regions": [
                {
                    "request_id": "counter",
                    "status": "observed",
                    "address": 0x40000054,
                    "requested_address": None,
                    "symbol": "counter",
                    "size": 4,
                    "raw_bytes_hex": "ba040900",
                    "decode": {"type": "uint32", "endianness": "little", "signed": False},
                }
            ],
        },
        "debugging": {
            "breakpoints": observed(
                [{"id": "1", "enabled": True, "address": "0x4000002c", "location": "*0x4000002c"}]
            )
        },
        "metadata": {
            "frame_seq": 10,
            "timestamp": 123.0,
            "capabilities": ["registers", "pc", "sp", "memory", "execution", "breakpoints"],
            "completeness": {"requested": ["registers", "pc"], "observed": ["registers", "pc"], "not_requested": [], "unavailable": [], "failed": []},
            "errors": [],
        },
    }
    for dotted, value in changes.items():
        section, field = dotted.split("__", 1)
        payload[section][field] = value  # type: ignore[index]
    return payload


_frame_sequences = count(1)


def frame(payload: dict[str, object] | None = None, *, sequence: int | None = None, envelope_sequence: int | None = None) -> TransportFrame:
    payload = payload or sample_payload()
    frame_sequence = next(_frame_sequences) if sequence is None else sequence
    payload["metadata"]["frame_seq"] = frame_sequence  # type: ignore[index]
    return TransportFrame(
        raw_payload=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        frame_seq=frame_sequence if envelope_sequence is None else envelope_sequence,
        timestamp=123.0,
    )


def test_rich_observation_parses_identity_register_names_and_values():
    observation = ARMStateAbstractor().parse_observation(frame())
    assert observation.runtime_id == "arm-test"
    assert observation.architecture.value == "armv7-a"
    assert observation.cpu.value == "cortex-a15"
    assert observation.registers.value[0].name == "r0"
    assert observation.registers.value[0].observation.value == 42
    assert observation.registers.value[0].observation.raw_value == "0x0000002a"


def test_pc_sp_status_execution_stop_instruction_and_breakpoints_parse():
    observation = ARMStateAbstractor().parse_observation(frame())
    assert observation.pc.value == 0x4000002C
    assert observation.sp.value == 0x40001000
    assert observation.status_registers["cpsr"].value == 0x600001D3
    assert observation.execution_state.value.value == "stopped"
    assert observation.stop_reason.value == "breakpoint-hit"
    assert observation.current_instruction.value.text == "str r2, [r3, #0]"
    assert observation.breakpoint_hit.value == {"breakpoint_id": "1"}
    assert observation.breakpoints.value[0]["id"] == "1"


def test_selected_memory_stays_raw_in_observation_and_decodes_in_l2():
    abstractor = ARMStateAbstractor()
    observation = abstractor.parse_observation(frame())
    assert observation.memory[0].raw_bytes_hex == "ba040900"
    state, _ = abstractor.abstract_state(observation, generation=10)
    assert state.memory[0].raw_bytes_hex == "ba040900"
    assert state.decoded_memory[0].value == 591034
    assert state.decoded_memory[0].value_type == "uint32"
    assert state.decoded_memory[0].endianness == "little"


def test_all_four_statuses_are_preserved():
    payload = sample_payload()
    payload["cpu"]["sp"] = {"status": "not_requested"}  # type: ignore[index]
    payload["execution"]["stop_reason"] = {"status": "unavailable"}  # type: ignore[index]
    payload["execution"]["current_instruction"] = {  # type: ignore[index]
        "status": "failed", "error": {"type": "TimeoutError", "message": "disassembly timeout"}
    }
    observation = ARMStateAbstractor().parse_observation(frame(payload))
    assert observation.sp.status == ObservationStatus.NOT_REQUESTED
    assert observation.stop_reason.status == ObservationStatus.UNAVAILABLE
    assert observation.current_instruction.status == ObservationStatus.FAILED
    assert observation.pc.status == ObservationStatus.OBSERVED


@pytest.mark.parametrize("raw", [b"{", b"[]"])
def test_malformed_observation_is_rejected(raw: bytes):
    with pytest.raises(ValueError):
        ARMStateAbstractor().parse_observation(TransportFrame(raw_payload=raw))


def test_wrong_environment_is_rejected():
    payload = sample_payload()
    payload["environment"] = "tn3270"
    with pytest.raises(ValueError, match="Expected ARM"):
        ARMStateAbstractor().parse_observation(frame(payload))


def test_invalid_register_values_are_rejected_as_failed_not_zero():
    payload = sample_payload()
    payload["cpu"]["registers"]["values"][0]["value"] = "oops"  # type: ignore[index]
    observation = ARMStateAbstractor().parse_observation(frame(payload))
    assert observation.registers.value[0].observation.status == ObservationStatus.FAILED
    assert observation.registers.value[0].observation.value is None


def test_omitted_categories_preserve_not_requested_state():
    payload = sample_payload()
    payload["cpu"]["status_flags"] = {}  # type: ignore[index]
    payload["cpu"]["sp"] = {"status": "not_requested"}  # type: ignore[index]
    observation = ARMStateAbstractor().parse_observation(frame(payload))
    state, _ = ARMStateAbstractor().abstract_state(observation, 1)
    assert state.sp.status == ObservationStatus.NOT_REQUESTED
    assert state.status_registers == {}


def test_state_hash_is_deterministic_and_excludes_timestamp_generation_and_provenance():
    abstractor = ARMStateAbstractor()
    one = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)[0]
    payload = sample_payload()
    payload["metadata"]["timestamp"] = 999.0  # type: ignore[index]
    two = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 99)[0]
    assert one.state_hash == two.state_hash


def test_different_semantic_register_state_changes_hash():
    abstractor = ARMStateAbstractor()
    first = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)[0]
    payload = sample_payload()
    payload["cpu"]["registers"]["values"][0]["value"] = 43  # type: ignore[index]
    second = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 2)[0]
    assert first.state_hash != second.state_hash


def test_delta_detects_register_pc_memory_execution_stop_and_breakpoint_changes():
    abstractor = ARMStateAbstractor()
    before, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)
    payload = sample_payload()
    payload["cpu"]["registers"]["values"][0]["value"] = 43  # type: ignore[index]
    payload["cpu"]["pc"] = observed(0x40000030, raw_value="0x40000030")  # type: ignore[index]
    payload["execution"]["state"] = observed("running")  # type: ignore[index]
    payload["execution"]["stop_reason"] = observed("step")  # type: ignore[index]
    payload["memory"]["regions"][0]["raw_bytes_hex"] = "bb040900"  # type: ignore[index]
    payload["debugging"]["breakpoints"] = observed([])  # type: ignore[index]
    after, delta = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 2, before)
    assert delta is not None
    assert delta.register_changes["r0"] == {"old": 42, "new": 43}
    assert delta.pc_change == {"old": 0x4000002C, "new": 0x40000030}
    assert delta.memory_changes["counter"]["old_value"] == 591034
    assert delta.memory_changes["counter"]["new_value"] == 591035
    assert delta.execution_state_change["old"].value == "stopped"
    assert delta.stop_reason_change == {"old": "breakpoint-hit", "new": "step"}
    assert delta.breakpoint_changes["1"]["new"] is None
    assert after.generation == 2


def test_no_false_register_or_memory_changes_when_capture_failed_or_not_requested():
    abstractor = ARMStateAbstractor()
    before, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)
    payload = sample_payload()
    payload["cpu"]["registers"]["values"][0] = {"number": 0, "name": "r0", "status": "failed", "raw_value": None, "value": None, "error": {"message": "read failed"}}  # type: ignore[index]
    payload["memory"]["regions"][0] = {"request_id": "counter", "status": "failed", "size": 4, "symbol": "counter", "error": {"message": "read failed"}}  # type: ignore[index]
    after, delta = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 2, before)
    assert delta is not None
    assert "r0" not in delta.register_changes
    assert "counter" not in delta.memory_changes
    assert after.memory[0].status == ObservationStatus.FAILED


def test_stability_remains_unknown_from_single_sample_even_if_stopped():
    state, _ = ARMStateAbstractor().abstract_state(
        ARMStateAbstractor().parse_observation(frame()), 1
    )
    assert state.stability.status == StabilityStatus.UNKNOWN
    assert state.stability.stable is None


def test_two_matching_stopped_semantic_samples_become_stable():
    abstractor = ARMStateAbstractor()
    first, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)
    second, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 2)
    assert first.stability.status == StabilityStatus.UNKNOWN
    assert second.stability.status == StabilityStatus.STABLE
    assert second.stability.stable is True
    assert second.stability.samples == 2


def test_different_consecutive_semantic_samples_are_unstable_then_new_state_can_stabilize():
    abstractor = ARMStateAbstractor()
    first, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)
    payload = sample_payload()
    payload["memory"]["regions"][0]["raw_bytes_hex"] = "bb040900"  # type: ignore[index]
    changed, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 2, first)
    repeated, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 3, changed)
    assert changed.stability.status == StabilityStatus.UNSTABLE
    assert changed.stability.stable is False
    assert repeated.stability.status == StabilityStatus.STABLE


def test_timestamp_and_frame_sequence_alone_do_not_break_semantic_stability():
    abstractor = ARMStateAbstractor()
    first, _ = abstractor.abstract_state(abstractor.parse_observation(frame()), 1)
    payload = sample_payload()
    payload["metadata"]["timestamp"] = 999.0  # type: ignore[index]
    second, _ = abstractor.abstract_state(
        abstractor.parse_observation(frame(payload, sequence=first.source_frame_seq + 1)), 2, first
    )
    assert first.state_hash == second.state_hash
    assert second.stability.status == StabilityStatus.STABLE


def test_execution_unavailable_stays_unknown_and_never_becomes_stable():
    abstractor = ARMStateAbstractor()
    payload = sample_payload()
    payload["execution"]["state"] = {"status": "unavailable"}  # type: ignore[index]
    first, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 1)
    second, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 2, first)
    assert first.stability.status == second.stability.status == StabilityStatus.UNKNOWN
    assert second.stability.stable is None


def test_running_evidence_is_unstable_and_counter_change_resets_stability_candidate():
    abstractor = ARMStateAbstractor()
    payload = sample_payload()
    payload["execution"]["state"] = observed("running")  # type: ignore[index]
    running, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 1)
    assert running.stability.status == StabilityStatus.UNSTABLE

    stopped_payload = sample_payload()
    first, _ = abstractor.abstract_state(abstractor.parse_observation(frame(stopped_payload)), 2)
    changed_payload = sample_payload()
    changed_payload["memory"]["regions"][0]["raw_bytes_hex"] = "bb040900"  # type: ignore[index]
    changed, _ = abstractor.abstract_state(abstractor.parse_observation(frame(changed_payload)), 3, first)
    assert changed.stability.status == StabilityStatus.UNSTABLE


def test_memory_decode_configuration_mismatch_is_reported_failed():
    payload = sample_payload()
    payload["memory"]["regions"][0]["decode"] = {"type": "uint16", "endianness": "little"}  # type: ignore[index]
    state, _ = ARMStateAbstractor().abstract_state(ARMStateAbstractor().parse_observation(frame(payload)), 1)
    assert state.decoded_memory[0].decode_status == ObservationStatus.FAILED


def test_uint32_decoder_rejects_contradictory_signedness_metadata():
    payload = sample_payload()
    payload["memory"]["regions"][0]["decode"] = {  # type: ignore[index]
        "type": "uint32", "endianness": "little", "signed": True,
    }
    abstractor = ARMStateAbstractor()
    state, _ = abstractor.abstract_state(abstractor.parse_observation(frame(payload)), 1)
    assert state.decoded_memory[0].decode_status == ObservationStatus.FAILED


def test_observation_validation_rejects_duplicate_register_numbers():
    payload = sample_payload()
    values = payload["cpu"]["registers"]["values"]  # type: ignore[index]
    values.append(dict(values[0]))
    values[-1]["name"] = "duplicate"
    with pytest.raises(ARMValidationError, match="unique"):
        ARMStateAbstractor().parse_observation(frame(payload))


def test_transport_envelope_and_payload_sequence_must_match():
    with pytest.raises(ValueError, match="sequences"):
        ARMStateAbstractor().parse_observation(frame(envelope_sequence=99))


@pytest.mark.parametrize("sequence", [10, 9])
def test_repeated_and_out_of_order_frames_are_rejected(sequence: int):
    abstractor = ARMStateAbstractor()
    abstractor.parse_observation(frame(sequence=10))
    with pytest.raises(ValueError, match="stale|duplicated|out of order"):
        abstractor.parse_observation(frame(sequence=sequence))


def test_stability_assessment_does_not_count_duplicate_frame_sequences():
    from dataclasses import replace

    abstractor = ARMStateAbstractor()
    state, _ = abstractor.abstract_state(abstractor.parse_observation(frame(sequence=500)), 1)
    assessor = ARMStateAbstractor().stability
    first = assessor.assess(state)
    duplicate = assessor.assess(replace(state, generation=2))
    assert first.status == StabilityStatus.UNKNOWN
    assert duplicate.status == StabilityStatus.UNSTABLE
    assert duplicate.method == "stale_frame_sequence"
