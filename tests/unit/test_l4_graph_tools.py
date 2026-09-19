"""Unit tests for L4 LiveValidationHarness integration with the Navigator graph tool.

Verifies:
- view_navigation_graph is exposed as a single unified tool alongside L3 tools
- All 6 modes ('predict', 'neighbors', 'path', 'search', 'stats', 'cypher') dispatch properly
- Graph tool calls are read-only: no pre-state capture and no transition emissions
- Graceful degradation when navigator is disabled or unreachable
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from layer4.harness import LiveValidationHarness


@pytest.fixture
async def mock_harness():
    """Create a mock-connected LiveValidationHarness with mocked NavigatorEmitter."""
    harness = LiveValidationHarness(
        config_path="configs/mainframe.toml",
        navigator_url="http://localhost:8100",
        target_name="test_system",
    )
    await harness.connect_mock()

    # Mock the navigator emitter methods
    harness.navigator.get_predictions = AsyncMock(return_value={"screen_hash": "current", "transitions": []})
    harness.navigator.get_neighbors = AsyncMock(return_value={"center_hash": "current", "nodes": []})
    harness.navigator.find_path = AsyncMock(return_value={"found": True, "path_length": 1, "steps": []})
    harness.navigator.get_stats = AsyncMock(return_value={"total_screens": 5, "total_transitions": 10})
    harness.navigator.search_screens = AsyncMock(return_value={"total_found": 1, "screens": []})
    harness.navigator.query_cypher = AsyncMock(return_value={"count": 1, "rows": []})
    harness.navigator.capture_pre_state = MagicMock()
    harness.navigator.emit_transition = AsyncMock()

    yield harness
    await harness.disconnect()


@pytest.mark.asyncio
async def test_get_tools_exposes_single_view_navigation_graph_tool(mock_harness):
    """Verify that get_tools injects exactly one graph tool (view_navigation_graph)."""
    tools = mock_harness.get_tools()
    tool_names = [t["function"]["name"] for t in tools]

    # Exactly one graph inspection tool is added
    assert "view_navigation_graph" in tool_names
    graph_tools = [name for name in tool_names if "graph" in name or "predict" in name]
    assert graph_tools == ["view_navigation_graph"]

    # Verify tool schema modes
    graph_tool = next(t for t in tools if t["function"]["name"] == "view_navigation_graph")
    modes = graph_tool["function"]["parameters"]["properties"]["mode"]["enum"]
    assert modes == ["predict", "neighbors", "path", "search", "stats", "cypher"]


@pytest.mark.asyncio
async def test_step_view_navigation_graph_predict_mode(mock_harness):
    """Verify mode='predict' delegates to get_predictions."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "predict"})
    assert result["success"] is True
    assert result["mode"] == "predict"
    mock_harness.navigator.get_predictions.assert_called_once()
    # Ensure no pre-state was captured (read-only inspection)
    mock_harness.navigator.capture_pre_state.assert_not_called()
    mock_harness.navigator.emit_transition.assert_not_called()


@pytest.mark.asyncio
async def test_step_view_navigation_graph_neighbors_mode(mock_harness):
    """Verify mode='neighbors' delegates to get_neighbors."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "neighbors", "depth": 2})
    assert result["success"] is True
    assert result["mode"] == "neighbors"
    mock_harness.navigator.get_neighbors.assert_called_once()
    mock_harness.navigator.capture_pre_state.assert_not_called()


@pytest.mark.asyncio
async def test_step_view_navigation_graph_path_mode(mock_harness):
    """Verify mode='path' delegates to find_path."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "path", "to_hash": "dest_hash_123"})
    assert result["success"] is True
    assert result["mode"] == "path"
    mock_harness.navigator.find_path.assert_called_once()


