"""Nova relocatable object format v1 (``.nobj``).

A ``.nobj`` file is a *relocatable* object: code and data in named sections,
a symbol table (local defined, global defined, and undefined external
symbols), and a small set of relocations that name the instruction sites
whose branch-like target fields must be adjusted by the linker. It is a
separate format from the ``.nob`` executable container, which is a flat,
final image with no symbols and no relocations. A ``.nobj`` object never
bakes in a final load address.

This module only defines the container and the relocation/symbol model; it
has no knowledge of instruction encoding, which stays in :mod:`nova.isa`
and :mod:`nova.assembler`.

Byte-for-byte layout, with all multi-byte fields little-endian::

    offset  size  field
    0       4     magic       ASCII ``NOBJ``
    4       2     version     ``1``
    6       2     flags       reserved, must be ``0``
    8       8     section_count
    16      8     symbol_count
    24      8     relocation_count
    32      N     payload     section records, then symbol records, then
                             relocation records, each laid out sequentially

    section record:
        u32  name_size;  N name bytes (UTF-8)
        u8   kind        0 = progbits, 1 = nobits
        u8   flags       bit0 alloc, bit1 exec, bit2 write, bit3 read
        u8   alignment_log2   stored alignment is ``1 << log2``
        u8   reserved    must be ``0``
        u32  size        logical size in bytes; for progbits sections this is
                         the number of data bytes that follow, for nobits
                         sections the reserved (uninitialized) byte size
        N    data        ``size`` bytes, present only for progbits sections

    symbol record:
        u32  name_size;  N name bytes (UTF-8)
        u8   binding     0 = local, 1 = global
        u32  section_index  index of the defining section, or ``0xFFFFFFFF``
                         for an undefined (external) symbol
        u64  value       section-relative byte offset, or ``0`` if undefined

    relocation record:
        u32  section_index  the section to relocate
        u64  offset      byte offset of the first word the relocation covers
        u32  symbol_index   the symbol whose final address participates
        u8   kind        ``0`` = PCREL_DISP14, ``1`` = LA64, ``2`` = ABS64
        i64  addend      signed 64-bit adjustment (``0`` when unused)

Validity rules, all enforced by the reader and the writer:

- ``section_count``/``symbol_count``/``relocation_count`` must exactly match
  the number of records; a valid file is consumed exactly, with no trailing
  bytes.
- Section and symbol names are non-empty UTF-8, at most 255 bytes, without
  NUL, and unique within their own table.
- Section kinds are ``progbits`` or ``nobits``; ``progbits`` sections carry
  exactly ``size`` data bytes, ``nobits`` sections carry none (their ``size``
  is a reservation). Flags are a subset of ``alloc``/``exec``/``write``/
  ``read``; alignment is a power of two in ``1..2^31`` bytes.
- A symbol is either defined (a valid section index and a section-relative
  byte offset within ``[0, size]``) or undefined; an undefined symbol is a
  global external with a ``0`` value. Local symbols must be defined.
- Relocations name a ``progbits`` section, a 4-byte-aligned ``offset`` whose
  word(s) fit inside the section, and a symbol in range. Three kinds exist.
  ``PCREL_DISP14`` (byte ``0``) is the signed 14-bit byte displacement from
  the instruction itself, used by branch and ``CALL`` instructions.
  ``LA64`` (byte ``1``) is a 9-instruction fixed-width ``LA``/``LI`` expansion
  beginning at ``offset``: the linker re-encodes the symbol's absolute address
  into the whole block, so nine instruction words must fit. ``ABS64`` (byte
  ``2``) is a data relocation: the 8 bytes at ``offset`` hold the symbol's
  resolved absolute address, little-endian (for ``.quad target`` data words).
  Addends are always ``0`` in v1 objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .isa import INSTRUCTION_BYTES

__all__ = [
    "BINDING_BY_NAME",
    "NOBJ_HEADER_SIZE",
    "NOBJ_MAGIC",
    "NOBJ_VERSION",
    "ObjectError",
    "ObjectFile",
    "RELOCATION_KIND_BY_NAME",
    "RELOCATION_SPAN_BYTES",
    "Relocation",
    "SECTION_KIND_BY_NAME",
    "SECTION_FLAG_BITS",
    "SYMBOL_TYPE_BY_NAME",
    "Section",
    "Symbol",
    "decode_object",
    "encode_object",
    "load_object",
    "write_object",
]

#: Four ASCII magic bytes at offset 0.
NOBJ_MAGIC = b"NOBJ"
#: Current container version at offset 4. Version 1 is still decoded; version 2
#: adds a symbol type and size field to each symbol record.
NOBJ_VERSION = 2
NOBJ_VERSION_V1 = 1
#: Total header size in bytes.
NOBJ_HEADER_SIZE = 32

#: The relocation kinds a v1 object can carry. ``PCREL_DISP14`` names the
#: target operand of a branch or ``CALL``: a signed 14-bit byte displacement
#: from the instruction itself, so the linker adds the final-address delta
#: into that field. ``LA64`` names a fixed 9-instruction ``LA``/``LI``
#: expansion whose immediate fields jointly encode an absolute 64-bit address;
#: the linker re-encodes the resolved address into the block (see
#: :mod:`nova.pseudo`). ``ABS64`` names an 8-byte little-endian data word
#: (from ``.quad target``) that holds the symbol's resolved absolute address;
#: ``PCREL_LONG`` names a fixed 10-word ``LI r15, target`` / ``JMP r15`` block
#: used by the long-branch pseudo-instruction for out-of-range jumps.
#: the linker writes ``value + addend`` into those bytes. The assembler emits
#: ``PCREL_DISP14`` only for branch/call targets that name an undefined
#: external symbol, ``LA64`` for any ``LA`` whose target is a symbol, and
#: ``ABS64`` for a ``.quad`` whose operand is a symbol.
RELOCATION_KIND_BY_NAME: Mapping[str, int] = {
    "PCREL_DISP14": 0,
    "LA64": 1,
    "ABS64": 2,
    "PCREL_LONG": 3,
}
RELOCATION_KIND_BY_BYTE = {v: k for k, v in RELOCATION_KIND_BY_NAME.items()}

#: Bytes ``offset`` must leave free at the end of its section, per relocation
#: kind. ``LA64`` occupies nine instruction words, ``ABS64`` an 8-byte data
#: word, and everything else one word.
RELOCATION_SPAN_BYTES: Mapping[str, int] = {
    "PCREL_DISP14": INSTRUCTION_BYTES,
    "LA64": 9 * INSTRUCTION_BYTES,
    "ABS64": 8,
    "PCREL_LONG": 10 * INSTRUCTION_BYTES,
}

SECTION_KIND_BY_NAME: Mapping[str, int] = {
    "progbits": 0,
    "nobits": 1,
}
SECTION_KIND_BY_BYTE = {v: k for k, v in SECTION_KIND_BY_NAME.items()}

#: Flags accepted on a section, as ``(name, bit)`` pairs.
SECTION_FLAG_BITS: Mapping[str, int] = {
    "alloc": 1,
    "exec": 2,
    "write": 4,
    "read": 8,
}

BINDING_BY_NAME: Mapping[str, int] = {
    "local": 0,
    "global": 1,
}
BINDING_BY_BYTE = {v: k for k, v in BINDING_BY_NAME.items()}

#: Symbol types carried by objects from version 2 onward. ``notype`` is the
#: default for v1 objects and for labels with no declared type.
SYMBOL_TYPE_BY_NAME: Mapping[str, int] = {
    "notype": 0,
    "object": 1,
    "function": 2,
    "section": 3,
    "file": 4,
}
SYMBOL_TYPE_BY_BYTE = {v: k for k, v in SYMBOL_TYPE_BY_NAME.items()}

_UNDEFINED_SECTION = 0xFFFFFFFF
_MAX_NAME_BYTES = 255
_MAX_ALIGNMENT_LOG2 = 31


class ObjectError(ValueError):
    """Raised when a ``.nobj`` object is invalid or cannot be constructed."""


@dataclass(frozen=True)
class Section:
    """One named section of an object.

    ``kind`` is ``"progbits"`` (``data`` bytes are stored in the file) or
    ``"nobits"`` (``size`` is reserved and uninitialized, ``data`` is empty).
    ``flags`` is any subset of ``alloc``/``exec``/``write``/``read`` and
    ``alignment`` a power of two giving the alignment of the section's first
    byte.
    """

    name: str
    kind: str = "progbits"
    flags: Iterable[str] = ()
    alignment: int = 4
    data: bytes = b""
    size: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "data", bytes(self.data))
        if self.kind == "progbits":
            if self.size is not None and self.size != len(self.data):
                raise ObjectError(
                    f"progbits section {self.name!r} size {self.size} does not "
                    f"match its data length {len(self.data)}"
                )
            object.__setattr__(self, "size", len(self.data))
        else:
            if self.data:
                raise ObjectError(
                    f"nobits section {self.name!r} cannot carry data bytes"
                )
            object.__setattr__(self, "size", 0 if self.size is None else self.size)
            object.__setattr__(self, "data", b"")


@dataclass(frozen=True)
class Symbol:
    """One symbol table entry.

    ``binding`` is ``"local"`` or ``"global"``. A defined symbol names a
    ``section`` and carries a ``value`` that is a section-relative byte offset
    (``size`` itself is allowed for a one-past-the-end label). An undefined
    symbol is a global external: ``binding == "global"``, ``section is None``,
    ``value == 0``.
    """

    name: str
    binding: str
    section: int | None
    value: int = 0
    type: str = "notype"
    size: int = 0

    @property
    def is_defined(self) -> bool:
        return self.section is not None


@dataclass(frozen=True)
class Relocation:
    """One pending fixup inside an object.

    ``section`` is the section to relocate, ``offset`` the byte offset of the
    word whose contents depend on the resolved address, ``symbol`` the symbol
    whose final address the value depends on, and ``kind`` the relocation
    style (``PCREL_DISP14`` for a branch/call field, ``LA64`` for a fixed
    ``LA``/``LI`` block, ``ABS64`` for an 8-byte data word); ``addend`` is an
    extra signed adjustment, always ``0`` for the current kinds.
    """

    section: int
    offset: int
    symbol: int
    kind: str = "PCREL_DISP14"
    addend: int = 0


@dataclass(frozen=True)
class ObjectFile:
    """A validated relocatable Nova object: sections, symbols, relocations.

    Records are kept in the order they were built, so encoding is a
    deterministic function of this structure: two equal objects always encode
    to the same bytes and decode back to the same structure.
    """

    sections: Sequence[Section] = ()
    symbols: Sequence[Symbol] = ()
    relocations: Sequence[Relocation] = ()
    #: Optional debug records. They live in a separate .nox container, not in
    #: the .nobj byte stream, so this field does not affect object encoding.
    debug: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "relocations", tuple(self.relocations))


def _u(value: int, width: int) -> bytes:
    return value.to_bytes(width, "little")


def _check_name(name: str, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise ObjectError(f"{what} name must be a non-empty string")
    encoded = name.encode("utf-8")
    if len(encoded) > _MAX_NAME_BYTES:
        raise ObjectError(
            f"{what} name {name!r} is longer than {_MAX_NAME_BYTES} UTF-8 bytes"
        )
    if "\x00" in name:
        raise ObjectError(f"{what} name {name!r} contains a NUL")


def _check_section(section: Section, seen_names: set[str]) -> None:
    _check_name(section.name, "section")
    if section.name in seen_names:
        raise ObjectError(f"duplicate section name {section.name!r}")
    seen_names.add(section.name)
    if section.kind not in SECTION_KIND_BY_NAME:
        raise ObjectError(f"unsupported section kind {section.kind!r}")
    unknown_flags = set(section.flags) - set(SECTION_FLAG_BITS)
    if unknown_flags:
        raise ObjectError(
            f"section {section.name!r} has unknown flags {sorted(unknown_flags)}"
        )
    if not isinstance(section.alignment, int) or not section.alignment >= 1:
        raise ObjectError(
            f"section {section.name!r} alignment must be a positive power of two"
        )
    if section.alignment & (section.alignment - 1):
        raise ObjectError(
            f"section {section.name!r} alignment {section.alignment} is not a "
            "power of two"
        )
    if section.alignment.bit_length() - 1 > _MAX_ALIGNMENT_LOG2:
        raise ObjectError(
            f"section {section.name!r} alignment exceeds 2^{_MAX_ALIGNMENT_LOG2}"
        )
    if section.kind == "progbits":
        if section.size != len(section.data):
            raise ObjectError(
                f"progbits section {section.name!r} size {section.size} does not "
                f"match its data length {len(section.data)}"
            )
    elif section.data:
        raise ObjectError(f"nobits section {section.name!r} cannot carry data")
    if section.kind == "nobits" and section.size < 0:
        raise ObjectError(f"nobits section {section.name!r} has a negative size")


def _check_symbol(symbol: Symbol, section_count: int,
                  section_sizes: Sequence[int], seen_names: set[str]) -> None:
    _check_name(symbol.name, "symbol")
    if symbol.name in seen_names:
        raise ObjectError(f"duplicate symbol name {symbol.name!r}")
    seen_names.add(symbol.name)
    if symbol.binding not in BINDING_BY_NAME:
        raise ObjectError(f"unsupported symbol binding {symbol.binding!r}")
    if symbol.type not in SYMBOL_TYPE_BY_NAME:
        raise ObjectError(f"unsupported symbol type {symbol.type!r}")
    if not isinstance(symbol.size, int) or symbol.size < 0:
        raise ObjectError(f"symbol {symbol.name!r} has a negative size")
    if symbol.section is None:
        if symbol.binding != "global":
            raise ObjectError(
                f"undefined symbol {symbol.name!r} must be a global external"
            )
        if symbol.value != 0:
            raise ObjectError(f"undefined symbol {symbol.name!r} must have value 0")
        return
    if not isinstance(symbol.section, int) or not 0 <= symbol.section < section_count:
        raise ObjectError(
            f"symbol {symbol.name!r} names out-of-range section "
            f"{symbol.section!r} of {section_count}"
        )
    if not isinstance(symbol.value, int) or not 0 <= symbol.value <= section_sizes[symbol.section]:
        raise ObjectError(
            f"symbol {symbol.name!r} offset {symbol.value} is outside its "
            f"section of size {section_sizes[symbol.section]}"
        )


def _check_relocation(relocation: Relocation, section_kinds: Sequence[str],
                      section_sizes: Sequence[int], symbol_count: int) -> None:
    if relocation.kind not in RELOCATION_KIND_BY_NAME:
        raise ObjectError(f"unsupported relocation kind {relocation.kind!r}")
    if not isinstance(relocation.section, int) or not 0 <= relocation.section < len(section_kinds):
        raise ObjectError(
            f"relocation names out-of-range section {relocation.section!r}"
        )
    if section_kinds[relocation.section] != "progbits":
        raise ObjectError(
            "relocations must name a progbits section, not "
            f"{section_kinds[relocation.section]!r}"
        )
    size = section_sizes[relocation.section]
    span = RELOCATION_SPAN_BYTES[relocation.kind]
    if not isinstance(relocation.offset, int) or relocation.offset < 0:
        raise ObjectError(f"relocation offset must be a byte offset: {relocation.offset!r}")
    if relocation.offset % INSTRUCTION_BYTES:
        raise ObjectError(
            f"relocation offset {relocation.offset} is not {INSTRUCTION_BYTES}-byte aligned"
        )
    if relocation.offset + span > size:
        raise ObjectError(
            f"relocation offset {relocation.offset} does not fit {span} bytes of "
            f"relocation in a section of {size} bytes"
        )
    if not isinstance(relocation.symbol, int) or not 0 <= relocation.symbol < symbol_count:
        raise ObjectError(
            f"relocation names out-of-range symbol {relocation.symbol!r}"
        )
    if not isinstance(relocation.addend, int) or not -(1 << 63) <= relocation.addend < (1 << 63):
        raise ObjectError(f"relocation addend must be a signed 64-bit value: {relocation.addend!r}")

def _validate_object(obj: ObjectFile) -> None:
    _check_sections_symbols_relocations(obj.sections, obj.symbols, obj.relocations)


def _check_sections_symbols_relocations(
    sections: Sequence[Section],
    symbols: Sequence[Symbol],
    relocations: Sequence[Relocation],
) -> None:
    seen_sections: set[str] = set()
    sizes = [0] * len(sections)
    kinds = ["progbits"] * len(sections)
    for index, section in enumerate(sections):
        if not isinstance(section, Section):
            raise ObjectError("sections must be Section records")
        _check_section(section, seen_sections)
        sizes[index] = section.size if section.size is not None else len(section.data)
        kinds[index] = section.kind
    seen_symbols: set[str] = set()
    for symbol in symbols:
        if not isinstance(symbol, Symbol):
            raise ObjectError("symbols must be Symbol records")
        _check_symbol(symbol, len(sections), sizes, seen_symbols)
    for relocation in relocations:
        if not isinstance(relocation, Relocation):
            raise ObjectError("relocations must be Relocation records")
        _check_relocation(relocation, kinds, sizes, len(symbols))


def encode_object(obj: ObjectFile) -> bytes:
    """Serialize a validated relocatable Nova object to bytes."""
    if not isinstance(obj, ObjectFile):
        raise ObjectError("an object must be an ObjectFile")
    _validate_object(obj)

    payload = bytearray()
    for section in obj.sections:
        name = section.name.encode("utf-8")
        payload += _u(len(name), 4) + name
        flags = 0
        for flag_name in section.flags:
            flags |= SECTION_FLAG_BITS[flag_name]
        alignment_log2 = section.alignment.bit_length() - 1
        payload += bytes(
            (
                SECTION_KIND_BY_NAME[section.kind],
                flags,
                alignment_log2,
                0,
            )
        )
        payload += _u(section.size, 4)
        if section.kind == "progbits":
            payload += section.data

    for symbol in obj.symbols:
        name = symbol.name.encode("utf-8")
        payload += _u(len(name), 4) + name
        section_index = _UNDEFINED_SECTION if symbol.section is None else symbol.section
        payload += bytes((BINDING_BY_NAME[symbol.binding],))
        payload += _u(section_index, 4)
        payload += _u(symbol.value, 8)
        # Version 2 symbol metadata: one type byte and an eight-byte size.
        payload += bytes((SYMBOL_TYPE_BY_NAME[symbol.type],))
        payload += _u(symbol.size, 8)

    for relocation in obj.relocations:
        payload += _u(relocation.section, 4)
        payload += _u(relocation.offset, 8)
        payload += _u(relocation.symbol, 4)
        payload += bytes((RELOCATION_KIND_BY_NAME[relocation.kind],))
        payload += relocation.addend.to_bytes(8, "little", signed=True)

    header = (
        NOBJ_MAGIC
        + NOBJ_VERSION.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + _u(len(obj.sections), 8)
        + _u(len(obj.symbols), 8)
        + _u(len(obj.relocations), 8)
    )
    return header + bytes(payload)


class _Cursor:
    """A bounds-checked little-endian reader over a byte string."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, count: int, what: str) -> bytes:
        end = self.offset + count
        if end > len(self.data):
            raise ObjectError(
                f"truncated .nobj {what}: need {count} bytes at offset "
                f"{self.offset}, found {len(self.data) - self.offset}"
            )
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def u(self, width: int, what: str) -> int:
        return int.from_bytes(self.take(width, what), "little")

    def name(self, what: str) -> str:
        length = self.u(4, f"{what} name length")
        if length > _MAX_NAME_BYTES:
            raise ObjectError(f"{what} name length {length} exceeds {_MAX_NAME_BYTES}")
        raw = self.take(length, f"{what} name")
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObjectError(f"{what} name is not valid UTF-8") from exc
        if not name or "\x00" in name:
            raise ObjectError(f"invalid {what} name")
        return name


