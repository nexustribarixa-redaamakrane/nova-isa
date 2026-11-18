"""Table-driven conformance tests for the Nova instruction set.

Every structural assertion here is derived from :mod:`nova.isa` rather than from
a second hand-written copy of the instruction definitions, so the tests cannot
drift away from the architecture. The encodings in
:data:`HAND_COMPUTED_ENCODINGS` are the deliberate exception: each one was
worked out by hand from the published field layout as
``opcode << 24 | rd << 19 | ... | low fields``, so they cross-check the table
itself instead of merely restating it.
"""

import pytest

from nova.assembler import AssemblyError, assemble_text, parse_source
from nova.disassembler import (
    DisassemblyError,
    disassemble_bytes,
    disassemble_stream,
    disassemble_word,
)
from nova.emulator import HANDLERS, NovaMachine
from nova.isa import (
    ALIASES,
    EXCEPTION_CODES,
    FORMATS,
    GPR_COUNT,
    IMM14_MAX,
    IMM14_MIN,
    INSTRUCTIONS_BY_OPCODE,
    INSTRUCTION_ALIGNMENT,
    INSTRUCTION_BYTES,
    INSTRUCTION_MASK,
    INSTRUCTION_TABLE,
    ISA_VERSION,
    NOP_OPCODE,
    OPCODE_RANGES,
    SHIFT_COUNT_MAX,
    UIMM14_MAX,
    WORD_MASK,
    IllegalEncoding,
    build_word,
    decode_instruction,
    decode_strict,
    extract_fields,
    format_operand,
    format_word,
    validate_table,
)

ALL_INSTRUCTIONS = sorted(INSTRUCTION_TABLE)
MNEMONICS = ALL_INSTRUCTIONS
REGISTER_INSTRUCTIONS = [
    name for name in MNEMONICS
    if any(operand.is_register for operand in INSTRUCTION_TABLE[name].operands)
]
IMMEDIATE_INSTRUCTIONS = [
    name for name in MNEMONICS
    if any(operand.is_immediate for operand in INSTRUCTION_TABLE[name].operands)
]
MEMORY_INSTRUCTIONS = [
    name for name in MNEMONICS
    if INSTRUCTION_TABLE[name].memory_operand() is not None
]


def ids(names, argument="name"):
    return pytest.mark.parametrize(argument, names, ids=names)


def canonical(definition, tokens):
    """Build an assembly line from explicit operand tokens."""
    if not tokens:
        return definition.mnemonic
    return definition.mnemonic + " " + ", ".join(tokens)


def example_tokens(definition):
    """Canonical operand tokens of ``definition.example()``."""
    fields = definition.example_fields()
    return [
        format_operand(operand, definition, fields)
        for operand in definition.operands
    ]


def example_line(definition):
    return canonical(definition, example_tokens(definition))


def example_word(definition):
    return int.from_bytes(assemble_text(example_line(definition) + "\n"), "little")


# --------------------------------------------------------------------------
# Structural invariants of the architecture tables
# --------------------------------------------------------------------------


def test_isa_tables_are_self_consistent():
    validate_table()


def test_opcodes_are_unique_and_table_is_bijective():
    assert len(INSTRUCTIONS_BY_OPCODE) == len(INSTRUCTION_TABLE)
    assert set(INSTRUCTIONS_BY_OPCODE.values()) == set(INSTRUCTION_TABLE.values())
    assert ALIASES == {name: name for name in INSTRUCTION_TABLE}


@ids(MNEMONICS)
def test_every_instruction_has_an_emulator_handler(name):
    assert name in HANDLERS


@ids(MNEMONICS)
def test_mnemonic_is_canonical_upper_case(name):
    assert name == name.upper()
    assert name.replace(".", "").isalnum()
    assert INSTRUCTION_TABLE[name].mnemonic == name


@ids(sorted(FORMATS), "format_name")
def test_format_layouts_cover_all_32_bits(format_name):
    fields = FORMATS[format_name]
    covered = 0
    for field in fields:
        assert field.high >= field.low
        assert field.high < 32
        covered |= field.mask << field.low
    assert covered == INSTRUCTION_MASK
    assert [field.name for field in fields][0] == "opcode"
    assert fields[0].high == 31 and fields[0].low == 24


@ids(MNEMONICS)
def test_definition_references_only_real_fields(name):
    definition = INSTRUCTION_TABLE[name]
    names = set(definition.field_names)
    assert len(names) == len(definition.field_names)
    for operand in definition.operands:
        for field in operand.fields:
            assert field in names, (name, field)
    for field, _value in definition.fixed:
        assert field in names, (name, field)