@pytest.mark.asyncio
async def test_step_view_navigation_graph_path_mode_missing_to_hash(mock_harness):
    """Verify mode='path' fails cleanly when to_hash is missing."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "path"})
    assert result["success"] is False
    assert result["error"] == "MissingArgument"


@pytest.mark.asyncio
async def test_step_view_navigation_graph_search_mode(mock_harness):
    """Verify mode='search' delegates to search_screens."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "search", "query": "CICS", "limit": 5})
    assert result["success"] is True
    assert result["mode"] == "search"
    mock_harness.navigator.search_screens.assert_called_once_with(query="CICS", limit=5)


@pytest.mark.asyncio
async def test_step_view_navigation_graph_stats_mode(mock_harness):
    """Verify mode='stats' delegates to get_stats."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "stats"})
    assert result["success"] is True
    assert result["mode"] == "stats"
    mock_harness.navigator.get_stats.assert_called_once()


@pytest.mark.asyncio
async def test_step_view_navigation_graph_cypher_mode(mock_harness):
    """Verify mode='cypher' delegates to query_cypher."""
    result = await mock_harness.step(
        "view_navigation_graph",
        {"mode": "cypher", "cypher": "MATCH (s:Screen) RETURN s.title"}
    )
    assert result["success"] is True
    assert result["mode"] == "cypher"
    mock_harness.navigator.query_cypher.assert_called_once_with("MATCH (s:Screen) RETURN s.title", parameters=None)


@pytest.mark.asyncio
async def test_step_view_navigation_graph_cypher_missing_query(mock_harness):
    """Verify mode='cypher' fails cleanly when cypher query is missing."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "cypher"})
    assert result["success"] is False
    assert result["error"] == "MissingArgument"


@pytest.mark.asyncio
async def test_step_view_navigation_graph_invalid_mode(mock_harness):
    """Verify invalid mode returns structured error."""
    result = await mock_harness.step("view_navigation_graph", {"mode": "invalid_mode"})
    assert result["success"] is False
    assert result["error"] == "InvalidMode"


@pytest.mark.asyncio
async def test_step_view_navigation_graph_service_unavailable(mock_harness):
    """Verify graceful handling when navigator returns None (unreachable)."""
    mock_harness.navigator.get_stats.return_value = None
    result = await mock_harness.step("view_navigation_graph", {"mode": "stats"})
    assert result["success"] is False
    assert result["error"] == "NavigatorUnavailable"


@pytest.mark.asyncio
async def test_step_view_navigation_graph_when_disabled():
    """Verify graceful handling when navigator is disabled (navigator_url=None)."""
    harness = LiveValidationHarness(config_path="configs/mainframe.toml", navigator_url=None)
    await harness.connect_mock()

    tools = harness.get_tools()
    tool_names = [t["function"]["name"] for t in tools]
    assert "view_navigation_graph" not in tool_names

    result = await harness.step("view_navigation_graph", {"mode": "stats"})
    assert result["success"] is False
    assert result["error"] == "NavigatorDisabled"
    await harness.disconnect()


@pytest.mark.asyncio
async def test_harness_properties_when_connected_and_disconnected(mock_harness):
    """Verify is_connected, active_payload, active_state, and generation_token properties."""
    assert mock_harness.is_connected is True
    assert mock_harness.active_payload is not None
    assert mock_harness.active_state is not None
    assert mock_harness.generation_token is not None
    assert mock_harness.generation_token == mock_harness.active_state.generation_token

    # Disconnected harness
    unconnected_harness = LiveValidationHarness(config_path="configs/mainframe.toml")
    assert unconnected_harness.is_connected is False
    assert unconnected_harness.active_payload is None
    assert unconnected_harness.active_state is None
    assert unconnected_harness.generation_token is None


@pytest.mark.asyncio
async def test_interactive_loop_disconnected_safely_returns(capsys):
    """Verify interactive_loop exits cleanly without AttributeError when gateway is None."""
    from scripts.live_agent_runner import interactive_loop

    unconnected_harness = LiveValidationHarness(config_path="configs/mainframe.toml")
    await interactive_loop(unconnected_harness)
    captured = capsys.readouterr()
    assert "Error: Harness gateway is not connected or initialized" in captured.out
