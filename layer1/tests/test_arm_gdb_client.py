"""Unit coverage for tokenized GDB/MI behavior and the ARM L1 driver."""

import asyncio
import hashlib
import json
import os
import stat
from unittest.mock import AsyncMock, Mock

import pytest

from layer1.arm.driver import ARMDriver
from layer1.arm.gdb_client import (
    GDBBreakpoint,
    GDBMIClient,
    GDBMIError,
    validate_gdb_executable,
    validate_gdb_host,
    validate_gdb_port,
)
from layer1.arm.observation import ARMObservationConfig, MemoryDecodeConfig, MemoryRequest


class _FakeStreamReader:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class _FakeStreamWriter:
    def __init__(self) -> None:
        self.commands: asyncio.Queue[str] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.commands.put_nowait(data.decode().strip())

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStreamReader()
        self.stdin = _FakeStreamWriter()
        self.stderr = None
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_mi_command_waits_for_matching_token_and_accepts_running():
    client = GDBMIClient(command_timeout=1)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())

    pending = asyncio.create_task(client.command("-exec-continue"))
    sent = await process.stdin.commands.get()
    assert sent == "1-exec-continue"
    await process.stdout.lines.put(b"*running,thread-id=all\n")
    await process.stdout.lines.put(b"1^running\n")
    assert await pending == ("running", "")
    await process.stdout.lines.put(b"*stopped,reason=breakpoint-hit\n")
    await asyncio.sleep(0)
    assert client.last_async_records[-1].startswith("*stopped")
    await client.close()


@pytest.mark.asyncio
async def test_mi_concurrent_commands_are_correlated_by_token_not_response_order():
    client = GDBMIClient(command_timeout=1)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())

    first = asyncio.create_task(client.command("-thread-info"))
    second = asyncio.create_task(client.command("-data-list-register-names"))
    first_command = await process.stdin.commands.get()
    second_command = await process.stdin.commands.get()
    first_token = int(first_command.split("-", 1)[0])
    second_token = int(second_command.split("-", 1)[0])
    await process.stdout.lines.put(f'{second_token}^done,register-names=[]\n'.encode())
    await process.stdout.lines.put(f'{first_token}^done,threads=[]\n'.encode())

    assert await first == ("done", "threads=[]")
    assert await second == ("done", "register-names=[]")
    await client.close()


@pytest.mark.asyncio
async def test_compound_transaction_blocks_other_tasks_but_allows_nested_commands():
    client = GDBMIClient(command_timeout=1)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())
    holding_transaction = asyncio.Event()
    release_transaction = asyncio.Event()

    async def compound_operation() -> None:
        async with client.transaction():
            await client.command("-first")
            holding_transaction.set()
            await release_transaction.wait()

    compound = asyncio.create_task(compound_operation())
    first = await process.stdin.commands.get()
    await process.stdout.lines.put(f'{first.split("-", 1)[0]}^done\n'.encode())
    await holding_transaction.wait()

    outside = asyncio.create_task(client.command("-outside"))
    await asyncio.sleep(0)
    assert process.stdin.commands.empty()

    release_transaction.set()
    await compound
    second = await process.stdin.commands.get()
    assert second.endswith("-outside")
    await process.stdout.lines.put(f'{second.split("-", 1)[0]}^done\n'.encode())
    assert await outside == ("done", "")
    await client.close()


@pytest.mark.asyncio
async def test_mi_timeout_removes_pending_command():
    client = GDBMIClient(command_timeout=0.01)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())

    with pytest.raises(asyncio.TimeoutError):
        await client.command("-thread-info")

    assert client._pending == {}
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_breakpoint_insertion_resolves_id_and_deletes_before_releasing_lock():
    driver = ARMDriver("firmware.elf", command_timeout=1)
    process = _FakeProcess()
    driver.client.process = process  # type: ignore[assignment]
    driver.client._reader_task = asyncio.create_task(driver.client._read_responses())
    driver._connected = True
    driver.client.read_execution_state = AsyncMock(return_value="stopped")  # type: ignore[method-assign]
    task = asyncio.create_task(driver.run_to_breakpoint("*0x1000"))
    insert_command = await process.stdin.commands.get()
    task.cancel()
    token = insert_command.split("-", 1)[0]
    await process.stdout.lines.put(f'{token}^done,bkpt={{number="7",addr="0x1000"}}\n'.encode())
    delete_command = await asyncio.wait_for(process.stdin.commands.get(), 1)
    assert delete_command.endswith("-break-delete 7")
    delete_token = delete_command.split("-", 1)[0]
    await process.stdout.lines.put(f"{delete_token}^done\n".encode())
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1):
        async with driver.execution_transaction():
            pass
    await driver.client.close()


