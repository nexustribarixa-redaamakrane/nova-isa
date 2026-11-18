"""Tests for Nova assembler includes and macros."""

import pytest

from nova.assembler import (
    AssemblyError,
    assemble_object,
    assemble_text,
    preprocess_source,
)


def test_include_expands_in_executable_assembly(tmp_path):
    (tmp_path / "body.no").write_text("MOVI r1, 7\n", encoding="utf-8")
    source = '.include "body.no"\nHALT\n'
    image = assemble_text(source, source_path=tmp_path / "main.no")
    assert image == assemble_text("MOVI r1, 7\nHALT\n")


def test_include_expands_in_object_assembly(tmp_path):
    (tmp_path / "body.no").write_text(
        ".global _start\n_start:\n  MOVI r1, 7\n  RET\n", encoding="utf-8"
    )
    obj = assemble_object('.include "body.no"\n',
                          source_path=tmp_path / "main.no")
    assert [symbol.name for symbol in obj.symbols] == ["_start"]


def test_nested_include_uses_including_file_directory(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inner.no").write_text("MOVI r2, 3\n", encoding="utf-8")
    (tmp_path / "outer.no").write_text(
        '.include "nested/inner.no"\n', encoding="utf-8"
    )
    image = assemble_text('.include "outer.no"\nHALT\n',
                          source_path=tmp_path / "main.no")
    assert image == assemble_text("MOVI r2, 3\nHALT\n")


def test_include_searches_additional_directories(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    (library / "defs.no").write_text("MOVI r3, 9\n", encoding="utf-8")
    image = assemble_text('.include "defs.no"\nHALT\n',
                          include_dirs=[library])
    assert image == assemble_text("MOVI r3, 9\nHALT\n")


@pytest.mark.parametrize(
    "source,message",
    [
        ('.include missing.no\n', "must be quoted"),
        ('.include "absent.no"\n', "was not found"),
        (".include\n", "requires one quoted path"),
    ],
)
def test_include_rejects_bad_input(source, message):
    with pytest.raises(AssemblyError, match=message):
        assemble_text(source)


def test_recursive_include_is_rejected(tmp_path):
    (tmp_path / "loop.no").write_text(
        '.include "loop.no"\n', encoding="utf-8"
    )
    with pytest.raises(AssemblyError, match="recursive .include"):
        assemble_text('.include "loop.no"\n', source_path=tmp_path / "main.no")


def test_macro_substitutes_positional_parameters():
    source = """
.macro load_pair dst, value
MOVI %dst, %value
ADDI %dst, %dst, 1
.endm
load_pair r5, 10
HALT
"""
    assert assemble_text(source) == assemble_text(
        "MOVI r5, 10\nADDI r5, r5, 1\nHALT\n"
    )


def test_macro_can_be_invoked_from_another_macro():
    source = """
.macro inner reg
MOVI %reg, 1
.endm
.macro outer reg
inner %reg
ADDI %reg, %reg, 1
.endm
outer r6
HALT
"""
    assert assemble_text(source) == assemble_text(
        "MOVI r6, 1\nADDI r6, r6, 1\nHALT\n"
    )


def test_macro_expands_in_object_assembly():
    source = """
.macro entry name
.global %name
%name:
  RET
.endm
entry _start
"""
    obj = assemble_object(source)
    assert [symbol.name for symbol in obj.symbols] == ["_start"]


@pytest.mark.parametrize(
    "source,message",
    [
        (".macro foo a\nMOVI r1, %a\n", "unterminated .macro"),
        (".macro foo a\nMOVI r1, %a\n.endm\nfoo r1, r2\n", "expects 1 argument"),
        (".macro foo a, a\n.endm\n", "duplicate macro parameter"),
        (".badmacro foo\n", "directives are not defined"),
    ],
)
def test_macro_rejects_bad_input(source, message):
    with pytest.raises(AssemblyError, match=message):
        assemble_text(source)


def test_preprocess_source_is_pure_and_deterministic():
    source = ".macro one x\nNOP\n.endm\none r1\n"
    assert preprocess_source(source) == preprocess_source(source)
