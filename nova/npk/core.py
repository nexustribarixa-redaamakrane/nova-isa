"""Host-side decoding and validation of the NPK processing core.

These helpers read the kernel's fixed observation regions through the reference
machine. They are harness code, not guest kernel APIs. Validation reconstructs
processing correctness from the recorded operation, PU, stage, outcome, result,
and reason fields instead of trusting a success flag.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..emulator import NovaMachine
from .layout import (
    CORE_STATE_ADDRESS,
    OP_SNAPSHOT_ADDRESS,
    OP_SNAPSHOT_SIZE,
    OP_SNAPSHOT_SLOTS,
    RECEIPT_SIZE,
    RECEIPT_SLOTS,
    RECEIPT_TABLE_ADDRESS,
)

__all__ = [
    "CORE_MAGIC",
    "CORE_VERSION",
    "RECEIPT_MAGIC",
    "NO_INDEX",
    "OP_BOOT_VERIFY",
    "OP_CORE_DIAGNOSTIC",
    "OP_TRAP_PROCESS",
    "OP_MEMORY_ALLOCATE",
    "OP_MEMORY_RELEASE",
    "OP_MEMORY_MAP",
    "OP_MEMORY_UNMAP",
    "OP_MEMORY_QUERY",
    "OP_OBJECT_CREATE",
    "OP_OBJECT_DESTROY",
    "OP_OBJECT_OPEN",
    "OP_OBJECT_CLOSE",
    "OP_OBJECT_QUERY",
    "OP_OBJECT_RETAIN",
    "OP_OBJECT_RELEASE",
    "OP_OBJECT_DIRECTORY_INSERT",
    "OP_OBJECT_DIRECTORY_REMOVE",
    "OP_OBJECT_DIRECTORY_LOOKUP",
    "OP_OBJECT_TYPE_REGISTER",
    "OP_OBJECT_TYPE_QUERY",
    "OP_SYSCALL_SERVICE",
    "OP_DEVICE_REGISTER",
    "OP_DEVICE_UNREGISTER",
    "OP_DEVICE_QUERY",
    "OP_DEVICE_WRITE",
    "OP_TIMER_CREATE",
    "OP_TIMER_CANCEL",
    "OP_TIMER_INSPECT",
    "OP_TIMER_EXPIRE",
    "OP_TIMER_DELIVER",
    "OP_TASK_CREATE",
    "OP_TASK_TERMINATE",
    "OP_TASK_YIELD",
    "OP_TASK_BLOCK",
    "OP_TASK_WAKE",
    "OP_TASK_QUERY",
    "OP_TASK_DESTROY",
    "BOOT_INPUT",
    "BOOT_OUTPUT",
    "DIAG_OUTPUT",
    "DIAG_SUCCESS",
    "DIAG_REJECT",
    "DIAG_EXEC_FAIL",
    "DIAG_COMMIT_FAIL",
    "STAGE_NONE",
    "STAGE_ADMIT",
    "STAGE_VALIDATE",
    "STAGE_EXECUTE",
    "STAGE_COMMIT",
    "STAGE_COMPLETE",
    "EXPECTED_STAGE_TRACE",
    "STATE_FREE",
    "STATE_ADMITTED",
    "STATE_VALIDATED",
    "STATE_EXECUTED",
    "STATE_COMMITTED",
    "STATE_COMPLETE",
    "STATE_REJECTED",
    "STATE_FAILED",
    "OUTCOME_NONE",
    "OUTCOME_PENDING",
    "OUTCOME_COMPLETE",
    "OUTCOME_REJECTED",
    "OUTCOME_FAILED",
    "PU_IDLE",
    "PU_BUSY",
    "REASON_NONE",
    "REASON_BAD_ID",
    "REASON_BAD_TYPE",
    "REASON_DUPLICATE_ID",
    "REASON_NO_SLOT",
    "REASON_UNIT_UNAVAILABLE",
    "REASON_NO_RECEIPT",
    "REASON_BAD_INPUT",
    "REASON_BOOT_MARKER_MISMATCH",
    "REASON_DIAG_REJECT",
    "REASON_DIAG_EXEC",
    "REASON_DIAG_COMMIT",
    "REASON_CONTRACT",
    "REASON_BAD_OUTPUT",
    "REASON_TRAP_BAD_FRAME",
    "REASON_TRAP_BAD_ECODE",
    "REASON_TRAP_UNSUPPORTED",
    "REASON_TRAP_STATE",
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
    "REASON_OBJECT_BAD_REQUEST",
    "REASON_OBJECT_BAD_TYPE",
    "REASON_OBJECT_BAD_HANDLE",
    "REASON_OBJECT_BAD_OBJECT",
    "REASON_OBJECT_BAD_OWNER",
    "REASON_OBJECT_BAD_PERMISSION",
    "REASON_OBJECT_STATE",
    "REASON_OBJECT_EXHAUSTED",
    "REASON_OBJECT_DUPLICATE",
    "REASON_OBJECT_BAD_PARENT",
    "REASON_OBJECT_PAYLOAD",
    "REASON_OBJECT_UNSUPPORTED",
    "REASON_OBJECT_BAD_NAME",
    "REASON_OBJECT_NO_ENTRY",
    "REASON_OBJECT_NOT_DIRECTORY",
    "REASON_OBJECT_BAD_PATH",
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
    "REASON_DEVICE_BAD_REQUEST",
    "REASON_DEVICE_BAD_KIND",
    "REASON_DEVICE_UNSUPPORTED",
    "REASON_DEVICE_NOT_FOUND",
    "REASON_DEVICE_DUPLICATE",
    "REASON_DEVICE_EXHAUSTED",
    "REASON_DEVICE_STATE",
    "REASON_DEVICE_CAPABILITY",
    "REASON_DEVICE_BAD_PARAMETER",
    "REASON_DEVICE_OWNER",
    "REASON_DEVICE_OBJECT",
    "REASON_TIMER_BAD_REQUEST",
    "REASON_TIMER_NOT_FOUND",
    "REASON_TIMER_DUPLICATE",
    "REASON_TIMER_EXHAUSTED",
    "REASON_TIMER_STATE",
    "REASON_TIMER_NOT_DUE",
    "REASON_TIMER_BAD_INTERVAL",
    "REASON_TIMER_EVENT",
    "REASON_TIMER_UNSUPPORTED",
    "REASON_DEVICE_BACKEND",
    "REASON_TASK_BAD_REQUEST",
    "REASON_TASK_BAD_ID",
    "REASON_TASK_BAD_STATE",
    "REASON_TASK_NOT_FOUND",
    "REASON_TASK_DUPLICATE",
    "REASON_TASK_EXHAUSTED",
    "REASON_TASK_BAD_ENTRY",
    "REASON_TASK_BAD_STACK",
    "REASON_TASK_BAD_CONTEXT",
    "REASON_TASK_BAD_TRANSITION",
    "REASON_TASK_TERMINATED",
    "REASON_TASK_NOT_RUNNING",
    "REASON_TASK_NOT_BLOCKED",
    "REASON_TASK_NOT_READY",
    "REASON_TASK_UNSUPPORTED",
    "ERR_OK",
    "ERR_BAD_ID",
    "ERR_BAD_TYPE",
    "ERR_DUPLICATE_ID",
    "ERR_NO_SLOT",
    "ERR_UNIT_UNAVAILABLE",
    "ERR_NO_RECEIPT",
    "ERR_BAD_SLOT",
    "ERR_NOT_ACTIVE",
    "ERR_OWNERSHIP",
    "ERR_INVALID_TRANSITION",
    "ERR_UNIT_BUSY",
    "ERR_UNIT_IDLE",
    "ERR_BAD_PU",
    "ERR_INCOMPLETE",
    "NpkCoreError",
    "CoreState",
    "OperationSnapshot",
    "Receipt",
    "CoreSnapshot",
    "ReceiptExpectation",
    "decode_core_snapshot",
    "validate_core_state",
    "validate_receipt_structure",
    "validate_receipt",
]


CORE_MAGIC = 0x4E504B43  # "NPKC"
CORE_VERSION = 2
RECEIPT_MAGIC = 0x4E504B52  # "NPKR"
NO_INDEX = 0xFFFFFFFFFFFFFFFF

OP_BOOT_VERIFY = 1
OP_CORE_DIAGNOSTIC = 2
OP_TRAP_PROCESS = 3
OP_MEMORY_ALLOCATE = 4
OP_MEMORY_RELEASE = 5
OP_MEMORY_MAP = 6
OP_MEMORY_UNMAP = 7
OP_MEMORY_QUERY = 8
OP_OBJECT_CREATE = 9
OP_OBJECT_DESTROY = 10
OP_OBJECT_OPEN = 11
OP_OBJECT_CLOSE = 12
OP_OBJECT_QUERY = 13
OP_OBJECT_RETAIN = 14
OP_OBJECT_RELEASE = 15
OP_OBJECT_DIRECTORY_INSERT = 16
OP_OBJECT_DIRECTORY_REMOVE = 17
OP_OBJECT_DIRECTORY_LOOKUP = 18
OP_OBJECT_TYPE_REGISTER = 19
OP_OBJECT_TYPE_QUERY = 20
OP_SYSCALL_SERVICE = 21
OP_DEVICE_REGISTER = 22
OP_DEVICE_UNREGISTER = 23
OP_DEVICE_QUERY = 24
OP_DEVICE_WRITE = 25
OP_TIMER_CREATE = 26
OP_TIMER_CANCEL = 27
OP_TIMER_INSPECT = 28
OP_TIMER_EXPIRE = 29
OP_TIMER_DELIVER = 30
OP_TASK_CREATE = 31
OP_TASK_TERMINATE = 32
OP_TASK_YIELD = 33
OP_TASK_BLOCK = 34
OP_TASK_WAKE = 35
OP_TASK_QUERY = 36
OP_TASK_DESTROY = 37
OP_EXEC_LOAD = 38
OP_EXEC_START = 39
OP_EXEC_QUERY = 40
OP_EXEC_RELEASE = 41

BOOT_INPUT = 0x4E504B31
BOOT_OUTPUT = 0x4E504B32
DIAG_OUTPUT = 0x4E504B44

DIAG_SUCCESS = 0
DIAG_REJECT = 1
DIAG_EXEC_FAIL = 2
DIAG_COMMIT_FAIL = 3

STAGE_NONE = 0
STAGE_ADMIT = 1
STAGE_VALIDATE = 2
STAGE_EXECUTE = 3
STAGE_COMMIT = 4
STAGE_COMPLETE = 5
EXPECTED_STAGE_TRACE = (
    STAGE_ADMIT,
    STAGE_VALIDATE,
    STAGE_EXECUTE,
    STAGE_COMMIT,
    STAGE_COMPLETE,
)

STATE_FREE = 0
STATE_ADMITTED = 1
STATE_VALIDATED = 2
STATE_EXECUTED = 3
STATE_COMMITTED = 4
STATE_COMPLETE = 5
STATE_REJECTED = 6
STATE_FAILED = 7

OUTCOME_NONE = 0
OUTCOME_PENDING = 1
OUTCOME_COMPLETE = 2
OUTCOME_REJECTED = 3
OUTCOME_FAILED = 4

PU_IDLE = 0
PU_BUSY = 1

REASON_NONE = 0
REASON_BAD_ID = 1
REASON_BAD_TYPE = 2
REASON_DUPLICATE_ID = 3
REASON_NO_SLOT = 4
REASON_UNIT_UNAVAILABLE = 5
REASON_NO_RECEIPT = 6
REASON_BAD_INPUT = 7
REASON_BOOT_MARKER_MISMATCH = 8
REASON_DIAG_REJECT = 9
REASON_DIAG_EXEC = 10
REASON_DIAG_COMMIT = 11
REASON_CONTRACT = 12
REASON_BAD_OUTPUT = 13
REASON_TRAP_BAD_FRAME = 14
REASON_TRAP_BAD_ECODE = 15
REASON_TRAP_UNSUPPORTED = 16
REASON_TRAP_STATE = 17
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
REASON_OBJECT_BAD_REQUEST = 30
REASON_OBJECT_BAD_TYPE = 31
REASON_OBJECT_BAD_HANDLE = 32
REASON_OBJECT_BAD_OBJECT = 33
REASON_OBJECT_BAD_OWNER = 34
REASON_OBJECT_BAD_PERMISSION = 35
REASON_OBJECT_STATE = 36
REASON_OBJECT_EXHAUSTED = 37
REASON_OBJECT_DUPLICATE = 38
REASON_OBJECT_BAD_PARENT = 39
REASON_OBJECT_PAYLOAD = 40
REASON_OBJECT_UNSUPPORTED = 41
REASON_OBJECT_BAD_NAME = 42
REASON_OBJECT_NO_ENTRY = 43
REASON_OBJECT_NOT_DIRECTORY = 44
REASON_OBJECT_BAD_PATH = 45
REASON_SYSCALL_BAD_FRAME = 46
REASON_SYSCALL_BAD_STATE = 47
REASON_SYSCALL_BAD_NUMBER = 48
REASON_SYSCALL_BAD_ARGUMENT = 49
REASON_SYSCALL_BAD_PRIVILEGE = 50
REASON_SYSCALL_BAD_POINTER = 51
REASON_SYSCALL_UNSUPPORTED = 52
REASON_SYSCALL_NO_RESOURCE = 53
REASON_SYSCALL_NOT_FOUND = 54
REASON_SYSCALL_COMMIT = 55
REASON_SYSCALL_CHILD = 56
REASON_DEVICE_BAD_REQUEST = 57
REASON_DEVICE_BAD_KIND = 58
REASON_DEVICE_UNSUPPORTED = 59
REASON_DEVICE_NOT_FOUND = 60
REASON_DEVICE_DUPLICATE = 61
REASON_DEVICE_EXHAUSTED = 62
REASON_DEVICE_STATE = 63
REASON_DEVICE_CAPABILITY = 64
REASON_DEVICE_BAD_PARAMETER = 65
REASON_DEVICE_OWNER = 66
REASON_DEVICE_OBJECT = 67
REASON_TIMER_BAD_REQUEST = 68
REASON_TIMER_NOT_FOUND = 69
REASON_TIMER_DUPLICATE = 70
REASON_TIMER_EXHAUSTED = 71
REASON_TIMER_STATE = 72
REASON_TIMER_NOT_DUE = 73
REASON_TIMER_BAD_INTERVAL = 74
REASON_TIMER_EVENT = 75
REASON_TIMER_UNSUPPORTED = 76
REASON_DEVICE_BACKEND = 77
REASON_TASK_BAD_REQUEST = 78
REASON_TASK_BAD_ID = 79
REASON_TASK_BAD_STATE = 80
REASON_TASK_NOT_FOUND = 81
REASON_TASK_DUPLICATE = 82
REASON_TASK_EXHAUSTED = 83
REASON_TASK_BAD_ENTRY = 84
REASON_TASK_BAD_STACK = 85
REASON_TASK_BAD_CONTEXT = 86
REASON_TASK_BAD_TRANSITION = 87
REASON_TASK_TERMINATED = 88
REASON_TASK_NOT_RUNNING = 89
REASON_TASK_NOT_BLOCKED = 90
REASON_TASK_NOT_READY = 91
REASON_TASK_UNSUPPORTED = 92
REASON_TASK_STALE = 93
REASON_EXEC_BAD_REQUEST = 94
REASON_EXEC_BAD_CONTAINER = 95
REASON_EXEC_BAD_SOURCE = 96
REASON_EXEC_BAD_IMAGE = 97
REASON_EXEC_BAD_ENTRY = 98
REASON_EXEC_BAD_BASE = 99
REASON_EXEC_BAD_SIZE = 100
REASON_EXEC_BAD_ALIGN = 101
REASON_EXEC_BAD_HANDLE = 102
REASON_EXEC_BAD_OBJECT = 103
REASON_EXEC_BAD_OWNER = 104
REASON_EXEC_BAD_PERMISSION = 105
REASON_EXEC_BAD_TASK = 106
REASON_EXEC_BAD_STATE = 107
REASON_EXEC_UNSUPPORTED = 108
REASON_EXEC_EXHAUSTED = 109
REASON_EXEC_BAD_MAPPING = 110
REASON_EXEC_BAD_CONTEXT = 111
REASON_EXEC_LAUNCH_FAILED = 112
REASON_EXEC_TRAP = 113
REASON_EXEC_COMMIT = 114
REASON_EXEC_STALE = 115

ERR_OK = 0
ERR_BAD_ID = 1
ERR_BAD_TYPE = 2
ERR_DUPLICATE_ID = 3
ERR_NO_SLOT = 4
ERR_UNIT_UNAVAILABLE = 5
ERR_NO_RECEIPT = 6
ERR_BAD_SLOT = 7
ERR_NOT_ACTIVE = 8
ERR_OWNERSHIP = 9
ERR_INVALID_TRANSITION = 10
ERR_UNIT_BUSY = 11
ERR_UNIT_IDLE = 12
ERR_BAD_PU = 13
ERR_INCOMPLETE = 14

VALID_STATES = {
    STATE_FREE,
    STATE_ADMITTED,
    STATE_VALIDATED,
    STATE_EXECUTED,
    STATE_COMMITTED,
    STATE_COMPLETE,
    STATE_REJECTED,
    STATE_FAILED,
}
VALID_OUTCOMES = {
    OUTCOME_NONE,
    OUTCOME_PENDING,
    OUTCOME_COMPLETE,
    OUTCOME_REJECTED,
    OUTCOME_FAILED,
}


class NpkCoreError(ValueError):
    """Raised when the processing core's observable state is inconsistent."""


