"""Focused tests for the ARM L2 semantic action mapping boundary."""

import ast
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from layer2.arm import (
    ARMActionMapper,
    ARMActionStatus,
    ARMExecutionState,
    ARMStateAbstractor,
    ARMSemanticActionIntent,
    ObservationStatus,
    StabilityStatus,
    ARMActionResult,
)
from layer2.arm.models import semantic_state_hash
from services.arm_demo import ARMEndToEnd
from arm_test_support import MockARMDriver, make_frame
from layer1.arm.driver import ARMDriver
from layer1.arm.gdb_client import GDBBreakpoint


async def stable_flow():
    driver = MockARMDriver()
    flow = ARMEndToEnd(driver)  # type: ignore[arg-type]
    await flow.connect_at_main()
    state, _ = await flow.observe_boundary()
    return flow, driver, state


@pytest.mark.asyncio
async def test_increment_counter_maps_to_constrained_plan_and_verifies_delta():
    flow, driver, state = await stable_flow()

    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )

    assert result.status == ARMActionStatus.SUCCEEDED
    assert result.plan is not None
    assert result.plan.target_symbol == "counter"
    assert result.plan.target_address == 0x40000054
    assert result.plan.store_instruction_address == 0x4000002C
    assert result.plan.source_generation == state.generation
    assert result.plan.source_state_hash == state.state_hash
    assert result.plan.runtime_id == state.runtime_id
    assert result.plan.firmware_sha256 == "a" * 64
    assert result.plan.postcondition.before_value == 591034
    assert result.plan.postcondition.expected_value == 591035
    assert result.after_state is not None and result.after_state.decoded_memory[0].value == 591035
    assert result.delta is not None and result.delta.memory_changes["counter"]["new_value"] == 591035
    assert result.evidence is not None
    assert result.evidence.breakpoint_verified and result.evidence.breakpoint_cleanup_verified
    assert result.evidence.single_step_verified
    assert driver.sequence >= 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "message"),
    [
        (ARMSemanticActionIntent("reset"), "unsupported"),
        (ARMSemanticActionIntent("increment_counter"), "exactly one parameter"),
        (ARMSemanticActionIntent("increment_counter", {"symbol": "other"}), "only supports configured"),
        (ARMSemanticActionIntent("increment_counter", {"symbol": "counter", "address": "0x4000"}), "exactly one parameter"),
    ],
)
async def test_rejects_unsupported_or_malformed_intent_without_runtime_execution(intent, message):
    flow, driver, state = await stable_flow()
    result = await flow.action_mapper.execute(intent, state)
    assert result.status == ARMActionStatus.REJECTED
    assert message in result.message
    assert driver.phase == "initial"


@pytest.mark.asyncio
async def test_rejects_incomplete_or_unstable_state_before_resolution():
    flow, driver, state = await stable_flow()
    incomplete_memory = replace(state.memory[0], status=ObservationStatus.UNAVAILABLE)
    incomplete_decoded = replace(state.decoded_memory[0], value=None, value_type=None, endianness=None,
                                 decode_status=ObservationStatus.UNAVAILABLE)
    incomplete = replace(state, memory=(incomplete_memory,), decoded_memory=(incomplete_decoded,))
    incomplete = replace(incomplete, state_hash=semantic_state_hash(incomplete))
    unstable = replace(state, stability=replace(state.stability, status=StabilityStatus.UNSTABLE, stable=False))

    for candidate, fragment in ((incomplete, "unavailable"), (unstable, "stable")):
        result = await flow.action_mapper.execute(
            ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), candidate
        )
        assert result.status == (ARMActionStatus.EVIDENCE_UNAVAILABLE if candidate is incomplete else ARMActionStatus.UNSTABLE)
        assert fragment in result.message
    assert driver.phase == "initial"


@pytest.mark.asyncio
async def test_rejects_running_execution_and_ambiguous_target_address():
    flow, driver, state = await stable_flow()
    running = replace(state, execution_state=replace(state.execution_state, value=ARMExecutionState.RUNNING))
    intent = ARMSemanticActionIntent("increment_counter", {"symbol": "counter"})
    result = await flow.action_mapper.execute(intent, running)
    assert result.status == ARMActionStatus.REJECTED
    assert "stopped" in result.message

    driver.resolve_symbol = lambda _symbol: _different_address()  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(intent, state)
    assert result.status == ARMActionStatus.EVIDENCE_UNAVAILABLE
    assert "conflicts" in result.message
    assert driver.phase == "initial"


@pytest.mark.asyncio
async def test_ambiguous_store_instruction_resolution_is_unavailable_evidence():
    flow, driver, state = await stable_flow()

    async def ambiguous_store(_address: int, _symbol: str = "main") -> int:
        raise RuntimeError("Ambiguous increment stores")

    driver.find_store_instruction = ambiguous_store  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EVIDENCE_UNAVAILABLE
    assert "missing or ambiguous" in result.message
    assert driver.phase == "initial"


