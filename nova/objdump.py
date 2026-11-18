"""``python -m nova.objdump`` and the ``nova-objdump`` console script.

Disassembles a ``.nob`` executable, printing the header and one canonical
instruction per image word. Undefined words render as ``.word 0x........``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import ISA_VERSION, TOOLCHAIN_VERSION
from .disassembler import disassemble_word
from .isa import INSTRUCTION_BYTES
from .nob import NOB_VERSION, NobError, load_nob

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-objdump",
        description="Disassemble a .nob Nova executable.",
    )
    parser.add_argument("input", metavar="INPUT", help=".nob executable to dump")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {TOOLCHAIN_VERSION} ({ISA_VERSION})")
    return parser


def _print_header(nob) -> None:
    print(f"Nova .nob executable (format v{NOB_VERSION})")
    print(f"  image_base: 0x{nob.image_base:016x}")
    print(f"  entry:      0x{nob.entry:016x}")
    print(f"  flags:      0x{nob.flags:04x}")
    print(f"  image_size: {nob.image_size}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        nob = load_nob(args.input)
    except OSError as exc:
        print(f"nova-objdump: {args.input}: cannot read: {exc.strerror or exc}",
              file=sys.stderr)
        return EXIT_ERROR
    except NobError as exc:
        print(f"nova-objdump: {args.input}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_header(nob)
    print()
    for offset in range(0, len(nob.image), INSTRUCTION_BYTES):
        word = int.from_bytes(nob.image[offset:offset + INSTRUCTION_BYTES], "little")
        address = nob.image_base + offset
        print(f"{address:08x}: {word:08x}  {disassemble_word(word)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())