@dataclass(frozen=True)
class CoreState:
    magic: int
    version: int
    op_slots: int
    pu_count: int
    admitted: int
    rejected: int
    failed: int
    completed: int
    last_admit_status: int
    last_admit_reason: int
    last_pu_status: int
    last_transition_status: int
    selftest_status: int
    reserved: int
    pu_state: tuple[int, ...]
    pu_operation: tuple[int, ...]


@dataclass(frozen=True)
class OperationSnapshot:
    id: int
    type: int
    stage: int
    state: int
    pu: int
    result: int
    reason: int
    reserved: int


@dataclass(frozen=True)
class Receipt:
    magic: int
    id: int
    type: int
    pu: int
    stage: int
    state: int
    outcome: int
    result: int
    reason: int
    trace: tuple[int, ...]
    reserved: tuple[int, ...]


@dataclass(frozen=True)
class CoreSnapshot:
    core: CoreState
    operations: tuple[OperationSnapshot, ...]
    receipts: tuple[Receipt, ...]


@dataclass(frozen=True)
class ReceiptExpectation:
    id: int
    type: int
    pu: int
    outcome: int
    result: int = 0
    reason: int = REASON_NONE
    stage: int | None = None
    state: int | None = None
    trace: tuple[int, ...] | None = None


def _word(machine: NovaMachine, address: int) -> int:
    return machine._read_mem(address, 8)


