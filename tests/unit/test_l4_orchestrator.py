"""Stage 1 tests for the runtime-agnostic Layer 4 orchestrator."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from layer4.orchestrator import (
    InvalidRuntimeState,
    LifecycleState,
    RuntimeNotConnected,
    RuntimeOrchestrator,
)


class RecordingAudit:
    def __init__(self):
        self.starts = []
        self.completions = []

    def log_action_start(self, **fields):
        self.starts.append(fields)

    def log_action_completion(self, **fields):
        self.completions.append(fields)


class FailingAudit:
    def log_action_start(self, **_fields):
        raise RuntimeError("audit start failed")

    def log_action_completion(self, **_fields):
        raise RuntimeError("audit completion failed")


@pytest.fixture
def pipeline():
    driver = SimpleNamespace(runtime_id="runtime-01")
    reducer = object()
    stability = object()
    lowerer = object()
    dispatcher = object()
    config = {"runtime": {"name": "test"}}
    return driver, reducer, stability, lowerer, dispatcher, config


@pytest.fixture
def orchestrator(pipeline):
    driver, reducer, stability, lowerer, dispatcher, config = pipeline
    session = MagicMock()
    session.connect = AsyncMock()
    session.initialize = AsyncMock(return_value=SimpleNamespace(runtime_id="runtime-01"))
    session.teardown = AsyncMock()
    session.disconnect = AsyncMock()

    gateway = MagicMock()
    gateway.get_tools.return_value = [{"name": "tool"}]
    gateway.get_observation.return_value.to_dict.return_value = {"generation": 1}
    gateway.execute_tool = AsyncMock(return_value={"success": True})
    gateway.get_active_payload.return_value.state = SimpleNamespace(runtime_id="runtime-01")

    pipeline_factory = MagicMock(return_value=pipeline)
    session_factory = MagicMock(return_value=session)
    gateway_factory = MagicMock(return_value=gateway)
    audit = RecordingAudit()

    instance = RuntimeOrchestrator(
        config_path="test.toml",
        pipeline_factory=pipeline_factory,
        session_factory=session_factory,
        gateway_factory=gateway_factory,
        audit_logger=audit,
    )
    return instance, pipeline_factory, session_factory, gateway_factory, session, gateway, audit


def test_initial_state_is_disconnected(orchestrator):
    instance, *_ = orchestrator
    assert instance.state is LifecycleState.DISCONNECTED
    assert instance.is_connected() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_observation", "get_tools"])
async def test_read_operations_require_connection(orchestrator, operation):
    instance, *_ = orchestrator
    with pytest.raises(RuntimeNotConnected):
        getattr(instance, operation)()


@pytest.mark.asyncio
async def test_execute_requires_connection(orchestrator):
    instance, *_ = orchestrator
    with pytest.raises(RuntimeNotConnected):
        await instance.execute_tool("tool", {})


@pytest.mark.asyncio
async def test_connect_reuses_existing_pipeline_and_reaches_ready(orchestrator):
    instance, pipeline_factory, session_factory, gateway_factory, session, _gateway, _audit = orchestrator

    await instance.connect()

    pipeline_factory.assert_called_once_with("test.toml")
    session_factory.assert_called_once_with(
        pipeline_factory.return_value[0],
        pipeline_factory.return_value[1],
        pipeline_factory.return_value[2],
        pipeline_factory.return_value[5],
    )
    session.connect.assert_awaited_once()
    session.initialize.assert_awaited_once()
    gateway_factory.assert_called_once()
    assert instance.state is LifecycleState.READY
    assert instance.is_connected() is True
    assert instance.runtime_id() == "runtime-01"


@pytest.mark.asyncio
async def test_gateway_operations_delegate_unchanged(orchestrator):
    instance, _pipeline_factory, _session_factory, _gateway_factory, _session, gateway, _audit = orchestrator
    await instance.connect()

    assert instance.get_observation() == {"generation": 1}
    assert instance.get_tools() == [{"name": "tool"}]
    result = await instance.execute_tool("tool", {"value": "unchanged"})

    assert result == {"success": True}
    gateway.execute_tool.assert_awaited_once_with("tool", {"value": "unchanged"})
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_successful_action_creates_audit_records(orchestrator):
    instance, *_factory_args, gateway, audit = orchestrator
    await instance.connect()
    gateway.execute_tool.return_value = {
        "success": True,
        "ticket_id": "ticket-success",
        "generation_before": 1,
        "generation_after": 2,
        "execution_time_ms": 12.5,
    }

    result = await instance.execute_tool("trigger_action", {"action_id": "ENTER"})

    assert result["success"] is True
    assert len(audit.starts) == 1
    assert len(audit.completions) == 1
    assert audit.starts[0]["tool_name"] == "trigger_action"
    assert audit.starts[0]["runtime_id"] == "runtime-01"
    assert audit.completions[0]["ticket_id"] == "ticket-success"
    assert audit.completions[0]["generation_before"] == 1
    assert audit.completions[0]["generation_after"] == 2
    assert audit.completions[0]["execution_time_ms"] == 12.5


@pytest.mark.asyncio
async def test_structured_failure_creates_audit_record_and_is_preserved(orchestrator):
    instance, *_factory_args, gateway, audit = orchestrator
    await instance.connect()
    failure = {
        "success": False,
        "error": "ContractValidationError",
        "message": "stale generation",
        "ticket_id": "ticket-failed",
        "generation_before": 3,
        "generation_after": 3,
        "execution_time_ms": 4.5,
    }
    gateway.execute_tool.return_value = failure

    assert await instance.execute_tool("trigger_action", {}) == failure
    assert audit.completions[0]["success"] is False
    assert audit.completions[0]["error"] == "ContractValidationError"
    assert audit.completions[0]["message"] == "stale generation"
    assert audit.completions[0]["ticket_id"] == "ticket-failed"


@pytest.mark.asyncio
async def test_gateway_exception_creates_audit_record_and_is_reraised(orchestrator):
    instance, *_factory_args, gateway, audit = orchestrator
    await instance.connect()
    gateway.execute_tool.side_effect = RuntimeError("gateway exploded")

    with pytest.raises(RuntimeError, match="gateway exploded"):
        await instance.execute_tool("trigger_action", {})

    assert audit.completions[0]["success"] is False
    assert audit.completions[0]["error"] == "RuntimeError"
    assert audit.completions[0]["message"] == "gateway exploded"
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_audit_failure_does_not_change_action_result_or_lifecycle(orchestrator):
    instance, pipeline_factory, session_factory, gateway_factory, _session, gateway, _audit = orchestrator
    instance = RuntimeOrchestrator(
        config_path="test.toml",
        pipeline_factory=pipeline_factory,
        session_factory=session_factory,
        gateway_factory=gateway_factory,
        audit_logger=FailingAudit(),
    )
    await instance.connect()
    gateway.execute_tool.return_value = {"success": True, "ticket_id": "ticket-audit"}

    assert await instance.execute_tool("trigger_action", {}) == {
        "success": True,
        "ticket_id": "ticket-audit",
    }
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_audit_failure_does_not_change_exception_behavior(orchestrator):
    instance, pipeline_factory, session_factory, gateway_factory, _session, gateway, _audit = orchestrator
    instance = RuntimeOrchestrator(
        config_path="test.toml",
        pipeline_factory=pipeline_factory,
        session_factory=session_factory,
        gateway_factory=gateway_factory,
        audit_logger=FailingAudit(),
    )
    await instance.connect()
    gateway.execute_tool.side_effect = RuntimeError("original failure")

    with pytest.raises(RuntimeError, match="original failure"):
        await instance.execute_tool("trigger_action", {})

    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_get_screen_state_does_not_create_action_audit_records(orchestrator):
    instance, *_factory_args, gateway, audit = orchestrator
    await instance.connect()
    gateway.execute_tool.return_value = {"success": True, "generation": 1}

    assert await instance.execute_tool("get_screen_state", {}) == {
        "success": True,
        "generation": 1,
    }
    assert audit.starts == []
    assert audit.completions == []


@pytest.mark.asyncio
async def test_connect_rejects_invalid_lifecycle_state(orchestrator):
    instance, *_ = orchestrator
    await instance.connect()

    with pytest.raises(InvalidRuntimeState):
        await instance.connect()


@pytest.mark.asyncio
async def test_disconnect_clears_runtime_and_is_idempotent(orchestrator):
    instance, _pipeline_factory, _session_factory, _gateway_factory, session, _gateway, _audit = orchestrator
    await instance.connect()

    await instance.disconnect()
    await instance.disconnect()

    session.teardown.assert_awaited_once()
    session.disconnect.assert_awaited_once()
    assert instance.state is LifecycleState.DISCONNECTED
    assert instance.is_connected() is False
    assert instance.runtime_id() is None


@pytest.mark.asyncio
async def test_connect_failure_enters_error_state(orchestrator):
    instance, _pipeline_factory, _session_factory, _gateway_factory, session, _gateway, _audit = orchestrator
    session.connect.side_effect = RuntimeError("connection failed")

    with pytest.raises(RuntimeError, match="connection failed"):
        await instance.connect()

    assert instance.state is LifecycleState.ERROR


@pytest.mark.asyncio
async def test_mutating_action_transitions_through_executing(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()

    gateway.execute_tool.side_effect = [
        {"success": True, "ticket_id": "ticket-1"},
    ]
    result = await instance.execute_tool("trigger_action", {"action_id": "ENTER"})

    assert result == {"success": True, "ticket_id": "ticket-1"}
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_gateway_exception_restores_ready_and_releases_lock(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    gateway.execute_tool.side_effect = [RuntimeError("gateway failed"), {"success": True}]

    with pytest.raises(RuntimeError, match="gateway failed"):
        await instance.execute_tool("trigger_action", {})

    assert instance.state is LifecycleState.READY
    assert await instance.execute_tool("trigger_action", {}) == {"success": True}


@pytest.mark.asyncio
async def test_mutating_actions_are_serialized(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    execution_order = []

    async def execute(name, arguments):
        execution_order.append(f"{arguments['id']}:start")
        await asyncio.sleep(0.01)
        execution_order.append(f"{arguments['id']}:finish")
        return {"success": True, "id": arguments["id"]}

    gateway.execute_tool.side_effect = execute
    results = await asyncio.gather(
        instance.execute_tool("trigger_action", {"id": "A"}),
        instance.execute_tool("trigger_action", {"id": "B"}),
    )

    assert execution_order in (
        ["A:start", "A:finish", "B:start", "B:finish"],
        ["B:start", "B:finish", "A:start", "A:finish"],
    )
    assert [result["success"] for result in results] == [True, True]
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_observation_is_rejected_during_mutating_action(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_name, _arguments):
        started.set()
        await release.wait()
        return {"success": True}

    gateway.execute_tool.side_effect = execute
    action = asyncio.create_task(instance.execute_tool("trigger_action", {}))
    await started.wait()

    with pytest.raises(InvalidRuntimeState, match="not ready"):
        instance.get_observation()

    release.set()
    await action


@pytest.mark.asyncio
async def test_disconnect_waits_for_active_action(orchestrator):
    instance, *_factory_args, session, gateway, _audit = orchestrator
    await instance.connect()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_name, _arguments):
        started.set()
        await release.wait()
        return {"success": True}

    gateway.execute_tool.side_effect = execute
    action = asyncio.create_task(instance.execute_tool("trigger_action", {}))
    await started.wait()
    disconnect = asyncio.create_task(instance.disconnect())
    await asyncio.sleep(0)

    assert not session.disconnect.await_count
    release.set()
    await action
    await disconnect

    session.disconnect.assert_awaited_once()
    assert instance.state is LifecycleState.DISCONNECTED


@pytest.mark.asyncio
async def test_structured_gateway_failure_is_preserved(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    failure = {
        "success": False,
        "error": "ContractValidationError",
        "message": "stale generation",
        "generation_token": "gen_1_hash",
    }
    gateway.execute_tool.return_value = failure

    assert await instance.execute_tool("trigger_action", {}) == failure
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_connecting_and_initializing_reject_actions(orchestrator):
    instance, *_ = orchestrator

    for lifecycle_state in (LifecycleState.CONNECTING, LifecycleState.INITIALIZING):
        instance.state = lifecycle_state
        with pytest.raises(InvalidRuntimeState):
            await instance.execute_tool("trigger_action", {})


@pytest.mark.asyncio
async def test_instances_have_independent_action_locks(orchestrator):
    first, pipeline_factory, session_factory, gateway_factory, _session, first_gateway, _audit = orchestrator
    second_session = MagicMock()
    second_session.connect = AsyncMock()
    second_session.initialize = AsyncMock(return_value=SimpleNamespace(runtime_id="runtime-02"))
    second_session.teardown = AsyncMock()
    second_session.disconnect = AsyncMock()
    second_gateway = MagicMock()
    second_gateway.execute_tool = AsyncMock(return_value={"success": True})
    second_gateway.get_active_payload.return_value.state = SimpleNamespace(runtime_id="runtime-02")
    second = RuntimeOrchestrator(
        config_path="test.toml",
        pipeline_factory=pipeline_factory,
        session_factory=MagicMock(return_value=second_session),
        gateway_factory=MagicMock(return_value=second_gateway),
    )
    await first.connect()
    await second.connect()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_name, _arguments):
        started.set()
        await release.wait()
        return {"success": True}

    first_gateway.execute_tool.side_effect = blocked
    first_action = asyncio.create_task(first.execute_tool("trigger_action", {}))
    await started.wait()

    second_result = await second.execute_tool("trigger_action", {})
    assert second_result == {"success": True}

    release.set()
    await first_action


@pytest.mark.asyncio
async def test_connect_exposes_lifecycle_checkpoints(pipeline):
    driver, reducer, stability, _lowerer, dispatcher, config = pipeline
    orchestrator = None
    checkpoints = []

    class CheckpointSession:
        async def connect(self):
            checkpoints.append(orchestrator.state)

        async def initialize(self):
            checkpoints.append(orchestrator.state)
            return SimpleNamespace(runtime_id=driver.runtime_id)

        async def teardown(self, _active_state):
            pass

        async def disconnect(self):
            pass

    def session_factory(_driver, _reducer, _stability, _config):
        return CheckpointSession()

    def gateway_factory(_payload, _dispatcher):
        checkpoints.append(orchestrator.state)
        return MagicMock()

    orchestrator = RuntimeOrchestrator(
        pipeline_factory=MagicMock(return_value=pipeline),
        session_factory=session_factory,
        gateway_factory=gateway_factory,
        audit_logger=RecordingAudit(),
    )

    await orchestrator.connect()

    assert checkpoints == [
        LifecycleState.CONNECTING,
        LifecycleState.INITIALIZING,
        LifecycleState.INITIALIZING,
    ]
    assert orchestrator.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_invalid_lifecycle_states_reject_mutating_actions(orchestrator):
    instance, *_ = orchestrator

    for lifecycle_state in (
        LifecycleState.DISCONNECTED,
        LifecycleState.CONNECTING,
        LifecycleState.INITIALIZING,
        LifecycleState.ERROR,
        LifecycleState.DISCONNECTING,
    ):
        instance.state = lifecycle_state
        with pytest.raises((RuntimeNotConnected, InvalidRuntimeState)):
            await instance.execute_tool("trigger_action", {})


@pytest.mark.asyncio
async def test_observation_does_not_acquire_mutation_lock(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    gateway.execute_tool.return_value = {"success": True, "generation": 7}

    await instance._action_lock.acquire()
    try:
        assert await instance.execute_tool("get_screen_state", {}) == {
            "success": True,
            "generation": 7,
        }
    finally:
        instance._action_lock.release()


@pytest.mark.asyncio
async def test_mutating_actions_are_deterministically_serialized(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    active_calls = 0
    max_active_calls = 0
    order = []

    async def execute(_name, arguments):
        nonlocal active_calls, max_active_calls
        action_id = arguments["id"]
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        order.append(f"{action_id}:start")
        if action_id == "A":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        order.append(f"{action_id}:finish")
        active_calls -= 1
        return {"success": True, "id": action_id}

    gateway.execute_tool.side_effect = execute
    first = asyncio.create_task(instance.execute_tool("trigger_action", {"id": "A"}))
    await first_started.wait()
    second = asyncio.create_task(instance.execute_tool("trigger_action", {"id": "B"}))
    await asyncio.sleep(0)

    assert not second_started.is_set()
    assert instance.state is LifecycleState.EXECUTING
    release_first.set()
    assert await first == {"success": True, "id": "A"}
    assert await second == {"success": True, "id": "B"}
    assert order == ["A:start", "A:finish", "B:start", "B:finish"]
    assert max_active_calls == 1


@pytest.mark.asyncio
async def test_audit_failure_releases_lock_for_subsequent_action(orchestrator):
    instance, pipeline_factory, session_factory, gateway_factory, _session, gateway, _audit = orchestrator
    instance = RuntimeOrchestrator(
        config_path="test.toml",
        pipeline_factory=pipeline_factory,
        session_factory=session_factory,
        gateway_factory=gateway_factory,
        audit_logger=FailingAudit(),
    )
    await instance.connect()
    gateway.execute_tool.side_effect = [
        {"success": True, "ticket_id": "first"},
        {"success": True, "ticket_id": "second"},
    ]

    assert await instance.execute_tool("trigger_action", {}) == {
        "success": True,
        "ticket_id": "first",
    }
    assert await instance.execute_tool("trigger_action", {}) == {
        "success": True,
        "ticket_id": "second",
    }
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_recreates_runtime(orchestrator):
    instance, pipeline_factory, _session_factory, _gateway_factory, session, _gateway, _audit = orchestrator

    await instance.connect()
    await instance.disconnect()
    await instance.connect()

    assert pipeline_factory.call_count == 2
    assert session.connect.await_count == 2
    assert session.disconnect.await_count == 1
    assert instance.state is LifecycleState.READY


@pytest.mark.asyncio
async def test_observation_payload_is_delegated_without_l4_reconstruction(orchestrator):
    instance, *_factory_args, gateway, _audit = orchestrator
    await instance.connect()
    observation = {
        "success": True,
        "generation": 4,
        "generation_token": "gen_4_abc12345",
        "screen_text": ["READY"],
        "fields": {"field": {"editable": True}},
        "changed_fields": {"field": {"old": "A", "new": "B"}},
        "available_tools": [{"type": "function"}],
        "is_stable": True,
        "ticket_id": "ticket-observation-source",
        "execution_time_ms": 8.0,
    }
    gateway.get_observation.return_value.to_dict.return_value = observation

    assert instance.get_observation() == observation


@pytest.mark.asyncio
async def test_runtime_agnostic_session_implementations_share_orchestrator_contract(pipeline):
    class RuntimeASession:
        def __init__(self, driver):
            self.driver = driver

        async def connect(self):
            self.connected = True

        async def initialize(self):
            return SimpleNamespace(runtime_id=self.driver.runtime_id)

        async def teardown(self, _active_state):
            pass

        async def disconnect(self):
            self.connected = False

    class RuntimeBSession(RuntimeASession):
        pass

    def build(session_type, runtime_id):
        driver = SimpleNamespace(runtime_id=runtime_id)
        local_pipeline = (driver, object(), object(), object(), object(), {})
        gateway = MagicMock()
        gateway.execute_tool = AsyncMock(return_value={"success": True, "runtime": runtime_id})
        gateway.get_active_payload.return_value.state = SimpleNamespace(runtime_id=runtime_id)
        return RuntimeOrchestrator(
            pipeline_factory=MagicMock(return_value=local_pipeline),
            session_factory=lambda driver, _reducer, _stability, _config: session_type(driver),
            gateway_factory=MagicMock(return_value=gateway),
            audit_logger=RecordingAudit(),
        )

    runtime_a = build(RuntimeASession, "runtime-a")
    runtime_b = build(RuntimeBSession, "runtime-b")
    await runtime_a.connect()
    await runtime_b.connect()

    assert await runtime_a.execute_tool("trigger_action", {}) == {
        "success": True,
        "runtime": "runtime-a",
    }
    assert await runtime_b.execute_tool("trigger_action", {}) == {
        "success": True,
        "runtime": "runtime-b",
    }