async def _different_address() -> int:
    return 0x40000058


@pytest.mark.asyncio
async def test_l1_failure_is_reported_once_without_retry():
    flow, driver, state = await stable_flow()
    calls = 0

    async def fail_before_run(_address: int) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("target is unavailable")

    driver.run_to_address = fail_before_run  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EXECUTION_UNCERTAIN
    assert result.execution_attempted and result.execution_may_have_occurred
    assert calls == 1
    assert driver.phase == "initial"


@pytest.mark.asyncio
async def test_uncertain_step_failure_is_never_retried():
    flow, driver, state = await stable_flow()
    calls = 0

    async def uncertain_step() -> None:
        nonlocal calls
        calls += 1
        driver.value += 1
        raise RuntimeError("MI connection lost after step request")

    driver.step_instruction = uncertain_step  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EXECUTION_UNCERTAIN
    assert "uncertain" in result.message
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_post_action_observation_is_distinct_from_execution_failure():
    flow, driver, state = await stable_flow()
    original_read = driver.read_frame
    calls = 0

    async def fail_after_step():
        nonlocal calls
        calls += 1
        if driver.phase == "after":
            raise RuntimeError("observation transport failed")
        return await original_read()

    driver.read_frame = fail_after_step  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EXECUTION_UNCERTAIN
    assert "post-action observation failed" in result.message
    assert driver.phase == "after"


@pytest.mark.asyncio
async def test_unstable_post_state_is_not_reported_as_success():
    flow, driver, state = await stable_flow()
    original_read = driver.read_frame
    after_samples = 0

    async def changing_reads():
        nonlocal after_samples
        if driver.phase == "after":
            after_samples += 1
            driver.sequence += 1
            return make_frame(driver.value, driver.sequence, driver.stop_reason, pc=0x40000030 + after_samples * 4)
        return await original_read()

    driver.read_frame = changing_reads  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.UNSTABLE
    assert after_samples == flow.action_mapper.max_observation_samples


@pytest.mark.asyncio
async def test_wrong_counter_transition_fails_postcondition():
    flow, driver, state = await stable_flow()
    original_step = driver.step_instruction

    async def increment_twice():
        await original_step()
        driver.value += 1

    driver.step_instruction = increment_twice  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.POSTCONDITION_FAILED
    assert result.observed_value == 591036


@pytest.mark.asyncio
async def test_concurrent_actions_on_one_runtime_are_serialized():
    flow, driver, state = await stable_flow()
    intent = ARMSemanticActionIntent("increment_counter", {"symbol": "counter"})
    first, second = await __import__("asyncio").gather(
        flow.action_mapper.execute(intent, state),
        flow.action_mapper.execute(intent, state),
    )
    assert sum(result.succeeded for result in (first, second)) == 1
    assert driver.value == 591035
    assert {first.status, second.status} == {ARMActionStatus.SUCCEEDED, ARMActionStatus.EVIDENCE_UNAVAILABLE}


@pytest.mark.asyncio
async def test_action_rejects_runtime_or_firmware_identity_mismatch():
    flow, _driver, state = await stable_flow()
    intent = ARMSemanticActionIntent("increment_counter", {"symbol": "counter"})
    wrong_runtime = replace(state, runtime_id="other-runtime")
    result = await flow.action_mapper.execute(intent, wrong_runtime)
    assert result.status == ARMActionStatus.EVIDENCE_UNAVAILABLE
    assert "runtime identity" in result.message

    wrong_firmware = replace(state, firmware_sha256=replace(state.firmware_sha256, value="b" * 64))
    wrong_firmware = replace(wrong_firmware, state_hash=semantic_state_hash(wrong_firmware))
    result = await flow.action_mapper.execute(intent, wrong_firmware)
    assert result.status == ARMActionStatus.EVIDENCE_UNAVAILABLE
    assert "firmware fingerprint" in result.message

    unavailable_firmware = replace(
        state, firmware_sha256=replace(state.firmware_sha256, status=ObservationStatus.UNAVAILABLE, value=None)
    )
    unavailable_firmware = replace(unavailable_firmware, state_hash=semantic_state_hash(unavailable_firmware))
    result = await flow.action_mapper.execute(intent, unavailable_firmware)
    assert result.status == ARMActionStatus.EVIDENCE_UNAVAILABLE
    assert "verified firmware SHA-256 is unavailable" in result.message


@pytest.mark.asyncio
async def test_inconsistent_decoded_counter_is_rejected_before_planning():
    flow, driver, state = await stable_flow()
    decoded = replace(state.decoded_memory[0], value=state.decoded_memory[0].value + 1)
    forged = replace(state, decoded_memory=(decoded,))
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), forged
    )
    assert result.status == ARMActionStatus.REJECTED
    assert "inconsistent with raw bytes" in result.message
    assert driver.phase == "initial"


