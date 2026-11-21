"""NPK Milestone 3: real exception entry, trap processing, and MRET return.

Every NPK trap test boots the real deterministic kernel image and drives the
installed machine-mode vector on ``NovaMachine``. Host helpers read the fixed
observation regions; the kernel, not the test, writes the trap frame, dispatch
state, processing-core counters, and trap receipts. The few ``inject_exception``
cases are explicitly host-driven platform cases that are awkward to provoke
with a guest instruction on the reference emulator (MMU faults and unsupported
exception classes).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nova import npk
from nova.assembler import assemble_text
from nova.disassembler import disassemble_word
from nova.emulator import NovaMachine
from nova.isa import (
    ECAUSE,
    ECODE,
    EXCEPTION_CODES,
    EXCEPTION_STACK_DEPTH,
    PRIVILEGE_LEVELS,
    PSR,
    TVAL,
    WORD_MASK,
)
from nova.npk import core as core_api
from nova.npk import traps
from nova.syscalls import SYSCALLS_BY_NAME

CODE_ADDRESS = 0xA0000
PSR_FLAGS = 0b1010
TVAL_SENTINEL = 0x1122334455667788
MAX_TRAP_STEPS = 50_000


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(npk.build_kernel(tmp_path_factory.mktemp("npk-m3")))


def boot_fresh(image):
    result = npk.boot_kernel(image)
    result.machine.halted = False
    return result


def install_code(machine, source=None, *, raw=None):
    code = assemble_text(source) if source is not None else raw
    assert code is not None
    machine.load_code(code, CODE_ADDRESS)
    machine.pc = CODE_ADDRESS
    machine.priv = PRIVILEGE_LEVELS["user"]
    return CODE_ADDRESS


def register_pattern():
    return {
        index: (0xA5A5000000000000 + index * 0x0101010101010101) & WORD_MASK
        for index in range(1, 32)
    }


def set_registers(machine, values):
    for index, value in values.items():
        machine.write_register(index, value)


def write_privileged_csr(machine, index, value):
    """Set a machine-only CSR from the harness without changing guest privilege."""
    privilege = machine.priv
    machine.priv = PRIVILEGE_LEVELS["machine"]
    machine._write_csr(index, value)
    machine.priv = privilege


def read_privileged_csr(machine, index):
    """Read a machine-only CSR from the harness without changing guest privilege."""
    privilege = machine.priv
    machine.priv = PRIVILEGE_LEVELS["machine"]
    value = machine._read_csr(index)
    machine.priv = privilege
    return value


def run_trap(machine, expected_pc, *, max_steps=MAX_TRAP_STEPS):
    """Run from an exception entry until MRET restores the expected PC."""
    machine.halted = False
    seen_exception = machine.exception_code != EXCEPTION_CODES["NONE"]
    for step in range(1, max_steps + 1):
        if (seen_exception
                and machine.exception_code == EXCEPTION_CODES["NONE"]
                and machine.pc == expected_pc):
            return step
        if machine.halted:
            break
        machine.step()
        if machine.exception_code != EXCEPTION_CODES["NONE"]:
            seen_exception = True
    raise AssertionError(
        f"trap did not return to 0x{expected_pc:x}: pc=0x{machine.pc:x}, "
        f"ecode={machine.exception_code}, halted={machine.halted}"
    )


def run_until_dispatch_state(machine, *, want_receipt=True,
                             max_steps=MAX_TRAP_STEPS):
    """Stop after the C dispatcher has published its outcome."""
    machine.halted = False
    for step in range(1, max_steps + 1):
        count = machine._read_mem(npk.TRAP_STATE_ADDRESS + 16, 8)
        status = machine._read_mem(npk.TRAP_STATE_ADDRESS + 32, 8)
        receipt_magic = machine._read_mem(npk.TRAP_RECEIPT_TABLE_ADDRESS, 8)
        if count > 0 and status != 0 and (not want_receipt or receipt_magic):
            return step
        if machine.halted:
            break
        machine.step()
    raise AssertionError("trap dispatcher did not publish an outcome")


def simple_trap_receipt(machine):
    receipts = traps.decode_trap_receipts(machine)
    assert receipts[0].magic == core_api.RECEIPT_MAGIC
    return receipts[0]


def test_milestone3_layout_regions_are_defined_and_non_overlapping():
    assert npk.TRAP_SCRATCH_ADDRESS == 0x200
    assert npk.TRAP_SCRATCH_SIZE == 24
    assert npk.TRAP_STACK_TOP == 0x88000
    assert npk.TRAP_FRAME_ADDRESS == 0x90900
    assert npk.TRAP_FRAME_SIZE == 304
    assert npk.TRAP_FRAME_ALIGN == 16
    assert npk.TRAP_STATE_ADDRESS == 0x90A40
    assert npk.TRAP_STATE_SIZE == 128
    assert npk.TRAP_RECEIPT_TABLE_ADDRESS == 0x90B00
    assert npk.TRAP_RECEIPT_SLOTS == 8

    frame_end = npk.TRAP_FRAME_ADDRESS + npk.TRAP_FRAME_SIZE
    state_end = npk.TRAP_STATE_ADDRESS + npk.TRAP_STATE_SIZE
    receipt_end = (npk.TRAP_RECEIPT_TABLE_ADDRESS
                   + npk.RECEIPT_SIZE * npk.TRAP_RECEIPT_SLOTS)
    assert frame_end <= npk.TRAP_STATE_ADDRESS
    assert state_end <= npk.TRAP_RECEIPT_TABLE_ADDRESS
    assert receipt_end <= CODE_ADDRESS
    assert npk.TRAP_STACK_TOP > npk.BOOT_STACK_TOP
    assert npk.TRAP_SCRATCH_ADDRESS + npk.TRAP_SCRATCH_SIZE <= npk.KERNEL_IMAGE_BASE


def test_trap_frame_offsets_and_alignment_are_exact():
    assert traps.GPR_COUNT == 32
    assert traps.gpr_offset(0) == 0
    assert traps.gpr_offset(15) == 120
    assert traps.gpr_offset(16) == 128
    assert traps.gpr_offset(31) == 248
    assert traps.PSR_OFFSET == 256
    assert traps.EPC_OFFSET == 264
    assert traps.ECAUSE_OFFSET == 272
    assert traps.ECODE_OFFSET == 280
    assert traps.HANDLER_PRIV_OFFSET == 288
    assert traps.TVAL_OFFSET == 296
    assert traps.TVAL_OFFSET + 8 == npk.TRAP_FRAME_SIZE
    assert npk.TRAP_FRAME_ADDRESS % npk.TRAP_FRAME_ALIGN == 0
    traps.validate_trap_layout()


def test_build_installs_a_real_nova_exception_vector(image):
    result = npk.boot_kernel(image)
    first = disassemble_word(result.machine._read_mem(result.vbar, 4))
    assert first is not None
    assert first.startswith("STD")
    words = [
        int.from_bytes(image.image[offset:offset + 4], "little")
        for offset in range(0, len(image.image), 4)
    ]
    assert any(disassemble_word(word) == "MRET" for word in words)


def test_boot_initializes_trap_observation_state(image):
    result = boot_fresh(image)
    state = traps.decode_trap_state(result.machine)
    assert state.magic == traps.TRAP_STATE_MAGIC
    assert state.version == traps.TRAP_STATE_VERSION
    assert state.dispatch_count == 0
    assert state.last_op_slot == core_api.NO_INDEX
    assert state.receipt_index == core_api.NO_INDEX
    assert state.reserved == (0, 0)


def test_system_call_frame_captures_every_gpr_and_exception_state(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "SCALL 5\nHALT\n")
    values = register_pattern()
    set_registers(machine, values)
    machine.write_register(1, 0)
    machine.psr = PSR_FLAGS
    write_privileged_csr(machine, TVAL, TVAL_SENTINEL)

    run_trap(machine, CODE_ADDRESS + 4)

    frame = traps.decode_trap_frame(machine)
    traps.validate_trap_frame(frame)
    # M6 publishes a0/a1 into the saved frame before MRET. Every other GPR is
    # still the exact interrupted register captured by the M3 vector.
    assert frame.gpr == (
        0, WORD_MASK, npk.ERR_NOT_FOUND,
        *[values[i] for i in range(3, 32)]
    )
    assert machine.read_register("a0") == WORD_MASK
    assert machine.read_register("a1") == npk.ERR_NOT_FOUND
    assert frame.psr == PSR_FLAGS
    assert frame.epc == CODE_ADDRESS + 4
    assert frame.ecause == 5
    assert frame.ecode == EXCEPTION_CODES["SYSTEM_CALL"]
    assert frame.handler_priv == PRIVILEGE_LEVELS["machine"]
    assert frame.tval == TVAL_SENTINEL

    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert state.classification == traps.CLASS_SYSTEM_CALL
    assert state.action == traps.ACTION_RESUME_NEXT
    assert state.last_ecode == EXCEPTION_CODES["SYSTEM_CALL"]
    assert state.last_ecause == 5
    assert state.last_epc == CODE_ADDRESS + 4


def test_mret_restores_registers_csrs_privilege_and_exact_pc(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "SCALL 9\nHALT\n")
    values = register_pattern()
    set_registers(machine, values)
    machine.psr = PSR_FLAGS
    write_privileged_csr(machine, TVAL, TVAL_SENTINEL)

    run_trap(machine, CODE_ADDRESS + 4)

    # An invalid syscall number is rejected, so MRET restores the published
    # -1/ERR_UNSUPPORTED in a0/a1 and the original value in every other GPR.
    assert [machine.read_register(i) for i in range(32)] == [
        0, WORD_MASK, npk.ERR_UNSUPPORTED,
        *[values[i] for i in range(3, 32)]
    ]
    assert machine.psr == PSR_FLAGS
    assert read_privileged_csr(machine, TVAL) == TVAL_SENTINEL
    assert machine.exception_code == EXCEPTION_CODES["NONE"]
    assert machine.ecause == 9
    assert machine.epc == CODE_ADDRESS + 4
    assert machine.pc == CODE_ADDRESS + 4
    assert machine.priv == PRIVILEGE_LEVELS["user"]
    assert machine.exception_stack_depth == 0


def test_software_trap_is_classified_and_resumes_after_the_trap(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "TRAP 42\nHALT\n")

    run_trap(machine, CODE_ADDRESS + 4)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["SOFTWARE_TRAP"]
    assert frame.ecause == 42
    assert frame.epc == CODE_ADDRESS + 4
    assert state.classification == traps.CLASS_SOFTWARE_TRAP
    assert state.action == traps.ACTION_RESUME_NEXT
    assert machine.pc == CODE_ADDRESS + 4


def test_illegal_instruction_is_classified_with_faulting_pc(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, raw=(0xFFFFFFFF).to_bytes(4, "little") + assemble_text("HALT\n"))

    run_trap(machine, CODE_ADDRESS)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["ILLEGAL_INSTRUCTION"]
    assert frame.ecause == 0xFFFFFFFF
    assert frame.epc == CODE_ADDRESS
    assert state.classification == traps.CLASS_ILLEGAL_INSTRUCTION
    assert state.action == traps.ACTION_REPORT_RESUME
    assert machine.pc == CODE_ADDRESS


def test_alignment_fault_is_classified_and_returns_to_faulting_pc(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "LDD r1, [r0 + 1]\nHALT\n")

    run_trap(machine, CODE_ADDRESS)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["ALIGNMENT_FAULT"]
    assert frame.ecause == 1
    assert frame.epc == CODE_ADDRESS
    assert state.classification == traps.CLASS_ALIGNMENT_FAULT
    assert state.last_ecode == EXCEPTION_CODES["ALIGNMENT_FAULT"]
    assert machine.pc == CODE_ADDRESS


def test_memory_fault_is_classified_with_faulting_pc(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "LI r5, 0xFFFFFFFFFFFFFFF8\nLDD r1, [r5 + 8]\nHALT\n")

    run_trap(machine, CODE_ADDRESS + 36)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["MEMORY_FAULT"]
    assert frame.ecause == 0
    assert frame.epc == CODE_ADDRESS + 36
    assert frame.gpr[5] == 0xFFFFFFFFFFFFFFF8
    assert state.classification == traps.CLASS_MEMORY_FAULT
    assert machine.pc == CODE_ADDRESS + 36


def test_mmu_fault_classification_uses_the_real_vector(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "HALT\nHALT\n")
    machine.inject_exception(
        EXCEPTION_CODES["MMU_FAULT"], 0x5, return_pc=CODE_ADDRESS + 4
    )

    run_trap(machine, CODE_ADDRESS + 4)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["MMU_FAULT"]
    assert frame.ecause == 5
    assert state.classification == traps.CLASS_MMU_FAULT
    assert state.action == traps.ACTION_REPORT_RESUME


def test_mmu_fault_preserves_tval_in_the_frame_and_after_return(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "HALT\nHALT\n")
    write_privileged_csr(machine, TVAL, TVAL_SENTINEL)
    machine.inject_exception(
        EXCEPTION_CODES["MMU_FAULT"], 0x25, return_pc=CODE_ADDRESS + 4
    )

    run_trap(machine, CODE_ADDRESS + 4)

    frame = traps.decode_trap_frame(machine)
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert frame.ecode == EXCEPTION_CODES["MMU_FAULT"]
    assert frame.ecause == 0x25
    assert frame.tval == TVAL_SENTINEL
    assert state.last_tval == TVAL_SENTINEL
    assert read_privileged_csr(machine, TVAL) == TVAL_SENTINEL


def test_unknown_exception_code_is_rejected_with_its_own_receipt(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "HALT\nHALT\n")
    machine.inject_exception(99, 0, return_pc=CODE_ADDRESS + 4)

    run_trap(machine, CODE_ADDRESS + 4)

    state = traps.decode_trap_state(machine)
    assert state.last_status == traps.TRAP_STATUS_ADVANCE
    assert state.last_reason == core_api.REASON_TRAP_BAD_ECODE
    assert state.result == 0
    receipt = simple_trap_receipt(machine)
    core_api.validate_receipt_structure(receipt)
    expected = core_api.ReceiptExpectation(
        id=state.dispatch_count,
        type=npk.OP_TRAP_PROCESS,
        pu=0,
        outcome=core_api.OUTCOME_REJECTED,
        result=0,
        reason=core_api.REASON_TRAP_BAD_ECODE,
        stage=core_api.STAGE_VALIDATE,
        state=core_api.STATE_REJECTED,
        trace=(core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE, 0, 0, 0),
    )
    core_api.validate_receipt(receipt, expected)
    assert core_api.decode_core_snapshot(machine).core.rejected == 4


def test_malformed_frame_is_rejected_before_publication(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "HALT\nHALT\n")
    machine.inject_exception(
        EXCEPTION_CODES["SYSTEM_CALL"], 7, return_pc=CODE_ADDRESS + 1
    )

    run_until_dispatch_state(machine)

    state = traps.decode_trap_state(machine)
    assert state.last_status == traps.TRAP_STATUS_ADVANCE
    assert state.last_reason == core_api.REASON_TRAP_BAD_FRAME
    assert state.result == 0
    receipt = simple_trap_receipt(machine)
    assert receipt.outcome == core_api.OUTCOME_REJECTED
    assert receipt.reason == core_api.REASON_TRAP_BAD_FRAME
    assert receipt.result == 0
    assert receipt.stage == core_api.STAGE_VALIDATE


def test_unsupported_exception_class_is_failed_not_completed(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "HALT\nHALT\n")
    machine.inject_exception(
        EXCEPTION_CODES["ARITHMETIC"], 0, return_pc=CODE_ADDRESS + 4
    )

    run_trap(machine, CODE_ADDRESS + 4)

    state = traps.decode_trap_state(machine)
    assert state.classification == traps.CLASS_UNSUPPORTED
    assert state.action == traps.ACTION_UNSUPPORTED
    assert state.last_status == traps.TRAP_STATUS_ADVANCE
    assert state.last_reason == core_api.REASON_TRAP_UNSUPPORTED
    assert state.result == 0
    receipt = simple_trap_receipt(machine)
    expected = core_api.ReceiptExpectation(
        id=state.dispatch_count,
        type=npk.OP_TRAP_PROCESS,
        pu=0,
        outcome=core_api.OUTCOME_FAILED,
        result=0,
        reason=core_api.REASON_TRAP_UNSUPPORTED,
        stage=core_api.STAGE_EXECUTE,
        state=core_api.STATE_FAILED,
        trace=(core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE,
               core_api.STAGE_EXECUTE, 0, 0),
    )
    core_api.validate_receipt(receipt, expected)
    assert core_api.decode_core_snapshot(machine).core.failed == 3


def test_corrupt_trap_state_is_reported_without_running_an_operation(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "SCALL 1\nHALT\n")
    machine._write_mem(npk.TRAP_STATE_ADDRESS, 0, 8)

    run_trap(machine, CODE_ADDRESS + 4)

    state = traps.decode_trap_state(machine)
    assert state.last_status == traps.TRAP_STATUS_BAD_STATE
    assert state.dispatch_count == 0
    assert state.last_op_slot == core_api.NO_INDEX
    assert machine._read_mem(npk.TRAP_RECEIPT_TABLE_ADDRESS, 8) == 0
    core = core_api.decode_core_snapshot(machine).core
    assert (core.admitted, core.completed, core.rejected, core.failed) == (10, 7, 3, 2)


def test_trap_receipt_is_validated_independently_of_kernel_status(image):
    result = boot_fresh(image)
    machine = result.machine
    install_code(machine, "SCALL 3\nHALT\n")

    run_trap(machine, CODE_ADDRESS + 4)

    state = traps.decode_trap_state(machine)
    receipt = simple_trap_receipt(machine)
    expectation = traps.trap_receipt_expectation(state)
    core_api.validate_receipt(receipt, expectation)
    with pytest.raises(core_api.NpkCoreError, match="receipt id mismatch"):
        core_api.validate_receipt(receipt, replace(expectation, id=99))


def test_trap_dispatch_does_not_disturb_m1_or_m2_receipt_evidence(image):
    result = boot_fresh(image)
    machine = result.machine
    # Use a non-syscall trap so the assertion remains about M3 trap dispatch;
    # syscall operations legitimately add their own receipts and core counters.
    install_code(machine, "TRAP 4\nHALT\n")

    run_trap(machine, CODE_ADDRESS + 4)

    assert result.status == 0
    npk.validate_boot_receipt(npk.decode_processing_receipt(machine))
    snapshot = core_api.decode_core_snapshot(machine)
    assert len(snapshot.receipts) == npk.RECEIPT_SLOTS
    for index, receipt in enumerate(snapshot.receipts):
        core_api.validate_receipt(receipt, npk.expected_receipt(index))
    core = snapshot.core
    assert (core.admitted, core.completed, core.rejected, core.failed) == (11, 8, 3, 2)
    assert core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)


def test_nova_exception_stack_is_four_deep_and_discards_oldest_frame():
    machine = NovaMachine(assemble_text("MRET\n"), entry=0x3000)
    machine.pc = 0x3000
    machine.vbar = 0x3000
    machine.priv = PRIVILEGE_LEVELS["machine"]
    for index in range(EXCEPTION_STACK_DEPTH + 3):
        machine.inject_exception(
            EXCEPTION_CODES["SYSTEM_CALL"], index,
            return_pc=0x4000 + index * 4,
        )
        expected_depth = min(index + 1, EXCEPTION_STACK_DEPTH)
        assert machine.exception_stack_depth == expected_depth

    frames = machine.exception_stack()
    assert len(frames) == EXCEPTION_STACK_DEPTH
    assert [frame[1] for frame in frames] == [EXCEPTION_CODES["SYSTEM_CALL"]] * 4
    assert [frame[2] for frame in frames] == [3, 4, 5, 6]
    machine.step()
    assert machine.exception_stack_depth == EXCEPTION_STACK_DEPTH - 1
    assert machine.pc == 0x4000 + 6 * 4


def test_system_call_numbers_for_planned_object_operations_are_preserved():
    assert SYSCALLS_BY_NAME["SYS_OBJ_CREATE"].number == 4
    assert SYSCALLS_BY_NAME["SYS_OBJ_SIZE"].number == 5
    assert SYSCALLS_BY_NAME["SYS_OBJ_DESTROY"].number == 6


def test_documentation_states_the_frame_size_return_and_nesting_limit():
    docs = Path(__file__).resolve().parents[1] / "docs"
    architecture = (docs / "NPK_ARCHITECTURE.md").read_text(encoding="utf-8")
    plan = (docs / "PHASE2_PLAN.md").read_text(encoding="utf-8")
    assert "304-byte" in architecture
    assert "MRET" in architecture
    assert "nested" in architecture.lower()
    assert "not supported" in architecture.lower()
    assert "10." in plan or "Milestone 10" in plan
    assert "Object Manager" in plan
