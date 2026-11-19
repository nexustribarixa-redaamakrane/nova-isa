"""Freestanding runtime, libnova, variadic, and formatted-output tests.

Every execution test links through the real ``nova-cc`` driver, loads the
resulting ``.nob`` at its image base, and runs the reference emulator with a
host-side syscall dispatcher (the reference model has no kernel).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nova import PRIVILEGE_LEVELS, assemble_object, load_nob, write_object
from nova.cc.driver import main
from nova.emulator import NovaMachine
from nova.isa import EXCEPTION_CODES
from nova.runtime import (
    INCLUDE_DIR,
    LIBRARY_SOURCES,
    library_objects,
    runtime_objects,
    startup_object,
    syscall_object,
)
from nova.syscalls import dispatch_syscall

REQUIRED_HEADERS = (
    "limits.h",
    "nova.h",
    "stdarg.h",
    "stdbool.h",
    "stddef.h",
    "stdint.h",
    "stdio.h",
    "stdlib.h",
    "string.h",
)


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def invoke(*args) -> int:
    return main([str(item) for item in args])


def run_image(path: Path, steps: int = 2_000_000, *, time: int | None = None):
    """Load a ``.nob`` at its image base and run it with syscall dispatch."""
    image = load_nob(path)
    machine = NovaMachine()
    machine.load_code(image.image, image.image_base)
    machine.pc = image.entry
    machine.priv = PRIVILEGE_LEVELS["machine"]
    if time is not None:
        machine.write_csr("time", time)
    for _ in range(steps):
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
        elif machine.exception_code != EXCEPTION_CODES["NONE"]:
            raise AssertionError(
                f"{path} raised exception {machine.exception_code} "
                f"at 0x{machine.epc:x} (detail 0x{machine.ecause:x})"
            )
    assert machine.halted, f"{path} did not halt (pc=0x{machine.pc:x})"
    return machine


def run_program(tmp_path: Path, *sources: Path, options=(), steps: int = 2_000_000,
                time: int | None = None):
    output = tmp_path / "program.nob"
    assert invoke(*options, *sources, "-o", output) == 0
    machine = run_image(output, steps, time=time)
    output_bytes = b"".join(data for fd, data in machine.io_log if fd == 1)
    return machine, output_bytes


def runtime_program(tmp_path: Path, name: str, source: str, *,
                    options=("--runtime",), steps: int = 2_000_000,
                    time: int | None = None):
    path = write(tmp_path, name, source)
    return run_program(tmp_path, path, options=options, steps=steps, time=time)


# ---------------------------------------------------------------------------
# Runtime package and object structure
# ---------------------------------------------------------------------------

class TestRuntimePackage:
    def test_required_headers_are_installed(self):
        for name in REQUIRED_HEADERS:
            assert (INCLUDE_DIR / name).is_file(), name

    def test_runtime_objects_have_expected_symbols(self):
        startup = startup_object()
        syscalls = syscall_object()
        library = library_objects()
        assert len(library) == len(LIBRARY_SOURCES) == 3

        startup_names = {symbol.name for symbol in startup.symbols}
        assert "_start" in startup_names

        syscall_names = {symbol.name for symbol in syscalls.symbols}
        assert {"nova_write", "write", "nova_read", "read", "nova_time",
                "nova_exit", "nova_halt"} <= syscall_names

        library_names = {symbol.name for item in library for symbol in item.symbols}
        assert {"memcpy", "memmove", "memset", "memcmp", "strlen", "strcmp",
                "strncmp", "strcpy", "strncpy", "strcat"} <= library_names
        assert {"atoi", "strtol", "strtoul", "exit", "abort"} <= library_names
        assert {"printf", "putchar", "puts"} <= library_names

    def test_runtime_object_selection_and_independence(self):
        assert len(runtime_objects()) == 5
        assert len(runtime_objects(with_startup=False)) == 4
        assert len(runtime_objects(with_library=False)) == 2

        first = runtime_objects()
        second = runtime_objects()
        assert first is not second
        assert all(a is not b for a, b in zip(first, second))
        assert [a.sections[0].data for a in first] == \
            [b.sections[0].data for b in second]

    def test_headers_compile_together(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "headers.c",
            """
            #include <limits.h>
            #include <stdarg.h>
            #include <stdbool.h>
            #include <stddef.h>
            #include <stdint.h>

            struct Pair { char first; long second; };

            int main(void) {
                size_t size = 3;
                ptrdiff_t difference = -1;
                bool flag = true;
                uint8_t byte = UINT8_MAX;
                int16_t small = INT16_MIN;
                uint32_t word = UINT32_MAX;
                int64_t wide = INT64_MAX;
                if (sizeof(int32_t) != 4 || sizeof(uint64_t) != 8) return 1;
                if (CHAR_BIT != 8 || CHAR_MAX != 255) return 2;
                if (INT_MAX != 2147483647 || LONG_MAX != INT64_MAX) return 3;
                if (offsetof(struct Pair, second) != 8) return 4;
                if (!flag || size != 3 || difference != -1) return 5;
                if (byte != 255 || small != -32768 || word != 4294967295U) return 6;
                if (wide != 9223372036854775807L) return 7;
                return 0;
            }
            """,
        )
        assert machine.regs[1] == 0
        assert output == b""


# ---------------------------------------------------------------------------
# Startup and syscall wrappers
# ---------------------------------------------------------------------------

class TestStartupAndSyscalls:
    def test_startup_calls_main_and_halts_with_its_return_value(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "main.c",
            """
            int main(void) { return 42; }
            """,
        )
        assert machine.regs[1] == 42
        assert machine.regs[29] == 0x80000
        assert output == b""

    def test_nova_write_returns_count_and_records_output(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "write.c",
            """
            #include <nova.h>
            int main(void) {
                const char *text = "abc";
                if (nova_write(1, text, 3) != 3) return 1;
                if (nova_write(2, text, 1) != 1) return 2;
                return 0;
            }
            """,
        )
        assert machine.regs[1] == 0
        assert output == b"abc"
        assert (2, b"a") in machine.io_log

    def test_nova_read_reports_the_reference_model_limitation(self, tmp_path):
        machine, _ = runtime_program(
            tmp_path, "read.c",
            """
            #include <nova.h>
            int main(void) {
                char buffer[4];
                long result = nova_read(0, buffer, 4);
                return result == -1 ? 0 : 1;
            }
            """,
        )
        assert machine.regs[1] == 0

    def test_nova_time_reads_the_time_csr(self, tmp_path):
        machine, _ = runtime_program(
            tmp_path, "time.c",
            """
            #include <nova.h>
            int main(void) { return (int)nova_time(); }
            """,
            time=1234,
        )
        assert 1234 <= machine.regs[1] < 5000

    def test_nova_exit_halts_through_scall(self, tmp_path):
        machine, _ = runtime_program(
            tmp_path, "exit.c",
            """
            #include <nova.h>
            int main(void) { nova_exit(0); return 1; }
            """,
        )
        assert machine.halted

    def test_nova_halt_is_architectural_and_leaves_status_in_a0(self, tmp_path):
        machine, _ = runtime_program(
            tmp_path, "halt.c",
            """
            #include <nova.h>
            int main(void) { nova_halt(123); return 1; }
            """,
        )
        assert machine.regs[1] == 123


# ---------------------------------------------------------------------------
# libnova memory, string, and integer conversion
# ---------------------------------------------------------------------------

LIBRARY_PROGRAM = """
#include <limits.h>
#include <stdlib.h>
#include <string.h>

