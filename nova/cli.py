"""Console-script shim for the Nova command-line tools.

The tools live in ``nova/as.py``, ``nova/objdump.py``, ``nova/run.py``, and
``nova/ld.py`` so that ``python -m nova.as``, ``python -m nova.objdump``,
``python -m nova.run``, and ``python -m nova.ld`` work. The ``pyproject.toml``
entry points route through this module instead because ``as`` is a Python
keyword: the wrapper that setuptools generates imports the target with
``from <module> import <name>``, and ``from nova.as import ...`` is a syntax
error. Importing ``nova.as`` by string keeps both invocation forms working
without duplicating any tool logic.
"""

from __future__ import annotations

import importlib
from typing import Optional, Sequence

__all__ = ["assemble_main", "link_main", "objdump_main", "run_main"]


def _invoke(module: str, argv: Optional[Sequence[str]]) -> int:
    tool = importlib.import_module(f"nova.{module}")
    return tool.main(argv)


def assemble_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``nova-as`` console script."""
    return _invoke("as", argv)


def link_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``nova-ld`` console script."""
    return _invoke("ld", argv)


def objdump_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``nova-objdump`` console script."""
    return _invoke("objdump", argv)


def run_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``nova-run`` console script."""
    return _invoke("run", argv)