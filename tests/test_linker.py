"""Nova linker v0.1 tests.

Covers the library contract of :mod:`nova.link`: deterministic section
layout and alignment, symbol resolution rules, both relocation kinds
(``PCREL_DISP14`` and ``LA64``) including the cross-object movable block,
entry-symbol handling, error diagnostics, and an end-to-end
assemble-link-run pipeline.
"""

from dataclasses import replace

import pytest

from nova import (
    DEFAULT_ENTRY_SYMBOL,
    DEFAULT_IMAGE_BASE,
    LinkError,
    assemble_object,
    decode_nob,
    encode_nob,
    link_objects,
)
from nova.emulator import NovaMachine
from nova.isa import INSTRUCTION_BYTES, PRIVILEGE_LEVELS, decode_instruction
from nova.object import ObjectFile, Relocation, Section, Symbol, decode_object, encode_object
from nova.pseudo import MAX_LI_LENGTH

IMAGE_BASE = DEFAULT_IMAGE_BASE

TEXT_FLAGS = ("alloc", "exec")
DATA_FLAGS = ("alloc", "write")


def text_section(data, alignment=4):
    return Section(name=".text", kind="progbits", flags=TEXT_FLAGS,
                   alignment=alignment, data=data)


def data_section(data, alignment=4):
    return Section(name=".data", kind="progbits", flags=DATA_FLAGS,
                   alignment=alignment, data=data)


def bss_section(size, alignment=4):
    return Section(name=".bss", kind="nobits", flags=DATA_FLAGS,
                   alignment=alignment, size=size, data=b"")


def rodata_section(data, alignment=4):
    return Section(name=".rodata", kind="progbits", flags=("alloc",),
                   alignment=alignment, data=data)


def symbol(name, section=None, value=0, binding="local"):
    return Symbol(name=name, section=section, value=value, binding=binding)


def validate(obj):
    encode_object(obj)
    return obj


def make(*sections, entry=False, symbols=()):
    symbols = tuple(symbols)
    if entry:
        symbols = symbols + (symbol("_start", section=0, binding="global"),)
    return validate(ObjectFile(sections=sections, symbols=symbols,
                               relocations=()))


def run_linked(image, steps=10_000):
    encoded = encode_nob(image_base=image.image_base, entry=image.entry,
                         image=image.image)
    loaded = decode_nob(encoded)
    machine = NovaMachine(code=loaded.image, entry=loaded.entry)
    machine.priv = PRIVILEGE_LEVELS["machine"]
    for _ in range(steps):
        if machine.halted:
            break
        machine.step()
    return machine


def word_at(image, offset):
    return int.from_bytes(image[offset:offset + 4], "little")


def target_field(word):
    decoded = decode_instruction(word)
    assert decoded is not None
    return decoded.fields[decoded.definition.operand("target").fields[0]]


# --------------------------------------------------------------------------
# Section layout
# --------------------------------------------------------------------------


def test_sections_are_placed_in_documented_order_and_aligned():
    first = make(text_section(b"\x11\x00\x00\x00"), entry=True)
    second = make(
        rodata_section(b"\xBB\xBB", alignment=8),
        data_section(b"\xCC\xCC\xCC\xCC"),
        bss_section(4),
    )
    image = link_objects([first, second])
    assert image.image_base == IMAGE_BASE
    expected = (
        b"\x11\x00\x00\x00"     # .text: 4 bytes, aligned 4
        b"\x00\x00\x00\x00"     # pad to 8-byte alignment for .rodata
        b"\xBB\xBB"             # .rodata: 2 bytes
        b"\x00\x00"             # pad to 4-byte alignment for .data
        b"\xCC\xCC\xCC\xCC"     # .data: 4 bytes
        b"\x00\x00\x00\x00"     # .bss: 4 bytes of zero-init storage
    )
    assert image.image == expected
    assert image.entry == IMAGE_BASE


def test_unknown_sections_are_placed_after_bss():
    first = make(
        bss_section(4),
        Section(name=".comments", kind="progbits", flags=("alloc",),
                alignment=4, data=b"\xDD\x00\x00\x00"),
        entry=True,
    )
    image = link_objects([first], entry_symbol="_start")
    assert image.image == b"\x00\x00\x00\x00\xDD\x00\x00\x00"


def test_members_are_grouped_by_section_name_in_input_order():
    a = make(text_section(b"\x01\x00\x00\x00"), entry=True)
    b = make(text_section(b"\x02\x00\x00\x00\x03\x00\x00\x00"))
    image = link_objects([a, b])
    assert image.image == b"\x01\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00"


def test_member_alignment_pads_within_a_group():
    first = make(text_section(b"\x01\x00\x00\x00"), entry=True)
    second = make(text_section(b"\x02\x00\x00\x00", alignment=8))
    image = link_objects([first, second])
    assert image.image == (
        b"\x01\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x02\x00\x00\x00"
    )


def test_missing_entry_symbol_is_rejected():
    a = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("never_entry", section=0),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="undefined entry symbol '_start'"):
        link_objects([a])


def test_custom_entry_symbol_is_resolved():
    a = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("begin", section=0),),
        relocations=(),
    ))
    image = link_objects([a], entry_symbol="begin")
    assert image.entry == IMAGE_BASE


def test_entry_symbol_outside_the_image_is_rejected():
    a = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("_start", section=0, value=4, binding="global"),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="entry symbol '_start' .* is outside"):
        link_objects([a])


def test_no_input_objects_is_rejected():
    with pytest.raises(LinkError, match="no input object files"):
        link_objects([])


# --------------------------------------------------------------------------
# Symbol resolution
# --------------------------------------------------------------------------


