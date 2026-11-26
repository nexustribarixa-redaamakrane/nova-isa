"""Repeatable NPK validation.

Run from the repository root::

    python scripts/npk_validate.py

The script discovers every ``tests/test_npk*.py`` file in sorted order, so new
NPK suites cannot be silently skipped. It runs the NPK boot/build, processing
core, exception/trap, memory, object manager, syscall processing, typed
device/timer, device/core integration, task/execution, executable processing,
cross-subsystem integration, and QEMU harness tests first, then the unchanged
Phase 1 stage groups. It exits 0 only when both stages pass. The QEMU boot itself
is a separate optional target because QEMU is not required for host validation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NPK_TESTS = sorted(
    str(path.relative_to(ROOT)).replace("\\", "/")
    for path in (ROOT / "tests").glob("test_npk*.py")
)
if not NPK_TESTS:
    raise SystemExit("no tests/test_npk*.py files were discovered")

STAGES = [
    ("npk", [sys.executable, "-m", "pytest", "-q", *NPK_TESTS]),
    ("phase1", [sys.executable, "scripts/phase1_validate.py"]),
]


def run_stage(name: str, command: list[str]) -> bool:
    print(f"\n== {name} ==")
    print("+ " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"{name}: FAIL (exit {result.returncode})")
        return False
    print(f"{name}: PASS")
    return True


def main() -> int:
    for name, command in STAGES:
        if not run_stage(name, command):
            print("NPK validation: FAIL")
            return 1
    print("\nNPK validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
