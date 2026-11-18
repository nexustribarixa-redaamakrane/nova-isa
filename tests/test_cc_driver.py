"""CLI, toolchain-integration, and execution tests for ``nova-cc``.

The tests exercise the real driver entry point (``nova.cc.driver.main``) and
the real assembler, linker, object format, and emulator.  Tests are grouped so
the counts for CLI behaviour, preprocessing, assembly, object/link handling, and
emulator execution can be reported separately.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nova import PRIVILEGE_LEVELS, assemble_object, load_nob, write_object
from nova.cc.driver import main
from nova.emulator import NovaMachine
from nova.object import load_object

REPO_ROOT = Path(__file__).resolve().parents[1]

CRT0 = """
.global _start
_start:
    LI sp, 0x80000
    CALL run
    HALT
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def write_crt0(directory: Path) -> Path:
    path = directory / "crt0.nobj"
    write_object(path, assemble_object(CRT0))
    return path


def invoke(*args) -> int:
    return main([str(item) for item in args])


def run_executable(path: Path, steps: int = 300_000) -> int:
    image = load_nob(path)
    machine = NovaMachine(code=image.image, entry=image.entry)
    machine.priv = PRIVILEGE_LEVELS["machine"]
    for _ in range(steps):
        if machine.halted:
            break
        machine.step()
    assert machine.halted, f"{path} did not halt (pc=0x{machine.pc:x})"
    return machine.regs[1]


