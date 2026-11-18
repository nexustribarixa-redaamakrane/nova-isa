"""Tests for the control/status register and exception model.

These cover the ``CSRR`` and ``CSRW`` instructions, CSR privilege and WARL
rules, the exception save stack that ``MRET`` unwinds, and the v1.0 virtual
memory, interrupt, and virtualization CSR file.
"""

import pytest

from nova.assembler import assemble_text
from nova.disassembler import disassemble_bytes
from nova.emulator import NovaMachine
from nova.isa import (
    CSR_BY_NAME,
    CSR_TABLE,
    EXCEPTION_CODES,
    EXCEPTION_STACK_DEPTH,
    FLAG_CARRY,
    FLAG_NEGATIVE,
    FLAG_OVERFLOW,
    FLAG_ZERO,
    INSTRUCTION_TABLE,
    RESET_VBAR,
    RESERVED_CSR_INDICES,
    WORD_MASK,
)

CODE = EXCEPTION_CODES
Z = 1 << FLAG_ZERO
N = 1 << FLAG_NEGATIVE
C = 1 << FLAG_CARRY
V = 1 << FLAG_OVERFLOW


def machine_for(source, *, entry=0x1000):
    """Machine with ``source`` loaded at ``entry`` and the program counter there.

    The exception vector at ``vbar`` is deliberately left empty unless a test
    installs a handler, so that entry state can be observed without executing
    anything at the vector.
    """
    machine = NovaMachine(assemble_text(source), entry=entry)
    machine.pc = entry
    return machine


def install_handler(machine, source, address):
    """Load a handler at ``address`` and point ``vbar`` at it.

    ``NovaMachine.load_code`` leaves the program counter at the load address, so
    the interrupted program counter is restored afterwards.
    """
    resume = machine.pc
    machine.load_code(assemble_text(source), address)
    machine.vbar = address
    machine.pc = resume
    return machine


def at_machine(machine):
    """Enter the machine directly, as reset firmware would."""
    machine.priv = 2
    return machine


# --------------------------------------------------------------------------
# The CSR file itself
# --------------------------------------------------------------------------


def test_csr_table_shape_and_access_rules():
    assert [csr.index for csr in CSR_TABLE.values()] == list(range(len(CSR_TABLE)))
    assert [csr.name for csr in CSR_TABLE.values()] == [
        "psr", "epc", "ecause", "ecode", "priv", "vbar", "coreid",
        "ptbr", "tval", "ie", "ip", "ivec", "time", "timecmp",
        "vpc", "vpriv", "vptbr", "vexit", "hstatus",
    ]
    for csr in CSR_TABLE.values():
        assert csr.mask and csr.mask & ~WORD_MASK == 0
        assert csr.reset & ~csr.mask == 0
        assert csr.readable
    assert CSR_BY_NAME["coreid"].writable is False
    for name in ("epc", "ecause", "ecode", "vbar"):
        assert CSR_BY_NAME[name].min_write_privilege == "machine"
        assert CSR_BY_NAME[name].min_read_privilege == "machine"
    for name in ("psr", "priv", "coreid"):
        assert CSR_BY_NAME[name].min_read_privilege == "user"
    assert CSR_BY_NAME["priv"].min_write_privilege == "machine"
    assert CSR_BY_NAME["psr"].min_write_privilege == "user"


def test_alignment_and_reset_rules():
    assert CSR_BY_NAME["epc"].align4 is True
    assert CSR_BY_NAME["vbar"].align4 is True
    assert CSR_BY_NAME["vbar"].reset == RESET_VBAR
    assert CSR_BY_NAME["priv"].reset == 0
    assert CSR_BY_NAME["psr"].mask == 0xF
    assert RESERVED_CSR_INDICES == tuple(range(19, 32))
    assert CSR_BY_NAME["ptbr"].align4 is False
    assert CSR_BY_NAME["ivec"].align4 is True
    assert CSR_BY_NAME["timecmp"].reset == WORD_MASK
    assert CSR_BY_NAME["time"].writable is False


def test_every_csr_instruction_operand_parses_names_and_indices():
    for mnemonic in ("CSRR", "CSRW"):
        definition = INSTRUCTION_TABLE[mnemonic]
        assert definition.csr_operand() is not None
        assert definition.privilege == "user"
    for name in ("psr", "priv", "coreid", "vbar", "epc", "ecause", "ecode"):
        assert assemble_text(f"CSRR r1, {name}\n")
        assert assemble_text(f"CSRW {name}, r1\n")
        assert assemble_text(f"CSRR r1, #{CSR_BY_NAME[name].index}\n")
    # Reserved indices are encodable so that software can probe them.
    assert assemble_text("CSRR r1, #19\n")
    assert assemble_text("CSRW #31, r1\n")


