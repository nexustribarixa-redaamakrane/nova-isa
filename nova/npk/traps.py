"""Host-side decoding and validation of NPK Milestone 3 trap evidence.

The frame, state, and receipt regions are written by the real kernel trap path.
These helpers read them through the reference machine and check the observable
contract independently of the kernel's return value.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..emulator import NovaMachine
from ..isa import PRIVILEGE_LEVELS
from . import core as core_api
from .layout import (
    RECEIPT_SIZE,
    TRAP_FRAME_ADDRESS,
    TRAP_FRAME_ALIGN,
    TRAP_FRAME_SIZE,
    TRAP_RECEIPT_SLOTS,
    TRAP_RECEIPT_TABLE_ADDRESS,
    TRAP_STATE_ADDRESS,
    TRAP_STATE_SIZE,
)

__all__ = [
    "TRAP_STATE_MAGIC",
    "TRAP_STATE_VERSION",
    "OP_TRAP_PROCESS",
    "GPR_COUNT",
    "PSR_OFFSET",
    "EPC_OFFSET",
    "ECAUSE_OFFSET",
    "ECODE_OFFSET",
    "HANDLER_PRIV_OFFSET",
    "TVAL_OFFSET",
    "TRAP_STATUS_OK",
    "TRAP_STATUS_BAD_FRAME",
    "TRAP_STATUS_BAD_STATE",
    "TRAP_STATUS_ADMIT",
    "TRAP_STATUS_ADVANCE",
    "CLASS_NONE",
    "CLASS_SYSTEM_CALL",
    "CLASS_SOFTWARE_TRAP",
    "CLASS_ILLEGAL_INSTRUCTION",
    "CLASS_ALIGNMENT_FAULT",
    "CLASS_MEMORY_FAULT",
    "CLASS_MMU_FAULT",
    "CLASS_UNSUPPORTED",
    "ACTION_NONE",
    "ACTION_RESUME_NEXT",
    "ACTION_REPORT_RESUME",
    "ACTION_UNSUPPORTED",
    "TRAP_RESULT_PROCESSED",
    "TrapFrame",
    "TrapState",
    "gpr_offset",
    "validate_trap_layout",
    "decode_trap_frame",
    "decode_trap_state",
    "decode_trap_receipts",
    "validate_trap_frame",
    "validate_trap_state",
    "trap_receipt_expectation",
]


TRAP_STATE_MAGIC = 0x4E504B54  # "NPKT"
TRAP_STATE_VERSION = 3
OP_TRAP_PROCESS = 3

GPR_COUNT = 32
PSR_OFFSET = 256
EPC_OFFSET = 264
ECAUSE_OFFSET = 272
ECODE_OFFSET = 280
HANDLER_PRIV_OFFSET = 288
TVAL_OFFSET = 296

TRAP_STATUS_OK = 0
TRAP_STATUS_BAD_FRAME = 1
TRAP_STATUS_BAD_STATE = 2
TRAP_STATUS_ADMIT = 3
TRAP_STATUS_ADVANCE = 4

CLASS_NONE = 0
CLASS_SYSTEM_CALL = 1
CLASS_SOFTWARE_TRAP = 2
CLASS_ILLEGAL_INSTRUCTION = 3
CLASS_ALIGNMENT_FAULT = 4
CLASS_MEMORY_FAULT = 5
CLASS_MMU_FAULT = 6
CLASS_UNSUPPORTED = 7

ACTION_NONE = 0
ACTION_RESUME_NEXT = 1
ACTION_REPORT_RESUME = 2
ACTION_UNSUPPORTED = 3

TRAP_RESULT_PROCESSED = 0x54525058  # "TRPX"

_VALID_CLASSES = {
    CLASS_SYSTEM_CALL,
    CLASS_SOFTWARE_TRAP,
    CLASS_ILLEGAL_INSTRUCTION,
    CLASS_ALIGNMENT_FAULT,
    CLASS_MEMORY_FAULT,
    CLASS_MMU_FAULT,
    CLASS_UNSUPPORTED,
}
_VALID_ACTIONS = {ACTION_RESUME_NEXT, ACTION_REPORT_RESUME, ACTION_UNSUPPORTED}


class NpkTrapError(ValueError):
    """Raised when observable trap state is missing or inconsistent."""


__all__.append("NpkTrapError")


@dataclass(frozen=True)
class TrapFrame:
    """The 304-byte software frame written by the Nova assembly vector."""

    gpr: tuple[int, ...]
    psr: int
    epc: int
    ecause: int
    ecode: int
    handler_priv: int
    tval: int


@dataclass(frozen=True)
class TrapState:
    """Trap dispatch observations published by the kernel."""

    magic: int
    version: int
    dispatch_count: int
    last_op_slot: int
    last_status: int
    last_ecode: int
    last_ecause: int
    last_epc: int
    last_tval: int
    classification: int
    action: int
    result: int
    receipt_index: int
    last_reason: int
    reserved: tuple[int, ...]


def gpr_offset(index: int) -> int:
    """Return the 304-byte frame offset of general-purpose register ``index``."""
    if not 0 <= index < GPR_COUNT:
        raise ValueError(f"GPR index out of range: {index}")
    return index * 8


def validate_trap_layout() -> None:
    """Check the documented frame/state layout constants."""
    if TRAP_FRAME_SIZE != 304:
        raise NpkTrapError(f"trap frame size mismatch: {TRAP_FRAME_SIZE}")
    if TRAP_FRAME_ADDRESS % TRAP_FRAME_ALIGN != 0:
        raise NpkTrapError(
            f"trap frame 0x{TRAP_FRAME_ADDRESS:x} is not "
            f"{TRAP_FRAME_ALIGN}-byte aligned"
        )
    if TVAL_OFFSET + 8 != TRAP_FRAME_SIZE:
        raise NpkTrapError(
            f"trap frame fields end at {TVAL_OFFSET + 8}, not {TRAP_FRAME_SIZE}"
        )
    frame_end = TRAP_FRAME_ADDRESS + TRAP_FRAME_SIZE
    if TRAP_STATE_ADDRESS < frame_end:
        raise NpkTrapError("trap state overlaps the software trap frame")
    state_end = TRAP_STATE_ADDRESS + TRAP_STATE_SIZE
    if TRAP_RECEIPT_TABLE_ADDRESS < state_end:
        raise NpkTrapError("trap receipt table overlaps the trap state")


def decode_trap_frame(machine: NovaMachine) -> TrapFrame:
    """Decode the software frame from machine memory."""
    validate_trap_layout()

    def word(offset: int) -> int:
        return machine._read_mem(TRAP_FRAME_ADDRESS + offset, 8)

    return TrapFrame(
        gpr=tuple(word(gpr_offset(index)) for index in range(GPR_COUNT)),
        psr=word(PSR_OFFSET),
        epc=word(EPC_OFFSET),
        ecause=word(ECAUSE_OFFSET),
        ecode=word(ECODE_OFFSET),
        handler_priv=word(HANDLER_PRIV_OFFSET),
        tval=word(TVAL_OFFSET),
    )


def decode_trap_state(machine: NovaMachine) -> TrapState:
    """Decode the kernel's trap dispatch state."""

    def word(offset: int) -> int:
        return machine._read_mem(TRAP_STATE_ADDRESS + offset, 8)

    return TrapState(
        magic=word(0),
        version=word(8),
        dispatch_count=word(16),
        last_op_slot=word(24),
        last_status=word(32),
        last_ecode=word(40),
        last_ecause=word(48),
        last_epc=word(56),
        last_tval=word(64),
        classification=word(72),
        action=word(80),
        result=word(88),
        receipt_index=word(96),
        last_reason=word(104),
        reserved=(word(112), word(120)),
    )


