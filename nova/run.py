"""``python -m nova.run`` and the ``nova-run`` console script.

Loads a ``.nob`` executable into a reference :class:`~nova.emulator.NovaMachine`
and executes it. By default the runner starts at machine privilege (as reset
firmware would) so a program can install exception handlers and use ``HALT``;
use ``--privilege`` to start lower. Control-passing exceptions are left to the
installed handler, and the machine stops only on HALT/WAIT, the step budget, or
an explicit ``--stop-on-exception``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import ISA_VERSION, TOOLCHAIN_VERSION
from .emulator import NovaMachine
from .isa import EXCEPTION_CODES, EXCEPTION_NAMES, PRIVILEGE_LEVELS
from .nob import NobError, load_nob

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_EXCEPTION = 1
EXIT_BUDGET = 2
DEFAULT_STEPS = 1_000_000


def _positive_int(value: str) -> int:
    try:
        count = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid step count: {value!r}") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError("step count must be positive")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-run",
        description="Execute a .nob Nova executable on the reference machine.",
    )
    parser.add_argument("input", metavar="INPUT", help=".nob executable to run")
    parser.add_argument("--steps", metavar="N", type=_positive_int,
                        default=DEFAULT_STEPS,
                        help=f"maximum instructions to execute (default {DEFAULT_STEPS})")
    parser.add_argument("--privilege", choices=sorted(PRIVILEGE_LEVELS),
                        default="machine",
                        help="initial privilege level (default: machine)")
    parser.add_argument("--stop-on-exception", action="store_true",
                        help="stop at the first taken exception and report it")
    parser.add_argument("--dump-regs", action="store_true",
                        help="print all 32 general-purpose registers")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {TOOLCHAIN_VERSION} ({ISA_VERSION})")
    return parser


def _report_exception(machine: "NovaMachine") -> str:
    ecode = machine.exception_code
    name = EXCEPTION_NAMES.get(ecode, f"#{ecode}")
    return (f"{name} (cause 0x{machine.ecause:x}, epc 0x{machine.epc:x}, "
            f"pc 0x{machine.pc:x})")


def _dump_registers(machine: "NovaMachine", width: int = 4) -> None:
    for row in range(0, 32, width):
        names = [f"r{r}" for r in range(row, row + width)]
        values = [machine.read_register(r) for r in range(row, row + width)]
        print("  " + "  ".join(f"{name}={value:016x}" for name, value in
                               zip(names, values)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        nob = load_nob(args.input)
    except OSError as exc:
        print(f"nova-run: {args.input}: cannot read: {exc.strerror or exc}",
              file=sys.stderr)
        return EXIT_EXCEPTION
    except NobError as exc:
        print(f"nova-run: {args.input}: {exc}", file=sys.stderr)
        return EXIT_EXCEPTION

    machine = NovaMachine(code=nob.image, entry=nob.image_base)
    machine.pc = nob.entry
    machine.priv = PRIVILEGE_LEVELS[args.privilege]

    none = EXCEPTION_CODES["NONE"]
    steps_done = 0
    reason = "budget"
    for _ in range(args.steps):
        if machine.halted:
            reason = "halt"
            break
        machine.step()
        steps_done += 1
        if args.stop_on_exception and machine.exception_code != none:
            reason = "exception"
            break

    if reason == "exception":
        print(f"nova-run: exception taken after {steps_done} step(s): "
              f"{_report_exception(machine)}")
        if args.dump_regs:
            _dump_registers(machine)
        return EXIT_EXCEPTION

    if reason == "budget":
        print(f"nova-run: step budget ({args.steps}) reached at "
              f"pc 0x{machine.pc:x}, privilege {machine.privilege_name}")
        if machine.exception_code != none:
            print(f"  exception pending: {_report_exception(machine)}")
        if args.dump_regs:
            _dump_registers(machine)
        return EXIT_BUDGET

    print(f"nova-run: halted after {steps_done} step(s) at "
          f"pc 0x{machine.pc:x}, privilege {machine.privilege_name}")
    if args.dump_regs:
        _dump_registers(machine)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())