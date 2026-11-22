"""Host-side boot helper for the minimal NPK kernel image.

This is a test/harness layer, not a kernel API. It models the loader handoff
that `nova-run` performs: load at the image base, set the entry PC, select
machine privilege, and execute until HALT. No kernel vector or firmware is
invented.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..emulator import NovaMachine
from ..isa import EXCEPTION_CODES, PRIVILEGE_LEVELS
from ..nob import NobImage
from .core import (
    NpkCoreError,
    CoreSnapshot,
    decode_core_snapshot,
    validate_core_state,
)
from .image import validate_kernel_image
from .layout import (
    BOOT_MARKER_ADDRESS,
    BOOT_MARKER_SIZE,
    BOOT_MARKER_VALUE,
    BOOT_PRIVILEGE,
    BOOT_STACK_TOP,
)
from .processing import (
    NpkProcessingError,
    ProcessingReceipt,
    decode_processing_receipt,
    validate_boot_receipt,
)

__all__ = ["BootResult", "NpkBootError", "boot_kernel"]


class NpkBootError(RuntimeError):
    """Raised when a kernel image fails its boot contract."""


@dataclass(frozen=True)
class BootResult:
    """Observable state after a successful NPK boot."""

    machine: NovaMachine
    steps: int
    status: int
    marker: int
    processing: ProcessingReceipt
    core: CoreSnapshot
    vbar: int
    stack_pointer: int


def boot_kernel(image: NobImage, *, max_steps: int = 400_000) -> BootResult:
    """Boot a validated NPK image on the reference machine."""
    validate_kernel_image(image)
    if max_steps <= 0:
        raise NpkBootError("max_steps must be positive")

    machine = NovaMachine(code=image.image, entry=image.image_base)
    machine.pc = image.entry
    machine.priv = PRIVILEGE_LEVELS[BOOT_PRIVILEGE]

    steps = 0
    while steps < max_steps and not machine.halted:
        machine.step()
        steps += 1
        if machine.exception_code != EXCEPTION_CODES["NONE"]:
            raise NpkBootError(
                f"unexpected exception during NPK boot: ecode="
                f"{machine.exception_code}, ecause=0x{machine.ecause:x}, "
                f"pc=0x{machine.pc:x}, step={steps}"
            )

    if not machine.halted:
        raise NpkBootError(f"NPK kernel did not halt within {max_steps} steps")

    marker = machine._read_mem(BOOT_MARKER_ADDRESS, BOOT_MARKER_SIZE)
    if marker != BOOT_MARKER_VALUE:
        raise NpkBootError(
            f"boot marker mismatch: expected 0x{BOOT_MARKER_VALUE:x}, "
            f"found 0x{marker:x}"
        )

    try:
        receipt = decode_processing_receipt(machine)
        validate_boot_receipt(receipt)
    except NpkProcessingError as exc:
        raise NpkBootError(f"invalid NPK processing receipt: {exc}") from exc

    try:
        core = decode_core_snapshot(machine)
        validate_core_state(core.core)
    except NpkCoreError as exc:
        raise NpkBootError(f"invalid NPK processing core state: {exc}") from exc

    vbar = machine.vbar
    image_end = image.image_base + len(image.image)
    if not image.image_base <= vbar < image_end:
        raise NpkBootError(
            f"exception vector 0x{vbar:x} is outside the kernel image "
            f"[0x{image.image_base:x}, 0x{image_end:x})"
        )

    stack_pointer = machine.read_register("sp")
    if stack_pointer != BOOT_STACK_TOP:
        raise NpkBootError(
            f"kernel did not establish the boot stack: sp=0x{stack_pointer:x}, "
            f"expected 0x{BOOT_STACK_TOP:x}"
        )

    return BootResult(
        machine=machine,
        steps=steps,
        status=machine.read_register("a0"),
        marker=marker,
        processing=receipt,
        core=core,
        vbar=vbar,
        stack_pointer=stack_pointer,
    )
