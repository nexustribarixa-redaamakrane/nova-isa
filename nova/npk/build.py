"""Reproducible NPK kernel build using the existing Nova toolchain.

The build deliberately uses only the Phase 1 interfaces: the assembler object
frontend, ``nova-cc``, the linker, and the ``.nob`` container. It assembles the
boot entry and Nova assembly trap entry, then compiles the processing, trap,
syscall, device, and coordinator translation units. No new build language or
executable format is introduced.
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
    trap_source = _source_path("trap.s")
    memory_asm_source = _source_path("memory.s")
    syscalls_asm_source = _source_path("syscalls.s")
    devices_asm_source = _source_path("devices.s")
    kernel_source = _source_path("kernel.c")
    processing_source = _source_path("processing.c")
    trap_c_source = _source_path("trap.c")
    memory_c_source = _source_path("memory.c")
    object_c_source = _source_path("objects.c")
    syscalls_c_source = _source_path("syscalls.c")
    devices_c_source = _source_path("devices.c")
    start_object = out / "npk_start.nobj"
    trap_object = out / "npk_trap_entry.nobj"
    memory_asm_object = out / "npk_memory_flush.nobj"
    syscalls_asm_object = out / "npk_syscall_time.nobj"
    devices_asm_object = out / "npk_devices_csr.nobj"
    kernel_object = out / "npk_kernel.nobj"
    processing_object = out / "npk_processing.nobj"
    trap_c_object = out / "npk_trap_dispatch.nobj"
    memory_c_object = out / "npk_memory.nobj"
    object_c_object = out / "npk_objects.nobj"
    syscalls_c_object = out / "npk_syscalls.nobj"
    devices_c_object = out / "npk_devices.nobj"
    image_path = out / "npk.nob"

    try:
        for source, object_path in ((start_source, start_object),
                                    (trap_source, trap_object),
                                    (memory_asm_source, memory_asm_object),
                                    (syscalls_asm_source, syscalls_asm_object),
                                    (devices_asm_source, devices_asm_object)):
            assembly = source.read_text(encoding="utf-8")
            obj = assemble_object(assembly)
            object_path.write_bytes(encode_object(obj))
    except (OSError, UnicodeDecodeError, AssemblyError) as exc:
        raise NpkBuildError(f"nova-as: {exc}") from exc

    cc_options = ["--long-calls", "-c"]
    _require_ok(
        cc_main([str(kernel_source), *cc_options, "-o", str(kernel_object)]),
        "nova-cc kernel",
    )
    _require_ok(
        cc_main([str(processing_source), *cc_options, "-o", str(processing_object)]),
        "nova-cc processing",
    )
    _require_ok(
        cc_main([str(trap_c_source), *cc_options, "-o", str(trap_c_object)]),
        "nova-cc trap dispatch",
    )
    _require_ok(
        cc_main([str(memory_c_source), *cc_options, "-o", str(memory_c_object)]),
        "nova-cc memory",
    )
    _require_ok(
        cc_main([str(object_c_source), *cc_options, "--long-branches",
                 "-o", str(object_c_object)]),
        "nova-cc objects",
    )
    _require_ok(
        cc_main([str(syscalls_c_source), *cc_options, "-o", str(syscalls_c_object)]),
        "nova-cc syscalls",
    )
    _require_ok(
        cc_main([str(devices_c_source), *cc_options, "-o", str(devices_c_object)]),
        "nova-cc devices",
    )

    try:
        objects = [
            load_object(start_object),
            load_object(trap_object),
            load_object(memory_asm_object),
            load_object(processing_object),
            load_object(trap_c_object),
            load_object(memory_c_object),
            load_object(object_c_object),
            load_object(kernel_object),
            load_object(syscalls_asm_object),
            load_object(devices_asm_object),
            load_object(syscalls_c_object),
            load_object(devices_c_object),
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
        trap_object.unlink(missing_ok=True)
        memory_asm_object.unlink(missing_ok=True)
        syscalls_asm_object.unlink(missing_ok=True)
        devices_asm_object.unlink(missing_ok=True)
        processing_object.unlink(missing_ok=True)
        trap_c_object.unlink(missing_ok=True)
        memory_c_object.unlink(missing_ok=True)
        object_c_object.unlink(missing_ok=True)
        syscalls_c_object.unlink(missing_ok=True)
        devices_c_object.unlink(missing_ok=True)
        kernel_object.unlink(missing_ok=True)
    return image_path
