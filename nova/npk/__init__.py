"""NPK — Nova Processed Kernel.

Phase 2 Milestone 1 provides the architecture contract, a deterministic kernel
build, the minimal boot-verification processing pipeline, and host-side boot and
receipt helpers. It deliberately does not provide a trap frame, allocator,
syscall dispatcher, devices, scheduler, or user loader.
"""

from __future__ import annotations

from .boot import BootResult, NpkBootError, boot_kernel
from .build import NpkBuildError, build_kernel
from .image import NpkImageError, load_kernel_image, validate_kernel_image
from .processing import (
    BOOT_VERIFY_INPUT,
    BOOT_VERIFY_OPERATION,
    BOOT_VERIFY_OUTPUT,
    EXPECTED_BOOT_TRACE,
    PROCESSING_MAGIC,
    STAGE_ADMIT,
    STAGE_COMMIT,
    STAGE_COMPLETE,
    STAGE_EXECUTE,
    STAGE_VALIDATE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSED,
    STATUS_REJECTED,
    NpkProcessingError,
    ProcessingReceipt,
    decode_processing_receipt,
    validate_boot_receipt,
)
from .layout import (
    BOOT_MARKER_ADDRESS,
    BOOT_MARKER_SIZE,
    BOOT_MARKER_VALUE,
    BOOT_PRIVILEGE,
    PROCESSING_RECEIPT_ADDRESS,
    PROCESSING_RECEIPT_SIZE,
    BOOT_STACK_GUARD,
    BOOT_STACK_TOP,
    KERNEL_ENTRY_SYMBOL,
    KERNEL_IMAGE_BASE,
    MIN_KERNEL_IMAGE_BYTES,
    NPK_VERSION,
)

__all__ = [
    "NPK_VERSION",
    "KERNEL_IMAGE_BASE",
    "KERNEL_ENTRY_SYMBOL",
    "MIN_KERNEL_IMAGE_BYTES",
    "BOOT_STACK_TOP",
    "BOOT_STACK_GUARD",
    "BOOT_MARKER_ADDRESS",
    "BOOT_MARKER_VALUE",
    "BOOT_MARKER_SIZE",
    "PROCESSING_RECEIPT_ADDRESS",
    "PROCESSING_RECEIPT_SIZE",
    "BOOT_PRIVILEGE",
    "PROCESSING_MAGIC",
    "BOOT_VERIFY_INPUT",
    "BOOT_VERIFY_OPERATION",
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
    "NpkBuildError",
    "NpkImageError",
    "NpkProcessingError",
    "NpkBootError",
    "BootResult",
    "ProcessingReceipt",
    "build_kernel",
    "load_kernel_image",
    "validate_kernel_image",
    "boot_kernel",
    "decode_processing_receipt",
    "validate_boot_receipt",
]
