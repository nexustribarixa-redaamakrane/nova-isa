"""``python -m nova.ld`` and the ``nova-ld`` console script.

Links one or more ``.nobj`` relocatable objects into a single ``.nob``
executable. See the README's "Nova Linker v0.1" section for the exact layout
rules, symbol-resolution rules, and exit codes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import ISA_VERSION, LINKER_VERSION, TOOLCHAIN_VERSION
from .link import (
    DEFAULT_ENTRY_SYMBOL,
    DEFAULT_IMAGE_BASE,
    LinkError,
    link_objects,
)
from .nob import NobError, encode_nob, parse_address
from .object import ObjectError, load_object

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
        prog="nova-ld",
        description="Link Nova .nobj relocatable objects into a .nob executable.",
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT",
                        help="relocatable object to link (.nobj)")
    parser.add_argument("-o", "--output", metavar="OUTPUT", default=None,
                        help="output executable; a .nob suffix is recommended "
                             "(default: second-to-last INPUT with the .nob suffix)")
    parser.add_argument("--entry", metavar="SYMBOL", default=DEFAULT_ENTRY_SYMBOL,
                        help=f"entry symbol to resolve (default {DEFAULT_ENTRY_SYMBOL!r})")
    parser.add_argument("--base", metavar="ADDR", type=_address_literal,
                        default=DEFAULT_IMAGE_BASE,
                        help="load address of image byte 0 (default 0x1000)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {TOOLCHAIN_VERSION} ({ISA_VERSION})")
    return parser


def _default_output(inputs: Sequence[str]) -> Path:
    return Path(inputs[0]).with_suffix(".nob")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output) if args.output else _default_output(args.inputs)

    objects = []
    for name in args.inputs:
        try:
            objects.append(load_object(Path(name)))
        except ObjectError as exc:
            print(f"nova-ld: {name}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except OSError as exc:
            print(f"nova-ld: {name}: cannot read: {exc.strerror or exc}",
                  file=sys.stderr)
            return EXIT_ERROR

    try:
        image = link_objects(objects, entry_symbol=args.entry, image_base=args.base)
        encoded = encode_nob(image_base=image.image_base, entry=image.entry,
                             image=image.image)
    except (LinkError, NobError) as exc:
        print(f"nova-ld: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        output.write_bytes(encoded)
    except OSError as exc:
        print(f"nova-ld: {output}: cannot write: {exc.strerror or exc}",
              file=sys.stderr)
        return EXIT_ERROR

    print(f"nova-ld: wrote {output}: {len(args.inputs)} object(s), "
          f"{len(image.image)} byte(s), entry 0x{image.entry:x}, "
          f"base 0x{image.image_base:x} ({LINKER_VERSION})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())