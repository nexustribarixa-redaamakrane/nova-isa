"""Nova relocatable object format (``.nobj``) tests.

Covers the container (deterministic encode/decode, validation, truncation and
malformed input) and the assembler's object frontend (``assemble_object``,
``.global`` directives, external ``PCREL_DISP14`` and ``LA64`` relocations).
"""

from pathlib import Path

import pytest

from nova.assembler import AssemblyError, assemble_object, assemble_text
from nova.fileformats import get_file_format
from nova.isa import decode_instruction
from nova.object import (
    NOBJ_HEADER_SIZE,
    NOBJ_MAGIC,
    NOBJ_VERSION,
    RELOCATION_SPAN_BYTES,
    ObjectError,
    ObjectFile,
    Relocation,
    Section,
    Symbol,
    decode_object,
    encode_object,
    load_object,
    write_object,
)


def sample_object():
    """A representative object: three sections, all symbol kinds, a fixup."""
    return ObjectFile(
        sections=[
            Section(".text", "progbits", ("alloc", "exec"), 4,
                    bytes([0x00] * 4 + [0x00] * 4)),
            Section(".data", "progbits", ("alloc", "write"), 4,
                    bytes([10, 20, 30, 40])),
            Section(".bss", "nobits", ("alloc", "write"), 8, b"", 16),
        ],
        symbols=[
            Symbol("main", "global", 0, 0),
            Symbol("fmt", "global", 1, 2),
            Symbol("local", "local", 0, 4),
            Symbol("external_timer", "global", None, 0),
        ],
        relocations=[
            Relocation(section=0, offset=4, symbol=3,
                       kind="PCREL_DISP14", addend=0),
        ],
    )


# --------------------------------------------------------------------------
# Container round trip and determinism
# --------------------------------------------------------------------------


def test_encode_decode_roundtrip():
    obj = sample_object()
    blob = encode_object(obj)
    assert blob[:4] == NOBJ_MAGIC
    assert blob[4:6] == NOBJ_VERSION.to_bytes(2, "little")
    assert len(blob) >= NOBJ_HEADER_SIZE
    decoded = decode_object(blob)
    assert decoded == obj
    assert decoded.sections[2].kind == "nobits"
    assert decoded.sections[2].size == 16
    assert decoded.sections[2].data == b""
    assert decoded.symbols[3].is_defined is False
    assert decoded.symbols[3].binding == "global"


def test_encoding_is_deterministic():
    obj = sample_object()
    first = encode_object(obj)
    assert encode_object(obj) == first
    assert encode_object(decode_object(first)) == first


def test_progbits_size_is_inferred_from_data():
    obj = ObjectFile(sections=[Section(".text", "progbits", (), 4,
                                        bytes(8))])
    assert obj.sections[0].size == 8
    assert decode_object(encode_object(obj)).sections[0].size == 8


def test_write_and_load_roundtrip(tmp_path):
    path = Path(tmp_path) / "program.nobj"
    write_object(path, sample_object())
    assert load_object(path) == sample_object()
    assert path.read_bytes()[:4] == NOBJ_MAGIC


def test_empty_object_is_valid():
    obj = ObjectFile()
    decoded = decode_object(encode_object(obj))
    assert decoded == obj
    assert decoded.sections == ()
    assert decoded.symbols == ()


# --------------------------------------------------------------------------
# Construction validation
# --------------------------------------------------------------------------


def test_undefined_symbol_must_be_global():
    with pytest.raises(ObjectError, match="undefined symbol"):
        encode_object(ObjectFile(symbols=[Symbol("x", "local", None)]))


def test_undefined_symbol_must_have_zero_value():
    with pytest.raises(ObjectError, match="value 0"):
        encode_object(ObjectFile(symbols=[Symbol("x", "global", None, 5)]))


def test_symbol_section_must_be_in_range():
    with pytest.raises(ObjectError, match="out-of-range section"):
        encode_object(ObjectFile(symbols=[Symbol("x", "global", 7, 0)]))


def test_symbol_offset_within_section():
    with pytest.raises(ObjectError, match="outside its section"):
        encode_object(
            ObjectFile(sections=[Section(".text", data=bytes(4))],
                       symbols=[Symbol("x", "local", 0, 8)])
        )


