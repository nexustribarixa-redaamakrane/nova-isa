"""Authoritative definitions for the Nova instruction-set architecture.

This module is the single source of truth for the Nova machine contract:
architectural constants, register names, instruction field layouts, opcode
allocation, operand syntax, encoding constraints, privilege requirements,
control/status registers, and exception codes.

The assembler (:mod:`nova.assembler`), the disassembler
(:mod:`nova.disassembler`), and the reference emulator (:mod:`nova.emulator`)
all derive their behavior from the tables defined here. None of them keeps an
independent copy of an instruction definition or of the reserved-field rules.

The narrative specification lives in ``README.md``; this module is its
machine-readable form. Both are generated from the tables below wherever they
describe instructions or CSRs, and a test fails if the two drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple

ISA_VERSION = "Nova ISA v1.0"

# --------------------------------------------------------------------------
# Architectural constants
# --------------------------------------------------------------------------

# Nova has exactly one execution mode. These constants are the architectural
# answer to "what does 64-bit Nova mean": registers, addresses, pointers,
# object references, and CSRs are all 64 bits wide. The 32-bit instruction
# word is an encoding decision, not a narrower execution mode.
WORD_BITS = 64
WORD_MASK = (1 << WORD_BITS) - 1
ADDRESS_BITS = 64
PHYSICAL_ADDRESS_BITS = 52
VIRTUAL_ADDRESS_BITS = 48
PAGE_BITS = 12
PAGE_SIZE = 1 << PAGE_BITS
PTE_BITS = 64
PT_LEVELS = 4
PT_INDEX_BITS = 9
PT_ENTRIES = 1 << PT_INDEX_BITS
#: Valid page-table-base bits: the paging enable, and the root page number.
PTBR_MASK = (1 << 63) | (((1 << PHYSICAL_ADDRESS_BITS) - 1) & ~(PAGE_SIZE - 1))
EXECUTION_MODES: Tuple[str, ...] = ("Nova64",)

#: The normative 64-bit-only profile, published in the specification text.
PROFILE: Mapping[str, object] = MappingProxyType(
    {
        "execution_modes": EXECUTION_MODES,
        "word_bits": WORD_BITS,
        "address_bits": ADDRESS_BITS,
        "physical_address_bits": PHYSICAL_ADDRESS_BITS,
        "virtual_address_bits": VIRTUAL_ADDRESS_BITS,
        "gpr_bits": WORD_BITS,
        "csr_bits": WORD_BITS,
        "reference_bits": WORD_BITS,
        "instruction_bits": 32,
        "instruction_alignment": 4,
    }
)

GPR_COUNT = 32
CSR_COUNT = 32

INSTRUCTION_BITS = 32
INSTRUCTION_BYTES = INSTRUCTION_BITS // 8
INSTRUCTION_MASK = (1 << INSTRUCTION_BITS) - 1
INSTRUCTION_ALIGNMENT = 4

IMM14_BITS = 14
IMM14_MIN = -(1 << (IMM14_BITS - 1))
IMM14_MAX = (1 << (IMM14_BITS - 1)) - 1
UIMM14_MAX = (1 << IMM14_BITS) - 1
SHIFT_COUNT_MAX = 63

#: Data access sizes Nova defines. Loads and stores must be naturally aligned
#: to their access size.
ACCESS_SIZES: Tuple[int, ...] = (1, 2, 4, 8)

PRIVILEGE_LEVELS: Mapping[str, int] = MappingProxyType(
    {"user": 0, "supervisor": 1, "machine": 2}
)
PRIVILEGE_NAMES: Mapping[int, str] = MappingProxyType(
    {level: name for name, level in PRIVILEGE_LEVELS.items()}
)
PRIV_USER = PRIVILEGE_LEVELS["user"]
PRIV_SUPERVISOR = PRIVILEGE_LEVELS["supervisor"]
PRIV_MACHINE = PRIVILEGE_LEVELS["machine"]
PRIV_RESERVED = 3

RESET_ENTRY = 0x1000
RESET_VBAR = 0x1000

#: Depth of the exception save stack used by exception entry and MRET.
EXCEPTION_STACK_DEPTH = 4

#: Architectural register aliases. ``a0``-``a5`` carry procedure arguments
#: and ``a0`` also carries the return value; ``t0``-``t3`` are caller-saved
#: temporaries; ``s0``-``s3`` are callee-saved. These names are part of the
#: Nova ABI v1.0 but do not change the 32-register encoding.
REGISTERS: Mapping[str, int] = MappingProxyType(
    {
        **{f"r{index}": index for index in range(GPR_COUNT)},
        "zero": 0,
        "sp": 29,
        "fp": 30,
        "ra": 31,
        "a0": 1, "a1": 2, "a2": 3, "a3": 4, "a4": 5, "a5": 6,
        "t0": 7, "t1": 8, "t2": 9, "t3": 10,
        "s0": 11, "s1": 12, "s2": 13, "s3": 14,
        "gp": 28,
    }
)

REGISTER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "zero": "r0", "sp": "r29", "fp": "r30", "ra": "r31",
        "a0": "r1", "a1": "r2", "a2": "r3", "a3": "r4",
        "a4": "r5", "a5": "r6",
        "t0": "r7", "t1": "r8", "t2": "r9", "t3": "r10",
        "s0": "r11", "s1": "r12", "s2": "r13", "s3": "r14",
        "gp": "r28",
    }
)

#: Opcode assigned to the architecturally defined ``NOP``. Opcode ``0x00``
#: therefore *is* assigned: every field other than the opcode must encode
#: zero, so the all-zero word is the only valid NOP encoding.
NOP_OPCODE = 0x00


class IllegalEncoding(ValueError):
    """Raised when a 32-bit word is not a defined Nova instruction."""


# --------------------------------------------------------------------------
# Instruction field layouts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One named bit range of an instruction word, counted from bit 0."""

    name: str
    high: int
    low: int

    @property
    def width(self) -> int:
        return self.high - self.low + 1

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    def extract(self, word: int) -> int:
        return (word >> self.low) & self.mask

    def insert(self, word: int, value: int) -> int:
        if not 0 <= value <= self.mask:
            raise ValueError(
                f"Value {value:#x} does not fit in field {self.name}"
                f"[{self.high}:{self.low}]"
            )
        return word | (value << self.low)


#: Every Nova instruction is one little-endian 32-bit word. The high byte is
#: the opcode and uniquely identifies the instruction; there is no secondary
#: function field. Fields named ``rsv*`` must encode zero in every instruction.
FORMATS: Mapping[str, Tuple[Field, ...]] = MappingProxyType(
    {
        # Register arithmetic, logic, and comparison.
        "R": (
            Field("opcode", 31, 24),
            Field("rd", 23, 19),
            Field("rs1", 18, 14),
            Field("rs2", 13, 9),
            Field("rsv9", 8, 0),
        ),
        # Register/immediate arithmetic and logic.
        "I": (
            Field("opcode", 31, 24),
            Field("rd", 23, 19),
            Field("rs1", 18, 14),
            Field("imm14", 13, 0),
        ),
        # Loads and stores. ``rdata`` is the load destination or the store
        # source, ``rbase`` the address base register, ``disp14`` a signed
        # byte displacement.
        "M": (
            Field("opcode", 31, 24),
            Field("rdata", 23, 19),
            Field("rbase", 18, 14),
            Field("disp14", 13, 0),
        ),
        # Branches and PC-relative control transfer. ``rca``/``rcb`` are the
        # two compared registers, or the target register of JMP. ``disp14``
        # is a signed byte displacement relative to this instruction's own
        # address, or an unsigned immediate for TRAP.
        "J": (
            Field("opcode", 31, 24),
            Field("rca", 23, 19),
            Field("rcb", 18, 14),
            Field("disp14", 13, 0),
        ),
        # System instructions and NOP: no register fields.
        "S": (
            Field("opcode", 31, 24),
            Field("rsv10", 23, 14),
            Field("uimm14", 13, 0),
        ),
        # Object/reference operations: same layout as I, with no immediate.
        "O": (
            Field("opcode", 31, 24),
            Field("rd", 23, 19),
            Field("rs1", 18, 14),
            Field("rsv14", 13, 0),
        ),
        # Control/status register access: destination, CSR index, source.
        "X": (
            Field("opcode", 31, 24),
            Field("rd", 23, 19),
            Field("csr", 18, 14),
            Field("rs1", 13, 9),
            Field("rsv9", 8, 0),
        ),
        # Atomic read-modify-write operations. ``aq`` requests acquire
        # ordering and ``rl`` requests release ordering; both are 1-bit
        # unsigned operands. ``rs1`` is the base address and ``rs2`` the
        # operand value.
        "A": (
            Field("opcode", 31, 24),
            Field("rd", 23, 19),
            Field("rs1", 18, 14),
            Field("rs2", 13, 9),
            Field("aq", 8, 8),
            Field("rl", 7, 7),
            Field("rsv7", 6, 0),
        ),
    }
)

