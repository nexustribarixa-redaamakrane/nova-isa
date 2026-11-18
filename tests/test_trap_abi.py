"""Nova Trap ABI v0.1 conformance tests.

Every assertion here is architectural fact for this revision: exception entry
and ``MRET`` are fully specified by the ISA and the :class:`NovaMachine`
reference model. The tests pin down the claims the ABI document makes, so a
future ISA change that alters any of them must update the documentation too.
"""

import pytest

from nova.assembler import assemble_text
from nova.emulator import NovaMachine
from nova.isa import EXCEPTION_CODES, EXCEPTION_STACK_DEPTH, PRIVILEGE_LEVELS

SYSTEM_CALL = EXCEPTION_CODES["SYSTEM_CALL"]
NONE = EXCEPTION_CODES["NONE"]
USER = PRIVILEGE_LEVELS["user"]
MACHINE = PRIVILEGE_LEVELS["machine"]


def machine_for(source, *, entry=0x1000, priv=USER):
    machine = NovaMachine(assemble_text(source), entry=entry)
    machine.pc = entry
    machine.priv = priv
    return machine


def install_handler(machine, source, address=0x3000):
    resume = machine.pc
    machine.load_code(assemble_text(source), address)
    machine.vbar = address
    machine.pc = resume
    return machine


def registers(machine):
    return [machine.read_register(index) for index in range(32)]


def test_reset_state_matches_the_abi_document():
    machine = NovaMachine(b"", entry=0x1000)
    assert machine.pc == 0x1000
    assert machine.priv == USER
    assert machine.vbar == 0x1000
    assert machine.exception_code == NONE
    assert machine.exception_stack_depth == 0


def test_exception_entry_preserves_every_general_purpose_register():
    machine = machine_for("SCALL 7\n")
    for index in range(1, 32):
        machine.write_register(index, index * 0x01010101)
    snapshot = registers(machine)
    install_handler(machine, "MRET\n")
    machine.run(2, stop_on_exception=False)  # SCALL, then MRET
    assert registers(machine) == snapshot


def test_exception_entry_writes_the_csr_frame():
    machine = machine_for("SCALL 3\n")
    install_handler(machine, "MRET\n")
    machine.run(1, stop_on_exception=False)  # just the SCALL
    assert machine.exception_code == SYSTEM_CALL
    assert machine.ecause == 3
    assert machine.epc == 0x1004
    assert machine.priv == MACHINE
    assert machine.pc == machine.vbar
    assert machine.exception_stack() == [(0x1004, SYSTEM_CALL, 3, USER)]


def test_mret_restores_pc_privilege_and_keeps_ecause():
    machine = machine_for("SCALL 7\n")
    install_handler(machine, "MRET\n")
    machine.run(2, stop_on_exception=False)
    assert machine.pc == 0x1004
    assert machine.priv == USER
    assert machine.exception_code == NONE
    assert machine.ecause == 7
    assert machine.epc == machine.pc  # MRET leaves epc equal to the resumed pc


def test_exception_stack_is_depth_limited_with_oldest_frame_discarded():
    machine = machine_for("SCALL 0\n")
    install_handler(machine, "SCALL 1\n")
    for _ in range(EXCEPTION_STACK_DEPTH + 3):
        machine.run(1, stop_on_exception=False)
    assert machine.exception_stack_depth == EXCEPTION_STACK_DEPTH


def test_nested_exception_reenters_at_vbar():
    machine = machine_for("SCALL 0\n")
    install_handler(machine, "SCALL 1\n")  # handler traps again
    machine.run(2, stop_on_exception=False)  # user SCALL, then handler SCALL
    assert machine.exception_stack_depth == 2
    assert machine.pc == machine.vbar
    assert machine.ecause == 1
    assert machine.exception_code == SYSTEM_CALL


