"""Asynchronous, token-correlated client for GDB's machine interface (MI)."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import json
import ipaddress
import math
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable


class GDBMIError(RuntimeError):
    """Raised when a GDB/MI command fails or its process exits unexpectedly."""


def validate_gdb_host(host: object) -> str:
    """Validate a hostname or IP literal before embedding it in a GDB/MI command."""
    if not isinstance(host, str) or not host:
        raise ValueError("GDB host must be a non-empty hostname or IP address")
    if any(ord(character) < 32 or ord(character) == 127 for character in host):
        raise ValueError("GDB host must not contain control characters")
    if any(character.isspace() for character in host):
        raise ValueError("GDB host must not contain whitespace")
    candidate = host
    if host.startswith("[") and host.endswith("]"):
        candidate = host[1:-1]
        try:
            ipaddress.IPv6Address(candidate)
        except ipaddress.AddressValueError as exc:
            raise ValueError("bracketed GDB host must be a valid IPv6 address") from exc
    elif ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ValueError("GDB host with colons must be a valid IPv6 address") from exc
    elif not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?",
        host,
    ):
        raise ValueError("GDB host must be a valid hostname or IP address")
    return host


def validate_gdb_port(port: object) -> int:
    """Require an integer TCP port in the usable range."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("GDB port must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("GDB port must be between 1 and 65535")
    return port


