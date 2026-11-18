"""Documentation conformance tests.

The narrative specification in ``README.md`` and the machine-readable contract
in :mod:`nova.isa` must not drift apart. These tests check the generated tables
byte for byte, and check that the document does not describe anything the code
does not implement.
"""

import re
from pathlib import Path

import pytest

from nova import LINKER_VERSION, TOOLCHAIN_VERSION, TRAP_ABI_VERSION
from nova.assembler import DEFAULT_ENTRY
from nova.isa import (
    CSR_TABLE,
    EXCEPTION_CODES,
    EXCEPTION_STACK_DEPTH,
    INSTRUCTION_TABLE,
    ISA_VERSION,
    RESET_ENTRY,
    RESET_VBAR,
    csr_table_markdown,
    instruction_table_markdown,
    opcode_table_markdown,
)
from nova.nob import NOB_HEADER_SIZE, NOB_MAGIC, NOB_VERSION
from nova.object import NOBJ_HEADER_SIZE, NOBJ_MAGIC, NOBJ_VERSION

README = Path(__file__).resolve().parent.parent / "README.md"
BLOCK = re.compile(
    r"<!-- BEGIN GENERATED: (?P<name>\w+) -->\n(?P<body>.*?)"
    r"<!-- END GENERATED: (?P=name) -->",
    re.S,
)


@pytest.fixture(scope="module")
def readme():
    return README.read_text(encoding="utf-8")


def generated_block(readme, name):
    for match in BLOCK.finditer(readme):
        if match.group("name") == name:
            return match.group("body").strip("\n").splitlines()
    raise AssertionError(f"README.md has no generated block named {name!r}")


def test_readme_exists_and_declares_the_isa_version(readme):
    assert ISA_VERSION in readme, (
        f"README.md must state {ISA_VERSION}; it is generated from nova.isa"
    )


def test_generated_instruction_table_matches_the_code(readme):
    expected = [
        "| Mnemonic | Opcode | Format | Operands | Privilege | Flags | Exceptions | Summary |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *instruction_table_markdown(),
    ]
    assert generated_block(readme, "instruction_table") == expected


def test_generated_csr_table_matches_the_code(readme):
    expected = [
        "| Index | Name | Width | Reset | Access | Alignment | Purpose |",
        "| ---: | --- | ---: | --- | --- | --- | --- |",
        *csr_table_markdown(),
    ]
    assert generated_block(readme, "csr_table") == expected


def test_generated_opcode_table_matches_the_code(readme):
    expected = [
        "| Opcodes | Purpose |",
        "| --- | --- |",
        *opcode_table_markdown(),
    ]
    assert generated_block(readme, "opcode_table") == expected


def test_every_instruction_appears_in_the_prose(readme):
    for name in INSTRUCTION_TABLE:
        assert f"`{name}`" in readme, f"{name} is defined but never described"


def test_every_csr_and_exception_code_appears_in_the_prose(readme):
    for csr in CSR_TABLE.values():
        assert f"`{csr.name}`" in readme, csr.name
    for name in EXCEPTION_CODES:
        assert f"`{name}`" in readme, name


#: Uppercase tokens that appear in the prose but are not mnemonics: the PSR
#: flag names, the program counter, the signed 64-bit minimum, the assembler
#: pseudo-instructions, the magic bytes of the ``.nob`` and ``.nobj``
#: containers, and the ``LA64`` relocation kind.
PROSE_TOKENS = {"Z", "N", "C", "V", "PC", "MIN", "LI", "LA", "NOBV", "NOBJ",
                "LA64", "ABS64", "PROFILE", "NOBO", "BL"}


def test_readme_does_not_claim_an_instruction_that_does_not_exist(readme):
    mentioned = set(re.findall(r"`([A-Z][A-Z0-9]*(?:\.[A-Z]+)?)`", readme))
    defined = set(INSTRUCTION_TABLE) | set(CSR_TABLE) | set(EXCEPTION_CODES)
    undeclared = {
        token for token in mentioned
        if token not in defined and token not in PROSE_TOKENS
    }
    assert not undeclared, f"README mentions undefined names: {sorted(undeclared)}"


def test_documented_constants_match_the_code(readme):
    assert f"`{RESET_ENTRY:#x}`" in readme
    assert f"`{RESET_VBAR:#x}`" in readme
    assert f"depth of {EXCEPTION_STACK_DEPTH}" in readme or (
        f"depth-4" in readme or "4 frames" in readme
    )
    assert f"`{DEFAULT_ENTRY:#x}`" in readme


def test_readme_states_the_operand_kinds_it_supports(readme):
    assert "`CSRR`" in readme and "`CSRW`" in readme


def test_readme_documents_the_command_line_tools(readme):
    for tool in ("nova-as", "nova-ld", "nova-objdump", "nova-run"):
        assert f"`{tool}`" in readme or f"``{tool}``" in readme
        assert f"python -m nova.{tool.split('-', 1)[1]}" in readme
    assert TOOLCHAIN_VERSION in readme


def test_readme_documents_the_pseudo_instructions(readme):
    assert "`LI rd, imm`" in readme
    assert "`LA rd, target`" in readme
    assert "never appear in a `.nob` image" in readme


def test_readme_documents_the_nob_container(readme):
    assert "`.nob`" in readme
    assert f"magic `{NOB_MAGIC.decode('ascii')}`" in readme
    assert f"{NOB_HEADER_SIZE}-byte header" in readme
    assert f"`NOB_VERSION`" in readme or "format v1" in readme
    assert str(NOB_VERSION) in readme


def test_readme_documents_the_relocatable_object(readme):
    assert "`.nobj`" in readme
    assert f"magic `{NOBJ_MAGIC.decode('ascii')}`" in readme
    assert f"{NOBJ_HEADER_SIZE}-byte header" in readme
    assert f"`NOBJ_VERSION`" in readme or "format v1" in readme
    assert str(NOBJ_VERSION) in readme
    assert "`assemble_object`" in readme
    assert "`PCREL_DISP14`" in readme
    assert "`LA64`" in readme
    assert "`nova-ld`" in readme
    assert "`.global`" in readme and "`.globl`" in readme


def test_readme_documents_the_linker(readme):
    assert LINKER_VERSION in readme
    for section in (".text", ".rodata", ".data", ".bss"):
        assert f"`{section}`" in readme
    assert "undefined symbol 'foo'" in readme
    assert "multiple definition of 'foo'" in readme
    assert "undefined entry symbol '_start'" in readme
    assert "`--entry`" in readme
    assert "`--base`" in readme
    assert "`_start`" in readme
    assert "byte-for-byte" in readme
    assert "not a compiler" in readme


def test_readme_documents_the_trap_abi(readme):
    assert TRAP_ABI_VERSION in readme
    assert "`ecode`" in readme
    assert "`MRET`" in readme
    assert "8-byte aligned" in readme
    assert "`sp`" in readme
    assert "repoint" in readme


def test_readme_has_no_developer_specific_paths(readme):
    assert "C:\\Users" not in readme
    assert "/home/" not in readme
    assert "/Users/" not in readme


def test_openwindows_directories_are_documented_as_runtime_paths(readme):
    assert "Documents/OpenWindows" in readme
    assert "NOVA_OPENWINDOWS_DIR" in readme
    assert "NOVA_OPENWINDOWS_ESSENTIALS_DIR" in readme
    assert "does not\ncreate directories" in readme or (
        "does not create directories" in readme.replace("\n", " ")
    )
