"""Host-side decoding and validation of NPK Milestone 6 syscall processing.

These helpers observe the real kernel through ``NovaMachine`` memory. They are
harness code, not guest kernel APIs: every syscall must enter through the real
Milestone 3 ``SCALL`` trap path and produce its own core operation and receipt.
A result is only credible when the frame GPRs, syscall state, dedicated receipt,
and any child M5 object receipts agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assembler import AssemblyError, assemble_text
from ..emulator import NovaMachine
from ..isa import EXCEPTION_CODES, PRIVILEGE_LEVELS, WORD_MASK
from ..syscalls import (
    ERROR_BAD_ARGUMENT,
    ERROR_NO_RESOURCE,
    ERROR_NOT_FOUND,
    ERROR_UNSUPPORTED,
    SYSCALL_TABLE,
)
from . import core as core_api
from . import traps
from .layout import (
    OBJECT_RECEIPT_EXT_ADDRESS,
    OBJECT_RECEIPT_EXT_SIZE,
    OBJECT_RECEIPT_SLOTS,
    RECEIPT_SIZE,
    SYSCALL_RECEIPT_SLOTS,
    SYSCALL_RECEIPT_TABLE_ADDRESS,
    SYSCALL_SCRATCH_ADDRESS,
    SYSCALL_SCRATCH_SIZE,
    SYSCALL_STATE_ADDRESS,
    SYSCALL_STATE_SIZE,
    SYSCALL_WRITE_LOG_ADDRESS,
    SYSCALL_WRITE_LOG_SIZE,
)

ERROR_NONE = 0

__all__ = [
    "ERROR_NONE",
    "ERROR_BAD_ARGUMENT",
    "ERROR_UNSUPPORTED",
    "ERROR_NOT_FOUND",
    "ERROR_NO_RESOURCE",
    "SYSCALL_STATE_MAGIC",
    "SYSCALL_STATE_VERSION",
    "SYSCALL_ID_BASE",
    "OBJECT_REQUEST_ID_BASE",
    "SYSCALL_LAYOUT_LIMIT",
    "OP_SYSCALL_SERVICE",
    "SYS_EXIT",
    "SYS_WRITE",
    "SYS_READ",
    "SYS_TIME",
    "SYS_OBJ_CREATE",
    "SYS_OBJ_SIZE",
    "SYS_OBJ_DESTROY",
    "SYS_YIELD",
    "SYSCALL_COUNT",
    "STATUS_OK",
    "STATUS_BAD_STATE",
    "STATUS_BAD_FRAME",
    "STATUS_ADMIT_FAIL",
    "STATUS_ADVANCE_FAIL",
    "STATUS_BAD_OUTCOME",
    "FLAG_HANDLER_CONTEXT",
    "FLAG_EXIT_REQUESTED",
    "FLAG_YIELD_REQUESTED",
    "REASON_SYSCALL_BAD_FRAME",
    "REASON_SYSCALL_BAD_STATE",
    "REASON_SYSCALL_BAD_NUMBER",
    "REASON_SYSCALL_BAD_ARGUMENT",
    "REASON_SYSCALL_BAD_PRIVILEGE",
    "REASON_SYSCALL_BAD_POINTER",
    "REASON_SYSCALL_UNSUPPORTED",
    "REASON_SYSCALL_NO_RESOURCE",
    "REASON_SYSCALL_NOT_FOUND",
    "REASON_SYSCALL_COMMIT",
    "REASON_SYSCALL_CHILD",
    "SyscallState",
    "SyscallResult",
    "NpkSyscallError",
    "word",
    "validate_syscall_layout",
    "decode_syscall_state",
    "decode_syscall_receipts",
    "decode_write_log",
    "decode_scratch",
    "decode_child_receipt_indexes",
    "validate_syscall_state",
    "validate_syscall_receipt",
    "syscall_receipt_expectation",
    "validate_syscall_return",
    "validate_syscall_frame_publication",
    "call_syscall",
]


SYSCALL_STATE_MAGIC = 0x4E504B53  # "NPKS"
SYSCALL_STATE_VERSION = 1
SYSCALL_ID_BASE = 0x53590000
OBJECT_REQUEST_ID_BASE = 0x4F420000
SYSCALL_LAYOUT_LIMIT = 0xA0000

OP_SYSCALL_SERVICE = core_api.OP_SYSCALL_SERVICE

SYS_EXIT = 0
SYS_WRITE = 1
SYS_READ = 2
SYS_TIME = 3
SYS_OBJ_CREATE = 4
SYS_OBJ_SIZE = 5
SYS_OBJ_DESTROY = 6
SYS_YIELD = 7
SYSCALL_COUNT = 8

STATUS_OK = 0
STATUS_BAD_STATE = 1
STATUS_BAD_FRAME = 2
STATUS_ADMIT_FAIL = 3
STATUS_ADVANCE_FAIL = 4
STATUS_BAD_OUTCOME = 5

FLAG_HANDLER_CONTEXT = 1
FLAG_EXIT_REQUESTED = 2
FLAG_YIELD_REQUESTED = 4

REASON_SYSCALL_BAD_FRAME = core_api.REASON_SYSCALL_BAD_FRAME
REASON_SYSCALL_BAD_STATE = core_api.REASON_SYSCALL_BAD_STATE
REASON_SYSCALL_BAD_NUMBER = core_api.REASON_SYSCALL_BAD_NUMBER
REASON_SYSCALL_BAD_ARGUMENT = core_api.REASON_SYSCALL_BAD_ARGUMENT
REASON_SYSCALL_BAD_PRIVILEGE = core_api.REASON_SYSCALL_BAD_PRIVILEGE
REASON_SYSCALL_BAD_POINTER = core_api.REASON_SYSCALL_BAD_POINTER
REASON_SYSCALL_UNSUPPORTED = core_api.REASON_SYSCALL_UNSUPPORTED
REASON_SYSCALL_NO_RESOURCE = core_api.REASON_SYSCALL_NO_RESOURCE
REASON_SYSCALL_NOT_FOUND = core_api.REASON_SYSCALL_NOT_FOUND
REASON_SYSCALL_COMMIT = core_api.REASON_SYSCALL_COMMIT
REASON_SYSCALL_CHILD = core_api.REASON_SYSCALL_CHILD

_VALID_STATUSES = frozenset(
    (STATUS_OK, STATUS_BAD_STATE, STATUS_BAD_FRAME, STATUS_ADMIT_FAIL,
     STATUS_ADVANCE_FAIL, STATUS_BAD_OUTCOME)
)
_VALID_ERRORS = frozenset(
    (ERROR_NONE, ERROR_BAD_ARGUMENT, ERROR_UNSUPPORTED, ERROR_NOT_FOUND,
     ERROR_NO_RESOURCE)
)
_UNUSED_ARGUMENT_COUNT = 0xFF


class NpkSyscallError(ValueError):
    """Raised when syscall observations are missing or inconsistent."""


@dataclass(frozen=True)
class SyscallState:
    """The kernel's 128-byte syscall dispatch observation block."""

    magic: int
    version: int
    init_status: int
    dispatch_count: int
    last_number: int
    last_arg_count: int
    last_op_slot: int
    last_status: int
    last_outcome: int
    last_error: int
    last_result: int
    last_receipt_index: int
    last_reason: int
    child_receipt: int
    flags: int
    write_log_count: int


