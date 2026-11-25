"""Host-side decoding and validation of NPK Milestone 9 executable processing.

These helpers observe the kernel's fixed executable regions through the
reference machine. They are harness code, not guest APIs. A loader receipt is
meaningful only together with the image record, launch context, staged ``.nob``
bytes, M4 page tables, M5 object handle, and M8 task record that the operation
actually used.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assembler import AssemblyError, assemble_text
from ..emulator import NovaMachine
from ..nob import NOB_HEADER_SIZE, NobError, decode_nob
from . import core as core_api
from .layout import (
    EXEC_ENTRY_ADDRESS,
    EXEC_LAYOUT_LIMIT,
    EXEC_LAUNCH_CONTEXT_ADDRESS,
    EXEC_LAUNCH_CONTEXT_SIZE,
    EXEC_RECORD_SIZE,
    EXEC_RECEIPT_SLOTS,
    EXEC_RECEIPT_TABLE_ADDRESS,
    EXEC_REQUEST_ADDRESS,
    EXEC_REQUEST_SIZE,
    EXEC_RESPONSE_ADDRESS,
    EXEC_RESPONSE_SIZE,
    EXEC_SLOTS,
    EXEC_SOURCE_ADDRESS,
    EXEC_SOURCE_SIZE,
    EXEC_STATE_ADDRESS,
    EXEC_STATE_MAGIC,
    EXEC_STATE_SIZE,
    EXEC_STATE_VERSION,
    EXEC_TABLE_ADDRESS,
    RECEIPT_SIZE,
    PHYSICAL_ARENA_BASE,
)

__all__ = [
    "EXEC_STATE_MAGIC",
    "EXEC_STATE_VERSION",
    "EXEC_REQUEST_MAGIC",
    "EXEC_RESPONSE_MAGIC",
    "EXEC_RECEIPT_SIZE",
    "OP_EXEC_LOAD",
    "OP_EXEC_START",
    "OP_EXEC_QUERY",
    "OP_EXEC_RELEASE",
    "REASON_EXEC_BAD_REQUEST",
    "REASON_EXEC_BAD_CONTAINER",
    "REASON_EXEC_BAD_SOURCE",
    "REASON_EXEC_BAD_IMAGE",
    "REASON_EXEC_BAD_ENTRY",
    "REASON_EXEC_BAD_BASE",
    "REASON_EXEC_BAD_SIZE",
    "REASON_EXEC_BAD_ALIGN",
    "REASON_EXEC_BAD_HANDLE",
    "REASON_EXEC_BAD_OBJECT",
    "REASON_EXEC_BAD_OWNER",
    "REASON_EXEC_BAD_PERMISSION",
    "REASON_EXEC_BAD_TASK",
    "REASON_EXEC_BAD_STATE",
    "REASON_EXEC_UNSUPPORTED",
    "REASON_EXEC_EXHAUSTED",
    "REASON_EXEC_BAD_MAPPING",
    "REASON_EXEC_BAD_CONTEXT",
    "REASON_EXEC_LAUNCH_FAILED",
    "REASON_EXEC_TRAP",
    "REASON_EXEC_COMMIT",
    "REASON_EXEC_STALE",
    "REQUEST_OK",
    "REQUEST_BAD_REQUEST",
    "REQUEST_BAD_OP",
    "REQUEST_ADMIT_FAIL",
    "REQUEST_ADVANCE_FAIL",
    "REQUEST_BAD_OUTCOME",
    "REQUEST_REJECTED",
    "REQUEST_FAILED",
    "IMAGE_FREE",
    "IMAGE_LOADED",
    "IMAGE_RUNNING",
    "IMAGE_EXITED",
    "IMAGE_FAILED",
    "TARGET_ARCHITECTURE",
    "NOB_MAGIC_WORD",
    "NOB_HEADER_SIZE",
    "NpkExecError",
    "ExecState",
    "ExecRecord",
    "ExecRequest",
    "ExecResponse",
    "LaunchContext",
    "ExecSnapshot",
    "word",
    "decode_exec_entry",
    "decode_exec_state",
    "decode_exec_records",
    "decode_exec_request",
    "decode_exec_response",
    "decode_launch_context",
    "decode_exec_receipts",
    "decode_exec_snapshot",
    "stage_nob",
    "validate_exec_layout",
    "validate_exec_state",
    "validate_exec_response",
    "validate_exec_snapshot",
    "validate_complete_exec_receipt",
    "validate_rejected_exec_receipt",
    "validate_failed_exec_receipt",
    "write_exec_request",
    "call_exec_entry",
    "call_exec_request",
    "exec_receipt_expectation",
]


EXEC_STATE_MAGIC = EXEC_STATE_MAGIC
EXEC_STATE_VERSION = EXEC_STATE_VERSION
EXEC_REQUEST_MAGIC = 0x4E504B51  # "NPKQ"
EXEC_RESPONSE_MAGIC = 0x4E504B52  # "NPKR"
EXEC_RECEIPT_SIZE = RECEIPT_SIZE

OP_EXEC_LOAD = core_api.OP_EXEC_LOAD
OP_EXEC_START = core_api.OP_EXEC_START
OP_EXEC_QUERY = core_api.OP_EXEC_QUERY
OP_EXEC_RELEASE = core_api.OP_EXEC_RELEASE

REASON_EXEC_BAD_REQUEST = core_api.REASON_EXEC_BAD_REQUEST
REASON_EXEC_BAD_CONTAINER = core_api.REASON_EXEC_BAD_CONTAINER
REASON_EXEC_BAD_SOURCE = core_api.REASON_EXEC_BAD_SOURCE
REASON_EXEC_BAD_IMAGE = core_api.REASON_EXEC_BAD_IMAGE
REASON_EXEC_BAD_ENTRY = core_api.REASON_EXEC_BAD_ENTRY
REASON_EXEC_BAD_BASE = core_api.REASON_EXEC_BAD_BASE
REASON_EXEC_BAD_SIZE = core_api.REASON_EXEC_BAD_SIZE
REASON_EXEC_BAD_ALIGN = core_api.REASON_EXEC_BAD_ALIGN
REASON_EXEC_BAD_HANDLE = core_api.REASON_EXEC_BAD_HANDLE
REASON_EXEC_BAD_OBJECT = core_api.REASON_EXEC_BAD_OBJECT
REASON_EXEC_BAD_OWNER = core_api.REASON_EXEC_BAD_OWNER
REASON_EXEC_BAD_PERMISSION = core_api.REASON_EXEC_BAD_PERMISSION
REASON_EXEC_BAD_TASK = core_api.REASON_EXEC_BAD_TASK
REASON_EXEC_BAD_STATE = core_api.REASON_EXEC_BAD_STATE
REASON_EXEC_UNSUPPORTED = core_api.REASON_EXEC_UNSUPPORTED
REASON_EXEC_EXHAUSTED = core_api.REASON_EXEC_EXHAUSTED
REASON_EXEC_BAD_MAPPING = core_api.REASON_EXEC_BAD_MAPPING
REASON_EXEC_BAD_CONTEXT = core_api.REASON_EXEC_BAD_CONTEXT
REASON_EXEC_LAUNCH_FAILED = core_api.REASON_EXEC_LAUNCH_FAILED
REASON_EXEC_TRAP = core_api.REASON_EXEC_TRAP
REASON_EXEC_COMMIT = core_api.REASON_EXEC_COMMIT
REASON_EXEC_STALE = core_api.REASON_EXEC_STALE

REQUEST_OK = 0
REQUEST_BAD_REQUEST = 1
REQUEST_BAD_OP = 2
REQUEST_ADMIT_FAIL = 3
REQUEST_ADVANCE_FAIL = 4
REQUEST_BAD_OUTCOME = 5
REQUEST_REJECTED = 6
REQUEST_FAILED = 7

IMAGE_FREE = 0
IMAGE_LOADED = 1
IMAGE_RUNNING = 2
IMAGE_EXITED = 3
IMAGE_FAILED = 4

TARGET_ARCHITECTURE = 1
NOB_MAGIC_WORD = 0x56424F4E  # little-endian "NOBV"
_VALID_REQUEST_STATUSES = frozenset(range(REQUEST_OK, REQUEST_FAILED + 1))


class NpkExecError(ValueError):
    """Raised when Milestone 9 executable observation state is inconsistent."""


@dataclass(frozen=True)
class ExecState:
    magic: int
    version: int
    init_status: int
    exec_slots: int
    exec_count: int
    loaded_count: int
    exited_count: int
    next_image_id: int
    next_generation: int
    dispatch_count: int
    launch_count: int
    last_status: int
    last_reason: int
    last_image: int
    last_task: int
    target_architecture: int


@dataclass(frozen=True)
class ExecRecord:
    state: int
    id: int
    generation: int
    owner: int
    handle: int
    region_id: int
    source_address: int
    image_base: int
    image_size: int
    entry: int
    task_id: int
    asid: int
    stack_base: int
    exit_code: int
    exit_cause: int
    exit_ecause: int
    launch_count: int
    flags: int


@dataclass(frozen=True)
class ExecRequest:
    magic: int
    operation: int
    id: int
    image_id: int
    owner: int
    handle: int
    source: int
    source_size: int
    task: int
    asid: int
    arg: int
    flags: int


@dataclass(frozen=True)
class ExecResponse:
    magic: int
    status: int
    operation: int
    reason: int
    result: int
    image_id: int
    receipt_index: int
    slot: int


@dataclass(frozen=True)
class LaunchContext:
    status: int
    record_index: int
    vpc: int
    vpriv: int
    vptbr: int
    guest_sp: int
    guest_arg: int
    stack_top: int
    exit_code: int
    exit_cause: int
    exit_ecause: int
    launch_count: int
    saved_pc: int
    saved_priv: int
    saved_ptbr: int
    flags: int


@dataclass(frozen=True)
class ExecSnapshot:
    state: ExecState
    records: tuple[ExecRecord, ...]
    request: ExecRequest
    response: ExecResponse
    launch_context: LaunchContext
    receipts: tuple[core_api.Receipt, ...]


def word(machine: NovaMachine, address: int) -> int:
    """Read one aligned little-endian observation word."""
    return machine._read_mem(address, 8)


def decode_exec_entry(machine: NovaMachine) -> int:
    return word(machine, EXEC_ENTRY_ADDRESS)


def decode_exec_state(machine: NovaMachine) -> ExecState:
    base = EXEC_STATE_ADDRESS
    return ExecState(*(word(machine, base + i * 8) for i in range(EXEC_STATE_SIZE // 8)))


def decode_exec_records(machine: NovaMachine) -> tuple[ExecRecord, ...]:
    records = []
    for i in range(EXEC_SLOTS):
        base = EXEC_TABLE_ADDRESS + i * EXEC_RECORD_SIZE
        records.append(ExecRecord(
            *(word(machine, base + j * 8) for j in range(EXEC_RECORD_SIZE // 8))
        ))
    return tuple(records)


def decode_exec_request(machine: NovaMachine) -> ExecRequest:
    base = EXEC_REQUEST_ADDRESS
    return ExecRequest(*(word(machine, base + i * 8)
                         for i in range(EXEC_REQUEST_SIZE // 8)))


def decode_exec_response(machine: NovaMachine) -> ExecResponse:
    base = EXEC_RESPONSE_ADDRESS
    return ExecResponse(*(word(machine, base + i * 8)
                          for i in range(EXEC_RESPONSE_SIZE // 8)))


def decode_launch_context(machine: NovaMachine) -> LaunchContext:
    base = EXEC_LAUNCH_CONTEXT_ADDRESS
    return LaunchContext(*(word(machine, base + i * 8)
                           for i in range(EXEC_LAUNCH_CONTEXT_SIZE // 8)))


def decode_exec_receipts(machine: NovaMachine) -> tuple[core_api.Receipt, ...]:
    receipts = []
    for i in range(EXEC_RECEIPT_SLOTS):
        base = EXEC_RECEIPT_TABLE_ADDRESS + i * RECEIPT_SIZE
        words = [word(machine, base + j * 8) for j in range(RECEIPT_SIZE // 8)]
        receipts.append(core_api.Receipt(
            magic=words[0], id=words[1], type=words[2], pu=words[3],
            stage=words[4], state=words[5], outcome=words[6], result=words[7],
            reason=words[8], trace=tuple(words[9:14]), reserved=tuple(words[14:16]),
        ))
    return tuple(receipts)


def decode_exec_snapshot(machine: NovaMachine) -> ExecSnapshot:
    return ExecSnapshot(
        state=decode_exec_state(machine),
        records=decode_exec_records(machine),
        request=decode_exec_request(machine),
        response=decode_exec_response(machine),
        launch_context=decode_launch_context(machine),
        receipts=decode_exec_receipts(machine),
    )


def stage_nob(machine: NovaMachine, data: bytes, *,
              address: int = EXEC_SOURCE_ADDRESS) -> int:
    """Write a validated ``.nob`` container into the bounded staging window."""
    try:
        image = decode_nob(data)
    except NobError as exc:
        raise NpkExecError(f"invalid staged executable: {exc}") from exc
    raw = bytes(data)
    if len(raw) > EXEC_SOURCE_SIZE:
        raise NpkExecError(
            f"staged executable is {len(raw)} bytes; capacity is {EXEC_SOURCE_SIZE}"
        )
    if address < EXEC_SOURCE_ADDRESS or (address & 7) != 0 or \
            address - EXEC_SOURCE_ADDRESS + len(raw) > EXEC_SOURCE_SIZE:
        raise NpkExecError("staged executable is outside the source window")
    for offset in range(0, len(raw), 8):
        chunk = raw[offset:offset + 8].ljust(8, b"\x00")
        machine._write_mem(address + offset,
                           int.from_bytes(chunk, "little"), 8)
    return len(raw)


def validate_exec_layout() -> None:
    """Check the public M9 region contract used by tests and documentation."""
    assert EXEC_ENTRY_ADDRESS == 0xB3000
    assert EXEC_STATE_ADDRESS == 0xB3010 and EXEC_STATE_SIZE == 128
    assert EXEC_REQUEST_ADDRESS == 0xB3090 and EXEC_REQUEST_SIZE == 96
    assert EXEC_RESPONSE_ADDRESS == 0xB30F0 and EXEC_RESPONSE_SIZE == 64
    assert EXEC_TABLE_ADDRESS == 0xB3130
    assert EXEC_RECORD_SIZE == 144 and EXEC_SLOTS == 8
    assert EXEC_RECEIPT_TABLE_ADDRESS == 0xB4000
    assert EXEC_RECEIPT_SLOTS == 16
    assert EXEC_LAUNCH_CONTEXT_ADDRESS == 0xB4800
    assert EXEC_SOURCE_ADDRESS == 0xB5000 and EXEC_SOURCE_SIZE == 0xB000
    assert EXEC_STATE_ADDRESS + EXEC_STATE_SIZE <= EXEC_REQUEST_ADDRESS
    assert EXEC_REQUEST_ADDRESS + EXEC_REQUEST_SIZE <= EXEC_RESPONSE_ADDRESS
    assert EXEC_RESPONSE_ADDRESS + EXEC_RESPONSE_SIZE <= EXEC_TABLE_ADDRESS
    assert EXEC_TABLE_ADDRESS + EXEC_SLOTS * EXEC_RECORD_SIZE <= EXEC_RECEIPT_TABLE_ADDRESS
    assert EXEC_RECEIPT_TABLE_ADDRESS + EXEC_RECEIPT_SLOTS * RECEIPT_SIZE <= \
        EXEC_LAUNCH_CONTEXT_ADDRESS
    assert EXEC_SOURCE_ADDRESS + EXEC_SOURCE_SIZE <= EXEC_LAYOUT_LIMIT
    assert EXEC_LAYOUT_LIMIT == PHYSICAL_ARENA_BASE


def validate_exec_state(state: ExecState) -> None:
    if state.magic != EXEC_STATE_MAGIC:
        raise NpkExecError("bad executable state magic")
    if state.version != EXEC_STATE_VERSION:
        raise NpkExecError("unsupported executable state version")
    if state.exec_slots != EXEC_SLOTS:
        raise NpkExecError("executable slot count mismatch")
    if state.target_architecture != TARGET_ARCHITECTURE:
        raise NpkExecError("unexpected executable target architecture")
    if state.exec_count > EXEC_SLOTS or state.loaded_count > state.exec_count:
        raise NpkExecError("inconsistent executable counts")


def validate_exec_response(response: ExecResponse, *, status: int | None = None,
                           operation: int | None = None,
                           reason: int | None = None,
                           result: int | None = None) -> None:
    if response.magic != EXEC_RESPONSE_MAGIC:
        raise NpkExecError("bad executable response magic")
    if status is not None and response.status != status:
        raise NpkExecError(f"executable status mismatch: {response.status} != {status}")
    if response.status not in _VALID_REQUEST_STATUSES:
        raise NpkExecError("invalid executable response status")
    if operation is not None and response.operation != operation:
        raise NpkExecError(f"executable operation mismatch: {response.operation}")
    if reason is not None and response.reason != reason:
        raise NpkExecError(f"executable reason mismatch: {response.reason} != {reason}")
    if result is not None and response.result != result:
        raise NpkExecError(f"executable result mismatch: {response.result} != {result}")


def validate_exec_snapshot(snapshot: ExecSnapshot, *,
                           response_published: bool | None = None) -> None:
    """Validate one observation snapshot.

    A freshly booted machine has a zero-filled, unpublished response block.
    When ``response_published`` is omitted, an all-zero response is treated as
    unpublished; any non-zero magic is checked against the full response
    contract. Callers that know a request ran can pass ``True`` to require it.
    """
    validate_exec_state(snapshot.state)
    published = (snapshot.response.magic != 0 if response_published is None
                 else response_published)
    if published:
        validate_exec_response(snapshot.response)
    elif any((snapshot.response.magic, snapshot.response.status,
              snapshot.response.operation, snapshot.response.reason,
              snapshot.response.result, snapshot.response.image_id,
              snapshot.response.receipt_index, snapshot.response.slot)):
        raise NpkExecError("unpublished executable response is not zero-filled")
    for record in snapshot.records:
        if record.state not in (IMAGE_FREE, IMAGE_LOADED, IMAGE_RUNNING,
                                IMAGE_EXITED, IMAGE_FAILED):
            raise NpkExecError("invalid executable record state")
    published_receipts = [receipt for receipt in snapshot.receipts
                          if receipt.magic != 0]
    if response_published is True and not published_receipts:
        raise NpkExecError("published executable response has no receipt")
    for receipt in published_receipts:
        core_api.validate_receipt_structure(receipt)


def validate_complete_exec_receipt(receipt: core_api.Receipt) -> None:
    core_api.validate_receipt_structure(receipt)
    if receipt.outcome != core_api.OUTCOME_COMPLETE:
        raise NpkExecError("executable receipt is not complete")
    if receipt.stage != core_api.STAGE_COMPLETE or \
            receipt.state != core_api.STATE_COMPLETE:
        raise NpkExecError("complete executable receipt lacks the complete stage/state")
    if receipt.trace != core_api.EXPECTED_STAGE_TRACE:
        raise NpkExecError(f"incomplete executable lifecycle trace: {receipt.trace}")


def validate_rejected_exec_receipt(receipt: core_api.Receipt) -> None:
    core_api.validate_receipt_structure(receipt)
    if receipt.outcome != core_api.OUTCOME_REJECTED:
        raise NpkExecError("executable receipt is not rejected")
    if receipt.stage != core_api.STAGE_VALIDATE or \
            receipt.state != core_api.STATE_REJECTED:
        raise NpkExecError("rejected executable receipt advanced past validation")


def validate_failed_exec_receipt(receipt: core_api.Receipt, *, stage: int) -> None:
    core_api.validate_receipt_structure(receipt)
    if receipt.outcome != core_api.OUTCOME_FAILED:
        raise NpkExecError("executable receipt is not failed")
    if receipt.state != core_api.STATE_FAILED or \
            stage not in (core_api.STAGE_EXECUTE, core_api.STAGE_COMMIT):
        raise NpkExecError("failed executable receipt has the wrong stage/state")
    if receipt.stage != stage:
        raise NpkExecError(
            f"failed executable stage mismatch: {receipt.stage} != {stage}"
        )


def write_exec_request(machine: NovaMachine, *, operation: int, id: int,
                       image_id: int = 0, owner: int = 0, handle: int = 0,
                       source: int = 0, source_size: int = 0, task: int = 0,
                       asid: int = 0, arg: int = 0, flags: int = 0) -> None:
    """Publish one 96-byte executable request block."""
    values = (
        EXEC_REQUEST_MAGIC, operation, id, image_id, owner, handle, source,
        source_size, task, asid, arg, flags,
    )
    for index, value in enumerate(values):
        machine._write_mem(EXEC_REQUEST_ADDRESS + index * 8,
                           value & 0xFFFFFFFFFFFFFFFF, 8)


def call_exec_entry(machine: NovaMachine, *, code_address: int = 0xA0000,
                    max_steps: int = 400_000) -> None:
    """Branch to the published executable entry through real Nova code."""
    entry = decode_exec_entry(machine)
    if entry == 0:
        raise NpkExecError("the kernel did not publish an executable entry")
    try:
        probe = assemble_text(f"LI ra, {code_address}\nBL 0x{entry:x}\nHALT\n")
        return_address = code_address + len(probe) - 4
        code = assemble_text(
            f"LI ra, {return_address}\nBL 0x{entry:x}\nHALT\n"
        )
    except AssemblyError as exc:
        raise NpkExecError(f"could not build executable call stub: {exc}") from exc
    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.halted = False
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    if not machine.halted:
        raise NpkExecError("executable request did not return")
    if machine.exception_code != 0:
        raise NpkExecError(
            f"executable request raised exception {machine.exception_code}"
        )


def call_exec_request(machine: NovaMachine, *, operation: int, id: int,
                      image_id: int = 0, owner: int = 0, handle: int = 0,
                      source: int = 0, source_size: int = 0, task: int = 0,
                      asid: int = 0, arg: int = 0, flags: int = 0,
                      code_address: int = 0xA0000,
                      max_steps: int = 400_000) -> ExecResponse:
    """Run one typed executable request through the real processed kernel."""
    write_exec_request(
        machine, operation=operation, id=id, image_id=image_id, owner=owner,
        handle=handle, source=source, source_size=source_size, task=task,
        asid=asid, arg=arg, flags=flags,
    )
    call_exec_entry(machine, code_address=code_address, max_steps=max_steps)
    return decode_exec_response(machine)


def exec_receipt_expectation(
    id: int,
    operation: int,
    outcome: int,
    *,
    result: int = 0,
    reason: int = core_api.REASON_NONE,
    pu: int = core_api.PU_IDLE,
    stage: int | None = None,
    state: int | None = None,
    trace: tuple[int, ...] | None = None,
) -> core_api.ReceiptExpectation:
    """Build a receipt expectation for one M9 lifecycle outcome."""
    return core_api.ReceiptExpectation(
        id=id, type=operation, pu=pu, outcome=outcome, result=result,
        reason=reason, stage=stage, state=state, trace=trace,
    )