def build_and_run(tmp_path: Path, *sources: Path, long_calls: bool = False) -> int:
    output = tmp_path / "program.nob"
    args = [write_crt0(tmp_path), *sources]
    if long_calls:
        args.insert(0, "--long-calls")
    assert invoke(*args, "-o", output) == 0
    return run_executable(output)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestCLI:
    def test_help_lists_modes_and_options(self, capsys):
        assert main(["--help"]) == 0
        out = capsys.readouterr().out
        for option in ("-E", "-S", "-c", "-o", "-I", "-D", "-U", "-std",
                       "--target", "--version", "--long-calls",
                       "--long-branches"):
            assert option in out
        assert "nova64" in out

    def test_version_reports_toolchain_versions(self, capsys):
        assert main(["--version"]) == 0
        out = capsys.readouterr().out
        assert "nova-cc" in out
        assert "1.1.0" in out
        assert "Nova ISA v1.0" in out
        assert "Nova Linker v0.1" in out

    def test_no_input_is_a_usage_error(self, capsys):
        assert main([]) == 2
        err = capsys.readouterr().err
        assert "no input files" in err
        assert "nova-cc --help" in err

    def test_unknown_option_is_a_usage_error(self, capsys):
        assert main(["--not-an-option"]) == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    @pytest.mark.parametrize("args,needle", [
        (["--target=nova32"], "unsupported target"),
        (["-std=c17"], "unsupported language mode"),
        (["-O2"], "unsupported optimization level"),
    ])
    def test_unsupported_options_are_rejected(self, args, needle, tmp_path, capsys):
        source = write(tmp_path, "x.c", "int run(void) { return 0; }\n")
        assert invoke(*args, source) == 2
        assert needle in capsys.readouterr().err

    def test_missing_input_is_an_error_not_a_traceback(self, tmp_path, capsys):
        assert invoke(tmp_path / "absent.c") == 1
        err = capsys.readouterr().err
        assert "cannot read" in err
        assert "Traceback" not in err

    def test_conflicting_modes_are_rejected(self, tmp_path):
        source = write(tmp_path, "x.c", "int run(void) { return 0; }\n")
        assert invoke("-E", "-c", source) == 2

    def test_entry_option_is_rejected_outside_link_mode(self, tmp_path, capsys):
        source = write(tmp_path, "x.c", "int run(void) { return 0; }\n")
        assert invoke("-c", "-e", "run", source) == 2
        assert "only meaningful when linking" in capsys.readouterr().err

    def test_module_entry_point(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "nova.cc", "--version"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "nova-cc" in result.stdout

    def test_installed_console_script(self):
        script = shutil.which("nova-cc")
        if script is None:
            pytest.skip("nova-cc console script is not installed in this environment")
        result = subprocess.run([script, "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "nova-cc" in result.stdout


# ---------------------------------------------------------------------------
# Deterministic naming, safe writes, and output handling
# ---------------------------------------------------------------------------

class TestOutputBehavior:
    def test_default_output_names(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        assert invoke("-E", source) == 0
        assert invoke("-S", source) == 0
        assert invoke("-c", source) == 0
        assert (tmp_path / "sample.i").is_file()
        assert (tmp_path / "sample.s").is_file()
        assert (tmp_path / "sample.nobj").is_file()

    def test_explicit_output_paths(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        assert invoke("-S", source, "-o", tmp_path / "out" / "custom.s") != 0
        (tmp_path / "out").mkdir()
        assert invoke("-S", source, "-o", tmp_path / "out" / "custom.s") == 0
        assert (tmp_path / "out" / "custom.s").is_file()

    def test_multiple_outputs_without_o(self, tmp_path):
        first = write(tmp_path, "first.c", "int a(void) { return 1; }\n")
        second = write(tmp_path, "second.c", "int b(void) { return 2; }\n")
        assert invoke("-S", first, second) == 0
        assert (tmp_path / "first.s").is_file()
        assert (tmp_path / "second.s").is_file()

    def test_o_with_multiple_inputs_requires_linking(self, tmp_path, capsys):
        first = write(tmp_path, "first.c", "int a(void) { return 1; }\n")
        second = write(tmp_path, "second.c", "int b(void) { return 2; }\n")
        assert invoke("-c", first, second, "-o", tmp_path / "both.nobj") == 2
        assert "multiple inputs requires link mode" in capsys.readouterr().err

    def test_refuses_to_overwrite_an_input_file(self, tmp_path, capsys):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        original = source.read_text(encoding="utf-8")
        assert invoke("-c", source, "-o", source) == 1
        assert "refusing to overwrite input file" in capsys.readouterr().err
        assert source.read_text(encoding="utf-8") == original

    def test_existing_output_is_replaced(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        output = tmp_path / "sample.nobj"
        output.write_bytes(b"stale")
        assert invoke("-c", source) == 0
        assert output.read_bytes()[:4] != b"stal"

    def test_failure_writes_no_partial_outputs(self, tmp_path):
        good = write(tmp_path, "good.c", "int run(void) { return 1; }\n")
        bad = write(tmp_path, "bad.c", "int run(void) { return missing; }\n")
        assert invoke("-c", good, bad) == 1
        assert not (tmp_path / "good.nobj").exists()
        assert not (tmp_path / "bad.nobj").exists()

    def test_missing_output_directory_is_diagnosed(self, tmp_path, capsys):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        target = tmp_path / "missing" / "sample.s"
        assert invoke("-S", source, "-o", target) == 1
        assert "does not exist" in capsys.readouterr().err
        assert not target.exists()

    def test_no_temporary_files_left_after_failure(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return nope; }\n")
        assert invoke("-c", source) == 1
        assert not list(tmp_path.glob(".*.tmp"))

    def test_default_link_output_is_a_nob(self, tmp_path, monkeypatch):
        source = write(tmp_path, "sample.c", "int run(void) { return 9; }\n")
        crt = write_crt0(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert invoke(crt, source) == 0
        assert (tmp_path / "a.nob").is_file()
        assert run_executable(tmp_path / "a.nob") == 9


# ---------------------------------------------------------------------------
# Preprocessor integration
# ---------------------------------------------------------------------------

class TestPreprocessing:
    def test_define_undefine_and_include_paths(self, tmp_path):
        include = tmp_path / "include"
        include.mkdir()
        write(include, "values.h", "#define BASE 30\n")
        source = write(
            tmp_path, "sample.c",
            """
            #include <values.h>
            #ifdef EXTRA
            #define ADD EXTRA
            #else
            #define ADD 0
            #endif
            int run(void) { return BASE + ADD; }
            """,
        )
        assert invoke("-E", "-I", include, "-D", "EXTRA=12", source) == 0
        text = (tmp_path / "sample.i").read_text(encoding="utf-8")
        assert "30" in text and "12" in text

    def test_undefine_removes_define_and_builtin(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            #ifdef FLAG
            PRESENT
            #else
            ABSENT
            #endif
            #ifdef __STDC__
            STDC
            #else
            NO_STDC
            #endif
            """,
        )
        assert invoke("-E", "-D", "FLAG=1", "-U", "FLAG", "-U", "__STDC__",
                      source) == 0
        text = (tmp_path / "sample.i").read_text(encoding="utf-8")
        assert "ABSENT" in text and "NO_STDC" in text

    def test_define_and_undefine_follow_command_line_order(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            "#ifdef FLAG\nVALUE\n#else\nNONE\n#endif\n",
        )
        assert invoke("-E", "-U", "FLAG", "-D", "FLAG=2", source) == 0
        assert "VALUE" in (tmp_path / "sample.i").read_text(encoding="utf-8")
        assert invoke("-E", "-D", "FLAG=2", "-U", "FLAG", source) == 0
        assert "NONE" in (tmp_path / "sample.i").read_text(encoding="utf-8")

    @pytest.mark.parametrize("mode,expected", [
        ("nova-c11", "NO_VERSION"),
        ("c11", "VERSION"),
        ("c99", "VERSION"),
        ("c89", "NO_VERSION"),
    ])
    def test_language_mode_reaches_preprocessor(self, tmp_path, mode, expected):
        source = write(
            tmp_path, "sample.c",
            "#ifdef __STDC_VERSION__\nVERSION\n#else\nNO_VERSION\n#endif\n",
        )
        assert invoke("-E", f"-std={mode}", source) == 0
        assert expected in (tmp_path / "sample.i").read_text(encoding="utf-8")

    def test_file_and_line_builtins_are_preserved(self, tmp_path):
        source = write(
            tmp_path, "where.c",
            """
            const char *file_name(void) { return __FILE__; }
            int line_number(void) { return __LINE__; }
            """,
        )
        assert invoke("-E", source) == 0
        text = (tmp_path / "where.i").read_text(encoding="utf-8")
        assert "where.c" in text
        compact = "".join(text.split())
        assert "line_number(void){return3;" in compact

    def test_preprocessor_error_has_location_and_no_output(self, tmp_path, capsys):
        source = write(tmp_path, "sample.c", "#include <absent.h>\n")
        assert invoke("-E", source) == 1
        err = capsys.readouterr().err
        assert "sample.c:1:1" in err
        assert "preprocessing" in err
        assert not (tmp_path / "sample.i").exists()


# ---------------------------------------------------------------------------
# Assembly generation
# ---------------------------------------------------------------------------

class TestAssembly:
    def test_s_emits_structural_nova_assembly(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            int answer(void) { return 6 * 7; }
            int run(void) { return answer(); }
            """,
        )
        assert invoke("-S", source) == 0
        assembly = (tmp_path / "sample.s").read_text(encoding="utf-8")
        assert assembly.startswith(".global")
        assert ".section .text" in assembly
        assert ".type answer, function" in assembly
        assert "CALL answer" in assembly
        assert ".size answer, " in assembly

    def test_s_is_deterministic(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return 1; }\n")
        assert invoke("-S", source, "-o", tmp_path / "one.s") == 0
        assert invoke("-S", source, "-o", tmp_path / "two.s") == 0
        assert (tmp_path / "one.s").read_bytes() == (tmp_path / "two.s").read_bytes()

    def test_long_call_assembly_uses_bl(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            "int answer(void) { return 42; } int run(void) { return answer(); }\n",
        )
        assert invoke("-S", "--long-calls", source) == 0
        assembly = (tmp_path / "sample.s").read_text(encoding="utf-8")
        assert "BL answer" in assembly
        assert "CALL answer" not in assembly

    def test_long_branch_assembly_inverts_short_branch_around_bl(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            int run(void) {
                int x = 0;
                if (x == 0) { x = 1; }
                return x;
            }
            """,
        )
        assert invoke("-S", "--long-branches", source) == 0
        assembly = (tmp_path / "sample.s").read_text(encoding="utf-8")
        # Conditional branches become an inverted branch to a nearby skip
        # label followed by the documented BL long jump.
        assert "BNE " in assembly or "BEQ " in assembly
        assert "BL .Lcgu_" in assembly
        # No branch targets the far label directly.
        assert "BEQ r0, r0, .Lcgu_" not in assembly

    def test_semantic_error_has_stage_and_location(self, tmp_path, capsys):
        source = write(tmp_path, "sample.c", "int run(void) { return unknown; }\n")
        assert invoke("-S", source) == 1
        err = capsys.readouterr().err
        assert "sample.c:1:" in err
        assert "semantic analysis" in err
        assert not (tmp_path / "sample.s").exists()


# ---------------------------------------------------------------------------
# Unsupported constructs: stage, file, line and column
# ---------------------------------------------------------------------------

class TestDiagnostics:
    @pytest.mark.parametrize("source_text,needle", [
        ("double x; int run(void) { return x; }", "floating-point"),
        ("struct S { int x : 3; }; int run(void) { struct S s; return s.x; }",
         "bit-field access"),
        ("int run(int n) { int a[n]; return 0; }",
         "variable length arrays are not supported"),
        ("int f(void) { return 1; } int x = f(); int run(void) { return x; }",
         "non-constant data initializer"),
        ("struct S; int x = sizeof(struct S); int run(void) { return 0; }",
         "sizeof incomplete type"),
    ])
    def test_unsupported_construct_is_source_located_with_stage(
            self, tmp_path, capsys, source_text, needle):
        source = write(tmp_path, "bad.c", source_text + "\n")
        assert invoke("-S", source) == 1
        err = capsys.readouterr().err
        assert needle in err
        assert re.search(
            r"bad\.c:1:\d+: error: (semantic analysis|code generation):", err
        )
        assert "Traceback" not in err
        assert not (tmp_path / "bad.s").exists()


# ---------------------------------------------------------------------------
# Object generation and linking
# ---------------------------------------------------------------------------

class TestObjectAndLink:
    def test_c_emits_a_valid_relocatable_object(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            "extern int value; int run(void) { return value; }\n",
        )
        assert invoke("-c", source) == 0
        obj = load_object(tmp_path / "sample.nobj")
        assert any(s.name == "run" and s.type == "function" for s in obj.symbols)
        assert any(s.name == "value" and not s.is_defined for s in obj.symbols)
        assert {r.kind for r in obj.relocations} >= {"LA64"}
        assert obj.sections[0].name == ".text"

    def test_compile_objects_then_link(self, tmp_path):
        main = write(
            tmp_path, "main.c",
            """
            extern int offset;
            int helper(void);
            int run(void) { return helper() + offset; }
            """,
        )
        helper = write(
            tmp_path, "helper.c",
            "int offset = 5; int helper(void) { return 37; }\n",
        )
        assert invoke("-c", main) == 0
        assert invoke("-c", helper) == 0
        output = tmp_path / "program.nob"
        assert invoke(write_crt0(tmp_path),
                      tmp_path / "main.nobj", tmp_path / "helper.nobj",
                      "-o", output) == 0
        assert run_executable(output) == 42

    def test_multiple_sources_link_directly(self, tmp_path):
        main = write(
            tmp_path, "main.c",
            "int helper(void); int run(void) { return helper(); }\n",
        )
        helper = write(tmp_path, "helper.c", "int helper(void) { return 33; }\n")
        assert build_and_run(tmp_path, main, helper) == 33

    def test_cross_file_global_data(self, tmp_path):
        main = write(
            tmp_path, "main.c",
            """
            extern int counter;
            void bump(void);
            int run(void) { bump(); bump(); return counter; }
            """,
        )
        data = write(
            tmp_path, "data.c",
            "int counter = 10; void bump(void) { counter += 3; }\n",
        )
        assert build_and_run(tmp_path, main, data) == 16

    def test_static_names_do_not_collide_across_translation_units(self, tmp_path):
        first = write(
            tmp_path, "first.c",
            """
            static int value = 3;
            static int helper(void) { return value; }
            int get_first(void) { return helper(); }
            """,
        )
        second = write(
            tmp_path, "second.c",
            """
            static int value = 4;
            static int helper(void) { return value; }
            int get_second(void) { return helper(); }
            """,
        )
        main_source = write(
            tmp_path, "main.c",
            """
            int get_first(void);
            int get_second(void);
            int run(void) { return get_first() * 10 + get_second(); }
            """,
        )
        assert build_and_run(tmp_path, first, second, main_source) == 34

    def test_duplicate_definition_is_diagnosed(self, tmp_path, capsys):
        first = write(
            tmp_path, "first.c",
            "int clash(void) { return 1; } int run(void) { return clash(); }\n",
        )
        second = write(tmp_path, "second.c", "int clash(void) { return 2; }\n")
        assert invoke(write_crt0(tmp_path), first, second,
                      "-o", tmp_path / "program.nob") == 1
        assert "multiple definition of 'clash'" in capsys.readouterr().err
        assert not (tmp_path / "program.nob").exists()

    def test_undefined_symbol_is_diagnosed(self, tmp_path, capsys):
        source = write(
            tmp_path, "main.c",
            "int missing(void); int run(void) { return missing(); }\n",
        )
        assert invoke(write_crt0(tmp_path), source,
                      "-o", tmp_path / "program.nob") == 1
        err = capsys.readouterr().err
        assert "undefined symbol 'missing'" in err
        assert "link:" in err

    def test_missing_object_input_is_diagnosed(self, tmp_path, capsys):
        assert invoke(tmp_path / "absent.nobj", "-o", tmp_path / "x.nob") == 1
        assert "cannot read object" in capsys.readouterr().err

    def test_linking_is_deterministic(self, tmp_path):
        source = write(tmp_path, "sample.c", "int run(void) { return 5; }\n")
        crt = write_crt0(tmp_path)
        assert invoke(crt, source, "-o", tmp_path / "one.nob") == 0
        assert invoke(crt, source, "-o", tmp_path / "two.nob") == 0
        assert (tmp_path / "one.nob").read_bytes() == (tmp_path / "two.nob").read_bytes()


# ---------------------------------------------------------------------------
# Emulator execution through the real CLI
# ---------------------------------------------------------------------------

class TestExecution:
    def test_simple_program_returns_expected_value(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            int square(int value) { return value * value; }
            int run(void) { return square(6) + square(1); }
            """,
        )
        assert build_and_run(tmp_path, source) == 37

    def test_control_flow_program(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            int run(void) {
                int total = 0;
                for (int i = 0; i < 10; i++) {
                    if (i == 5) continue;
                    total += i;
                }
                return total;
            }
            """,
        )
        assert build_and_run(tmp_path, source) == 40

    def test_aggregate_arguments_and_return(self, tmp_path):
        source = write(
            tmp_path, "sample.c",
            """
            struct Pair { int left; int right; };
            struct Pair make_pair(int a, int b) {
                struct Pair p;
                p.left = a;
                p.right = b;
                return p;
            }
            int run(void) {
                struct Pair p = make_pair(20, 22);
                return p.left + p.right;
            }
            """,
        )
        assert build_and_run(tmp_path, source) == 42

    def test_long_call_outside_short_range_requires_flag_and_then_runs(self, tmp_path):
        body = " ".join("x += 1;" for _ in range(600))
        source = write(
            tmp_path, "big.c",
            f"""
            int main(void);
            int run(void) {{ return main(); }}
            int filler(int x) {{ {body} return x; }}
            int main(void) {{ return 42; }}
            """,
        )
        crt = write_crt0(tmp_path)
        short_output = tmp_path / "short.nob"
        assert invoke(crt, source, "-o", short_output) == 1
        assert not short_output.exists()
        assert run_executable(
            _build_long(tmp_path, crt, source)
        ) == 42

    def test_long_branch_outside_short_range_requires_flag_and_then_runs(
            self, tmp_path, capsys):
        body = " ".join("x += 1;" for _ in range(700))
        source = write(
            tmp_path, "bigbranch.c",
            f"int run(void) {{ int x = 0; if (x == 0) {{ {body} }} return x; }}\n",
        )
        crt = write_crt0(tmp_path)
        short_output = tmp_path / "short.nob"
        assert invoke(crt, source, "-o", short_output) == 1
        err = capsys.readouterr().err
        assert "--long-branches" in err
        assert "--long-calls" not in err
        assert not short_output.exists()

        long_output = tmp_path / "long.nob"
        assert invoke("--long-branches", crt, source, "-o", long_output) == 0
        assert run_executable(long_output) == 700


def _build_long(tmp_path: Path, crt: Path, source: Path) -> Path:
    output = tmp_path / "long.nob"
    assert invoke("--long-calls", crt, source, "-o", output) == 0
    return output
