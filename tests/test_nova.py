from nova.assembler import assemble_text
from nova.disassembler import disassemble_bytes
from nova.emulator import NovaMachine
from nova.isa import (EXCEPTION_CODES, INSTRUCTIONS_BY_OPCODE, INSTRUCTION_TABLE,
                      WORD_MASK, make_object_reference)
from nova.fileformats import (
    FILE_FORMATS,
    get_file_format,
    openwindows_directories,
)


def test_assembler_roundtrip():
    source = """
    MOVI r1, 10
    MOVI r2, 5
    ADD r3, r1, r2
    LDD r4, [r1 + 8]
    STW r4, [r1 + 8]
    """
    code = assemble_text(source)
    asm = disassemble_bytes(code)
    assert asm[0].startswith("MOVI")
    assert asm[2].startswith("ADD")
    assert len(code) == 20


def test_emulator_arithmetic():
    source = """
    MOVI r1, 10
    MOVI r2, 5
    ADD r3, r1, r2
    SUB r4, r1, r2
    MUL r5, r3, r2
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(5)
    assert machine.regs[3] == 15
    assert machine.regs[4] == 5
    assert machine.regs[5] == 75


def test_memory_load_store():
    source = """
    MOVI r1, 0x2000
    MOVI r2, 0x55
    STB r2, [r1 + 0]
    LDB r3, [r1 + 0]
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(4)
    assert machine.regs[3] == 85


def test_signed_and_unsigned_load_extension():
    source = """
    MOVI r1, 512
    MOVI r2, 255
    STB r2, [r1]
    LDB r3, [r1]
    LDBU r4, [r1]
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(5)
    assert machine.regs[3] == WORD_MASK
    assert machine.regs[4] == 255


def test_object_reference_operations():
    source = """
    MOVI r1, 64
    OBJ.CREATE r2, r1
    OBJ.GET r3, r2
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(3)
    assert machine.regs[2] == make_object_reference(1, 1)
    assert machine.regs[3] == 64


def test_system_call_exception():
    source = "SCALL 7"
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["SYSTEM_CALL"]
    assert machine.pc == machine.vbar


def test_instruction_roundtrip_preserves_encoded_stream():
    source = """
    MOVI r1, 16383
    ADDI r2, r1, -8192
    CMP r1, r2
    CMPI r1, -1
    LDW r3, [sp - 12]
    STB r3, [r4 + 7]
    BEQ r1, r2, -8
    CALL 12
    SCALL 1234
    OBJ.DROP r8
    """
    code = assemble_text(source)
    reparsed = assemble_text("\n".join(disassemble_bytes(code)))
    assert reparsed == code


def test_assembler_checks_immediate_bounds_and_labels():
    import pytest

    with pytest.raises(ValueError, match="unsigned 14-bit"):
        assemble_text("MOVI r1, 16384")
    with pytest.raises(ValueError, match="signed 14-bit"):
        assemble_text("ADDI r1, r0, 8192")
    with pytest.raises(ValueError, match="unknown CALL target"):
        assemble_text("CALL missing")
    with pytest.raises(ValueError, match="duplicate label"):
        assemble_text("again: NOP\nagain: NOP")


def test_branch_label_displacement_uses_each_instruction_pc():
    source = """
    MOVI r1, 1
    MOVI r2, 1
    BEQ r1, r2, target
    MOVI r3, 99
    target: MOVI r3, 7
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(4)
    assert machine.regs[3] == 7
    assert machine.pc == 0x1014


def test_signed_compare_flags_and_signed_branch():
    source = """
    MOVI r1, 5
    MOVI r2, 7
    CMP r1, r2
    BLT r1, r2, less
    MOVI r3, 99
    less: CMPI r1, 5
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(5)
    assert machine.psr == 0b0101
    assert machine.regs[3] == 0


