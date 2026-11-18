"""Nova ISA: architecture definition, assembler, disassembler, and emulator.

``nova.isa`` is the single source of truth for the Nova machine contract; the
tools and the reference emulator are generated from it.
"""

from .assembler import (
    AssemblyError,
    assemble_line,
    assemble_object,
    assemble_program,
    assemble_text,
    parse_source,
    preprocess_source,
)
from .disassembler import (
    DisassemblyError,
    disassemble_bytes,
    disassemble_stream,
    disassemble_word,
    disassemble_words,
)
from . import cabi
from .emulator import HANDLERS, NovaCluster, NovaMachine
from .nox import (
    DebugInfo,
    FrameDescription,
    RegisterSave,
    SourceFile,
    SourceLocation,
    decode_debug,
    encode_debug,
)
from . import nox, objfmt, syscalls
from .isa import (
    ALIASES,
    CSR_BY_NAME,
    CSR_TABLE,
    EXCEPTION_CODES,
    EXCEPTION_NAMES,
    FORMATS,
    INSTRUCTIONS_BY_OPCODE,
    INSTRUCTION_TABLE,
    INSTRUCTION_ALIGNMENT,
    INSTRUCTION_BYTES,
    INSTRUCTION_MASK,
    ISA_VERSION,
    NOP_OPCODE,
    OPCODE_RANGES,
    PRIVILEGE_LEVELS,
    REGISTERS,
    RESERVED_CSR_INDICES,
    WORD_MASK,
    DecodedInstruction,
    IllegalEncoding,
    InstructionDef,
    decode_instruction,
    decode_strict,
    format_word,
    instruction_table_markdown,
    csr_table_markdown,
    opcode_table_markdown,
    validate_table,
)
from .pseudo import (
    MAX_LI_LENGTH,
    PSEUDO_INSTRUCTIONS,
    expand_la,
    expand_li,
    expand_pseudo,
    is_pseudo,
    li_maximal,
)
from .nob import (
    NOB_HEADER_SIZE,
    NOB_MAGIC,
    NOB_VERSION,
    NobError,
    NobImage,
    decode_nob,
    encode_nob,
    load_nob,
    parse_address,
    write_nob,
)
from .object import (
    BINDING_BY_NAME,
    NOBJ_HEADER_SIZE,
    NOBJ_MAGIC,
    NOBJ_VERSION,
    ObjectError,
    ObjectFile,
    RELOCATION_KIND_BY_NAME,
    RELOCATION_SPAN_BYTES,
    Relocation,
    SECTION_FLAG_BITS,
    SECTION_KIND_BY_NAME,
    Section,
    Symbol,
    decode_object,
    encode_object,
    load_object,
    write_object,
)
from .link import (
    DEFAULT_ENTRY_SYMBOL,
    DEFAULT_IMAGE_BASE,
    LINKER_VERSION,
    LinkError,
    link_objects,
)
from .fileformats import (
    FILE_FORMATS,
    FILE_FORMAT_VERSION,
    FileFormat,
    get_file_format,
    openwindows_directories,
)

#: Version of the user-facing toolchain scripts (``nova-as``, ``nova-objdump``,
#: ``nova-run``). Keep it in step with ``pyproject.toml``; the CLI tests check.
TOOLCHAIN_VERSION = "1.1.0"

#: The documented software contract between Nova code and its runtime: the
#: procedure-calling convention, system-call convention, and exception-handler
#: conventions. Hardware state is architectural; the software conventions are
#: defined in the README's "Nova ABI v1.0" section.
TRAP_ABI_VERSION = "Nova ABI v1.0"

__all__ = [
    # tools
    "assemble_text",
    "preprocess_source",
    "assemble_program",
    "assemble_line",
    "assemble_object",
    "parse_source",
    "AssemblyError",
    "disassemble_bytes",
    "disassemble_word",
    "disassemble_words",
    "disassemble_stream",
    "DisassemblyError",
    "NovaMachine",
    "NovaCluster",
    "HANDLERS",
    # architecture
    "ISA_VERSION",
    "REGISTERS",
    "EXCEPTION_CODES",
    "EXCEPTION_NAMES",
    "INSTRUCTION_TABLE",
    "INSTRUCTIONS_BY_OPCODE",
    "INSTRUCTION_BYTES",
    "INSTRUCTION_ALIGNMENT",
    "INSTRUCTION_MASK",
    "NOP_OPCODE",
    "FORMATS",
    "OPCODE_RANGES",
    "PRIVILEGE_LEVELS",
    "ALIASES",
    "WORD_MASK",
    "InstructionDef",
    "DecodedInstruction",
    "decode_instruction",
    "decode_strict",
    "format_word",
    "IllegalEncoding",
    "validate_table",
    # control/status registers
    "CSR_TABLE",
    "CSR_BY_NAME",
    "RESERVED_CSR_INDICES",
    "instruction_table_markdown",
    "csr_table_markdown",
    "opcode_table_markdown",
    # object identities, syscalls, and conformance
    "objfmt",
    "syscalls",
    "cabi",
    "nox",
    "DebugInfo",
    "FrameDescription",
    "RegisterSave",
    "SourceFile",
    "SourceLocation",
    "decode_debug",
    "encode_debug",
    "CONFORMANCE_VERSION",
    "CONFORMANCE_VECTORS",
    "run_conformance_suite",
    "conformance_report",
    # file formats
    "FILE_FORMATS",
    "FILE_FORMAT_VERSION",
    "FileFormat",
    "get_file_format",
    "openwindows_directories",
    # toolchain
    "TOOLCHAIN_VERSION",
    "TRAP_ABI_VERSION",
    "PSEUDO_INSTRUCTIONS",
    "expand_pseudo",
    "expand_li",
    "expand_la",
    "is_pseudo",
    "MAX_LI_LENGTH",
    "li_maximal",
    "NOB_MAGIC",
    "NOB_VERSION",
    "NOB_HEADER_SIZE",
    "NobError",
    "NobImage",
    "encode_nob",
    "decode_nob",
    "load_nob",
    "write_nob",
    "parse_address",
    # relocatable object format
    "NOBJ_MAGIC",
    "NOBJ_VERSION",
    "NOBJ_HEADER_SIZE",
    "ObjectError",
    "ObjectFile",
    "Section",
    "Symbol",
    "Relocation",
    "BINDING_BY_NAME",
    "SECTION_KIND_BY_NAME",
    "SECTION_FLAG_BITS",
    "RELOCATION_KIND_BY_NAME",
    "RELOCATION_SPAN_BYTES",
    "encode_object",
    "decode_object",
    "load_object",
    "write_object",
    # linker
    "LINKER_VERSION",
    "DEFAULT_ENTRY_SYMBOL",
    "DEFAULT_IMAGE_BASE",
    "LinkError",
    "link_objects",
]

#: Conformance exports are resolved lazily so that ``python -m
#: nova.conformance`` executes a fresh module rather than reusing the copy
#: imported by this package initializer.
_CONFORMANCE_EXPORTS = frozenset({
    "CONFORMANCE_VECTORS",
    "CONFORMANCE_VERSION",
    "conformance_report",
    "run_conformance_suite",
})


def __getattr__(name: str):
    if name in _CONFORMANCE_EXPORTS:
        from . import conformance

        return getattr(conformance, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")