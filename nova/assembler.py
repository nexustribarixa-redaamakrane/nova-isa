"""Canonical Nova assembly assembler.

The assembler is driven entirely by :mod:`nova.isa`: it looks up an
:class:`~nova.isa.InstructionDef`, parses exactly the operands that definition
declares, and encodes through the declared instruction fields. There is no
per-mnemonic special case here, so the assembler cannot drift away from the
instruction table.

Accepted source grammar, one statement per line::

    ; comment to end of line
    label: [instruction [operands]]     # a label may share a line

Mnemonics and register names are case-insensitive. Immediates accept decimal,
hexadecimal (``0x``/``0X``), binary (``0b``), and octal (``0o``) literals with
an optional ``#`` or ``$`` prefix. Memory operands are ``[reg]``,
``[reg + disp]``, or ``[reg - disp]`` with a signed 14-bit ``disp``. Branch and
call targets may be a label or a signed byte displacement relative to the
address of the instruction itself. Every instruction is exactly one 32-bit
word, so labels are byte offsets counted in words.

Directives are not defined by the ISA and are rejected explicitly in
executable assembly (:func:`assemble_text`). The ``.nobj`` object frontend
(:func:`assemble_object`) additionally understands a fixed set: the linking
directives ``.global``/``.globl NAME``, the section selector ``.section
.text|.rodata|.data|.bss`` (default ``.text``), and the data directives
``.byte``/``.hword``/``.word``/``.quad`` (1/2/4/8-byte little-endian
integers), ``.asciz "text"`` (UTF-8 bytes plus a NUL), ``.space N`` (zero
bytes; in ``.bss`` only a reservation), and ``.align N`` (pad the current
data section to a multiple of ``N``). Data directives are only accepted in
the data sections, never in ``.text``. Branch and call targets must lie in
``.text``; a target that names a symbol defined in a data section is an
error. The frontend turns branch/call targets that name an undefined symbol
into ``PCREL_DISP14`` relocations, any ``LA`` whose target is a symbol
(defined or external) into a ``LA64`` relocation over a fixed
``MAX_LI_LENGTH``-instruction expansion, and any ``.quad`` whose operand is a
symbol into an ``ABS64`` relocation covering the 8-byte data word, so no
relocatable constant depends on a load address. A bare ``LA`` to an integer
constant expands as usual and carries no relocation; ``.quad`` with an
integer operand is plain data.
Every malformed input raises :class:`AssemblyError` carrying the source line
number. No operand is ever silently substituted or truncated.

The assembler also understands the ``LI`` and ``LA`` pseudo-instructions from
:mod:`nova.pseudo`. They have no encoding of their own; each expands to real
instructions (see ``expand_pseudo``). Expansion lengths are laid out in a
monotone fixed-point loop: because an expansion's length depends only on its
value, and ``LA`` values grow monotonically with the layout, the layout
converges after at most a few passes and a stable image is always produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .isa import (
    CSR_BY_NAME,
    CSR_COUNT,
    GPR_COUNT,
    INSTRUCTION_ALIGNMENT,
    INSTRUCTION_BYTES,
    INSTRUCTION_MASK,
    INSTRUCTION_TABLE,
    ISA_VERSION,
    OPERAND_KINDS,
    REGISTERS,
    WORD_MASK,
    zero_extend,
)
from .object import ObjectFile, Relocation, Section, Symbol, SYMBOL_TYPE_BY_NAME

__all__ = [
    "AssemblyError",
    "DEFAULT_ENTRY",
    "ParsedStatement",
    "SUPPORTED_OPERAND_KINDS",
    "assemble_line",
    "assemble_object",
    "assemble_program",
    "assemble_statement",
    "assemble_text",
    "parse_csr",
    "parse_integer",
    "parse_memory_operand",
    "parse_program",
    "parse_register",
    "parse_source",
    "words_to_bytes",
]

DEFAULT_ENTRY = 0x1000

#: Operand kinds this assembler implements, identical to the ISA's set.
SUPPORTED_OPERAND_KINDS: Tuple[str, ...] = OPERAND_KINDS

_LABEL_RE = re.compile(r"^[A-Za-z_.$][A-Za-z0-9_.$]*$")
_REGISTER_RE = re.compile(r"^r(\d+)$", re.IGNORECASE)
_INTEGER_RE = re.compile(
    r"^[+-]?(0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)$"
)
_CSR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_MEMORY_RE = re.compile(r"^\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
                        r"(?:([+-])\s*([^\]]*?)\s*)?\]$")


class AssemblyError(ValueError):
    """Raised for any Nova assembly source error, with its line number."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


# --------------------------------------------------------------------------
# Token parsing
# --------------------------------------------------------------------------


def parse_register(token: str, line: int | None = None) -> int:
    """Parse a register name or alias into an index in 0..31."""
    name = token.strip().lower()
    match = _REGISTER_RE.fullmatch(name)
    if match is not None:
        number = int(match.group(1))
        if number < GPR_COUNT:
            return number
        raise AssemblyError(f"unknown register: {token!r}", line)
    if name in REGISTERS:
        return REGISTERS[name]
    raise AssemblyError(f"unknown register: {token!r}", line)


