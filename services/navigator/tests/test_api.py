"""Integration tests for the Navigator FastAPI API with mocked Neo4j backend.

Tests the full HTTP request/response cycle: POST transitions, GET predictions,
and verifies the round-trip data flow.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient


@pytest.fixture
def mock_graph_client():
    """Patch the graph_client singleton in the navigator main module."""
    with patch("services.navigator.main.graph_client") as mock_client:
        mock_client.connect = AsyncMock()
        mock_client.close = AsyncMock()
        mock_client.health_check = AsyncMock(return_value=True)
        mock_client.record_transition = AsyncMock()
        mock_client.get_predictions = AsyncMock()
        mock_client.get_neighbors = AsyncMock()
        mock_client.find_path = AsyncMock()
        mock_client.get_stats = AsyncMock()
        mock_client.search_screens = AsyncMock()
        mock_client.run_read_query = AsyncMock()
        yield mock_client



@pytest.fixture
def client(mock_graph_client):
    """Create a FastAPI TestClient with mocked graph backend."""
    from services.navigator.main import app
    return TestClient(app)


def test_health_endpoint(client, mock_graph_client):
    """Verify /health returns healthy when Neo4j is connected."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_health_endpoint_unhealthy(client, mock_graph_client):
    """Verify /health returns 503 when Neo4j is unavailable."""
    mock_graph_client.health_check.return_value = False
    response = client.get("/health")
    assert response.status_code == 503