def decode_core_snapshot(machine: NovaMachine) -> CoreSnapshot:
    """Decode the core state, operation slots, and receipt log."""

    def core_word(offset: int) -> int:
        return _word(machine, CORE_STATE_ADDRESS + offset)

    core = CoreState(
        magic=core_word(0),
        version=core_word(8),
        op_slots=core_word(16),
        pu_count=core_word(24),
        admitted=core_word(32),
        rejected=core_word(40),
        failed=core_word(48),
        completed=core_word(56),
        last_admit_status=core_word(64),
        last_admit_reason=core_word(72),
        last_pu_status=core_word(80),
        last_transition_status=core_word(88),
        selftest_status=core_word(96),
        reserved=core_word(104),
        pu_state=tuple(core_word(112 + index * 8) for index in range(2)),
        pu_operation=tuple(core_word(128 + index * 8) for index in range(2)),
    )

    operations = []
    for slot in range(OP_SNAPSHOT_SLOTS):
        base = OP_SNAPSHOT_ADDRESS + slot * OP_SNAPSHOT_SIZE
        operations.append(OperationSnapshot(
            id=_word(machine, base),
            type=_word(machine, base + 8),
            stage=_word(machine, base + 16),
            state=_word(machine, base + 24),
            pu=_word(machine, base + 32),
            result=_word(machine, base + 40),
            reason=_word(machine, base + 48),
            reserved=_word(machine, base + 56),
        ))

    receipts = []
    for index in range(RECEIPT_SLOTS):
        base = RECEIPT_TABLE_ADDRESS + index * RECEIPT_SIZE
        receipts.append(Receipt(
            magic=_word(machine, base),
            id=_word(machine, base + 8),
            type=_word(machine, base + 16),
            pu=_word(machine, base + 24),
            stage=_word(machine, base + 32),
            state=_word(machine, base + 40),
            outcome=_word(machine, base + 48),
            result=_word(machine, base + 56),
            reason=_word(machine, base + 64),
            trace=tuple(_word(machine, base + 72 + i * 8) for i in range(5)),
            reserved=(_word(machine, base + 112), _word(machine, base + 120)),
        ))

    return CoreSnapshot(core=core, operations=tuple(operations),
                        receipts=tuple(receipts))