def test_success_result_cannot_be_constructed_without_execution_evidence():
    with pytest.raises(ValueError, match="requires plan"):
        ARMActionResult(ARMActionStatus.SUCCEEDED, "increment_counter", "claimed success")


@pytest.mark.asyncio
async def test_cleanup_failure_is_preserved_with_uncertain_execution_result():
    flow, driver, state = await stable_flow()

    async def fail_with_cleanup(_address: int) -> None:
        error = RuntimeError("continue timed out")
        error.add_note("temporary breakpoint cleanup also failed: delete failed")
        raise error

    driver.run_to_address = fail_with_cleanup  # type: ignore[method-assign]
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EXECUTION_UNCERTAIN
    assert result.cleanup_errors == ("temporary breakpoint cleanup also failed: delete failed",)
    assert "delete failed" in result.message


@pytest.mark.asyncio
async def test_real_l1_cleanup_only_error_is_structured_through_mapper():
    flow, mock_driver, state = await stable_flow()
    l1 = ARMDriver("firmware.elf")
    l1._connected = True
    l1._frozen = True
    l1.client.read_execution_state = AsyncMock(return_value="stopped")  # type: ignore[method-assign]
    l1.client.insert_breakpoint_with_address = AsyncMock(return_value=GDBBreakpoint("9", 0x4000002C))  # type: ignore[method-assign]
    l1.client.clear_execution_evidence = Mock()
    l1.client.continue_execution = AsyncMock()
    l1.client.wait_for_stop = AsyncMock()
    l1.client.latest_stop_details = Mock(return_value={"reason": "breakpoint-hit", "bkptno": "9"})
    l1.read_pc = AsyncMock(return_value="0x4000002c")  # type: ignore[method-assign]

    async def fail_delete(_breakpoint_id: str) -> None:
        raise RuntimeError("delete rejected")

    l1.client.delete_breakpoint = fail_delete  # type: ignore[method-assign]
    mock_driver.run_to_address = l1.run_to_address  # type: ignore[method-assign]
    # Execute uses the same mock runtime for semantic observations, while the real
    # L1 method provides the structured cleanup failure.
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.status == ARMActionStatus.EXECUTION_UNCERTAIN
    assert result.primary_error is None
    assert any("delete breakpoint failed: delete rejected" in error for error in result.cleanup_errors)


@pytest.mark.asyncio
async def test_fully_populated_but_inconsistent_success_result_is_rejected():
    flow, _driver, state = await stable_flow()
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), state
    )
    assert result.succeeded and result.plan and result.delta and result.evidence and result.after_state
    after_firmware = replace(
        result.after_state,
        firmware_sha256=replace(result.after_state.firmware_sha256, value="b" * 64),
    )
    after_firmware = replace(after_firmware, state_hash=semantic_state_hash(after_firmware))
    bad_memory_changes = dict(result.delta.memory_changes)
    bad_memory_changes["counter"] = {**bad_memory_changes["counter"], "new_value": 591099}
    corruptions = [
        {"plan": replace(result.plan, postcondition=replace(result.plan.postcondition, expected_value=1))},
        {"after_state": replace(result.after_state, runtime_id="other-runtime")},
        {"after_state": after_firmware},
        {"observed_value": result.observed_value + 1},
        {"delta": replace(result.delta, state_hash_to="0" * 64)},
        {"delta": replace(result.delta, generation_to=result.delta.generation_to + 1)},
        {
            "after_state": replace(result.after_state, generation=result.before_state.generation),
            "delta": replace(result.delta, generation_to=result.before_state.generation),
        },
        {
            "after_state": replace(result.after_state, source_frame_seq=result.before_state.source_frame_seq),
        },
        {"delta": replace(result.delta, memory_changes=bad_memory_changes)},
        {"evidence": replace(result.evidence, instruction_address=result.evidence.instruction_address + 4)},
        {"evidence": replace(result.evidence, instruction_text="ldr r2, [r3]")},
        {"evidence": replace(result.evidence, post_step_pc=result.evidence.post_step_pc + 4)},
        {"evidence": replace(result.evidence, post_step_stop_reason="breakpoint-hit")},
    ]
    for changes in corruptions:
        with pytest.raises(ValueError, match="successful action"):
            replace(result, **changes)


@pytest.mark.asyncio
async def test_counter_increment_wraps_at_uint32_boundary():
    flow, driver, _state = await stable_flow()
    driver.value = 0xFFFFFFFF
    before, _ = await flow.observe_boundary()
    result = await flow.action_mapper.execute(
        ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}), before
    )
    assert result.succeeded
    assert result.observed_value == 0
    assert result.plan is not None and result.plan.postcondition.expected_value == 0


def test_l2_action_module_has_no_gdb_or_subprocess_integration():
    source_path = Path(__file__).parents[2] / "layer2" / "arm" / "actions.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("gdb" in item.lower() or "subprocess" in item.lower() for item in imported)