@pytest.mark.asyncio
async def test_cancelled_breakpoint_insertion_without_mi_result_reports_ambiguity():
    driver = ARMDriver("firmware.elf", command_timeout=0.02)
    process = _FakeProcess()
    driver.client.process = process  # type: ignore[assignment]
    driver.client._reader_task = asyncio.create_task(driver.client._read_responses())
    driver._connected = True
    driver.client.read_execution_state = AsyncMock(return_value="stopped")  # type: ignore[method-assign]
    task = asyncio.create_task(driver.run_to_breakpoint("*0x1000"))
    await process.stdin.commands.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 1)
    assert getattr(caught.value, "arm_breakpoint_insertion_ambiguous", False)
    assert any("insertion outcome is ambiguous" in note for note in caught.value.__notes__)
    assert driver.client._pending == {}
    async with asyncio.timeout(1):
        async with driver.execution_transaction():
            pass
    await driver.client.close()


@pytest.mark.asyncio
async def test_cancellation_during_interrupt_still_attempts_stop_wait_and_delete():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.client.read_execution_state = AsyncMock(return_value="stopped")  # type: ignore[method-assign]
    driver.client.insert_breakpoint_with_address = AsyncMock(return_value=GDBBreakpoint("4", 0x1000))  # type: ignore[method-assign]
    driver.client.continue_execution = AsyncMock(side_effect=RuntimeError("continue failed"))  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    interrupt_started = asyncio.Event()

    async def blocked_interrupt(_command: str) -> tuple[str, str]:
        interrupt_started.set()
        await asyncio.Event().wait()
        return "done", ""

    driver.client.command = blocked_interrupt  # type: ignore[method-assign]
    driver.client.wait_for_stop = AsyncMock(return_value="*stopped")  # type: ignore[method-assign]
    driver.delete_breakpoint = AsyncMock()  # type: ignore[method-assign]
    task = asyncio.create_task(driver.run_to_breakpoint("*0x1000"))
    await interrupt_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    driver.client.wait_for_stop.assert_awaited_once()
    driver.delete_breakpoint.assert_awaited_once_with("4")
    assert "continue failed" in str(caught.value.arm_primary_error)
    assert caught.value.arm_cleanup_errors == ("target interrupt was cancelled",)
    async with asyncio.timeout(1):
        async with driver.execution_transaction():
            pass


@pytest.mark.asyncio
async def test_cancellation_during_breakpoint_deletion_is_reported_and_releases_lock():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.client.read_execution_state = AsyncMock(return_value="stopped")  # type: ignore[method-assign]
    driver.client.insert_breakpoint_with_address = AsyncMock(return_value=GDBBreakpoint("5", 0x1000))  # type: ignore[method-assign]
    driver.client.continue_execution = AsyncMock()  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    driver.client.wait_for_stop = AsyncMock(return_value="*stopped")  # type: ignore[method-assign]
    driver.client.latest_stop_details = Mock(return_value={"reason": "breakpoint-hit", "bkptno": "5"})
    driver.read_pc = AsyncMock(return_value="0x1000")  # type: ignore[method-assign]
    deletion_started = asyncio.Event()

    async def blocked_delete(_breakpoint_id: str) -> None:
        deletion_started.set()
        await asyncio.Event().wait()

    driver.client.delete_breakpoint = blocked_delete  # type: ignore[method-assign]
    task = asyncio.create_task(driver.run_to_breakpoint("*0x1000"))
    await deletion_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.arm_cleanup_errors == ("delete breakpoint outcome is ambiguous after cancellation",)
    async with asyncio.timeout(1):
        async with driver.execution_transaction():
            pass


@pytest.mark.asyncio
async def test_breakpoint_locations_and_ids_are_validated_and_deletion_is_targeted():
    client = GDBMIClient()
    client.command = AsyncMock(return_value=("done", 'bkpt={number="7",addr="0x1000"}'))  # type: ignore[method-assign]

    assert await client.insert_breakpoint("*0x1000") == "7"
    await client.delete_breakpoint("7")
    assert client.command.await_args_list[0].args == ("-break-insert *0x1000",)
    assert client.command.await_args_list[1].args == ("-break-delete 7",)

    with pytest.raises(ValueError, match="breakpoint location"):
        await client.insert_breakpoint("*0x1000; -exec-continue")
    with pytest.raises(ValueError, match="breakpoint ID"):
        await client.delete_breakpoint("7; -break-delete")