def parse_integer(token: str, line: int | None = None) -> int:
    """Parse a Nova integer literal."""
    text = token.strip()
    if text[:1] in ("#", "$"):
        text = text[1:].strip()
    if not _INTEGER_RE.fullmatch(text):
        raise AssemblyError(f"invalid integer literal: {token!r}", line)
    return int(text.replace("_", ""), 0)


def parse_csr(token: str, line: int | None = None) -> int:
    """Parse a CSR name or an explicit ``#index`` into an index in 0..31."""
    text = token.strip()
    if text[:1] in ("#", "$") or _INTEGER_RE.fullmatch(text):
        index = parse_integer(text, line)
        if not 0 <= index < CSR_COUNT:
            raise AssemblyError(
                f"CSR index {index} is outside 0..{CSR_COUNT - 1}", line
            )
        return index
    if not _CSR_NAME_RE.fullmatch(text):
        raise AssemblyError(f"invalid CSR operand: {token!r}", line)
    csr = CSR_BY_NAME.get(text.lower())
    if csr is None:
        known = ", ".join(sorted(CSR_BY_NAME))
        raise AssemblyError(f"unknown CSR {text!r}; defined CSRs are {known}", line)
    return csr.index


def parse_memory_operand(token: str, line: int | None = None) -> Tuple[int, int]:
    """Parse ``[reg]``, ``[reg + disp]``, or ``[reg - disp]``."""
    match = _MEMORY_RE.fullmatch(token.strip())
    if match is None:
        raise AssemblyError(
            f"malformed memory operand {token!r}; expected [reg], "
            "[reg + disp], or [reg - disp]", line
        )
    base = parse_register(match.group(1), line)
    sign = match.group(2)
    displacement_text = match.group(3)
    if sign is None:
        return base, 0
    if not displacement_text:
        raise AssemblyError(f"missing displacement in {token!r}", line)
    displacement = parse_integer(displacement_text, line)
    if sign == "-":
        displacement = -displacement
    return base, displacement


# --------------------------------------------------------------------------
# Line structure
# --------------------------------------------------------------------------


def _strip_comment(text: str) -> str:
    return text.split(";", 1)[0]


def _split_labels(line: str, line_number: int) -> Tuple[List[str], str]:
    """Peel leading ``label:`` prefixes off a statement."""
    labels: List[str] = []
    remainder = line
    while True:
        head, separator, tail = remainder.partition(":")
        if not separator:
            return labels, head.strip()
        head = head.strip()
        if not head or not _LABEL_RE.fullmatch(head):
            return labels, remainder.strip()
        labels.append(head)
        remainder = tail.strip()


def _split_operands(text: str, line_number: int) -> List[str]:
    if not text.strip():
        return []
    operands = [operand.strip() for operand in text.split(",")]
    for operand in operands:
        if not operand:
            raise AssemblyError(f"empty operand in {text!r}", line_number)
    return operands


def _split_object_operands(text: str, line_number: int,
                           split_quotes: bool = True) -> List[str]:
    """Split directive operands, honoring double-quoted string literals.

    A comma inside a ``"..."`` string does not separate operands, so ``.asciz
    "a,b"`` stays one operand. Returns stripped tokens. With ``split_quotes``
    disabled the whole ``text`` is returned as a single operand (for
    ``.global``/``.globl``, whose names never quote).
    """
    if not split_quotes:
        return [text.strip()]
    text = text.strip()
    if not text:
        return []
    operands: List[str] = []
    quoted = False
    token: List[str] = []
    for character in text:
        if character == '"':
            quoted = not quoted
            token.append(character)
        elif character == "," and not quoted:
            operands.append("".join(token).strip())
            token = []
        else:
            token.append(character)
    if quoted:
        raise AssemblyError("unterminated string literal", line_number)
    operands.append("".join(token).strip())
    for index, operand in enumerate(operands):
        if not operand:
            raise AssemblyError(f"empty operand in {text!r}", line_number)
    return operands


@dataclass(frozen=True)
class ParsedStatement:
    """One instruction statement with the labels attached to it.

    ``labels`` are the label names defined immediately before this statement;
    they all point at the first instruction this statement expands to.
    """

    line_number: int
    text: str
    labels: Tuple[str, ...]


@dataclass(frozen=True)
class ParsedDirective:
    """One object-frontend directive with the labels attached to it.

    ``directive`` is the lowercase name including the leading dot (``.byte``,
    ``.section``, ...); ``operands`` are the stripped tokens; ``labels`` are
    the label names ``offset``-bound to the directive's emitted bytes (labels
    on ``.section``/``.global`` lines are rejected).
    """

    line_number: int
    directive: str
    operands: Tuple[str, ...]
    labels: Tuple[str, ...]