def decode_trap_receipts(machine: NovaMachine) -> tuple[core_api.Receipt, ...]:
    """Decode the dedicated NPK trap receipt log."""

    def word(address: int) -> int:
        return machine._read_mem(address, 8)

    receipts = []
    for index in range(TRAP_RECEIPT_SLOTS):
        base = TRAP_RECEIPT_TABLE_ADDRESS + index * RECEIPT_SIZE
        receipts.append(core_api.Receipt(
            magic=word(base),
            id=word(base + 8),
            type=word(base + 16),
            pu=word(base + 24),
            stage=word(base + 32),
            state=word(base + 40),
            outcome=word(base + 48),
            result=word(base + 56),
            reason=word(base + 64),
            trace=tuple(word(base + 72 + i * 8) for i in range(5)),
            reserved=(word(base + 112), word(base + 120)),
        ))
    return tuple(receipts)


def validate_trap_frame(frame: TrapFrame) -> None:
    """Check invariant properties of a captured trap frame."""
    if len(frame.gpr) != GPR_COUNT:
        raise NpkTrapError(f"expected {GPR_COUNT} GPRs, found {len(frame.gpr)}")
    if frame.handler_priv != PRIVILEGE_LEVELS["machine"]:
        raise NpkTrapError(
            f"trap did not enter machine privilege: {frame.handler_priv}"
        )
    if frame.epc % 4 != 0:
        raise NpkTrapError(f"misaligned trap EPC: 0x{frame.epc:x}")
    if frame.ecode > 11:
        raise NpkTrapError(f"unknown exception code: {frame.ecode}")