# --------------------------------------------------------------------------
# CSRR / CSRW behaviour
# --------------------------------------------------------------------------


def test_csrr_reads_every_user_readable_csr():
    machine = machine_for("CSRR r1, psr\nCSRR r2, coreid\nCSRR r3, priv\n")
    machine.psr = 0x5
    machine.run(3, stop_on_exception=False)
    assert machine.regs[1] == 0x5
    assert machine.regs[2] == 0
    assert machine.regs[3] == 0


def test_csrw_writes_at_the_documented_privileges():
    machine = machine_for("CSRW psr, r1\n")
    machine.regs[1] = 0xA
    machine.run(1, stop_on_exception=False)
    assert machine.psr == 0xA

    machine = machine_for("CSRW priv, r1\n")
    machine.regs[1] = 2
    machine.run(1, stop_on_exception=False)
    assert machine.priv == 2  # not observable as a fault from machine level


def test_warl_discards_reserved_bits_of_psr():
    machine = machine_for("CSRW psr, r1\nCSRR r2, psr\n")
    machine.regs[1] = 0xFFFFFFFFFFFFFFFF
    machine.run(2, stop_on_exception=False)
    assert machine.psr == 0xF
    assert machine.regs[2] == 0xF


def test_reserved_csr_indices_read_zero_and_discard_writes():
    source = "CSRW #19, r1\nCSRR r2, #19\nCSRR r3, #31\n"
    machine = machine_for(source)
    machine.regs[1] = 0x1234
    machine.run(3, stop_on_exception=False)
    assert machine.regs[2] == 0
    assert machine.regs[3] == 0
    assert machine.exception_code == CODE["NONE"]
    # The discarded write must not have created any hidden state.
    assert machine.read_csr(19) == 0


def test_user_mode_cannot_access_machine_only_csrs():
    for source, cause in (
        ("CSRR r1, epc\n", 1),
        ("CSRR r1, vbar\n", 5),
        ("CSRW epc, r1\n", 1),
        ("CSRW vbar, r1\n", 5),
        ("CSRW ecode, r1\n", 3),
    ):
        machine = machine_for(source)
        machine.run(1, stop_on_exception=False)
        assert machine.exception_code == CODE["PRIVILEGE"], source
        assert machine.ecause == cause, source
        assert machine.epc == 0x1000
        assert machine.priv == 2


def test_read_only_csr_write_raises_privilege_fault():
    machine = machine_for("CSRW coreid, r1\n")
    machine.regs[1] = 7
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["PRIVILEGE"]
    assert machine.ecause == CSR_BY_NAME["coreid"].index
    assert machine.coreid == 0


def test_user_mode_may_read_psr_priv_and_coreid_but_not_write_priv():
    machine = machine_for("CSRR r1, psr\nCSRR r2, coreid\nCSRR r3, priv\n")
    machine.run(3, stop_on_exception=False)
    assert machine.exception_code == CODE["NONE"]
    assert (machine.regs[1], machine.regs[2], machine.regs[3]) == (0, 0, 0)

    machine = machine_for("CSRW priv, r1\n")
    machine.regs[1] = 1
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["PRIVILEGE"]
    assert machine.priv == 2


def test_misaligned_epc_and_vbar_writes_fault():
    for source, index in (("CSRW epc, r1\n", 1), ("CSRW vbar, r1\n", 5)):
        machine = at_machine(machine_for(source))
        machine.regs[1] = 0x1002
        machine.run(1, stop_on_exception=False)
        assert machine.exception_code == CODE["ALIGNMENT_FAULT"], source
        assert machine.ecause == 0x1002
        assert machine.read_csr(index) != 0x1002


def test_writing_the_reserved_privilege_level_faults():
    machine = at_machine(machine_for("CSRW priv, r1\n"))
    machine.regs[1] = 3
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["PRIVILEGE"]
    assert machine.ecause == 3
    assert machine.priv == 2


def test_aligned_epc_and_vbar_writes_are_accepted_at_machine_level():
    machine = at_machine(machine_for("CSRW epc, r1\nCSRW vbar, r1\n"))
    machine.regs[1] = 0x2000
    machine.run(2, stop_on_exception=False)
    assert machine.exception_code == CODE["NONE"]
    assert machine.epc == 0x2000
    assert machine.vbar == 0x2000


def test_host_csr_access_bypasses_checks_but_not_width():
    machine = NovaMachine()
    machine.write_csr("psr", 0xFFFFFFFFFFFFFFFF)
    assert machine.read_csr("psr") == 0xFFFFFFFFFFFFFFFF
    with pytest.raises(ValueError):
        machine.read_csr("nosuchcsr")
    with pytest.raises(ValueError):
        machine.read_csr(32)
    with pytest.raises(ValueError):
        machine.write_csr(-1, 0)


