"""Configuration for selected ARM runtime observations captured by L1."""

from dataclasses import dataclass
import re


MAX_MEMORY_READ_SIZE = 4096
MAX_ARM_ADDRESS = 0xFFFFFFFF
_SYMBOL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MemoryDecodeConfig:
    """L2 decoding instructions carried as metadata; L1 never decodes memory bytes."""

    value_type: str
    endianness: str
    signed: bool = False


@dataclass(frozen=True)
class MemoryRequest:
    """A selected memory read, identified by exactly one address or symbol."""

    request_id: str
    size: int
    address: int | None = None
    symbol: str | None = None
    decode: MemoryDecodeConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("memory request requires a non-empty request_id")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ValueError("memory request size must be a positive integer")
        if self.size > MAX_MEMORY_READ_SIZE:
            raise ValueError(f"memory request size cannot exceed {MAX_MEMORY_READ_SIZE} bytes")
        if (self.address is None) == (self.symbol is None):
            raise ValueError("memory request must specify exactly one of address or symbol")
        if self.address is not None:
            if isinstance(self.address, bool) or not isinstance(self.address, int):
                raise ValueError("memory request address must be an integer")
            if self.address < 0 or self.address + self.size - 1 > MAX_ARM_ADDRESS:
                raise ValueError("memory request address range is outside the 32-bit ARM address space")
        if self.symbol is not None and not _SYMBOL_IDENTIFIER.fullmatch(self.symbol):
            raise ValueError(f"invalid firmware symbol identifier: {self.symbol!r}")
        if self.decode and self.decode.endianness not in {"little", "big"}:
            raise ValueError("memory decoding endianness must be little or big")


@dataclass(frozen=True)
class ARMObservationConfig:
    """Select which runtime categories ARM L1 queries for each TransportFrame."""

    include_registers: bool = True
    include_pc: bool = True
    include_sp: bool = True
    status_registers: tuple[str, ...] = ("cpsr",)
    include_execution: bool = True
    include_current_instruction: bool = True
    include_breakpoints: bool = True
    memory: tuple[MemoryRequest, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.status_registers)) != len(self.status_registers):
            raise ValueError("status register names must be unique")
        for name in self.status_registers:
            if not _SYMBOL_IDENTIFIER.fullmatch(name):
                raise ValueError(f"invalid status register identifier: {name!r}")