@pytest.mark.asyncio
async def test_breakpoint_insertion_retains_gdb_resolved_address():
    client = GDBMIClient()
    client.command = AsyncMock(return_value=("done", 'bkpt={number="7",addr="0x40000016",func="main"}'))  # type: ignore[method-assign]

    assert await client.insert_breakpoint_with_address("main") == GDBBreakpoint("7", 0x40000016)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('bkpt={number="7",func="main"}', "without a resolved address"),
        ('bkpt={number="7",addr="<MULTIPLE>"}', "malformed resolved address"),
    ],
)
async def test_breakpoint_insertion_rejects_missing_or_malformed_resolved_address(payload, message):
    client = GDBMIClient()
    client.command = AsyncMock(return_value=("done", payload))  # type: ignore[method-assign]

    with pytest.raises(GDBMIError, match=message) as caught:
        await client.insert_breakpoint_with_address("main")
    assert caught.value.arm_breakpoint_id == "7"


@pytest.mark.asyncio
async def test_execution_state_uses_latest_stopped_async_record_when_thread_info_has_no_thread():
    client = GDBMIClient()
    client.command = AsyncMock(return_value=("done", "threads=[]"))  # type: ignore[method-assign]
    client.last_execution_record = '*stopped,reason="breakpoint-hit",bkptno="1"'
    client.last_stop_record = client.last_execution_record

    assert await client.read_execution_state() == "stopped"
    assert client.last_execution_state_source == "GDB/MI *stopped async record"
    assert client.latest_stop_details() == {"reason": "breakpoint-hit", "bkptno": "1"}


@pytest.mark.asyncio
async def test_execution_state_uses_running_async_record_and_does_not_use_stale_stop():
    client = GDBMIClient()
    client.command = AsyncMock(return_value=("done", "threads=[]"))  # type: ignore[method-assign]
    client.last_execution_record = "*running,thread-id=all"
    client.last_stop_record = None

    assert await client.read_execution_state() == "running"
    assert client.last_execution_state_source == "GDB/MI *running async record"
    assert client.latest_stop_details() is None


@pytest.mark.asyncio
async def test_mi_memory_and_register_values_are_machine_readable():
    client = GDBMIClient(command_timeout=1)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())

    memory_task = asyncio.create_task(client.read_memory(0x40000054, 4))
    command = await process.stdin.commands.get()
    assert command.endswith("-data-read-memory-bytes 0x40000054 4")
    await process.stdout.lines.put(b'1^done,memory=[{begin="0x40000054",contents="0000002a"}]\n')
    assert await memory_task == bytes.fromhex("0000002a")

    registers_task = asyncio.create_task(client.read_registers())
    command = await process.stdin.commands.get()
    token = command.split("-", 1)[0]
    await process.stdout.lines.put(f'{token}^done,register-values=[{{number="0",value="0x2a"}}]\n'.encode())
    assert await registers_task == [{"number": 0, "value": "0x2a"}]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "size", "message"),
    [
        (-1, 4, "32-bit ARM address"),
        (0xFFFFFFFF, 2, "address range"),
        ("0x10; -exec-continue", 4, "numeric 32-bit ARM address"),
        (0x1000, 4097, "cannot exceed"),
        (0x1000, 0, "positive integer"),
    ],
)
async def test_memory_read_rejects_malformed_or_oversized_requests_before_mi(address, size, message):
    client = GDBMIClient()
    client.command = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match=message):
        await client.read_memory(address, size)

    client.command.assert_not_awaited()


def test_memory_request_rejects_oversize_address_range_and_expression_symbols():
    with pytest.raises(ValueError, match="cannot exceed"):
        MemoryRequest("large", 4097, address=0x1000)
    with pytest.raises(ValueError, match="address range"):
        MemoryRequest("wrap", 4, address=0xFFFFFFFF)
    with pytest.raises(ValueError, match="symbol identifier"):
        MemoryRequest("unsafe", 4, symbol="counter;quit")


@pytest.mark.asyncio
async def test_arm_frame_is_json_observation_and_raw_write_is_unsupported():
    driver = ARMDriver(
        "firmware.elf",
        observation_config=ARMObservationConfig(
            include_registers=False,
            include_pc=False,
            include_sp=False,
            status_registers=(),
            include_execution=False,
            include_current_instruction=False,
            include_breakpoints=False,
        ),
    )
    driver._connected = True
    frame = await driver.read_frame()
    observation = json.loads(frame.raw_payload)
    assert observation["environment"] == "arm"
    assert observation["schema_version"] == "2.0"
    assert observation["identity"]["runtime_id"] == driver.runtime_id
    assert observation["metadata"]["frame_seq"] == frame.frame_seq == 1
    assert observation["cpu"]["pc"]["status"] == "not_requested"
    assert observation["cpu"]["registers"]["status"] == "not_requested"
    assert frame.frame_seq == 1
    with pytest.raises(NotImplementedError, match="generic raw byte writes"):
        await driver.write_raw(b"x")