def test_csr_instructions_round_trip_through_text():
    source = "CSRR r1, psr\nCSRW psr, r2\nCSRR r3, #19\nCSRW #19, r4\n"
    code = assemble_text(source)
    assert disassemble_bytes(code) == [
        "CSRR r1, psr", "CSRW psr, r2", "CSRR r3, #19", "CSRW #19, r4",
    ]
    assert assemble_text("\n".join(disassemble_bytes(code)) + "\n") == code


# --------------------------------------------------------------------------
# PSR flags through the CSR file
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right,expected,why",
    [
        (5, 5, Z | C, "equal: zero set, no borrow"),
        (1, 0, C, "no borrow, nonzero positive result"),
        (0, 1, N, "borrow, result -1 is negative"),
        (0xFFFFFFFFFFFFFFFF, 1, N | C, "result -2 is negative, no borrow"),
        (0x8000000000000000, 1, V | C, "signed overflow, no borrow"),
        (0x8000000000000000, 0x7FFFFFFFFFFFFFFF, V | C, "signed overflow, result 1"),
    ],
)
def test_compare_flags_are_observable_through_psr(left, right, expected, why):
    """``CMP`` reports ``C`` as "no borrow", i.e. ``rs1 >= rs2`` unsigned."""
    machine = machine_for("CMP r1, r2\nCSRR r3, psr\n")
    machine.regs[1] = left
    machine.regs[2] = right
    machine.run(2, stop_on_exception=False)
    assert machine.psr == expected, why
    assert machine.regs[3] == expected, why


def test_cmpi_sets_flags_through_psr():
    machine = machine_for("CMPI r1, 12\nCSRR r2, psr\n")
    machine.regs[1] = 12
    machine.run(2, stop_on_exception=False)
    assert machine.psr == Z | C


def test_psr_flag_bits_are_not_branches():
    """Branches read registers, never PSR flags."""
    machine = machine_for("CMP r1, r1\nBNE r1, r1, skip\nMOVI r2, 1\nskip: NOP\n")
    machine.regs[1] = 0
    machine.run(3, stop_on_exception=False)
    assert machine.psr == Z | C
    assert machine.regs[2] == 1


# --------------------------------------------------------------------------
# Exception entry, the save stack, and MRET
# --------------------------------------------------------------------------


def test_exception_entry_sets_the_architectural_state():
    machine = machine_for("SCALL 1234\n")
    machine.vbar = 0x2000
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["SYSTEM_CALL"]
    assert machine.ecause == 1234
    assert machine.epc == 0x1004
    assert machine.priv == 2
    assert machine.pc == 0x2000
    assert machine.exception_stack() == [(0x1004, CODE["SYSTEM_CALL"], 1234, 0)]


def test_faults_record_the_faulting_address_or_value_in_ecause():
    machine = machine_for("LDD r2, [r1 + 4]\n")
    machine.regs[1] = 2  # not 8-byte aligned
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["ALIGNMENT_FAULT"]
    assert machine.ecause == 6
    assert machine.epc == 0x1000

    machine = machine_for("LDD r2, [r1 + 4]\n")
    machine.regs[1] = WORD_MASK - 3  # access crosses the top of the address space
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["MEMORY_FAULT"]


def test_mret_restores_pc_privilege_and_clears_ecode_but_keeps_ecause():
    machine = install_handler(machine_for("SCALL 77\nMOVI r1, 5\n"), "MRET\n", 0x2000)
    machine.run(3, stop_on_exception=False)
    assert machine.exception_code == CODE["NONE"]
    assert machine.priv == 0
    assert machine.pc == 0x1008
    assert machine.regs[1] == 5
    assert machine.ecause == 77, "ecause survives MRET for post-mortem inspection"
    assert machine.exception_stack_depth == 0


