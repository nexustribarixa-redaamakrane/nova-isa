"""Nova debug information container (``.nox``), version 1.

This is a small, deterministic, architecture-neutral record format that carries
the metadata a compiler needs for source mapping and call-frame description. It
is deliberately not ELF/DWARF: Nova objects and executables have their own
containers. The record layout is documented in ``README.md`` and in
``docs/NOVA_C_ABI.md``.

Debug records use explicit section-relative byte offsets instead of implied
"current address" semantics, because Nova pseudo-instructions expand to a
variable number of words and layout is decided by the assembler.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Tuple

__all__ = [
    "NOX_MAGIC",
    "NOX_VERSION",
    "NOX_HEADER_SIZE",
    "NoxError",
    "SourceFile",
    "SourceLocation",
    "FrameDescription",
    "RegisterSave",
    "DebugInfo",
    "encode_debug",
    "decode_debug",
]

NOX_MAGIC = b"NOVADBG\x00"
NOX_VERSION = 1
NOX_HEADER_SIZE = 24

_KIND_FILE = 1
_KIND_LOCATION = 2
_KIND_FRAME = 3
_KIND_REGISTER_SAVE = 4

_HEADER = struct.Struct("<8sHHII4s")
_FILE = struct.Struct("<I")
_LOCATION = struct.Struct("<QIII")
_FRAME = struct.Struct("<QQIi")
_SAVE = struct.Struct("<QIi")


class NoxError(ValueError):
    """Raised when a .nox container or one of its records is invalid."""


@dataclass(frozen=True)
class SourceFile:
    """A source file index and its path."""

    index: int
    path: str


@dataclass(frozen=True)
class SourceLocation:
    """A section-relative offset mapped to a source position."""

    offset: int
    file: int
    line: int
    column: int = 0


@dataclass(frozen=True)
class FrameDescription:
    """Call-frame description for one function body."""

    name: str
    start: int
    end: int
    cfa_register: int
    cfa_offset: int


@dataclass(frozen=True)
class RegisterSave:
    """A register saved at ``cfa + offset`` at a given instruction offset."""

    offset: int
    register: int
    save_offset: int


@dataclass(frozen=True)
class DebugInfo:
    """All debug records for one compilation unit."""

    files: Tuple[SourceFile, ...] = ()
    locations: Tuple[SourceLocation, ...] = ()
    frames: Tuple[FrameDescription, ...] = ()
    register_saves: Tuple[RegisterSave, ...] = ()

    def __post_init__(self) -> None:
        if len({item.index for item in self.files}) != len(self.files):
            raise NoxError("duplicate source file index")
        if any(len(item.path.encode("utf-8")) > 65535 for item in self.files):
            raise NoxError("source path is too long")
        for item in self.locations:
            if item.offset < 0 or item.file < 0 or item.line < 0 or item.column < 0:
                raise NoxError("negative source location field")
        for item in self.frames:
            if item.start > item.end:
                raise NoxError(f"frame {item.name!r} ends before it starts")
            if item.cfa_register < 0:
                raise NoxError(f"frame {item.name!r} has a negative register")
        for item in self.register_saves:
            if item.offset < 0 or item.register < 0:
                raise NoxError("negative register-save field")


def encode_debug(info: DebugInfo) -> bytes:
    """Serialize debug records deterministically (sorted by record key)."""
    if not isinstance(info, DebugInfo):
        raise NoxError("debug information must be a DebugInfo")
    body = bytearray()

    def record(kind: int, payload: bytes) -> None:
        body.extend(struct.pack("<BI", kind, len(payload)))
        body.extend(payload)

    for item in sorted(info.files, key=lambda item: item.index):
        path = item.path.encode("utf-8")
        record(_KIND_FILE, _FILE.pack(item.index) + path)
    for item in sorted(info.locations,
                       key=lambda item: (item.offset, item.file, item.line,
                                         item.column)):
        record(_KIND_LOCATION, _LOCATION.pack(item.offset, item.file,
                                              item.line, item.column))
    for item in sorted(info.frames, key=lambda item: (item.start, item.name)):
        name = item.name.encode("utf-8")
        record(_KIND_FRAME, _FRAME.pack(item.start, item.end, item.cfa_register,
                                        item.cfa_offset) + name)
    for item in sorted(info.register_saves,
                       key=lambda item: (item.offset, item.register)):
        record(_KIND_REGISTER_SAVE, _SAVE.pack(item.offset, item.register,
                                              item.save_offset))

    header = _HEADER.pack(NOX_MAGIC, NOX_VERSION, 0, len(body), 0, b"\x00" * 4)
    return header + bytes(body)


def decode_debug(data: bytes | bytearray | memoryview) -> DebugInfo:
    """Parse and validate a .nox container."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise NoxError("a .nox container must be a byte string")
    blob = bytes(data)
    if len(blob) < NOX_HEADER_SIZE:
        raise NoxError("truncated .nox header")
    magic, version, flags, body_size, _unit_count, reserved = _HEADER.unpack_from(blob, 0)
    if magic != NOX_MAGIC:
        raise NoxError(f"bad .nox magic: {magic!r}")
    if version != NOX_VERSION:
        raise NoxError(f"unsupported .nox version: {version}")
    if flags or any(reserved):
        raise NoxError("nonzero .nox header flags")
    if len(blob) != NOX_HEADER_SIZE + body_size:
        raise NoxError(".nox body size does not match the header")

    files: List[SourceFile] = []
    locations: List[SourceLocation] = []
    frames: List[FrameDescription] = []
    saves: List[RegisterSave] = []
    offset = NOX_HEADER_SIZE
    end = len(blob)
    while offset < end:
        if offset + 5 > end:
            raise NoxError("truncated .nox record header")
        kind, size = struct.unpack_from("<BI", blob, offset)
        offset += 5
        if offset + size > end:
            raise NoxError("truncated .nox record body")
        payload = blob[offset:offset + size]
        offset += size
        if kind == _KIND_FILE:
            if size < _FILE.size:
                raise NoxError("short .nox file record")
            index = _FILE.unpack_from(payload, 0)[0]
            files.append(SourceFile(index=index,
                                     path=bytes(payload[_FILE.size:]).decode("utf-8")))
        elif kind == _KIND_LOCATION:
            if size != _LOCATION.size:
                raise NoxError("invalid .nox location record size")
            locations.append(SourceLocation(*_LOCATION.unpack(payload)))
        elif kind == _KIND_FRAME:
            if size < _FRAME.size:
                raise NoxError("short .nox frame record")
            start, stop, reg, cfa = _FRAME.unpack_from(payload, 0)
            frames.append(FrameDescription(
                name=bytes(payload[_FRAME.size:]).decode("utf-8"),
                start=start, end=stop, cfa_register=reg, cfa_offset=cfa,
            ))
        elif kind == _KIND_REGISTER_SAVE:
            if size != _SAVE.size:
                raise NoxError("invalid .nox register-save record size")
            saves.append(RegisterSave(*_SAVE.unpack(payload)))
        else:
            raise NoxError(f"unknown .nox record kind {kind}")

    info = DebugInfo(files=tuple(files), locations=tuple(locations),
                     frames=tuple(frames), register_saves=tuple(saves))
    # Re-encoding must be byte-identical: this rejects duplicate or unordered
    # records that would otherwise make encodings ambiguous.
    if encode_debug(info) != blob:
        raise NoxError(".nox records are duplicated or out of order")
    return info