@pytest.mark.asyncio
async def test_arm_capture_collects_selected_raw_memory_and_execution_evidence():
    driver = ARMDriver(
        "firmware.elf",
        observation_config=ARMObservationConfig(
            memory=(MemoryRequest("counter", size=4, symbol="counter", decode=MemoryDecodeConfig("uint32", "little")),)
        ),
    )
    driver._connected = True
    driver.read_registers = AsyncMock(return_value=[{"number": 0, "value": "0x2a"}])  # type: ignore[method-assign]
    driver.read_pc = AsyncMock(return_value="0x4000002c")  # type: ignore[method-assign]
    driver.client.read_register_names = AsyncMock(return_value=["r0"])
    driver.client.evaluate = AsyncMock(side_effect=lambda expr: {
        "$sp": "0x40001000", "$cpsr": "0x600001d3", "&counter": "0x40000054 <counter>"
    }[expr])
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.last_stop_record = '*stopped,reason="breakpoint-hit",bkptno="1"'
    driver.client.disassemble_instruction = AsyncMock(
        return_value={"address": 0x4000002C, "text": "str r2, [r3, #0]", "raw": "structured-mi"}
    )
    driver.client.read_breakpoints = AsyncMock(
        return_value=[{"id": "1", "enabled": True, "address": "0x4000002c", "location": "*0x4000002c"}]
    )
    driver.read_memory = AsyncMock(return_value=bytes.fromhex("ba040900"))  # type: ignore[method-assign]

    frame = await driver.read_frame()
    observation = json.loads(frame.raw_payload)

    assert observation["cpu"]["registers"]["values"][0] == {
        "number": 0, "name": "r0", "status": "observed", "raw_value": "0x2a", "value": 42
    }
    assert observation["execution"]["state"]["value"] == "stopped"
    assert observation["execution"]["stop_reason"]["value"] == "breakpoint-hit"
    assert observation["execution"]["current_instruction"]["text"] == "str r2, [r3, #0]"
    assert observation["memory"]["regions"][0]["raw_bytes_hex"] == "ba040900"
    assert "value" not in observation["memory"]["regions"][0]


@pytest.mark.asyncio
async def test_firmware_fingerprint_identifies_snapshot_bytes_loaded_after_source_replacement(tmp_path):
    from pathlib import Path

    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"initialized firmware")
    driver = ARMDriver(str(firmware))
    replacement = b"replacement firmware bytes"
    firmware.write_bytes(replacement)
    driver.client.start = AsyncMock()  # type: ignore[method-assign]
    driver.client.connect_remote = AsyncMock()  # type: ignore[method-assign]
    loaded: list[tuple[str, bytes]] = []

    async def load(path: str) -> None:
        from pathlib import Path

        loaded.append((path, Path(path).read_bytes()))

    driver.client.load_firmware = load  # type: ignore[method-assign]
    await driver.connect()
    assert loaded[0][0] != str(firmware)
    assert loaded[0][1] == replacement
    assert driver._firmware_sha256 == hashlib.sha256(loaded[0][1]).hexdigest()
    assert driver._firmware_sha256 != hashlib.sha256(b"initialized firmware").hexdigest()
    await driver.disconnect()


@pytest.mark.asyncio
async def test_firmware_path_replacement_before_connect_loads_and_hashes_replacement(tmp_path):
    from pathlib import Path

    firmware = tmp_path / "firmware.elf"
    replacement = tmp_path / "replacement.elf"
    firmware.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    driver = ARMDriver(str(firmware))
    os.replace(replacement, firmware)
    driver.client.start = AsyncMock()  # type: ignore[method-assign]
    driver.client.connect_remote = AsyncMock()  # type: ignore[method-assign]
    loaded: list[bytes] = []

    async def load(path: str) -> None:
        loaded.append(Path(path).read_bytes())

    driver.client.load_firmware = load  # type: ignore[method-assign]
    await driver.connect()
    assert loaded == [b"replacement"]
    assert driver._firmware_sha256 == hashlib.sha256(b"replacement").hexdigest()
    await driver.disconnect()


@pytest.mark.asyncio
async def test_firmware_snapshot_mutation_during_load_fails_closed(tmp_path):
    from pathlib import Path

    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"firmware bytes")
    driver = ARMDriver(str(firmware))
    driver.client.start = AsyncMock()  # type: ignore[method-assign]
    driver.client.connect_remote = AsyncMock()  # type: ignore[method-assign]
    closed = AsyncMock()
    driver.client.close = closed  # type: ignore[method-assign]

    async def mutate_snapshot(path: str) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        with open(path, "wb") as snapshot:
            snapshot.write(b"different bytes")

    driver.client.load_firmware = mutate_snapshot  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="snapshot changed"):
        await driver.connect()
    assert driver._firmware_sha256 is None
    assert driver._connected is False
    closed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("gdb_value", ["0x40000054 <counter>", "0x40000054"])
