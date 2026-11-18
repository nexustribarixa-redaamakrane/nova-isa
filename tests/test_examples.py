"""Canonical ``examples/`` programs: they assemble, halt, and behave.

Every example must assemble through the CLI and through the Python API, run
to a clean ``HALT`` on the reference machine at machine privilege (the
``nova-run`` default), and leave the documented register/object state.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from nova import assemble_text, load_nob
from nova.cli import assemble_main, run_main
from nova.emulator import NovaMachine
from nova.isa import PRIVILEGE_LEVELS
from nova.object import load_object

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PYTHON = sys.executable

EXPECTED_EXAMPLES = {
    "hello.no",
    "arithmetic.no",
    "branches.no",
    "labels.no",
    "exceptions.no",
    "objects.no",
}

#: name of example -> a callable checked against the finished machine.
EXPECTED_STATE = {
    "hello": lambda m: m.regs[1] == 42,
    "arithmetic": lambda m: (m.regs[3], m.regs[5], m.regs[6]) == (51, 91, 90),
    "branches": lambda m: (m.regs[3], m.regs[4], m.regs[5]) == (11, 3, 0),
    "labels": lambda m: (
        m.regs[3] == 12 and m.regs[2] - m.regs[1] == 12
    ),
    "exceptions": lambda m: (
        m.regs[5] == 7 and m.regs[6] == 7 and m.exception_stack() == []
    ),
    "objects": lambda m: (
        m.regs[2] != 0 and m.regs[3] == 24 and m.regs[5] == 40
        and len(m.objects) == 0
    ),
}


def example_source(name):
    return (EXAMPLES / f"{name}.no").read_text(encoding="utf-8")


def run_example(name):
    """Assemble and run one example at machine privilege; return the machine."""
    machine = NovaMachine(code=assemble_text(example_source(name), entry=0x1000),
                          entry=0x1000)
    machine.priv = PRIVILEGE_LEVELS["machine"]
    machine.run(10_000, stop_on_exception=False)
    return machine


@pytest.mark.parametrize("name", sorted(EXPECTED_STATE))
def test_example_halts_with_documented_state(name):
    machine = run_example(name)
    assert machine.halted
    assert EXPECTED_STATE[name](machine), (
        f"{name}.no finished in an unexpected state: "
        f"regs={machine.regs}, objects={list(machine.objects)}"
    )


def test_example_set_is_exactly_the_canonical_list():
    found = {path.name for path in EXAMPLES.glob("*.no")}
    assert found == EXPECTED_EXAMPLES
    for name in EXPECTED_EXAMPLES:
        assert example_source(name[:-3]).strip()


@pytest.mark.parametrize("name", sorted(EXPECTED_STATE))
def test_example_builds_and_runs_through_the_cli(name, tmp_path):
    output = tmp_path / f"{name}.nob"
    result = assemble_main([str(EXAMPLES / f"{name}.no"), "-o", str(output)])
    assert result == 0
    assert output.exists()

    nob = load_nob(output)
    assert nob.image is not None

    run_result = run_main([str(output)])
    assert run_result == 0


def test_example_object_build_matches_python_api(tmp_path):
    output = tmp_path / "objects.nobj"
    result = assemble_main([str(EXAMPLES / "objects.no"), "-o", str(output)])
    assert result == 0
    assert output.suffix == ".nobj"
    assert output.exists()
    written = load_object(output)
    from nova import encode_object, assemble_object
    assert encode_object(written) == encode_object(assemble_object(example_source("objects")))


def test_hello_pipeline_via_module_invocation(tmp_path):
    import os
    work = tmp_path
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    target = work / "hello.nob"
    as_proc = subprocess.run(
        [PYTHON, "-m", "nova.as", str(EXAMPLES / "hello.no"), "-o", str(target)],
        cwd=work, capture_output=True, text=True, timeout=60, env=env,
    )
    assert as_proc.returncode == 0
    assert "hello.nob" in as_proc.stdout
    assert target.exists()

    run_proc = subprocess.run(
        [PYTHON, "-m", "nova.run", str(target)],
        cwd=work, capture_output=True, text=True, timeout=60, env=env,
    )
    assert run_proc.returncode == 0
    assert "halted after" in run_proc.stdout