FORMAT_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "R": "opcode[31:24] rd[23:19] rs1[18:14] rs2[13:9] rsv9[8:0]",
        "I": "opcode[31:24] rd[23:19] rs1[18:14] imm14[13:0]",
        "M": "opcode[31:24] rdata[23:19] rbase[18:14] disp14[13:0]",
        "J": "opcode[31:24] rca[23:19] rcb[18:14] disp14[13:0]",
        "S": "opcode[31:24] rsv10[23:14] uimm14[13:0]",
        "O": "opcode[31:24] rd[23:19] rs1[18:14] rsv14[13:0]",
        "X": "opcode[31:24] rd[23:19] csr[18:14] rs1[13:9] rsv9[8:0]",
        "A": "opcode[31:24] rd[23:19] rs1[18:14] rs2[13:9] aq[8] rl[7] rsv7[6:0]",
    }
)


def format_description(format_name: str) -> str:
    """Return the bit layout of ``format_name``, most significant bit first."""
    try:
        return FORMAT_DESCRIPTIONS[format_name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"Unknown Nova instruction format: {format_name}") from exc


# --------------------------------------------------------------------------
# Operands
# --------------------------------------------------------------------------

#: Operand kinds understood by the encoder, the formatter, and the parser.
OPERAND_KINDS = ("reg", "simm", "uimm", "mem", "target", "csr")


@dataclass(frozen=True)
class Operand:
    """One architectural operand of an instruction.

    ``kind`` drives parsing, canonical formatting, and sign handling.
    ``fields`` names the instruction fields the operand occupies; a memory
    operand occupies two of them (base register plus displacement).
    """

    name: str
    kind: str
    fields: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in OPERAND_KINDS:
            raise ValueError(f"Unknown operand kind: {self.kind}")
        if not self.fields:
            raise ValueError(f"Operand {self.name} must name at least one field")

    @property
    def is_register(self) -> bool:
        return self.kind == "reg"

    @property
    def is_signed_immediate(self) -> bool:
        return self.kind == "simm"

    @property
    def is_unsigned_immediate(self) -> bool:
        return self.kind == "uimm"

    @property
    def is_memory(self) -> bool:
        return self.kind == "mem"

    @property
    def is_target(self) -> bool:
        return self.kind == "target"

    @property
    def is_csr(self) -> bool:
        return self.kind == "csr"

    @property
    def is_immediate(self) -> bool:
        return self.is_immediate_kind(self.kind)

    @staticmethod
    def is_immediate_kind(kind: str) -> bool:
        return kind in ("simm", "uimm")


_O_RD = Operand("rd", "reg", ("rd",))
_O_RS1 = Operand("rs1", "reg", ("rs1",))
_O_RS2 = Operand("rs2", "reg", ("rs2",))
_O_RDATA = Operand("rd", "reg", ("rdata",))
_O_RSRC = Operand("rs1", "reg", ("rs1",))
_O_RCA = Operand("reg_a", "reg", ("rca",))
_O_RCB = Operand("reg_b", "reg", ("rcb",))
_O_MEM = Operand("mem", "mem", ("rbase", "disp14"))
_O_TARGET = Operand("target", "target", ("disp14",))
_O_SIMM = Operand("simm", "simm", ("imm14",))
_O_UIMM_I = Operand("uimm", "uimm", ("imm14",))
_O_UIMM_S = Operand("uimm", "uimm", ("uimm14",))
_O_UIMM_J = Operand("uimm", "uimm", ("disp14",))
_O_CSR = Operand("csr", "csr", ("csr",))
_O_AQ = Operand("aq", "uimm", ("aq",))
_O_RL = Operand("rl", "uimm", ("rl",))

BRANCH_OPERANDS: Tuple[Operand, ...] = (_O_RCA, _O_RCB, _O_TARGET)