def validate_gdb_executable(path: object) -> str:
    """Validate executable text before passing it to subprocess creation."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("GDB executable must be a non-empty path or command name")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError("GDB executable must not contain control characters")
    return path


@dataclass(frozen=True)
class GDBBreakpoint:
    """One GDB-resolved breakpoint location returned by ``-break-insert``."""

    number: str
    address: int


class _ReentrantAsyncLock:
    """Task-reentrant lock for a whole MI transaction and its nested commands."""

    def __init__(self) -> None:
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0
        self._condition = asyncio.Condition()
        self._active_commands = 0
        self._waiting_transactions = 0

    async def __aenter__(self) -> "_ReentrantAsyncLock":
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("MI transaction requires an asyncio task")
        if self._owner is task:
            self._depth += 1
            return self
        async with self._condition:
            self._waiting_transactions += 1
            try:
                await self._condition.wait_for(lambda: self._owner is None and self._active_commands == 0)
                self._owner = task
                self._depth = 1
            finally:
                self._waiting_transactions -= 1
                self._condition.notify_all()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._owner is not asyncio.current_task():
            raise RuntimeError("MI transaction lock released by a non-owner task")
        async with self._condition:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._condition.notify_all()

    @asynccontextmanager
    async def command(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("MI command requires an asyncio task")
        async with self._condition:
            if self._owner is task:
                yield_allowed = False
            else:
                yield_allowed = True
                await self._condition.wait_for(lambda: self._owner is None and self._waiting_transactions == 0)
                self._active_commands += 1
        try:
            yield
        finally:
            if yield_allowed:
                async with self._condition:
                    self._active_commands -= 1
                    self._condition.notify_all()


def _serialized_mi(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    async def wrapped(self: "GDBMIClient", *args: Any, **kwargs: Any) -> Any:
        async with self.transaction():
            return await method(self, *args, **kwargs)

    return wrapped


def _serialized_command(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    async def wrapped(self: "GDBMIClient", *args: Any, **kwargs: Any) -> Any:
        async with self._transaction_lock.command():
            return await method(self, *args, **kwargs)
    return wrapped


class GDBMIClient:
    """Manage a GDB process and correlate MI results with their command tokens."""

    _TOKEN_RESULT = re.compile(rb"^(\d+)\^(done|connected|running|error)(?:,(.*))?$")
    MAX_MEMORY_READ_SIZE = 4096
    MAX_ARM_ADDRESS = 0xFFFFFFFF

    def __init__(self, gdb_path: str = "arm-none-eabi-gdb", command_timeout: float = 15.0) -> None:
        self.gdb_path = validate_gdb_executable(gdb_path)
        if (isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float))
                or not math.isfinite(command_timeout) or command_timeout <= 0):
            raise ValueError("GDB/MI command timeout must be a positive finite number")
        self.command_timeout = command_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._token = 0
        self._pending: dict[int, asyncio.Future[tuple[str, str]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._transaction_lock = _ReentrantAsyncLock()
        self.last_async_records: list[str] = []
        self.last_stop_record: str | None = None
        self.last_execution_record: str | None = None
        self.last_execution_state_source: str | None = None
        self._async_condition = asyncio.Condition()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Exclude other tasks across a compound GDB/MI operation."""
        async with self._transaction_lock:
            yield

    async def start(self) -> None:
        if self.is_running:
            return
        self._closed = False
        self.process = await asyncio.create_subprocess_exec(
            self.gdb_path,
            "--interpreter=mi2",
            "--quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_responses())

    async def _read_responses(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                line = line.rstrip(b"\r\n")
                match = self._TOKEN_RESULT.match(line)
                if match:
                    token = int(match.group(1))
                    future = self._pending.get(token)
                    if future is not None and not future.done():
                        future.set_result((match.group(2).decode("ascii"), (match.group(3) or b"").decode("utf-8", "replace")))
                elif line.startswith((b"*", b"+", b"=")):
                    record = line.decode("utf-8", "replace")
                    self.last_async_records.append(record)
                    if record.startswith("*stopped"):
                        self.last_stop_record = record
                        self.last_execution_record = record
                    elif record.startswith("*running"):
                        self.last_stop_record = None
                        self.last_execution_record = record
                    if len(self.last_async_records) > 100:
                        del self.last_async_records[:-100]
                    async with self._async_condition:
                        self._async_condition.notify_all()
        except asyncio.CancelledError:
            raise
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(GDBMIError("GDB process closed before returning a command result"))

    @_serialized_command
    async def command(self, command: str, timeout: float | None = None) -> tuple[str, str]:
        if not self.is_running or self.process is None or self.process.stdin is None:
            raise GDBMIError("GDB process is not running")
        if timeout is not None and timeout <= 0:
            raise ValueError("GDB/MI command timeout must be positive")
        self._token += 1
        token = self._token
        future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        self._pending[token] = future
        sent = False
        command_timeout = timeout or self.command_timeout
        try:
            self.process.stdin.write(f"{token}{command}\n".encode("utf-8"))
            sent = True
            await self.process.stdin.drain()
            result, payload = await asyncio.wait_for(asyncio.shield(future), command_timeout)
        except asyncio.CancelledError as cancelled:
            if sent:
                try:
                    result_payload = await asyncio.wait_for(asyncio.shield(future), command_timeout)
                    setattr(cancelled, "gdb_mi_result", result_payload)
                except BaseException as resolution_error:
                    setattr(cancelled, "gdb_mi_outcome_ambiguous", True)
                    cancelled.add_note(
                        f"GDB/MI command outcome is ambiguous after cancellation: {resolution_error}"
                    )
            raise
        except asyncio.TimeoutError as timeout_error:
            if sent:
                setattr(timeout_error, "gdb_mi_outcome_ambiguous", True)
            raise
        finally:
            self._pending.pop(token, None)
        if result == "error":
            raise GDBMIError(f"GDB/MI command {command!r} failed: {payload}")
        return result, payload

    async def connect_remote(self, host: str, port: int) -> None:
        host = validate_gdb_host(host)
        port = validate_gdb_port(port)
        remote_host = host
        if ":" in host and not host.startswith("["):
            remote_host = f"[{host}]"
        await self.command(f"-target-select remote tcp:{remote_host}:{port}")

    @_serialized_mi
    async def wait_for_stop(self, since: int = 0, timeout: float | None = None) -> str:
        """Wait for a fresh *stopped async record after an execution command."""
        deadline = time.monotonic() + (timeout or self.command_timeout)
        async with self._async_condition:
            while True:
                for record in self.last_async_records[since:]:
                    if record.startswith("*stopped"):
                        return record
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for GDB/MI *stopped record")
                await asyncio.wait_for(self._async_condition.wait(), remaining)

    async def load_firmware(self, firmware_path: str) -> None:
        escaped_path = firmware_path.replace("\\", "\\\\").replace('"', '\\"')
        await self.command(f'-file-exec-and-symbols "{escaped_path}"')
        await self.command("-target-download")

    async def insert_breakpoint(self, location: str) -> str:
        """Insert one breakpoint and return its number (legacy narrow API)."""
        return (await self.insert_breakpoint_with_address(location)).number

    async def insert_breakpoint_with_address(self, location: str) -> GDBBreakpoint:
        """Insert a breakpoint and retain GDB's concrete code address."""
        if not isinstance(location, str) or not (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", location)
            or re.fullmatch(r"\*0[xX][0-9a-fA-F]+", location)
        ):
            raise ValueError(f"invalid breakpoint location: {location!r}")
        try:
            _, payload = await self.command(f"-break-insert {location}")
        except asyncio.CancelledError as cancelled:
            result = getattr(cancelled, "gdb_mi_result", None)
            if result is not None and result[0] != "error":
                breakpoint_id = self._breakpoint_id(result[1])
                if breakpoint_id is not None:
                    setattr(cancelled, "arm_breakpoint_id", breakpoint_id)
                else:
                    setattr(cancelled, "arm_breakpoint_insertion_ambiguous", True)
            elif getattr(cancelled, "gdb_mi_outcome_ambiguous", False):
                setattr(cancelled, "arm_breakpoint_insertion_ambiguous", True)
            raise
        except asyncio.TimeoutError as exc:
            if getattr(exc, "gdb_mi_outcome_ambiguous", False):
                setattr(exc, "arm_breakpoint_insertion_ambiguous", True)
            raise
        breakpoint_id = self._breakpoint_id(payload)
        if breakpoint_id is None:
            error = GDBMIError(f"GDB did not return a valid breakpoint ID: {payload}")
            setattr(error, "arm_breakpoint_insertion_ambiguous", True)
            raise error
        address_match = re.search(r'\baddr="([^"]+)"', payload)
        if address_match is None:
            error = GDBMIError(f"GDB returned breakpoint {breakpoint_id} without a resolved address: {payload}")
            setattr(error, "arm_breakpoint_id", breakpoint_id)
            raise error
        address_text = address_match.group(1)
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", address_text):
            error = GDBMIError(
                f"GDB returned a malformed resolved address for breakpoint {breakpoint_id}: {address_text!r}"
            )
            setattr(error, "arm_breakpoint_id", breakpoint_id)
            raise error
        address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
        if address < 0 or address > self.MAX_ARM_ADDRESS:
            error = GDBMIError(
                f"GDB returned an out-of-range resolved address for breakpoint {breakpoint_id}: {address_text!r}"
            )
            setattr(error, "arm_breakpoint_id", breakpoint_id)
            raise error
        return GDBBreakpoint(breakpoint_id, address)

    @staticmethod
    def _breakpoint_id(payload: str) -> str | None:
        match = re.search(r'number="([^"]+)"', payload)
        if match is None or not re.fullmatch(r"\d+(?:\.\d+)*", match.group(1)):
            return None
        return match.group(1)

    async def delete_breakpoint(self, breakpoint_id: str) -> None:
        """Delete one validated GDB breakpoint without affecting other breakpoints."""
        if not isinstance(breakpoint_id, str) or not re.fullmatch(r"\d+(?:\.\d+)*", breakpoint_id):
            raise ValueError(f"invalid GDB breakpoint ID: {breakpoint_id!r}")
        await self.command(f"-break-delete {breakpoint_id}")

    @_serialized_mi
    async def continue_execution(self) -> None:
        self.clear_execution_evidence()
        await self.command("-exec-continue")

    @_serialized_mi
    async def step_instruction(self) -> None:
        self.clear_execution_evidence()
        await self.command("-exec-step-instruction")

    def clear_execution_evidence(self) -> None:
        """Discard stale run/stop evidence before issuing an execution command."""
        self.last_async_records.clear()
        self.last_stop_record = None
        self.last_execution_record = None
        self.last_execution_state_source = None

    async def read_memory(self, address: int | str, size: int) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("memory read size must be a positive integer")
        if size > self.MAX_MEMORY_READ_SIZE:
            raise ValueError(f"memory read size cannot exceed {self.MAX_MEMORY_READ_SIZE} bytes")
        if isinstance(address, bool):
            raise ValueError("memory read address must be a numeric 32-bit ARM address")
        if isinstance(address, int):
            numeric_address = address
        elif isinstance(address, str) and re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", address):
            numeric_address = int(address, 16 if address.lower().startswith("0x") else 10)
        else:
            raise ValueError("memory read address must be a numeric 32-bit ARM address")
        if numeric_address < 0 or numeric_address + size - 1 > self.MAX_ARM_ADDRESS:
            raise ValueError("memory read address range is outside the 32-bit ARM address space")
        address_text = f"0x{numeric_address:x}"
        _, payload = await self.command(f"-data-read-memory-bytes {address_text} {size}")
        match = re.search(r'contents="([0-9a-fA-F]*)"', payload)
        if not match:
            raise GDBMIError(f"GDB returned no memory contents for {address_text}")
        contents = bytes.fromhex(match.group(1))
        if len(contents) != size:
            raise GDBMIError(
                f"GDB returned {len(contents)} bytes for {address_text}; expected {size}"
            )
        return contents

    async def read_registers(self) -> list[dict[str, Any]]:
        _, payload = await self.command("-data-list-register-values x")
        values: list[dict[str, Any]] = []
        for record in re.findall(r'\{number="(\d+)",value="([^"]*)"\}', payload):
            values.append({"number": int(record[0]), "value": record[1]})
        return values

    async def read_register_names(self) -> list[str]:
        _, payload = await self.command("-data-list-register-names")
        match = re.search(r'register-names=\[(.*)\]$', payload)
        if match is None:
            raise GDBMIError(f"GDB returned no register names: {payload}")
        return [self._decode_mi_string(value) for value in re.findall(r'"((?:\\.|[^"\\])*)"', match.group(1))]

    @staticmethod
    def _decode_mi_string(value: str) -> str:
        return bytes(value, "utf-8").decode("unicode_escape")

    @staticmethod
    def _parse_mi_attributes(payload: str) -> dict[str, str]:
        return {
            key: GDBMIClient._decode_mi_string(value)
            for key, value in re.findall(r'([\w-]+)="((?:\\.|[^"\\])*)"', payload)
        }

    @_serialized_mi
    async def read_execution_state(self) -> str:
        """Read thread state, falling back only to the latest GDB/MI run/stop event."""
        thread_info_error: Exception | None = None
        try:
            _, payload = await self.command("-thread-info")
            match = re.search(r'threads=\[\{([^{}]*)\}', payload)
            if match is not None:
                attributes = self._parse_mi_attributes(match.group(1))
                state = attributes.get("state")
                if state in {"running", "stopped"}:
                    self.last_execution_state_source = "GDB/MI -thread-info"
                    return state
            thread_info_error = GDBMIError(f"GDB returned no current thread state: {payload}")
        except Exception as exc:
            thread_info_error = exc

        event = self.last_execution_record
        if event is not None:
            if event.startswith("*stopped"):
                self.last_execution_state_source = "GDB/MI *stopped async record"
                return "stopped"
            if event.startswith("*running"):
                self.last_execution_state_source = "GDB/MI *running async record"
                return "running"
        raise GDBMIError(f"No reliable GDB/MI execution-state evidence: {thread_info_error}")

    async def read_breakpoints(self) -> list[dict[str, Any]]:
        _, payload = await self.command("-break-list")
        body_match = re.search(r"body=\[(.*)\]\}\s*$", payload)
        if body_match is None:
            raise GDBMIError(f"GDB returned no breakpoint table: {payload}")
        entries = re.findall(r"bkpt=\{([^{}]*)\}", body_match.group(1))
        breakpoints: list[dict[str, Any]] = []
        for entry in entries:
            values = self._parse_mi_attributes(entry)
            number = values.get("number")
            if number is None:
                continue
            breakpoints.append(
                {
                    "id": number,
                    "enabled": values.get("enabled") == "y",
                    "address": values.get("addr"),
                    "location": values.get("original-location") or values.get("what"),
                    "hit_count": int(values["times"]) if values.get("times", "").isdigit() else None,
                }
            )
        return breakpoints

    def latest_stop_details(self) -> dict[str, str] | None:
        """Parse details from the latest actual GDB *stopped async record."""
        if self.last_stop_record is None:
            return None
        return self._parse_mi_attributes(self.last_stop_record)

    async def disassemble_instruction(self, address: int, max_bytes: int = 4) -> dict[str, Any]:
        """Return structured MI disassembly whose address exactly matches PC."""
        _, payload = await self.command(
            f"-data-disassemble -s 0x{address:x} -e 0x{address + max_bytes:x} -- 0"
        )
        match = re.search(r"asm_insns=\[(.*)\]$", payload)
        if match is None:
            raise GDBMIError(f"GDB returned no instruction at 0x{address:x}: {payload}")
        for entry in re.findall(r"\{([^{}]*)\}", match.group(1)):
            values = self._parse_mi_attributes(entry)
            try:
                instruction_address = int(values.get("address", ""), 0)
            except ValueError:
                continue
            if instruction_address == address and "inst" in values:
                return {"address": instruction_address, "text": values["inst"], "raw": entry}
        raise GDBMIError(f"GDB disassembly did not include an instruction at PC 0x{address:x}")

    async def read_pc(self) -> str:
        """Read the program counter using GDB's architecture-aware $pc expression."""
        return await self.evaluate("$pc")

    async def evaluate(self, expression: str) -> str:
        """Evaluate a GDB expression and return its MI string value."""
        escaped = expression.replace("\\", "\\\\").replace('"', '\\"')
        _, payload = await self.command(f'-data-evaluate-expression "{escaped}"')
        match = re.search(r'value="((?:\\.|[^"\\])*)"', payload)
        if not match:
            raise GDBMIError(f"GDB returned no value for expression {expression!r}")
        return bytes(match.group(1), "utf-8").decode("unicode_escape")

    async def disassemble(self, function: str) -> str:
        escaped = function.replace("\\", "\\\\").replace('"', '\\"')
        _, payload = await self.command(f'-interpreter-exec console "disassemble {escaped}"')
        return payload

    async def disassemble_range(self, start: int, end: int) -> str:
        """Return machine-readable GDB/MI disassembly for an address range."""
        _, payload = await self.command(f"-data-disassemble -s 0x{start:x} -e 0x{end:x} -- 0")
        match = re.search(r"asm_insns=\[(.*)\]$", payload)
        if match is None:
            raise GDBMIError(f"GDB returned no disassembly for 0x{start:x}-0x{end:x}: {payload}")
        return match.group(1)

    @_serialized_mi
    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            if self.process.stdin is not None:
                self.process.stdin.write(b"-gdb-exit\n")
                await self.process.stdin.drain()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        if self._reader_task is not None:
            if not self._reader_task.done():
                self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self.process = None
        self._reader_task = None


def encode_observation(value: dict[str, Any]) -> bytes:
    """Encode an ARM runtime observation as deterministic JSON bytes for TransportFrame."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
