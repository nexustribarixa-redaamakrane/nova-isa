"""Tests for the documented Nova C ABI v1.0 conventions."""

from nova import NovaMachine, cabi
from nova.assembler import assemble_text
from nova.isa import PRIVILEGE_LEVELS, REGISTERS


def test_c_abi_version_and_register_aliases():
    assert cabi.C_ABI_VERSION == "Nova C ABI v1.0"
    for name, index in (
        ("a0", 1), ("a5", 6), ("t0", 7), ("t3", 10),
        ("s0", 11), ("s3", 14), ("gp", 28), ("sp", 29),
        ("fp", 30), ("ra", 31),
    ):
        assert REGISTERS[name] == index
    assert cabi.ARGUMENT_REGISTERS == tuple(f"a{i}" for i in range(6))
    assert cabi.GLOBAL_POINTER_REGISTER == "gp"
    assert REGISTERS[cabi.GLOBAL_POINTER_REGISTER] == 28


def test_callee_and_caller_saved_sets_are_disjoint_and_complete():
    caller = set(cabi.CALLER_SAVED_REGISTERS)
    callee = set(cabi.CALLEE_SAVED_REGISTERS)
    assert not caller & callee
    assert set(cabi.ARGUMENT_REGISTERS) <= caller
    assert {"gp", "sp", "fp"} <= callee
    assert cabi.MAX_REGISTER_RETURN_BYTES == 16


def test_va_list_layout_constants():
    assert cabi.VA_LIST_SIZE == 24
    assert cabi.VA_LIST_ALIGNMENT == 8
    assert cabi.VA_GP_OFFSET == 0
    assert cabi.VA_FP_OFFSET == 4
    assert cabi.VA_OVERFLOW_ARG_AREA == 8
    assert cabi.VA_REG_SAVE_AREA == 16
    assert cabi.VA_REGISTER_SAVE_BYTES == 48
    assert cabi.VA_REG_SAVE_AREA + cabi.VA_REGISTER_SAVE_BYTES == 64


def test_call_preserves_stack_pointer_and_sets_return_address():
    machine = NovaMachine(
        assemble_text("CALL target\nHALT\ntarget:\n  RET\n"), entry=0x1000
    )
    machine.priv = PRIVILEGE_LEVELS["machine"]
    machine.write_register("sp", 0x8000)
    machine.run(3, stop_on_exception=False)
    assert machine.read_register("sp") == 0x8000
    assert machine.read_register("ra") == 0x1004
    assert machine.halted


def test_stack_frame_keeps_required_alignment():
    machine = NovaMachine(
        assemble_text("ADDI sp, sp, -16\nADDI sp, sp, -32\nHALT\n"),
        entry=0x1000,
    )
    machine.priv = PRIVILEGE_LEVELS["machine"]
    machine.write_register("sp", 0x8000)
    machine.run(3, stop_on_exception=False)
    assert machine.read_register("sp") % cabi.STACK_ALIGNMENT == 0
    assert machine.read_register("sp") == 0x8000 - 48


def test_gp_alias_assembles_as_register_28():
    image = assemble_text("MOVI gp, 1\n")
    machine = NovaMachine(image, entry=0)
    machine.step()
    assert machine.regs[28] == 1
