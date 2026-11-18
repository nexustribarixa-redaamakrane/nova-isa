"""Nova source, build-artifact, and binary-format registry."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

FILE_FORMAT_VERSION = "Nova file formats v1.0"


@dataclass(frozen=True)
class FileFormat:
    extension: str
    name: str
    kind: str
    description: str
    magic: bytes | None = None
    implemented: bool = False


_FORMATS = (
    FileFormat(".no", "Nova Assembly", "source", "Canonical assembly source.", implemented=True),
    FileFormat(".nova", "Nova Assembly", "source", "Alternate assembly source suffix.", implemented=True),
    FileFormat(".nos", "Nova Source", "source", "High-level Nova source; language/compiler not implemented."),
    FileFormat(".noi", "Nova Include", "source", "Include, interface, or stub declarations; preprocessor not implemented."),
    FileFormat(".nol", "Nova Listing", "text", "Addressed assembly/disassembly listing."),
    FileFormat(".inok", "Intermediate Nova Kompile", "intermediate", "Versioned compiler intermediate representation.", b"NOVIR\x00\x01\x00"),
    FileFormat(".noo", "Nova Object", "object", "Reserved name for a relocatable Nova object; the implemented object format is `.nobj`."),
    FileFormat(".nobj", "Nova Object Format", "object", "Relocatable Nova object v1: named sections (.text/.data/.rodata/.bss), a symbol table (local, global, and undefined external), and PCREL_DISP14 relocations for branch/call sites. Written by `nova-as -o file.nobj`; read by `nova.load_object`.", b"NOBJ", implemented=True),
    FileFormat(".nok", "Nova Kompile", "executable", "Linked Nova executable container.", b"NOVAKMP\x00"),
    FileFormat(".nob", "Nova Binary", "executable", "Nova executable container v1: 32-byte header (magic, version, flags, image_base, entry, image_size) followed by a little-endian word image. Written by nova-as; read by nova-objdump and nova-run.", b"NOBV", implemented=True),
    FileFormat(".nod", "Nova Dynamic Library", "shared-library", "Loadable Nova module; not a native PE/COFF DLL.", b"NOVADLL\x00"),
    FileFormat(".nox", "Nova Debug", "debug", "Debug symbols and source/address mapping.", b"NOVADBG\x00"),
    FileFormat(".nom", "Nova Module Manifest", "metadata", "Module metadata and declared imports/exports.", b"NOVAMOD\x00"),
    FileFormat(".nop", "Nova Package", "package", "Distribution archive containing Nova modules and metadata.", b"NOVAPKG\x00"),
)

FILE_FORMATS: Dict[str, FileFormat] = {
    format_def.extension: format_def for format_def in _FORMATS
}


def get_file_format(path_or_extension: str) -> FileFormat:
    """Return format metadata for an extension or filename, case-insensitively."""
    extension = path_or_extension.lower()
    if not extension.startswith("."):
        extension = "." + extension.rsplit(".", 1)[-1]
    try:
        return FILE_FORMATS[extension]
    except KeyError as exc:
        raise ValueError(f"Unknown Nova file extension: {path_or_extension}") from exc


def openwindows_directories(home: Path | None = None) -> tuple[Path, Path]:
    """Return user-relative OpenWindows module directories without creating them.

    NOVA_OPENWINDOWS_DIR and NOVA_OPENWINDOWS_ESSENTIALS_DIR can override
    either destination. Otherwise, paths are resolved from the current user's
    home directory at runtime; no machine-specific path is stored in the repo.
    """
    documents = (home if home is not None else Path.home()) / "Documents"
    main = Path(os.environ["NOVA_OPENWINDOWS_DIR"]).expanduser() \
        if os.environ.get("NOVA_OPENWINDOWS_DIR") \
        else documents / "OpenWindows"
    essentials = Path(os.environ["NOVA_OPENWINDOWS_ESSENTIALS_DIR"]).expanduser() \
        if os.environ.get("NOVA_OPENWINDOWS_ESSENTIALS_DIR") \
        else documents / "OpenWindows-Essentials"
    return main, essentials
