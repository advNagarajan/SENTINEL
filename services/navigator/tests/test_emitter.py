"""Unit tests for the Navigator emitter — structural hash, action signatures, and state capture."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.navigator.emitter import (
    compute_structural_hash,
    build_action_signature,
    NavigatorEmitter,
)


class FakeField:
    """Minimal field mock matching RuntimeState.fields values."""
    def __init__(self, field_id, label, row, col, length, protected):
        self.field_id = field_id
        self.label = label
        self.row = row
        self.col = col
        self.length = length
        self.protected = protected
        self.value = ""


class FakeState:
    """Minimal RuntimeState mock."""
    def __init__(self, screen_hash, title, fields):
        self.screen_hash = screen_hash
        self.title = title
        self.fields = fields


def test_structural_hash_ignores_values():
    """Two screens with same structure but different field values should have the same structural hash."""
    fields_a = {
        "fld_userid": FakeField("fld_userid", "User ID", 10, 20, 8, False),
    }
    fields_b = {
        "fld_userid": FakeField("fld_userid", "User ID", 10, 20, 8, False),
    }
    fields_b["fld_userid"].value = "ADMIN"

    hash_a = compute_structural_hash("CICS Login", fields_a)
    hash_b = compute_structural_hash("CICS Login", fields_b)

    assert hash_a == hash_b


def test_structural_hash_differs_on_structure_change():
    """Screens with different field layouts should have different structural hashes."""
    fields_a = {
        "fld_userid": FakeField("fld_userid", "User ID", 10, 20, 8, False),
    }
    fields_b = {
        "fld_userid": FakeField("fld_userid", "User ID", 10, 20, 8, False),
        "fld_password": FakeField("fld_password", "Password", 12, 20, 8, True),
    }

    hash_a = compute_structural_hash("CICS Login", fields_a)
    hash_b = compute_structural_hash("CICS Login", fields_b)

    assert hash_a != hash_b


def test_structural_hash_differs_on_title_change():
    """Different titles should produce different structural hashes."""
    fields = {"fld_x": FakeField("fld_x", "X", 5, 5, 10, False)}

    hash_a = compute_structural_hash("Screen A", fields)
    hash_b = compute_structural_hash("Screen B", fields)

    assert hash_a != hash_b


def test_structural_hash_is_deterministic():
    """Same input should always produce the same hash."""
    fields = {"fld_x": FakeField("fld_x", "X", 5, 5, 10, False)}
    h1 = compute_structural_hash("Title", fields)
    h2 = compute_structural_hash("Title", fields)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_build_action_signature():
    """Verify canonical action signature format."""
    assert build_action_signature("set_field_and_submit", "fld_userid", "ENTER") == "set_field_and_submit:fld_userid:ENTER"
    assert build_action_signature("trigger_action", None, "PF3") == "trigger_action::PF3"
    assert build_action_signature("fill_form_and_submit", "multi", "ENTER") == "fill_form_and_submit:multi:ENTER"


def test_capture_pre_state():
    """Verify that capture_pre_state snapshots the correct data."""
    emitter = NavigatorEmitter(base_url="http://localhost:8100", target_name="test")

    state = FakeState(
        screen_hash="abc123" + "0" * 58,
        title="Login Screen",
        fields={"fld_userid": FakeField("fld_userid", "User ID", 10, 20, 8, False)},
    )

    emitter.capture_pre_state(state)

    assert emitter._pre_state is not None
    assert emitter._pre_state["screen_hash"] == state.screen_hash
    assert emitter._pre_state["title"] == "Login Screen"
    assert "fld_userid" in emitter._pre_state["field_ids"]
    assert len(emitter._pre_state["structural_hash"]) == 64


@pytest.mark.asyncio
async def test_emit_skips_self_transition():
    """Verify that transitions where screen didn't change are skipped."""
    emitter = NavigatorEmitter(base_url="http://localhost:8100", target_name="test")

    same_hash = "abc123" + "0" * 58
    state = FakeState(
        screen_hash=same_hash,
        title="Login",
        fields={},
    )

    emitter.capture_pre_state(state)

    # Emit with same hash — should be skipped
    await emitter.emit_transition(
        new_state=state,
        tool_name="trigger_action",
        arguments={"action_id": "ENTER"},
        runtime_id="mock",
        latency_ms=100.0,
    )

    # Pre-state should be cleared
    assert emitter._pre_state is None


@pytest.mark.asyncio
async def test_emit_clears_pre_state_even_on_skip():
    """Pre-state should always be cleared after emit, even if skipped."""
    emitter = NavigatorEmitter(base_url="http://localhost:8100", target_name="test")

    state = FakeState(screen_hash="a" * 64, title="X", fields={})
    emitter.capture_pre_state(state)

    assert emitter._pre_state is not None

    await emitter.emit_transition(
        new_state=state,
        tool_name="trigger_action",
        arguments={},
        runtime_id="mock",
        latency_ms=0,
    )

    assert emitter._pre_state is None


@pytest.mark.asyncio
async def test_emitter_query_methods():
    """Verify that all emitter query methods make correct HTTP calls."""
    emitter = NavigatorEmitter(base_url="http://localhost:8100", target_name="test")

    mock_client = AsyncMock()
    emitter._client = mock_client

    # get_neighbors
    mock_resp_neighbors = MagicMock()
    mock_resp_neighbors.status_code = 200
    mock_resp_neighbors.json.return_value = {"center_hash": "aaa", "nodes": []}
    mock_client.get.return_value = mock_resp_neighbors

    res = await emitter.get_neighbors("aaa", depth=2)
    assert res["center_hash"] == "aaa"

    # find_path
    mock_resp_path = MagicMock()
    mock_resp_path.status_code = 200
    mock_resp_path.json.return_value = {"found": True, "path_length": 1}
    mock_client.get.return_value = mock_resp_path

    res = await emitter.find_path("aaa", "bbb")
    assert res["found"] is True

    # get_stats
    mock_resp_stats = MagicMock()
    mock_resp_stats.status_code = 200
    mock_resp_stats.json.return_value = {"total_screens": 10}
    mock_client.get.return_value = mock_resp_stats

    res = await emitter.get_stats()
    assert res["total_screens"] == 10

    # search_screens
    mock_resp_search = MagicMock()
    mock_resp_search.status_code = 200
    mock_resp_search.json.return_value = {"total_found": 1, "screens": []}
    mock_client.get.return_value = mock_resp_search

    res = await emitter.search_screens(query="LOGIN", limit=5)
    assert res["total_found"] == 1

    # query_cypher
    mock_resp_cypher = MagicMock()
    mock_resp_cypher.status_code = 200
    mock_resp_cypher.json.return_value = {"count": 1, "rows": []}
    mock_client.post.return_value = mock_resp_cypher

    res = await emitter.query_cypher("MATCH (s:Screen) RETURN s")
    assert res["count"] == 1

