"""``python -m nova.as`` and the ``nova-as`` console script.

Assembles Nova assembly source into a ``.nob`` executable, or into a ``.nobj``
relocatable object when the output file ends in ``.nobj``. See the README's
"Command-line tools" section for the exact behavior and exit codes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import ISA_VERSION, TOOLCHAIN_VERSION
from .assembler import AssemblyError, DEFAULT_ENTRY, assemble_object, assemble_text
from .nob import NobError, encode_nob, parse_address
from .object import encode_object

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _address_literal(value: str) -> int:
    try:
        return parse_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-as",
        description="Assemble Nova assembly source into a .nob executable "
                    "or a .nobj relocatable object.",
    )
    parser.add_argument("input", metavar="INPUT",
                        help="Nova assembly source (.no or .nova)")
    parser.add_argument("-o", "--output", metavar="OUTPUT", default=None,
                        help="output file; a .nobj suffix selects "
                             "relocatable-object output (default: INPUT with "
                             "the .nob suffix)")
    parser.add_argument("--image-base", metavar="ADDR", type=_address_literal,
                        default=DEFAULT_ENTRY,
                        help="load address of image byte 0 (default 0x1000)")
    parser.add_argument("--entry", metavar="ADDR", type=_address_literal,
                        default=None,
                        help="execution entry address (default: the image base)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {TOOLCHAIN_VERSION} ({ISA_VERSION})")
    return parser


def _default_output(input_name: str) -> Path:
    return Path(input_name).with_suffix(".nob")


def _source_line(source: str, line: int | None) -> str | None:
    if line is None:
        return None
    lines = source.splitlines()
    if 0 < line <= len(lines):
        return lines[line - 1].strip()
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"nova-as: {args.input}: cannot read source: {exc.strerror or exc}",
              file=sys.stderr)
        return EXIT_ERROR
    except UnicodeDecodeError as exc:
        print(f"nova-as: {args.input}: source is not valid UTF-8: {exc}",
              file=sys.stderr)
        return EXIT_ERROR

    output = Path(args.output) if args.output else _default_output(args.input)

    if output.suffix.lower() == ".nobj":
        try:
            obj = assemble_object(source)
            encoded = encode_object(obj)
        except AssemblyError as exc:
            print(f"nova-as: {args.input}: {exc}", file=sys.stderr)
            offending = _source_line(source, exc.line)
            if offending is not None:
                print(f"    {exc.line}: {offending}", file=sys.stderr)
            return EXIT_ERROR
        try:
            output.write_bytes(encoded)
        except OSError as exc:
            print(f"nova-as: {output}: cannot write: {exc.strerror or exc}",
                  file=sys.stderr)
            return EXIT_ERROR
        image_words = len(obj.sections[0].data) // 4  # only .text is generated
        print(f"nova-as: wrote {output}: {image_words} instruction(s), "
              f"{len(encoded)} bytes, {len(obj.sections)} section(s), "
              f"{len(obj.symbols)} symbol(s), {len(obj.relocations)} "
              f"relocation(s), relocatable object")
        return EXIT_OK

    image_base = args.image_base
    entry = args.entry if args.entry is not None else image_base
    try:
        image = assemble_text(source, entry=image_base)
        encoded = encode_nob(image_base=image_base, entry=entry, image=image)
    except AssemblyError as exc:
        print(f"nova-as: {args.input}: {exc}", file=sys.stderr)
        offending = _source_line(source, exc.line)
        if offending is not None:
            print(f"    {exc.line}: {offending}", file=sys.stderr)
        return EXIT_ERROR
    except NobError as exc:
        print(f"nova-as: {args.input}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        output.write_bytes(encoded)
    except OSError as exc:
        print(f"nova-as: {output}: cannot write: {exc.strerror or exc}",
              file=sys.stderr)
        return EXIT_ERROR
    print(f"nova-as: wrote {output}: {len(image) // 4} instruction(s), "
          f"{len(encoded)} bytes, image base 0x{image_base:x}, entry 0x{entry:x}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())