@ids(MNEMONICS)
def test_operand_order_matches_field_order(name):
    definition = INSTRUCTION_TABLE[name]
    order = {field.name: index for index, field in enumerate(definition.fields)}
    for operand in definition.operands:
        positions = [order[field] for field in operand.fields]
        assert positions == sorted(positions), (name, operand.name)


@ids(MNEMONICS)
def test_privilege_is_defined(name):
    assert INSTRUCTION_TABLE[name].privilege in ("user", "supervisor", "machine")


@ids(MNEMONICS)
def test_opcode_is_one_byte_and_sorted_by_mnemonic_list(name):
    definition = INSTRUCTION_TABLE[name]
    assert 0 <= definition.opcode <= 0xFF
    assert INSTRUCTIONS_BY_OPCODE[definition.opcode] is definition


@ids(MNEMONICS)
def test_documented_fields_exist_in_the_instruction_row(name):
    """Every ``exceptions`` entry must be a real exception code name."""
    definition = INSTRUCTION_TABLE[name]
    for token in definition.exceptions.split(","):
        token = token.strip()
        if token and token != "-":
            assert token in EXCEPTION_CODES, (name, token)


# --------------------------------------------------------------------------
# Encodings cross-checked by hand against the published field layout
# --------------------------------------------------------------------------

# Worked out by hand from the format layouts, for example
#   ADD r1, r2, r3 -> R: 0x10<<24 | 1<<19 | 2<<14 | 3<<9 = 0x10088600
#   CSRW psr, r1   -> X: 0x81<<24 | 0<<19 | 0<<14 | 1<<9 = 0x81000200
HAND_COMPUTED_ENCODINGS = {
    "NOP": 0x00000000,          # S: opcode 0, every other field zero
    "ADD": 0x10088600,          # R: three-register operation
    "CMP": 0x1B004400,          # R: rd pinned to 0, rs1=1, rs2=2
    "MOV": 0x1C088000,          # R: rd=1, rs1=2, rs2 pinned to 0
    "ADDI": 0x2008800C,         # I: rd=1, rs1=2, imm14=12
    "CMPI": 0x2900400C,         # I: rd pinned to 0, rs1=1, imm14=12
    "MOVI": 0x2A08000C,         # I: rd=1, rs1 pinned to 0, uimm14=12
    "LDD": 0x33088008,          # M: rdata=1, rbase=2, disp14=8
    "STD": 0x43088008,          # M: rdata=1, rbase=2, disp14=8
    "JMP": 0x50080000,          # J: rca=1, rcb and disp14 pinned to 0
    "BEQ": 0x51088008,          # J: rca=1, rcb=2, disp14=8
    "CALL": 0x5B000008,         # J: disp14=8, both register fields pinned
    "RET": 0x5C000000,          # J: every field pinned to zero
    "TRAP": 0x5D00000C,         # J: unsigned uimm14=12 in disp14
    "SCALL": 0x6000000C,        # S: uimm14=12
    "MRET": 0x61000000,         # S: no operands
    "OBJ.CREATE": 0x70088000,   # O: rd=1, rs1=2, rsv14=0
    "OBJ.DROP": 0x73080000,     # O: rd=1, rs1 pinned to 0
    "CSRR": 0x80080000,         # X: rd=1, csr=psr(0), rs1 pinned to 0
    "CSRW": 0x81000200,         # X: rd pinned to 0, csr=psr(0), rs1=1
}


@ids(sorted(HAND_COMPUTED_ENCODINGS))
def test_hand_computed_encoding_matches_the_assembler(name):
    definition = INSTRUCTION_TABLE[name]
    expected = HAND_COMPUTED_ENCODINGS[name]
    assert example_word(definition) == expected, definition.example()
    assert definition.encode(definition.example_fields()) == expected
    assert decode_strict(expected).mnemonic == name
    assert definition.is_valid_encoding(expected)


@ids(MNEMONICS)
def test_definition_encoder_agrees_with_the_generic_field_encoder(name):
    """``InstructionDef.encode`` and ``build_word`` must produce one word."""
    definition = INSTRUCTION_TABLE[name]
    values = {"opcode": definition.opcode}
    values.update(definition.fixed_fields)
    values.update(definition.example_fields())
    assert definition.encode(definition.example_fields()) == build_word(
        definition.format, values
    )


# --------------------------------------------------------------------------
# Every instruction: assembly -> encoding -> disassembly -> assembly
# --------------------------------------------------------------------------


