"""NPK Milestone 1: layout, deterministic build, boot, and processing.

These tests use the real Phase 1 assembler, compiler, linker, ``.nob`` reader,
and NovaMachine emulator. The boot helper and receipt decoder are host-side
harness code, not kernel APIs.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.emulator import NovaMachine
from nova.isa import EXCEPTION_CODES, PRIVILEGE_LEVELS
from nova.nob import NOB_HEADER_SIZE, NobImage, decode_nob, load_nob


def test_layout_constants_define_the_boot_contract():
    assert npk.NPK_VERSION == "NPK Phase 2 foundation"
    assert npk.KERNEL_IMAGE_BASE == 0x1000
    assert npk.KERNEL_ENTRY_SYMBOL == "_npk_start"
    assert npk.BOOT_STACK_TOP == 0x80000
    assert npk.BOOT_MARKER_ADDRESS == 0x90000
    assert npk.BOOT_MARKER_VALUE == 0x4E504B31
    assert npk.BOOT_MARKER_SIZE == 8
    assert npk.PROCESSING_RECEIPT_ADDRESS == 0x90008
    assert npk.PROCESSING_RECEIPT_SIZE == 96
    assert npk.PROCESSING_MAGIC == 0x4E504B50
    assert npk.EXPECTED_BOOT_TRACE == (1, 2, 3, 4, 5)
    assert npk.BOOT_PRIVILEGE == "machine"
    assert npk.BOOT_STACK_TOP - npk.BOOT_STACK_GUARD > npk.KERNEL_IMAGE_BASE


def test_build_kernel_is_deterministic(tmp_path):
    first = npk.build_kernel(tmp_path / "first")
    second = npk.build_kernel(tmp_path / "second")

    assert first.is_file() and second.is_file()
    assert first.name == "npk.nob"
    assert first.read_bytes() == second.read_bytes()

    image = load_nob(first)
    npk.validate_kernel_image(image)
    assert image.image_base == npk.KERNEL_IMAGE_BASE
    assert image.entry == npk.KERNEL_IMAGE_BASE
    assert image.flags == 0


def test_build_kernel_removes_intermediate_objects_by_default(tmp_path):
    image = npk.build_kernel(tmp_path / "image")
    assert image.is_file()
    assert not (tmp_path / "image" / "npk_start.nobj").exists()
    assert not (tmp_path / "image" / "npk_processing.nobj").exists()
    assert not (tmp_path / "image" / "npk_kernel.nobj").exists()


def test_build_kernel_can_keep_intermediate_objects(tmp_path):
    image = npk.build_kernel(tmp_path / "image", keep_objects=True)
    assert image.is_file()
    assert (tmp_path / "image" / "npk_start.nobj").is_file()
    assert (tmp_path / "image" / "npk_processing.nobj").is_file()
    assert (tmp_path / "image" / "npk_kernel.nobj").is_file()


def test_boot_contract_is_observable_and_deterministic(tmp_path):
    image_path = npk.build_kernel(tmp_path / "boot")
    image = npk.load_kernel_image(image_path)
    result = npk.boot_kernel(image)

    assert isinstance(result.machine, NovaMachine)
    assert result.steps > 0
    assert result.status == 0
    assert result.marker == npk.BOOT_MARKER_VALUE
    assert result.processing == npk.ProcessingReceipt(
        magic=npk.PROCESSING_MAGIC,
        operation=npk.BOOT_VERIFY_OPERATION,
        stage=npk.STAGE_COMPLETE,
        status=npk.STATUS_PROCESSED,
        input=npk.BOOT_VERIFY_INPUT,
        output=npk.BOOT_VERIFY_OUTPUT,
        trace=npk.EXPECTED_BOOT_TRACE,
    )
    assert result.stack_pointer == npk.BOOT_STACK_TOP
    assert image.image_base <= result.vbar < image.image_base + len(image.image)
    assert result.machine.halted is True
    assert result.machine.priv == PRIVILEGE_LEVELS["machine"]
    assert result.machine.exception_code == EXCEPTION_CODES["NONE"]


def test_processing_receipt_validation_rejects_a_skipped_stage():
    receipt = npk.ProcessingReceipt(
        magic=npk.PROCESSING_MAGIC,
        operation=npk.BOOT_VERIFY_OPERATION,
        stage=npk.STAGE_COMPLETE,
        status=npk.STATUS_PROCESSED,
        input=npk.BOOT_VERIFY_INPUT,
        output=npk.BOOT_VERIFY_OUTPUT,
        trace=(npk.STAGE_ADMIT, npk.STAGE_VALIDATE, npk.STAGE_COMMIT,
               npk.STAGE_COMMIT, npk.STAGE_COMPLETE),
    )

    with pytest.raises(npk.NpkProcessingError, match="stage trace"):
        npk.validate_boot_receipt(receipt)


def test_boot_rejects_an_undefined_instruction(tmp_path):
    image_path = npk.build_kernel(tmp_path / "bad")
    data = bytearray(image_path.read_bytes())
    data[NOB_HEADER_SIZE:NOB_HEADER_SIZE + 4] = (0xFFFFFFFF).to_bytes(4, "little")
    image = decode_nob(bytes(data))

    with pytest.raises(npk.NpkBootError, match="unexpected exception"):
        npk.boot_kernel(image)


def test_image_validation_rejects_wrong_base_and_bad_flags(tmp_path):
    image = npk.load_kernel_image(npk.build_kernel(tmp_path / "image"))

    wrong_base = NobImage(
        image_base=image.image_base + 0x1000,
        entry=image.entry + 0x1000,
        image=image.image,
    )
    with pytest.raises(npk.NpkImageError, match="image_base"):
        npk.validate_kernel_image(wrong_base)

    bad_flags = NobImage(
        image_base=image.image_base,
        entry=image.entry,
        image=image.image,
        flags=1,
    )
    with pytest.raises(npk.NpkImageError, match="flags"):
        npk.validate_kernel_image(bad_flags)


def test_image_validation_rejects_non_nob_input():
    with pytest.raises(npk.NpkImageError, match="NobImage"):
        npk.validate_kernel_image(b"NOBV")