def validate_core_state(core: CoreState) -> None:
    """Validate the final core state after the kernel self-test."""
    if core.magic != CORE_MAGIC:
        raise NpkCoreError(f"core magic mismatch: 0x{core.magic:x}")
    if core.version != CORE_VERSION:
        raise NpkCoreError(f"core version mismatch: {core.version}")
    if core.op_slots != OP_SNAPSHOT_SLOTS or core.pu_count != 2:
        raise NpkCoreError(
            f"unexpected core dimensions: slots={core.op_slots}, "
            f"pus={core.pu_count}"
        )
    if core.selftest_status != 0:
        raise NpkCoreError(f"kernel core self-test failed: {core.selftest_status}")
    if core.pu_state != (PU_IDLE, PU_IDLE):
        raise NpkCoreError(f"processing units were not released: {core.pu_state}")
    if core.pu_operation != (0, 0):
        raise NpkCoreError(f"processing units retain operations: {core.pu_operation}")
    if core.admitted != 10 or core.completed != 7 or core.rejected != 3 or core.failed != 2:
        raise NpkCoreError(
            "unexpected core counters: admitted={admitted}, completed={completed}, "
            "rejected={rejected}, failed={failed}".format(
                admitted=core.admitted,
                completed=core.completed,
                rejected=core.rejected,
                failed=core.failed,
            )
        )