async def test_resolve_symbol_parses_address_with_or_without_symbol_name(gdb_value: str):
    driver = ARMDriver("firmware.elf")
    driver.client.evaluate = AsyncMock(return_value=gdb_value)  # type: ignore[method-assign]

    assert await driver.resolve_symbol("counter") == 0x40000054


@pytest.mark.asyncio
async def test_resolve_symbol_rejects_non_address_response():
    driver = ARMDriver("firmware.elf")
    driver.client.evaluate = AsyncMock(return_value="not an address")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="invalid address for symbol 'counter'"):
        await driver.resolve_symbol("counter")


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["counter;quit", "$pc", "counter[0]", " main"])
async def test_resolve_symbol_rejects_expressions_before_sending_to_gdb(symbol: str):
    driver = ARMDriver("firmware.elf")
    driver.client.evaluate = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="symbol identifier"):
        await driver.resolve_symbol(symbol)

    driver.client.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_disassemble_range_parses_structured_mi_assembly_output():
    client = GDBMIClient()
    client.command = AsyncMock(return_value=(
        "done",
        'asm_insns=[{address="0x40000028",func-name="main",offset="24",inst="movt\\tr3, #16384\\t@ 0x4000"},'
        '{address="0x4000002c",func-name="main",offset="28",inst="str\\tr2, [r3, #0]"}]',
    ))  # type: ignore[method-assign]

    result = await client.disassemble_range(0x40000010, 0x40000060)

    assert 'address="0x40000028"' in result
    assert 'inst="str\\tr2, [r3, #0]"' in result
    client.command.assert_awaited_once_with("-data-disassemble -s 0x40000010 -e 0x40000060 -- 0")


@pytest.mark.asyncio
async def test_find_store_instruction_uses_mi_disassembly_for_counter_increment():
    driver = ARMDriver("firmware.elf")
    driver.client.evaluate = AsyncMock(return_value="0x40000010 <main>")  # type: ignore[method-assign]
    driver.client.disassemble_range = AsyncMock(return_value=(
        '{address="0x40000010",func-name="main",offset="0",inst="str.w\\tr7, [sp, #-4]!"},'
        '{address="0x40000016",func-name="main",offset="6",inst="movw\\tr3, #84\\t@ 0x54"},'
        '{address="0x4000001a",func-name="main",offset="10",inst="movt\\tr3, #16384\\t@ 0x4000"},'
        '{address="0x4000001e",func-name="main",offset="14",inst="ldr\\tr3, [r3, #0]"},'
        '{address="0x40000020",func-name="main",offset="16",inst="add.w\\tr2, r3, #1"},'
        '{address="0x40000024",func-name="main",offset="20",inst="movw\\tr3, #84\\t@ 0x54"},'
        '{address="0x40000028",func-name="main",offset="24",inst="movt\\tr3, #16384\\t@ 0x4000"},'
        '{address="0x4000002c",func-name="main",offset="28",inst="str\\tr2, [r3, #0]"},'
        '{address="0x40000044",func-name="main",offset="52",inst="movw\\tr3, #84\\t@ 0x54"},'
        '{address="0x40000048",func-name="main",offset="56",inst="movt\\tr3, #16384\\t@ 0x4000"},'
        '{address="0x4000004c",func-name="main",offset="60",inst="mov.w\\tr2, #0"},'
        '{address="0x40000050",func-name="main",offset="64",inst="str\\tr2, [r3, #0]"}'
    ))  # type: ignore[method-assign]

    address, target_address, source_register = await driver._find_store_instruction_details(0x40000054, "main")

    assert address == 0x4000002C
    assert target_address == 0x40000054
    assert source_register == "r2"
    driver.client.evaluate.assert_awaited_once_with("&main")
    driver.client.disassemble_range.assert_awaited_once_with(0x40000010, 0x40000210)


@pytest.mark.asyncio
async def test_find_store_instruction_ignores_unrelated_store_base_register():
    driver = ARMDriver("firmware.elf")
    driver.resolve_symbol = AsyncMock(return_value=0x40000010)  # type: ignore[method-assign]
    driver.client.disassemble_range = AsyncMock(return_value=(
        '{address="0x40000016",func-name="main",inst="movw\\tr3, #84"},'
        '{address="0x4000001a",func-name="main",inst="movt\\tr3, #16384"},'
        '{address="0x4000001e",func-name="main",inst="ldr\\tr3, [r3, #0]"},'
        '{address="0x40000020",func-name="main",inst="add.w\\tr2, r3, #1"},'
        '{address="0x40000024",func-name="main",inst="str\\tr2, [r4, #0]"}'
    ))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="No increment store for counter 0x40000054"):
        await driver.find_store_instruction(0x40000054)


