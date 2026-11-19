"""Host-side decoding of NPK processing receipts.

The receipt is written by kernel C code and is the observable proof that a
kernel operation traversed the NPK processing pipeline. This module is a test
and harness layer; it is not part of the guest kernel ABI.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..emulator import NovaMachine
from .layout import BOOT_MARKER_VALUE, PROCESSING_RECEIPT_ADDRESS

__all__ = [
    "PROCESSING_MAGIC",
    "BOOT_VERIFY_OPERATION",
    "BOOT_VERIFY_INPUT",
    "BOOT_VERIFY_OUTPUT",
    "STAGE_ADMIT",
    "STAGE_VALIDATE",
    "STAGE_EXECUTE",
    "STAGE_COMMIT",
    "STAGE_COMPLETE",
    "STATUS_PENDING",
    "STATUS_PROCESSED",
    "STATUS_REJECTED",
    "STATUS_FAILED",
    "EXPECTED_BOOT_TRACE",
    "NpkProcessingError",
    "ProcessingReceipt",
    "decode_processing_receipt",
    "validate_boot_receipt",
]


PROCESSING_MAGIC = 0x4E504B50  # "NPKP"
BOOT_VERIFY_OPERATION = 1
BOOT_VERIFY_INPUT = BOOT_MARKER_VALUE
BOOT_VERIFY_OUTPUT = 0x4E504B32  # "NPK2"

STAGE_ADMIT = 1
STAGE_VALIDATE = 2
STAGE_EXECUTE = 3
STAGE_COMMIT = 4
STAGE_COMPLETE = 5

STATUS_PENDING = 0
STATUS_PROCESSED = 1
STATUS_REJECTED = 2
STATUS_FAILED = 3

EXPECTED_BOOT_TRACE = (
    STAGE_ADMIT,
    STAGE_VALIDATE,
    STAGE_EXECUTE,
    STAGE_COMMIT,
    STAGE_COMPLETE,
)


class NpkProcessingError(ValueError):
    """Raised when a processing receipt does not satisfy its contract."""


@dataclass(frozen=True)
class ProcessingReceipt:
    """Decoded kernel-to-harness processing result."""

    magic: int
    operation: int
    stage: int
    status: int
    input: int
    output: int
    trace: tuple[int, ...]


def decode_processing_receipt(machine: NovaMachine) -> ProcessingReceipt:
    """Read the fixed NPK processing receipt from machine memory."""

    def word(offset: int) -> int:
        return machine._read_mem(PROCESSING_RECEIPT_ADDRESS + offset, 8)

    return ProcessingReceipt(
        magic=word(0),
        operation=word(8),
        stage=word(16),
        status=word(24),
        input=word(32),
        output=word(40),
        trace=tuple(word(48 + index * 8) for index in range(5)),
    )


def validate_boot_receipt(receipt: ProcessingReceipt) -> None:
    """Validate the receipt produced by the Milestone 1 boot operation."""
    if receipt.magic != PROCESSING_MAGIC:
        raise NpkProcessingError(
            f"processing magic mismatch: expected 0x{PROCESSING_MAGIC:x}, "
            f"found 0x{receipt.magic:x}"
        )
    if receipt.operation != BOOT_VERIFY_OPERATION:
        raise NpkProcessingError(
            f"unexpected operation code: {receipt.operation}, expected "
            f"{BOOT_VERIFY_OPERATION}"
        )
    if receipt.input != BOOT_VERIFY_INPUT:
        raise NpkProcessingError(
            f"processing input mismatch: expected 0x{BOOT_VERIFY_INPUT:x}, "
            f"found 0x{receipt.input:x}"
        )
    if receipt.status != STATUS_PROCESSED:
        raise NpkProcessingError(
            f"boot operation did not process successfully: status="
            f"{receipt.status}, stage={receipt.stage}, trace={receipt.trace}"
        )
    if receipt.stage != STAGE_COMPLETE:
        raise NpkProcessingError(
            f"boot operation stopped at stage {receipt.stage}, expected "
            f"{STAGE_COMPLETE}"
        )
    if receipt.output != BOOT_VERIFY_OUTPUT:
        raise NpkProcessingError(
            f"processing output mismatch: expected 0x{BOOT_VERIFY_OUTPUT:x}, "
            f"found 0x{receipt.output:x}"
        )
    if receipt.trace != EXPECTED_BOOT_TRACE:
        raise NpkProcessingError(
            f"processing stage trace is {receipt.trace}, expected "
            f"{EXPECTED_BOOT_TRACE}"
        )
