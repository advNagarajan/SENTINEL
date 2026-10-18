"""Shared deterministic ARM driver and frame fixtures for unit tests."""

import asyncio
import json
from contextlib import asynccontextmanager

from schemas.pipeline import TransportFrame


def make_frame(
    counter: int,
    sequence: int,
    stop_reason: str,
    *,
    pc: int = 0x4000002C,
    instruction: bool = False,
) -> TransportFrame:
    payload = {
        "environment": "arm",
        "schema_version": "2.0",
        "identity": {
            "runtime_id": "mock-arm",
            "architecture": {"status": "observed", "value": "armv7-a"},
            "cpu": {"status": "observed", "value": "cortex-a15"},
            "firmware": {
                "path": {"status": "observed", "value": "firmware.elf"},
                "sha256": {"status": "observed", "value": "a" * 64},
            },
        },
        "cpu": {
            "registers": {"status": "not_requested"},
            "pc": {"status": "observed", "value": pc},
            "sp": {"status": "not_requested"},
            "status_flags": {},
        },
        "execution": {
            "state": {"status": "observed", "value": "stopped"},
            "current_instruction": (
                {
                    "status": "observed",
                    "value": {"address": pc, "text": "str r2, [r3, #0]", "raw": "mock"},
                }
                if instruction
                else {"status": "not_requested"}
            ),
            "stop_reason": {"status": "observed", "value": stop_reason},
            "breakpoint_hit": {"status": "observed", "value": {"breakpoint_id": "1"}},
        },
        "memory": {
            "status": "observed",
            "regions": [{
                "request_id": "counter",
                "status": "observed",
                "size": 4,
                "address": 0x40000054,
                "requested_address": None,
                "symbol": "counter",
                "raw_bytes_hex": counter.to_bytes(4, "little").hex(),
                "decode": {"type": "uint32", "endianness": "little", "signed": False},
            }],
        },
        "debugging": {"breakpoints": {"status": "not_requested"}},
        "metadata": {
            "frame_seq": sequence,
            "timestamp": float(sequence),
            "capabilities": ["memory"],
            "completeness": {
                "requested": ["memory:counter"],
                "observed": ["memory:counter"],
                "not_requested": [],
                "unavailable": [],
                "failed": [],
            },
            "errors": [],
        },
    }
    return TransportFrame(
        raw_payload=json.dumps(payload).encode(),
        frame_seq=sequence,
        timestamp=float(sequence),
    )


class MockARMDriver:
    def __init__(self) -> None:
        self.value = 591034
        self.sequence = 0
        self.stop_reason = "breakpoint-hit"
        self.phase = "initial"
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def execution_transaction(self):
        async with self._lock:
            yield

    async def connect(self) -> None:
        pass

    async def run_to_breakpoint(self, location: str) -> None:
        if location == "main":
            return
        assert location == "*0x4000002c"
        self.phase = "breakpoint"

    async def read_frame(self) -> TransportFrame:
        self.sequence += 1
        at_store = self.phase == "breakpoint"
        pc = 0x4000002C if at_store else (0x40000030 if self.phase == "after" else 0x40000020)
        return make_frame(
            self.value,
            self.sequence,
            self.stop_reason,
            pc=pc,
            instruction=at_store,
        )

    async def resolve_symbol(self, symbol: str) -> int:
        assert symbol == "counter"
        return 0x40000054

    async def find_store_instruction(self, address: int, symbol: str = "main") -> int:
        assert address == 0x40000054 and symbol == "main"
        return 0x4000002C

    async def run_to_address(self, address: int) -> None:
        assert address == 0x4000002C
        self.phase = "breakpoint"

    async def step_instruction(self) -> None:
        self.value = (self.value + 1) & 0xFFFFFFFF
        self.stop_reason = "end-stepping-range"
        self.phase = "after"

    async def disconnect(self) -> None:
        pass