@pytest.mark.asyncio
async def test_find_store_instruction_rejects_ambiguous_matching_stores():
    driver = ARMDriver("firmware.elf")
    driver.client.evaluate = AsyncMock(side_effect=["0x1000 <main>", "0x1001 <main>"])  # type: ignore[method-assign]
    driver.client.disassemble_range = AsyncMock(return_value=(
        '{address="0x1000",func-name="main",inst="movw\\tr3, #84"},'
        '{address="0x1004",func-name="main",inst="movt\\tr3, #16384"},'
        '{address="0x1008",func-name="main",inst="ldr\\tr3, [r3, #0]"},'
        '{address="0x100c",func-name="main",inst="add\\tr2, r3, #1"},'
        '{address="0x1010",func-name="main",inst="str\\tr2, [r3, #0]"},'
        '{address="0x1014",func-name="main",inst="str\\tr2, [r3, #0]"}'
    ))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Ambiguous increment stores.*0x1010, 0x1014"):
        await driver.find_store_instruction(0x40000054)


def _prepare_increment_driver(
    *,
    breakpoint_stop: dict[str, str] | None = None,
    step_stop: dict[str, str] | None = None,
    program_counters: tuple[str, ...] = ("0x4000002c", "0x40000030"),
    counter_values: tuple[str, ...] = ("591034", "591035"),
) -> ARMDriver:
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver._frozen = True
    driver.resolve_symbol = AsyncMock(return_value=0x40000054)  # type: ignore[method-assign]
    driver.find_store_instruction = AsyncMock(return_value=0x4000002C)  # type: ignore[method-assign]
    driver.read_pc = AsyncMock(side_effect=program_counters)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.evaluate = AsyncMock(side_effect=counter_values)
    driver.client.insert_breakpoint = AsyncMock(return_value="7")
    driver.client.delete_breakpoint = AsyncMock()
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.step_instruction = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.command = AsyncMock()
    driver.client.latest_stop_details = Mock(side_effect=[
        breakpoint_stop or {"reason": "breakpoint-hit", "bkptno": "7"},
        step_stop or {"reason": "end-stepping-range"},
    ])
    driver.client.disassemble_instruction = AsyncMock(return_value={
        "address": 0x4000002C, "text": "str r2, [r3, #0]", "raw": "structured-mi"
    })
    return driver


@pytest.mark.asyncio
async def test_increment_counter_verifies_stop_step_and_memory_then_deletes_breakpoint():
    driver = _prepare_increment_driver()

    await driver.increment_counter("counter")

    driver.client.insert_breakpoint.assert_awaited_once_with("*0x4000002c")
    driver.client.step_instruction.assert_awaited_once()
    driver.client.delete_breakpoint.assert_awaited_once_with("7")
    assert driver.client.evaluate.await_args_list[0].args[0] == "*(unsigned int*)0x40000054"
    assert driver.client.evaluate.await_args_list[1].args[0] == "*(unsigned int*)0x40000054"


@pytest.mark.asyncio
async def test_run_to_breakpoint_verifies_and_removes_setup_breakpoint():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.resolve_symbol = AsyncMock(return_value=0x1000)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(return_value=GDBBreakpoint("3", 0x1000))
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value={"reason": "breakpoint-hit", "bkptno": "3", "addr": "0x1000"})
    driver.read_pc = AsyncMock(return_value="0x1000")  # type: ignore[method-assign]
    driver.client.delete_breakpoint = AsyncMock()

    await driver.run_to_breakpoint("main")

    driver.client.insert_breakpoint_with_address.assert_awaited_once_with("main")
    driver.client.delete_breakpoint.assert_awaited_once_with("3")


