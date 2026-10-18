"""Layer 1 driver for ARM firmware running on a QEMU GDB remote target."""

import asyncio
import hashlib
import os
import re
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncIterator, Callable

import structlog

from layer1.arm.gdb_client import (
    GDBBreakpoint,
    GDBMIClient,
    encode_observation,
    validate_gdb_host,
    validate_gdb_port,
)
from layer1.arm.observation import ARMObservationConfig
from layer1.base import EnvironmentDriver
from schemas.events import RuntimeEvent, RuntimeEventType
from schemas.pipeline import TransportFrame

logger = structlog.get_logger(__name__)

_SYMBOL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMERIC_ADDRESS = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")


class ARMExecutionCleanupError(RuntimeError):
    """Structured failure retaining action and temporary-breakpoint cleanup evidence."""

    def __init__(
        self,
        primary_error: BaseException | None,
        cleanup_errors: tuple[str, ...],
        *,
        breakpoint_insertion_ambiguous: bool = False,
    ) -> None:
        self.primary_error = primary_error
        self.cleanup_errors = cleanup_errors
        self.breakpoint_insertion_ambiguous = breakpoint_insertion_ambiguous
        details = "; ".join(cleanup_errors) or "temporary breakpoint insertion outcome is ambiguous"
        primary = f"{type(primary_error).__name__}: {primary_error}; " if primary_error else ""
        super().__init__(f"{primary}temporary breakpoint cleanup failed or insertion outcome is ambiguous: {details}")