static int strings(void)
{
    char buffer[32];
    char copy[8];
    char moved[16] = "abcdefghijklmno";
    char bytes[8];

    if (strlen("") != 0) return 1;
    if (strlen("hello") != 5) return 2;
    if (strcmp("abc", "abc") != 0) return 3;
    if (strcmp("abc", "abd") >= 0) return 4;
    if (strcmp("abd", "abc") <= 0) return 5;
    if (strncmp("abcdef", "abcxyz", 3) != 0) return 6;
    if (strncmp("abcdef", "abcxyz", 4) >= 0) return 7;
    if (strcpy(buffer, "world") != buffer) return 8;
    if (strcmp(buffer, "world") != 0) return 9;
    if (strncpy(copy, "hi", 5) != copy) return 10;
    if (copy[0] != 'h' || copy[2] != 0 || copy[4] != 0) return 11;
    strcpy(buffer, "hello");
    if (strcat(buffer, " world") != buffer) return 12;
    if (strcmp(buffer, "hello world") != 0) return 13;
    if (memcpy(bytes, "abcdef", 6) != bytes) return 14;
    if (bytes[0] != 'a' || bytes[5] != 'f') return 15;
    if (memmove(moved + 2, moved, 5) != moved + 2) return 16;
    if (strncmp(moved, "ababcde", 7) != 0) return 17;
    strcpy(moved, "abcdefghijklmno");
    if (memmove(moved, moved + 2, 5) != moved) return 18;
    if (strncmp(moved, "cdefg", 5) != 0) return 19;
    if (memset(bytes, 0x5a, 8) != bytes) return 20;
    if (bytes[0] != 0x5a || bytes[7] != 0x5a) return 21;
    if (memcmp("abc", "abc", 3) != 0) return 22;
    if (memcmp("abc", "abd", 3) >= 0) return 23;
    if (memcmp("abd", "abc", 3) <= 0) return 24;
    if (memcmp(bytes, bytes, 0) != 0) return 25;
    return 0;
}

