"""Host-side decoding and validation of NPK Milestone 8 task processing.

Tasks are kernel-managed typed records scheduled cooperatively through the
existing trap/syscall path. Nova MRET restores EPC/privilege from the hidden
exception stack, so NPK cannot redirect return by editing frame.epc alone. The
cooperative scheduler patches a suspended return site with a JMP-r15 trampoline
and restores r15 to the selected task resume PC before MRET returns to that
patched site. This module observes kernel task state; it does not invent
scheduler behavior.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..emulator import NovaMachine
from . import core as core_api
from .layout import (
    RECEIPT_SIZE,
    TASK_ENTRY_ADDRESS,
    TASK_LAYOUT_LIMIT,
    TASK_READY_QUEUE_ADDRESS,
    TASK_READY_QUEUE_ENTRY_SIZE,
    TASK_READY_QUEUE_SLOTS,
    TASK_RECEIPT_SLOTS,
    TASK_RECEIPT_TABLE_ADDRESS,
    TASK_REQUEST_ADDRESS,
    TASK_REQUEST_SIZE,
    TASK_RESPONSE_ADDRESS,
    TASK_RESPONSE_SIZE,
    TASK_STATE_ADDRESS,
    TASK_STATE_MAGIC,
    TASK_STATE_SIZE,
    TASK_STATE_VERSION,
    TASK_SLOTS,
    TASK_TABLE_ADDRESS,
    TASK_TCB_SIZE,
)

__all__ = [
    "TASK_STATE_FREE", "TASK_STATE_CREATED", "TASK_STATE_READY",
    "TASK_STATE_RUNNING", "TASK_STATE_BLOCKED", "TASK_STATE_TERMINATED",
    "OP_TASK_CREATE", "OP_TASK_TERMINATE", "OP_TASK_YIELD",
    "OP_TASK_BLOCK", "OP_TASK_WAKE", "OP_TASK_QUERY", "OP_TASK_DESTROY",
    "REASON_TASK_BAD_REQUEST", "REASON_TASK_BAD_ID", "REASON_TASK_BAD_STATE",
    "REASON_TASK_NOT_FOUND", "REASON_TASK_DUPLICATE", "REASON_TASK_EXHAUSTED",
    "REASON_TASK_BAD_ENTRY", "REASON_TASK_BAD_STACK", "REASON_TASK_BAD_CONTEXT",
    "REASON_TASK_BAD_TRANSITION", "REASON_TASK_TERMINATED",
    "REASON_TASK_NOT_RUNNING", "REASON_TASK_NOT_BLOCKED",
    "REASON_TASK_NOT_READY", "REASON_TASK_UNSUPPORTED", "REASON_TASK_STALE",
    "TASK_REQUEST_MAGIC", "TASK_RESPONSE_MAGIC",
    "NpkTaskError", "TaskState", "TaskTcb", "TaskReadyEntry", "TaskRequest",
    "TaskResponse", "TaskSnapshot",
    "decode_task_state", "decode_task_tcbs", "decode_task_ready_queue",
    "decode_task_receipts", "decode_task_request", "decode_task_response",
    "decode_task_snapshot", "decode_task_entry", "validate_task_state", "validate_task_tcb",
    "validate_task_receipts", "validate_task_snapshot", "write_task_request",
    "call_task_entry", "call_task_request", "task_receipt_expectation",
]

TASK_STATE_FREE = 0
TASK_STATE_CREATED = 1
TASK_STATE_READY = 2
TASK_STATE_RUNNING = 3
TASK_STATE_BLOCKED = 4
TASK_STATE_TERMINATED = 5

OP_TASK_CREATE = core_api.OP_TASK_CREATE
OP_TASK_TERMINATE = core_api.OP_TASK_TERMINATE
OP_TASK_YIELD = core_api.OP_TASK_YIELD
OP_TASK_BLOCK = core_api.OP_TASK_BLOCK
OP_TASK_WAKE = core_api.OP_TASK_WAKE
OP_TASK_QUERY = core_api.OP_TASK_QUERY
OP_TASK_DESTROY = core_api.OP_TASK_DESTROY

REASON_TASK_BAD_REQUEST = core_api.REASON_TASK_BAD_REQUEST
REASON_TASK_BAD_ID = core_api.REASON_TASK_BAD_ID
REASON_TASK_BAD_STATE = core_api.REASON_TASK_BAD_STATE
REASON_TASK_NOT_FOUND = core_api.REASON_TASK_NOT_FOUND
REASON_TASK_DUPLICATE = core_api.REASON_TASK_DUPLICATE
REASON_TASK_EXHAUSTED = core_api.REASON_TASK_EXHAUSTED
REASON_TASK_BAD_ENTRY = core_api.REASON_TASK_BAD_ENTRY
REASON_TASK_BAD_STACK = core_api.REASON_TASK_BAD_STACK
REASON_TASK_BAD_CONTEXT = core_api.REASON_TASK_BAD_CONTEXT
REASON_TASK_BAD_TRANSITION = core_api.REASON_TASK_BAD_TRANSITION
REASON_TASK_TERMINATED = core_api.REASON_TASK_TERMINATED
REASON_TASK_NOT_RUNNING = core_api.REASON_TASK_NOT_RUNNING
REASON_TASK_NOT_BLOCKED = core_api.REASON_TASK_NOT_BLOCKED
REASON_TASK_NOT_READY = core_api.REASON_TASK_NOT_READY
REASON_TASK_UNSUPPORTED = core_api.REASON_TASK_UNSUPPORTED
REASON_TASK_STALE = core_api.REASON_TASK_STALE

TASK_REQUEST_MAGIC = 0x4E504B51
TASK_RESPONSE_MAGIC = 0x4E504B52


class NpkTaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskState:
    magic: int
    version: int
    init_status: int
    task_slots: int
    task_count: int
    ready_count: int
    blocked_count: int
    terminated_count: int
    current_task: int
    current_index: int
    next_task_id: int
    dispatch_count: int
    switch_count: int
    last_status: int
    last_reason: int
    last_task: int


@dataclass(frozen=True)
class TaskTcb:
    id: int
    generation: int
    state: int
    owner: int
    parent: int
    entry: int
    stack_base: int
    stack_size: int
    stack_top: int
    arg: int
    address_space: int
    exit_status: int
    resume_pc: int
    resume_patch_addr: int
    resume_patch_word: int
    resume_patch_active: int
    flags: int
    frame: tuple[int, ...]


@dataclass(frozen=True)
class TaskReadyEntry:
    task_id: int
    task_index: int


@dataclass(frozen=True)
class TaskRequest:
    magic: int
    operation: int
    id: int
    task: int
    arg0: int
    arg1: int
    arg2: int
    arg3: int
    arg4: int
    arg5: int
    arg6: int
    arg7: int


@dataclass(frozen=True)
class TaskResponse:
    magic: int
    status: int
    operation: int
    reason: int
    result: int
    task: int
    receipt_index: int
    slot: int


@dataclass(frozen=True)
class TaskSnapshot:
    state: TaskState
    tcbs: tuple[TaskTcb, ...]
    ready: tuple[TaskReadyEntry, ...]
    receipts: tuple[core_api.Receipt, ...]
    request: TaskRequest
    response: TaskResponse


def _u64(machine: NovaMachine, addr: int) -> int:
    return machine._read_mem(addr, 8)


def decode_task_state(machine: NovaMachine) -> TaskState:
    base = TASK_STATE_ADDRESS
    fields = [_u64(machine, base + i * 8) for i in range(16)]
    return TaskState(*fields)


def decode_task_tcbs(machine: NovaMachine) -> tuple[TaskTcb, ...]:
    tcbs = []
    for i in range(TASK_SLOTS):
        base = TASK_TABLE_ADDRESS + i * TASK_TCB_SIZE
        words = [_u64(machine, base + j * 8) for j in range(TASK_TCB_SIZE // 8)]
        frame = tuple(words[32:32 + 38])
        tcbs.append(TaskTcb(*words[:17], frame=frame))
    return tuple(tcbs)


def decode_task_ready_queue(machine: NovaMachine) -> tuple[TaskReadyEntry, ...]:
    entries = []
    for i in range(TASK_READY_QUEUE_SLOTS):
        base = TASK_READY_QUEUE_ADDRESS + i * TASK_READY_QUEUE_ENTRY_SIZE
        entries.append(TaskReadyEntry(_u64(machine, base), _u64(machine, base + 8)))
    return tuple(entries)


def decode_task_receipts(machine: NovaMachine) -> tuple[core_api.Receipt, ...]:
    receipts = []
    for i in range(TASK_RECEIPT_SLOTS):
        base = TASK_RECEIPT_TABLE_ADDRESS + i * RECEIPT_SIZE
        words = [_u64(machine, base + j * 8) for j in range(RECEIPT_SIZE // 8)]
        receipts.append(core_api.Receipt(
            magic=words[0], id=words[1], type=words[2], pu=words[3],
            stage=words[4], state=words[5], outcome=words[6], result=words[7],
            reason=words[8], trace=tuple(words[9:14]), reserved=tuple(words[14:16]),
        ))
    return tuple(receipts)


def decode_task_request(machine: NovaMachine) -> TaskRequest:
    base = TASK_REQUEST_ADDRESS
    fields = [_u64(machine, base + i * 8) for i in range(TASK_REQUEST_SIZE // 8)]
    return TaskRequest(*fields)


def decode_task_response(machine: NovaMachine) -> TaskResponse:
    base = TASK_RESPONSE_ADDRESS
    fields = [_u64(machine, base + i * 8) for i in range(TASK_RESPONSE_SIZE // 8)]
    return TaskResponse(*fields)


def decode_task_snapshot(machine: NovaMachine) -> TaskSnapshot:
    return TaskSnapshot(
        state=decode_task_state(machine),
        tcbs=decode_task_tcbs(machine),
        ready=decode_task_ready_queue(machine),
        receipts=decode_task_receipts(machine),
        request=decode_task_request(machine),
        response=decode_task_response(machine),
    )


def validate_task_state(state: TaskState) -> None:
    if state.magic != TASK_STATE_MAGIC:
        raise NpkTaskError(f"bad task state magic: {state.magic:#x}")
    if state.version != TASK_STATE_VERSION:
        raise NpkTaskError(f"bad task state version: {state.version}")
    if state.task_slots != TASK_SLOTS:
        raise NpkTaskError(f"bad task slot count: {state.task_slots}")


def validate_task_tcb(tcb: TaskTcb) -> None:
    if tcb.state not in {TASK_STATE_FREE, TASK_STATE_CREATED, TASK_STATE_READY,
                         TASK_STATE_RUNNING, TASK_STATE_BLOCKED, TASK_STATE_TERMINATED}:
        raise NpkTaskError(f"bad task state: {tcb.state}")
    if tcb.state == TASK_STATE_FREE and tcb.id != 0:
        raise NpkTaskError("free task must not have id")
    if tcb.state != TASK_STATE_FREE and tcb.id == 0:
        raise NpkTaskError("live task missing id")


def validate_task_receipts(receipts: tuple[core_api.Receipt, ...]) -> None:
    seen = set()
    for r in receipts:
        if r.magic == 0:
            continue
        core_api.validate_receipt_structure(r)
        if r.type not in {OP_TASK_CREATE, OP_TASK_TERMINATE, OP_TASK_YIELD,
                          OP_TASK_BLOCK, OP_TASK_WAKE, OP_TASK_QUERY, OP_TASK_DESTROY}:
            raise NpkTaskError(f"unexpected task receipt type: {r.type}")
        if r.id in seen:
            raise NpkTaskError(f"duplicate task receipt id: {r.id}")
        seen.add(r.id)


def validate_task_snapshot(snapshot: TaskSnapshot) -> None:
    validate_task_state(snapshot.state)
    for tcb in snapshot.tcbs:
        validate_task_tcb(tcb)
    validate_task_receipts(snapshot.receipts)
    live = [t for t in snapshot.tcbs if t.state != TASK_STATE_FREE]
    if snapshot.state.task_count != len(live):
        raise NpkTaskError("task_count mismatch")


def write_task_request(machine: NovaMachine, *, operation: int, id: int, task: int = 0,
                       arg0: int = 0, arg1: int = 0, arg2: int = 0, arg3: int = 0,
                       arg4: int = 0, arg5: int = 0, arg6: int = 0, arg7: int = 0) -> None:
    base = TASK_REQUEST_ADDRESS
    words = [TASK_REQUEST_MAGIC, operation, id, task, arg0, arg1, arg2, arg3, arg4, arg5, arg6, arg7]
    for i, w in enumerate(words):
        machine._write_mem(base + i * 8, w & 0xFFFFFFFFFFFFFFFF, 8)


def decode_task_entry(machine: NovaMachine) -> int:
    return _u64(machine, TASK_ENTRY_ADDRESS)


def call_task_entry(machine: NovaMachine, *, code_address: int = 0xA0000,
                    max_steps: int = 400_000) -> None:
    """Branch to the published task entry through real guest instructions."""
    from ..assembler import AssemblyError, assemble_text
    entry = decode_task_entry(machine)
    if entry == 0:
        raise NpkTaskError("task entry not published")
    try:
        probe = assemble_text(f"LI ra, {code_address}\nBL 0x{entry:x}\nHALT\n")
        return_address = code_address + len(probe) - 4
        code = assemble_text(f"LI ra, {return_address}\nBL 0x{entry:x}\nHALT\n")
    except AssemblyError as exc:
        raise NpkTaskError(f"could not build task call stub: {exc}") from exc
    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.halted = False
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    if not machine.halted:
        raise NpkTaskError("task request did not return")
    if machine.exception_code != 0:
        raise NpkTaskError(f"task request raised exception {machine.exception_code}")


def call_task_request(machine: NovaMachine, *, operation: int, id: int, task: int = 0,
                      arg0: int = 0, arg1: int = 0, arg2: int = 0, arg3: int = 0,
                      arg4: int = 0, arg5: int = 0, arg6: int = 0, arg7: int = 0,
                      code_address: int = 0xA0000, max_steps: int = 400_000) -> TaskResponse:
    write_task_request(machine, operation=operation, id=id, task=task, arg0=arg0, arg1=arg1,
                       arg2=arg2, arg3=arg3, arg4=arg4, arg5=arg5, arg6=arg6, arg7=arg7)
    call_task_entry(machine, code_address=code_address, max_steps=max_steps)
    return decode_task_response(machine)


def task_receipt_expectation(*, id: int, type: int, outcome: int, result: int = 0,
                             reason: int = core_api.REASON_NONE, pu: int = 0,
                             stage: int = core_api.STAGE_COMPLETE,
                             state: int = core_api.STATE_COMPLETE,
                             trace=core_api.EXPECTED_STAGE_TRACE) -> core_api.ReceiptExpectation:
    return core_api.ReceiptExpectation(id=id, type=type, pu=pu, outcome=outcome, result=result,
                                      reason=reason, stage=stage, state=state, trace=trace)