def test_record_transition(client, mock_graph_client):
    """Verify POST /transitions records a transition and returns 201."""
    mock_graph_client.record_transition.return_value = {
        "src_hash": "aaa111",
        "tgt_hash": "bbb222",
        "traversal_count": 1,
        "src_visits": 1,
        "tgt_visits": 1,
    }

    payload = {
        "src_screen_hash": "aaa111",
        "src_structural_hash": "struct_aaa",
        "src_title": "CICS Login",
        "src_field_ids": ["fld_userid"],
        "tgt_screen_hash": "bbb222",
        "tgt_structural_hash": "struct_bbb",
        "tgt_title": "CICS Menu",
        "tgt_field_ids": [],
        "tool_name": "set_field_and_submit",
        "field_id": "fld_userid",
        "action_id": "ENTER",
        "value": "ADMIN",
        "action_signature": "set_field_and_submit:fld_userid:ENTER",
        "runtime_id": "mock_01",
        "target_name": "MVS 3.8J",
        "latency_ms": 150.0,
    }

    response = client.post("/transitions", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["recorded"] is True
    assert data["traversal_count"] == 1

    # Verify the graph client was called with correct parameters
    mock_graph_client.record_transition.assert_called_once()
    call_kwargs = mock_graph_client.record_transition.call_args
    assert call_kwargs.kwargs["src_hash"] == "aaa111"
    assert call_kwargs.kwargs["tgt_hash"] == "bbb222"


def test_predict_navigation(client, mock_graph_client):
    """Verify GET /predict/{hash} returns predictions with probabilities."""
    mock_graph_client.get_predictions.return_value = {
        "screen_hash": "aaa111",
        "title": "CICS Login",
        "total_outgoing": 10,
        "transitions": [
            {
                "target_screen_hash": "bbb222",
                "target_structural_hash": "s2",
                "target_title": "Menu",
                "action_signature": "set_field_and_submit:fld_userid:ENTER",
                "tool_name": "set_field_and_submit",
                "field_id": "fld_userid",
                "action_id": "ENTER",
                "traversal_count": 7,
                "avg_latency_ms": 100.0,
                "values_seen": ["ADMIN"],
                "probability": 0.7,
            },
        ],
    }

    response = client.get("/predict/aaa111")
    assert response.status_code == 200
    data = response.json()
    assert data["screen_hash"] == "aaa111"
    assert data["total_outgoing"] == 10
    assert len(data["transitions"]) == 1
    assert data["transitions"][0]["probability"] == 0.7


def test_predict_unknown_screen(client, mock_graph_client):
    """Verify prediction returns empty results for an unknown screen hash."""
    mock_graph_client.get_predictions.return_value = {
        "screen_hash": "unknown",
        "title": None,
        "total_outgoing": 0,
        "transitions": [],
    }

    response = client.get("/predict/unknown")
    assert response.status_code == 200
    data = response.json()
    assert data["total_outgoing"] == 0
    assert data["transitions"] == []


def test_find_path(client, mock_graph_client):
    """Verify GET /graph/paths returns shortest path."""
    mock_graph_client.find_path.return_value = {
        "from_hash": "aaa",
        "to_hash": "ccc",
        "found": True,
        "path_length": 2,
        "steps": [
            {"screen_hash": "aaa", "title": "Login", "action_to_next": "login"},
            {"screen_hash": "bbb", "title": "Menu", "action_to_next": "nav"},
            {"screen_hash": "ccc", "title": "Account", "action_to_next": None},
        ],
    }

    response = client.get("/graph/paths", params={"from_hash": "aaa", "to_hash": "ccc"})
    assert response.status_code == 200
    data = response.json()
    assert data["found"] is True
    assert data["path_length"] == 2
    assert len(data["steps"]) == 3


def test_get_stats(client, mock_graph_client):
    """Verify GET /graph/stats returns aggregate counts."""
    mock_graph_client.get_stats.return_value = {
        "total_screens": 15,
        "total_transitions": 22,
        "total_traversals": 150,
        "most_visited_screens": [{"screen_hash": "aaa", "title": "Login", "visit_count": 50}],
        "most_traversed_edges": [],
    }

    response = client.get("/graph/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["total_screens"] == 15
    assert data["total_transitions"] == 22


def test_transition_then_prediction_roundtrip(client, mock_graph_client):
    """Full round-trip: record transition, then query prediction."""
    # Record
    mock_graph_client.record_transition.return_value = {
        "src_hash": "aaa",
        "tgt_hash": "bbb",
        "traversal_count": 1,
        "src_visits": 1,
        "tgt_visits": 1,
    }
    post_resp = client.post("/transitions", json={
        "src_screen_hash": "aaa",
        "src_structural_hash": "s1",
        "src_title": "Screen A",
        "src_field_ids": [],
        "tgt_screen_hash": "bbb",
        "tgt_structural_hash": "s2",
        "tgt_title": "Screen B",
        "tgt_field_ids": [],
        "tool_name": "trigger_action",
        "action_id": "ENTER",
        "action_signature": "trigger_action::ENTER",
        "runtime_id": "test",
        "target_name": "test",
        "latency_ms": 50.0,
    })
    assert post_resp.status_code == 201

    # Predict
    mock_graph_client.get_predictions.return_value = {
        "screen_hash": "aaa",
        "title": "Screen A",
        "total_outgoing": 1,
        "transitions": [{
            "target_screen_hash": "bbb",
            "target_structural_hash": "s2",
            "target_title": "Screen B",
            "action_signature": "trigger_action::ENTER",
            "tool_name": "trigger_action",
            "traversal_count": 1,
            "avg_latency_ms": 50.0,
            "values_seen": [],
            "probability": 1.0,
        }],
    }
    get_resp = client.get("/predict/aaa")
    assert get_resp.status_code == 200
    assert get_resp.json()["transitions"][0]["target_screen_hash"] == "bbb"


def test_search_screens_endpoint(client, mock_graph_client):
    """Verify GET /graph/screens returns search results."""
    mock_graph_client.search_screens.return_value = {
        "query": "LOGIN",
        "total_found": 1,
        "screens": [{
            "screen_hash": "hash_123",
            "structural_hash": "struct_123",
            "title": "CICS LOGIN",
            "visit_count": 3,
            "field_ids": ["fld_user"],
        }],
    }
    resp = client.get("/graph/screens?query=LOGIN&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "LOGIN"
    assert len(data["screens"]) == 1
    assert data["screens"][0]["title"] == "CICS LOGIN"


def test_execute_query_endpoint(client, mock_graph_client):
    """Verify POST /graph/query executes valid Cypher query."""
    mock_graph_client.run_read_query.return_value = {
        "columns": ["title"],
        "rows": [{"title": "LOGIN"}],
        "count": 1,
    }
    resp = client.post("/graph/query", json={"query": "MATCH (s:Screen) RETURN s.title AS title"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["columns"] == ["title"]
    assert data["rows"][0]["title"] == "LOGIN"


def test_execute_query_endpoint_forbidden(client, mock_graph_client):
    """Verify POST /graph/query returns 400 when ValueError is raised."""
    mock_graph_client.run_read_query.side_effect = ValueError("Forbidden Cypher operation: CREATE")
    resp = client.post("/graph/query", json={"query": "CREATE (n:Bad)"})
    assert resp.status_code == 400
    assert "Forbidden Cypher operation" in resp.json()["detail"]