@pytest.mark.asyncio
async def test_symbol_breakpoint_verifies_against_gdb_resolved_address_not_symbol_entry():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.resolve_symbol = AsyncMock(return_value=0x40000010)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(
        return_value=GDBBreakpoint("7", 0x40000016)
    )  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value={
        "reason": "breakpoint-hit", "bkptno": "7", "addr": "0x40000016",
    })
    driver.read_pc = AsyncMock(return_value="0x40000016")  # type: ignore[method-assign]
    driver.client.delete_breakpoint = AsyncMock()

    await driver.run_to_breakpoint("main")

    driver.client.insert_breakpoint_with_address.assert_awaited_once_with("main")
    driver.read_pc.assert_awaited_once()
    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "message"),
    [
        ({"reason": "breakpoint-hit", "addr": "0x1016"}, "did not identify the expected temporary breakpoint"),
        ({"reason": "breakpoint-hit", "bkptno": "8", "addr": "0x1016"}, "stopped at breakpoint 8"),
    ],
)
async def test_run_to_breakpoint_requires_matching_breakpoint_number(stop, message):
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.resolve_symbol = AsyncMock(return_value=0x1000)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(
        return_value=GDBBreakpoint("7", 0x1016)
    )  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value=stop)
    driver.read_pc = AsyncMock(return_value="0x1016")  # type: ignore[method-assign]
    driver.client.delete_breakpoint = AsyncMock()

    with pytest.raises(RuntimeError, match=message):
        await driver.run_to_breakpoint("main")

    driver.read_pc.assert_not_awaited()
    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_numeric_breakpoint_refuses_wrong_stopped_pc_and_cleans_up():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(
        return_value=GDBBreakpoint("7", 0x1000)
    )  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value={
        "reason": "breakpoint-hit", "bkptno": "7", "addr": "0x1000",
    })
    driver.read_pc = AsyncMock(return_value="0x1004")  # type: ignore[method-assign]
    driver.client.delete_breakpoint = AsyncMock()

    with pytest.raises(RuntimeError, match="target stopped at PC 0x1004, expected breakpoint 0x1000"):
        await driver.run_to_breakpoint("*0x1000")
    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_run_to_breakpoint_rejects_stop_frame_address_disagreement():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.resolve_symbol = AsyncMock(return_value=0x1000)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(
        return_value=GDBBreakpoint("7", 0x1016)
    )  # type: ignore[method-assign]
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value={
        "reason": "breakpoint-hit", "bkptno": "7", "addr": "0x1014",
    })
    driver.read_pc = AsyncMock(return_value="0x1016")  # type: ignore[method-assign]
    driver.client.delete_breakpoint = AsyncMock()

    with pytest.raises(RuntimeError, match="stopped frame address 0x1014 disagrees with resolved breakpoint address 0x1016"):
        await driver.run_to_breakpoint("main")
    driver.read_pc.assert_not_awaited()
    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_run_to_breakpoint_cleans_up_when_inserted_address_is_missing_or_malformed():
    for payload in ('bkpt={number="3"}', 'bkpt={number="3",addr="<MULTIPLE>"}'):
        driver = ARMDriver("firmware.elf")
        driver._connected = True
        driver.resolve_symbol = AsyncMock(return_value=0x1000)  # type: ignore[method-assign]
        driver.client.read_execution_state = AsyncMock(return_value="stopped")
        driver.client.continue_execution = AsyncMock()  # type: ignore[method-assign]
        driver.client.command = AsyncMock(side_effect=[("done", payload), ("done", "")])  # type: ignore[method-assign]

        with pytest.raises(GDBMIError, match="resolved address"):
            await driver.run_to_breakpoint("main")

        driver.client.command.assert_any_await("-break-delete 3")
        driver.client.continue_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_to_breakpoint_cleans_up_after_unrelated_stop():
    driver = ARMDriver("firmware.elf")
    driver._connected = True
    driver.resolve_symbol = AsyncMock(return_value=0x1000)  # type: ignore[method-assign]
    driver.client.read_execution_state = AsyncMock(return_value="stopped")
    driver.client.insert_breakpoint_with_address = AsyncMock(return_value=GDBBreakpoint("3", 0x1000))
    driver.client.clear_execution_evidence = Mock()
    driver.client.continue_execution = AsyncMock()
    driver.client.wait_for_stop = AsyncMock()
    driver.client.latest_stop_details = Mock(return_value={"reason": "signal-received"})
    driver.client.delete_breakpoint = AsyncMock()

    with pytest.raises(RuntimeError, match="was not the stop reason"):
        await driver.run_to_breakpoint("main")

    driver.read_pc = AsyncMock()  # type: ignore[method-assign]
    driver.client.delete_breakpoint.assert_awaited_once_with("3")
    driver.read_pc.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "pcs", "message"),
    [
        ({"reason": "breakpoint-hit", "bkptno": "8"}, ("0x4000002c", "0x40000030"), "expected temporary breakpoint"),
        ({"reason": "signal-received", "bkptno": "7"}, ("0x4000002c", "0x40000030"), "not the stop reason"),
        ({"reason": "breakpoint-hit", "bkptno": "7"}, ("0x40000028", "0x40000030"), "expected counter store"),
    ],
)
async def test_increment_counter_refuses_unrelated_stop_and_cleans_breakpoint(stop, pcs, message):
    driver = _prepare_increment_driver(breakpoint_stop=stop, program_counters=pcs)

    with pytest.raises(RuntimeError, match=message):
        await driver.increment_counter("counter")

    driver.client.step_instruction.assert_not_awaited()
    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_increment_counter_rejects_failed_step_postcondition_and_cleans_breakpoint():
    driver = _prepare_increment_driver(step_stop={"reason": "signal-received"})

    with pytest.raises(RuntimeError, match="single-step did not complete normally"):
        await driver.increment_counter("counter")

    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_increment_counter_attempts_interrupt_and_cleanup_after_continue_error():
    driver = _prepare_increment_driver()
    driver.client.continue_execution.side_effect = GDBMIError("continue failed")

    with pytest.raises(GDBMIError, match="continue failed"):
        await driver.increment_counter("counter")

    driver.client.command.assert_awaited_once_with("-exec-interrupt")
    driver.client.delete_breakpoint.assert_awaited_once_with("7")
    driver.client.step_instruction.assert_not_awaited()