def test_defined_global_symbol_is_not_duplicated_once_owned():
    a = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("_start", section=0, binding="global"),),
        relocations=(),
    ))
    image = link_objects([a])
    assert image.entry == IMAGE_BASE


def test_local_symbol_is_invisible_to_other_objects():
    owner = make(
        text_section(b"\x00\x00\x00\x00"),
        entry=True,
        symbols=(symbol("helper", section=0),),
    )
    user = make(
        text_section(b"\x00\x00\x00\x00"),
        symbols=(symbol("helper", section=None, binding="global"),),
    )
    with pytest.raises(LinkError, match="undefined symbol 'helper'"):
        link_objects([owner, user])


def test_undefined_symbol_is_rejected():
    user = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("_start", section=0, binding="global"),
                 symbol("missing", section=None, binding="global"),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="undefined symbol 'missing'"):
        link_objects([user])


def test_duplicate_global_definition_is_rejected():
    a = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("_start", section=0, binding="global"),
                 symbol("add", section=0, binding="global"),),
        relocations=(),
    ))
    b = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00"),),
        symbols=(symbol("add", section=0, binding="global"),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="multiple definition of 'add'"):
        link_objects([a, b])


# --------------------------------------------------------------------------
# Relocations
# --------------------------------------------------------------------------


def test_pcrel_relocation_is_rebased_across_objects():
    caller = assemble_object("""
.global _start
_start:
    CALL add
    RET
""")
    callee = assemble_object("""
.global add
add:
    MOVI r1, 7
    RET
""")
    image = link_objects([caller, callee])
    call_word = word_at(image.image, 0)          # the CALL site
    assert image.entry == IMAGE_BASE
    assert target_field(call_word) == len(caller.sections[0].data)


def test_pcrel_relocation_overflow_is_reported():
    caller = assemble_object("""
.global _start
_start:
    CALL far
    RET
""")
    callee = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00", alignment=0x8000),),
        symbols=(symbol("far", section=0, binding="global"),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="relocation of 'far' .* overflows"):
        link_objects([caller, callee])


def test_pcrel_relocation_to_unaligned_target_is_reported():
    caller = assemble_object("""
.global _start
_start:
    BNE r1, r2, odd
    RET
""")
    holder = validate(ObjectFile(
        sections=(text_section(b"\x00\x00\x00\x00", alignment=8),),
        symbols=(symbol("odd", section=0, value=1, binding="global"),),
        relocations=(),
    ))
    with pytest.raises(LinkError, match="not instruction-aligned"):
        link_objects([caller, holder])


def test_la64_relocation_rewrites_the_cross_object_block():
    caller = assemble_object("""
.global _start
_start:
    LA r3, value
    HALT
""")
    callee = assemble_object("""
.global value
value:
    NOP
""")
    image = link_objects([caller, callee])
    machine = run_linked(image)
    assert machine.halted
    assert len(caller.sections[0].data) == (MAX_LI_LENGTH + 1) * INSTRUCTION_BYTES
    expected = IMAGE_BASE + len(caller.sections[0].data)
    assert machine.regs[3] == expected


def test_la64_relocation_must_start_at_a_movi():
    body = "\n".join("    ADDI r1, r1, 1" for _ in range(9))
    text_data = assemble_object(f"start:\n{body}\n").sections[0].data
    obj = validate(ObjectFile(
        sections=(text_section(text_data),),
        symbols=(symbol("_start", section=0, binding="global"),
                 symbol("value", section=None, binding="global")),
        relocations=(Relocation(section=0, offset=0, kind="LA64",
                                symbol=1, addend=0),),
    ))
    holder = assemble_object(".global value\nvalue:\n  NOP\n")
    with pytest.raises(LinkError, match="does not start at a MOVI"):
        link_objects([obj, holder])


def test_unsupported_relocation_kind_is_reported():
    base = assemble_object(".global _start\n_start:\n  NOP\n")
    obj = replace(base, relocations=(Relocation(
        section=0, offset=0, kind="bogus", symbol=0, addend=0),))
    with pytest.raises(LinkError, match="unsupported relocation kind 'bogus'"):
        link_objects([obj])


def test_la64_encoding_roundtrip_preserves_the_kind():
    obj = assemble_object("start:\n  LA r1, gone\n  RET\n")
    decoded = decode_object(encode_object(obj))
    assert decoded.relocations == obj.relocations
    assert decoded.relocations[0].kind == "LA64"


# --------------------------------------------------------------------------
# Determinism and end-to-end
# --------------------------------------------------------------------------


def test_linking_is_deterministic():
    caller = assemble_object("""
.global _start
_start:
    LI r0, 0
    LA r3, left
    CALL right
    LA r3, right
    LA r3, left
    RET
""")
    callee = assemble_object("""
.global left
.global right
left:
    MOVI r4, 1
    RET
right:
    MOVI r5, 2
    RET
""")
    first = link_objects([caller, callee])
    second = link_objects([caller, callee])
    assert first.image == second.image
    assert first.entry == second.entry
    assert encode_nob(image_base=first.image_base, entry=first.entry,
                      image=first.image) == encode_nob(
        image_base=second.image_base, entry=second.entry, image=second.image)


def test_end_to_end_objects_link_and_run():
    main = assemble_object("""
.global _start
_start:
    LI r1, 3
    CALL math_square
    LA r2, math_table
    STW r1, [r2]
    LDW r5, [r2]
    SUB r6, r5, r1
    HALT
""")
    math = assemble_object("""
.global math_square
.global math_table
math_square:
    MUL r1, r1, r1
    RET
math_table:
    NOP
""")
    image = link_objects([main, math])
    machine = run_linked(image)
    assert machine.halted
    assert machine.regs[1] == 9
    assert machine.regs[5] == 9
    assert machine.regs[6] == 0