"""Repeatable Phase 1 validation, ordered by pipeline stage.

Run from the repository root::

    python scripts/phase1_validate.py

The script runs the disjoint pytest groups below, compiles and executes the
freestanding hello example through the real driver and emulator, checks the 18
architecture conformance vectors, and finishes with ``compileall`` and a full
suite run. Each group reports its own pass count so a failure is attributable
to a single stage.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nova import PRIVILEGE_LEVELS, load_nob
from nova.cc.driver import main as cc_main
from nova.emulator import NovaMachine
from nova.isa import EXCEPTION_CODES
from nova.syscalls import dispatch_syscall

GROUPS = [
    ("frontend", [
        "tests/test_cc_lexer.py",
        "tests/test_cc_preprocessor.py",
        "tests/test_cc_parser.py",
    ], ()),
    ("sema", ["tests/test_cc_sema.py"], ()),
    ("codegen", ["tests/test_cc_codegen.py"], ()),
    ("driver", ["tests/test_cc_driver.py"], ()),
    ("runtime", ["tests/test_runtime.py"], (
        "not Libnova and not Variadic and not DriverIntegration",)),
    ("library", ["tests/test_runtime.py"], ("Libnova",)),
    ("end_to_end", ["tests/test_runtime.py"], (
        "Variadic or DriverIntegration",)),
    ("emulator", [
        "tests/test_instruction_table.py",
        "tests/test_preprocessor.py",
        "tests/test_pseudo.py",
        "tests/test_nova.py",
        "tests/test_nob.py",
        "tests/test_object.py",
        "tests/test_linker.py",
        "tests/test_csr.py",
        "tests/test_syscalls.py",
        "tests/test_trap_abi.py",
        "tests/test_v1_features.py",
        "tests/test_nox.py",
        "tests/test_cli.py",
        "tests/test_conformance.py",
        "tests/test_examples.py",
        "tests/test_documentation.py",
    ], ()),
]

HELLO_OUTPUT = b"hello world -42 42 ff FF ! %\ndone\n"
COUNT_RE = re.compile(r"(\d+) passed")


def run(command: list[str], label: str) -> tuple[int, str]:
    print(f"\n== {label} ==")
    print("+ " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, text=True,
                            capture_output=True, check=False)
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    if result.stderr.strip():
        tail += "\n" + "\n".join(result.stderr.strip().splitlines()[-5:])
    print(tail)
    return result.returncode, result.stdout + result.stderr


def run_pytest(label: str, selectors: list[str], expression: tuple[str, ...]) -> bool:
    command = [sys.executable, "-m", "pytest", "-q", *selectors]
    if expression:
        command += ["-k", expression[0]]
    code, output = run(command, label)
    match = COUNT_RE.search(output)
    count = match.group(1) if match else "0"
    print(f"{label}: {count} passed")
    return code == 0


def run_hello_example() -> bool:
    print("\n== example: examples/hello_runtime.c ==")
    with tempfile.TemporaryDirectory() as directory:
        image_path = Path(directory) / "hello.nob"
        if cc_main(["--runtime", "examples/hello_runtime.c",
                    "-o", str(image_path)]) != 0:
            print("example: driver link failed")
            return False
        image = load_nob(image_path)
        machine = NovaMachine()
        machine.load_code(image.image, image.image_base)
        machine.pc = image.entry
        machine.priv = PRIVILEGE_LEVELS["machine"]
        for _ in range(2_000_000):
            if machine.halted:
                break
            machine.step()
            if machine.exception_code == EXCEPTION_CODES["SYSTEM_CALL"]:
                dispatch_syscall(machine)
                if machine.halted:
                    break
                machine.pc = machine.epc
                if machine._exception_stack:
                    machine._exception_stack.pop()
                machine.exception_code = EXCEPTION_CODES["NONE"]
        output = b"".join(data for fd, data in machine.io_log if fd == 1)
        ok = machine.halted and machine.regs[1] == 0 and output == HELLO_OUTPUT
        print(f"example: {'1 passed' if ok else 'FAILED'} (a0={machine.regs[1]}, "
              f"output={output!r})")
        return ok


def main() -> int:
    ok = True
    for label, selectors, expression in GROUPS:
        ok = run_pytest(label, selectors, expression) and ok
    ok = run_hello_example() and ok

    code, output = run([sys.executable, "-m", "nova.conformance"], "conformance")
    vectors = re.search(r"(\d+)/(\d+) conformance vectors passed", output)
    print(f"conformance: {vectors.group(0) if vectors else 'FAILED'}")
    ok = code == 0 and vectors is not None and ok

    code, _ = run([sys.executable, "-m", "compileall", "-q",
                   "nova", "tests", "scripts"], "compileall")
    print(f"compileall: {'passed' if code == 0 else 'FAILED'}")
    ok = code == 0 and ok

    ok = run_pytest("full_suite", ["tests"], ()) and ok

    print(f"\nPhase 1 validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