def test_one_past_the_end_symbol_offset_is_allowed():
    obj = ObjectFile(sections=[Section(".text", data=bytes(4))],
                     symbols=[Symbol("end", "local", 0, 4)])
    assert decode_object(encode_object(obj)).symbols[0].value == 4


def test_relocation_symbol_and_section_must_be_in_range():
    with pytest.raises(ObjectError, match="out-of-range symbol"):
        encode_object(
            ObjectFile(
                sections=[Section(".text", data=bytes(8))],
                symbols=[Symbol("x", "global", None, 0)],
                relocations=[Relocation(section=0, offset=0, symbol=9)],
            )
        )
    with pytest.raises(ObjectError, match="out-of-range section"):
        encode_object(
            ObjectFile(
                sections=[Section(".text", data=bytes(8))],
                symbols=[Symbol("x", "global", None, 0)],
                relocations=[Relocation(section=5, offset=0, symbol=0)],
            )
        )


def test_relocation_offset_must_be_instruction_aligned_and_in_section():
    with pytest.raises(ObjectError, match="aligned"):
        encode_object(
            ObjectFile(
                sections=[Section(".text", data=bytes(8))],
                symbols=[Symbol("x", "global", None, 0)],
                relocations=[Relocation(section=0, offset=2, symbol=0)],
            )
        )
    with pytest.raises(ObjectError, match="does not fit 4 bytes of relocation"):
        encode_object(
            ObjectFile(
                sections=[Section(".text", data=bytes(4))],
                symbols=[Symbol("x", "global", None, 0)],
                relocations=[Relocation(section=0, offset=4, symbol=0)],
            )
        )


def test_relocations_only_on_progbits_sections():
    with pytest.raises(ObjectError, match="progbits section"):
        encode_object(
            ObjectFile(
                sections=[Section(".bss", "nobits", ("alloc", "write"),
                                 4, b"", 16)],
                symbols=[Symbol("x", "global", None, 0)],
                relocations=[Relocation(section=0, offset=0, symbol=0)],
            )
        )


def test_duplicate_section_and_symbol_names_rejected():
    with pytest.raises(ObjectError, match="duplicate section name"):
        encode_object(ObjectFile(sections=[Section(".text", data=bytes(4)),
                                           Section(".text", data=bytes(4))]))
    with pytest.raises(ObjectError, match="duplicate symbol name"):
        encode_object(
            ObjectFile(
                symbols=[Symbol("a", "global", None, 0),
                         Symbol("a", "global", None, 0)],
            )
        )


def test_section_validation():
    with pytest.raises(ObjectError, match="does not match its data length"):
        Section(".text", "progbits", (), 4, bytes(4), size=8)
    with pytest.raises(ObjectError, match="cannot carry data"):
        Section(".bss", "nobits", (), 4, bytes(4))
    with pytest.raises(ObjectError, match="power of two"):
        encode_object(ObjectFile(sections=[Section(".text", "progbits", (), 3,
                                                   bytes(4))]))
    with pytest.raises(ObjectError, match="section kind"):
        encode_object(ObjectFile(sections=[Section(".x", "data", (), 4)]))


def test_noname_rejected():
    with pytest.raises(ObjectError, match="non-empty"):
        encode_object(ObjectFile(sections=[Section("", data=bytes(4))]))


# --------------------------------------------------------------------------
# Malformed and truncated byte streams
# --------------------------------------------------------------------------


def _raw(obj):
    return bytearray(encode_object(obj))


def test_bad_magic_is_rejected():
    blob = _raw(sample_object())
    blob[0:4] = b"NOPE"
    with pytest.raises(ObjectError, match="magic"):
        decode_object(bytes(blob))


def test_unsupported_version_and_flags_rejected():
    blob = _raw(sample_object())
    blob[4:6] = (2).to_bytes(2, "little")
    with pytest.raises(ObjectError, match="version"):
        decode_object(bytes(blob))
    blob = _raw(sample_object())
    blob[6:8] = (1).to_bytes(2, "little")
    with pytest.raises(ObjectError, match="flags"):
        decode_object(bytes(blob))


