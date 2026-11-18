"""Nova executable container (``.nob``) format tests."""

from pathlib import Path

import pytest

from nova.fileformats import get_file_format
from nova.nob import (
    NOB_HEADER_SIZE,
    NOB_MAGIC,
    NOB_VERSION,
    NobError,
    decode_nob,
    encode_nob,
    load_nob,
    parse_address,
    write_nob,
)
from nova.isa import INSTRUCTION_BYTES

WORDS = bytes(range(32))  # a word-multiple dummy image
N_BYTES = len(WORDS)


def make_nob(*, base=0x1000, entry=0x1000, image=WORDS, flags=0):
    return encode_nob(image_base=base, entry=entry, image=image, flags=flags)


def test_encode_decode_roundtrip():
    blob = make_nob(base=0x1000, entry=0x1010)
    assert blob[:4] == NOB_MAGIC
    assert blob[4:6] == NOB_VERSION.to_bytes(2, "little")
    assert len(blob) == NOB_HEADER_SIZE + N_BYTES
    nob = decode_nob(blob)
    assert nob.image_base == 0x1000
    assert nob.entry == 0x1010
    assert nob.image == WORDS
    assert nob.flags == 0
    assert nob.image_size == N_BYTES


def test_entry_may_differ_from_image_base():
    blob = make_nob(base=0x1000, entry=0x1004)
    assert decode_nob(blob).entry == 0x1004


def test_write_and_load_roundtrip(tmp_path):
    path = Path(tmp_path) / "program.nob"
    write_nob(path, image_base=0x1000, entry=0x1000, image=WORDS)
    nob = load_nob(path)
    assert nob.image == WORDS
    assert nob.image_base == 0x1000


def test_empty_image_requires_entry_equal_to_base():
    blob = make_nob(image=b"", base=0x1000, entry=0x1000)
    assert decode_nob(blob).image_size == 0
    with pytest.raises(NobError, match="empty image"):
        make_nob(image=b"", base=0x1000, entry=0x1004)


def test_bad_magic_is_rejected():
    blob = bytearray(make_nob())
    blob[0:4] = b"XXXX"
    with pytest.raises(NobError, match="magic"):
        decode_nob(bytes(blob))


def test_unsupported_version_is_rejected():
    blob = bytearray(make_nob())
    blob[4:6] = (2).to_bytes(2, "little")
    with pytest.raises(NobError, match="version"):
        decode_nob(bytes(blob))


def test_reserved_flags_must_be_zero():
    with pytest.raises(NobError, match="flags"):
        make_nob(flags=0x1)
    blob = bytearray(make_nob())
    blob[6:8] = (0x2).to_bytes(2, "little")
    with pytest.raises(NobError, match="flags"):
        decode_nob(bytes(blob))


def test_truncated_header_is_rejected():
    with pytest.raises(NobError, match="truncated"):
        decode_nob(b"NOBV\x01\x00")


def test_truncated_image_is_rejected():
    blob = make_nob()[:-4]
    with pytest.raises(NobError, match="truncated"):
        decode_nob(blob)


def test_trailing_bytes_are_rejected():
    blob = make_nob() + b"\x00" * 4
    with pytest.raises(NobError, match="trailing"):
        decode_nob(blob)


def test_impossible_image_size_is_rejected():
    blob = bytearray(make_nob())
    blob[24:32] = (N_BYTES * 2).to_bytes(8, "little")
    with pytest.raises(NobError, match="truncated"):
        decode_nob(bytes(blob))


def test_image_size_must_be_a_word_multiple():
    with pytest.raises(NobError, match="multiple of 4"):
        make_nob(image=b"\x00\x00")  # 2 bytes
    with pytest.raises(NobError, match="multiple of 4"):
        make_nob(image=b"\x00" * 6)


def test_image_base_and_entry_must_be_aligned():
    with pytest.raises(NobError, match="aligned"):
        make_nob(base=0x1001)
    with pytest.raises(NobError, match="aligned"):
        make_nob(base=0x1000, entry=0x1002)


def test_account_for_the_alignment_constant():
    assert INSTRUCTION_BYTES == 4


def test_entry_must_lie_within_the_image():
    with pytest.raises(NobError, match="outside the image"):
        make_nob(base=0x1000, entry=0x1000 + N_BYTES)
    with pytest.raises(NobError, match="outside the image"):
        make_nob(base=0x1000, entry=0x0FF0)


def test_image_may_not_wrap_the_address_space():
    with pytest.raises(NobError, match="address space"):
        make_nob(base=2**64 - 4, image=b"\x00" * 8)
    header = (
        NOB_MAGIC
        + NOB_VERSION.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (2**64 - 4).to_bytes(8, "little")   # image_base
        + (2**64 - 4).to_bytes(8, "little")   # entry
        + (8).to_bytes(8, "little")           # image_size
    )
    with pytest.raises(NobError, match="address space"):
        decode_nob(header + b"\x00" * 8)


def test_images_may_carry_left_to_right_from_the_base():
    blob = make_nob(base=0x2000, entry=0x2000)
    assert decode_nob(blob).image_base == 0x2000


def test_registry_describes_the_executable_container():
    fmt = get_file_format("program.nob")
    assert fmt.kind == "executable"
    assert fmt.magic == NOB_MAGIC
    assert fmt.implemented


def test_load_nob_reports_missing_files():
    with pytest.raises(OSError):
        load_nob(Path("no-such-file.nob"))


@pytest.mark.parametrize("text,expected", [
    ("0x1000", 0x1000),
    ("4096", 4096),
    ("0b10000", 16),
    ("0o4000", 2048),
    ("#0x2000", 0x2000),
    ("$0x2000", 0x2000),
    ("-4", -4),
    ("0x8_000", 0x8000),
])
def test_parse_address_literals(text, expected):
    assert parse_address(text) == expected


@pytest.mark.parametrize("text", ["hello", "0x", "", "1.5"])
def test_parse_address_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_address(text)


def test_writer_rejects_non_byte_images():
    with pytest.raises(NobError):
        encode_nob(image_base=0x1000, entry=0x1000, image=[1, 2, 3, 4])