def test_memory_alignment_fault_is_architectural():
    source = """
    MOVI r1, 1
    LDD r2, [r1]
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(2)
    assert machine.exception_code == EXCEPTION_CODES["ALIGNMENT_FAULT"]
    assert machine.epc == 0x1004


def test_reserved_encoding_and_privileged_instruction_trap():
    invalid_add = (0x10 << 24) | 1
    machine = NovaMachine(invalid_add.to_bytes(4, "little"), entry=0x1000)
    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["ILLEGAL_INSTRUCTION"]

    machine = NovaMachine(assemble_text("HALT"), entry=0x1000)
    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["PRIVILEGE"]


def test_exception_return_restores_prior_privilege_and_continues():
    source = "SCALL 1234\nMOVI r1, 5"
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.vbar = 0x2000
    machine.load_code(assemble_text("MRET"), 0x2000)
    machine.pc = 0x1000

    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["SYSTEM_CALL"]
    assert machine.ecause == 1234
    assert machine.epc == 0x1004
    assert machine.priv == 2

    machine.run(2, stop_on_exception=False)
    assert machine.exception_code == EXCEPTION_CODES["NONE"]
    assert machine.priv == 0
    assert machine.regs[1] == 5


def test_object_drop_uses_single_handle_operand():
    source = """
    MOVI r1, 64
    OBJ.CREATE r2, r1
    OBJ.DROP r2
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(3)
    assert machine.objects == {}


def test_signed_division_truncates_toward_zero():
    source = """
    ADDI r1, r0, -7
    MOVI r2, 3
    DIV r3, r1, r2
    REM r4, r1, r2
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(4)
    assert machine.regs[3] == WORD_MASK - 1
    assert machine.regs[4] == WORD_MASK


def test_memory_address_overflow_raises_memory_fault():
    machine = NovaMachine(assemble_text("LDD r2, [r1 + 4]"), entry=0x1000)
    machine.regs[1] = WORD_MASK - 7
    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["MEMORY_FAULT"]


def test_instruction_opcodes_are_unique():
    assert len(INSTRUCTIONS_BY_OPCODE) == len(INSTRUCTION_TABLE)


def test_nop_advances_pc_without_trapping():
    machine = NovaMachine(assemble_text("NOP\nMOVI r1, 9"), entry=0x1000)
    machine.run(2)
    assert machine.pc == 0x1008
    assert machine.regs[1] == 9
    assert machine.exception_code == EXCEPTION_CODES["NONE"]


def test_call_and_return_preserve_link_register():
    source = """
    CALL function
    MOVI r1, 2
    NOP
    function: MOVI r1, 1
    RET
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(4)
    assert machine.regs[1] == 2
    assert machine.regs[31] == 0x1004


def test_indirect_jump_uses_target_register():
    source = """
    MOVI r1, 4108
    JMP r1
    MOVI r2, 99
    MOVI r2, 7
    """
    machine = NovaMachine(assemble_text(source), entry=0x1000)
    machine.run(3)
    assert machine.regs[2] == 7


def test_nova_file_format_registry():
    assert get_file_format("example.NO").name == "Nova Assembly"
    assert get_file_format(".nok").kind == "executable"
    assert get_file_format("module.nod").kind == "shared-library"
    assert get_file_format("image.nob").implemented
    assert len(FILE_FORMATS) == len(set(FILE_FORMATS))


def test_openwindows_paths_are_home_relative_and_overridable(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVA_OPENWINDOWS_DIR", raising=False)
    monkeypatch.delenv("NOVA_OPENWINDOWS_ESSENTIALS_DIR", raising=False)
    main, essentials = openwindows_directories(tmp_path)
    assert main == tmp_path / "Documents" / "OpenWindows"
    assert essentials == tmp_path / "Documents" / "OpenWindows-Essentials"

    monkeypatch.setenv("NOVA_OPENWINDOWS_DIR", str(tmp_path / "NovaModules"))
    main, essentials = openwindows_directories(tmp_path)
    assert main == tmp_path / "NovaModules"
    assert essentials == tmp_path / "Documents" / "OpenWindows-Essentials"