def test_truncated_header_rejected():
    with pytest.raises(ObjectError, match="truncated"):
        decode_object(b"NOBJ" + bytes(NOBJ_HEADER_SIZE - 8))


def test_truncated_record_rejected():
    blob = _raw(sample_object())
    with pytest.raises(ObjectError, match="truncated"):
        decode_object(bytes(blob[:-3]))


def test_trailing_bytes_rejected():
    blob = _raw(sample_object())
    with pytest.raises(ObjectError, match="trailing"):
        decode_object(bytes(blob) + b"junk")


def test_implausible_counts_rejected():
    blob = _raw(sample_object())
    blob[16:24] = (2 ** 62).to_bytes(8, "little")  # absurd symbol count
    with pytest.raises(ObjectError):
        decode_object(bytes(blob))


def test_bad_section_kind_byte_rejected():
    blob = _raw(sample_object())
    # First section record: name_length(4) + ".text"(5), then the kind byte.
    blob[32 + 4 + 5] = 0x7F  # kind byte
    with pytest.raises(ObjectError, match="unknown kind byte"):
        decode_object(bytes(blob))


def test_section_size_mismatch_after_header_rejected():
    blob = _raw(sample_object())
    # First section: name_length(4) + ".text"(5) + kind+flags+align+reserved(4)
    # then size at offset 32 + 4 + 5 + 4 = 45.
    blob[45] = 0xFF  # claim 255 data bytes
    with pytest.raises(ObjectError, match="truncated"):
        decode_object(bytes(blob))


def test_nonzero_reserved_padding_rejected():
    blob = _raw(sample_object())
    blob[32 + 4 + 5 + 3] = 0x01  # reserved padding byte in first section
    with pytest.raises(ObjectError, match="reserved"):
        decode_object(bytes(blob))