@ids(MNEMONICS)
def test_example_assembles_to_its_example_fields(name):
    definition = INSTRUCTION_TABLE[name]
    assert example_word(definition) == definition.encode(definition.example_fields())


@ids(MNEMONICS)
def test_example_round_trips_through_disassembly(name):
    definition = INSTRUCTION_TABLE[name]
    code = assemble_text(example_line(definition) + "\n")
    assert len(code) == INSTRUCTION_BYTES
    text = disassemble_word(int.from_bytes(code, "little"))
    assert text == definition.example()
    assert assemble_text(text + "\n") == code
    assert disassemble_bytes(code) == [text]
    decoded = disassemble_stream(code)
    assert [instruction.mnemonic for instruction in decoded] == [name]


@ids(MNEMONICS)
def test_example_decodes_to_the_expected_operands(name):
    definition = INSTRUCTION_TABLE[name]
    decoded = decode_strict(definition.encode(definition.example_fields()))
    assert decoded.mnemonic == name
    assert decoded.definition is definition
    assert decoded.privilege == definition.privilege
    assert decoded.opcode == definition.opcode
    for operand in definition.operands:
        if operand.is_register:
            assert decoded.register(operand.name) == definition.example_fields()[
                operand.fields[0]
            ]
    if definition.memory_operand() is not None:
        assert decoded.memory_base == definition.example_fields()["rbase"]
        assert decoded.memory_displacement == 8


# --------------------------------------------------------------------------
# Reserved encodings and reserved bits
# --------------------------------------------------------------------------


@ids(MNEMONICS)
def test_reserved_bits_must_encode_zero(name):
    definition = INSTRUCTION_TABLE[name]
    word = definition.encode(definition.example_fields())
    covered = definition.covered_fields
    for field in definition.fields:
        if field.name == "opcode" or field.name in covered:
            continue
        for bit in range(field.width):
            mutated = word | (1 << (field.low + bit))
            assert decode_instruction(mutated) is None, (
                f"{name}: setting reserved bit {field.low + bit} of "
                f"{field.name} must be illegal"
            )
            assert not definition.is_valid_encoding(mutated)


@ids(MNEMONICS)
def test_pinned_fields_must_hold_their_value(name):
    definition = INSTRUCTION_TABLE[name]
    word = definition.encode(definition.example_fields())
    order = {field.name: field for field in definition.fields}
    for field_name, value in definition.fixed:
        field = order[field_name]
        for offset in range(field.width):
            candidate = value ^ (1 << offset)
            mutated = (word & ~(field.mask << field.low)) | (candidate << field.low)
            assert decode_instruction(mutated) is None, (
                f"{name}: field {field_name} is pinned to {value}"
            )


@ids(MNEMONICS)
def test_every_other_opcode_does_not_decode_to_this_instruction(name):
    definition = INSTRUCTION_TABLE[name]
    fields = extract_fields(definition.format, definition.encode(
        definition.example_fields()))
    for opcode in range(0x100):
        if opcode == definition.opcode:
            continue
        word = build_word(definition.format, {**fields, "opcode": opcode})
        decoded = decode_instruction(word)
        assert decoded is None or decoded.mnemonic != name, (name, opcode)


def test_nop_is_the_all_zero_word_and_only_that():
    definition = INSTRUCTION_TABLE["NOP"]
    assert definition.opcode == NOP_OPCODE
    assert definition.encode({}) == 0x00000000
    assert decode_strict(0).mnemonic == "NOP"
    # Every bit below the opcode byte is reserved for NOP.
    for bit in range(24):
        assert decode_instruction(1 << bit) is None, bit


def test_reserved_opcode_ranges_contain_no_definitions():
    for low, high, purpose in OPCODE_RANGES:
        expect_assigned = "reserved" not in purpose
        for opcode in range(low, high + 1):
            assert (opcode in INSTRUCTIONS_BY_OPCODE) == expect_assigned, (
                opcode, purpose
            )


def test_illegal_words_are_reported_not_guessed():
    with pytest.raises(IllegalEncoding):
        decode_strict(0xDEADBEEF)
    assert decode_instruction(0xDEADBEEF) is None
    assert format_word(0xDEADBEEF) == ".word 0xdeadbeef"
    assert disassemble_word(0xDEADBEEF) == ".word 0xdeadbeef"
    with pytest.raises(DisassemblyError):
        disassemble_stream((0xDEADBEEF).to_bytes(4, "little"))
    machine = NovaMachine((0xDEADBEEF).to_bytes(4, "little"), entry=0x1000)
    machine.run(1)
    assert machine.exception_code == EXCEPTION_CODES["ILLEGAL_INSTRUCTION"]
    assert machine.ecause == 0xDEADBEEF
    assert machine.epc == 0x1000


