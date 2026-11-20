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

#: Fixed 96-byte Milestone 1 receipt preserved for boot compatibility.
PROCESSING_RECEIPT_ADDRESS = BOOT_MARKER_ADDRESS + BOOT_MARKER_SIZE
PROCESSING_RECEIPT_SIZE = 96

#: Milestone 2 reusable processing-core observation regions.
CORE_STATE_ADDRESS = 0x90080
CORE_STATE_SIZE = 144
OP_SNAPSHOT_ADDRESS = 0x90120
OP_SNAPSHOT_SIZE = 64
OP_SNAPSHOT_SLOTS = 4
RECEIPT_TABLE_ADDRESS = 0x90220
RECEIPT_SIZE = 128
RECEIPT_SLOTS = 12

#: Milestone 3 scratch slots addressed through r0 by the trap entry stub.
TRAP_SCRATCH_ADDRESS = 0x200
TRAP_SCRATCH_SIZE = 24

#: Separate trap stack; handlers never use the interrupted context's stack.
TRAP_STACK_TOP = 0x88000

#: 304-byte software trap frame preserved by the assembly trap entry.
TRAP_FRAME_ADDRESS = 0x90900
TRAP_FRAME_SIZE = 304
TRAP_FRAME_ALIGN = 16

#: Trap dispatch observation state and its own processing-core receipt log.
TRAP_STATE_ADDRESS = 0x90A40
TRAP_STATE_SIZE = 128
TRAP_RECEIPT_TABLE_ADDRESS = 0x90B00
TRAP_RECEIPT_SLOTS = 8

#: Milestone 4 fixed memory observation and request regions. These occupy the
#: free gap above the Milestone 3 receipt log and below guest test code.
MEMORY_ENTRY_ADDRESS = 0x90F00
MEMORY_STATE_ADDRESS = 0x90F10
MEMORY_STATE_SIZE = 128
MEMORY_REGION_TABLE_ADDRESS = 0x90F90
MEMORY_REGION_SIZE = 64
MEMORY_REGION_SLOTS = 64
ADDRESS_SPACE_TABLE_ADDRESS = 0x91F90
ADDRESS_SPACE_SIZE = 32
ADDRESS_SPACE_SLOTS = 2
MEMORY_REQUEST_ADDRESS = 0x91FD0
MEMORY_REQUEST_SIZE = 64
MEMORY_RESPONSE_ADDRESS = 0x92010
MEMORY_RESPONSE_SIZE = 64
MEMORY_RECEIPT_TABLE_ADDRESS = 0x92050
MEMORY_RECEIPT_SLOTS = 128

#: NPK-declared physical arena. NovaMachine has no RAM-size model, so this is a
#: kernel policy region over sparse address space, not a hardware frame pool.
PHYSICAL_ARENA_BASE = 0xC0000
PHYSICAL_ARENA_SIZE = 0x40000
MEMORY_PAGE_SIZE = 0x1000

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
    "CORE_STATE_ADDRESS",
    "CORE_STATE_SIZE",
    "OP_SNAPSHOT_ADDRESS",
    "OP_SNAPSHOT_SIZE",
    "OP_SNAPSHOT_SLOTS",
    "RECEIPT_TABLE_ADDRESS",
    "RECEIPT_SIZE",
    "RECEIPT_SLOTS",
    "TRAP_SCRATCH_ADDRESS",
    "TRAP_SCRATCH_SIZE",
    "TRAP_STACK_TOP",
    "TRAP_FRAME_ADDRESS",
    "TRAP_FRAME_SIZE",
    "TRAP_FRAME_ALIGN",
    "TRAP_STATE_ADDRESS",
    "TRAP_STATE_SIZE",
    "TRAP_RECEIPT_TABLE_ADDRESS",
    "TRAP_RECEIPT_SLOTS",
    "MEMORY_ENTRY_ADDRESS",
    "MEMORY_STATE_ADDRESS",
    "MEMORY_STATE_SIZE",
    "MEMORY_REGION_TABLE_ADDRESS",
    "MEMORY_REGION_SIZE",
    "MEMORY_REGION_SLOTS",
    "ADDRESS_SPACE_TABLE_ADDRESS",
    "ADDRESS_SPACE_SIZE",
    "ADDRESS_SPACE_SLOTS",
    "MEMORY_REQUEST_ADDRESS",
    "MEMORY_REQUEST_SIZE",
    "MEMORY_RESPONSE_ADDRESS",
    "MEMORY_RESPONSE_SIZE",
    "MEMORY_RECEIPT_TABLE_ADDRESS",
    "MEMORY_RECEIPT_SLOTS",
    "PHYSICAL_ARENA_BASE",
    "PHYSICAL_ARENA_SIZE",
    "MEMORY_PAGE_SIZE",
    "BOOT_PRIVILEGE",
]
