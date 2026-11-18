"""Deterministic serialization for persistent Nova object identities.

The binary layout is part of the Nova ISA v1.0 architectural contract. A
reference is a 64-bit value mixing a generation and an index; it stays
unchanged across export and import, so it can be used as a persistent object
identity by software in a different process or on a different machine.
"""

from __future__ import annotations

import struct
import zlib
from typing import List, Mapping

from .isa import (
    OBJECT_FLAG_EXTERNAL,
    OBJECT_FORMAT_VERSION,
    OBJECT_GENERATION_MASK,
    OBJECT_HEADER_SIZE,
    OBJECT_INDEX_MASK,
    OBJECT_MAGIC,
    OBJECT_PERM_MASK,
    OBJECT_RECORD_SIZE,
    OBJECT_TRAILER_SIZE,
    WORD_MASK,
    ObjectFormatError,
    split_object_reference,
)

__all__ = ["encode_objects", "decode_objects"]

_HEADER = struct.Struct("<4sHHII")   # magic, version, flags, count, reserved
_RECORD = struct.Struct("<QQQQ")     # reference, size, permissions, flags
_TRAILER = struct.Struct("<II")      # checksum, reserved

_RECORD_KEYS = ("reference", "size", "permissions", "flags")
_REQUIRED_KEYS = frozenset(_RECORD_KEYS)


def _validate_record(record: Mapping[str, int], *, index: int) -> None:
    if frozenset(record) != _REQUIRED_KEYS:
        raise ObjectFormatError(f"record {index} has missing or extra fields")
    for key in _RECORD_KEYS:
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ObjectFormatError(f"record {index} field {key} must be an integer")
        if not 0 <= value <= WORD_MASK:
            raise ObjectFormatError(f"record {index} field {key} is not 64-bit")
    split_object_reference(record["reference"])
    flags = record["flags"]
    if flags & ~OBJECT_FLAG_EXTERNAL:
        raise ObjectFormatError(f"record {index} has unknown flags: {flags:#x}")
    if not flags and record["permissions"] & ~OBJECT_PERM_MASK:
        raise ObjectFormatError(f"record {index} has unknown permissions")


def encode_objects(records: List[Mapping[str, int]]) -> bytes:
    """Serialize object records deterministically.

    Records are sorted by reference and must be unique. ``flags`` marks an
    external reference that must already exist in the importing machine.
    """
    for index, record in enumerate(records):
        _validate_record(record, index=index)
    ordered = sorted(records, key=lambda record: record["reference"])
    references = [record["reference"] for record in ordered]
    if len(references) != len(set(references)):
        raise ObjectFormatError("duplicate object reference in export set")
    if len(ordered) > 0xFFFFFFFF:
        raise ObjectFormatError("too many object records")

    body = bytearray(_HEADER.pack(OBJECT_MAGIC, OBJECT_FORMAT_VERSION, 0,
                                  len(ordered), 0))
    for record in ordered:
        body.extend(_RECORD.pack(record["reference"], record["size"],
                                 record["permissions"], record["flags"]))
    checksum = zlib.crc32(bytes(body)) & 0xFFFFFFFF
    return bytes(body) + _TRAILER.pack(checksum, 0)


def decode_objects(data: bytes) -> List[dict]:
    """Parse and validate an external object container."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ObjectFormatError("object container must be a byte string")
    data = bytes(data)
    minimum = OBJECT_HEADER_SIZE + OBJECT_TRAILER_SIZE
    if len(data) < minimum:
        raise ObjectFormatError("object container is truncated")

    magic, version, flags, count, reserved = _HEADER.unpack_from(data, 0)
    if magic != OBJECT_MAGIC:
        raise ObjectFormatError(f"bad object container magic: {magic!r}")
    if version > OBJECT_FORMAT_VERSION:
        raise ObjectFormatError(f"unsupported object format version: {version}")
    if version != OBJECT_FORMAT_VERSION:
        raise ObjectFormatError(f"obsolete object format version: {version}")
    if flags or reserved:
        raise ObjectFormatError("object container has nonzero header flags")

    expected = minimum + count * OBJECT_RECORD_SIZE
    if len(data) != expected:
        raise ObjectFormatError("object container length does not match record count")
    checksum, trailer_reserved = _TRAILER.unpack_from(data, len(data) - OBJECT_TRAILER_SIZE)
    if trailer_reserved:
        raise ObjectFormatError("object container has nonzero trailer reserved field")
    if zlib.crc32(data[:len(data) - OBJECT_TRAILER_SIZE]) & 0xFFFFFFFF != checksum:
        raise ObjectFormatError("object container checksum mismatch")

    records: List[dict] = []
    for index in range(count):
        offset = OBJECT_HEADER_SIZE + index * OBJECT_RECORD_SIZE
        reference, size, permissions, rec_flags = _RECORD.unpack_from(data, offset)
        record = {
            "reference": reference,
            "size": size,
            "permissions": permissions,
            "flags": rec_flags,
        }
        _validate_record(record, index=index)
        records.append(record)
    references = [record["reference"] for record in records]
    if len(references) != len(set(references)):
        raise ObjectFormatError("duplicate object reference in container")
    return records
