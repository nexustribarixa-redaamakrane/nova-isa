"""Reproducible NPK kernel build using the existing Nova toolchain.

The build deliberately uses only the Phase 1 interfaces: the assembler object
frontend, ``nova-cc``, the linker, and the ``.nob`` container. It assembles the
boot entry and compiles the processing and coordinator translation units. No new
build language or executable format is introduced.
"""

from __future__ import annotations

from pathlib import Path

from ..assembler import AssemblyError, assemble_object
from ..cc.driver import main as cc_main
from ..link import LinkError, link_objects
from ..nob import NobError, encode_nob
from ..object import ObjectError, encode_object, load_object
from .layout import KERNEL_ENTRY_SYMBOL, KERNEL_IMAGE_BASE

__all__ = ["NpkBuildError", "build_kernel"]


class NpkBuildError(RuntimeError):
    """Raised when a kernel build stage fails."""


def _source_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "src" / name


def _require_ok(return_code: int, stage: str) -> None:
    if return_code != 0:
        raise NpkBuildError(f"{stage} failed with exit code {return_code}")


def build_kernel(output_dir: str | Path, *, keep_objects: bool = False) -> Path:
    """Build the minimal NPK kernel image into ``output_dir``.

    Returns the path of the generated ``npk.nob``. The build is deterministic:
    fixed sources, fixed input order, fixed entry symbol, and fixed image base.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_source = _source_path("start.s")
    kernel_source = _source_path("kernel.c")
    processing_source = _source_path("processing.c")
    start_object = out / "npk_start.nobj"
    kernel_object = out / "npk_kernel.nobj"
    processing_object = out / "npk_processing.nobj"
    image_path = out / "npk.nob"

    try:
        start_assembly = start_source.read_text(encoding="utf-8")
        start_obj = assemble_object(start_assembly)
        start_object.write_bytes(encode_object(start_obj))
    except (OSError, UnicodeDecodeError, AssemblyError) as exc:
        raise NpkBuildError(f"nova-as: {exc}") from exc

    _require_ok(
        cc_main([str(kernel_source), "-c", "-o", str(kernel_object)]),
        "nova-cc kernel",
    )
    _require_ok(
        cc_main([str(processing_source), "-c", "-o", str(processing_object)]),
        "nova-cc processing",
    )

    try:
        objects = [
            load_object(start_object),
            load_object(processing_object),
            load_object(kernel_object),
        ]
        image = link_objects(
            objects,
            entry_symbol=KERNEL_ENTRY_SYMBOL,
            image_base=KERNEL_IMAGE_BASE,
        )
        image_path.write_bytes(encode_nob(
            image_base=image.image_base,
            entry=image.entry,
            image=image.image,
        ))
    except (ObjectError, LinkError, NobError, OSError) as exc:
        raise NpkBuildError(f"nova-ld: {exc}") from exc

    if not keep_objects:
        start_object.unlink(missing_ok=True)
        processing_object.unlink(missing_ok=True)
        kernel_object.unlink(missing_ok=True)
    return image_path