def test_wrong_object_type_is_rejected():
    with pytest.raises(ObjectError, match="ObjectFile"):
        encode_object({"sections": []})  # type: ignore[arg-type]
    with pytest.raises(ObjectError, match="byte string"):
        decode_object(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Assembler object frontend
# --------------------------------------------------------------------------


def test_assemble_object_text_matches_executable_layout():
    source = """
start:
    MOVI r1, 42
    ADDI r2, r1, -4
loop:
    SUB r2, r2, r1
    CMP r2, r1
    BNE r2, r1, loop
end:
    RET
"""
    obj = assemble_object(source)
    assert obj.sections[0].data == assemble_text(source, entry=0)
    assert obj.sections[0].flags == ("alloc", "exec")
    assert obj.sections[0].alignment == 4
    assert obj.relocations == ()
    names = {s.name for s in obj.symbols}
    assert names == {"start", "loop", "end"}
    assert all(s.is_defined and s.section == 0 for s in obj.symbols)
    assert all(s.binding == "local" for s in obj.symbols)


def test_assemble_object_symbol_values_are_section_relative():
    source = """
start:
    MOVI r1, 42
middle:
    MOVI r2, 7
end:
    RET
"""
    obj = assemble_object(source)
    offsets = {s.name: s.value for s in obj.symbols}
    assert offsets == {"start": 0, "middle": 4, "end": 8}


def test_assemble_object_global_directive_exports_local_labels():
    obj = assemble_object("""
.global start
.globl start
start:
    MOVI r1, 1
    RET
""")
    start = next(s for s in obj.symbols if s.name == "start")
    assert start.binding == "global"
    assert start.is_defined


def test_assemble_object_branch_to_external_is_a_relocation():
    obj = assemble_object("""
start:
    CALL helper
    BEQ r1, r2, elsewhere
    RET
helper:
    RET
""")
    names = {s.name: s for s in obj.symbols}
    assert names["helper"].is_defined and names["helper"].binding == "local"
    assert names["elsewhere"].is_defined is False
    assert names["elsewhere"].binding == "global"
    assert names["elsewhere"].value == 0
    kinds = {r.kind for r in obj.relocations}
    assert kinds == {"PCREL_DISP14"}
    assert all(r.addend == 0 for r in obj.relocations)
    # The BEQ site is the word at offset 4 and carries the placeholder 0.
    by_symbol = {obj.symbols[r.symbol].name: r for r in obj.relocations}
    assert by_symbol["elsewhere"].offset == 4
    # The defined internal call (helper) produces no relocation.
    assert "helper" not in by_symbol


def test_assemble_object_external_survives_encoding():
    obj = assemble_object("start:\n  CALL helper\nstart_helper:\n  RET\n")
    blob = encode_object(obj)
    decoded = decode_object(blob)
    assert decoded == obj
    assert decoded.symbols[0].name == "start"
    assert decoded.relocations[0].kind == "PCREL_DISP14"
    assert decoded.relocations[0].offset == 0


def test_assemble_object_declared_but_undefined_external():
    obj = assemble_object(".global timer\nstart:\n  MOVI r1, 1\n  HALT\n")
    timer = next(s for s in obj.symbols if s.name == "timer")
    assert timer.is_defined is False and timer.binding == "global"
    assert obj.relocations == ()


def test_assemble_object_la_to_defined_label_is_an_la64_relocation():
    source = """
start:
    LA r1, end
    MOVI r2, 0
end:
    RET
"""
    obj = assemble_object(source)
    # LA to a symbol is a LA64 relocation over a fixed nine-instruction block;
    # the section-relative layout puts its placeholder at zero so the value is
    # not baked in as a constant.
    assert len(obj.relocations) == 1
    relocation = obj.relocations[0]
    assert relocation.offset == 0
    assert relocation.kind == "LA64"
    assert relocation.addend == 0
    assert obj.symbols[relocation.symbol].name == "end"
    assert relocation.offset + RELOCATION_SPAN_BYTES["LA64"] == 36
    offsets = {s.name: s.value for s in obj.symbols}
    assert offsets == {"start": 0, "end": 40}
    assert len(obj.sections[0].data) == 44
    first = int.from_bytes(obj.sections[0].data[:4], "little")
    assert decode_instruction(first).mnemonic == "MOVI"
    assert decode_instruction(first).register("rd") == 1


def test_assemble_object_la_to_undefined_becomes_an_la64_relocation():
    obj = assemble_object("start:\n  LA r1, gone\n  RET\n")
    gone = next(s for s in obj.symbols if s.name == "gone")
    assert gone.binding == "global" and gone.is_defined is False
    assert len(obj.relocations) == 1
    assert obj.relocations[0].kind == "LA64"
    assert obj.symbols[obj.relocations[0].symbol].name == "gone"


def test_assemble_object_unknown_directive_rejected():
    with pytest.raises(AssemblyError, match="not defined"):
        assemble_object(".data 100\nstart:\n  MOVI r1, 1\n")


def test_assemble_object_keeps_statement_line_numbers():
    with pytest.raises(AssemblyError) as excinfo:
        assemble_object(".global ok\nMOVI r1, 1\nThisIsNotAnInstruction x\n")
    assert excinfo.value.line == 3


def test_assemble_object_malformed_global_rejected():
    with pytest.raises(AssemblyError, match="malformed .global symbol"):
        assemble_object(".global 1bad\n")

def test_two_objects_reference_external_symbol_by_name():
    module_a = assemble_object("""
start:
    CALL notify
    RET
""")
    module_b = assemble_object("""
.global notify
notify:
    MOVI r1, 42
    RET
""")
    blob_a, blob_b = encode_object(module_a), encode_object(module_b)
    a, b = decode_object(blob_a), decode_object(blob_b)
    reloc = a.relocations[0]
    referenced = b.symbols[0]
    assert a.symbols[reloc.symbol].name == "notify"
    assert a.symbols[reloc.symbol].is_defined is False
    assert referenced.name == a.symbols[reloc.symbol].name
    assert referenced.is_defined and referenced.binding == "global"
    assert referenced.section == 0


def test_nobj_registry_publishes_implemented_object_format():
    fmt = get_file_format("module.nobj")
    assert fmt.kind == "object"
    assert fmt.implemented
    assert fmt.magic == NOBJ_MAGIC