@pytest.mark.asyncio
async def test_increment_counter_rejects_unchanged_value_and_cleans_breakpoint():
    driver = _prepare_increment_driver(counter_values=("591034", "591034"))

    with pytest.raises(RuntimeError, match="counter store postcondition failed"):
        await driver.increment_counter("counter")

    driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_increment_counter_reports_cleanup_failure_without_hiding_success():
    driver = _prepare_increment_driver()
    driver.client.delete_breakpoint.side_effect = GDBMIError("delete failed")

    with pytest.raises(RuntimeError, match="breakpoint cleanup failed"):
        await driver.increment_counter("counter")


@pytest.mark.asyncio
async def test_mi_error_is_reported():
    client = GDBMIClient(command_timeout=1)
    process = _FakeProcess()
    client.process = process  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_responses())
    pending = asyncio.create_task(client.command("-file-exec-and-symbols missing.elf"))
    await process.stdin.commands.get()
    await process.stdout.lines.put(b'1^error,msg="No such file"\n')
    with pytest.raises(GDBMIError, match="No such file"):
        await pending
    await client.close()


@pytest.mark.parametrize("host", ["", "bad host", "host\n-exec-continue", "host\r", "host\x00"])
def test_gdb_host_validation_rejects_control_or_malformed_values(host):
    with pytest.raises(ValueError):
        validate_gdb_host(host)


@pytest.mark.parametrize("port", [0, 65536, True, "1234", 12.5])
def test_gdb_port_validation_rejects_out_of_range_or_non_integer_values(port):
    with pytest.raises(ValueError):
        validate_gdb_port(port)


@pytest.mark.parametrize("executable", ["", "gdb\n-exec-continue", "gdb\x00"])
def test_gdb_executable_validation_rejects_empty_or_control_values(executable):
    with pytest.raises(ValueError):
        validate_gdb_executable(executable)


@pytest.mark.asyncio
async def test_connect_remote_validates_values_before_sending_mi_command():
    client = GDBMIClient(command_timeout=1)
    client.command = AsyncMock()  # type: ignore[method-assign]
    for host, port in [("host\n-exec-continue", 1234), ("127.0.0.1", 0), ("127.0.0.1", True)]:
        with pytest.raises(ValueError):
            await client.connect_remote(host, port)
    client.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_increment_counter_rejects_missing_invalid_or_mismatched_breakpoint_id():
    for stop in (
        {"reason": "breakpoint-hit"},
        {"reason": "breakpoint-hit", "bkptno": "bad;7"},
        {"reason": "breakpoint-hit", "bkptno": "8"},
    ):
        driver = _prepare_increment_driver(breakpoint_stop=stop)
        with pytest.raises(RuntimeError, match="breakpoint"):
            await driver.increment_counter("counter")
        driver.client.step_instruction.assert_not_awaited()
        driver.client.delete_breakpoint.assert_awaited_once_with("7")


@pytest.mark.asyncio
async def test_increment_counter_rejects_missing_or_invalid_inserted_breakpoint_id():
    for inserted_id in (None, "bad;7"):
        driver = _prepare_increment_driver()
        driver.client.insert_breakpoint = AsyncMock(return_value=inserted_id)

        with pytest.raises(RuntimeError, match="valid ID"):
            await driver.increment_counter("counter")

        driver.client.continue_execution.assert_not_awaited()
        if inserted_id is None:
            driver.client.delete_breakpoint.assert_not_awaited()
        else:
            driver.client.delete_breakpoint.assert_awaited_once_with("bad;7")
