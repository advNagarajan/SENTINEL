"""Unit tests validating the L2 -> L3 handoff contract and invariant rules."""
import pytest
from layer2.reducer import TN3270StateReducer
from schemas.contracts import ContractValidationError, L2toL3HandoffPayload, validate_l2_to_l3_contract
from schemas.pipeline import TransportFrame
from schemas.state import FieldEntry, RuntimeState, ScreenType, StabilityReport


def test_valid_l2_to_l3_handoff_contract():
    """Verify that a valid RuntimeState from TN3270StateReducer passes L2->L3 contract validation."""
    reducer = TN3270StateReducer(rows=24, cols=80)
    ebcdic_text = "LOGIN TO MAINFRAME".encode("cp037")
    frame = TransportFrame(raw_payload=bytes([0xF5, 0x1D, 0x20]) + ebcdic_text)

    decoded = reducer.parse_frame(frame)
    som = reducer.build_object_model(decoded)
    payload = reducer.reduce_payload(som, runtime_id="test_node", generation=1, validate=True)

    assert isinstance(payload, L2toL3HandoffPayload)
    assert payload.state.runtime_id == "test_node"
    assert len(payload.state.screen_hash) == 64
    assert payload.state.is_stable is True


def test_invalid_confidence_score_fails_contract():
    """Verify ContractValidationError is raised if confidence_score is out of bounds."""
    state = RuntimeState(
        runtime_id="node_1",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.5,  # Invalid (> 1.0)
        raw_grid=[" " * 80 for _ in range(24)],
        title=None,
        fields={},
        status_line=None,
        stability_report=StabilityReport(is_stable=True, method="oia", confidence=1.0, delta_value=0.0, iterations=1),
        screen_hash="a" * 64,
    )
    payload = L2toL3HandoffPayload(state=state)

    with pytest.raises(ContractValidationError, match="Invalid confidence_score"):
        validate_l2_to_l3_contract(payload)


def test_invalid_screen_hash_fails_contract():
    """Verify ContractValidationError is raised if screen_hash is not 64-char hex."""
    state = RuntimeState(
        runtime_id="node_1",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=[" " * 80 for _ in range(24)],
        title=None,
        fields={},
        status_line=None,
        stability_report=StabilityReport(is_stable=True, method="oia", confidence=1.0, delta_value=0.0, iterations=1),
        screen_hash="short_hash",  # Invalid (not 64 chars)
    )
    payload = L2toL3HandoffPayload(state=state)

    with pytest.raises(ContractValidationError, match="Invalid screen_hash"):
        validate_l2_to_l3_contract(payload)


def test_invalid_field_out_of_bounds_fails_contract():
    """Verify ContractValidationError is raised if a FieldEntry has out-of-bounds row/col."""
    invalid_field = FieldEntry(
        label="invalid", value="val", row=30, col=0, length=5, protected=True  # Row 30 > 24
    )
    state = RuntimeState(
        runtime_id="node_1",
        screen_type=ScreenType.TEXT_GRID,
        is_stable=True,
        confidence_score=1.0,
        raw_grid=[" " * 80 for _ in range(24)],
        title=None,
        fields={"invalid": invalid_field},
        status_line=None,
        stability_report=StabilityReport(is_stable=True, method="oia", confidence=1.0, delta_value=0.0, iterations=1),
        screen_hash="a" * 64,
    )
    payload = L2toL3HandoffPayload(state=state)

    with pytest.raises(ContractValidationError, match="Field 'invalid' row 30 out of bounds"):
        validate_l2_to_l3_contract(payload)