static int conversions(void)
{
    char *end;

    if (atoi("0") != 0) return 1;
    if (atoi("42") != 42) return 2;
    if (atoi("  -17xyz") != -17) return 3;
    if (atoi("xyz") != 0) return 4;
    if (strtol("123", &end, 10) != 123 || *end != 0) return 5;
    if (strtol("ff", &end, 16) != 255 || *end != 0) return 6;
    if (strtol("0x20", &end, 0) != 32 || *end != 0) return 7;
    if (strtol("010", &end, 0) != 8 || *end != 0) return 8;
    if (strtol("-99", &end, 10) != -99 || *end != 0) return 9;
    if (strtol("12abc", &end, 10) != 12 || *end != 'a') return 10;
    if (strtol("abc", &end, 10) != 0 || end == 0 || *end != 'a') return 11;
    if (strtol("zz", &end, 36) != 35 * 36 + 35 || *end != 0) return 12;
    if (strtol("10", &end, 1) != 0 || *end != '1') return 13;
    if (strtoul("18446744073709551615", &end, 10) != ULONG_MAX) return 14;
    if (strtoul("-1", &end, 10) != ULONG_MAX) return 15;
    if (strtol("9223372036854775807", &end, 10) != LONG_MAX) return 16;
    if (strtol("-9223372036854775808", &end, 10) != LONG_MIN) return 17;
    if (strtol("999999999999999999999999", &end, 10) != LONG_MAX) return 18;
    if (strtol("-999999999999999999999999", &end, 10) != LONG_MIN) return 19;
    return 0;
}

int main(void)
{
    int result = strings();
    if (result != 0) return result;
    result = conversions();
    if (result != 0) return 100 + result;
    return 0;
}
"""


class TestLibnova:
    def test_memory_string_and_conversion_routines(self, tmp_path):
        machine, output = runtime_program(tmp_path, "libnova.c", LIBRARY_PROGRAM)
        assert machine.regs[1] == 0
        assert output == b""


# ---------------------------------------------------------------------------
# Variadic functions
# ---------------------------------------------------------------------------

VARIADIC_PROGRAM = """
#include <stdarg.h>
#include <string.h>

struct Six { char a[6]; };
struct Twelve { int a; int b; int c; };
struct Sixteen { unsigned long a; unsigned long b; };

static int no_variadic_reads(int fixed, ...)
{
    va_list ap;
    va_start(ap, fixed);
    va_end(ap);
    return fixed;
}

static int sum_ten(int fixed, ...)
{
    va_list ap;
    int i;
    int total = fixed;
    va_start(ap, fixed);
    for (i = 0; i < 10; i++) {
        total += va_arg(ap, int);
    }
    va_end(ap);
    return total;
}

static int sum_twelve(int a, int b, int c, int d, int e, ...)
{
    va_list ap;
    int total = a + b + c + d + e;
    va_start(ap, e);
    total += va_arg(ap, int);
    total += va_arg(ap, int);
    va_end(ap);
    return total;
}