def test_decode_rejects_out_of_range_and_non_integer_words():
    for bad in (-1, 1 << 32, "0x10", 1.5, True):
        with pytest.raises((ValueError, TypeError)):
            decode_instruction(bad)


def test_disassembly_rejects_bad_input_types():
    with pytest.raises(TypeError):
        disassemble_word("0x10")
    with pytest.raises(TypeError):
        disassemble_bytes("not bytes")


# --------------------------------------------------------------------------
# Assembler input validation
# --------------------------------------------------------------------------


@ids(MNEMONICS)
def test_operand_count_is_enforced(name):
    definition = INSTRUCTION_TABLE[name]
    tokens = example_tokens(definition)
    with pytest.raises(AssemblyError):
        assemble_text(canonical(definition, tokens + ["7"]) + "\n")
    if tokens:
        with pytest.raises(AssemblyError):
            assemble_text(definition.mnemonic + "\n")


@ids(REGISTER_INSTRUCTIONS)
def test_register_operands_are_range_checked_and_aliased(name):
    definition = INSTRUCTION_TABLE[name]
    tokens = example_tokens(definition)
    index = next(
        position for position, operand in enumerate(definition.operands)
        if operand.is_register
    )
    tokens[index] = f"r{GPR_COUNT}"
    with pytest.raises(AssemblyError):
        assemble_text(canonical(definition, tokens) + "\n")
    tokens[index] = "sp"
    assemble_text(canonical(definition, tokens) + "\n")


@ids(IMMEDIATE_INSTRUCTIONS)
def test_immediate_limits_are_enforced(name):
    definition = INSTRUCTION_TABLE[name]
    tokens = example_tokens(definition)
    for position, operand in enumerate(definition.operands):
        if not operand.is_immediate:
            continue
        low, high = definition.limits(operand)
        for bad in (low - 1, high + 1):
            tokens[position] = str(bad)
            with pytest.raises(AssemblyError):
                assemble_text(canonical(definition, tokens) + "\n")
        for good in (low, high):
            tokens[position] = str(good)
            assemble_text(canonical(definition, tokens) + "\n")
        tokens[position] = example_tokens(definition)[position]


@ids(MEMORY_INSTRUCTIONS)
def test_memory_displacements_are_range_checked(name):
    definition = INSTRUCTION_TABLE[name]
    operand = definition.memory_operand()
    for text, ok in (("[r2]", True), ("[r2 + 8191]", True), ("[r2 - 8192]", True),
                     ("[r2 + 8192]", False), ("[r2 - 8193]", False),
                     ("[r2 +]", False), ("[r2", False), ("r2", False),
                     ("[r2 + 1.5]", False), ("[r32]", False)):
        if ok:
            assemble_text(canonical(definition, ["r1", text]) + "\n")
        else:
            with pytest.raises(AssemblyError):
                assemble_text(canonical(definition, ["r1", text]) + "\n")
    assert operand.fields == ("rbase", "disp14")


def test_signed_and_unsigned_immediate_ranges_are_the_documented_ones():
    assert (IMM14_MIN, IMM14_MAX) == (-8192, 8191)
    assert UIMM14_MAX == 16383
    assert SHIFT_COUNT_MAX == 63
    movi = INSTRUCTION_TABLE["MOVI"]
    assert movi.limits(movi.operand("uimm")) == (0, 16383)
    for name in ("SHLI", "SHRI", "SARI"):
        definition = INSTRUCTION_TABLE[name]
        assert definition.limits(definition.operand("simm")) == (0, 63)
    addi = INSTRUCTION_TABLE["ADDI"]
    assert addi.limits(addi.operand("simm")) == (IMM14_MIN, IMM14_MAX)


def test_malformed_source_is_rejected_explicitly():
    bad_sources = [
        "FROB r1, r2",
        "ADD r1, r2",
        "ADD r1, r2, r3, r4",
        "ADD r32, r1, r2",
        "ADD r35, r1, r2",
        "ADD q1, r1, r2",
        "ADD r1,, r2",
        "MOVI r1, -1",
        "MOVI r1, 0x4000",
        "ADDI r1, r2, 8192",
        "ADDI r1, r2, -8193",
        "SHLI r1, r2, 64",
        "SHLI r1, r2, -1",
        "LDD r1, r2",
        "LDD r1, [r2 + 8192]",
        "BEQ r1, r2, nowhere",
        "BEQ r1, r2, 3",
        "CALL nowhere",
        "CSRR r1, nosuchcsr",
        "CSRR r1, #32",
        "CSRR r1, #-1",
        "NOP r1",
        "RET r1",
        "MRET 1",
        ".word 0x10",
        "1: NOP",
        "MOVI r1",
        "ADD",
    ]
    for source in bad_sources:
        with pytest.raises(AssemblyError):
            assemble_text(source + "\n")