def validate_receipt_structure(receipt: Receipt) -> None:
    """Check receipt invariants without assuming a particular operation."""
    if receipt.magic != RECEIPT_MAGIC:
        raise NpkCoreError(f"receipt magic mismatch: 0x{receipt.magic:x}")
    if receipt.pu != NO_INDEX and receipt.pu >= 2:
        raise NpkCoreError(f"invalid processing-unit id: {receipt.pu}")
    if receipt.stage > STAGE_COMPLETE:
        raise NpkCoreError(f"invalid receipt stage: {receipt.stage}")
    if receipt.state not in VALID_STATES:
        raise NpkCoreError(f"invalid receipt state: {receipt.state}")
    if receipt.outcome not in VALID_OUTCOMES:
        raise NpkCoreError(f"invalid receipt outcome: {receipt.outcome}")
    if any(step > STAGE_COMPLETE for step in receipt.trace):
        raise NpkCoreError(f"invalid stage trace: {receipt.trace}")

    if receipt.stage == STAGE_NONE:
        if any(receipt.trace):
            raise NpkCoreError("stage-zero receipt has a non-empty trace")
        if receipt.outcome != OUTCOME_REJECTED or receipt.state != STATE_REJECTED:
            raise NpkCoreError("stage-zero receipt is not an admission rejection")
        return

    expected_prefix = tuple(range(1, receipt.stage + 1))
    if receipt.trace[:receipt.stage] != expected_prefix:
        raise NpkCoreError(
            f"stage trace does not match stage {receipt.stage}: {receipt.trace}"
        )
    if any(receipt.trace[receipt.stage:]):
        raise NpkCoreError(f"stage trace continues past stage {receipt.stage}: {receipt.trace}")

    if receipt.outcome == OUTCOME_COMPLETE:
        if receipt.stage != STAGE_COMPLETE or receipt.state != STATE_COMPLETE:
            raise NpkCoreError("complete outcome lacks the complete stage/state")
        if receipt.reason != REASON_NONE:
            raise NpkCoreError("complete receipt has a failure reason")
        return
    if receipt.outcome == OUTCOME_REJECTED:
        if receipt.stage > STAGE_VALIDATE or receipt.state != STATE_REJECTED:
            raise NpkCoreError("rejected receipt executed or failed to record rejection")
        if receipt.result != 0 or receipt.reason == REASON_NONE:
            raise NpkCoreError("rejected receipt has an invalid result or reason")
        return
    if receipt.outcome == OUTCOME_FAILED:
        if receipt.stage not in (STAGE_EXECUTE, STAGE_COMMIT):
            raise NpkCoreError("failed receipt lacks an execution or commit stage")
        if receipt.state != STATE_FAILED:
            raise NpkCoreError("failed receipt does not record STATE_FAILED")
        if receipt.result != 0 or receipt.reason == REASON_NONE:
            raise NpkCoreError("failed receipt has an invalid result or reason")
        return
    if receipt.outcome == OUTCOME_PENDING:
        if receipt.state in (STATE_COMPLETE, STATE_REJECTED, STATE_FAILED):
            raise NpkCoreError("pending receipt has a terminal state")


