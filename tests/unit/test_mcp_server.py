"""Minimal MCP adapter smoke tests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from layer4.mcp_server import _safe_runtime_id, create_server
from schemas.observability import EventOutcome, EventType
from services.observability_producer import DEFAULT_ENDPOINT, EventProducer, ProducerConfig
from services.observability_context import current_context


class RecordingProducer:
    def __init__(self, *, submit_error=False):
        self.events = []
        self.submit_error = submit_error

    def submit(self, event):
        if self.submit_error:
            raise RuntimeError("synthetic producer failure")
        self.events.append(event)
        return True


@pytest.fixture
def orchestrator():
    runtime = MagicMock()
    runtime.connect = AsyncMock()
    runtime.disconnect = AsyncMock()
    runtime.get_observation.return_value = {
        "generation": 1,
        "generation_token": "gen_1_test",
        "is_stable": True,
    }
    runtime.execute_tool = AsyncMock(return_value={"success": True, "generation": 2})
    runtime.state = SimpleNamespace(value="ready")
    return runtime


@pytest.mark.asyncio
async def test_mcp_tool_call_delegates_to_runtime_orchestrator(orchestrator):
    server = create_server(orchestrator=orchestrator)

    _content, structured = await server.call_tool("trigger_action", {
        "generation_token": "gen_1_test",
        "action_id": "ENTER",
    })

    assert structured == {"success": True, "generation": 2}
    orchestrator.execute_tool.assert_awaited_once_with(
        "trigger_action",
        {"generation_token": "gen_1_test", "action_id": "ENTER"},
    )


@pytest.mark.asyncio
async def test_mcp_read_tools_delegate_to_runtime(orchestrator):
    server = create_server(orchestrator=orchestrator)

    _content, structured = await server.call_tool("get_observation", {})
    assert structured == {
        "generation": 1,
        "generation_token": "gen_1_test",
        "is_stable": True,
    }
    _content, structured = await server.call_tool("get_screen_state", {})
    assert structured == {
        "success": True,
        "generation": 2,
    }
    orchestrator.execute_tool.assert_awaited_once_with("get_screen_state", {})


@pytest.mark.asyncio
async def test_mcp_tool_emits_request_and_success_response_with_shared_context(orchestrator):
    producer = RecordingProducer()
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    async with server.settings.lifespan(server):
        result = await wrapper("gen_1_test", "ENTER")

    assert result == {"success": True, "generation": 2}
    assert [event.event_type for event in producer.events] == [
        EventType.RUNTIME_CONNECTING,
        EventType.RUNTIME_CONNECTED,
        EventType.MCP_REQUEST,
        EventType.MCP_RESPONSE,
        EventType.SESSION_DISCONNECTING,
        EventType.SESSION_DISCONNECTED,
    ]
    request, response = producer.events[2:4]
    assert request.outcome is EventOutcome.STARTED
    assert response.outcome is EventOutcome.SUCCESS
    assert request.session_id == response.session_id
    assert request.correlation_id == response.correlation_id
    assert request.operation_id == response.operation_id
    assert request.metadata == {"tool_name": "trigger_action"}
    assert response.metadata["tool_name"] == "trigger_action"


@pytest.mark.asyncio
async def test_mcp_structured_failure_emits_failure_without_using_error_field_as_success(orchestrator):
    producer = RecordingProducer()
    orchestrator.execute_tool.return_value = {
        "success": False,
        "error": "ContractValidationError",
        "message": "secret=do-not-record",
    }
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    result = await wrapper("gen_1_test", "ENTER")

    response = producer.events[-1]
    assert result["success"] is False
    assert response.event_type is EventType.MCP_RESPONSE
    assert response.outcome is EventOutcome.FAILURE
    assert response.metadata["error_category"] == "contract_validation"
    assert "secret" not in response.to_dict()
    assert "do-not-record" not in response.to_dict()


@pytest.mark.asyncio
async def test_mcp_exception_emits_terminal_failure_and_preserves_exception(orchestrator):
    producer = RecordingProducer()
    orchestrator.execute_tool.side_effect = RuntimeError("password=do-not-record")
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    with pytest.raises(RuntimeError, match="password=do-not-record"):
        await wrapper("gen_1_test", "ENTER")

    response = producer.events[-1]
    assert response.event_type is EventType.MCP_RESPONSE
    assert response.outcome is EventOutcome.FAILURE
    assert response.metadata["error_category"] == "unknown"
    assert "password" not in response.to_dict()
    assert "do-not-record" not in response.to_dict()
    assert current_context() is None


@pytest.mark.asyncio
async def test_mcp_cancellation_emits_cancelled_response_and_restores_context(orchestrator):
    producer = RecordingProducer()

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    orchestrator.execute_tool.side_effect = cancel
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    with pytest.raises(asyncio.CancelledError):
        await wrapper("gen_1_test", "ENTER")

    assert producer.events[-1].event_type is EventType.MCP_RESPONSE
    assert producer.events[-1].outcome is EventOutcome.CANCELLED
    assert current_context() is None


@pytest.mark.asyncio
async def test_mcp_observability_failure_does_not_change_result_or_exception(orchestrator):
    producer = RecordingProducer(submit_error=True)
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    assert await wrapper("gen_1_test", "ENTER") == {"success": True, "generation": 2}

    orchestrator.execute_tool.side_effect = ValueError("synthetic failure")
    with pytest.raises(ValueError, match="synthetic failure"):
        await wrapper("gen_1_test", "ENTER")
    assert current_context() is None


@pytest.mark.asyncio
async def test_mcp_event_construction_failure_does_not_break_tool(monkeypatch, orchestrator):
    producer = RecordingProducer()
    import layer4.mcp_server as mcp_server

    monkeypatch.setattr(
        mcp_server,
        "ObservabilityEvent",
        lambda **_fields: (_ for _ in ()).throw(ValueError("synthetic construction failure")),
    )
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["trigger_action"].fn

    assert await wrapper("gen_1_test", "ENTER") == {"success": True, "generation": 2}
    assert producer.events == []
    assert current_context() is None


@pytest.mark.asyncio
async def test_mcp_calls_receive_distinct_operation_identities(orchestrator):
    producer = RecordingProducer()
    server = create_server(orchestrator=orchestrator, producer=producer)
    wrapper = server._tool_manager._tools["get_observation"].fn

    await wrapper()
    await wrapper()

    requests = [event for event in producer.events if event.event_type is EventType.MCP_REQUEST]
    assert len(requests) == 2
    assert requests[0].operation_id != requests[1].operation_id


@pytest.mark.asyncio
async def test_registered_mcp_wrapper_binds_distinct_operation_contexts(orchestrator):
    contexts = []

    def get_observation():
        contexts.append(current_context())
        return {"generation": 1, "generation_token": "gen_1_test", "is_stable": True}

    orchestrator.get_observation.side_effect = get_observation
    server = create_server(orchestrator=orchestrator)
    wrapper = server._tool_manager._tools["get_observation"].fn

    async with server.settings.lifespan(server):
        first = await wrapper()
        second = await wrapper()

    assert first["generation"] == second["generation"] == 1
    assert contexts[0].session_id != ""
    assert contexts[0].session_id == contexts[1].session_id
    assert contexts[0].correlation_id != contexts[1].correlation_id
    assert contexts[0].operation_id != contexts[1].operation_id
    assert current_context() is None


@pytest.mark.asyncio
async def test_mcp_lifespan_clears_session_for_calls_after_teardown(orchestrator):
    contexts = []

    def get_observation():
        contexts.append(current_context())
        return {"generation": 1, "generation_token": "gen_1_test", "is_stable": True}

    orchestrator.get_observation.side_effect = get_observation
    server = create_server(orchestrator=orchestrator)
    wrapper = server._tool_manager._tools["get_observation"].fn

    async with server.settings.lifespan(server):
        await wrapper()
    await wrapper()

    assert contexts[0].session_id != contexts[1].session_id
    assert current_context() is None


def test_default_mcp_producer_uses_environment_endpoint(monkeypatch):
    import layer4.mcp_server as mcp_server

    monkeypatch.setenv("SENTINEL_OBSERVABILITY_ENDPOINT", "http://logging.internal:8200/v1/events")
    producer = mcp_server._build_default_producer()

    assert isinstance(producer, EventProducer)
    assert producer.config.endpoint == "http://logging.internal:8200/v1/events"


def test_blank_or_malformed_environment_endpoint_uses_host_default(monkeypatch):
    import layer4.mcp_server as mcp_server

    for value in ("", "   ", "not-a-url"):
        monkeypatch.setenv("SENTINEL_OBSERVABILITY_ENDPOINT", value)
        assert mcp_server.resolve_observability_endpoint() == DEFAULT_ENDPOINT


def test_explicit_producer_takes_precedence_over_environment(monkeypatch, orchestrator):
    import layer4.mcp_server as mcp_server

    monkeypatch.setenv("SENTINEL_OBSERVABILITY_ENDPOINT", "http://environment:8200/v1/events")
    explicit = EventProducer(ProducerConfig(endpoint="http://explicit:8200/v1/events"))
    monkeypatch.setattr(
        mcp_server,
        "_build_default_producer",
        lambda: pytest.fail("environment must not replace an explicit producer"),
    )

    server = mcp_server.create_server(orchestrator=orchestrator, producer=explicit)

    assert server is not None
    assert explicit.config.endpoint == "http://explicit:8200/v1/events"


def test_runtime_identifier_with_sensitive_marker_is_omitted(orchestrator):
    orchestrator.runtime_id.return_value = "runtime-password"

    assert _safe_runtime_id(orchestrator) is None


@pytest.mark.asyncio
async def test_arm_mcp_tools_are_registered_and_route_only_to_arm_adapter():
    arm_runtime = MagicMock()
    arm_runtime.observe = AsyncMock(return_value={"success": True, "state": {"environment": "arm"}})
    arm_runtime.execute_action = AsyncMock(
        return_value={"success": True, "status": "succeeded", "evidence": {"verified": True}}
    )
    server = create_server(arm_runtime=arm_runtime)

    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert {"get_arm_observation", "execute_arm_action"} <= set(tools)
    assert set(tools["get_arm_observation"].parameters["properties"]) == {"runtime_id"}
    assert set(tools["execute_arm_action"].parameters["properties"]) == {
        "runtime_id", "action", "symbol"
    }

    _content, observation = await server.call_tool(
        "get_arm_observation", {"runtime_id": "arm-01"}
    )
    assert observation == {"success": True, "state": {"environment": "arm"}}
    arm_runtime.observe.assert_awaited_once_with("arm-01")

    _content, action = await server.call_tool(
        "execute_arm_action",
        {"runtime_id": "arm-01", "action": "increment_counter", "symbol": "counter"},
    )
    assert action["success"] is True
    assert action["evidence"]["verified"] is True
    arm_runtime.execute_action.assert_awaited_once_with("arm-01", "increment_counter", "counter")


@pytest.mark.asyncio
async def test_arm_mcp_returns_configuration_error_without_changing_default_runtime():
    server = create_server()
    _content, result = await server.call_tool("execute_arm_action", {
        "runtime_id": "arm-01", "action": "increment_counter", "symbol": "counter"
    })
    assert result["success"] is False
    assert result["error"] == "runtime_not_configured"
    assert result["execution_attempted"] is False
