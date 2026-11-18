"""Command-line tool tests: ``nova-as``, ``nova-ld``, ``nova-objdump``, ``nova-run``.

Both the console-script entry points (``nova.cli`` shim) and the
``python -m nova.*`` invocation forms are exercised. The console scripts
themselves are built by pip at install time; the shim imports the same
``main`` functions those scripts invoke, and a subprocess test covers the
``python -m`` module path.
"""

import importlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from nova import TOOLCHAIN_VERSION, decode_nob, load_nob
from nova.cli import assemble_main, link_main, objdump_main, run_main

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

HELLO = "MOVI r1, 42\nHALT\n"

TRAP_DEMO = """
    start: LA r9, handler       ; handler address
           CSRW vbar, r9        ; install the handler (machine privilege)
           LI r1, 7
           SCALL 3              ; trap into the handler
           HALT
    handler:
           CSRR r2, ecode
           CSRR r3, ecause
           MRET
"""


def module_tool(name):
    return importlib.import_module(f"nova.{name}")


def write(text, path):
    path.write_text(text, encoding="utf-8")
    return path


def module_invocation(*args, cwd, env):
    return subprocess.run(
        [PYTHON, "-m", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def cloned_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return env


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def test_toolchain_version_matches_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert TOOLCHAIN_VERSION == pyproject["project"]["version"]
    scripts = pyproject["project"]["scripts"]
    assert scripts == {
        "nova-as": "nova.cli:assemble_main",
        "nova-ld": "nova.cli:link_main",
        "nova-objdump": "nova.cli:objdump_main",
        "nova-run": "nova.cli:run_main",
    }


def test_pyproject_build_backend_is_set_up_for_scripts():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["build-system"]["build-backend"] == "setuptools.build_meta"


# --------------------------------------------------------------------------
# nova-as
# --------------------------------------------------------------------------


def test_as_assembles_a_nob_file(workdir, capsys):
    source = write(HELLO, workdir / "hello.no")
    result = module_tool("as").main([str(source)])
    out = capsys.readouterr().out
    assert result == 0
    assert "hello.nob" in out
    target = workdir / "hello.nob"
    assert target.exists()
    nob = load_nob(target)
    assert nob.image_base == 0x1000
    assert nob.entry == 0x1000
    assert len(nob.image) == 8


def test_as_respects_image_base_and_entry_flags(workdir):
    source = write(HELLO, workdir / "hello.no")
    result = module_tool("as").main([
        "--image-base", "0x2000", "--entry", "0x2004", str(source),
    ])
    assert result == 0
    nob = load_nob(workdir / "hello.nob")
    assert nob.image_base == 0x2000
    assert nob.entry == 0x2004


def test_as_diagnostic_reports_line_and_source(workdir, capsys):
    source = write("MOVI r1, 5\nBAD r2, 3\n", workdir / "bad.no")
    result = module_tool("as").main([str(source)])
    err = capsys.readouterr().err
    assert result == 1
    assert "bad.no" in err
    assert "line 2" in err
    assert "BAD" in err
    assert "Traceback" not in err


def test_as_missing_input_file(workdir, capsys):
    result = module_tool("as").main([str(workdir / "missing.no")])
    err = capsys.readouterr().err
    assert result == 1
    assert "cannot read" in err


def test_as_invalid_address_is_a_usage_error(workdir, capsys):
    source = write(HELLO, workdir / "hello.no")
    with pytest.raises(SystemExit) as excinfo:
        module_tool("as").main(["--image-base", "oops", str(source)])
    assert excinfo.value.code == 2
    assert "invalid address" in capsys.readouterr().err


# --------------------------------------------------------------------------
# nova-objdump
# --------------------------------------------------------------------------


def test_objdump_prints_header_and_instructions(workdir, capsys):
    source = write(HELLO, workdir / "hello.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    result = module_tool("objdump").main([str(workdir / "hello.nob")])
    out = capsys.readouterr().out
    assert result == 0
    assert "image_base: 0x0000000000001000" in out
    assert "entry:      0x0000000000001000" in out
    assert "MOVI r1, 42" in out
    assert "HALT" in out


def test_objdump_roundtrips_through_as(workdir, capsys):
    program = write(TRAP_DEMO, workdir / "demo.no")
    assert module_tool("as").main([str(program)]) == 0
    capsys.readouterr()
    assert module_tool("objdump").main([str(workdir / "demo.nob")]) == 0
    out = capsys.readouterr().out
    assert "CSRR r2, ecode" in out
    assert "MRET" in out


def test_objdump_rejects_a_bad_container(workdir, capsys):
    blob = workdir / "bad.nob"
    blob.write_bytes(b"X" * 40)
    result = module_tool("objdump").main([str(blob)])
    err = capsys.readouterr().err
    assert result == 1
    assert "magic" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# nova-run
# --------------------------------------------------------------------------


def test_run_executes_to_halt(workdir, capsys):
    source = write("MOVI r1, 42\nHALT\n", workdir / "hello.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    result = module_tool("run").main([str(workdir / "hello.nob")])
    out = capsys.readouterr().out
    assert result == 0
    assert "halted after 2 step(s)" in out


def test_run_reports_the_step_budget(workdir, capsys):
    source = write("NOP\n", workdir / "spin.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    result = module_tool("run").main(["--steps", "50", str(workdir / "spin.nob")])
    captured = capsys.readouterr()
    assert result == 2
    assert "step budget (50)" in captured.out + captured.err


def test_run_stop_on_exception_reports_the_fault(workdir, capsys):
    source = write("MOVI r2, 3\nLDD r1, [r2]\nHALT\n", workdir / "fault.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    result = module_tool("run").main([
        "--stop-on-exception", str(workdir / "fault.nob"),
    ])
    out = capsys.readouterr().out
    assert result == 1
    assert "ALIGNMENT_FAULT" in out


def test_run_dump_regs(workdir, capsys):
    source = write("MOVI r1, 42\nHALT\n", workdir / "hello.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    result = module_tool("run").main(["--dump-regs", str(workdir / "hello.nob")])
    out = capsys.readouterr().out
    assert result == 0
    assert "r1=000000000000002a" in out


def test_run_honors_the_initial_privilege_flag(workdir, capsys):
    source = write("CSRW vbar, r0\nNOP\n", workdir / "priv.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    # CSRW vbar is machine-only: from user it traps and the PRIVILEGE fault
    # stays pending. From machine privilege it executes cleanly.
    user = module_tool("run").main([
        "--privilege", "user", "--steps", "20", str(workdir / "priv.nob"),
    ])
    assert user == 2
    assert "PRIVILEGE" in capsys.readouterr().out
    machine = module_tool("run").main([
        "--steps", "20", str(workdir / "priv.nob"),
    ])
    assert machine == 2
    assert "PRIVILEGE" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# End to end: the trap ABI smoke test on the real toolchain
# --------------------------------------------------------------------------


def test_end_to_end_trap_abi_smoke(workdir, capsys):
    source = write(TRAP_DEMO, workdir / "demo.no")
    assert module_tool("as").main([str(source)]) == 0
    capsys.readouterr()
    assert module_tool("objdump").main([str(workdir / "demo.nob")]) == 0
    capsys.readouterr()
    result = module_tool("run").main([
        "--dump-regs", str(workdir / "demo.nob"),
    ])
    out = capsys.readouterr().out
    assert result == 0
    assert "halted after" in out
    assert "r2=0000000000000007" in out  # ecode read by the handler
    assert "r3=0000000000000003" in out  # ecause read by the handler


# --------------------------------------------------------------------------
# python -m invocation and console-script shims
# --------------------------------------------------------------------------


def test_module_invocation_assembles_hello(workdir, cloned_env):
    source = write(HELLO, workdir / "hello.no")
    result = module_invocation("nova.as", str(source), cwd=workdir, env=cloned_env)
    assert result.returncode == 0, result.stderr
    assert (workdir / "hello.nob").exists()
    image = load_nob(workdir / "hello.nob")
    assert len(image.image) == 8


def test_module_invocation_run_hello(workdir, cloned_env):
    source = write(HELLO, workdir / "hello.no")
    assert module_invocation("nova.as", str(source), cwd=workdir,
                             env=cloned_env).returncode == 0
    result = module_invocation("nova.run", str(workdir / "hello.nob"),
                               cwd=workdir, env=cloned_env)
    assert result.returncode == 0
    assert "halted after 2 step(s)" in result.stdout


def test_module_invocation_version_flags(workdir, cloned_env):
    for tool in ("nova.as", "nova.ld", "nova.objdump", "nova.run"):
        result = module_invocation(tool, "--version", cwd=workdir, env=cloned_env)
        assert result.returncode == 0
        assert TOOLCHAIN_VERSION in result.stdout


def test_module_invocation_errors_have_no_traceback(workdir, cloned_env):
    bad = workdir / "bad.nob"
    bad.write_bytes(b"garbage")
    result = module_invocation("nova.objdump", str(bad), cwd=workdir,
                               env=cloned_env)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    run = module_invocation("nova.run", str(bad), cwd=workdir, env=cloned_env)
    assert run.returncode == 1
    assert "Traceback" not in run.stderr


# --------------------------------------------------------------------------
# nova-ld
# --------------------------------------------------------------------------


def _two_objects(workdir):
    main = write("""
.global _start
_start:
    LI r1, 3
    CALL math_square
    HALT
""", workdir / "main.no")
    math = write("""
.global math_square
math_square:
    MUL r1, r1, r1
    RET
""", workdir / "math.no")
    assert module_tool("as").main(["-o", str(workdir / "main.nobj"),
                                   str(main)]) == 0
    assert module_tool("as").main(["-o", str(workdir / "math.nobj"),
                                   str(math)]) == 0
    return main, math


def test_ld_links_objects_into_an_executable(workdir, cloned_env, capsys):
    _two_objects(workdir)
    result = module_tool("ld").main([
        "-o", str(workdir / "program.nob"),
        str(workdir / "main.nobj"),
        str(workdir / "math.nobj"),
    ])
    out = capsys.readouterr().out
    assert result == 0
    assert "program.nob" in out
    assert "2 object(s)" in out
    nob = load_nob(workdir / "program.nob")
    assert nob.image_base == 0x1000
    run = module_invocation("nova.run", str(workdir / "program.nob"),
                            cwd=workdir, env=cloned_env)
    assert run.returncode == 0
    assert "halted after" in run.stdout


def test_ld_default_output_is_the_first_input_with_nob_suffix(workdir, capsys):
    main, math = _two_objects(workdir)
    assert module_tool("ld").main(
        [str(workdir / "main.nobj"), str(workdir / "math.nobj")]) == 0
    assert (workdir / "main.nob").exists()
    capsys.readouterr()


def test_ld_custom_entry_and_base(workdir, capsys):
    main = write("""
.global entry
entry:
    MOVI r1, 5
    HALT
""", workdir / "custom.no")
    assert module_tool("as").main(["-o", str(workdir / "custom.nobj"),
                                   str(main)]) == 0
    result = module_tool("ld").main([
        "--entry", "entry", "--base", "0x2000",
        "-o", str(workdir / "program.nob"), str(workdir / "custom.nobj"),
    ])
    capsys.readouterr()
    assert result == 0
    nob = load_nob(workdir / "program.nob")
    assert nob.image_base == 0x2000
    assert nob.entry == 0x2000


def test_ld_missing_input_is_a_usage_error(workdir, cloned_env):
    result = module_invocation("nova.ld", cwd=workdir, env=cloned_env)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_ld_malformed_object_exits_one(workdir, capsys):
    bad = workdir / "bad.nobj"
    bad.write_bytes(b"garbage")
    assert module_tool("ld").main(["-o", str(workdir / "o.nob"), str(bad)]) == 1
    assert "bad.nobj" in capsys.readouterr().err


def test_ld_undefined_symbol_diagnostic_has_no_traceback(workdir, capsys):
    main = write("""
.global _start
_start:
    CALL missing
    HALT
""", workdir / "main.no")
    assert module_tool("as").main(["-o", str(workdir / "main.nobj"),
                                   str(main)]) == 0
    result = module_tool("ld").main([str(workdir / "main.nobj")])
    err = capsys.readouterr().err
    assert result == 1
    assert "undefined symbol 'missing'" in err
    assert "Traceback" not in err


def test_cli_shim_entry_points_route_to_the_tools(workdir, capsys):
    source = write(HELLO, workdir / "hello.no")
    assert assemble_main([str(source)]) == 0
    capsys.readouterr()
    assert objdump_main([str(workdir / "hello.nob")]) == 0
    capsys.readouterr()
    assert run_main([str(workdir / "hello.nob")]) == 0
    assert "halted after 2 step(s)" in capsys.readouterr().out
    # Plain safe error returns, no exceptions leaking out of the shim.
    assert run_main([str(workdir / "missing.nob")]) == 1


def test_decode_nob_of_the_cli_output_is_the_python_api_image(workdir):
    source = write(HELLO, workdir / "hello.no")
    assert module_tool("as").main([str(source)]) == 0
    blob = (workdir / "hello.nob").read_bytes()
    from nova.assembler import assemble_text
    nob = decode_nob(blob)
    assert nob.image == assemble_text(HELLO, entry=nob.image_base)