# --------------------------------------------------------------------------
# Instruction definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InstructionDef:
    """Authoritative definition of one Nova instruction.

    ``operands`` names the architectural operands in assembly order.
    ``fixed`` pins fields to a constant; ``disp14``-style unconditional-zero
    rules are derived automatically from the operand list, so an instruction
    never states a constraint twice.
    """

    mnemonic: str
    opcode: int
    format: str
    operands: Tuple[Operand, ...]
    fixed: Tuple[Tuple[str, int], ...] = ()
    privilege: str = "user"
    summary: str = ""
    flags: str = "-"
    exceptions: str = "-"
    operand_limits: Tuple[Tuple[str, Tuple[int, int]], ...] = ()

    # -- structure ----------------------------------------------------------

    @property
    def fields(self) -> Tuple[Field, ...]:
        return FORMATS[self.format]

    @property
    def field_names(self) -> Tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def operand_names(self) -> Tuple[str, ...]:
        return tuple(operand.name for operand in self.operands)

    @property
    def covered_fields(self) -> frozenset:
        """Fields that an operand or a constraint makes meaningful."""
        names = set()
        for operand in self.operands:
            names.update(operand.fields)
        names.update(name for name, _ in self.fixed)
        return frozenset(names)

    @property
    def fixed_fields(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self.fixed))

    @property
    def syntax(self) -> str:
        return ", ".join(self.operand_names) if self.operands else "no operands"

    @property
    def layout(self) -> str:
        return format_description(self.format)

    def operand(self, name: str) -> Operand:
        for operand in self.operands:
            if operand.name == name:
                return operand
        raise KeyError(f"{self.mnemonic} has no operand named {name!r}")

    def field_width(self, name: str) -> int:
        for field in self.fields:
            if field.name == name:
                return field.width
        raise KeyError(f"{self.format} has no field named {name!r}")

    def memory_operand(self) -> Optional[Operand]:
        for operand in self.operands:
            if operand.is_memory:
                return operand
        return None

    def csr_operand(self) -> Optional[Operand]:
        for operand in self.operands:
            if operand.is_csr:
                return operand
        return None

    def limits_for(self, name: str) -> Optional[Tuple[int, int]]:
        for operand_name, limits in self.operand_limits:
            if operand_name == name:
                return limits
        return None

    def limits(self, operand: Operand) -> Tuple[int, int]:
        """Inclusive assembler range for a register/memory/csr-free operand."""
        explicit = self.limits_for(operand.name)
        if explicit is not None:
            return explicit
        if operand.is_signed_immediate:
            return (IMM14_MIN, IMM14_MAX)
        if operand.is_unsigned_immediate:
            return (0, UIMM14_MAX)
        raise KeyError(f"Operand kind {operand.kind!r} has no numeric range")

    # -- encoding -----------------------------------------------------------

    def encode(self, values: Mapping[str, int]) -> int:
        """Encode this instruction from raw instruction-field values."""
        fields = {field.name: field for field in self.fields}
        supplied = dict(self.fixed)
        supplied.update(values)
        if "opcode" in values:
            raise ValueError("The opcode field is not settable")
        word = self.opcode << fields["opcode"].low
        for name, value in supplied.items():
            if name not in fields:
                raise ValueError(
                    f"{self.mnemonic} has no instruction field named {name!r}"
                )
            word = fields[name].insert(word, value)
        return word

    # -- decoding -----------------------------------------------------------

    def is_valid_encoding(self, word: int) -> bool:
        """True when ``word`` is a valid encoding of this instruction.

        A field is valid when it is an operand field, when it is pinned by a
        ``fixed`` constraint, or when it encodes zero. Any other value makes
        the entire word architecturally illegal, which the emulator reports as
        ILLEGAL_INSTRUCTION and the disassembler renders as data.
        """
        if isinstance(word, bool) or not isinstance(word, int):
            raise TypeError("Nova instruction words must be Python integers.")
        if not 0 <= word <= INSTRUCTION_MASK:
            raise ValueError("Nova instructions must be unsigned 32-bit words.")
        covered = self.covered_fields
        fixed = self.fixed_fields
        for field in self.fields:
            value = field.extract(word)
            if field.name == "opcode":
                if value != self.opcode:
                    return False
            elif field.name in fixed:
                if value != fixed[field.name]:
                    return False
            elif field.name not in covered and value != 0:
                return False
        return True

    # -- documentation and examples ----------------------------------------

    def example(self) -> str:
        """A canonical assembly line that exercises this instruction.

        The result is in the same canonical syntax the formatter emits, so it
        can be assembled and disassembled again without a special case.
        """
        return self.mnemonic + self.example_operands()

    def example_immediate(self, operand: Operand) -> int:
        """Example value for an immediate, clamped to its declared limits."""
        low, high = self.limits(operand)
        return min(max(12, low), high)

    def example_operands(self) -> str:
        """Canonical operand text for :meth:`example`, without the mnemonic."""
        registers = ("r1", "r2", "r3")
        tokens = []
        register_index = 0
        for operand in self.operands:
            if operand.is_register:
                tokens.append(registers[register_index % len(registers)])
                register_index += 1
            elif operand.is_immediate:
                tokens.append(str(self.example_immediate(operand)))
            elif operand.is_memory:
                tokens.append(f"[{registers[register_index % len(registers)]} + 8]")
                register_index += 1
            elif operand.is_target:
                tokens.append("8")
            elif operand.is_csr:
                tokens.append(self.example_csr())
            else:  # pragma: no cover - guarded by Operand validation
                raise ValueError(f"Cannot render operand kind {operand.kind!r}")
        if not tokens:
            return ""
        return " " + ", ".join(tokens)

    def example_fields(self) -> Dict[str, int]:
        """Instruction-field values implied by :meth:`example`."""
        registers = (1, 2, 3)
        values: Dict[str, int] = {}
        register_index = 0
        for operand in self.operands:
            if operand.is_register:
                values[operand.fields[0]] = registers[register_index % len(registers)]
                register_index += 1
            elif operand.is_immediate:
                values[operand.fields[0]] = self.example_immediate(operand)
            elif operand.is_memory:
                values[operand.fields[0]] = registers[register_index % len(registers)]
                values[operand.fields[1]] = 8
                register_index += 1
            elif operand.is_target:
                values[operand.fields[0]] = 8
            elif operand.is_csr:
                values[operand.fields[0]] = CSR_BY_NAME[self.example_csr()].index
        return values

    def example_csr(self) -> str:
        """CSR used by :meth:`example`; always user-readable in this revision."""
        for index in sorted(CSR_TABLE):
            csr = CSR_TABLE[index]
            if csr.min_read_privilege == "user":
                return csr.name
        raise KeyError("No user-readable CSR exists")  # pragma: no cover

    def summary_row(self) -> str:
        """One Markdown table row describing this instruction."""
        return (
            f"| `{self.mnemonic}` | `0x{self.opcode:02X}` | {self.format} | "
            f"`{self.syntax}` | {self.privilege} | {self.flags} | "
            f"{self.exceptions} | {self.summary} |"
        )


def _definition(mnemonic, opcode, format_name, operands, *, fixed=(), privilege="user",
                summary="", flags="-", exceptions="-", limits=()) -> InstructionDef:
    return InstructionDef(
        mnemonic, opcode, format_name, operands, tuple(fixed), privilege,
        summary, flags, exceptions, tuple(limits),
    )


def _r3(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "R", (_O_RD, _O_RS1, _O_RS2),
                       summary=summary, **kwargs)


def _r2(mnemonic, opcode, summary, **kwargs):
    """Two-operand R-format instruction; ``rs2`` must encode zero."""
    return _definition(mnemonic, opcode, "R", (_O_RD, _O_RS1),
                       fixed=(("rs2", 0),), summary=summary, **kwargs)


def _r2_flags(mnemonic, opcode, summary, **kwargs):
    """Two-operand R-format instruction that writes PSR flags and no register."""
    return _definition(mnemonic, opcode, "R", (_O_RS1, _O_RS2),
                       fixed=(("rd", 0),), summary=summary, flags="ZNCV", **kwargs)


def _i3(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "I", (_O_RD, _O_RS1, _O_SIMM),
                       summary=summary, **kwargs)


def _i2(mnemonic, opcode, summary, **kwargs):
    """Two-operand I-format instruction; ``rs1`` must encode zero."""
    return _definition(mnemonic, opcode, "I", (_O_RD, _O_UIMM_I),
                       fixed=(("rs1", 0),), summary=summary, **kwargs)


def _i2_flags(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "I", (_O_RS1, _O_SIMM),
                       fixed=(("rd", 0),), summary=summary, flags="ZNCV", **kwargs)


def _mem(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "M", (_O_RDATA, _O_MEM),
                       summary=summary, **kwargs)


def _branch(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "J", BRANCH_OPERANDS,
                       summary=summary, **kwargs)


def _jmp(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "J", (_O_RCA,),
                       fixed=(("rcb", 0), ("disp14", 0)), summary=summary, **kwargs)


def _call(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "J", (_O_TARGET,),
                       fixed=(("rca", 0), ("rcb", 0)), summary=summary, **kwargs)


def _ret(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "J", (),
                       fixed=(("rca", 0), ("rcb", 0), ("disp14", 0)),
                       summary=summary, **kwargs)


def _trap(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "J", (_O_UIMM_J,),
                       fixed=(("rca", 0), ("rcb", 0)), summary=summary, **kwargs)


def _sys(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "S", (), summary=summary, **kwargs)


def _sys_imm(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "S", (_O_UIMM_S,), summary=summary, **kwargs)


def _sys_privileged(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "S", (), privilege="machine",
                       summary=summary, **kwargs)


def _obj(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "O", (_O_RD, _O_RS1),
                       summary=summary, **kwargs)


def _obj_drop(mnemonic, opcode, summary, **kwargs):
    return _definition(mnemonic, opcode, "O", (_O_RD,), fixed=(("rs1", 0),),
                       summary=summary, **kwargs)


def _amo(mnemonic, opcode, summary, **kwargs):
    """Atomic memory operation: ``rd, rs1, rs2, aq, rl``.

    ``rs1`` holds the 8-byte-aligned virtual address, ``rs2`` the operand,
    and ``rd`` receives the memory value observed before the operation.
    """
    return _definition(
        mnemonic, opcode, "A", (_O_RD, _O_RS1, _O_RS2, _O_AQ, _O_RL),
        summary=summary,
        exceptions=kwargs.pop("exceptions", "MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT"),
        limits=(("aq", (0, 1)), ("rl", (0, 1))),
        **kwargs,
    )