@dataclass(frozen=True)
class SyscallResult:
    """Observable outcome of one real ``SCALL`` trap."""

    number: int
    a0: int
    a1: int
    state: SyscallState
    receipt: core_api.Receipt | None
    frame: traps.TrapFrame


def word(machine: NovaMachine, address: int) -> int:
    """Read one 64-bit observation word."""
    return machine._read_mem(address, 8)


def validate_syscall_layout() -> None:
    """Check that M6 regions are bounded and do not overwrite M5 evidence."""
    if SYSCALL_STATE_SIZE != 128:
        raise NpkSyscallError("syscall state block is not 128 bytes")
    if SYSCALL_RECEIPT_SLOTS != 8:
        raise NpkSyscallError("syscall receipt capacity changed")
    if RECEIPT_SIZE * SYSCALL_RECEIPT_SLOTS != 1024:
        raise NpkSyscallError("unexpected syscall receipt log size")

    object_ext_end = (OBJECT_RECEIPT_EXT_ADDRESS
                      + OBJECT_RECEIPT_SLOTS * OBJECT_RECEIPT_EXT_SIZE)
    state_end = SYSCALL_STATE_ADDRESS + SYSCALL_STATE_SIZE
    receipt_end = (SYSCALL_RECEIPT_TABLE_ADDRESS
                   + SYSCALL_RECEIPT_SLOTS * RECEIPT_SIZE)
    write_end = SYSCALL_WRITE_LOG_ADDRESS + SYSCALL_WRITE_LOG_SIZE
    scratch_end = SYSCALL_SCRATCH_ADDRESS + SYSCALL_SCRATCH_SIZE

    if SYSCALL_STATE_ADDRESS < object_ext_end:
        raise NpkSyscallError("syscall state overlaps the M5 receipt extension")
    if SYSCALL_RECEIPT_TABLE_ADDRESS < state_end:
        raise NpkSyscallError("syscall receipt log overlaps syscall state")
    if SYSCALL_WRITE_LOG_ADDRESS < receipt_end:
        raise NpkSyscallError("syscall write log overlaps syscall receipts")
    if SYSCALL_SCRATCH_ADDRESS < write_end:
        raise NpkSyscallError("syscall scratch overlaps the write log")
    if scratch_end > SYSCALL_LAYOUT_LIMIT:
        raise NpkSyscallError(
            f"syscall region ends at 0x{scratch_end:x}, past "
            f"0x{SYSCALL_LAYOUT_LIMIT:x}"
        )


