"""Host-side decoding and validation of NPK Milestone 4 memory processing.

These helpers read the kernel's fixed memory observation regions through the
reference machine. They are harness code, not guest kernel APIs. A memory
receipt is only meaningful together with the region table, address spaces, page
tables, and raw memory bytes that the operation actually changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assembler import AssemblyError, assemble_text
from ..emulator import NovaMachine
from .core import (
    EXPECTED_STAGE_TRACE,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_REJECTED,
    PU_IDLE,
    RECEIPT_MAGIC,
    REASON_NONE,
    Receipt,
    ReceiptExpectation,
    STAGE_ADMIT,
    STAGE_COMMIT,
    STAGE_COMPLETE,
    STAGE_EXECUTE,
    STAGE_NONE,
    STAGE_VALIDATE,
    STATE_ADMITTED,
    STATE_COMMITTED,
    STATE_COMPLETE,
    STATE_EXECUTED,
    STATE_FAILED,
    STATE_REJECTED,
    validate_receipt_structure,
)
from .layout import (
    ADDRESS_SPACE_SIZE,
    ADDRESS_SPACE_SLOTS,
    ADDRESS_SPACE_TABLE_ADDRESS,
    MEMORY_ENTRY_ADDRESS,
    MEMORY_PAGE_SIZE,
    MEMORY_REGION_SIZE,
    MEMORY_REGION_SLOTS,
    MEMORY_REGION_TABLE_ADDRESS,
    MEMORY_REQUEST_ADDRESS,
    MEMORY_REQUEST_SIZE,
    MEMORY_RESPONSE_ADDRESS,
    MEMORY_RESPONSE_SIZE,
    MEMORY_RECEIPT_SLOTS,
    MEMORY_RECEIPT_TABLE_ADDRESS,
    MEMORY_STATE_ADDRESS,
    MEMORY_STATE_SIZE,
    PHYSICAL_ARENA_BASE,
    PHYSICAL_ARENA_SIZE,
    RECEIPT_SIZE,
)

__all__ = [
    "MEMORY_STATE_MAGIC",
    "MEMORY_STATE_VERSION",
    "MEMORY_REQUEST_MAGIC",
    "MEMORY_RESPONSE_MAGIC",
    "MEMORY_RECEIPT_SIZE",
    "OP_MEMORY_ALLOCATE",
    "OP_MEMORY_RELEASE",
    "OP_MEMORY_MAP",
    "OP_MEMORY_UNMAP",
    "OP_MEMORY_QUERY",
    "REASON_MEMORY_BAD_REQUEST",
    "REASON_MEMORY_BAD_REGION",
    "REASON_MEMORY_BAD_SIZE",
    "REASON_MEMORY_BAD_ALIGN",
    "REASON_MEMORY_OVERLAP",
    "REASON_MEMORY_EXHAUSTED",
    "REASON_MEMORY_OWNER",
    "REASON_MEMORY_STATE",
    "REASON_MEMORY_BAD_MAPPING",
    "REASON_MEMORY_UNSUPPORTED",
    "REASON_MEMORY_BAD_ADDRESS_SPACE",
    "REASON_MEMORY_PTE_CONFLICT",
    "REQUEST_OK",
    "REQUEST_BAD_REQUEST",
    "REQUEST_BAD_OP",
    "REQUEST_ADMIT_FAIL",
    "REQUEST_ADVANCE_FAIL",
    "REQUEST_BAD_OUTCOME",
    "REQUEST_REJECTED",
    "REQUEST_FAILED",
    "REGION_FREE",
    "REGION_ALLOCATED",
    "REGION_PAGE_TABLE",
    "REGION_TYPE_NORMAL",
    "REGION_TYPE_PAGE_TABLE",
    "ADDRESS_SPACE_FREE",
    "ADDRESS_SPACE_ACTIVE",
    "PERM_READ",
    "PERM_WRITE",
    "PERM_EXEC",
    "PERM_USER",
    "PTE_VALID",
    "PTE_READ",
    "PTE_WRITE",
    "PTE_EXEC",
    "PTE_USER",
    "PTE_PPN_MASK",
    "ALLOCATION_TAG",
    "NpkMemoryError",
    "MemoryState",
    "MemoryRegion",
    "AddressSpace",
    "MemoryRequest",
    "MemoryResponse",
    "MemorySnapshot",
    "word",
    "decode_memory_entry",
    "decode_memory_state",
    "decode_memory_regions",
    "decode_address_spaces",
    "decode_memory_request",
    "decode_memory_response",
    "decode_memory_receipts",
    "decode_memory_snapshot",
    "validate_memory_layout",
    "validate_memory_state",
    "validate_memory_response",
    "validate_complete_memory_receipt",
    "validate_rejected_memory_receipt",
    "validate_failed_memory_receipt",
    "write_memory_request",
    "call_memory_entry",
    "call_memory_request",
    "memory_receipt_expectation",
]


MEMORY_STATE_MAGIC = 0x4E504B4D  # "NPKM"
MEMORY_STATE_VERSION = 4
MEMORY_REQUEST_MAGIC = 0x4E504B51  # "NPKQ"
MEMORY_RESPONSE_MAGIC = 0x4E504B5A  # "NPKZ"
MEMORY_RECEIPT_SIZE = RECEIPT_SIZE

OP_MEMORY_ALLOCATE = 4
OP_MEMORY_RELEASE = 5
OP_MEMORY_MAP = 6
OP_MEMORY_UNMAP = 7
OP_MEMORY_QUERY = 8

REASON_MEMORY_BAD_REQUEST = 18
REASON_MEMORY_BAD_REGION = 19
REASON_MEMORY_BAD_SIZE = 20
REASON_MEMORY_BAD_ALIGN = 21
REASON_MEMORY_OVERLAP = 22
REASON_MEMORY_EXHAUSTED = 23
REASON_MEMORY_OWNER = 24
REASON_MEMORY_STATE = 25
REASON_MEMORY_BAD_MAPPING = 26
REASON_MEMORY_UNSUPPORTED = 27
REASON_MEMORY_BAD_ADDRESS_SPACE = 28
REASON_MEMORY_PTE_CONFLICT = 29

REQUEST_OK = 0
REQUEST_BAD_REQUEST = 1
REQUEST_BAD_OP = 2
REQUEST_ADMIT_FAIL = 3
REQUEST_ADVANCE_FAIL = 4
REQUEST_BAD_OUTCOME = 5
REQUEST_REJECTED = 6
REQUEST_FAILED = 7

REGION_FREE = 0
REGION_ALLOCATED = 1
REGION_PAGE_TABLE = 2

REGION_TYPE_NORMAL = 1
REGION_TYPE_PAGE_TABLE = 2

ADDRESS_SPACE_FREE = 0
ADDRESS_SPACE_ACTIVE = 1

PERM_READ = 1
PERM_WRITE = 2
PERM_EXEC = 4
PERM_USER = 8

PTE_VALID = 0x1
PTE_READ = 0x2
PTE_WRITE = 0x4
PTE_EXEC = 0x8
PTE_USER = 0x10
PTE_PPN_MASK = 0x000FFFFFFFFFF000

ALLOCATION_TAG = 0x4E504B4D414C4C00

_VALID_REQUEST_STATUSES = frozenset(range(REQUEST_OK, REQUEST_FAILED + 1))
_VALID_REASONS = frozenset(range(REASON_NONE, REASON_MEMORY_PTE_CONFLICT + 1))


class NpkMemoryError(ValueError):
    """Raised when M4 memory observation state is inconsistent."""


@dataclass(frozen=True)
class MemoryState:
    magic: int
    version: int
    init_status: int
    arena_base: int
    arena_size: int
    page_size: int
    region_slots: int
    region_count: int
    address_space_slots: int
    mapping_count: int
    next_physical: int
    allocation_epoch: int
    invalidation_epoch: int
    next_region_id: int
    last_status: int
    last_reason: int


@dataclass(frozen=True)
class MemoryRegion:
    state: int
    id: int
    base: int
    size: int
    owner: int
    type: int
    flags: int
    reserved: int


@dataclass(frozen=True)
class AddressSpace:
    state: int
    owner: int
    root: int
    mapping_count: int


@dataclass(frozen=True)
class MemoryRequest:
    magic: int
    operation: int
    id: int
    owner: int
    address: int
    size: int
    flags: int
    aux: int


@dataclass(frozen=True)
class MemoryResponse:
    magic: int
    status: int
    operation: int
    reason: int
    result: int
    aux: int
    receipt_index: int
    slot: int


@dataclass(frozen=True)
class MemorySnapshot:
    state: MemoryState
    regions: tuple[MemoryRegion, ...]
    address_spaces: tuple[AddressSpace, ...]
    request: MemoryRequest
    response: MemoryResponse
    receipts: tuple[Receipt, ...]


def word(machine: NovaMachine, address: int) -> int:
    """Read one 64-bit observation word."""
    return machine._read_mem(address, 8)


def decode_memory_entry(machine: NovaMachine) -> int:
    """Return the published address of the typed memory request entry."""
    return word(machine, MEMORY_ENTRY_ADDRESS)


def decode_memory_state(machine: NovaMachine) -> MemoryState:
    """Decode the fixed memory-manager state block."""
    def state_word(offset: int) -> int:
        return word(machine, MEMORY_STATE_ADDRESS + offset)

    return MemoryState(
        magic=state_word(0),
        version=state_word(8),
        init_status=state_word(16),
        arena_base=state_word(24),
        arena_size=state_word(32),
        page_size=state_word(40),
        region_slots=state_word(48),
        region_count=state_word(56),
        address_space_slots=state_word(64),
        mapping_count=state_word(72),
        next_physical=state_word(80),
        allocation_epoch=state_word(88),
        invalidation_epoch=state_word(96),
        next_region_id=state_word(104),
        last_status=state_word(112),
        last_reason=state_word(120),
    )


def decode_memory_regions(machine: NovaMachine) -> tuple[MemoryRegion, ...]:
    """Decode every fixed region descriptor."""
    regions = []
    for index in range(MEMORY_REGION_SLOTS):
        base = MEMORY_REGION_TABLE_ADDRESS + index * MEMORY_REGION_SIZE
        regions.append(MemoryRegion(
            state=word(machine, base + 0),
            id=word(machine, base + 8),
            base=word(machine, base + 16),
            size=word(machine, base + 24),
            owner=word(machine, base + 32),
            type=word(machine, base + 40),
            flags=word(machine, base + 48),
            reserved=word(machine, base + 56),
        ))
    return tuple(regions)


def decode_address_spaces(machine: NovaMachine) -> tuple[AddressSpace, ...]:
    """Decode every software address-space root."""
    spaces = []
    for index in range(ADDRESS_SPACE_SLOTS):
        base = ADDRESS_SPACE_TABLE_ADDRESS + index * ADDRESS_SPACE_SIZE
        spaces.append(AddressSpace(
            state=word(machine, base + 0),
            owner=word(machine, base + 8),
            root=word(machine, base + 16),
            mapping_count=word(machine, base + 24),
        ))
    return tuple(spaces)


def decode_memory_request(machine: NovaMachine) -> MemoryRequest:
    """Decode the shared memory request block."""
    def request_word(offset: int) -> int:
        return word(machine, MEMORY_REQUEST_ADDRESS + offset)

    return MemoryRequest(
        magic=request_word(0),
        operation=request_word(8),
        id=request_word(16),
        owner=request_word(24),
        address=request_word(32),
        size=request_word(40),
        flags=request_word(48),
        aux=request_word(56),
    )


def decode_memory_response(machine: NovaMachine) -> MemoryResponse:
    """Decode the shared memory response block."""
    def response_word(offset: int) -> int:
        return word(machine, MEMORY_RESPONSE_ADDRESS + offset)

    return MemoryResponse(
        magic=response_word(0),
        status=response_word(8),
        operation=response_word(16),
        reason=response_word(24),
        result=response_word(32),
        aux=response_word(40),
        receipt_index=response_word(48),
        slot=response_word(56),
    )


def decode_memory_receipts(machine: NovaMachine) -> tuple[Receipt, ...]:
    """Decode the dedicated M4 receipt log."""
    receipts = []
    for index in range(MEMORY_RECEIPT_SLOTS):
        base = MEMORY_RECEIPT_TABLE_ADDRESS + index * MEMORY_RECEIPT_SIZE
        receipts.append(Receipt(
            magic=word(machine, base + 0),
            id=word(machine, base + 8),
            type=word(machine, base + 16),
            pu=word(machine, base + 24),
            stage=word(machine, base + 32),
            state=word(machine, base + 40),
            outcome=word(machine, base + 48),
            result=word(machine, base + 56),
            reason=word(machine, base + 64),
            trace=tuple(word(machine, base + 72 + step * 8) for step in range(5)),
            reserved=(word(machine, base + 112), word(machine, base + 120)),
        ))
    return tuple(receipts)


def decode_memory_snapshot(machine: NovaMachine) -> MemorySnapshot:
    """Decode all M4 observation regions into one immutable snapshot."""
    return MemorySnapshot(
        state=decode_memory_state(machine),
        regions=decode_memory_regions(machine),
        address_spaces=decode_address_spaces(machine),
        request=decode_memory_request(machine),
        response=decode_memory_response(machine),
        receipts=decode_memory_receipts(machine),
    )


def validate_memory_layout() -> None:
    """Check that all M4 observation regions are distinct and bounded."""
    ranges = (
        ("entry", MEMORY_ENTRY_ADDRESS, 8),
        ("state", MEMORY_STATE_ADDRESS, MEMORY_STATE_SIZE),
        ("regions", MEMORY_REGION_TABLE_ADDRESS,
         MEMORY_REGION_SIZE * MEMORY_REGION_SLOTS),
        ("address spaces", ADDRESS_SPACE_TABLE_ADDRESS,
         ADDRESS_SPACE_SIZE * ADDRESS_SPACE_SLOTS),
        ("request", MEMORY_REQUEST_ADDRESS, MEMORY_REQUEST_SIZE),
        ("response", MEMORY_RESPONSE_ADDRESS, MEMORY_RESPONSE_SIZE),
        ("receipts", MEMORY_RECEIPT_TABLE_ADDRESS,
         MEMORY_RECEIPT_SIZE * MEMORY_RECEIPT_SLOTS),
    )
    ordered = sorted(ranges, key=lambda item: item[1])
    for index in range(len(ordered) - 1):
        left_name, left_base, left_size = ordered[index]
        right_name, right_base, _ = ordered[index + 1]
        left_end = left_base + left_size
        if left_end > right_base:
            raise NpkMemoryError(
                f"{left_name} region [0x{left_base:x}, 0x{left_end:x}) overlaps "
                f"{right_name} at 0x{right_base:x}"
            )

    receipt_end = MEMORY_RECEIPT_TABLE_ADDRESS + MEMORY_RECEIPT_SIZE * MEMORY_RECEIPT_SLOTS
    if receipt_end > 0xA0000:
        raise NpkMemoryError("M4 receipt log reaches the guest test code area")
    if PHYSICAL_ARENA_BASE <= receipt_end:
        raise NpkMemoryError("physical arena overlaps kernel observation regions")
    if PHYSICAL_ARENA_BASE + PHYSICAL_ARENA_SIZE > 0x100000:
        raise NpkMemoryError("physical arena exceeds the declared 20-bit test window")
    if MEMORY_PAGE_SIZE != 0x1000:
        raise NpkMemoryError("NPK M4 requires 4 KiB pages")


def validate_memory_state(state: MemoryState) -> None:
    """Validate boot-time memory state without trusting the kernel status."""
    if state.magic != MEMORY_STATE_MAGIC:
        raise NpkMemoryError(f"memory state magic mismatch: 0x{state.magic:x}")
    if state.version != MEMORY_STATE_VERSION:
        raise NpkMemoryError(f"memory state version mismatch: {state.version}")
    if state.init_status != 0:
        raise NpkMemoryError(f"memory init failed with status {state.init_status}")
    if state.arena_base != PHYSICAL_ARENA_BASE:
        raise NpkMemoryError(f"unexpected arena base 0x{state.arena_base:x}")
    if state.arena_size != PHYSICAL_ARENA_SIZE:
        raise NpkMemoryError(f"unexpected arena size 0x{state.arena_size:x}")
    if state.page_size != MEMORY_PAGE_SIZE:
        raise NpkMemoryError(f"unexpected page size 0x{state.page_size:x}")
    if state.region_slots != MEMORY_REGION_SLOTS:
        raise NpkMemoryError(f"unexpected region slots {state.region_slots}")
    if state.address_space_slots != ADDRESS_SPACE_SLOTS:
        raise NpkMemoryError(f"unexpected address spaces {state.address_space_slots}")
    if state.region_count > state.region_slots:
        raise NpkMemoryError("region count exceeds the fixed table")
    if not state.arena_base <= state.next_physical <= state.arena_base + state.arena_size:
        raise NpkMemoryError(f"next_physical outside the arena: 0x{state.next_physical:x}")
    if state.last_status not in _VALID_REQUEST_STATUSES:
        raise NpkMemoryError(f"invalid last request status {state.last_status}")
    if state.last_reason not in _VALID_REASONS:
        raise NpkMemoryError(f"invalid last reason {state.last_reason}")


def validate_memory_response(response: MemoryResponse, *, status: int,
                             operation: int, reason: int = REASON_NONE,
                             result: int = 0) -> None:
    """Validate one memory response against an independent expectation."""
    if response.magic != MEMORY_RESPONSE_MAGIC:
        raise NpkMemoryError(f"response magic mismatch: 0x{response.magic:x}")
    checks = (
        ("status", response.status, status),
        ("operation", response.operation, operation),
        ("reason", response.reason, reason),
        ("result", response.result, result),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise NpkMemoryError(f"response {name} mismatch: {actual} != {expected}")
    if response.slot >= 4:
        raise NpkMemoryError(f"response slot outside the core: {response.slot}")
    if response.receipt_index >= MEMORY_RECEIPT_SLOTS:
        raise NpkMemoryError(f"receipt index out of range: {response.receipt_index}")
    if response.aux != response.receipt_index:
        raise NpkMemoryError("response aux is not the receipt index")


def write_memory_request(machine: NovaMachine, *, operation: int, operation_id: int,
                         owner: int, address: int = 0, size: int = 0,
                         flags: int = 0, aux: int = 0) -> None:
    """Publish one 64-byte memory request block."""
    values = (
        MEMORY_REQUEST_MAGIC,
        operation,
        operation_id,
        owner,
        address,
        size,
        flags,
        aux,
    )
    for index, value in enumerate(values):
        machine._write_mem(MEMORY_REQUEST_ADDRESS + index * 8, value, 8)


def call_memory_entry(machine: NovaMachine, *, code_address: int = 0xA0000,
                      max_steps: int = 400_000) -> None:
    """Branch to the published kernel entry using real guest instructions.

    ``BL`` expands through an instruction scratch register, so the stub sets
    ``ra`` explicitly to the ``HALT`` that follows the call. This helper leaves
    the request and response blocks untouched.
    """
    entry = decode_memory_entry(machine)
    if entry == 0:
        raise NpkMemoryError("the kernel did not publish a memory request entry")

    try:
        probe = assemble_text(
            f"LI ra, {code_address}\nBL 0x{entry:x}\nHALT\n"
        )
        return_address = code_address + len(probe) - 4
        code = assemble_text(
            f"LI ra, {return_address}\nBL 0x{entry:x}\nHALT\n"
        )
    except AssemblyError as exc:
        raise NpkMemoryError(f"could not build memory call stub: {exc}") from exc

    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.halted = False
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    if not machine.halted:
        raise NpkMemoryError("memory request did not return")
    if machine.exception_code != 0:
        raise NpkMemoryError(f"memory request raised exception {machine.exception_code}")


def call_memory_request(machine: NovaMachine, *, operation: int, operation_id: int,
                        owner: int, address: int = 0, size: int = 0,
                        flags: int = 0, aux: int = 0,
                        code_address: int = 0xA0000,
                        max_steps: int = 400_000) -> MemoryResponse:
    """Run one typed memory request through the real processed kernel entry."""
    write_memory_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        owner=owner,
        address=address,
        size=size,
        flags=flags,
        aux=aux,
    )
    call_memory_entry(machine, code_address=code_address, max_steps=max_steps)
    return decode_memory_response(machine)


def memory_receipt_expectation(
    operation_id: int,
    operation: int,
    outcome: int,
    *,
    result: int = 0,
    reason: int = REASON_NONE,
    pu: int = PU_IDLE,
    stage: int | None = None,
    state: int | None = None,
    trace: tuple[int, ...] | None = None,
) -> ReceiptExpectation:
    """Build a receipt expectation for one M4 lifecycle outcome."""
    return ReceiptExpectation(
        id=operation_id,
        type=operation,
        pu=pu,
        outcome=outcome,
        result=result,
        reason=reason,
        stage=stage,
        state=state,
        trace=trace,
    )


def validate_complete_memory_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a successful M4 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_COMPLETE:
        raise NpkMemoryError("memory receipt is not complete")
    if receipt.stage != STAGE_COMPLETE or receipt.state != STATE_COMPLETE:
        raise NpkMemoryError("complete memory receipt lacks the complete stage/state")
    if receipt.trace != EXPECTED_STAGE_TRACE:
        raise NpkMemoryError(f"incomplete memory lifecycle trace: {receipt.trace}")


def validate_rejected_memory_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a validation-rejected M4 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_REJECTED:
        raise NpkMemoryError("memory receipt is not rejected")
    if receipt.stage != STAGE_VALIDATE or receipt.state != STATE_REJECTED:
        raise NpkMemoryError("rejected memory receipt advanced past validation")
    if receipt.trace != (STAGE_ADMIT, STAGE_VALIDATE, 0, 0, 0):
        raise NpkMemoryError(f"rejected memory lifecycle trace: {receipt.trace}")


def validate_failed_memory_receipt(receipt: Receipt, *, stage: int) -> None:
    """Check structural invariants of an execution- or commit-failed receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_FAILED:
        raise NpkMemoryError("memory receipt is not failed")
    if receipt.state != STATE_FAILED or stage not in (STAGE_EXECUTE, STAGE_COMMIT):
        raise NpkMemoryError("failed memory receipt has the wrong stage/state")
    if receipt.stage != stage:
        raise NpkMemoryError(f"failed memory stage mismatch: {receipt.stage} != {stage}")