def test_nested_exceptions_unwind_one_frame_at_a_time():
    """Each ``MRET`` pops exactly one frame, returning to where it was taken.

    The outer handler repoints ``vbar`` before faulting again, which is what a
    real handler must do: an exception vector that still points at the faulting
    instruction would simply re-enter it.
    """
    machine = NovaMachine(assemble_text("SCALL 1\n"), entry=0x1000)
    machine.regs[1] = 0x3000  # the nested vector
    machine.load_code(
        assemble_text("CSRW vbar, r1\nSCALL 2\nMRET\nMRET\n"), 0x2000
    )
    machine.load_code(assemble_text("MRET\n"), 0x3000)
    machine.vbar = 0x2000
    machine.pc = 0x1000

    machine.run(1, stop_on_exception=False)  # the user's SCALL
    assert machine.epc == 0x1004
    assert machine.priv == 2
    assert machine.exception_stack() == [(0x1004, CODE["SYSTEM_CALL"], 1, 0)]

    machine.run(2, stop_on_exception=False)  # CSRW vbar, then the handler's SCALL
    assert machine.vbar == 0x3000
    assert machine.epc == 0x2008, "SCALL reports the following instruction"
    assert machine.ecause == 2
    assert machine.pc == 0x3000
    assert machine.exception_stack_depth == 2

    machine.run(1, stop_on_exception=False)  # nested MRET: back into the handler
    assert machine.pc == 0x2008
    assert machine.priv == 2
    assert machine.exception_code == CODE["NONE"]
    assert machine.exception_stack_depth == 1

    machine.run(1, stop_on_exception=False)  # MRET: back into user code
    assert machine.pc == 0x1004
    assert machine.priv == 0
    assert machine.exception_code == CODE["NONE"]
    assert machine.exception_stack_depth == 0
    assert machine.ecause == 2, "ecause is not restored by MRET"


def test_save_stack_has_a_fixed_depth_and_discards_the_oldest_frame():
    machine = NovaMachine(assemble_text("SCALL 1\n"), entry=0x1000)
    machine.vbar = 0x2000
    for index in range(EXCEPTION_STACK_DEPTH + 3):
        machine.inject_exception(CODE["SYSTEM_CALL"], index, return_pc=0x1000 + index)
        assert machine.exception_stack_depth == min(
            index + 1, EXCEPTION_STACK_DEPTH
        )
    frames = machine.exception_stack()
    assert [frame[2] for frame in frames] == [3, 4, 5, 6]


def test_save_stack_copies_are_defensive():
    machine = NovaMachine()
    frames = machine.exception_stack()
    frames.append((0, 0, 0, 0))
    assert machine.exception_stack() == []


def test_mret_without_a_frame_is_illegal():
    machine = at_machine(machine_for("MRET\n"))
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["ILLEGAL_INSTRUCTION"]
    assert machine.priv == 2


def test_mret_with_a_misaligned_epc_faults():
    machine = at_machine(machine_for("MRET\n"))
    machine.epc = 2
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == CODE["ALIGNMENT_FAULT"]
    assert machine.ecause == 2


def test_machine_privilege_instructions_are_refused_in_user_mode():
    for source in ("MRET\n", "HALT\n", "WAIT\n"):
        machine = machine_for(source)
        machine.run(1, stop_on_exception=False)
        assert machine.exception_code == CODE["PRIVILEGE"], source
        assert machine.halted is False
        assert machine.priv == 2


def test_machine_privilege_instructions_execute_at_machine_level():
    machine = machine_for("HALT\n")
    machine.priv = 2
    machine.run(1, stop_on_exception=False)
    assert machine.halted is True
    assert machine.exception_code == CODE["NONE"]


def test_privilege_change_through_the_csr_is_immediate():
    machine = at_machine(machine_for("CSRW priv, r1\nMRET\n"))
    machine.regs[1] = 1  # supervisor
    machine.run(1, stop_on_exception=False)
    assert machine.priv == 1
    assert machine.privilege_name == "supervisor"
    machine.run(1, stop_on_exception=False)  # MRET is machine-only
    assert machine.exception_code == CODE["PRIVILEGE"]


def test_vbar_selects_the_vector_and_is_honoured_on_entry():
    machine = install_handler(machine_for("SCALL 3\n"), "NOP\n", 0x8000)
    machine.run(1, stop_on_exception=False)
    assert machine.pc == 0x8000


def test_exception_entry_is_reentrant_from_any_depth():
    machine = NovaMachine()
    machine.vbar = 0x4000
    machine.epc = 0x1234
    machine.priv = 1
    machine.inject_exception(CODE["INTERRUPT"], 5, return_pc=0x2000)
    assert machine.epc == 0x2000
    assert machine.ecause == 5
    assert machine.priv == 2
    assert machine.pc == 0x4000
    assert machine.exception_stack() == [(0x2000, CODE["INTERRUPT"], 5, 1)]


def test_run_stops_at_the_first_exception_by_default():
    machine = install_handler(machine_for("SCALL 1\nMOVI r1, 1\nMOVI r2, 2\n"),
                              "MRET\n", 0x2000)
    machine.run(3)
    assert machine.pc == 0x2000, "run() returned as soon as the exception was taken"
    assert machine.regs[1] == 0 and machine.regs[2] == 0
    machine.run(3, stop_on_exception=False)
    assert machine.pc == 0x100C
    assert machine.regs[1] == 1
    assert machine.regs[2] == 2