def decode_syscall_state(machine: NovaMachine) -> SyscallState:
    """Decode the fixed syscall dispatch state block."""

    def state_word(offset: int) -> int:
        return word(machine, SYSCALL_STATE_ADDRESS + offset)

    return SyscallState(
        magic=state_word(0),
        version=state_word(8),
        init_status=state_word(16),
        dispatch_count=state_word(24),
        last_number=state_word(32),
        last_arg_count=state_word(40),
        last_op_slot=state_word(48),
        last_status=state_word(56),
        last_outcome=state_word(64),
        last_error=state_word(72),
        last_result=state_word(80),
        last_receipt_index=state_word(88),
        last_reason=state_word(96),
        child_receipt=state_word(104),
        flags=state_word(112),
        write_log_count=state_word(120),
    )


def decode_syscall_receipts(machine: NovaMachine) -> tuple[core_api.Receipt, ...]:
    """Decode the dedicated bounded syscall receipt log."""

    receipts = []
    for index in range(SYSCALL_RECEIPT_SLOTS):
        base = SYSCALL_RECEIPT_TABLE_ADDRESS + index * RECEIPT_SIZE
        receipts.append(core_api.Receipt(
            magic=word(machine, base),
            id=word(machine, base + 8),
            type=word(machine, base + 16),
            pu=word(machine, base + 24),
            stage=word(machine, base + 32),
            state=word(machine, base + 40),
            outcome=word(machine, base + 48),
            result=word(machine, base + 56),
            reason=word(machine, base + 64),
            trace=tuple(word(machine, base + 72 + offset * 8)
                        for offset in range(5)),
            reserved=(word(machine, base + 112), word(machine, base + 120)),
        ))
    return tuple(receipts)


def decode_write_log(machine: NovaMachine, *, count: int | None = None) -> bytes:
    """Decode the syscall write sink as bytes, optionally truncated."""
    if count is None:
        count = decode_syscall_state(machine).write_log_count
    if not 0 <= count <= SYSCALL_WRITE_LOG_SIZE:
        raise NpkSyscallError(f"invalid write log count: {count}")
    data = bytearray()
    for offset in range(0, SYSCALL_WRITE_LOG_SIZE, 8):
        data.extend(word(machine, SYSCALL_WRITE_LOG_ADDRESS + offset)
                    .to_bytes(8, "little"))
    return bytes(data[:count])


