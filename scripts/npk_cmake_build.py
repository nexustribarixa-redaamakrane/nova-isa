#!/usr/bin/env python3
"""CMake-facing wrapper around ``nova.npk.build_kernel()``.

The Nova kernel is built with the in-tree assembler, compiler, linker, and
``.nob`` container. CMake orchestrates that Python build but does not replace
it with a second toolchain. This helper exists so CMake needs only one stable
command-line contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from nova import npk  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="directory for npk.nob and intermediate objects")
    parser.add_argument("--keep-objects", action="store_true",
                        help="retain .nobj intermediates for inspection")
    args = parser.parse_args(argv)

    image = npk.build_kernel(args.output_dir, keep_objects=args.keep_objects)
    print(f"NPK kernel: {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
