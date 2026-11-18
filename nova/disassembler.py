"""Nova disassembler.

The disassembler is a formatter over the shared decoder in :mod:`nova.isa`:
decoding, reserved-field validation, and operand rendering all come from the
same tables the assembler and emulator use.

Words that are not defined Nova instructions are rendered as ``.word 0x...``
rather than being forced into an instruction, because a Nova image may contain
data. :func:`disassemble_words` additionally reports which words were not
instructions, and :func:`disassemble_stream` raises on them.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .isa import (
    INSTRUCTION_BYTES,
    INSTRUCTION_MASK,
    DecodedInstruction,
    decode_instruction,
    format_instruction,
)

__all__ = [
    "DisassemblyError",
    "disassemble_bytes",
    "disassemble_word",
    "disassemble_words",
    "disassemble_stream",
]


class DisassemblyError(ValueError):
    """Raised when a byte stream cannot be disassembled as Nova code."""


def _check_word(word: int) -> int:
    if isinstance(word, bool) or not isinstance(word, int):
        raise TypeError("Nova instruction words must be Python integers.")
    if not 0 <= word <= INSTRUCTION_MASK:
        raise ValueError("Nova instructions must be unsigned 32-bit words.")
    return word


def disassemble_word(word: int) -> str:
    """Canonical Nova assembly text for one 32-bit word.

    Undefined encodings become ``.word 0x........``, which is *not* accepted
    by the assembler; use :func:`disassemble_words` to detect them.
    """
    _check_word(word)
    decoded = decode_instruction(word)
    if decoded is None:
        return f".word 0x{word:08x}"
    return format_instruction(decoded.definition, decoded.fields)


def disassemble_words(words: Sequence[int]) -> List[Optional[DecodedInstruction]]:
    """Decode a sequence of words, using ``None`` for undefined encodings."""
    return [decode_instruction(_check_word(word)) for word in words]


def disassemble_stream(data: bytes) -> List[DecodedInstruction]:
    """Decode a little-endian word stream, rejecting undefined encodings."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Nova instruction streams are byte strings.")
    data = bytes(data)
    if len(data) % INSTRUCTION_BYTES:
        raise DisassemblyError(
            "Nova instruction stream must be a multiple of four bytes, got "
            f"{len(data)}"
        )
    decoded = []
    for offset in range(0, len(data), INSTRUCTION_BYTES):
        word = int.from_bytes(data[offset:offset + INSTRUCTION_BYTES], "little")
        instruction = decode_instruction(word)
        if instruction is None:
            raise DisassemblyError(
                f"offset {offset}: 0x{word:08x} is not a defined Nova instruction"
            )
        decoded.append(instruction)
    return decoded


def disassemble_bytes(data: bytes) -> List[str]:
    """Disassemble a little-endian word stream into canonical assembly text."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Nova instruction streams are byte strings.")
    data = bytes(data)
    if len(data) % INSTRUCTION_BYTES:
        raise DisassemblyError(
            "Nova instruction stream must be a multiple of four bytes, got "
            f"{len(data)}"
        )
    return [
        disassemble_word(
            int.from_bytes(data[offset:offset + INSTRUCTION_BYTES], "little")
        )
        for offset in range(0, len(data), INSTRUCTION_BYTES)
    ]