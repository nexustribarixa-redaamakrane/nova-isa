"""Tests for the Nova .nox debug container and its assembler directives."""

import pytest

from nova import (
    DebugInfo,
    FrameDescription,
    RegisterSave,
    SourceFile,
    SourceLocation,
    decode_debug,
    encode_debug,
)
from nova.assembler import AssemblyError, assemble_object
from nova.fileformats import get_file_format
from nova.nox import NOX_MAGIC, NOX_VERSION, NoxError


def sample_debug():
    return DebugInfo(
        files=(SourceFile(1, "main.c"), SourceFile(3, "inc/math.h")),
        locations=(
            SourceLocation(0, 1, 10, 1),
            SourceLocation(8, 3, 42, 7),
        ),
        frames=(FrameDescription("main", 0, 24, 29, -16),),
        register_saves=(RegisterSave(4, 31, -8), RegisterSave(8, 14, -16)),
    )


def test_debug_info_round_trip_is_deterministic():
    blob = encode_debug(sample_debug())
    assert blob[:len(NOX_MAGIC)] == NOX_MAGIC
    decoded = decode_debug(blob)
    assert decoded == sample_debug()
    assert encode_debug(decoded) == blob


def test_debug_info_decodes_in_canonical_order():
    info = DebugInfo(
        files=(SourceFile(3, "b.c"), SourceFile(1, "a.c")),
        locations=(SourceLocation(16, 1, 2), SourceLocation(0, 1, 1)),
    )
    decoded = decode_debug(encode_debug(info))
    assert [item.index for item in decoded.files] == [1, 3]
    assert [item.offset for item in decoded.locations] == [0, 16]


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda data: b"NOVADBGX" + data[8:], "magic"),
        (lambda data: data[:8] + bytes([9]) + data[9:], "version"),
        (lambda data: data[:-1], "body size"),
        (lambda data: data[:12]
         + (int.from_bytes(data[12:16], "little") + 5).to_bytes(4, "little")
         + data[16:] + bytes((99, 0, 0, 0, 0)), "unknown .nox record"),
    ],
)
def test_malformed_containers_are_rejected(mutate, message):
    with pytest.raises(NoxError, match=message):
        decode_debug(mutate(encode_debug(sample_debug())))


def test_debug_info_rejects_inconsistent_records():
    with pytest.raises(NoxError, match="duplicate source file"):
        DebugInfo(files=(SourceFile(1, "a.c"), SourceFile(1, "b.c")))
    with pytest.raises(NoxError, match="ends before"):
        DebugInfo(frames=(FrameDescription("f", 16, 8, 29, 0),))


def test_assembler_emits_debug_records():
    obj = assemble_object("""
.file 1, "main.c"
.global _start
.type _start, function
_start:
.loc 1, 7, 0
    MOVI r1, 1
.cfi_startproc _start, 0, 8, 29, -16
.cfi_offset 4, 31, -8
.cfi_endproc
.size _start, 8
    HALT
""")
    assert obj.debug is not None
    assert obj.debug.files == (SourceFile(1, "main.c"),)
    assert obj.debug.locations == (SourceLocation(0, 1, 7),)
    assert obj.debug.frames == (FrameDescription("_start", 0, 8, 29, -16),)
    assert obj.debug.register_saves == (RegisterSave(4, 31, -8),)
    assert decode_debug(encode_debug(obj.debug)) == obj.debug


@pytest.mark.parametrize(
    "source,message",
    [
        (".loc 2, 1, 0\nNOP\n", "undeclared file index"),
        ('.file 1, "a.c"\n.loc 1, 1, 999\nNOP\n', "beyond the .text section"),
        (".file 0, main.c\nNOP\n", "must be quoted"),
        (".cfi_startproc f, 0, 999, 29, -16\nNOP\n", "beyond the .text section"),
        (".loc 1, 1\nNOP\n", "expects file, line and offset"),
    ],
)
def test_debug_directives_reject_bad_input(source, message):
    with pytest.raises(AssemblyError, match=message):
        assemble_object(source)


def test_nox_is_registered_as_implemented():
    fmt = get_file_format("program.nox")
    assert fmt.kind == "debug"
    assert fmt.implemented
    assert fmt.magic == NOX_MAGIC
    assert NOX_VERSION == 1