def decode_object(data: bytes | bytearray | memoryview) -> ObjectFile:
    """Parse and validate ``.nobj`` bytes, returning :class:`ObjectFile`."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ObjectError("a .nobj object must be a byte string")
    blob = bytes(data)
    if len(blob) < NOBJ_HEADER_SIZE:
        raise ObjectError(
            f"truncated .nobj header: {len(blob)} bytes, expected {NOBJ_HEADER_SIZE}"
        )
    if blob[:4] != NOBJ_MAGIC:
        raise ObjectError(f"bad .nobj magic: {blob[:4]!r}; expected {NOBJ_MAGIC!r}")
    version = int.from_bytes(blob[4:6], "little")
    if version not in (NOBJ_VERSION_V1, NOBJ_VERSION):
        raise ObjectError(
            f"unsupported .nobj version {version}; expected {NOBJ_VERSION}"
        )
    flags = int.from_bytes(blob[6:8], "little")
    if flags != 0:
        raise ObjectError(f"unsupported .nobj flags: 0x{flags:x}")

    cursor = _Cursor(blob)
    cursor.offset = 8
    section_count = cursor.u(8, "section count")
    symbol_count = cursor.u(8, "symbol count")
    relocation_count = cursor.u(8, "relocation count")
    cursor.offset = NOBJ_HEADER_SIZE

    sections: list[Section] = []
    for _ in range(section_count):
        name = cursor.name("section")
        tail = cursor.take(4, "section header")
        kind_byte, flag_byte, alignment_log2, reserved = tail
        size = cursor.u(4, "section size")
        if reserved != 0:
            raise ObjectError(f"section {name!r} has nonzero reserved padding")
        if kind_byte not in SECTION_KIND_BY_BYTE:
            raise ObjectError(f"section {name!r} has unknown kind byte {kind_byte}")
        kind = SECTION_KIND_BY_BYTE[kind_byte]
        flags = tuple(
            flag_name
            for flag_name, bit in SECTION_FLAG_BITS.items()
            if flag_byte & bit
        )
        known_bits = sum(SECTION_FLAG_BITS.values())
        if flag_byte & ~known_bits:
            raise ObjectError(
                f"section {name!r} has unknown flag bits 0x{flag_byte & ~known_bits:x}"
            )
        data = cursor.take(size, f"section {name!r} data") if kind == "progbits" else b""
        sections.append(
            Section(name=name, kind=kind, flags=flags, alignment=1 << alignment_log2,
                    size=size, data=data)
        )

    symbols: list[Symbol] = []
    for _ in range(symbol_count):
        name = cursor.name("symbol")
        binding_byte = cursor.take(1, "symbol binding")[0]
        if binding_byte not in BINDING_BY_BYTE:
            raise ObjectError(f"symbol {name!r} has unknown binding byte {binding_byte}")
        binding = BINDING_BY_BYTE[binding_byte]
        section_index = cursor.u(4, "symbol section index")
        value = cursor.u(8, "symbol value")
        if version == NOBJ_VERSION:
            type_byte = cursor.take(1, "symbol type")[0]
            if type_byte not in SYMBOL_TYPE_BY_BYTE:
                raise ObjectError(f"symbol {name!r} has unknown type byte {type_byte}")
            symbol_type = SYMBOL_TYPE_BY_BYTE[type_byte]
            size = cursor.u(8, "symbol size")
        else:
            # Version 1 records carried no metadata; defaults keep old files
            # readable without changing their meaning.
            symbol_type = "notype"
            size = 0
        symbols.append(
            Symbol(
                name=name,
                binding=binding,
                section=None if section_index == _UNDEFINED_SECTION else section_index,
                value=value,
                type=symbol_type,
                size=size,
            )
        )

    relocations: list[Relocation] = []
    for _ in range(relocation_count):
        section_index = cursor.u(4, "relocation section index")
        offset = cursor.u(8, "relocation offset")
        symbol_index = cursor.u(4, "relocation symbol index")
        kind_byte = cursor.take(1, "relocation kind")[0]
        if kind_byte not in RELOCATION_KIND_BY_BYTE:
            raise ObjectError(f"unknown relocation kind byte {kind_byte}")
        addend = int.from_bytes(cursor.take(8, "relocation addend"), "little", signed=True)
        relocations.append(
            Relocation(
                section=section_index,
                offset=offset,
                symbol=symbol_index,
                kind=RELOCATION_KIND_BY_BYTE[kind_byte],
                addend=addend,
            )
        )

    if cursor.offset != len(cursor.data):
        raise ObjectError(
            f".nobj trailing bytes: {len(cursor.data) - cursor.offset} byte(s) "
            "after the last record"
        )

    obj = ObjectFile(sections=sections, symbols=symbols, relocations=relocations)
    _validate_object(obj)
    return obj


def write_object(path: str | Path, obj: ObjectFile) -> None:
    """Write a validated relocatable Nova object to ``path``."""
    Path(path).write_bytes(encode_object(obj))


def load_object(path: str | Path) -> ObjectFile:
    """Read and validate a ``.nobj`` relocatable object from disk."""
    return decode_object(Path(path).read_bytes())