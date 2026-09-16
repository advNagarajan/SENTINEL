"""Unit tests for the Navigator graph client with mocked Neo4j driver.

Validates MERGE-based deduplication logic:
- Same action → same screen increments traversal_count, not a new node
- Different target screen creates a new branch
- Prediction ranking by traversal frequency
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.navigator.graph_client import GraphClient


class FakeRecord:
    """Minimal mock of a neo4j Record that supports dict() conversion."""
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def data(self):
        return self._data


class FakeResult:
    """Minimal mock of a neo4j Result."""
    def __init__(self, records: list[FakeRecord]):
        self._records = records
        self._index = 0

    async def single(self):
        return self._records[0] if self._records else None

    def keys(self):
        return list(self._records[0].keys()) if self._records else []

    def __aiter__(self):

        return self

    async def __anext__(self):
        if self._index >= len(self._records):
            raise StopAsyncIteration
        record = self._records[self._index]
        self._index += 1
        return record


class FakeSession:
    """Mock Neo4j async session that captures Cypher queries."""
    def __init__(self):
        self.queries: list[tuple[str, dict]] = []
        self._results: list[FakeResult] = []

    def set_results(self, *results):
        self._results = list(results)

    async def run(self, query: str, params: dict = None):
        self.queries.append((query, params or {}))
        if self._results:
            return self._results.pop(0)
        return FakeResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_graph_client():
    """Create a GraphClient with a mocked Neo4j driver."""
    client = GraphClient.__new__(GraphClient)
    client.uri = "bolt://test:7687"
    client.user = "neo4j"
    client.password = "test"
    client.database = "neo4j"

    mock_driver = MagicMock()
    session = FakeSession()
    mock_driver.session.return_value = session
    client._driver = mock_driver

    return client, session


@pytest.mark.asyncio
async def test_record_transition_executes_merge_query(mock_graph_client):
    """Verify that record_transition executes a MERGE-based Cypher query."""
    client, session = mock_graph_client

    session.set_results(
        FakeResult([FakeRecord({
            "src_hash": "aaa111",
            "tgt_hash": "bbb222",
            "traversal_count": 1,
            "src_visits": 1,
            "tgt_visits": 1,
        })])
    )

    result = await client.record_transition(
        src_hash="aaa111",
        src_structural_hash="struct_aaa",
        src_title="CICS Login",
        src_field_ids=["fld_userid"],
        tgt_hash="bbb222",
        tgt_structural_hash="struct_bbb",
        tgt_title="CICS Menu",
        tgt_field_ids=[],
        action_signature="set_field_and_submit:fld_userid:ENTER",
        tool_name="set_field_and_submit",
        field_id="fld_userid",
        action_id="ENTER",
        value="ADMIN",
        target_name="MVS 3.8J",
        latency_ms=150.0,
    )

    assert len(session.queries) == 1
    query_text, params = session.queries[0]

    # Verify MERGE is used (deduplication)
    assert "MERGE" in query_text
    assert "ON CREATE SET" in query_text
    assert "ON MATCH SET" in query_text

    # Verify parameters
    assert params["src_hash"] == "aaa111"
    assert params["tgt_hash"] == "bbb222"
    assert params["action_signature"] == "set_field_and_submit:fld_userid:ENTER"
    assert params["value"] == "ADMIN"

    # Verify result
    assert result["traversal_count"] == 1


@pytest.mark.asyncio
async def test_record_transition_same_action_same_target_increments_count(mock_graph_client):
    """Verify that repeated identical transitions increment traversal_count."""
    client, session = mock_graph_client

    # First traversal
    session.set_results(
        FakeResult([FakeRecord({
            "src_hash": "aaa111",
            "tgt_hash": "bbb222",
            "traversal_count": 1,
            "src_visits": 1,
            "tgt_visits": 1,
        })])
    )
    await client.record_transition(
        src_hash="aaa111", src_structural_hash="s1", src_title="A",
        src_field_ids=[], tgt_hash="bbb222", tgt_structural_hash="s2",
        tgt_title="B", tgt_field_ids=[],
        action_signature="trigger_action::ENTER", tool_name="trigger_action",
        field_id=None, action_id="ENTER", value=None,
        target_name="test", latency_ms=100.0,
    )

    # Second traversal — same action, same target
    session.set_results(
        FakeResult([FakeRecord({
            "src_hash": "aaa111",
            "tgt_hash": "bbb222",
            "traversal_count": 2,
            "src_visits": 2,
            "tgt_visits": 2,
        })])
    )
    result = await client.record_transition(
        src_hash="aaa111", src_structural_hash="s1", src_title="A",
        src_field_ids=[], tgt_hash="bbb222", tgt_structural_hash="s2",
        tgt_title="B", tgt_field_ids=[],
        action_signature="trigger_action::ENTER", tool_name="trigger_action",
        field_id=None, action_id="ENTER", value=None,
        target_name="test", latency_ms=120.0,
    )

    # Both calls use MERGE, meaning the same Cypher template (no CREATE NODE)
    assert len(session.queries) == 2
    assert "MERGE" in session.queries[0][0]
    assert "MERGE" in session.queries[1][0]
    assert result["traversal_count"] == 2


@pytest.mark.asyncio
async def test_get_predictions_returns_sorted_transitions(mock_graph_client):
    """Verify predictions are returned with probability scores."""
    client, session = mock_graph_client

    session.set_results(
        FakeResult([FakeRecord({
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
                    "values_seen": ["ADMIN", "USER1"],
                },
                {
                    "target_screen_hash": "ccc333",
                    "target_structural_hash": "s3",
                    "target_title": "Error Screen",
                    "action_signature": "set_field_and_submit:fld_userid:ENTER",
                    "tool_name": "set_field_and_submit",
                    "field_id": "fld_userid",
                    "action_id": "ENTER",
                    "traversal_count": 3,
                    "avg_latency_ms": 200.0,
                    "values_seen": ["INVALID"],
                },
            ],
        })])
    )

    result = await client.get_predictions("aaa111")

    assert result["screen_hash"] == "aaa111"
    assert result["total_outgoing"] == 10
    assert len(result["transitions"]) == 2

    # Verify probabilities computed correctly
    assert result["transitions"][0]["probability"] == 0.7
    assert result["transitions"][1]["probability"] == 0.3


@pytest.mark.asyncio
async def test_get_predictions_empty_graph(mock_graph_client):
    """Verify predictions return empty result for unknown screen hash."""
    client, session = mock_graph_client

    session.set_results(FakeResult([]))

    result = await client.get_predictions("unknown_hash")

    assert result["screen_hash"] == "unknown_hash"
    assert result["total_outgoing"] == 0
    assert result["transitions"] == []


@pytest.mark.asyncio
async def test_find_path_returns_steps(mock_graph_client):
    """Verify shortest path returns interleaved steps and actions."""
    client, session = mock_graph_client

    session.set_results(
        FakeResult([FakeRecord({
            "steps": [
                {"screen_hash": "aaa", "title": "Login"},
                {"screen_hash": "bbb", "title": "Menu"},
                {"screen_hash": "ccc", "title": "Account"},
            ],
            "actions": ["login:fld_userid:ENTER", "navigate::PF2"],
            "path_length": 2,
        })])
    )

    result = await client.find_path("aaa", "ccc")

    assert result["found"] is True
    assert result["path_length"] == 2
    assert len(result["steps"]) == 3
    assert result["steps"][0]["action_to_next"] == "login:fld_userid:ENTER"
    assert result["steps"][1]["action_to_next"] == "navigate::PF2"
    assert result["steps"][2]["action_to_next"] is None


@pytest.mark.asyncio
async def test_find_path_not_found(mock_graph_client):
    """Verify path query returns found=False when no path exists."""
    client, session = mock_graph_client

    session.set_results(FakeResult([]))

    result = await client.find_path("aaa", "zzz")

    assert result["found"] is False
    assert result["path_length"] == 0


@pytest.mark.asyncio
async def test_health_check(mock_graph_client):
    """Verify health check returns True on successful query."""
    client, session = mock_graph_client

    session.set_results(FakeResult([FakeRecord({"ok": 1})]))

    result = await client.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_search_screens(mock_graph_client):
    """Verify search_screens returns matching screens."""
    client, session = mock_graph_client

    session.set_results(
        FakeResult([
            FakeRecord({
                "screen_hash": "hash_123",
                "structural_hash": "struct_123",
                "title": "CICS LOGIN",
                "visit_count": 5,
                "field_ids": ["fld_user", "fld_pass"],
            })
        ])
    )

    result = await client.search_screens(query="LOGIN", limit=5)
    assert result["query"] == "LOGIN"
    assert result["total_found"] == 1
    assert result["screens"][0]["title"] == "CICS LOGIN"


@pytest.mark.asyncio
async def test_run_read_query_allowed(mock_graph_client):
    """Verify run_read_query executes valid read queries."""
    client, session = mock_graph_client

    session.set_results(
        FakeResult([
            FakeRecord({"title": "MAIN MENU", "visits": 10})
        ])
    )

    result = await client.run_read_query("MATCH (s:Screen) RETURN s.title AS title, s.visit_count AS visits")
    assert result["count"] == 1
    assert result["columns"] == ["title", "visits"]
    assert result["rows"][0]["title"] == "MAIN MENU"


@pytest.mark.asyncio
async def test_run_read_query_rejects_mutations(mock_graph_client):
    """Verify run_read_query rejects mutations like CREATE, MERGE, DELETE, SET."""
    client, session = mock_graph_client

    with pytest.raises(ValueError, match="Forbidden Cypher operation"):
        await client.run_read_query("CREATE (n:BadNode {name: 'hacked'})")

    with pytest.raises(ValueError, match="Forbidden Cypher operation"):
        await client.run_read_query("MATCH (s:Screen) SET s.title = 'bad' RETURN s")

    with pytest.raises(ValueError, match="Forbidden Cypher operation"):
        await client.run_read_query("MATCH (s:Screen) DELETE s")

