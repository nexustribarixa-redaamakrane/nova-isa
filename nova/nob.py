"""Nova executable container format v1 (``.nob``).

The image is a flat little-endian byte string of instructions and data. This
module only defines the container: it has no knowledge of instruction encoding,
which stays in :mod:`nova.isa` and :mod:`nova.assembler`.

Byte-for-byte layout, with all multi-byte fields little-endian::

    offset  size  field
    0       4     magic       ASCII ``NOBV``
    4       2     version     ``1``
    6       2     flags       reserved, must be ``0``
    8       8     image_base  load address of image byte 0
    16      8     entry       address execution starts at
    24      8     image_size  exact number of image bytes
    32      N     image       the instruction/data image

``image_base`` is the architectural address where byte 0 of the image is
loaded; it is *not* implied by ``entry``. ``entry`` is where execution begins
and may differ from ``image_base``. Validity rules, all enforced by the reader
and the writer:

- ``image_size`` must equal the exact number of image bytes; a valid file is
  ``32 + image_size`` bytes long.
- ``image_size`` must be a multiple of four bytes (instruction alignment).
- ``image_base`` and ``entry`` must be 4-byte aligned unsigned 64-bit
  addresses, and ``image_base + image_size`` must not leave the address space.
- ``entry`` must lie within the loaded image range ``[image_base,
  image_base + image_size)``; an empty image must have ``entry == image_base``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from .isa import INSTRUCTION_BYTES, WORD_MASK

__all__ = [
    "NOB_HEADER_SIZE",
    "NOB_MAGIC",
    "NOB_VERSION",
    "NobError",
    "NobImage",
    "decode_nob",
    "encode_nob",
    "load_nob",
    "parse_address",
    "write_nob",
]

#: Four ASCII magic bytes at offset 0.
NOB_MAGIC = b"NOBV"
#: Current container version at offset 4.
NOB_VERSION = 1
#: Total header size in bytes.
NOB_HEADER_SIZE = 32

_ADDRESS_RE = re.compile(
    r"^[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)$"
)


class NobError(ValueError):
    """Raised when a ``.nob`` container is invalid or cannot be constructed."""


@dataclass(frozen=True)
class NobImage:
    """A validated Nova executable container."""

    image_base: int
    entry: int
    image: bytes
    flags: int = 0

    @property
    def image_size(self) -> int:
        return len(self.image)


def parse_address(token: str) -> int:
    """Parse a decimal, hex, binary, or octal address literal for the CLIs."""
    text = token.strip()
    if text[:1] in ("#", "$"):
        text = text[1:].strip()
    if not _ADDRESS_RE.fullmatch(text):
        raise ValueError(f"invalid address literal: {token!r}")
    return int(text.replace("_", ""), 0)


def _validate(image_base: int, entry: int, image_size: int, flags: int) -> None:
    if flags != 0:
        raise NobError(f"unsupported .nob flags: 0x{flags:x}")
    if image_size % INSTRUCTION_BYTES:
        raise NobError(
            f".nob image_size {image_size} is not a multiple of "
            f"{INSTRUCTION_BYTES} bytes"
        )
    if not isinstance(image_base, int) or not 0 <= image_base <= WORD_MASK:
        raise NobError(f"image_base must be an unsigned 64-bit address: {image_base!r}")
    if not isinstance(entry, int) or not 0 <= entry <= WORD_MASK:
        raise NobError(f"entry must be an unsigned 64-bit address: {entry!r}")
    if image_base % INSTRUCTION_BYTES:
        raise NobError(
            f"image_base 0x{image_base:x} is not {INSTRUCTION_BYTES}-byte aligned"
        )
    if entry % INSTRUCTION_BYTES:
        raise NobError(f"entry 0x{entry:x} is not {INSTRUCTION_BYTES}-byte aligned")
    if image_size and image_base + image_size - 1 > WORD_MASK:
        raise NobError("image exceeds the 64-bit address space")
    if image_size == 0:
        if entry != image_base:
            raise NobError(
                f"empty image: entry 0x{entry:x} must equal image_base "
                f"0x{image_base:x}"
            )
        return
    if not image_base <= entry < image_base + image_size:
        raise NobError(
            f"entry 0x{entry:x} is outside the image range "
            f"[0x{image_base:x}, 0x{image_base + image_size:x})"
        )


def encode_nob(*, image_base: int, entry: int, image: bytes,
               flags: int = 0) -> bytes:
    """Serialize a validated ``.nob`` executable."""
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise NobError("image must be a byte string")
    image = bytes(image)
    _validate(image_base, entry, len(image), flags)
    header = (
        NOB_MAGIC
        + NOB_VERSION.to_bytes(2, "little")
        + flags.to_bytes(2, "little")
        + image_base.to_bytes(8, "little")
        + entry.to_bytes(8, "little")
        + len(image).to_bytes(8, "little")
    )
    return header + image


def decode_nob(data: Union[bytes, bytearray, memoryview]) -> NobImage:
    """Parse and validate ``.nob`` bytes, returning :class:`NobImage`."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise NobError("a .nob executable must be a byte string")
    data = bytes(data)
    if len(data) < NOB_HEADER_SIZE:
        raise NobError(
            f"truncated .nob header: {len(data)} bytes, expected "
            f"{NOB_HEADER_SIZE}"
        )
    if data[:4] != NOB_MAGIC:
        raise NobError(
            f"bad .nob magic: {data[:4]!r}; expected {NOB_MAGIC!r}"
        )
    version = int.from_bytes(data[4:6], "little")
    if version != NOB_VERSION:
        raise NobError(f"unsupported .nob version {version}; expected {NOB_VERSION}")
    flags = int.from_bytes(data[6:8], "little")
    image_base = int.from_bytes(data[8:16], "little")
    entry = int.from_bytes(data[16:24], "little")
    image_size = int.from_bytes(data[24:32], "little")
    _validate(image_base, entry, image_size, flags)
    before = data[NOB_HEADER_SIZE:]
    if len(before) < image_size:
        raise NobError(
            f"truncated .nob image: header claims {image_size} bytes, "
            f"found {len(before)}"
        )
    if len(before) > image_size:
        raise NobError(
            f".nob trailing bytes: header claims {image_size} image bytes, "
            f"found {len(before)}"
        )
    return NobImage(
        image_base=image_base,
        entry=entry,
        image=before[:image_size],
        flags=flags,
    )


def write_nob(path: str | Path, *, image_base: int, entry: int, image: bytes,
              flags: int = 0) -> None:
    """Write a validated ``.nob`` executable to ``path``."""
    Path(path).write_bytes(encode_nob(image_base=image_base, entry=entry,
                                      image=image, flags=flags))


def load_nob(path: str | Path) -> NobImage:
    """Read and validate a ``.nob`` executable from disk."""
    return decode_nob(Path(path).read_bytes())