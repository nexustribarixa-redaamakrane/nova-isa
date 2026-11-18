"""Pseudo-instruction tests: ``LI`` and ``LA`` expansion, layout, and errors.

The pseudo layer must produce a deterministic, finite, valid sequence of real
Nova instructions for any 64-bit constant, must resolve labels absolutely, and
must never leak pseudo mnemonics into the ISA, decoder, or images.
"""

import pytest

from nova.assembler import AssemblyError, assemble_program, assemble_text
from nova.disassembler import disassemble_bytes
from nova.emulator import NovaMachine
from nova.isa import INSTRUCTION_TABLE
from nova.pseudo import expand_la, expand_li, expand_pseudo, is_pseudo

ALL_ONES = 2**64 - 1
SIGNED_MIN = -(2**63)


def run_program(source, *, entry=0x1000, steps=200):
    machine = NovaMachine(assemble_text(source, entry=entry), entry=entry)
    machine.priv = 2
    machine.run(steps, stop_on_exception=False)
    return machine


def test_pseudo_mnemonics_are_not_isa_instructions():
    assert "LI" not in INSTRUCTION_TABLE
    assert "LA" not in INSTRUCTION_TABLE
    assert is_pseudo("li") and is_pseudo("LA") and not is_pseudo("MOVI")


def test_li_small_value_is_a_single_movi():
    assert expand_li("r1", "42", 1) == ["MOVI r1, 42"]
    assert expand_li("sp", "16383", 1) == ["MOVI sp, 16383"]


def test_li_16384_boundary_uses_three_instructions():
    assert expand_li("r1", "16384", 1) == [
        "MOVI r1, 2",
        "SHLI r1, r1, 13",
        "ORI r1, r1, 0",
    ]


def test_li_expansion_is_deterministic_and_canonical():
    a = expand_li("r7", "0x123456789abcdef0", 1)
    b = expand_li("r7", "0x123456789abcdef0", 1)
    assert a == b
    source = "LI r7, 0x123456789abcdef0\nHALT\n"
    assert assemble_text(source) == assemble_text(source)


def test_li_negative_one_expands_to_nine_instructions():
    sequence = expand_li("r6", "-1", 1)
    assert len(sequence) == 9
    assert sequence[0] == "MOVI r6, 4095"
    assert sequence[1:3] == ["SHLI r6, r6, 13", "ORI r6, r6, 8191"]


@pytest.mark.parametrize("operand,expected", [
    ("-1", ALL_ONES),
    ("-8192", 2**64 - 8192),
    ("-1000000", 2**64 - 1000000),
    ("16384", 16384),
    ("16383", 16383),
    ("0", 0),
    ("0x8000000000000000", 2**63),
    ("0xFFFFFFFFFFFFFFFF", ALL_ONES),
    ("0x123456789abcdef0", 0x123456789ABCDEF0),
    ("0b11111111111111111111111111111111", 2**32 - 1),
])
def test_li_loads_arbitrary_64_bit_values(operand, expected):
    machine = run_program(f"LI r3, {operand}\n")
    assert machine.read_register(3) == expected


def test_li_register_aliases_and_case():
    machine = run_program("li SP, 5\n")
    assert machine.read_register("sp") == 5


@pytest.mark.parametrize("source", [
    "LI r1, 0x10000000000000000",
    f"LI r1, {SIGNED_MIN - 1}",
])
def test_li_rejects_values_outside_64_bits(source):
    with pytest.raises(AssemblyError, match="64-bit range"):
        assemble_text(source)


@pytest.mark.parametrize("source", [
    "LI r1",                          # missing immediate
    "LI r1, 5, 6",                    # too many operands
    "LI 5, 6",                        # rd is not a register
    "LI r1, hello",                   # immediate is not a literal
])
def test_li_rejects_malformed_operands(source):
    with pytest.raises(AssemblyError):
        assemble_text(source)


def test_la_forward_reference_resolves_to_link_address():
    machine = run_program("LA r5, target\nMOVI r1, 0\ntarget: HALT\n")
    assert machine.read_register(5) == 0x1000 + 8


def test_la_backward_reference_resolves_to_link_address():
    machine = run_program("start: NOP\nLA r5, start\n")
    assert machine.read_register(5) == 0x1000


def test_la_accepts_an_absolute_integer_address():
    machine = run_program("LA r8, 0x1234\n")
    assert machine.read_register(8) == 0x1234


def test_la_error_message_carries_the_line_number():
    with pytest.raises(AssemblyError, match=r"line 2") as excinfo:
        assemble_text("MOVI r1, 0\nLA r5, missing\n")
    assert "missing" in str(excinfo.value)


def test_la_rejects_label_expressions_that_are_not_names():
    with pytest.raises(AssemblyError, match="not a label"):
        assemble_text("LA r1, target - 4\n")


def test_la_at_high_link_address_expands_and_converges():
    entry = 0x100000
    machine = run_program("LA r5, target\nMOVI r1, 0\ntarget: HALT\n", entry=entry)
    assert machine.read_register(5) == machine.pc  # the HALT address


def test_la_at_top_of_address_space_uses_full_expansion_and_converges():
    entry = 0xFFFFFFFFFFFE0000
    machine = run_program("LA r5, target\nMOVI r1, 0\ntarget: HALT\n", entry=entry)
    assert machine.read_register(5) == machine.pc  # the HALT address
    assert machine.read_register(5) > 0xFFFFFFFFFFFE0000
    words = assemble_text("LA r5, target\nMOVI r1, 0\ntarget: HALT\n", entry=entry)
    assert len(words) == 11 * 4  # 9-instruction LA + MOVI + HALT


def test_label_on_a_pseudo_line_points_at_the_expansion():
    source = "here: LI r1, 0x12345678\nLA r2, here\n"
    sequence = expand_li("r1", "0x12345678", 1)
    machine = run_program(source)
    assert machine.read_register(2) == 0x1000  # 'here' is the first instruction


def test_mixed_program_roundtrip_through_the_disassembler():
    source = """
    start: LA r9, handler
    LI r1, -1
    MOVI r2, 5
    handler: NOP
    HALT
    """
    image = assemble_text(source)
    text = disassemble_bytes(image)
    from nova.pseudo import PSEUDO_INSTRUCTIONS
    mnemonics = {line.split(maxsplit=1)[0] for line in text}
    assert not (mnemonics & set(PSEUDO_INSTRUCTIONS))
    text_source = "\n".join(text)
    assert assemble_text(text_source) == image


def test_expansion_of_li_uses_only_real_isa_instructions():
    for line in expand_li("r1", "-1", 1):
        mnemonic = line.split(None, 1)[0]
        assert mnemonic in INSTRUCTION_TABLE


def test_assemble_program_supports_pseudo_instructions():
    image = assemble_program(["LI r1, 1", "HALT"])
    assert len(image) == 8


def test_pseudo_errors_report_the_original_statement_line():
    with pytest.raises(AssemblyError, match=r"line 3"):
        assemble_text("NOP\nNOP\nLA r5, nope\nLA r6, 0x1234\n")