def _serialized_driver(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    async def wrapped(self: "ARMDriver", *args: Any, **kwargs: Any) -> Any:
        async with self.execution_transaction():
            return await method(self, *args, **kwargs)
    return wrapped


def _parse_address(value: int | str, description: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{description} must be a numeric 32-bit ARM address")
    if isinstance(value, int):
        address = value
    elif isinstance(value, str) and _NUMERIC_ADDRESS.fullmatch(value):
        address = int(value, 16 if value.lower().startswith("0x") else 10)
    else:
        raise ValueError(f"{description} must be a numeric 32-bit ARM address")
    if address < 0 or address > GDBMIClient.MAX_ARM_ADDRESS:
        raise ValueError(f"{description} is outside the 32-bit ARM address space")
    return address


def _parse_gdb_integer(value: str, description: str) -> int:
    match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)(?=\s|$)", value)
    if match is None:
        raise RuntimeError(f"GDB returned an invalid {description}: {value}")
    token = match.group(1)
    return int(token, 16 if token.lower().startswith("0x") else 10)


class ARMDriver(EnvironmentDriver):
    """Control ARM through GDB/MI; the firmware hash identifies the local loaded snapshot.

    This fingerprint verifies the bytes supplied to GDB's local firmware load
    command. GDB accepts a path rather than an already-open file handle, so the
    read-only snapshot and before/after hashes minimize but cannot eliminate a
    same-user concurrent replace-and-restore race. It does not measure or attest
    firmware on a remote physical target.
    """

    def __init__(
        self,
        firmware_path: str,
        gdb_path: str = "arm-none-eabi-gdb",
        gdb_host: str = "127.0.0.1",
        gdb_port: int = 1234,
        runtime_id: str = "arm_qemu_01",
        command_timeout: float = 15.0,
        observation_config: ARMObservationConfig | None = None,
        architecture: str = "arm",
        cpu: str | None = "cortex-a15",
    ) -> None:
        self.firmware_path = firmware_path
        self.gdb_host = validate_gdb_host(gdb_host)
        self.gdb_port = validate_gdb_port(gdb_port)
        self._runtime_id = runtime_id
        self.client = GDBMIClient(gdb_path, command_timeout)
        self.observation_config = observation_config or ARMObservationConfig()
        self.architecture = architecture
        self.cpu = cpu
        self._firmware_sha256: str | None = None
        self._firmware_hash_error: str | None = "firmware has not been snapshotted and loaded"
        self._firmware_snapshot: tempfile.TemporaryDirectory[str] | None = None
        self._firmware_snapshot_path: str | None = None
        self._connected = False
        self._frozen = True
        self._frame_seq = 0
        self._event_listeners: list[Callable[[RuntimeEvent], None]] = []

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def add_event_listener(self, listener: Callable[[RuntimeEvent], None]) -> None:
        self._event_listeners.append(listener)

    @asynccontextmanager
    async def execution_transaction(self) -> AsyncIterator[None]:
        """Serialize compound actions and observations for this GDB runtime."""
        async with self.client.transaction():
            yield

    def _emit_event(self, event_type: RuntimeEventType, message: str, details: dict[str, Any] | None = None) -> None:
        event = RuntimeEvent(event_type, self._runtime_id, message, time.time(), details or {})
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception as exc:
                logger.error("arm_event_listener_failed", error=str(exc))

    @_serialized_driver
    async def connect(self) -> None:
        if self._connected:
            return
        snapshot_path, snapshot_digest = self._snapshot_firmware()
        try:
            await self.client.start()
            await self.client.connect_remote(self.gdb_host, self.gdb_port)
            await self.client.load_firmware(snapshot_path)
            loaded_digest = self._hash_file(snapshot_path)
            if loaded_digest != snapshot_digest:
                raise RuntimeError("firmware snapshot changed while GDB was loading it; refusing to connect")
        except BaseException as operation_error:
            try:
                await self.client.close()
            except BaseException as close_error:
                operation_error.add_note(f"GDB close after failed firmware load also failed: {close_error}")
            self._discard_firmware_snapshot()
            self._firmware_sha256 = None
            self._firmware_hash_error = str(operation_error)
            raise
        self._firmware_sha256 = snapshot_digest
        self._firmware_hash_error = None
        self._connected = True
        self._frozen = True
        self._emit_event(RuntimeEventType.CONNECTED, f"Connected to ARM target at {self.gdb_host}:{self.gdb_port}")
        logger.info("arm_l1_connected", runtime_id=self._runtime_id, host=self.gdb_host, port=self.gdb_port)

    @_serialized_driver
    async def disconnect(self) -> None:
        was_connected = self._connected
        try:
            await self.client.close()
        finally:
            self._connected = False
            self._discard_firmware_snapshot()
        if was_connected:
            self._emit_event(RuntimeEventType.DISCONNECTED, "Disconnected from ARM target")

    @_serialized_driver
    async def freeze(self) -> None:
        self._require_connected()
        # QEMU is launched halted (-S); GDB's remote interrupt stops a running target.
        if not self._frozen:
            self.client.clear_execution_evidence()
            await self.client.command("-exec-interrupt")
            await self.client.wait_for_stop(0)
            self._frozen = True

    @_serialized_driver
    async def unfreeze(self) -> None:
        self._require_connected()
        if self._frozen:
            self.client.clear_execution_evidence()
            await self.client.continue_execution()
            self._frozen = False

    async def health_check(self) -> bool:
        if not self._connected or not self.client.is_running:
            return False
        try:
            await self.read_registers()
            return True
        except Exception as exc:
            logger.warning("arm_health_check_failed", error=str(exc))
            return False

    @_serialized_driver
    async def read_frame(self) -> TransportFrame:
        """Capture configured ARM runtime data while preserving per-field failures."""
        self._require_connected()
        config = self.observation_config
        self._frame_seq += 1
        timestamp = time.time()
        errors: list[dict[str, str]] = []
        observed: list[str] = []
        unavailable: list[str] = []
        failed: list[str] = []
        capabilities: set[str] = set()

        async def capture(name: str, operation: Any, unavailable_on_error: bool = False) -> dict[str, Any]:
            try:
                value = await operation
            except Exception as exc:
                status = "unavailable" if unavailable_on_error else "failed"
                (unavailable if unavailable_on_error else failed).append(name)
                error = {"type": type(exc).__name__, "message": str(exc)}
                if not unavailable_on_error:
                    errors.append({"field": name, **error})
                return {"status": status, "error": error}
            observed.append(name)
            capabilities.add(name.split(":", 1)[0])
            return {"status": "observed", "value": value}

        registers_result: dict[str, Any]
        if config.include_registers:
            try:
                values, names = await self.read_registers(), await self.client.read_register_names()
                normalized = []
                for item in values:
                    number = item["number"]
                    raw_value = str(item["value"])
                    match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)", raw_value)
                    normalized.append(
                        {
                            "number": number,
                            "name": names[number] if number < len(names) and names[number] else None,
                            "status": "observed" if match else "failed",
                            "raw_value": raw_value,
                            "value": int(match.group(1), 0) if match else None,
                            **({"error": {"type": "ValueError", "message": "GDB register value is not numeric"}} if not match else {}),
                        }
                    )
                registers_result = {"status": "observed", "values": normalized}
                observed.append("registers")
                capabilities.add("registers")
                for item in normalized:
                    if item["status"] == "failed":
                        failed.append(f"register:{item['number']}")
                        errors.append({"field": f"register:{item['number']}", **item["error"]})
            except Exception as exc:
                registers_result = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
                failed.append("registers")
                errors.append({"field": "registers", **registers_result["error"]})
        else:
            registers_result = {"status": "not_requested"}

        async def numeric_expression(expression: str) -> dict[str, Any]:
            source = await self.client.evaluate(expression)
            match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)", source)
            if match is None:
                raise ValueError(f"GDB returned a non-numeric value for {expression}: {source}")
            return {"raw_value": source, "value": int(match.group(1), 0)}

        if config.include_pc:
            try:
                pc_source = await self.read_pc()
                pc_match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)", pc_source)
                if pc_match is None:
                    raise ValueError(f"GDB returned a non-numeric PC: {pc_source}")
                pc_result = {"status": "observed", "raw_value": pc_source, "value": int(pc_match.group(1), 0)}
                observed.append("pc")
                capabilities.add("pc")
            except Exception as exc:
                pc_result = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
                failed.append("pc")
                errors.append({"field": "pc", **pc_result["error"]})
        else:
            pc_result = {"status": "not_requested"}

        if config.include_sp:
            sp_capture = await capture("sp", numeric_expression("$sp"))
            sp_result = {"status": "observed", **sp_capture["value"]} if sp_capture["status"] == "observed" else sp_capture
        else:
            sp_result = {"status": "not_requested"}

        status_results: dict[str, Any] = {}
        for register_name in config.status_registers:
            key = f"status:{register_name}"
            try:
                source = await self.client.evaluate(f"${register_name}")
                match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)", source)
                if match is None:
                    raise ValueError(f"GDB returned a non-numeric status register: {source}")
                status_results[register_name] = {
                    "status": "observed", "raw_value": source, "value": int(match.group(1), 0)
                }
                observed.append(key)
                capabilities.add("status_registers")
            except Exception as exc:
                status_results[register_name] = {"status": "unavailable", "error": {"type": type(exc).__name__, "message": str(exc)}}
                unavailable.append(key)

        if config.include_execution:
            execution_result = await capture("execution", self.client.read_execution_state(), unavailable_on_error=True)
            if execution_result["status"] == "observed":
                execution_result["source"] = self.client.last_execution_state_source
            execution_state = execution_result.get("value")
            stop_details = self.client.latest_stop_details() if execution_state == "stopped" else None
            if execution_state == "stopped":
                reason = stop_details.get("reason") if stop_details else None
                stop_result = {"status": "observed", "value": reason} if reason else {"status": "unavailable"}
                if not reason:
                    unavailable.append("stop_reason")
                hit_id = stop_details.get("bkptno") if stop_details else None
                hit_result = {"status": "observed", "value": {"breakpoint_id": hit_id}} if hit_id else {"status": "unavailable"}
                if not hit_id:
                    unavailable.append("breakpoint_hit")
            else:
                stop_result = {"status": "unavailable"}
                hit_result = {"status": "unavailable"}
                unavailable.extend(["stop_reason", "breakpoint_hit"])
        else:
            execution_result = {"status": "not_requested"}
            stop_result = hit_result = {"status": "not_requested"}

        if config.include_current_instruction:
            if pc_result.get("status") == "observed":
                try:
                    instruction = await self.client.disassemble_instruction(pc_result["value"])
                    instruction_result = {"status": "observed", "value": instruction, **instruction}
                    observed.append("current_instruction")
                    capabilities.add("disassembly")
                except Exception as exc:
                    instruction_result = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
                    failed.append("current_instruction")
                    errors.append({"field": "current_instruction", **instruction_result["error"]})
            else:
                instruction_result = {"status": "unavailable"}
                unavailable.append("current_instruction")
        else:
            instruction_result = {"status": "not_requested"}

        if config.include_breakpoints:
            breakpoints_result = await capture("breakpoints", self.client.read_breakpoints(), unavailable_on_error=True)
        else:
            breakpoints_result = {"status": "not_requested"}

        memory_results: list[dict[str, Any]] = []
        for request in config.memory:
            request_data = {
                "request_id": request.request_id,
                "symbol": request.symbol,
                "requested_address": request.address,
                "size": request.size,
                "decode": (
                    {"type": request.decode.value_type, "endianness": request.decode.endianness, "signed": request.decode.signed}
                    if request.decode else None
                ),
            }
            try:
                address = request.address if request.address is not None else await self.resolve_symbol(str(request.symbol))
                raw = await self.read_memory(address, request.size)
                memory_results.append({**request_data, "status": "observed", "address": address, "raw_bytes_hex": raw.hex()})
                observed.append(f"memory:{request.request_id}")
                capabilities.add("memory")
            except Exception as exc:
                memory_results.append({**request_data, "status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}})
                failed.append(f"memory:{request.request_id}")
                errors.append({"field": f"memory:{request.request_id}", "type": type(exc).__name__, "message": str(exc)})

        identity = {
            "runtime_id": self._runtime_id,
            "architecture": {"status": "observed", "value": self.architecture, "source": "driver configuration"},
            "cpu": (
                {"status": "observed", "value": self.cpu, "source": "driver configuration"}
                if self.cpu else {"status": "unavailable"}
            ),
            "firmware": {
                "path": {"status": "observed", "value": self.firmware_path, "source": "driver configuration"},
                "sha256": (
                    {"status": "observed", "value": self._firmware_sha256}
                    if self._firmware_sha256 else {"status": "unavailable", "error": self._firmware_hash_error}
                ),
            },
        }
        payload_data = {
            "environment": "arm",
            "schema_version": "2.0",
            "identity": identity,
            "cpu": {
                "registers": registers_result,
                "pc": pc_result,
                "sp": sp_result,
                "status_flags": status_results,
            },
            "execution": {
                "state": execution_result,
                "current_instruction": instruction_result,
                "stop_reason": stop_result,
                "breakpoint_hit": hit_result,
            },
            "memory": {"status": "observed" if memory_results else "not_requested", "regions": memory_results},
            "debugging": {"breakpoints": breakpoints_result},
            "metadata": {
                "frame_seq": self._frame_seq,
                "timestamp": timestamp,
                "capabilities": sorted(capabilities),
                "completeness": {
                    "requested": sorted(set(observed + unavailable + failed + [
                        name for name, enabled in (("registers", config.include_registers), ("pc", config.include_pc),
                                                   ("sp", config.include_sp), ("execution", config.include_execution),
                                                   ("breakpoints", config.include_breakpoints)) if enabled
                    ] + [f"memory:{r.request_id}" for r in config.memory] + [f"status:{n}" for n in config.status_registers])),
                    "observed": sorted(set(observed)),
                    "not_requested": sorted(
                        (["registers"] if not config.include_registers else [])
                        + (["pc"] if not config.include_pc else [])
                        + (["sp"] if not config.include_sp else [])
                        + (["execution", "stop_reason", "breakpoint_hit"] if not config.include_execution else [])
                        + (["current_instruction"] if not config.include_current_instruction else [])
                        + (["breakpoints"] if not config.include_breakpoints else [])
                    ),
                    "unavailable": sorted(set(unavailable)),
                    "failed": sorted(set(failed)),
                },
                "errors": errors,
            },
        }
        payload = encode_observation(payload_data)
        register_count = len(registers_result.get("values", []))
        self._emit_event(RuntimeEventType.SCREEN_RECEIVED, f"Received ARM observation #{self._frame_seq}", {"register_count": register_count})
        return TransportFrame(raw_payload=payload, timestamp=timestamp, frame_seq=self._frame_seq)

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError("ARM targets do not support generic raw byte writes; use ARM-specific operations")

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("ARM driver is not connected")

    @staticmethod
    def _hash_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as firmware_file:
            for chunk in iter(lambda: firmware_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_firmware(self) -> tuple[str, str]:
        """Copy the current local firmware to a private read-only load snapshot."""
        self._discard_firmware_snapshot()
        snapshot_dir = tempfile.TemporaryDirectory(prefix="sentinel-arm-firmware-")
        snapshot_path = os.path.join(snapshot_dir.name, os.path.basename(self.firmware_path) or "firmware.elf")
        digest = hashlib.sha256()
        try:
            with open(self.firmware_path, "rb") as source, open(snapshot_path, "xb") as snapshot:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    snapshot.write(chunk)
                snapshot.flush()
                os.fsync(snapshot.fileno())
            os.chmod(snapshot_path, stat.S_IREAD)
            fingerprint = digest.hexdigest()
            if self._hash_file(snapshot_path) != fingerprint:
                raise RuntimeError("firmware snapshot does not match the bytes copied from the configured file")
        except BaseException:
            try:
                os.chmod(snapshot_path, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
            snapshot_dir.cleanup()
            raise
        self._firmware_snapshot = snapshot_dir
        self._firmware_snapshot_path = snapshot_path
        return snapshot_path, fingerprint

    def _discard_firmware_snapshot(self) -> None:
        snapshot = self._firmware_snapshot
        snapshot_path = self._firmware_snapshot_path
        self._firmware_snapshot = None
        self._firmware_snapshot_path = None
        if snapshot is None:
            return
        if snapshot_path:
            try:
                os.chmod(snapshot_path, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
        try:
            snapshot.cleanup()
        except OSError as exc:
            logger.error("arm_firmware_snapshot_cleanup_failed", path=snapshot_path, error=str(exc))

    async def read_memory(self, address: int | str, size: int) -> bytes:
        self._require_connected()
        return await self.client.read_memory(address, size)

    async def read_registers(self) -> list[dict[str, Any]]:
        self._require_connected()
        return await self.client.read_registers()

    async def read_pc(self) -> str:
        """Read PC without assuming a target-specific GDB register number."""
        self._require_connected()
        return await self.client.read_pc()

    async def set_breakpoint(self, location: str) -> str:
        self._require_connected()
        return await self.client.insert_breakpoint(location)

    async def _set_breakpoint_with_address(self, location: str) -> GDBBreakpoint:
        self._require_connected()
        return await self.client.insert_breakpoint_with_address(location)

    async def delete_breakpoint(self, breakpoint_id: str) -> None:
        self._require_connected()
        await self.client.delete_breakpoint(breakpoint_id)

    @_serialized_driver
    async def run_to_breakpoint(self, location: str) -> None:
        """Stop at one identified breakpoint, verify its address, and remove it."""
        self._require_connected()
        if not isinstance(location, str):
            raise ValueError(f"invalid breakpoint location: {location!r}")
        is_numeric_location = location.startswith("*")
        if is_numeric_location:
            expected_address = _parse_address(location[1:], "breakpoint address")
            breakpoint_location = f"*0x{expected_address:x}"
        else:
            if not _SYMBOL_IDENTIFIER.fullmatch(location):
                raise ValueError(f"invalid breakpoint location: {location!r}")
            expected_address = await self.resolve_symbol(location)
            breakpoint_location = location
        if await self.client.read_execution_state() != "stopped":
            self._frozen = False
            raise RuntimeError("run_to_breakpoint requires the ARM target to be stopped")
        self._frozen = True
        breakpoint_id: str | None = None
        operation_error: BaseException | None = None
        insertion_ambiguous = False
        try:
            breakpoint = await self._set_breakpoint_with_address(breakpoint_location)
            breakpoint_id = breakpoint.number
            if not breakpoint_id:
                raise RuntimeError("GDB did not return an ID for the temporary breakpoint")
            if is_numeric_location and breakpoint.address != expected_address:
                raise RuntimeError(
                    f"GDB resolved numeric breakpoint 0x{expected_address:x} to unexpected address "
                    f"0x{breakpoint.address:x}"
                )
            await self.continue_execution(wait_for_stop=True)
            stop = self.client.latest_stop_details()
            if stop is None or stop.get("reason") != "breakpoint-hit":
                raise RuntimeError(f"requested breakpoint was not the stop reason: {stop!r}")
            hit_id = stop.get("bkptno")
            if hit_id is None:
                raise RuntimeError(
                    f"GDB stop record did not identify the expected temporary breakpoint {breakpoint_id}: {stop!r}"
                )
            if hit_id != breakpoint_id:
                raise RuntimeError(
                    f"target stopped at breakpoint {hit_id}, expected temporary breakpoint {breakpoint_id}"
                )
            stop_address = breakpoint.address if not is_numeric_location else expected_address
            frame_address_source = stop.get("addr")
            if frame_address_source is not None:
                try:
                    frame_address = _parse_address(
                        _parse_gdb_integer(frame_address_source, "stopped frame address"),
                        "stopped frame address",
                    )
                except (RuntimeError, ValueError) as exc:
                    raise RuntimeError(
                        f"GDB returned an invalid stopped frame address: {frame_address_source!r}"
                    ) from exc
                if frame_address != stop_address:
                    raise RuntimeError(
                        f"GDB stopped frame address 0x{frame_address:x} disagrees with resolved breakpoint "
                        f"address 0x{stop_address:x}"
                    )
            pc = _parse_address(
                _parse_gdb_integer(await self.read_pc(), "program counter at breakpoint"),
                "program counter at breakpoint",
            )
            if pc != stop_address:
                raise RuntimeError(f"target stopped at PC 0x{pc:x}, expected breakpoint 0x{stop_address:x}")
        except BaseException as exc:
            operation_error = exc
            breakpoint_id = breakpoint_id or getattr(exc, "arm_breakpoint_id", None)
            insertion_ambiguous = bool(getattr(exc, "arm_breakpoint_insertion_ambiguous", False))
            if insertion_ambiguous and breakpoint_id is None:
                message = "temporary breakpoint insertion outcome is ambiguous; no breakpoint ID was returned"
                logger.error("arm_temporary_breakpoint_insertion_ambiguous", location=breakpoint_location)
                if isinstance(exc, asyncio.CancelledError):
                    exc.add_note(message)
                else:
                    raise ARMExecutionCleanupError(
                        exc, (message,), breakpoint_insertion_ambiguous=True
                    ) from exc
            raise
        finally:
            if breakpoint_id:
                await self._cleanup_temporary_breakpoint(breakpoint_id, operation_error)

    async def run_to_address(self, address: int) -> None:
        """Run to a numeric address without exposing GDB location syntax to L2."""
        numeric = _parse_address(address, "breakpoint address")
        await self.run_to_breakpoint(f"*0x{numeric:x}")

    async def _cleanup_temporary_breakpoint(
        self, breakpoint_id: str, operation_error: BaseException | None
    ) -> None:
        cleanup_errors: list[str] = []
        cleanup_cancellation: asyncio.CancelledError | None = None

        async def attempt(phase: str, operation: Any) -> bool:
            nonlocal cleanup_cancellation
            try:
                await operation
                return True
            except asyncio.CancelledError as exc:
                if cleanup_cancellation is None:
                    cleanup_cancellation = exc
                result = getattr(exc, "gdb_mi_result", None)
                if result is not None:
                    if result[0] == "error":
                        cleanup_errors.append(f"{phase} failed: GDB/MI returned error: {result[1]}")
                        return False
                    if phase in {"target interrupt", "delete breakpoint"}:
                        return True
                message = (
                    f"{phase} outcome is ambiguous after cancellation"
                    if getattr(exc, "gdb_mi_outcome_ambiguous", False) or phase == "delete breakpoint"
                    else f"{phase} was cancelled"
                )
                cleanup_errors.append(message)
                return False
            except Exception as exc:
                cleanup_errors.append(f"{phase} failed: {exc}")
                return False

        if not self._frozen:
            self.client.clear_execution_evidence()
            interrupt_ok = await attempt("target interrupt", self.client.command("-exec-interrupt"))
            stopped = await attempt("wait for target stop", self.client.wait_for_stop(0))
            if interrupt_ok and stopped:
                self._frozen = True
        await attempt("delete breakpoint", self.delete_breakpoint(breakpoint_id))
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            logger.error("arm_temporary_breakpoint_cleanup_failed", breakpoint_id=breakpoint_id,
                         cleanup_errors=cleanup_errors,
                         operation_error=str(operation_error) if operation_error else None)
        if cleanup_cancellation is not None:
            if operation_error is not None and operation_error is not cleanup_cancellation:
                setattr(cleanup_cancellation, "arm_primary_error", operation_error)
                cleanup_cancellation.add_note(f"original ARM operation also failed: {operation_error}")
            setattr(cleanup_cancellation, "arm_cleanup_errors", tuple(cleanup_errors))
            if cleanup_errors:
                cleanup_cancellation.add_note("ARM breakpoint cleanup failures: " + "; ".join(cleanup_errors))
            if operation_error is cleanup_cancellation:
                return
            if operation_error is not None:
                raise cleanup_cancellation from operation_error
            raise cleanup_cancellation
        if cleanup_errors:
            raise ARMExecutionCleanupError(operation_error, tuple(cleanup_errors)) from operation_error

    @_serialized_driver
    async def resolve_symbol(self, symbol: str) -> int:
        """Resolve a firmware symbol to its numeric address through GDB."""
        if not isinstance(symbol, str) or not _SYMBOL_IDENTIFIER.fullmatch(symbol):
            raise ValueError(f"invalid firmware symbol identifier: {symbol!r}")
        value = await self.client.evaluate(f"&{symbol}")
        try:
            return _parse_address(_parse_gdb_integer(value, f"address for symbol {symbol!r}"), "symbol address")
        except (ValueError, RuntimeError) as exc:
            raise RuntimeError(f"GDB returned an invalid address for symbol {symbol!r}: {value}") from exc

    @_serialized_driver
    async def find_store_instruction(self, counter_address: int, symbol: str = "main") -> int:
        """Locate the STR writing the counter in a function's GDB disassembly."""
        instruction_address, _target_address, _source_register = await self._find_store_instruction_details(
            counter_address, symbol
        )
        return instruction_address

    async def _find_store_instruction_details(
        self, counter_address: int, symbol: str
    ) -> tuple[int, int, str]:
        """Return the matching STR address, effective target, and source register."""
        counter_address = _parse_address(counter_address, "counter address")
        if not isinstance(symbol, str) or not _SYMBOL_IDENTIFIER.fullmatch(symbol):
            raise ValueError(f"invalid firmware function identifier: {symbol!r}")
        function_address = await self.resolve_symbol(symbol)
        # Function-pointer + 1 is not a function-end query: GDB may return only
        # one byte past the entry, which truncates Thumb functions mid-body.
        # Scan a bounded window and retain only instructions GDB attributes to
        # the requested function so neighboring functions cannot add candidates.
        function_end = min(function_address + 512, GDBMIClient.MAX_ARM_ADDRESS + 1)
        if function_end <= function_address:
            raise RuntimeError(f"No valid disassembly range remains for {symbol!r} at 0x{function_address:x}")
        listing = await self.client.disassemble_range(function_address, function_end)
        decoded: list[tuple[int, str]] = []
        for entry in re.findall(r"\{([^{}]*)\}", listing):
            attributes = self.client._parse_mi_attributes(entry)
            if attributes.get("func-name") != symbol:
                continue
            address_text, instruction = attributes.get("address"), attributes.get("inst")
            if address_text is None or instruction is None:
                continue
            try:
                instruction_address = _parse_address(address_text, "disassembled instruction address")
            except ValueError:
                continue
            decoded.append((instruction_address, instruction.lower()))

        matches: list[tuple[int, int, str]] = []

        def immediate(operand: str) -> int | None:
            value = operand.strip()
            if not value.startswith("#"):
                return None
            text = value[1:]
            if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
                return None
            return int(text, 16 if text.lower().startswith("0x") else 10)

        def absolute_register_value(instructions_before: list[tuple[int, str]], register: str) -> int | None:
            low_half: int | None = None
            resolved: int | None = None
            for _address, prior_instruction in instructions_before:
                parts = re.match(r"\s*([a-z][a-z0-9.]*)\s+([^;@]+)", prior_instruction)
                if parts is None:
                    continue
                mnemonic = parts.group(1)
                operands = [part.strip() for part in parts.group(2).split(",")]
                if not operands or operands[0] != register:
                    continue
                if mnemonic.startswith("movw") and len(operands) >= 2:
                    low = immediate(operands[1])
                    low_half = low & 0xFFFF if low is not None else None
                    resolved = None
                elif mnemonic.startswith("movt") and len(operands) >= 2 and low_half is not None:
                    high = immediate(operands[1])
                    resolved = (((high & 0xFFFF) << 16) | low_half) if high is not None else None
                    low_half = None
                else:
                    # Any other write to the base register breaks the MOVW/MOVT pair.
                    low_half = None
                    resolved = None
            return resolved

        def writes_register(instruction: str, register: str) -> bool:
            parts = re.match(r"\s*([a-z][a-z0-9.]*)\s+([^;@]+)", instruction)
            if parts is None:
                return False
            mnemonic = parts.group(1)
            operands = [part.strip() for part in parts.group(2).split(",")]
            non_writing = mnemonic.startswith(("str", "cmp", "cmn", "tst", "teq", "push", "stm"))
            return bool(operands and operands[0] == register and not non_writing)

        # Bind each STR through its value-producing ADD and LDR to the absolute
        # address constructed in the LDR/STR base register.
        for index, (address, instruction) in enumerate(decoded):
            store = re.match(
                r"\s*str(?:\.\w+)?\s+(r\d+)\s*,\s*\[(r\d+)(?:\s*,\s*(#[^\]]+))?\]",
                instruction,
            )
            if store is None:
                continue
            stored_register = store.group(1)
            store_base = store.group(2)
            store_offset = immediate(store.group(3)) if store.group(3) else 0
            if store_offset != 0:
                continue
            prior = decoded[max(0, index - 12):index]
            latest_write: tuple[int, str, list[str]] | None = None
            for prior_index, (_prior_address, prior_instruction) in enumerate(prior):
                parts = re.match(r"\s*([a-z][a-z0-9.]*)\s+([^;@]+)", prior_instruction)
                if parts is None:
                    continue
                mnemonic = parts.group(1)
                operands = [part.strip() for part in parts.group(2).split(",")]
                non_writing = mnemonic.startswith(("str", "cmp", "cmn", "tst", "teq", "push", "stm"))
                if operands and operands[0] == stored_register and not non_writing:
                    latest_write = (prior_index, mnemonic, operands)
            if latest_write is None:
                continue
            write_index, mnemonic, operands = latest_write
            if not mnemonic.startswith("add") or len(operands) < 3 or operands[-1] != "#1":
                continue
            source_register = operands[1]
            load_index: int | None = None
            load_base: str | None = None
            for prior_index in range(write_index - 1, -1, -1):
                load = re.match(
                    rf"\s*ldr(?:\.\w+)?\s+{re.escape(source_register)}\s*,\s*"
                    r"\[(r\d+)(?:\s*,\s*(#[^\]]+))?\]",
                    prior[prior_index][1],
                )
                if load is not None:
                    load_index, load_base = prior_index, load.group(1)
                    if load.group(2) and immediate(load.group(2)) != 0:
                        load_index = None
                    break
            if load_index is None or load_base != store_base:
                continue
            if any(writes_register(item, source_register) for _, item in prior[load_index + 1:write_index]):
                continue
            target_address = absolute_register_value(prior[:load_index], load_base)
            if target_address == counter_address:
                matches.append((address, target_address, stored_register))

        if len(matches) == 1:
            return matches[0]
        if matches:
            rendered = ", ".join(f"0x{address:x}" for address, _, _ in matches)
            raise RuntimeError(
                f"Ambiguous increment stores for counter 0x{counter_address:x} in {symbol!r}: {rendered}"
            )
        raise RuntimeError(f"No increment store for counter 0x{counter_address:x} found in {symbol!r}")

    @_serialized_driver
    async def continue_execution(self, wait_for_stop: bool = True) -> None:
        self._require_connected()
        self.client.clear_execution_evidence()
        self._frozen = False
        await self.client.continue_execution()
        if wait_for_stop:
            await self.client.wait_for_stop(0)
            self._frozen = True

    @_serialized_driver
    async def step_instruction(self) -> None:
        self._require_connected()
        self.client.clear_execution_evidence()
        self._frozen = False
        await self.client.step_instruction()
        await self.client.wait_for_stop(0)
        self._frozen = True
        stop = self.client.latest_stop_details()
        if stop is None or stop.get("reason") != "end-stepping-range":
            raise RuntimeError(f"single-step did not complete normally: {stop!r}")

    @_serialized_driver
    async def increment_counter(
        self,
        counter_address: int | str = "counter",
        store_address: int | str | None = None,
    ) -> None:
        """Stop at one verified counter store, execute it, verify its effect, and clean up."""
        self._require_connected()
        if isinstance(counter_address, str) and not _NUMERIC_ADDRESS.fullmatch(counter_address):
            numeric_counter_address = await self.resolve_symbol(counter_address)
        else:
            numeric_counter_address = _parse_address(counter_address, "counter address")
        if store_address is None:
            numeric_store_address = await self.find_store_instruction(numeric_counter_address, "main")
        elif isinstance(store_address, str) and not _NUMERIC_ADDRESS.fullmatch(store_address):
            numeric_store_address = await self.resolve_symbol(store_address)
        else:
            numeric_store_address = _parse_address(store_address, "store address")

        if await self.client.read_execution_state() != "stopped":
            self._frozen = False
            raise RuntimeError("counter increment requires the ARM target to be stopped")
        self._frozen = True
        before = await self._read_uint32(numeric_counter_address)
        breakpoint_id: str | None = None
        operation_error: BaseException | None = None
        location = f"*0x{numeric_store_address:x}"
        try:
            breakpoint_id = await self.set_breakpoint(location)
            if not isinstance(breakpoint_id, str) or not re.fullmatch(r"\d+(?:\.\d+)*", breakpoint_id):
                raise RuntimeError("GDB did not return a valid ID for the temporary counter breakpoint")
            await self.continue_execution(wait_for_stop=True)
            stop = self.client.latest_stop_details()
            if stop is None or stop.get("reason") != "breakpoint-hit":
                raise RuntimeError(f"counter store breakpoint was not the stop reason: {stop!r}")
            hit_id = stop.get("bkptno")
            if not isinstance(hit_id, str) or not re.fullmatch(r"\d+(?:\.\d+)*", hit_id):
                raise RuntimeError(
                    f"GDB stop record did not identify a valid temporary counter breakpoint {breakpoint_id}: {stop!r}"
                )
            if hit_id != breakpoint_id:
                raise RuntimeError(
                    f"target stopped at breakpoint {hit_id}, expected temporary breakpoint {breakpoint_id}"
                )
            pc = _parse_address(
                _parse_gdb_integer(await self.read_pc(), "program counter at breakpoint"),
                "program counter at breakpoint",
            )
            if pc != numeric_store_address:
                raise RuntimeError(
                    f"target stopped at PC 0x{pc:x}, expected counter store 0x{numeric_store_address:x}"
                )
            instruction = await self.client.disassemble_instruction(pc)
            if instruction.get("address") != pc or not re.match(
                r"\s*str(?:\.\w+)?\b", str(instruction.get("text", "")), re.IGNORECASE
            ):
                raise RuntimeError(f"PC 0x{pc:x} is not the expected store instruction: {instruction!r}")

            await self.step_instruction()
            after_pc = _parse_address(
                _parse_gdb_integer(await self.read_pc(), "program counter after single-step"),
                "program counter after single-step",
            )
            if after_pc == numeric_store_address:
                raise RuntimeError("single-step stopped at the same instruction address")
            after = await self._read_uint32(numeric_counter_address)
            expected = (before + 1) & 0xFFFFFFFF
            if after != expected:
                raise RuntimeError(f"counter store postcondition failed: expected {expected}, observed {after}")
        except BaseException as exc:
            operation_error = exc
            breakpoint_id = breakpoint_id or getattr(exc, "arm_breakpoint_id", None)
            if getattr(exc, "arm_breakpoint_insertion_ambiguous", False) and breakpoint_id is None:
                message = "temporary counter breakpoint insertion outcome is ambiguous; no breakpoint ID was returned"
                logger.error("arm_temporary_breakpoint_insertion_ambiguous", location=location)
                if isinstance(exc, asyncio.CancelledError):
                    exc.add_note(message)
                else:
                    raise ARMExecutionCleanupError(
                        exc, (message,), breakpoint_insertion_ambiguous=True
                    ) from exc
            raise
        finally:
            if breakpoint_id:
                await self._cleanup_temporary_breakpoint(breakpoint_id, operation_error)

    async def _read_uint32(self, address: int) -> int:
        value = await self.client.evaluate(f"*(unsigned int*)0x{address:x}")
        result = _parse_gdb_integer(value, "32-bit counter value")
        if result > 0xFFFFFFFF:
            raise RuntimeError(f"GDB returned an out-of-range 32-bit counter value: {value}")
        return result