static int narrow(int fixed, ...)
{
    va_list ap;
    int total = fixed;
    va_start(ap, fixed);
    total += va_arg(ap, int);
    total += va_arg(ap, int);
    va_end(ap);
    return total;
}

static int mixed(int fixed, ...)
{
    va_list ap;
    const char *text;
    int value;
    int result;
    va_start(ap, fixed);
    value = va_arg(ap, int);
    text = va_arg(ap, const char *);
    result = value + (int)strlen(text);
    va_end(ap);
    return result;
}

static int copy_test(int fixed, ...)
{
    va_list ap;
    va_list copy;
    int a;
    int b;
    int c;

    va_start(ap, fixed);
    a = va_arg(ap, int);
    va_copy(copy, ap);
    b = va_arg(ap, int);
    c = va_arg(copy, int);
    va_end(copy);
    va_end(ap);
    return a * 100 + b * 10 + c;
}

static int two_lists(int fixed, ...)
{
    va_list first;
    va_list second;
    int a;
    int b;

    va_start(first, fixed);
    va_start(second, fixed);
    a = va_arg(first, int) + va_arg(first, int);
    b = va_arg(second, int) + va_arg(second, int);
    va_end(second);
    va_end(first);
    return a * 100 + b;
}

static int repeated(int fixed, ...)
{
    va_list ap;
    int first;
    int second;

    va_start(ap, fixed);
    first = va_arg(ap, int);
    va_end(ap);
    va_start(ap, fixed);
    second = va_arg(ap, int);
    va_end(ap);
    return first * 10 + second;
}

static int six_struct(int fixed, ...)
{
    va_list ap;
    struct Six s;
    va_start(ap, fixed);
    s = va_arg(ap, struct Six);
    va_end(ap);
    return s.a[0] + s.a[5];
}

static int twelve_struct(int a, int b, int c, int d, int e, ...)
{
    va_list ap;
    struct Twelve t;
    int total = a + b + c + d + e;
    va_start(ap, e);
    t = va_arg(ap, struct Twelve);
    va_end(ap);
    return total + t.a + t.b + t.c;
}

static int sixteen_struct(int fixed, ...)
{
    va_list ap;
    struct Sixteen s;
    va_start(ap, fixed);
    s = va_arg(ap, struct Sixteen);
    va_end(ap);
    return (int)s.a + (int)s.b;
}

int main(void)
{
    signed char sc = -3;
    unsigned char uc = 250;
    struct Six s6 = {{1, 2, 3, 4, 5, 6}};
    struct Twelve s12 = {10, 20, 30};
    struct Sixteen s16 = {100, 200};

    if (no_variadic_reads(7) != 7) return 1;
    if (sum_ten(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11) != 66) return 2;
    if (sum_twelve(1, 2, 3, 4, 5, 6, 7) != 28) return 3;
    if (narrow(0, sc, uc) != 247) return 4;
    if (mixed(0, 40, "abcd") != 44) return 5;
    if (copy_test(0, 1, 2, 3) != 122) return 6;
    if (two_lists(0, 1, 2, 3, 4) != 303) return 7;
    if (repeated(0, 6, 7) != 66) return 8;
    if (six_struct(0, s6) != 7) return 9;
    if (twelve_struct(1, 2, 3, 4, 5, s12) != 75) return 10;
    if (sixteen_struct(0, s16) != 300) return 11;
    return 0;
}
"""


class TestVariadic:
    def test_registers_stack_promotions_copy_and_aggregates(self, tmp_path):
        machine, output = runtime_program(tmp_path, "variadic.c",
                                          VARIADIC_PROGRAM)
        assert machine.regs[1] == 0
        assert output == b""

    def test_va_start_outside_a_variadic_function_is_diagnosed(self, tmp_path, capsys):
        source = write(
            tmp_path, "bad.c",
            """
            #include <stdarg.h>
            int main(void) {
                va_list ap;
                va_start(ap, ap);
                return 0;
            }
            """,
        )
        assert invoke("-S", source, "-o", tmp_path / "bad.s") == 1
        err = capsys.readouterr().err
        assert "not variadic" in err
        assert "Traceback" not in err
        assert not (tmp_path / "bad.s").exists()


# ---------------------------------------------------------------------------
# printf / puts / putchar
# ---------------------------------------------------------------------------

PRINTF_PROGRAM = r"""
#include <stdio.h>