def decode_scratch(machine: NovaMachine) -> bytes:
    """Decode the uncommitted SYS_WRITE staging buffer."""
    data = bytearray()
    for offset in range(0, SYSCALL_SCRATCH_SIZE, 8):
        data.extend(word(machine, SYSCALL_SCRATCH_ADDRESS + offset)
                    .to_bytes(8, "little"))
    return bytes(data)


def decode_child_receipt_indexes(state: SyscallState) -> tuple[int, int]:
    """Unpack the two M5 child receipt indexes recorded by SYS_OBJ_DESTROY."""
    if state.child_receipt == core_api.NO_INDEX:
        return core_api.NO_INDEX, core_api.NO_INDEX
    return ((state.child_receipt >> 32) & 0xFFFFFFFF,
            state.child_receipt & 0xFFFFFFFF)


def validate_syscall_state(state: SyscallState) -> None:
    """Check invariant properties of one syscall dispatch observation."""
    if state.magic != SYSCALL_STATE_MAGIC:
        raise NpkSyscallError(f"syscall state magic mismatch: 0x{state.magic:x}")
    if state.version != SYSCALL_STATE_VERSION:
        raise NpkSyscallError(f"syscall state version mismatch: {state.version}")
    if state.init_status != 0:
        raise NpkSyscallError(f"syscall initialization failed: {state.init_status}")
    if state.dispatch_count == 0:
        raise NpkSyscallError("no syscall dispatch has been recorded")
    if state.last_status not in _VALID_STATUSES:
        raise NpkSyscallError(f"invalid syscall status: {state.last_status}")
    if state.last_outcome not in core_api.VALID_OUTCOMES:
        raise NpkSyscallError(f"invalid syscall outcome: {state.last_outcome}")
    if state.last_error not in _VALID_ERRORS:
        raise NpkSyscallError(f"invalid ABI error: {state.last_error}")
    if state.last_receipt_index != core_api.NO_INDEX and \
            state.last_receipt_index >= SYSCALL_RECEIPT_SLOTS:
        raise NpkSyscallError(
            f"syscall receipt index out of range: {state.last_receipt_index}"
        )
    if not 0 <= state.write_log_count <= SYSCALL_WRITE_LOG_SIZE:
        raise NpkSyscallError(
            f"write log count out of range: {state.write_log_count}"
        )
    if 0 <= state.last_number < SYSCALL_COUNT:
        expected = len(SYSCALL_TABLE[state.last_number].arguments)
        if state.last_arg_count != expected:
            raise NpkSyscallError(
                f"argument count mismatch: {state.last_arg_count} != {expected}"
            )
    elif state.last_arg_count != _UNUSED_ARGUMENT_COUNT:
        raise NpkSyscallError(
            f"invalid syscall number {state.last_number} has "
            f"argument count {state.last_arg_count}"
        )


def validate_syscall_receipt(receipt: core_api.Receipt,
                             expectation: core_api.ReceiptExpectation) -> None:
    """Validate a syscall receipt against an independent expectation."""
    core_api.validate_receipt(receipt, expectation)
    if receipt.type != OP_SYSCALL_SERVICE:
        raise NpkSyscallError(f"receipt type is not syscall service: {receipt.type}")
    if not 0 <= receipt.reserved[0] <= _UNUSED_ARGUMENT_COUNT:
        raise NpkSyscallError(
            f"receipt has an invalid syscall number: {receipt.reserved[0]}"
        )
    if receipt.reserved[1] not in _VALID_ERRORS:
        raise NpkSyscallError(
            f"receipt has an invalid ABI error: {receipt.reserved[1]}"
        )


