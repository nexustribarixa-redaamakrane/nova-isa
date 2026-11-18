"""Nova linker v0.1 by services for the :mod:`nova` toolchain.

The linker consumes relocatable ``.nobj`` objects (:mod:`nova.object`) built by
``nova-as -o *.nobj`` and produces a final ``.nob`` executable
(:mod:`nova.nob`). It is a deliberately minimal static linker: a small,
deterministic rule set, no dynamic linking, no linker scripts, no relaxation.

Nova Linker v0.1 contract
=========================

Inputs and output
    Any number of ``.nobj`` objects and one ``.nob`` executable. The output
    image is flat; there are no virtual-memory concepts in this revision.

Section layout
    Sections are grouped by name across the inputs. Groups are emitted in the
    order ``.text``, ``.rodata``, ``.data``, ``.bss``, then any other names in
    lexical order. Each output section starts at an address aligned to the
    largest member alignment; each member starts at an address aligned to its
    own alignment; members appear in input order. ``.bss`` (a ``nobits``
    section) is materialized as zero-initialized bytes inside the flat image,
    because the ``.nob`` container has no separate uninitialized area. The
    image is padded to a multiple of four bytes.

Symbol resolution
    A defined symbol resolves to its own object's section base plus its
    section-relative value. A local symbol is visible only inside the object
    that defines it. A global definition is visible to every object; a
    reference to an external symbol resolves to the (single) global definition
    of that name. Every undefined global symbol in every input must be
    resolved, and no global name may be defined more than once. The entry
    symbol (default ``_start``) must be defined exactly once.

Relocations
    ``PCREL_DISP14``: the resolved address minus the site address, written
    into the signed 14-bit target field of a branch or ``CALL``; a
    non-instruction-aligned target or a value outside ``[-8192, 8191]`` is a
    linker error.
    ``LA64``: the resolved absolute address re-encoded as a fixed
    9-instruction ``LA``/``LI`` expansion (see :func:`nova.pseudo.li_maximal`)
    over the block the relocation names.

Determinism
    Given the same inputs and options, output bytes are identical: no hashed
    iteration order, timestamps, random identifiers, or host paths participate.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from .assembler import assemble_statement
from .isa import (
    INSTRUCTION_ALIGNMENT,
    INSTRUCTION_BYTES,
    WORD_MASK,
    build_word,
    decode_instruction,
    zero_extend,
)
from .nob import NobError, NobImage, encode_nob
from .object import ObjectError, ObjectFile, Symbol, encode_object
from .pseudo import li_maximal

__all__ = [
    "DEFAULT_ENTRY_SYMBOL",
    "DEFAULT_IMAGE_BASE",
    "LINKER_VERSION",
    "LinkError",
    "RESOLVED_PCREL_RANGE",
    "link_objects",
]

#: The documented software contract for :mod:`nova.link`.
LINKER_VERSION = "Nova Linker v0.1"

#: The default entry symbol ``nova-ld`` resolves.
DEFAULT_ENTRY_SYMBOL = "_start"

#: Default load address of image byte 0, matching ``nova-as``.
DEFAULT_IMAGE_BASE = 0x1000

#: Output section placement order; names outside this map sort after ``.bss``.
SECTION_ORDER = {".text": 0, ".rodata": 1, ".data": 2, ".bss": 3}

#: The signed 14-bit displacement an ``PCREL_DISP14`` relocation may encode.
RESOLVED_PCREL_RANGE = (-(1 << 13), (1 << 13) - 1)

_PCREL_KIND = "PCREL_DISP14"
_LA64_KIND = "LA64"
_ABS64_KIND = "ABS64"


class LinkError(ValueError):
    """Raised when the linker cannot produce a valid output image."""


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & -alignment


def _link_error_from(exc: Exception) -> LinkError:
    if isinstance(exc, LinkError):
        return exc
    return LinkError(str(exc))


def link_objects(objects: Iterable[ObjectFile], *,
                 entry_symbol: str = DEFAULT_ENTRY_SYMBOL,
                 image_base: int = DEFAULT_IMAGE_BASE) -> NobImage:
    """Link zero or more validated objects into a final ``.nob`` executable.

    ``entry_symbol`` names the defined symbol execution starts at, and
    ``image_base`` the load address of image byte 0 (the same meaning it has
    for ``nova-as`` and ``nova-run``).
    """
    inputs = tuple(objects)
    if not inputs:
        raise LinkError("no input object files")
    if image_base % INSTRUCTION_BYTES:
        raise LinkError(
            f"image base 0x{image_base:x} is not {INSTRUCTION_BYTES}-byte aligned"
        )
    for obj in inputs:
        if not isinstance(obj, ObjectFile):
            raise LinkError("linker inputs must be .nobj ObjectFile records")
        try:
            encode_object(obj)  # validates section/symbol/relocation tables
        except ObjectError as exc:
            raise LinkError(str(exc)) from exc

    # -- Section grouping and placement ---------------------------------------
    groups: Dict[str, List[Tuple[int, int]]] = {}
    for obj_index, obj in enumerate(inputs):
        for section_index, section in enumerate(obj.sections):
            groups.setdefault(section.name, []).append((obj_index, section_index))
    ordered = sorted(
        groups.items(),
        key=lambda item: (SECTION_ORDER.get(item[0], 4), item[0]),
    )

    image = bytearray()
    member_addrs: Dict[Tuple[int, int], int] = {}

    def pad_to(alignment: int) -> None:
        next_address = image_base + len(image)
        image.extend(b"\x00" * ((alignment - next_address % alignment) % alignment))

    for name, members in ordered:
        pad_to(max(inputs[o].sections[s].alignment for o, s in members))
        for obj_index, section_index in members:
            section = inputs[obj_index].sections[section_index]
            pad_to(section.alignment)
            member_addrs[(obj_index, section_index)] = image_base + len(image)
            if section.kind == "progbits":
                image.extend(section.data)
            else:  # nobits: zero-initialized bytes inside the flat image
                image.extend(b"\x00" * section.size)
    while len(image) % INSTRUCTION_BYTES:  # .nob requires a word-multiple image
        image.append(0)

    # -- Symbol resolution ----------------------------------------------------
    global_defs: Dict[str, List[Tuple[int, Symbol]]] = {}
    for obj_index, obj in enumerate(inputs):
        for symbol in obj.symbols:
            if symbol.binding == "global" and symbol.is_defined:
                global_defs.setdefault(symbol.name, []).append((obj_index, symbol))

    duplicates = sorted(name for name, defs in global_defs.items() if len(defs) > 1)
    if duplicates:
        raise LinkError(f"multiple definition of '{duplicates[0]}'")

    for obj in inputs:
        for symbol in obj.symbols:
            if not symbol.is_defined and symbol.name not in global_defs:
                raise LinkError(f"undefined symbol '{symbol.name}'")

    def resolve_address(obj_index: int, symbol: Symbol) -> int:
        if symbol.is_defined:
            return member_addrs[(obj_index, symbol.section)] + symbol.value
        owner, definition = global_defs[symbol.name][0]
        return member_addrs[(owner, definition.section)] + definition.value

    # -- Entry symbol ---------------------------------------------------------
    entry_defs: List[Tuple[int, Symbol]] = [
        (obj_index, symbol)
        for obj_index, obj in enumerate(inputs)
        for symbol in obj.symbols
        if symbol.name == entry_symbol and symbol.is_defined
    ]
    if not entry_defs:
        raise LinkError(f"undefined entry symbol '{entry_symbol}'")
    if len(entry_defs) > 1:
        raise LinkError(f"multiple definition of '{entry_symbol}'")
    entry_address = resolve_address(*entry_defs[0])
    if not image_base <= entry_address < image_base + len(image):
        raise LinkError(
            f"entry symbol '{entry_symbol}' at 0x{entry_address:x} is outside "
            f"the linked image [0x{image_base:x}, 0x{image_base + len(image):x})"
        )

    # -- Relocations ----------------------------------------------------------
    result = bytearray(image)

    def word_at(address: int) -> int:
        return int.from_bytes(result[address - image_base:address - image_base + 4],
                              "little")

    def write_word(address: int, word: int) -> None:
        result[address - image_base:address - image_base + 4] = (
            word.to_bytes(4, "little")
        )

    def patch_pcrel(site: int, symbol: Symbol, value: int) -> None:
        delta = value - site
        if delta % INSTRUCTION_ALIGNMENT:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} targets address "
                f"0x{value:x}, which is not instruction-aligned"
            )
        low, high = RESOLVED_PCREL_RANGE
        if not low <= delta <= high:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} overflows the "
                f"signed 14-bit displacement: displacement 0x{delta:x}"
            )
        decoded = decode_instruction(word_at(site))
        if decoded is None:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} does not mark a "
                "branch or call instruction"
            )
        field_name = decoded.definition.operand("target").fields[0]
        width = decoded.definition.field_width(field_name)
        fields = dict(decoded.fields)
        fields[field_name] = zero_extend(delta, width)
        write_word(site, build_word(decoded.definition.format, fields))

    def rewrite_la64(site: int, symbol: Symbol, value: int) -> None:
        if not 0 <= value <= WORD_MASK:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} overflows the "
                f"64-bit address space: address 0x{value:x}"
            )
        decoded = decode_instruction(word_at(site))
        if decoded is None or decoded.mnemonic != "MOVI":
            raise LinkError(
                f"LA64 relocation of '{symbol.name}' at 0x{site:x} does not "
                "start at a MOVI instruction"
            )
        register = decoded.register("rd")
        for offset, line in enumerate(li_maximal(f"r{register}", value)):
            write_word(site + offset * INSTRUCTION_BYTES,
                       assemble_statement(line, 0, 0, {}))

    def write_abs64(site: int, symbol: Symbol, value: int) -> None:
        """Write an absolute 64-bit address into a ``.quad`` data word."""
        if not 0 <= value <= WORD_MASK:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} overflows the "
                f"64-bit address space: address 0x{value:x}"
            )
        if site % 8:
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} targets address "
                f"0x{value:x}, which is not 8-byte aligned"
            )
        offset = site - image_base
        if offset < 0 or offset + 8 > len(result):
            raise LinkError(
                f"relocation of '{symbol.name}' at 0x{site:x} is outside the "
                "linked image"
            )
        result[offset:offset + 8] = value.to_bytes(8, "little")

    for obj_index, obj in enumerate(inputs):
        for relocation in obj.relocations:
            if relocation.kind == _PCREL_KIND:
                apply = patch_pcrel
            elif relocation.kind == _LA64_KIND:
                apply = rewrite_la64
            elif relocation.kind == _ABS64_KIND:
                apply = write_abs64
            else:
                raise LinkError(
                    f"unsupported relocation kind {relocation.kind!r}"
                )
            symbol = obj.symbols[relocation.symbol]
            site = member_addrs[(obj_index, relocation.section)] + relocation.offset
            value = resolve_address(obj_index, symbol) + relocation.addend
            apply(site, symbol, value)

    # -- Output ---------------------------------------------------------------
    image_bytes = bytes(result)
    try:
        encode_nob(image_base=image_base, entry=entry_address, image=image_bytes)
    except NobError as exc:
        raise LinkError(f"linked output is not a valid .nob: {exc}") from exc
    return NobImage(image_base=image_base, entry=entry_address, image=image_bytes)