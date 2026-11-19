"""NPK boot-time layout constants.

These values are part of the Phase 2 Milestone 1 boot contract. They are not
new ISA constants and do not alter Nova ISA v1.0 or the Phase 1 C ABI.
"""

from __future__ import annotations

NPK_VERSION = "NPK Phase 2 foundation"

#: Load address of image byte 0, inherited from the Nova toolchain default.
KERNEL_IMAGE_BASE = 0x1000
#: Global symbol resolved by the linker as the kernel entry point.
KERNEL_ENTRY_SYMBOL = "_npk_start"
#: Smallest image that can contain a credible NPK boot stub.
MIN_KERNEL_IMAGE_BYTES = 64

#: Initial kernel stack top. The stack grows down; the image must stay below it.
BOOT_STACK_TOP = 0x80000
#: Reserved gap beneath the boot stack.
BOOT_STACK_GUARD = 0x10000

#: Eight-byte early-boot handshake word written by ``_npk_start``.
BOOT_MARKER_ADDRESS = 0x90000
BOOT_MARKER_VALUE = 0x4E504B31  # "NPK1" in little-endian byte order
BOOT_MARKER_SIZE = 8

#: Fixed 96-byte processing receipt published by the kernel pipeline.
PROCESSING_RECEIPT_ADDRESS = BOOT_MARKER_ADDRESS + BOOT_MARKER_SIZE
PROCESSING_RECEIPT_SIZE = 96

#: Privilege level selected by the loader before entering the kernel.
BOOT_PRIVILEGE = "machine"

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
]