def test_empty_source_and_bare_labels_are_legal():
    assert assemble_text("") == b""
    assert assemble_text("\n\n   \n; only a comment\n") == b""
    assert assemble_text("start:\n") == b""
    assert assemble_text("start: ; comment only\n") == b""


def test_integer_literal_bases_and_prefixes():
    for text, expected in (
        ("MOVI r1, 10", 10),
        ("MOVI r1, 0x10", 16),
        ("MOVI r1, 0b1010", 10),
        ("MOVI r1, 0o12", 10),
        ("MOVI r1, #10", 10),
        ("MOVI r1, $10", 10),
        ("MOVI r1, 1_000", 1000),
    ):
        assert int.from_bytes(assemble_text(text + "\n"), "little") & 0x3FFF == expected


def test_mnemonics_and_registers_are_case_insensitive():
    lower = assemble_text("addi r1, r2, 12\n")
    upper = assemble_text("ADDI R1, R2, 12\n")
    assert lower == upper


def test_labels_may_share_a_line_and_may_be_referenced_forward():
    source = (
        "start: MOVI r1, 1 ; set the flag\n"
        "BEQ r1, r1, done\n"
        "MOVI r1, 2\n"
        "done: MOVI r2, 3\n"
    )
    statements, labels = parse_source(source)
    assert [text for _number, text in statements] == [
        "MOVI r1, 1", "BEQ r1, r1, done", "MOVI r1, 2", "MOVI r2, 3",
    ]
    assert labels == {"start": 0, "done": 12}
    code = assemble_text(source, entry=0x1000)
    assert len(code) == 4 * INSTRUCTION_BYTES
    assert extract_fields("J", int.from_bytes(code[4:8], "little"))["disp14"] == 8
    machine = NovaMachine(code, entry=0x1000)
    machine.run(3)
    assert machine.regs[1] == 1
    assert machine.regs[2] == 3


def test_branch_displacements_are_relative_and_independent_of_entry():
    source = "BEQ r1, r1, done\nNOP\nNOP\ndone: NOP\n"
    at_zero = assemble_text(source, entry=0)
    at_page = assemble_text(source, entry=0x8000)
    assert at_zero == at_page
    word = int.from_bytes(at_page[:4], "little")
    assert extract_fields("J", word)["disp14"] == 12
    assert decode_strict(word).target_displacement == 12
    machine = NovaMachine(at_page, entry=0x8000)
    assert machine.pc == 0x8000
    machine.regs[1] = 1
    machine.run(1)
    assert machine.pc == 0x8000 + 12


def test_instruction_stream_length_must_be_a_multiple_of_four():
    with pytest.raises(DisassemblyError):
        disassemble_bytes(b"\x00\x00\x00")
    with pytest.raises(ValueError):
        disassemble_stream(b"\x00\x00\x00")
    with pytest.raises(ValueError):
        NovaMachine(b"\x00\x00\x00", entry=0x1000)
    with pytest.raises(ValueError):
        NovaMachine(b"", entry=0x1003)


# --------------------------------------------------------------------------
# Execution smoke test over the whole table
# --------------------------------------------------------------------------


@ids(MNEMONICS)
def test_every_instruction_executes_or_faults_without_stopping_the_machine(name):
    """No instruction may crash the reference model.

    Architecturally defined faults are allowed here; the reference model must
    report them through the exception path and stay runnable.
    """
    definition = INSTRUCTION_TABLE[name]
    machine = NovaMachine(assemble_text("NOP\n"), entry=0x1000)
    machine.load_code(assemble_text(example_line(definition) + "\nNOP\n"), 0x1000)
    machine.run(2)
    assert machine.exception_code in EXCEPTION_CODES.values()
    assert machine.pc % INSTRUCTION_ALIGNMENT == 0


def test_constants_are_the_documented_ones():
    assert ISA_VERSION.startswith("Nova ISA v1")
    assert INSTRUCTION_BYTES == 4
    assert INSTRUCTION_ALIGNMENT == 4
    assert WORD_MASK == (1 << 64) - 1
    assert GPR_COUNT == 32