"""Repeatable NPK validation.

Run from the repository root::

    python scripts/npk_validate.py

The script runs the NPK boot/build and processing-core tests first, then the
unchanged Phase 1 regression suite. It exits 0 only when both stages pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAGES = [
    (
        "npk",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_npk.py",
            "tests/test_npk_core.py",
        ],
    ),
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