int main(void)
{
    int count = printf("[%c][%s][%d][%i][%u][%x][%X][%p][%%]\n",
                       'A', "text", -42, 42, 4294967295u,
                       0xdeadbeefu, 0xdeadbeefu, (void *)0);
    if (count != 59) return 1;
    if (printf("[%s]", (const char *)0) != 8) return 2;
    if (printf("[%q][%]") != 7) return 3;
    if (printf("") != 0) return 4;
    if (printf("%d %d", -2147483647 - 1, 2147483647) != 22) return 5;
    if (puts("line") != 0) return 6;
    if (putchar('!') != '!') return 7;
    if (putchar('\n') != '\n') return 8;
    return 0;
}
"""

PRINTF_OUTPUT = (
    b"[A][text][-42][42][4294967295][deadbeef][DEADBEEF][0x0][%]\n"
    b"[(null)][%q][%]-2147483648 2147483647line\n!\n"
)


class TestPrintf:
    def test_supported_conversions_and_return_counts(self, tmp_path):
        machine, output = runtime_program(tmp_path, "printf.c", PRINTF_PROGRAM)
        assert machine.regs[1] == 0
        assert output == PRINTF_OUTPUT

    def test_null_format_is_an_error(self, tmp_path):
        machine, _ = runtime_program(
            tmp_path, "null.c",
            """
            #include <stdio.h>
            int main(void) {
                return printf((const char *)0) == -1 ? 0 : 1;
            }
            """,
        )
        assert machine.regs[1] == 0

    def test_puts_of_null_uses_the_documented_null_text(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "putsnull.c",
            """
            #include <stdio.h>
            int main(void) { return puts((const char *)0); }
            """,
        )
        assert machine.regs[1] == 0
        assert output == b"(null)\n"

    def test_unsupported_flags_width_precision_and_lengths_are_literal(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "unsupported.c",
            r"""
            #include <stdio.h>
            int main(void) {
                return printf("[%5d][%-d][%.2f][%ld]") == 21 ? 0 : 1;
            }
            """,
        )
        assert machine.regs[1] == 0
        assert output == b"[%5d][%-d][%.2f][%ld]"

    def test_putchar_can_write_a_nul_byte(self, tmp_path):
        machine, output = runtime_program(
            tmp_path, "nul.c",
            """
            #include <stdio.h>
            int main(void) {
                if (putchar(0) != 0) return 1;
                return putchar('x') == 'x' ? 0 : 2;
            }
            """,
        )
        assert machine.regs[1] == 0
        assert output == b"\x00x"


# ---------------------------------------------------------------------------
# nova-cc integration
# ---------------------------------------------------------------------------

class TestDriverIntegration:
    def test_object_compile_then_runtime_link(self, tmp_path):
        source = write(
            tmp_path, "main.c",
            """
            #include <string.h>
            int main(void) {
                char buffer[8];
                strcpy(buffer, "ok");
                return strcmp(buffer, "ok");
            }
            """,
        )
        obj = tmp_path / "main.nobj"
        # Objects are not rewritten by the linker; a runtime link must use the
        # layout-independent call forms.
        assert invoke("-c", "--long-calls", "--long-branches", source,
                      "-o", obj) == 0
        output = tmp_path / "program.nob"
        assert invoke("--runtime", obj, "-o", output) == 0
        machine = run_image(output)
        assert machine.regs[1] == 0

    def test_runtime_link_accepts_multiple_user_translation_units(self, tmp_path):
        helper = write(
            tmp_path, "helper.c",
            """
            #include <string.h>
            int helper(void) {
                char buffer[8];
                return (int)strlen(strcpy(buffer, "abcd"));
            }
            """,
        )
        entry = write(
            tmp_path, "main.c",
            """
            int helper(void);
            int main(void) { return helper() == 4 ? 0 : 1; }
            """,
        )
        machine, _ = run_program(tmp_path, entry, helper, options=("--runtime",))
        assert machine.regs[1] == 0

    def test_nostdlib_keeps_syscall_wrappers_but_omits_libnova(self, tmp_path):
        source = write(
            tmp_path, "main.c",
            """
            #include <nova.h>
            int main(void) {
                return nova_write(1, "x", 1) == 1 ? 0 : 1;
            }
            """,
        )
        machine, output = run_program(tmp_path, source,
                                      options=("--runtime", "--nostdlib"))
        assert machine.regs[1] == 0
        assert output == b"x"

    def test_nostdlib_reports_missing_libnova_symbol(self, tmp_path, capsys):
        source = write(
            tmp_path, "main.c",
            """
            #include <string.h>
            int main(void) { return (int)strlen("abc"); }
            """,
        )
        assert invoke("--runtime", "--nostdlib", source,
                      "-o", tmp_path / "program.nob") == 1
        err = capsys.readouterr().err
        assert "undefined symbol 'strlen'" in err
        assert not (tmp_path / "program.nob").exists()

    def test_nostartfiles_uses_a_user_supplied_start(self, tmp_path):
        start = write(
            tmp_path, "start.s",
            """
            .global _start
            _start:
                LI sp, 0x80000
                LA ra, done
                BL main
            done:
                HALT
            """,
        )
        start_obj = tmp_path / "start.nobj"
        write_object(start_obj, assemble_object(start.read_text()))
        main_source = write(
            tmp_path, "main.c",
            """
            #include <string.h>
            int main(void) { return (int)strlen("hello") + 37; }
            """,
        )
        output = tmp_path / "program.nob"
        assert invoke("--runtime", "--nostartfiles", start_obj, main_source,
                      "-o", output) == 0
        machine = run_image(output)
        assert machine.regs[1] == 42

    @pytest.mark.parametrize("args,needle", [
        (("-c", "--runtime"), "--runtime is only meaningful when linking"),
        (("--nostartfiles",), "--nostartfiles is only meaningful with --runtime"),
        (("--nostdlib",), "--nostdlib is only meaningful with --runtime"),
    ])
    def test_runtime_options_are_rejected_outside_link_mode(
            self, tmp_path, capsys, args, needle):
        source = write(tmp_path, "main.c", "int main(void) { return 0; }\n")
        assert invoke(*args, source) == 2
        assert needle in capsys.readouterr().err
        assert not (tmp_path / "main.nobj").exists()

    def test_missing_main_is_reported_by_the_linker(self, tmp_path, capsys):
        source = write(
            tmp_path, "helper.c",
            "int helper(void) { return 1; }\n",
        )
        assert invoke("--runtime", source, "-o", tmp_path / "program.nob") == 1
        err = capsys.readouterr().err
        assert "undefined symbol 'main'" in err
        assert not (tmp_path / "program.nob").exists()

    def test_runtime_link_is_deterministic(self, tmp_path):
        source = write(
            tmp_path, "main.c",
            """
            #include <stdio.h>
            int main(void) { return printf("x") == 1 ? 0 : 1; }
            """,
        )
        first = tmp_path / "one.nob"
        second = tmp_path / "two.nob"
        assert invoke("--runtime", source, "-o", first) == 0
        assert invoke("--runtime", source, "-o", second) == 0
        assert first.read_bytes() == second.read_bytes()

    def test_user_include_path_takes_precedence_over_the_builtin_headers(
            self, tmp_path, capsys):
        include = tmp_path / "include"
        write(include, "stdio.h", "#define NOVA_TEST_OVERRIDE 7\n")
        source = write(
            tmp_path, "main.c",
            """
            #include <stdio.h>
            int main(void) { return NOVA_TEST_OVERRIDE; }
            """,
        )
        assert invoke("-c", "-I", include, source, "-o", tmp_path / "main.nobj") == 0
        without = write(tmp_path, "plain.c",
                        "#include <stdio.h>\nint main(void) { return NOVA_TEST_OVERRIDE; }\n")
        assert invoke("-c", without, "-o", tmp_path / "plain.nobj") == 1
        assert "NOVA_TEST_OVERRIDE" in capsys.readouterr().err
