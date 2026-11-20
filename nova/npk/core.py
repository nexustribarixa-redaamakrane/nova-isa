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
