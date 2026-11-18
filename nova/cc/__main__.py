"""Module entry point for ``python -m nova.cc``."""

from __future__ import annotations

import sys

from .driver import main

if __name__ == "__main__":
    sys.exit(main())