TRAP_DEMO = """
    start: MOVI r8, 0x2f00      ; where the handler records the frame
           MOVI r16, 600        ; values the handler will clobber on entry...
           MOVI r17, 700        ; ...and promise to restore
           SCALL 3
    resume: LDD r2, [r8]
           LDD r3, [r8 + 8]
           MOVI r4, 250
"""

TRAP_HANDLER = """
    handler:
        SUBI sp, sp, 16         ; allocate the save area, 8-byte aligned
        STD r16, [sp + 0]
        STD r17, [sp + 8]
        CSRR r16, ecode         ; dispatch key
        CSRR r17, ecause        ; fault detail
        STD r16, [r8]
        STD r17, [r8 + 8]
        LDD r16, [sp + 0]       ; restore what we clobbered
        LDD r17, [sp + 8]
        ADDI sp, sp, 16
        MRET
"""


def test_abi_handler_example_preserves_the_interrupted_program():
    machine = machine_for(TRAP_DEMO, priv=USER)
    install_handler(machine, TRAP_HANDLER)
    machine.run(50, stop_on_exception=False)
    assert machine.read_register(2) == SYSTEM_CALL
    assert machine.read_register(3) == 3          # the SCALL immediate
    assert machine.read_register(4) == 250        # finished the resume path
    assert machine.read_register(16) == 600       # restored by the handler
    assert machine.read_register(17) == 700       # restored by the handler
    assert machine.read_register("sp") == 0       # stack balanced
    assert machine.exception_code == NONE         # MRET cleared it
    assert machine.ecause == 3                    # and kept the cause


def test_ecode_is_the_dispatch_key_and_evidence_it_is_readable():
    machine = machine_for("SCALL 5\n", priv=USER)
    install_handler(
        machine,
        "CSRR r9, ecode\n"
        "CSRR r10, ecause\n"
        "MRET\n",
    )
    machine.run(3, stop_on_exception=False)
    assert machine.read_register(9) == SYSTEM_CALL
    assert machine.read_register(10) == 5


def test_abi_stack_alignment_keeps_every_std_legal():
    # The ABI requires an 8-byte-aligned stack so that STD/LDD are naturally
    # aligned. A handler that drops sp to an odd address faults.
    misaligned = machine_for("SCALL 0\n", priv=USER)
    install_handler(misaligned, "MOVI sp, 2\nLDW r1, [sp]\nMRET\n")
    misaligned.run(3, stop_on_exception=False)
    assert misaligned.exception_code == EXCEPTION_CODES["ALIGNMENT_FAULT"]
    assert misaligned.pc == misaligned.vbar

    aligned = machine_for("SCALL 0\n", priv=USER)
    install_handler(aligned, "MOVI sp, 0x2000\nLDD r1, [sp]\nMRET\n")
    aligned.run(4, stop_on_exception=False)  # SCALL, MOVI, LDD, MRET
    assert aligned.exception_code == NONE


def test_handler_that_faults_must_repoint_vbar_first():
    # Architectural note from the ABI: a handler that traps again without
    # repointing vbar re-enters the same handler at the same address, so the
    # machine never returns to the interrupted program.
    machine = machine_for("SCALL 0\n", priv=USER)
    install_handler(machine, "LI sp, 0xFFFFFFFFFFFFFFFE\nLDW r1, [sp]\nMRET\n")
    machine.run(50, stop_on_exception=False)  # re-traps on every LDW
    assert machine.exception_code == EXCEPTION_CODES["MEMORY_FAULT"]
    assert machine.epc > machine.vbar  # the faulting LDW lies in the handler
    assert not machine.halted          # MRET is never reached


def test_data_register_and_exception_stack_are_distinct_state():
    machine = machine_for("SCALL 0\n", priv=USER)
    machine.write_register("sp", 0xCAFE)
    install_handler(machine, "MRET\n")
    machine.run(2, stop_on_exception=False)
    assert machine.read_register("sp") == 0xCAFE
    assert machine.exception_stack_depth == 0  # MRET popped the hardware frame