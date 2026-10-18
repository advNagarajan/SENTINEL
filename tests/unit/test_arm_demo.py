"""Mock coverage for the ARM L1-to-L2 composition used by the live demo."""

import pytest
from dataclasses import replace

from layer2.arm import ARMSemanticActionIntent
from layer2.arm.models import ARMStability, StabilityStatus
from services.arm_demo import ARMEndToEnd, validate_counter_transition
from arm_test_support import MockARMDriver, make_frame


@pytest.mark.asyncio
async def test_arm_composition_transports_l1_frame_into_l2_state_and_delta():
    flow = ARMEndToEnd(MockARMDriver())  # type: ignore[arg-type]
    await flow.connect_at_main()
    before, before_delta = await flow.observe_boundary()
    after, delta = await flow.increment_and_observe()

    assert before_delta is not None and before_delta.memory_changes == {}
    assert before.decoded_memory[0].value == 591034
    assert after.decoded_memory[0].value == 591035
    assert delta is not None
    assert delta.memory_changes["counter"] == {
        "old_raw_bytes_hex": "ba040900",
        "new_raw_bytes_hex": "bb040900",
        "old_value": 591034,
        "new_value": 591035,
    }
    assert flow.last_action_result is not None and flow.last_action_result.succeeded


@pytest.mark.asyncio
async def test_two_stable_boundaries_and_expected_delta_validate_transition():
    flow = ARMEndToEnd(MockARMDriver())  # type: ignore[arg-type]
    await flow.connect_at_main()
    before, _ = await flow.observe_boundary()
    after, delta = await flow.increment_and_observe()

    assert before.stability.stable is True
    assert after.stability.stable is True
    validation = validate_counter_transition(before, after, delta)
    assert validation.valid
    assert validation.reasons == ()


@pytest.mark.asyncio
async def test_missing_repeated_sample_never_validates_transition():
    flow = ARMEndToEnd(MockARMDriver())  # type: ignore[arg-type]
    await flow.connect_at_main()
    before, _ = await flow.observe()
    result = await flow.perform_action(ARMSemanticActionIntent("increment_counter", {"symbol": "counter"}))
    assert result.status.value == "unstable"
    assert result.after_state is None
    assert flow.driver.phase == "initial"


def test_validate_counter_transition_accepts_uint32_wraparound():
    flow = ARMEndToEnd(MockARMDriver())  # type: ignore[arg-type]
    before, _ = flow.abstractor.abstract_state(
        flow.abstractor.parse_observation(make_frame(0xFFFFFFFF, 100, "breakpoint-hit")), 1
    )
    after, delta = flow.abstractor.abstract_state(
        flow.abstractor.parse_observation(make_frame(0, 101, "end-stepping-range", pc=0x40000030)), 2, before
    )
    before = replace(before, stability=ARMStability(StabilityStatus.STABLE, True, "test", "test", 2))
    after = replace(after, stability=ARMStability(StabilityStatus.STABLE, True, "test", "test", 2))
    validation = validate_counter_transition(before, after, delta)
    assert validation.valid
    assert validation.reasons == ()