_INSTRUCTIONS: Tuple[InstructionDef, ...] = (
    # -- opcode 0x00: NOP ---------------------------------------------------
    _sys("NOP", NOP_OPCODE,
         "No operation; `PC` advances by four. The all-zero word is the only "
         "valid NOP encoding."),
    # -- R format: rotates and unsigned division (Nova v1.0) ----------------
    _r3("ROTL", 0x01, "`rd = rotl64(rs1, rs2[5:0])`; rotate left."),
    _r3("ROTR", 0x02, "`rd = rotr64(rs1, rs2[5:0])`; rotate right."),
    _r3("UDIV", 0x03,
        "Unsigned `rd = rs1 / rs2`; divide by zero raises ARITHMETIC.",
        exceptions="ARITHMETIC"),
    _r3("UREM", 0x04,
        "Unsigned `rd = rs1 % rs2`; divide by zero raises ARITHMETIC.",
        exceptions="ARITHMETIC"),
    # -- A format: atomic memory operations (Nova v1.0) ----------------------
    _amo("AMOADD", 0x05,
         "Atomic `mem64[rs1] += rs2`; `rd` is the old value. Alignment 8, "
         "aq/rl order the operation."),
    _amo("AMOSWAP", 0x06,
         "Atomic `mem64[rs1] = rs2`; `rd` is the old value."),
    _amo("AMOCAS", 0x07,
         "Atomic compare-and-swap: if `mem64[rs1] == rd` write `rs2`; "
         "`rd` always receives the observed old value."),
    _amo("AMOAND", 0x08,
         "Atomic `mem64[rs1] &= rs2`; `rd` is the old value."),
    _amo("AMOOR", 0x09,
         "Atomic `mem64[rs1] |= rs2`; `rd` is the old value."),
    _amo("AMOXOR", 0x0A,
         "Atomic `mem64[rs1] ^= rs2`; `rd` is the old value."),
    # -- R format: integer, logic, comparison -------------------------------
    _r3("ADD", 0x10, "`rd = rs1 + rs2`, low 64 bits of the sum."),
    _r3("SUB", 0x11, "`rd = rs1 - rs2`, low 64 bits of the difference."),
    _r3("MUL", 0x12, "`rd = rs1 * rs2`, low 64 bits of the product."),
    _r3("DIV", 0x13,
        "Signed `rd = trunc(rs1 / rs2)`; `MIN / -1` wraps to `MIN`.",
        exceptions="ARITHMETIC"),
    _r3("REM", 0x14,
        "Signed `rd = rs1 - trunc(rs1 / rs2) * rs2`, sign of the dividend.",
        exceptions="ARITHMETIC"),
    _r3("AND", 0x15, "`rd = rs1 & rs2`."),
    _r3("OR", 0x16, "`rd = rs1 | rs2`."),
    _r3("XOR", 0x17, "`rd = rs1 ^ rs2`."),
    _r3("SHL", 0x18, "`rd = rs1 << rs2[5:0]`; zeros shifted in."),
    _r3("SHR", 0x19, "Logical `rd = rs1 >> rs2[5:0]`; zeros shifted in."),
    _r3("SAR", 0x1A, "Arithmetic `rd = rs1 >> rs2[5:0]`; sign bit replicated."),
    _r2_flags("CMP", 0x1B, "Set `Z N C V` from `rs1 - rs2`; no register written."),
    _r2("MOV", 0x1C, "`rd = rs1`."),
    _r2("NOT", 0x1D, "`rd = ~rs1`."),
    _r2("NEG", 0x1E, "`rd = -rs1`, two's complement."),
    # -- I format: immediate arithmetic and logic ----------------------------
    _i3("ADDI", 0x20, "`rd = rs1 + sext(simm)`."),
    _i3("SUBI", 0x21, "`rd = rs1 - sext(simm)`."),
    _i3("MULI", 0x22, "`rd = rs1 * sext(simm)`."),
    _i3("ANDI", 0x23, "`rd = rs1 & sext(simm)`."),
    _i3("ORI", 0x24, "`rd = rs1 | sext(simm)`."),
    _i3("XORI", 0x25, "`rd = rs1 ^ sext(simm)`."),
    _i3("SHLI", 0x26, "`rd = rs1 << simm[5:0]`; assembler requires 0..63.",
        limits=(("simm", (0, SHIFT_COUNT_MAX)),)),
    _i3("SHRI", 0x27, "Logical `rd = rs1 >> simm[5:0]`; requires 0..63.",
        limits=(("simm", (0, SHIFT_COUNT_MAX)),)),
    _i3("SARI", 0x28, "Arithmetic `rd = rs1 >> simm[5:0]`; requires 0..63.",
        limits=(("simm", (0, SHIFT_COUNT_MAX)),)),
    _i2_flags("CMPI", 0x29,
              "Set `Z N C V` from `rs1 - sext(simm)`; no register written."),
    _i2("MOVI", 0x2A,
        "`rd = zext(uimm)` for uimm in 0..16383. Cannot load a full 64-bit "
        "constant."),
    # -- I format: rotate immediates and TLB invalidation (Nova v1.0) --------
    _i3("ROTLI", 0x2B, "`rd = rotl64(rs1, simm[5:0])`; requires 0..63.",
        limits=(("simm", (0, SHIFT_COUNT_MAX)),)),
    _i3("ROTRI", 0x2C, "`rd = rotr64(rs1, simm[5:0])`; requires 0..63.",
        limits=(("simm", (0, SHIFT_COUNT_MAX)),)),
    _definition("TLBI", 0x2D, "I", (_O_RS1,),
                fixed=(("rd", 0), ("imm14", 0)), privilege="machine",
                summary="Invalidate translations: `rs1 = 0` flushes every "
                        "entry, otherwise only the entry for virtual address "
                        "`rs1` is flushed."),
    # -- M format: loads and stores -----------------------------------------
    _mem("LDB", 0x30, "`rd = sext(mem8[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDH", 0x31, "`rd = sext(mem16[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDW", 0x32, "`rd = sext(mem32[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDD", 0x33, "`rd = mem64[rbase + sext(disp14)]`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDBU", 0x34, "`rd = zext(mem8[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDHU", 0x35, "`rd = zext(mem16[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("LDWU", 0x36, "`rd = zext(mem32[rbase + sext(disp14)])`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("STB", 0x40, "`mem8[rbase + sext(disp14)] = rd[7:0]`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("STH", 0x41, "`mem16[rbase + sext(disp14)] = rd[15:0]`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("STW", 0x42, "`mem32[rbase + sext(disp14)] = rd[31:0]`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    _mem("STD", 0x43, "`mem64[rbase + sext(disp14)] = rd`.",
         exceptions="MEMORY_FAULT, ALIGNMENT_FAULT"),
    # -- J format: branches and control transfer ----------------------------
    _jmp("JMP", 0x50, "`PC = reg_a`.",
         exceptions="ALIGNMENT_FAULT, MEMORY_FAULT"),
    _branch("BEQ", 0x51, "Branch if `reg_a == reg_b`.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BNE", 0x52, "Branch if `reg_a != reg_b`.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BLT", 0x53, "Branch if `reg_a < reg_b`, signed.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BLE", 0x54, "Branch if `reg_a <= reg_b`, signed.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BGT", 0x55, "Branch if `reg_a > reg_b`, signed.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BGE", 0x56, "Branch if `reg_a >= reg_b`, signed.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BLTU", 0x57, "Branch if `reg_a < reg_b`, unsigned.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BLEU", 0x58, "Branch if `reg_a <= reg_b`, unsigned.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BGTU", 0x59, "Branch if `reg_a > reg_b`, unsigned.",
            exceptions="ALIGNMENT_FAULT"),
    _branch("BGEU", 0x5A, "Branch if `reg_a >= reg_b`, unsigned.",
            exceptions="ALIGNMENT_FAULT"),
    _call("CALL", 0x5B, "`ra = PC + 4`; `PC = PC + sext(disp14)`.",
          exceptions="ALIGNMENT_FAULT"),
    _ret("RET", 0x5C, "`PC = ra`.",
         exceptions="ALIGNMENT_FAULT"),
    _trap("TRAP", 0x5D,
          "Raise SOFTWARE_TRAP with `ecause = zext(disp14)` and `epc = PC + 4`."),
    # -- S format: system and privileged control ---------------------------
    _sys_imm("SCALL", 0x60,
             "Raise SYSTEM_CALL with `ecause = uimm` and `epc = PC + 4`."),
    _sys_privileged(
        "MRET", 0x61,
        "Pop one exception frame: `PC = epc`, `priv = frame.priv`, `ecode = 0`, "
        "`ecause` unchanged.",
        exceptions="ALIGNMENT_FAULT, ILLEGAL_INSTRUCTION",
    ),
    _sys("FENCE", 0x62,
         "Full memory barrier: every load, store, and atomic before FENCE is "
         "ordered before every one after it, on every hart. Nova v1.0 is "
         "sequentially consistent for ordinary memory; FENCE is the explicit "
         "synchronization point for conforming relaxed implementations."),
    _sys_privileged(
        "WAIT", 0x63,
         "Wait for an enabled, pending interrupt: retire normally when one is "
         "pending, otherwise stop execution until a host or hart raises one.",
    ),
    _sys_privileged(
        "HALT", 0x64,
        "Stop architectural execution; no further architectural activity "
        "occurs without explicit host restart.",
    ),
    # -- S format: interrupts, virtualization (Nova v1.0) --------------------
    _sys_privileged(
        "IRET", 0x65,
        "Return from interrupt: pop one exception frame and restore `pc`, "
        "`priv`, and `ecode` exactly as MRET does.",
        exceptions="ALIGNMENT_FAULT, ILLEGAL_INSTRUCTION",
    ),
    _sys_imm(
        "SIPI", 0x66,
        "Send a software interrupt to the hart identified by `uimm`; the "
        "target's `ip` software bit becomes pending.",
        privilege="machine",
        exceptions="PRIVILEGE",
    ),
    _sys_privileged(
        "VMLAUNCH", 0x67,
        "Enter guest execution from `vpc`/`vpriv`/`vptbr`, saving host state; "
        "guest traps cause a VM exit.",
        exceptions="PRIVILEGE, VM_FAULT",
    ),
    _sys_privileged(
        "VMRESUME", 0x68,
        "Resume guest execution after a VM exit using the saved host state.",
        exceptions="PRIVILEGE, VM_FAULT",
    ),
    _sys_imm(
        "VMEXIT", 0x69,
        "Return from guest to host with `ecause = uimm`; restore host `pc`, "
        "`priv`, and page table base.",
        privilege="machine",
        exceptions="PRIVILEGE, VM_FAULT",
    ),
    # -- O format: capability-based object/reference operations -------------
    _obj("OBJ.CREATE", 0x70,
         "Create an object whose prototype metadata size is `rs1`; "
         "`rd = new reference`."),
    _obj("OBJ.GET", 0x71, "`rd = metadata size of the object referenced by rs1`.",
         exceptions="OBJECT_FAULT"),
    _obj("OBJ.SET", 0x72,
         "Set the metadata size of the object referenced by `rs1` to `rd`.",
         exceptions="OBJECT_FAULT"),
    _obj_drop("OBJ.DROP", 0x73,
              "Destroy the object referenced by `rd`; the reference becomes "
              "invalid.",
              exceptions="OBJECT_FAULT"),
    # -- O/R format: finalized capability-based objects (Nova v1.0) ---------
    _obj("OBJ.PERM", 0x74,
         "`rd = permission mask of the object referenced by rs1`; the mask "
         "uses the OBJECT_PERM_* bits.",
         exceptions="OBJECT_FAULT"),
    _r3("OBJ.CMP", 0x75,
        "`rd = 1` when `rs1` and `rs2` name the same live object, else `0`; "
        "invalid references do not fault."),
    # -- X format: control/status register access ---------------------------
    _definition("CSRR", 0x80, "X", (_O_RD, _O_CSR), fixed=(("rs1", 0),),
                summary="`rd = CSR[csr]`. Reserved CSR indices read as zero.",
                exceptions="PRIVILEGE"),
    _definition("CSRW", 0x81, "X", (_O_CSR, _O_RSRC), fixed=(("rd", 0),),
                summary="`CSR[csr] = rs1`. Reserved bits are discarded (WARL); "
                        "misaligned EPC/VBAR writes and reserved privilege "
                        "values fault.",
                exceptions="PRIVILEGE, ALIGNMENT_FAULT"),
)

INSTRUCTION_TABLE: Mapping[str, InstructionDef] = MappingProxyType(
    {definition.mnemonic: definition for definition in _INSTRUCTIONS}
)

INSTRUCTIONS_BY_OPCODE: Mapping[int, InstructionDef] = MappingProxyType(
    {definition.opcode: definition for definition in _INSTRUCTIONS}
)

ALIASES: Mapping[str, str] = MappingProxyType(
    {name: name for name in INSTRUCTION_TABLE}
)

#: Opcode allocation map, published in the specification text.
OPCODE_RANGES: Tuple[Tuple[int, int, str], ...] = (
    (0x00, 0x00, "`NOP`"),
    (0x01, 0x04, "R format: rotates and unsigned division"),
    (0x05, 0x0A, "A format: atomic memory operations"),
    (0x0B, 0x0F, "reserved"),
    (0x10, 0x1E, "R format: integer, logic, comparison"),
    (0x1F, 0x1F, "reserved"),
    (0x20, 0x2A, "I format: immediate arithmetic and logic"),
    (0x2B, 0x2C, "I format: rotate immediates"),
    (0x2D, 0x2D, "I format: TLB invalidation"),
    (0x2E, 0x2F, "reserved"),
    (0x30, 0x36, "M format: loads"),
    (0x37, 0x3F, "reserved"),
    (0x40, 0x43, "M format: stores"),
    (0x44, 0x4F, "reserved"),
    (0x50, 0x5D, "J format: branches and control transfer"),
    (0x5E, 0x5F, "reserved"),
    (0x60, 0x69, "S format: system, interrupts, virtualization"),
    (0x6A, 0x6F, "reserved"),
    (0x70, 0x75, "O/R format: capability-based object/reference operations"),
    (0x76, 0x7F, "reserved"),
    (0x80, 0x81, "X format: control/status register access"),
    (0x82, 0xFF, "reserved"),
)


def opcode_is_reserved(opcode: int) -> bool:
    """True when ``opcode`` is unassigned in this ISA revision."""
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"Opcode out of range: {opcode}")
    return opcode not in INSTRUCTIONS_BY_OPCODE


# --------------------------------------------------------------------------
# Exception codes
# --------------------------------------------------------------------------

EXCEPTION_CODES: Mapping[str, int] = MappingProxyType(
    {
        "NONE": 0,
        "ILLEGAL_INSTRUCTION": 1,
        "ARITHMETIC": 2,
        "MEMORY_FAULT": 3,
        "ALIGNMENT_FAULT": 4,
        "PRIVILEGE": 5,
        "OBJECT_FAULT": 6,
        "SYSTEM_CALL": 7,
        "INTERRUPT": 8,
        "MMU_FAULT": 9,
        "SOFTWARE_TRAP": 10,
        "VM_FAULT": 11,
    }
)

EXCEPTION_NAMES: Mapping[int, str] = MappingProxyType(
    {code: name for name, code in EXCEPTION_CODES.items()}
)

#: ``ecode`` values allocated by this revision. Every allocated code has an
#: architectural mechanism in Nova v1.0.
RESERVED_EXCEPTION_CODES: Tuple[int, ...] = tuple(
    code for code in range(1, 64) if code not in EXCEPTION_NAMES
)

#: ``ecause`` detail values raised with ARITHMETIC.
ARITHMETIC_DETAIL_DIVIDE = 0
ARITHMETIC_DETAIL_REMAINDER = 1

#: ``ecause`` detail values raised with MMU_FAULT. The faulting virtual address
#: is always saved in the ``tval`` CSR.
MMU_DETAIL_MISSING = 0
MMU_DETAIL_PERMISSION = 1
MMU_DETAIL_RESERVED_PTE = 2
MMU_DETAIL_NONCANONICAL = 3
MMU_DETAIL_MISALIGNED_PTE = 4

#: Page sizes supported by the Nova v1.0 translator. A leaf PTE at level 3
#: maps 4 KiB, at level 2 maps 2 MiB, and at level 1 maps 1 GiB. Level 0
#: leaves are reserved because they would map 1 TiB.
PAGE_SIZE_4K = PAGE_SIZE
PAGE_SIZE_2M = 1 << 21
PAGE_SIZE_1G = 1 << 30
PAGE_SIZES: Tuple[int, ...] = (PAGE_SIZE_4K, PAGE_SIZE_2M, PAGE_SIZE_1G)
#: Physical page number field of a PTE: bits 51:12.
PTE_PPN_MASK = ((1 << PHYSICAL_ADDRESS_BITS) - 1) & ~(PAGE_SIZE - 1)

#: ``ecause`` detail values raised with OBJECT_FAULT.
OBJECT_DETAIL_NULL = 0
OBJECT_DETAIL_INVALID = 1
OBJECT_DETAIL_PERMISSION = 2

#: Object permission bits, reported by OBJ.PERM.
OBJECT_PERM_READ = 1 << 0
OBJECT_PERM_WRITE = 1 << 1
OBJECT_PERM_EXEC = 1 << 2
OBJECT_PERM_GRANT = 1 << 3
OBJECT_PERM_DROP = 1 << 4
OBJECT_PERM_META = 1 << 5

#: Interrupt sources. These are the bit positions of ``ie`` and ``ip`` and the
#: low bits of ``ecause`` for an INTERRUPT exception, in delivery priority.
INT_SOFTWARE = 0
INT_TIMER = 1
INT_EXTERNAL = 2
INTERRUPT_SOURCES: Tuple[int, ...] = (INT_SOFTWARE, INT_TIMER, INT_EXTERNAL)
INTERRUPT_IE_MASK = sum(1 << source for source in INTERRUPT_SOURCES)

#: ``ecause`` detail values for VM_FAULT / VM exit reasons.
VM_EXIT_TRAP = 0
VM_EXIT_EXPLICIT = 1
VM_EXIT_ILLEGAL = 2

# --------------------------------------------------------------------------
# Object reference representation and persistent object format
# --------------------------------------------------------------------------

#: A reference is one opaque 64-bit value: generation in the high half and a
#: stable object index in the low half. Both halves start at 1; zero is null.
OBJECT_INDEX_BITS = 32
OBJECT_GENERATION_BITS = 32
OBJECT_INDEX_MASK = (1 << OBJECT_INDEX_BITS) - 1
OBJECT_GENERATION_MASK = (1 << OBJECT_GENERATION_BITS) - 1
OBJECT_NULL = 0

#: The external object container: magic, one-byte format version, flags and a
#: record count in a 16-byte header, 32-byte records, and an 8-byte trailer
#: with a CRC-32 of everything before it. Records are sorted by reference.
OBJECT_MAGIC = b"NOBO"
OBJECT_FORMAT_VERSION = 1
OBJECT_HEADER_SIZE = 16
OBJECT_RECORD_SIZE = 32
OBJECT_TRAILER_SIZE = 8
OBJECT_PERM_MASK = (
    OBJECT_PERM_READ | OBJECT_PERM_WRITE | OBJECT_PERM_EXEC
    | OBJECT_PERM_GRANT | OBJECT_PERM_DROP | OBJECT_PERM_META
)
#: A record with this flag is an unresolved external reference. It must
#: already exist in the importing machine and is not created again.
OBJECT_FLAG_EXTERNAL = 1 << 0


class ObjectFormatError(ValueError):
    """Raised when an external object container or reference is invalid."""


def make_object_reference(generation: int, index: int) -> int:
    """Compose a stable 64-bit object reference."""
    if not 1 <= generation <= OBJECT_GENERATION_MASK:
        raise ObjectFormatError(f"Object generation out of range: {generation}")
    if not 1 <= index <= OBJECT_INDEX_MASK:
        raise ObjectFormatError(f"Object index out of range: {index}")
    return (generation << OBJECT_INDEX_BITS) | index


def split_object_reference(reference: int) -> Tuple[int, int]:
    """Split a reference into ``(generation, index)``; validate its encoding."""
    if not 1 <= reference <= WORD_MASK:
        raise ObjectFormatError(f"Invalid object reference: {reference:#x}")
    generation = (reference >> OBJECT_INDEX_BITS) & OBJECT_GENERATION_MASK
    index = reference & OBJECT_INDEX_MASK
    if generation == 0 or index == 0:
        raise ObjectFormatError(f"Malformed object reference: {reference:#x}")
    return generation, index


# --------------------------------------------------------------------------
# Control/status registers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CsrDef:
    """Definition of one control/status register."""

    index: int
    name: str
    description: str
    reset: int = 0
    mask: int = WORD_MASK
    readable: bool = True
    writable: bool = True
    min_read_privilege: str = "machine"
    min_write_privilege: str = "machine"
    align4: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.index < CSR_COUNT:
            raise ValueError(f"CSR index out of range: {self.index}")
        for level in (self.min_read_privilege, self.min_write_privilege):
            if level not in PRIVILEGE_LEVELS:
                raise ValueError(f"Unknown privilege level: {level}")
        if self.reset & ~self.mask:
            raise ValueError(f"CSR {self.name} reset value does not fit its mask")
        if self.mask & ~WORD_MASK:
            raise ValueError(f"CSR {self.name} mask exceeds the register width")

    @property
    def width(self) -> int:
        return self.mask.bit_length()

    @property
    def access(self) -> str:
        read = f"{self.min_read_privilege} read"
        if not self.writable:
            return f"{read}, read-only"
        return f"{read}, {self.min_write_privilege} write"

    @property
    def alignment_rule(self) -> str:
        return "4-byte aligned writes" if self.align4 else "any value"

    def summary_row(self) -> str:
        return (
            f"| `{self.index}` | `{self.name}` | {self.width} | "
            f"`{self.reset:#x}` | {self.access} | {self.alignment_rule} | "
            f"{self.description} |"
        )


_CSR_DEFINITIONS: Tuple[CsrDef, ...] = (
    CsrDef(
        index=0,
        name="psr",
        description="Processor status. `Z`=bit 0, `N`=bit 1, `C`=bit 2, "
                    "`V`=bit 3, written by `CMP` and `CMPI`. Bits 63:4 are "
                    "reserved: they read as zero and discard writes.",
        reset=0,
        mask=0xF,
        min_read_privilege="user",
        min_write_privilege="user",
    ),
    CsrDef(
        index=1,
        name="epc",
        description="Saved program counter of the active exception; the "
                    "address that `MRET` returns to.",
        min_read_privilege="machine",
        min_write_privilege="machine",
        align4=True,
    ),
    CsrDef(
        index=2,
        name="ecause",
        description="Exception detail: faulting address, immediate, CSR index, "
                    "or offending privilege value depending on `ecode`.",
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=3,
        name="ecode",
        description="Active exception code. `NONE` means no exception is "
                    "active. `MRET` sets it to `NONE`.",
        reset=EXCEPTION_CODES["NONE"],
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=4,
        name="priv",
        description="Current privilege level: 0 user, 1 supervisor, 2 machine. "
                    "Level 3 is reserved; writing it raises PRIVILEGE.",
        reset=0,
        mask=0x3,
        min_read_privilege="user",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=5,
        name="vbar",
        description="Exception vector base address used by exception entry.",
        reset=RESET_VBAR,
        min_read_privilege="machine",
        min_write_privilege="machine",
        align4=True,
    ),
    CsrDef(
        index=6,
        name="coreid",
        description="Architectural identity of the executing processor. The "
                    "reference model is single-processor, so it reads 0; a "
                    "Nova cluster assigns one unique id per hart.",
        writable=False,
        min_read_privilege="user",
    ),
    # -- Virtual memory (Nova v1.0) -----------------------------------------
    CsrDef(
        index=7,
        name="ptbr",
        description="Page-table base and paging mode. Bit 63 enables the "
                    "four-level translator; bits 51:12 hold the root page "
                    "number. Reset 0 selects bare mode (virtual == physical). "
                    "Writing it flushes every TLB entry.",
        reset=0,
        mask=PTBR_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=8,
        name="tval",
        description="Trap value: the faulting virtual address for MMU_FAULT.",
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    # -- Interrupts (Nova v1.0) ---------------------------------------------
    CsrDef(
        index=9,
        name="ie",
        description="Interrupt enable mask. Bit 0 software, bit 1 timer, "
                    "bit 2 external. Higher bits are reserved (WARL).",
        reset=0,
        mask=INTERRUPT_IE_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=10,
        name="ip",
        description="Interrupt pending mask, same bit positions as `ie`. "
                    "Set by SIPI, the timer, the host, or an external source.",
        reset=0,
        mask=INTERRUPT_IE_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=11,
        name="ivec",
        description="Interrupt vector base address used on interrupt entry.",
        reset=0x2000,
        min_read_privilege="machine",
        min_write_privilege="machine",
        align4=True,
    ),
    CsrDef(
        index=12,
        name="time",
        description="Monotonic 64-bit time counter; incremented once per "
                    "retired instruction by the reference model.",
        reset=0,
        writable=False,
        min_read_privilege="user",
    ),
    CsrDef(
        index=13,
        name="timecmp",
        description="Timer compare value. When `time` reaches `timecmp`, the "
                    "timer interrupt pending bit is set. All ones disables it.",
        reset=WORD_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    # -- Virtualization, extension V (Nova v1.0) -----------------------------
    CsrDef(
        index=14,
        name="vpc",
        description="Guest program counter used by VMLAUNCH and VMRESUME.",
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=15,
        name="vpriv",
        description="Guest privilege level (0 user, 1 supervisor, 2 machine) "
                    "entered by VMLAUNCH.",
        reset=0,
        mask=0x3,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=16,
        name="vptbr",
        description="Guest page-table base, in the same format as `ptbr`.",
        mask=PTBR_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=17,
        name="vexit",
        description="VM exit reason, written on every guest exit.",
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=18,
        name="hstatus",
        description="Hypervisor status. Bit 0 is set while guest execution is "
                    "active; other bits are reserved (WARL).",
        reset=0,
        mask=0x1,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
    CsrDef(
        index=19,
        name="s2ptbr",
        description="Stage-2 (guest-physical to host-physical) page-table base. "
                    "Same format as `ptbr`: bit 63 enables stage-2 translation "
                    "and bits 51:12 hold the root page number. Reset 0 disables "
                    "stage 2, preserving the flat guest behavior. Writing it "
                    "flushes every TLB entry.",
        reset=0,
        mask=PTBR_MASK,
        min_read_privilege="machine",
        min_write_privilege="machine",
    ),
)

CSR_TABLE: Mapping[int, CsrDef] = MappingProxyType(
    {definition.index: definition for definition in _CSR_DEFINITIONS}
)

CSR_BY_NAME: Mapping[str, CsrDef] = MappingProxyType(
    {definition.name: definition for definition in _CSR_DEFINITIONS}
)

#: CSR indices with no architectural meaning in this revision. They are WARL:
#: reads return zero and writes are discarded, so a later revision can assign
#: an index without changing the meaning of existing instruction streams.
RESERVED_CSR_INDICES: Tuple[int, ...] = tuple(
    index for index in range(CSR_COUNT) if index not in CSR_TABLE
)

PSR = CSR_BY_NAME["psr"].index
EPC = CSR_BY_NAME["epc"].index
ECAUSE = CSR_BY_NAME["ecause"].index
ECODE = CSR_BY_NAME["ecode"].index
PRIV = CSR_BY_NAME["priv"].index
VBAR = CSR_BY_NAME["vbar"].index
COREID = CSR_BY_NAME["coreid"].index
PTBR = CSR_BY_NAME["ptbr"].index
TVAL = CSR_BY_NAME["tval"].index
IE = CSR_BY_NAME["ie"].index
IP = CSR_BY_NAME["ip"].index
IVEC = CSR_BY_NAME["ivec"].index
TIME = CSR_BY_NAME["time"].index
TIMECMP = CSR_BY_NAME["timecmp"].index
VPC = CSR_BY_NAME["vpc"].index
VPRIV = CSR_BY_NAME["vpriv"].index
VPTBR = CSR_BY_NAME["vptbr"].index
VEXIT = CSR_BY_NAME["vexit"].index
HSTATUS = CSR_BY_NAME["hstatus"].index
S2PTBR = CSR_BY_NAME["s2ptbr"].index

#: PSR flag bit positions.
FLAG_ZERO = 0
FLAG_NEGATIVE = 1
FLAG_CARRY = 2
FLAG_OVERFLOW = 3
PSR_FLAG_MASK = CSR_BY_NAME["psr"].mask


# --------------------------------------------------------------------------
# Bit helpers
# --------------------------------------------------------------------------


def sign_extend(value: int, width: int) -> int:
    sign_bit = 1 << (width - 1)
    return (value ^ sign_bit) - sign_bit


def zero_extend(value: int, width: int) -> int:
    return value & ((1 << width) - 1)


def to_u64(value: int) -> int:
    return value & WORD_MASK


def to_signed64(value: int) -> int:
    """Interpret a 64-bit register value as a signed integer."""
    value = to_u64(value)
    return value if value < (1 << 63) else value - (1 << WORD_BITS)


def imm14_to_i(value: int) -> int:
    """Interpret a 14-bit immediate field as a signed value."""
    return sign_extend(value, IMM14_BITS)


# --------------------------------------------------------------------------
# Encoding, decoding, and canonical formatting
# --------------------------------------------------------------------------


def extract_fields(format_name: str, word: int) -> Dict[str, int]:
    """Split an instruction word into its named fields."""
    try:
        fields = FORMATS[format_name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"Unknown Nova instruction format: {format_name}") from exc
    if isinstance(word, bool) or not isinstance(word, int):
        raise TypeError("Nova instruction words must be Python integers.")
    if not 0 <= word <= INSTRUCTION_MASK:
        raise ValueError("Nova instructions must be unsigned 32-bit words.")
    return {field.name: field.extract(word) for field in fields}


def build_word(format_name: str, values: Mapping[str, int]) -> int:
    """Assemble a word from raw field values, validating every range."""
    try:
        fields = {field.name: field for field in FORMATS[format_name]}
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"Unknown Nova instruction format: {format_name}") from exc
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(
            f"Format {format_name} has no field(s): {', '.join(sorted(unknown))}"
        )
    word = 0
    for field in fields.values():
        word = field.insert(word, values.get(field.name, 0))
    return word


@dataclass(frozen=True)
class DecodedInstruction:
    """A decoded, architecturally valid Nova instruction."""

    word: int
    definition: InstructionDef
    fields: Mapping[str, int]

    @property
    def mnemonic(self) -> str:
        return self.definition.mnemonic

    @property
    def opcode(self) -> int:
        return self.definition.opcode

    @property
    def privilege(self) -> str:
        return self.definition.privilege

    def field(self, name: str) -> int:
        return self.fields[name]

    def register(self, operand: str) -> int:
        """Raw register index of a register operand."""
        operand_def = self.definition.operand(operand)
        return self.fields[operand_def.fields[0]]

    def signed_immediate(self, operand: str) -> int:
        operand_def = self.definition.operand(operand)
        field = operand_def.fields[0]
        return sign_extend(self.fields[field], self.definition.field_width(field))

    def unsigned_immediate(self, operand: str) -> int:
        operand_def = self.definition.operand(operand)
        return self.fields[operand_def.fields[0]]

    @property
    def memory_base(self) -> int:
        operand = self.definition.memory_operand()
        if operand is None:
            raise KeyError(f"{self.mnemonic} has no memory operand")
        return self.fields[operand.fields[0]]

    @property
    def memory_displacement(self) -> int:
        operand = self.definition.memory_operand()
        if operand is None:
            raise KeyError(f"{self.mnemonic} has no memory operand")
        field = operand.fields[1]
        return sign_extend(self.fields[field], self.definition.field_width(field))

    @property
    def target_displacement(self) -> int:
        field = self.definition.operand("target").fields[0]
        return sign_extend(self.fields[field], self.definition.field_width(field))

    @property
    def csr_index(self) -> int:
        operand = self.definition.csr_operand()
        if operand is None:
            raise KeyError(f"{self.mnemonic} has no CSR operand")
        return self.fields[operand.fields[0]]


def decode_instruction(word: int) -> Optional[DecodedInstruction]:
    """Decode ``word``; return ``None`` when it is not a defined instruction."""
    if isinstance(word, bool) or not isinstance(word, int):
        raise TypeError("Nova instruction words must be Python integers.")
    if not 0 <= word <= INSTRUCTION_MASK:
        raise ValueError("Nova instructions must be unsigned 32-bit words.")
    definition = INSTRUCTIONS_BY_OPCODE.get(word >> 24)
    if definition is None:
        return None
    if not definition.is_valid_encoding(word):
        return None
    return DecodedInstruction(word, definition, extract_fields(definition.format, word))


def decode_strict(word: int) -> DecodedInstruction:
    """Decode ``word``, raising :class:`IllegalEncoding` when it is undefined."""
    decoded = decode_instruction(word)
    if decoded is None:
        raise IllegalEncoding(f"0x{word:08x} is not a defined Nova instruction")
    return decoded


def decode_bytes(data: bytes) -> List[Optional[DecodedInstruction]]:
    """Decode a little-endian instruction stream.

    Returns ``None`` entries for words that are not defined instructions, so
    callers can report data as data instead of guessing.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Nova instruction streams are byte strings.")
    data = bytes(data)
    if len(data) % INSTRUCTION_BYTES:
        raise ValueError("Nova instruction stream must be a multiple of four bytes.")
    return [
        decode_instruction(
            int.from_bytes(data[index:index + INSTRUCTION_BYTES], "little")
        )
        for index in range(0, len(data), INSTRUCTION_BYTES)
    ]


def format_operand(operand: Operand, definition: InstructionDef,
                   fields: Mapping[str, int]) -> str:
    """Render one operand in canonical Nova assembly syntax."""

    def signed(name: str) -> int:
        return sign_extend(fields[name], definition.field_width(name))

    if operand.is_register:
        return f"r{fields[operand.fields[0]]}"
    if operand.is_signed_immediate:
        return str(signed(operand.fields[0]))
    if operand.is_unsigned_immediate:
        return str(fields[operand.fields[0]])
    if operand.is_memory:
        base = fields[operand.fields[0]]
        displacement = signed(operand.fields[1])
        if displacement == 0:
            return f"[r{base}]"
        sign = "+" if displacement > 0 else "-"
        return f"[r{base} {sign} {abs(displacement)}]"
    if operand.is_target:
        return str(signed(operand.fields[0]))
    if operand.is_csr:
        index = fields[operand.fields[0]]
        csr = CSR_TABLE.get(index)
        return csr.name if csr is not None else f"#{index}"
    raise ValueError(f"Cannot render operand kind {operand.kind!r}")


def format_instruction(definition: InstructionDef,
                       fields: Mapping[str, int]) -> str:
    """Render a decoded instruction in canonical Nova assembly syntax."""
    tokens = [format_operand(operand, definition, fields)
              for operand in definition.operands]
    if not tokens:
        return definition.mnemonic
    return f"{definition.mnemonic} " + ", ".join(tokens)


def format_word(word: int) -> str:
    """Canonical assembly text for ``word``.

    Words that are not defined instructions are rendered as ``.word 0x...``,
    because a Nova instruction stream may contain data and an undefined word
    is not silently reinterpreted as an instruction.
    """
    decoded = decode_instruction(word)
    if decoded is None:
        return f".word 0x{word:08x}"
    return format_instruction(decoded.definition, decoded.fields)


# --------------------------------------------------------------------------
# Generated specification tables
# --------------------------------------------------------------------------


def instruction_table_markdown() -> List[str]:
    """Markdown rows for every instruction, generated from the ISA tables."""
    return [
        definition.summary_row()
        for definition in sorted(_INSTRUCTIONS, key=lambda item: item.opcode)
    ]


def csr_table_markdown() -> List[str]:
    """Markdown rows for every implemented CSR, generated from the ISA tables."""
    return [CSR_TABLE[index].summary_row() for index in sorted(CSR_TABLE)]


def opcode_table_markdown() -> List[str]:
    """Markdown rows for the opcode allocation map."""
    rows = []
    for low, high, purpose in OPCODE_RANGES:
        span = f"`0x{low:02X}`" if low == high else f"`0x{low:02X}-0x{high:02X}`"
        rows.append(f"| {span} | {purpose} |")
    return rows


def reserved_csr_list() -> str:
    """Comma-separated list of reserved CSR indices."""
    return ", ".join(f"`{index}`" for index in RESERVED_CSR_INDICES)


# --------------------------------------------------------------------------
# Table self-validation
# --------------------------------------------------------------------------


def validate_table() -> None:
    """Check that the ISA tables are internally consistent.

    Raises :class:`ValueError` on duplicate opcodes or mnemonics, unknown or
    out-of-range field references, layouts that do not cover all 32 bits,
    CSR definitions outside the CSR space, and instructions that cannot be
    encoded and decoded through their own public API.
    """
    # Nova is a single, 64-bit-only architecture. Guard the profile itself so
    # no future edit can silently introduce a narrower or alternate mode.
    if EXECUTION_MODES != ("Nova64",):
        raise ValueError("Nova defines exactly one execution mode: Nova64")
    if any(PROFILE[key] != WORD_BITS for key in
           ("word_bits", "address_bits", "gpr_bits", "csr_bits", "reference_bits")):
        raise ValueError("Nova requires 64-bit registers, addresses, and references")
    if PHYSICAL_ADDRESS_BITS > ADDRESS_BITS or VIRTUAL_ADDRESS_BITS > ADDRESS_BITS:
        raise ValueError("Nova address widths cannot exceed the 64-bit address width")
    if PAGE_SIZE != (1 << PAGE_BITS) or PT_LEVELS * PT_INDEX_BITS + PAGE_BITS != VIRTUAL_ADDRESS_BITS:
        raise ValueError("Nova page geometry must cover the canonical virtual address")

    if len(_INSTRUCTIONS) != len(INSTRUCTION_TABLE):
        raise ValueError("Duplicate mnemonic in the instruction table.")
    if len(_INSTRUCTIONS) != len(INSTRUCTIONS_BY_OPCODE):
        raise ValueError("Duplicate opcode in the instruction table.")

    for definition in _INSTRUCTIONS:
        fields = definition.fields
        names = [field.name for field in fields]
        if len(set(names)) != len(names):
            raise ValueError(f"{definition.mnemonic} repeats an instruction field")
        if definition.opcode > 0xFF:
            raise ValueError(f"{definition.mnemonic} opcode exceeds one byte")
        covered_bits = 0
        for field in fields:
            covered_bits |= field.mask << field.low
        if covered_bits != INSTRUCTION_MASK:
            raise ValueError(f"{definition.mnemonic} format does not cover 32 bits")
        for name, value in definition.fixed:
            if name not in names:
                raise ValueError(
                    f"{definition.mnemonic} pins unknown field {name!r}"
                )
            width = definition.field_width(name)
            if not 0 <= value < (1 << width):
                raise ValueError(
                    f"{definition.mnemonic} pins {name} to {value}, which does "
                    f"not fit in {width} bits"
                )
        for operand in definition.operands:
            for name in operand.fields:
                if name not in names:
                    raise ValueError(
                        f"{definition.mnemonic} operand {operand.name} uses "
                        f"unknown field {name!r}"
                    )
            if operand.is_memory and len(operand.fields) != 2:
                raise ValueError(
                    f"{definition.mnemonic} memory operand needs two fields"
                )
        if definition.privilege not in PRIVILEGE_LEVELS:
            raise ValueError(
                f"{definition.mnemonic} names unknown privilege "
                f"{definition.privilege!r}"
            )
        word = definition.encode(definition.example_fields())
        decoded = decode_instruction(word)
        if decoded is None or decoded.mnemonic != definition.mnemonic:
            raise ValueError(
                f"{definition.mnemonic} example encoding 0x{word:08x} does not "
                "decode back to the same instruction"
            )

    for index, csr in CSR_TABLE.items():
        if index != csr.index:
            raise ValueError(f"CSR {csr.name} is keyed by the wrong index")


validate_table()