def validate_receipt(receipt: Receipt, expected: ReceiptExpectation) -> None:
    """Validate one receipt against an independently supplied expectation."""
    validate_receipt_structure(receipt)
    checks = (
        ("id", receipt.id, expected.id),
        ("type", receipt.type, expected.type),
        ("pu", receipt.pu, expected.pu),
        ("outcome", receipt.outcome, expected.outcome),
        ("result", receipt.result, expected.result),
        ("reason", receipt.reason, expected.reason),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            raise NpkCoreError(f"receipt {name} mismatch: {actual} != {wanted}")
    if expected.stage is not None and receipt.stage != expected.stage:
        raise NpkCoreError(
            f"receipt stage mismatch: {receipt.stage} != {expected.stage}"
        )
    if expected.state is not None and receipt.state != expected.state:
        raise NpkCoreError(
            f"receipt state mismatch: {receipt.state} != {expected.state}"
        )
    if expected.trace is not None and receipt.trace != expected.trace:
        raise NpkCoreError(
            f"receipt trace mismatch: {receipt.trace} != {expected.trace}"
        )


def expected_receipt(index: int, **changes: object) -> ReceiptExpectation:
    """Build an expectation from the canonical Milestone 2 receipt sequence."""
    base = _EXPECTED_RECEIPTS[index]
    return replace(base, **changes)


_EXPECTED_RECEIPTS = (
    ReceiptExpectation(
        id=1, type=OP_BOOT_VERIFY, pu=0, outcome=OUTCOME_COMPLETE,
        result=BOOT_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=2, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=3, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_REJECTED,
        reason=REASON_DIAG_REJECT, stage=STAGE_VALIDATE, state=STATE_REJECTED,
        trace=(STAGE_ADMIT, STAGE_VALIDATE, 0, 0, 0),
    ),
    ReceiptExpectation(
        id=4, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_FAILED,
        reason=REASON_DIAG_EXEC, stage=STAGE_EXECUTE, state=STATE_FAILED,
        trace=(STAGE_ADMIT, STAGE_VALIDATE, STAGE_EXECUTE, 0, 0),
    ),
    ReceiptExpectation(
        id=5, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_FAILED,
        reason=REASON_DIAG_COMMIT, stage=STAGE_COMMIT, state=STATE_FAILED,
        trace=(STAGE_ADMIT, STAGE_VALIDATE, STAGE_EXECUTE, STAGE_COMMIT, 0),
    ),
    ReceiptExpectation(
        id=6, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=6, type=OP_CORE_DIAGNOSTIC, pu=NO_INDEX,
        outcome=OUTCOME_REJECTED, reason=REASON_DUPLICATE_ID,
        stage=STAGE_NONE, state=STATE_REJECTED,
        trace=(0, 0, 0, 0, 0),
    ),
    ReceiptExpectation(
        id=7, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=8, type=OP_CORE_DIAGNOSTIC, pu=1, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=9, type=OP_CORE_DIAGNOSTIC, pu=NO_INDEX,
        outcome=OUTCOME_REJECTED, reason=REASON_UNIT_UNAVAILABLE,
        stage=STAGE_NONE, state=STATE_REJECTED,
        trace=(0, 0, 0, 0, 0),
    ),
    ReceiptExpectation(
        id=10, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
    ReceiptExpectation(
        id=11, type=OP_CORE_DIAGNOSTIC, pu=0, outcome=OUTCOME_COMPLETE,
        result=DIAG_OUTPUT, stage=STAGE_COMPLETE, state=STATE_COMPLETE,
        trace=EXPECTED_STAGE_TRACE,
    ),
)

__all__.append("expected_receipt")