def validate_trap_state(state: TrapState) -> None:
    """Check a successful trap dispatch observation."""
    if state.magic != TRAP_STATE_MAGIC:
        raise NpkTrapError(f"trap state magic mismatch: 0x{state.magic:x}")
    if state.version != TRAP_STATE_VERSION:
        raise NpkTrapError(f"trap state version mismatch: {state.version}")
    if state.dispatch_count == 0:
        raise NpkTrapError("no trap dispatch has been recorded")
    if state.last_status != TRAP_STATUS_OK:
        raise NpkTrapError(f"trap dispatch failed with status {state.last_status}")
    if state.classification not in _VALID_CLASSES:
        raise NpkTrapError(f"invalid trap classification: {state.classification}")
    if state.classification == CLASS_UNSUPPORTED:
        raise NpkTrapError("trap was classified as unsupported")
    if state.action not in _VALID_ACTIONS:
        raise NpkTrapError(f"invalid trap action: {state.action}")
    if state.result != TRAP_RESULT_PROCESSED:
        raise NpkTrapError(f"trap result was not committed: 0x{state.result:x}")
    if state.receipt_index >= TRAP_RECEIPT_SLOTS:
        raise NpkTrapError(f"invalid trap receipt index: {state.receipt_index}")
    if state.last_reason != core_api.REASON_NONE:
        raise NpkTrapError(f"successful trap has reason {state.last_reason}")


def trap_receipt_expectation(
    state: TrapState,
    *,
    outcome: int = core_api.OUTCOME_COMPLETE,
    reason: int = core_api.REASON_NONE,
    stage: int | None = core_api.STAGE_COMPLETE,
    trap_state: int | None = core_api.STATE_COMPLETE,
    result: int = TRAP_RESULT_PROCESSED,
    trace: tuple[int, ...] | None = core_api.EXPECTED_STAGE_TRACE,
) -> core_api.ReceiptExpectation:
    """Build the independent receipt expectation for one trap dispatch."""
    return core_api.ReceiptExpectation(
        id=state.dispatch_count,
        type=OP_TRAP_PROCESS,
        pu=0,
        outcome=outcome,
        result=result,
        reason=reason,
        stage=stage,
        state=trap_state,
        trace=trace,
    )