def syscall_receipt_expectation(
    state: SyscallState,
    *,
    outcome: int = core_api.OUTCOME_COMPLETE,
    result: int = 0,
    reason: int = core_api.REASON_NONE,
    stage: int | None = core_api.STAGE_COMPLETE,
    syscall_state: int | None = core_api.STATE_COMPLETE,
    trace: tuple[int, ...] | None = core_api.EXPECTED_STAGE_TRACE,
    number: int | None = None,
    error: int = ERROR_NONE,
) -> core_api.ReceiptExpectation:
    """Build the independent receipt expectation for one syscall dispatch."""
    return core_api.ReceiptExpectation(
        id=SYSCALL_ID_BASE + state.dispatch_count,
        type=OP_SYSCALL_SERVICE,
        pu=core_api.PU_IDLE,
        outcome=outcome,
        result=result,
        reason=reason,
        stage=stage,
        state=syscall_state,
        trace=trace,
    )


def validate_syscall_return(a0: int, a1: int) -> None:
    """Check the frozen ``a0`` return / ``a1`` error ABI convention."""
    if a1 not in _VALID_ERRORS:
        raise NpkSyscallError(f"invalid syscall error register: {a1}")
    if a1 != ERROR_NONE and a0 != WORD_MASK:
        raise NpkSyscallError(
            f"failed syscall did not return -1 in a0: 0x{a0:x}"
        )


def validate_syscall_frame_publication(result: SyscallResult) -> None:
    """Check that results were written into the saved frame before MRET."""
    if result.frame.gpr[1] != result.a0 or result.frame.gpr[2] != result.a1:
        raise NpkSyscallError(
            "frame GPRs do not match the live a0/a1 return values"
        )


def call_syscall(machine: NovaMachine, number: int, *args: int,
                 code_address: int = 0xA0000,
                 max_steps: int = 400_000) -> SyscallResult:
    """Run one real ``SCALL`` through the NPK trap and syscall subsystems."""
    validate_syscall_layout()
    if number < 0 or number > 0xFFFF:
        raise NpkSyscallError(f"syscall number out of range: {number}")
    if len(args) > 6:
        raise NpkSyscallError("Nova ABI exposes at most six syscall arguments")

    try:
        code = assemble_text(f"SCALL {number}\nNOP\n")
    except AssemblyError as exc:
        raise NpkSyscallError(f"could not build syscall stub: {exc}") from exc

    before = decode_syscall_state(machine)
    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.priv = PRIVILEGE_LEVELS["user"]
    machine.halted = False
    for index in range(6):
        value = args[index] if index < len(args) else 0
        machine.write_register(index + 1, value & WORD_MASK)

    seen_exception = machine.exception_code != EXCEPTION_CODES["NONE"]
    for _ in range(max_steps):
        if (seen_exception and
                machine.exception_code == EXCEPTION_CODES["NONE"] and
                machine.pc == code_address + 4):
            break
        if machine.halted:
            break
        machine.step()
        if machine.exception_code != EXCEPTION_CODES["NONE"]:
            seen_exception = True
    else:
        raise NpkSyscallError(
            f"syscall did not return to 0x{code_address + 4:x}"
        )
    if machine.exception_code != EXCEPTION_CODES["NONE"]:
        raise NpkSyscallError(f"syscall stub raised exception {machine.exception_code}")
    if machine.pc != code_address + 4:
        raise NpkSyscallError(f"syscall returned to unexpected PC 0x{machine.pc:x}")
    if machine.priv != PRIVILEGE_LEVELS["user"]:
        raise NpkSyscallError(f"MRET did not restore user privilege: {machine.priv}")

    after = decode_syscall_state(machine)
    if after.dispatch_count != before.dispatch_count + 1:
        raise NpkSyscallError(
            "syscall trap did not increment the dispatch counter exactly once"
        )

    a0 = machine.read_register("a0")
    a1 = machine.read_register("a1")
    frame = traps.decode_trap_frame(machine)
    receipts = decode_syscall_receipts(machine)
    receipt = None
    if after.last_receipt_index != core_api.NO_INDEX:
        if after.last_receipt_index >= len(receipts):
            raise NpkSyscallError(
                f"syscall receipt index out of range: {after.last_receipt_index}"
            )
        receipt = receipts[after.last_receipt_index]
    return SyscallResult(number=number, a0=a0, a1=a1, state=after,
                         receipt=receipt, frame=frame)
