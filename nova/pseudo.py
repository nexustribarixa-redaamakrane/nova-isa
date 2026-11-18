"""Assembler pseudo-instructions for the Nova ISA.

``LI`` and ``LA`` are assembly-language abbreviations that expand into
sequences of real Nova instructions. They are *not* instructions: the ISA
tables in :mod:`nova.isa` do not define them, the emulator has no handler for
them, and the disassembler never emits them. Expansion is a pure function of
the operands and the resolved label table, so the same source always produces
the same image.

``LI rd, imm`` loads an arbitrary 64-bit constant. The canonical expansion is
defined by two rules, applied in order:

1. ``0 <= imm <= 16383`` expands to a single ``MOVI rd, imm``.
2. Otherwise the unsigned value is built from a top chunk of at most 14 bits
   followed by 13-bit chunks, most significant first: ``MOVI rd, top`` and then
   one ``SHLI rd, rd, 13`` / ``ORI rd, rd, chunk`` pair per lower chunk.

The chunks are 13 bits wide (never 14) so every ``ORI`` immediate stays inside
the signed 14-bit range ``[-8192, 8191]``, and the top chunk fits the 14-bit
unsigned ``MOVI`` immediate. Because the length of the sequence is determined
exactly by ``imm.bit_length()``, and that is monotone in the value, ``LI`` and
``LA`` lengths are monotone in the resolved address, which lets the assembler
reach a stable layout without guessing.

``LA rd, label`` loads the absolute address of ``label`` (labels are linked
against the image base), and ``LA rd, addr`` loads an integer address. ``LA``
expands exactly like ``LI`` with that address, so an ``LA`` whose target lies
above 16383 needs more than one instruction.
"""

from __future__ import annotations

from typing import List, Mapping, Sequence

from .assembler import (
    AssemblyError,
    _split_operands,
    parse_integer,
    parse_register,
)
from .isa import WORD_MASK

__all__ = [
    "MAX_LI_LENGTH",
    "PSEUDO_INSTRUCTIONS",
    "expand_la",
    "expand_li",
    "expand_pseudo",
    "is_pseudo",
    "li_maximal",
]

#: Mnemonic -> short description. The instruction tables in :mod:`nova.isa`
#: deliberately do not contain these names.
PSEUDO_INSTRUCTIONS: dict[str, str] = {
    "LI": "load a full 64-bit constant into a register",
    "LA": "load the absolute address of a label, or an integer address",
}

#: Instructions in a fixed-width (``li_maximal``) expansion: a ``MOVI`` top
#: chunk plus four ``SHLI``/``ORI`` chunk pairs. This is also the span the
#: ``LA64`` relocation covers.
MAX_LI_LENGTH = 9

_SIGNED_MIN = -(1 << 63)


def is_pseudo(mnemonic: str) -> bool:
    """True if ``mnemonic`` is an assembler pseudo-instruction."""
    return mnemonic.strip().upper() in PSEUDO_INSTRUCTIONS


def _li_sequence(rd: str, value: int) -> List[str]:
    """Canonical real-instruction text that loads ``value`` into ``rd``."""
    if value <= 0x3FFF:  # 16383; fits a single MOVI
        return [f"MOVI {rd}, {value}"]
    length = value.bit_length()
    steps = (length - 14 + 12) // 13  # ceil((length - 14) / 13), at least 1
    top = value >> (13 * steps)
    lines = [f"MOVI {rd}, {top}"]
    for shift in range(13 * (steps - 1), -1, -13):
        lines.append(f"SHLI {rd}, {rd}, 13")
        lines.append(f"ORI {rd}, {rd}, {(value >> shift) & 0x1FFF}")
    return lines


def li_maximal(rd: str, value: int, line: int | None = None) -> List[str]:
    """Expand a 64-bit constant into a fixed ``MAX_LI_LENGTH``-instruction
    sequence, regardless of the value.

    The sequence is the full-width four-chunk shape of :func:`_li_sequence`
    with the top chunk always taken from bits 52..63: ``MOVI rd, top`` then
    four ``SHLI rd, rd, 13`` / ``ORI rd, rd, chunk`` pairs. Smaller values
    simply have zero high chunks, so the sequence computes the value exactly
    for any ``0 <= value <= 2**64 - 1``. Because the length does not depend on
    the value, ``LA64`` relocations can rewrite the block in place without
    changing its size.
    """
    register = _resolve_rd(rd, line)
    if not 0 <= value <= WORD_MASK:
        raise AssemblyError(
            f"LI/LA value 0x{value:x} is outside the 64-bit address space", line
        )
    steps = 4
    top = value >> (13 * steps)
    lines = [f"MOVI {register}, {top}"]
    for shift in range(13 * (steps - 1), -1, -13):
        lines.append(f"SHLI {register}, {register}, 13")
        lines.append(f"ORI {register}, {register}, {(value >> shift) & 0x1FFF}")
    return lines


def _resolve_rd(rd_text: str, line: int | None) -> str:
    parse_register(rd_text, line)  # validate and normalize errors
    return rd_text.strip()


def expand_li(rd: str, immediate: str, line: int | None = None) -> List[str]:
    """Expand ``LI rd, immediate`` into real Nova instruction text."""
    target = _resolve_rd(rd, line)
    value = parse_integer(immediate, line)
    if not _SIGNED_MIN <= value <= WORD_MASK:
        raise AssemblyError(
            f"LI immediate {immediate!r} is outside the 64-bit range "
            f"[{_SIGNED_MIN}, {WORD_MASK}]",
            line,
        )
    return _li_sequence(target, value & WORD_MASK)


def expand_la(rd: str, target: str, labels: Mapping[str, int],
              line: int | None = None) -> List[str]:
    """Expand ``LA rd, target`` into real Nova instruction text.

    ``target`` is a label already resolved to an absolute address, or an
    integer address.
    """
    register = _resolve_rd(rd, line)
    name = target.strip()
    if name in labels:
        address = labels[name]
    else:
        try:
            address = parse_integer(name, line)
        except AssemblyError as exc:
            raise AssemblyError(
                f"unknown LA target {target!r}: not a label or integer address",
                line,
            ) from exc
    if not _SIGNED_MIN <= address <= WORD_MASK:
        raise AssemblyError(
            f"LA address {target!r} is outside the 64-bit address space", line
        )
    return _li_sequence(register, address & WORD_MASK)


def expand_pseudo(text: str, line: int | None = None,
                  labels: Mapping[str, int] | None = None) -> List[str]:
    """Expand one statement if it is a pseudo-instruction, otherwise return it.

    Real instructions are returned unchanged so the caller can hand every line
    to the table-driven assembler in one uniform pass.
    """
    parts = text.split(None, 1)
    mnemonic = parts[0].strip().upper() if parts else ""
    if mnemonic not in PSEUDO_INSTRUCTIONS:
        return [text]
    operand_text = parts[1] if len(parts) == 2 else ""
    operands = _split_operands(operand_text, line)
    if mnemonic == "LI":
        if len(operands) != 2:
            raise AssemblyError(
                f"LI expects rd and an immediate, got {len(operands)} operand(s)",
                line,
            )
        return expand_li(operands[0], operands[1], line)
    if len(operands) != 2:
        raise AssemblyError(
            f"LA expects rd and a label, got {len(operands)} operand(s)", line
        )
    return expand_la(operands[0], operands[1], {} if labels is None else labels, line)