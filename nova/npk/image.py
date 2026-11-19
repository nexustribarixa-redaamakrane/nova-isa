"""Validation of NPK kernel images.

NPK uses the existing ``.nob`` container. The only NPK-specific requirements
are the fixed load address, the reserved flag value, and the minimal size
needed for a boot stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from ..nob import NobImage, load_nob
from .layout import (
    BOOT_STACK_GUARD,
    BOOT_STACK_TOP,
    KERNEL_IMAGE_BASE,
    MIN_KERNEL_IMAGE_BYTES,
)

__all__ = ["NpkImageError", "load_kernel_image", "validate_kernel_image"]


class NpkImageError(ValueError):
    """Raised when a ``.nob`` image does not satisfy the NPK boot contract."""


def validate_kernel_image(image: NobImage) -> None:
    """Validate an NPK kernel image without changing it."""
    if not isinstance(image, NobImage):
        raise NpkImageError("an NPK kernel image must be a decoded NobImage")
    if image.flags != 0:
        raise NpkImageError(f"unsupported NPK kernel flags: 0x{image.flags:x}")
    if image.image_base != KERNEL_IMAGE_BASE:
        raise NpkImageError(
            f"kernel image_base 0x{image.image_base:x} must be "
            f"0x{KERNEL_IMAGE_BASE:x}"
        )
    if len(image.image) < MIN_KERNEL_IMAGE_BYTES:
        raise NpkImageError(
            f"kernel image is {len(image.image)} bytes; expected at least "
            f"{MIN_KERNEL_IMAGE_BYTES}"
        )
    image_end = image.image_base + len(image.image)
    if image_end >= BOOT_STACK_TOP - BOOT_STACK_GUARD:
        raise NpkImageError(
            f"kernel image ends at 0x{image_end:x}, which overlaps the "
            f"reserved boot stack guard below 0x{BOOT_STACK_TOP:x}"
        )
    if not image.image_base <= image.entry < image_end:
        raise NpkImageError(
            f"kernel entry 0x{image.entry:x} is outside the image range "
            f"[0x{image.image_base:x}, 0x{image_end:x})"
        )


def load_kernel_image(path: Union[str, Path]) -> NobImage:
    """Load and validate a kernel image from disk."""
    image = load_nob(path)
    validate_kernel_image(image)
    return image
