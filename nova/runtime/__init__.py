"""Freestanding Nova64 runtime and ``libnova``.

This package owns the startup file, the syscall wrappers, and the C library
subset documented in ``docs/RUNTIME.md``.  It deliberately reuses the existing
compiler, assembler, object format, and linker instead of duplicating them:
runtime C sources pass through ``nova.cc.preprocess``, ``nova.cc.parser``,
``nova.cc.sema``, ``nova.cc.codegen``, and ``nova.assembler`` exactly like user
code.

Runtime objects are compiled on demand and cached as immutable templates; each
driver link receives deep copies so linker bookkeeping cannot leak between
invocations.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import List, Sequence

from ..assembler import assemble_object
from ..cc.codegen import generate
from ..cc.diagnostics import CompileError
from ..cc.parser import parse
from ..cc.preprocessor import preprocess_to_tokens
from ..cc.sema import analyze
from ..object import ObjectFile

__all__ = [
    "RUNTIME_ROOT",
    "INCLUDE_DIR",
    "STARTUP_SOURCE",
    "SYSCALL_SOURCE",
    "LIBRARY_SOURCES",
    "startup_object",
    "syscall_object",
    "library_objects",
    "runtime_objects",
]

RUNTIME_ROOT = Path(__file__).resolve().parent
INCLUDE_DIR = RUNTIME_ROOT / "include"
STARTUP_SOURCE = RUNTIME_ROOT / "startup.s"
SYSCALL_SOURCE = RUNTIME_ROOT / "syscalls.s"

#: Order is stable so repeated links are deterministic.
LIBRARY_SOURCES: Sequence[Path] = (
    RUNTIME_ROOT / "src" / "string.c",
    RUNTIME_ROOT / "src" / "stdlib.c",
    RUNTIME_ROOT / "src" / "stdio.c",
)


def _assemble(path: Path, assembly: str) -> ObjectFile:
    return assemble_object(assembly, source_path=str(path))


def _compile_c_source(path: Path) -> ObjectFile:
    source = path.read_text(encoding="utf-8")
    try:
        tokens = preprocess_to_tokens(
            source,
            filename=str(path),
            directory=str(path.parent),
            system_include_paths=[str(INCLUDE_DIR)],
        )
        tree = parse(tokens, filename=str(path))
        tree = analyze(tree, target="nova64")
        assembly = generate(tree, target="nova64",
                            long_calls=True, long_branches=True)
    except CompileError as exc:
        raise RuntimeError(
            f"cannot build runtime source '{path.name}': {exc}"
        ) from exc
    return _assemble(path, assembly)


@lru_cache(maxsize=1)
def _template_objects() -> tuple:
    startup = _assemble(STARTUP_SOURCE,
                        STARTUP_SOURCE.read_text(encoding="utf-8"))
    syscalls = _assemble(SYSCALL_SOURCE,
                         SYSCALL_SOURCE.read_text(encoding="utf-8"))
    library = tuple(_compile_c_source(path) for path in LIBRARY_SOURCES)
    return (startup, syscalls) + library


def _copies(objects: Sequence[ObjectFile]) -> List[ObjectFile]:
    return [copy.deepcopy(item) for item in objects]


def startup_object() -> ObjectFile:
    """The documented ``_start`` object."""
    return _copies(_template_objects()[:1])[0]


def syscall_object() -> ObjectFile:
    """The SYS_* wrapper object."""
    return _copies(_template_objects()[1:2])[0]


def library_objects() -> List[ObjectFile]:
    """The libnova objects (string, conversion, formatted output)."""
    return _copies(_template_objects()[2:])


def runtime_objects(*, with_startup: bool = True,
                    with_library: bool = True) -> List[ObjectFile]:
    """Return startup, syscall wrappers, and libnova in link order."""
    objects: List[ObjectFile] = []
    if with_startup:
        objects.append(startup_object())
    objects.append(syscall_object())
    if with_library:
        objects.extend(library_objects())
    return objects