def parse_program(source: str) -> Tuple[List[ParsedStatement], List[str]]:
    """Split source into statements with their labels, and trailing labels.

    Returns ``(entries, trailing)`` where ``entries`` is the list of
    :class:`ParsedStatement` objects in source order and ``trailing`` is the
    list of label names defined after the last statement. Offsets are computed
    later by the layout pass, because a pseudo-instruction statement expands to
    a variable number of instructions.
    """
    if not isinstance(source, str):
        raise TypeError("Nova assembly source must be a string.")
    entries: List[ParsedStatement] = []
    trailing: List[str] = []
    seen: Dict[str, int] = {}
    pending: List[str] = []
    for line_number, raw in enumerate(source.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        found, line = _split_labels(line, line_number)
        for label in found:
            if label in seen:
                raise AssemblyError(f"duplicate label: {label}", line_number)
            seen[label] = line_number
            pending.append(label)
        if not line:
            continue
        if line.startswith("."):
            raise AssemblyError(
                f"directives are not defined by {ISA_VERSION}: {line.split()[0]!r}",
                line_number,
            )
        if ":" in line:
            raise AssemblyError(f"malformed label or statement: {line!r}", line_number)
        entries.append(ParsedStatement(line_number, line, tuple(pending)))
        pending = []
    trailing.extend(pending)
    return entries, trailing


def parse_source(source: str) -> Tuple[List[Tuple[int, str]], Dict[str, int]]:
    """Split source into instruction lines and a label table.

    Labels map to the byte offset of the next instruction within the image.
    """
    entries, trailing = parse_program(source)
    statements: List[Tuple[int, str]] = [
        (entry.line_number, entry.text) for entry in entries
    ]
    labels: Dict[str, int] = {}
    offset = 0
    for entry in entries:
        for label in entry.labels:
            labels[label] = offset
        offset += INSTRUCTION_BYTES
    for label in trailing:
        labels[label] = offset
    return statements, labels


# --------------------------------------------------------------------------
# Object-frontend directives
# --------------------------------------------------------------------------

#: Directive -> emitted bytes per item. ``.byte`` 1, ``.hword`` 2, ``.word`` 4,
#: ``.quad`` 8; ``.asciz`` and ``.space``/``.align`` are handled separately.
_DATA_DIRECTIVES: set[str] = {
    ".byte", ".hword", ".word", ".quad", ".asciz", ".space", ".align",
}

#: The four sections the ``.section`` directive may select, with
#: ``(kind, flags, default alignment)``. ``.text`` always stays section 0 and
#: keeps its instruction alignment; data sections default to byte alignment so
#: ``.align`` alone controls padding.
_SECTION_DEFAULTS: Dict[str, tuple[str, tuple[str, ...], int]] = {
    ".text": ("progbits", ("alloc", "exec"), INSTRUCTION_ALIGNMENT),
    ".rodata": ("progbits", ("alloc", "read"), 1),
    ".data": ("progbits", ("alloc", "write"), 1),
    ".bss": ("nobits", ("alloc", "write"), 1),
}


def _asciz_bytes(token: str, line_number: int) -> bytes:
    """Decode one ``.asciz "..."`` operand into its UTF-8 bytes plus a NUL.

    Supported escapes are ``\\n``, ``\\t``, ``\\r``, ``\\0``, ``\\\\``, and
    ``\\"``; any other backslash sequence is an error.
    """
    if not (len(token) >= 2 and token[0] == '"' and token[-1] == '"'):
        raise AssemblyError(
            f".asciz operand {token!r} must be a quoted string literal",
            line_number,
        )
    body = token[1:-1]
    out = bytearray()
    index = 0
    escapes = {
        "n": b"\n", "t": b"\t", "r": b"\r", "0": b"\x00",
        "\\": b"\\", '"': b'"',
    }
    while index < len(body):
        character = body[index]
        if character == "\\":
            if index + 1 >= len(body):
                raise AssemblyError("unterminated escape in string literal",
                                    line_number)
            escaped = body[index + 1]
            if escaped not in escapes:
                raise AssemblyError(
                    f"unsupported escape '\\{escaped}' in string literal",
                    line_number,
                )
            out += escapes[escaped]
            index += 2
            continue
        out += character.encode("utf-8")
        index += 1
    out += b"\x00"
    return bytes(out)


def _parse_object_entries(
        source: str) -> Tuple[Sequence[object], List[str], List[str], Dict[str, Dict[str, object]]]:
    """Parse object-frontend source into instruction/directive entries.

    Returns ``(entries, globals_order, trailing, symbol_meta)`` where ``entries`` is a list
    of :class:`ParsedStatement` and :class:`ParsedDirective` in source order,
    ``globals_order`` the ordered list of ``.global``/``.globl`` names, and
    ``trailing`` the labels defined after the last content line, and
    ``symbol_meta`` a per-symbol map of ``.type``/``.size`` values. A
    ``.global``/``.globl``/``.section``/``.type``/``.size`` line may not carry a label; every
    other directive and every instruction takes the labels defined on its
    line. Statement and directive line numbers are preserved for diagnostics.
    """
    if not isinstance(source, str):
        raise TypeError("Nova assembly source must be a string.")
    entries: List[object] = []
    globals_order: List[str] = []
    globals_seen: set[str] = set()
    seen: Dict[str, int] = {}
    pending: List[str] = []
    symbol_meta: Dict[str, Dict[str, object]] = {}
    for line_number, raw in enumerate(source.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        found, line = _split_labels(line, line_number)
        for label in found:
            if label in seen:
                raise AssemblyError(f"duplicate label: {label}", line_number)
            seen[label] = line_number
            pending.append(label)
        if not line:
            continue
        if not line.startswith("."):
            if ":" in line:
                raise AssemblyError(
                    f"malformed label or statement: {line!r}", line_number
                )
            entries.append(ParsedStatement(line_number, line, tuple(pending)))
            pending = []
            continue
        parts = line.split(None, 1)
        directive = parts[0].lower()
        rest = parts[1].strip() if len(parts) == 2 else ""
        if directive in (".global", ".globl"):
            if pending:
                raise AssemblyError(
                    f"{directive} cannot carry a label", line_number
                )
            name = rest.strip()
            if not name:
                raise AssemblyError(
                    f"{directive} requires exactly one symbol name", line_number
                )
            if not _LABEL_RE.fullmatch(name):
                raise AssemblyError(
                    f"malformed {directive} symbol {name!r}", line_number
                )
            if name not in globals_seen:
                globals_seen.add(name)
                globals_order.append(name)
            continue
        if directive == ".section":
            if pending:
                raise AssemblyError(
                    ".section cannot carry a label", line_number
                )
            operands = _split_object_operands(rest, line_number)
            if len(operands) != 1 or operands[0] not in _SECTION_DEFAULTS:
                known = ", ".join(sorted(_SECTION_DEFAULTS))
                raise AssemblyError(
                    f".section operand must be one of {known}", line_number
                )
            entries.append(
                ParsedDirective(line_number, directive, tuple(operands), ())
            )
            continue
        if directive in (".type", ".size"):
            if pending:
                raise AssemblyError(
                    f"{directive} cannot carry a label", line_number
                )
            operands = _split_object_operands(rest, line_number)
            if len(operands) != 2:
                raise AssemblyError(
                    f"{directive} requires exactly two operands", line_number
                )
            name = operands[0]
            if not _LABEL_RE.fullmatch(name):
                raise AssemblyError(
                    f"malformed {directive} symbol {name!r}", line_number
                )
            meta = symbol_meta.setdefault(name, {"line": line_number})
            if directive == ".type":
                symbol_type = operands[1].lower().lstrip("@%")
                if symbol_type not in SYMBOL_TYPE_BY_NAME:
                    known = ", ".join(sorted(SYMBOL_TYPE_BY_NAME))
                    raise AssemblyError(
                        f"unknown symbol type {operands[1]!r}; expected one of {known}",
                        line_number,
                    )
                meta["type"] = symbol_type
            else:
                try:
                    size = parse_integer(operands[1], line_number)
                except AssemblyError:
                    raise AssemblyError(
                        f".size operand {operands[1]!r} is not an integer", line_number
                    ) from None
                if size < 0:
                    raise AssemblyError(
                        ".size cannot be negative", line_number
                    )
                meta["size"] = size
            continue
        if directive not in _DATA_DIRECTIVES:
            raise AssemblyError(
                f"directive {directive!r} is not defined by {ISA_VERSION} in "
                "the .nobj object format", line_number
            )
        operands = _split_object_operands(rest, line_number)
        if not operands:
            raise AssemblyError(
                f"{directive} requires at least one operand", line_number
            )
        entries.append(
            ParsedDirective(line_number, directive, tuple(operands),
                            tuple(pending))
        )
        pending = []
    return entries, globals_order, pending, symbol_meta


# --------------------------------------------------------------------------
# Operand encoding
# --------------------------------------------------------------------------


def _range_error(definition, operand, value: int, token: str, line: int) -> None:
    low, high = definition.limits(operand)
    width = definition.field_width(operand.fields[0])
    if operand.is_signed_immediate:
        kind = f"signed {width}-bit range [{low}, {high}]"
    else:
        kind = f"unsigned {width}-bit range [{low}, {high}]"
    raise AssemblyError(
        f"{definition.mnemonic} immediate {token!r} is outside the {kind}", line
    )


def _encode_immediate(definition, operand, value: int, token: str, line: int) -> int:
    low, high = definition.limits(operand)
    if not low <= value <= high:
        _range_error(definition, operand, value, token, line)
    width = definition.field_width(operand.fields[0])
    if operand.is_signed_immediate:
        return zero_extend(value, width)
    return value


def _encode_signed_field(definition, operand, value: int, token: str,
                         line: int) -> int:
    field = operand.fields[-1]
    width = definition.field_width(field)
    low = -(1 << (width - 1))
    high = (1 << (width - 1)) - 1
    if not low <= value <= high:
        raise AssemblyError(
            f"{definition.mnemonic} operand {token!r} has displacement {value}, "
            f"outside the signed {width}-bit range [{low}, {high}]", line
        )
    return zero_extend(value, width)


def _encode_operand(definition, operand, token: str, labels: Mapping[str, int],
                    pc: int, line: int,
                    undefined: Callable | None = None,
                    label_sections: Mapping[str, str] | None = None) -> Dict[str, int]:
    if operand.is_register:
        return {operand.fields[0]: parse_register(token, line)}
    if operand.is_immediate:
        return {
            operand.fields[0]: _encode_immediate(
                definition, operand, parse_integer(token, line), token, line
            )
        }
    if operand.is_memory:
        base, displacement = parse_memory_operand(token, line)
        return {
            operand.fields[0]: base,
            operand.fields[1]: _encode_signed_field(
                definition, operand, displacement, token, line
            ),
        }
    if operand.is_target:
        if token in labels:
            displacement = labels[token] - pc
        else:
            try:
                displacement = parse_integer(token, line)
            except AssemblyError as exc:
                if label_sections is not None and token in label_sections:
                    raise AssemblyError(
                        f"{definition.mnemonic} target {token!r} is defined in "
                        f"section {label_sections[token]!r}, but branch and "
                        "call targets must lie in the .text section", line
                    ) from exc
                if undefined is not None:
                    displacement = undefined(token, pc, definition, line)
                else:
                    raise AssemblyError(
                        f"unknown {definition.mnemonic} target {token!r}: not a "
                        "label or an integer displacement", line
                    ) from exc
        if displacement % INSTRUCTION_ALIGNMENT:
            raise AssemblyError(
                f"{definition.mnemonic} target displacement {displacement} is not "
                f"a multiple of {INSTRUCTION_ALIGNMENT} bytes", line
            )
        return {
            operand.fields[0]: _encode_signed_field(
                definition, operand, displacement, token, line
            )
        }
    if operand.is_csr:
        return {operand.fields[0]: parse_csr(token, line)}
    raise AssemblyError(  # pragma: no cover - guarded by Operand validation
        f"unsupported operand kind {operand.kind!r}", line
    )


def assemble_statement(text: str, line_number: int, pc: int,
                       labels: Mapping[str, int],
                       *,
                       undefined: Callable | None = None,
                       label_sections: Mapping[str, str] | None = None) -> int:
    """Assemble one instruction statement into a 32-bit word.

    When ``undefined`` is provided it is called as
    ``undefined(target, pc, definition, line)`` for a branch/call target that
    is neither a known label nor an integer; the object frontend uses it to
    record a ``PCREL_DISP14`` relocation and return a placeholder displacement.
    ``label_sections`` maps every defined label to its section name; when the
    object frontend passes it, a branch/call target that names a symbol
    defined outside ``.text`` is an error instead of an external reference.
    """
    parts = text.split(None, 1)
    mnemonic = parts[0].strip().upper()
    operand_text = parts[1] if len(parts) == 2 else ""
    operands = _split_operands(operand_text, line_number)

    definition = INSTRUCTION_TABLE.get(mnemonic)
    if definition is None:
        raise AssemblyError(f"unknown instruction: {mnemonic}", line_number)

    expected = len(definition.operands)
    if len(operands) != expected:
        wanted = f"expects {expected} operand(s)" if expected else "takes no operands"
        raise AssemblyError(
            f"{mnemonic} {wanted}, got {len(operands)}", line_number
        )

    values: Dict[str, int] = {}
    for operand, token in zip(definition.operands, operands):
        encoded = _encode_operand(definition, operand, token, labels, pc,
                                  line_number, undefined=undefined,
                                  label_sections=label_sections)
        overlap = set(values) & set(encoded)
        if overlap:  # pragma: no cover - ISA definitions never overlap fields
            raise AssemblyError(
                f"{mnemonic} operands share field(s) {sorted(overlap)}", line_number
            )
        values.update(encoded)

    word = definition.encode(values)
    if not definition.is_valid_encoding(word):  # pragma: no cover - internal
        raise AssemblyError(
            f"internal error: 0x{word:08x} is not a valid {mnemonic} encoding",
            line_number,
        )
    return word


def assemble_line(text: str, pc: int = DEFAULT_ENTRY,
                  labels: Mapping[str, int] | None = None) -> int:
    """Assemble a single instruction line for execution at address ``pc``."""
    line = _strip_comment(text).strip()
    if not line:
        raise AssemblyError("empty instruction")
    return assemble_statement(line, 0, pc, {} if labels is None else labels)


def _layout_labels(entries: Sequence[ParsedStatement], trailing: Sequence[str],
                   entry: int, lengths: Sequence[int]) -> Dict[str, int]:
    """Resolve labels to absolute addresses for an assumed statement layout."""
    labels: Dict[str, int] = {}
    offset = 0
    for statement, length in zip(entries, lengths):
        for label in statement.labels:
            labels[label] = entry + offset
        offset += length * INSTRUCTION_BYTES
    for label in trailing:
        labels[label] = entry + offset
    return labels


def assemble_text(source: str, *, entry: int = 0) -> bytes:
    """Assemble Nova assembly source into a raw little-endian instruction image.

    ``entry`` is the address the image is linked at (the load address). Branch
    and call displacements are relative to their own instruction, so they are
    unaffected by ``entry``; ``entry`` decides the addresses the label table
    refers to, which is what ``LA`` loads.

    ``LI`` and ``LA`` pseudo-instructions are expanded to real instructions
    before encoding. Their expansion lengths are laid out in a monotone
    fixed-point loop: lengths never shrink, so the loop terminates with a
    stable, deterministic image.
    """
    from .pseudo import expand_pseudo  # local import avoids an import cycle

    entries, trailing = parse_program(source)
    count = len(entries)
    lengths = [1] * count
    expansions: List[List[str]] | None = None
    for _ in range(1 + 8 * count + 2):
        labels = _layout_labels(entries, trailing, entry, lengths)
        expansions = [
            expand_pseudo(statement.text, statement.line_number, labels)
            for statement in entries
        ]
        next_lengths = [len(lines) for lines in expansions]
        if next_lengths == lengths:
            break
        lengths = next_lengths
    else:
        raise AssemblyError(
            "assembly layout did not converge; expansion lengths oscillated"
        )

    labels = _layout_labels(entries, trailing, entry, lengths)
    words = []
    pc = entry
    for statement, lines in zip(entries, expansions):
        for line in lines:
            words.append(assemble_statement(line, statement.line_number, pc, labels))
            pc += INSTRUCTION_BYTES
    return words_to_bytes(words)


def assemble_program(lines: Iterable[str], *, entry: int = 0) -> bytes:
    """Assemble an iterable of assembly source lines."""
    return assemble_text("\n".join(lines), entry=entry)


def _integer_operand(token: str, line_number: int, directive: str) -> int:
    """Parse an integer literal, refusing symbol operands with a clear error."""
    try:
        return parse_integer(token.strip(), line_number)
    except AssemblyError:
        raise AssemblyError(
            f"{directive} operand {token!r} must be an integer literal",
            line_number,
        ) from None


def _nonnegative_operand(token: str, line_number: int, directive: str) -> int:
    """Parse an integer literal that must be zero or positive."""
    value = _integer_operand(token, line_number, directive)
    if value < 0:
        raise AssemblyError(
            f"{directive} operand {token!r} must be a non-negative integer",
            line_number,
        )
    return value


def _align_operand(token: str, line_number: int) -> int:
    """Parse a ``.align`` operand: a positive power of two, at most 2^31."""
    value = _nonnegative_operand(token, line_number, ".align")
    if value < 1 or (value & (value - 1)) or value > (1 << 31):
        raise AssemblyError(
            f".align operand {token!r} must be a power of two from 1 to 2^31",
            line_number,
        )
    return value


def _data_integer_bytes(width: int, token: str, line_number: int,
                        directive: str) -> bytes:
    """Encode one integer operand of a fixed-width data directive.

    ``width`` is the byte width (1, 2, 4, or 8 for ``.byte``/``.hword``/
    ``.word``/``.quad``). Signed values are accepted down to ``-(1 << (8w-1))``
    and unsigned values up to ``(1 << 8w) - 1``, matching how the loader
    materializes memory words; the value is emitted little-endian.
    """
    value = _integer_operand(token, line_number, directive)
    bits = 8 * width
    if not -(1 << (bits - 1)) <= value <= (1 << bits) - 1:
        raise AssemblyError(
            f"{directive} operand {value} does not fit in {width} bytes",
            line_number,
        )
    return (value & ((1 << bits) - 1)).to_bytes(width, "little")


def assemble_object(source: str) -> ObjectFile:
    """Assemble Nova assembly source into a relocatable ``.nobj`` object.

    The layout is produced at section-relative address 0, so no final load
    address is baked into the object. ``.text`` instructions are laid out and
    pseudo-expanded as described in :func:`assemble_text`, while ``.rodata``,
    ``.data``, and ``.bss`` hold the data directives described in the module
    docstring. Every defined label becomes a symbol whose value is its offset
    within its section; ``.global``/``.globl`` directives mark a symbol as
    globally visible, and a ``.global`` whose name is never defined or
    referenced becomes an undefined external symbol.

    Branch and call targets that are neither a defined label nor an integer
    become ``PCREL_DISP14`` relocations against an undefined external symbol,
    encoded at a placeholder displacement of 0. An ``LA`` whose target is a
    symbol (defined in this object or external) becomes an ``LA64`` relocation
    over a fixed, maximal-width ``LA``/``LI`` expansion (see
    :func:`nova.pseudo.li_maximal`): the expansion length is constant, so it
    does not disturb the monotone layout pass, and the linker rewrites the
    block with the resolved absolute address. A ``.quad`` whose operand is a
    symbol becomes an ``ABS64`` relocation: the linker writes the resolved
    absolute address into the 8-byte slot at load time. ``LI`` and all other
    ``LA`` forms expand as in executable assembly and carry no relocation.
    """
    from .pseudo import MAX_LI_LENGTH, expand_pseudo, li_maximal  # local import avoids a cycle

    entries, globals_order, trailing, symbol_meta = _parse_object_entries(source)
    global_set: set[str] = set(globals_order)

    # Section order: .text is always section 0; other sections follow in
    # first-appearance order. A .section directive itself contributes no
    # content but switches the current section for following entries.
    section_names = [".text"]
    current_index = 0
    section_of_entry: List[int] = []
    for entry in entries:
        if isinstance(entry, ParsedDirective) and entry.directive == ".section":
            name = entry.operands[0]
            if name not in section_names:
                section_names.append(name)
            current_index = section_names.index(name)
        section_of_entry.append(current_index)

    # -- .text fixed-point layout --------------------------------
    text_indices = [
        i for i, sec in enumerate(section_of_entry)
        if sec == 0 and isinstance(entries[i], ParsedStatement)
    ]
    text_entries = [entries[i] for i in text_indices]
    text_position = {i: k for k, i in enumerate(text_indices)}
    count = len(text_entries)

    # Statements of the form ``LA rd, symbol`` expand to a fixed-width LA64
    # block; everything else goes through the pseudo-expansion machinery.
    symbol_la: Dict[int, Tuple[str, str]] = {}
    for index, statement in enumerate(text_entries):
        parts = statement.text.split(None, 1)
        if len(parts) != 2 or parts[0].strip().upper() != "LA":
            continue
        operands = _split_operands(parts[1], statement.line_number)
        if len(operands) != 2:
            continue  # malformed; expand_pseudo reports it
        target = operands[1].strip()
        try:
            parse_integer(target, statement.line_number)
        except AssemblyError:
            if _LABEL_RE.fullmatch(target):
                symbol_la[index] = (operands[0].strip(), target)

    lengths = [MAX_LI_LENGTH if index in symbol_la else 1 for index in range(count)]
    expansions: Optional[List[List[str]]] = None
    for _ in range(1 + 8 * count + 2):
        labels = _layout_labels(text_entries, [], 0, lengths)
        expansions = []
        for index, statement in enumerate(text_entries):
            if index in symbol_la:
                rd, _ = symbol_la[index]
                expansions.append(li_maximal(rd, 0, statement.line_number))
            else:
                expansions.append(
                    expand_pseudo(statement.text, statement.line_number, labels)
                )
        next_lengths = [len(lines) for lines in expansions]
        if next_lengths == lengths:
            break
        lengths = next_lengths
    else:
        raise AssemblyError(
            "assembly layout did not converge; expansion lengths oscillated"
        )

    labels = _layout_labels(text_entries, [], 0, lengths)

    # -- Per-section layout and emission -------------------------
    section_data = {sec: bytearray() for sec in range(len(section_names))}
    section_alignment = [_SECTION_DEFAULTS[name][2] for name in section_names]
    offset = [0] * len(section_names)
    symbol_rows: List[Tuple[str, int, int]] = []
    abs64_rows: List[Tuple[int, int, str]] = []
    defined_names: set[str] = set()

    def record_symbol(name: str, sec: int, value: int) -> None:
        if name not in defined_names:
            defined_names.add(name)
            symbol_rows.append((name, sec, value))

    data_width = {".byte": 1, ".hword": 2, ".word": 4, ".quad": 8}
    for i, entry in enumerate(entries):
        sec = section_of_entry[i]
        section_name = section_names[sec]
        if isinstance(entry, ParsedStatement):
            if sec != 0:
                raise AssemblyError(
                    f"instructions are only accepted in the .text section; "
                    f"{entry.text!r} appears in the {section_name} section",
                    entry.line_number,
                )
            for label in entry.labels:
                record_symbol(label, 0, offset[0])
            offset[0] += lengths[text_position[i]] * INSTRUCTION_BYTES
            continue
        directive = entry.directive
        if directive == ".section":
            continue
        if sec == 0:
            raise AssemblyError(
                f"the {directive} directive is only accepted in the .rodata, "
                f".data, or .bss sections, not in .text",
                entry.line_number,
            )
        for label in entry.labels:
            record_symbol(label, sec, offset[sec])
        is_bss = section_name == ".bss"
        if directive == ".align":
            if len(entry.operands) != 1:
                raise AssemblyError(
                    ".align expects exactly one operand", entry.line_number
                )
            value = _align_operand(entry.operands[0], entry.line_number)
            pad = (-offset[sec]) % value
            if not is_bss:
                section_data[sec] += b"\x00" * pad
            offset[sec] += pad
            if value > section_alignment[sec]:
                section_alignment[sec] = value
        elif directive == ".space":
            if len(entry.operands) != 1:
                raise AssemblyError(
                    ".space expects exactly one operand", entry.line_number
                )
            value = _nonnegative_operand(entry.operands[0], entry.line_number,
                                         ".space")
            if is_bss:
                offset[sec] += value
            else:
                section_data[sec] += b"\x00" * value
                offset[sec] += value
        elif is_bss:
            raise AssemblyError(
                f"the {directive} directive is not accepted in .bss; "
                "use .space to reserve uninitialized storage",
                entry.line_number,
            )
        elif directive == ".asciz":
            if len(entry.operands) != 1:
                raise AssemblyError(
                    ".asciz expects exactly one string literal",
                    entry.line_number,
                )
            data = _asciz_bytes(entry.operands[0], entry.line_number)
            section_data[sec] += data
            offset[sec] += len(data)
        else:
            width = data_width[directive]
            for token in entry.operands:
                item_offset = offset[sec]
                if item_offset % width:
                    raise AssemblyError(
                        f"{directive} operand requires a {width}-byte-aligned "
                        f"offset within {section_name}",
                        entry.line_number,
                    )
                if directive == ".quad":
                    target = token.strip()
                    try:
                        value = parse_integer(target, entry.line_number)
                    except AssemblyError:
                        if not _LABEL_RE.fullmatch(target):
                            raise AssemblyError(
                                f".quad operand {token!r} is not an integer "
                                "or a symbol name",
                                entry.line_number,
                            ) from None
                        section_data[sec] += b"\x00" * 8
                        abs64_rows.append((sec, item_offset, target))
                    else:
                        if not -(1 << 63) <= value <= (1 << 64) - 1:
                            raise AssemblyError(
                                f".quad operand {value} does not fit in 8 bytes",
                                entry.line_number,
                            )
                        section_data[sec] += (value & WORD_MASK).to_bytes(8, "little")
                else:
                    section_data[sec] += _data_integer_bytes(
                        width, token, entry.line_number, directive
                    )
                offset[sec] += width

    for label in trailing:
        record_symbol(label, current_index, offset[current_index])

    defined_names = {name for name, _, _ in symbol_rows}

    # -- Relocations and undefined externals ----------------------
    undefined_refs: List[str] = []
    reloc_rows: List[Tuple[int, int, str, str]] = []

    def ensure_symbol(name: str) -> None:
        if name not in defined_names and name not in undefined_refs:
            undefined_refs.append(name)

    for sec, item_offset, name in abs64_rows:
        ensure_symbol(name)
        reloc_rows.append((sec, item_offset, name, "ABS64"))

    def external_target(name: str, pc: int, definition, line_number: int) -> int:
        if not _LABEL_RE.fullmatch(name):
            raise AssemblyError(f"invalid external target {name!r}", line_number)
        ensure_symbol(name)
        reloc_rows.append((0, pc, name, "PCREL_DISP14"))
        return 0

    label_sections = {name: section_names[sec] for name, sec, _ in symbol_rows}

    pc = 0
    for index, statement in enumerate(text_entries):
        if index in symbol_la:
            _, name = symbol_la[index]
            ensure_symbol(name)
            reloc_rows.append((0, pc, name, "LA64"))
        for asm_line in expansions[index]:
            word = assemble_statement(asm_line, statement.line_number, pc,
                                      labels, undefined=external_target,
                                      label_sections=label_sections)
            section_data[0] += word.to_bytes(INSTRUCTION_BYTES, "little")
            pc += INSTRUCTION_BYTES

    symbols = [
        Symbol(name=name,
               binding="global" if name in global_set else "local",
               section=sec, value=value)
        for name, sec, value in symbol_rows
    ]
    for name in undefined_refs:
        symbols.append(Symbol(name=name, binding="global", section=None, value=0))
    for name in globals_order:
        if name not in defined_names and name not in undefined_refs:
            symbols.append(Symbol(name=name, binding="global", section=None, value=0))

    symbol_index = {symbol.name: index for index, symbol in enumerate(symbols)}
    for name, meta in symbol_meta.items():
        if name not in symbol_index:
            raise AssemblyError(
                f".type/.size name {name!r} is not a defined symbol",
                int(meta["line"]),
            )
        updates = {
            key: value for key, value in meta.items() if key != "line"
        }
        index = symbol_index[name]
        symbols[index] = replace(symbols[index], **updates)
    relocations = [
        Relocation(section=sec, offset=item_offset, kind=kind,
                   symbol=symbol_index[name], addend=0)
        for sec, item_offset, name, kind in reloc_rows
    ]

    sections = []
    for sec, name in enumerate(section_names):
        _, flags, _ = _SECTION_DEFAULTS[name]
        if name == ".bss":
            sections.append(Section(
                name=name, kind="nobits", flags=flags,
                alignment=section_alignment[sec], size=offset[sec], data=b"",
            ))
        else:
            sections.append(Section(
                name=name, kind="progbits", flags=flags,
                alignment=section_alignment[sec],
                data=bytes(section_data[sec]),
            ))
    return ObjectFile(sections=tuple(sections), symbols=symbols,
                      relocations=relocations)


def words_to_bytes(words: Sequence[int]) -> bytes:
    """Serialize instruction words as a little-endian raw Nova image."""
    image = bytearray()
    for word in words:
        if isinstance(word, bool) or not isinstance(word, int):
            raise TypeError("Instruction words must be Python integers.")
        if not 0 <= word <= INSTRUCTION_MASK:
            raise ValueError(f"Instruction word out of range: {word!r}")
        image += word.to_bytes(INSTRUCTION_BYTES, "little")
    return bytes(image)