"""End-to-end tests for Nova64 C lowering (Phase 1 Milestone 5).

Every program in this module goes through the complete pipeline:

    C source -> preprocessing -> tokens -> AST -> semantic analysis
             -> Nova64 code generation -> .nobj -> link -> .nob -> execution

The tests also validate the generated assembly structurally: only documented
directives, real ISA instructions plus the ``LI``/``LA`` assembler pseudos, and
only the register set reserved for the compiler are allowed.
"""

import re
import textwrap

import pytest

from nova import (
    INSTRUCTION_TABLE,
    PRIVILEGE_LEVELS,
    assemble_object,
    decode_nob,
    encode_nob,
    encode_object,
    decode_object,
    link_objects,
)
from nova.cc import analyze, generate, parse_source
from nova.cc.diagnostics import CompileError

#: Minimal freestanding startup code. ``main`` returns its value in ``a0``.
CRT0 = """
.global _start
_start:
    LI sp, 0x80000
    CALL main
    HALT
"""

#: Assembler pseudos the lowerer is allowed to emit.
ALLOWED_PSEUDOS = {"LI", "LA", "BL"}

#: Object-frontend directives the lowerer is allowed to emit.
ALLOWED_DIRECTIVES = {
    ".section",
    ".global",
    ".type",
    ".size",
    ".byte",
    ".hword",
    ".word",
    ".quad",
    ".asciz",
    ".space",
    ".align",
}

#: Registers reserved by ``docs/NOVA_C_ABI.md``; codegen may never touch them.
FORBIDDEN_REGISTERS = {
    "s0", "s1", "s2", "s3", "gp", "fp",
    "r11", "r12", "r13", "r14", "r28", "r30",
}

REGISTER_RE = re.compile(
    r"\b(r(?:[0-9]|[12][0-9]|3[01])|zero|ra|sp|fp|gp|a[0-5]|t[0-3]|s[0-3])\b"
)

#: (name, C source, expected ``main`` return value). Expected values were
#: computed by hand from the C semantics, not from generated assembly.
PROGRAMS = [
    (
        "arithmetic_and_logic",
        """
        int main(void) {
            int a = 7, b = 3;
            int c = a * b + (a << 2) - (b / 2) + (a % b);
            if (c != 49) return 1;
            if ((a > b) != 1) return 2;
            if ((!a || b) != 1) return 3;
            if ((a & b) != 3 || (a | b) != 7 || (a ^ b) != 4) return 4;
            if ((unsigned int)-1 == 0) return 5;
            return c;
        }
        """,
        49,
    ),
    (
        "control_flow",
        """
        int main(void) {
            int i, s = 0;
            for (i = 0; i < 10; i++) {
                if (i == 3) continue;
                if (i == 8) break;
                s += i;
            }
            while (i > 0) { i--; s++; }
            do { s++; } while (s < 26);
            switch (s) {
            case 25: s = 9; break;
            case 26: s = 8; break;
            default: s = 7;
            }
            return s;
        }
        """,
        7,
    ),
    (
        "recursion",
        """
        int fact(int n) { if (n < 2) return 1; return n * fact(n - 1); }
        int main(void) { return fact(5); }
        """,
        120,
    ),
    (
        "stack_arguments",
        """
        int sum8(int a, int b, int c, int d, int e, int f, int g, int h) {
            return a + b + c + d + e + f + g + h;
        }
        int main(void) { return sum8(1, 2, 3, 4, 5, 6, 7, 8); }
        """,
        36,
    ),
    (
        "function_pointers",
        """
        int add(int a, int b) { return a + b; }
        int sub(int a, int b) { return a - b; }
        int main(void) {
            int (*p)(int, int) = add;
            int r = p(10, 3);
            p = sub;
            return r + p(10, 3);
        }
        """,
        20,
    ),
    (
        "arrays_and_pointers",
        """
        int main(void) {
            int a[4] = {10, 20, 30, 40};
            int *p = a;
            int s = 0;
            for (int i = 0; i < 4; i++) s += *(p + i);
            p += 2;
            s += *p;
            s += (int)(p - a);
            return s;
        }
        """,
        132,
    ),
    (
        "strings",
        """
        int len(char *s) { int n = 0; while (*s) { n++; s++; } return n; }
        int main(void) { char *p = "hello"; return len(p) + p[1]; }
        """,
        106,
    ),
    (
        "struct_pass_and_return",
        """
        struct P { int x; int y; };
        struct P make(int x, int y) { struct P p; p.x = x; p.y = y; return p; }
        int sum(struct P p) { return p.x + p.y; }
        int main(void) {
            struct P p = make(3, 4);
            struct P q;
            q = p;
            q.x += 2;
            return sum(q);
        }
        """,
        9,
    ),
    (
        "large_struct_hidden_return",
        """
        struct B { int v[8]; };
        struct B make(int n) {
            struct B b;
            for (int i = 0; i < 8; i++) b.v[i] = n + i;
            return b;
        }
        int sum(struct B b) {
            int s = 0;
            for (int i = 0; i < 8; i++) s += b.v[i];
            return s;
        }
        int main(void) { struct B b = make(1); return sum(b); }
        """,
        36,
    ),
    (
        "unions",
        """
        union U { int i; char c[4]; };
        int main(void) { union U u; u.i = 0x01020304; return u.c[0]; }
        """,
        4,
    ),
    (
        "goto_labels",
        """
        int main(void) {
            int x = 0;
            goto skip;
            x = 99;
        skip:
            x += 5;
            return x;
        }
        """,
        5,
    ),
    (
        "global_data",
        """
        int g = 5;
        static int sg = 7;
        int arr[3] = {1, 2, 3};
        char msg[] = "hi";
        int main(void) {
            g = g + sg + arr[1];
            return g + msg[1];
        }
        """,
        119,
    ),
    (
        "global_pointer_initializers",
        """
        int x = 11;
        int *p = &x;
        char *s = "abc";
        int main(void) { return *p + s[1]; }
        """,
        109,
    ),
    (
        "designated_initializers",
        """
        struct S { int a; int b; };
        int main(void) {
            int a[5] = {[2] = 7, [4] = 9};
            struct S s = {.b = 3, .a = 4};
            return a[2] + a[4] + s.a + s.b;
        }
        """,
        23,
    ),
    (
        "sizeof_conditional_comma",
        """
        int main(void) {
            int a = 3;
            int b = sizeof(a) + sizeof(int) * 2;
            b += (a > 2) ? 5 : 7;
            b += (a, 4);
            return b;
        }
        """,
        21,
    ),
    (
        "arrow_enum_typedef",
        """
        typedef unsigned short u16;
        enum Color { RED = 1, GREEN = 3, BLUE = 7 };
        struct Node { int value; struct Node *next; };
        int main(void) {
            enum Color c = GREEN;
            u16 u = 65535;
            struct Node n2 = {9, 0};
            struct Node n1 = {4, &n2};
            return c + (u & 1) + n1.value + n1.next->value + (BLUE - RED);
        }
        """,
        23,
    ),
    (
        "increment_and_short_circuit",
        """
        int side = 0;
        int bump(void) { side++; return side; }
        int main(void) {
            int x = 1;
            int y = x++ + ++x;
            int z = (x > 0) && bump();
            int w = (x < 0) || bump();
            int q = !x;
            return y + z + w + q + side * 10;
        }
        """,
        26,
    ),
    (
        "void_function_nested_loops",
        """
        void addto(int *p, int v) { *p += v; }
        int main(void) {
            int s = 0;
            for (int i = 0; i < 3; i++) {
                int j = 0;
                while (j < 2) { addto(&s, i + j); j++; }
            }
            return s;
        }
        """,
        9,
    ),
    (
        "unsigned_and_shifts",
        """
        int main(void) {
            unsigned int u = 0x80000000u;
            int s = (int)(u >> 31);
            int t = -8 >> 2;
            int r = 1 << 5;
            unsigned char c = 200;
            signed char d = -3;
            return s + (-t) + r + c / 8 + (-d);
        }
        """,
        63,
    ),
    (
        "struct_arrays_and_arrow",
        """
        struct P { int x; int y; };
        int main(void) {
            struct P a[3] = {{1, 2}, {3, 4}, {5, 6}};
            int s = 0;
            for (int i = 0; i < 3; i++) s += a[i].x * a[i].y;
            struct P *p = &a[2];
            p->y = 7;
            return s + p->y;
        }
        """,
        51,
    ),
    (
        "switch_fallthrough",
        """
        int main(void) {
            int x = 2, s = 0;
            switch (x) {
            case 1: s += 1;
            case 2: s += 10;
            case 3: s += 100; break;
            case 4: s += 1000;
            }
            return s;
        }
        """,
        110,
    ),
]


def compile_source(src: str, filename: str = "test.c") -> str:
    """Run preprocessing, parsing, semantic analysis, and code generation."""
    tree = analyze(parse_source(textwrap.dedent(src), filename=filename))
    return generate(tree)


def instruction_lines(asm: str):
    """Yield ``(mnemonic, operand_text)`` pairs for real/pseudo instructions."""
    for raw in asm.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.endswith(":") or line.startswith("."):
            continue
        parts = line.split(None, 1)
        yield parts[0], parts[1] if len(parts) > 1 else ""


def validate_assembly(asm: str) -> None:
    """Check the structural contract shared by every generated program."""
    assert asm.endswith("\n")
    assert asm.count(".section .text") == 1

    for raw in asm.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            # Local labels (for example ``.Lcgu_for_top_1:``) are scoped to
            # the object and are not assembler directives.
            continue
        if line.startswith("."):
            directive = line.split(None, 1)[0]
            assert directive in ALLOWED_DIRECTIVES, f"undocumented {directive!r}"
            if directive == ".section":
                assert line in {
                    ".section .text",
                    ".section .rodata",
                    ".section .data",
                    ".section .bss",
                }
            if directive == ".type":
                assert re.fullmatch(r"\.type \S+, (function|object)", line)
            if directive == ".size":
                assert re.fullmatch(r"\.size \S+, \d+", line)

    for mnemonic, operands in instruction_lines(asm):
        assert mnemonic in INSTRUCTION_TABLE or mnemonic in ALLOWED_PSEUDOS, (
            f"unknown mnemonic {mnemonic!r}"
        )
        for register in REGISTER_RE.findall(operands):
            assert register not in FORBIDDEN_REGISTERS, (
                f"reserved register {register!r} used in {mnemonic} {operands}"
            )

    # ``JMP`` is register-only; a directly labelled intra-function branch must
    # therefore be a guaranteed-true BEQ against r0.
    for mnemonic, operands in instruction_lines(asm):
        if mnemonic == "JMP":
            assert REGISTER_RE.fullmatch(operands.strip()), (
                f"JMP must take a register, got {operands!r}"
            )
    assert not re.search(r"^JMP \.", asm, re.MULTILINE)

    # Every function saves and restores ra inside a 16-byte-aligned frame that
    # also reserves the 128-byte outgoing argument area.
    assert "STD ra, [sp + 128]" in asm
    assert "LDD ra, [sp + 128]" in asm
    frames = [int(m.group(1)) for m in
              re.finditer(r"^ADDI sp, sp, -(\d+)$", asm, re.MULTILINE)]
    frames += [int(m.group(1)) for m in
               re.finditer(r"^LI t3, (\d+)\nSUB sp, sp, t3$", asm,
                           re.MULTILINE)]
    assert frames
    for frame in frames:
        assert frame % 16 == 0
        assert frame >= 144


def assemble_and_link(asm: str):
    """Assemble the lowered program, link it with CRT0, and return artifacts."""
    validate_assembly(asm)
    program = assemble_object(asm)
    # The object must serialise and decode back to the same structure.
    assert encode_object(decode_object(encode_object(program))) == \
        encode_object(program)
    link = link_objects([assemble_object(CRT0), program])
    return program, link


def run_program(asm: str, steps: int = 200_000):
    """Link and execute a lowered program, returning the final ``a0`` value."""
    from nova.emulator import NovaMachine

    _, link = assemble_and_link(asm)
    encoded = encode_nob(image_base=link.image_base, entry=link.entry,
                         image=link.image)
    loaded = decode_nob(encoded)
    machine = NovaMachine(code=loaded.image, entry=loaded.entry)
    machine.priv = PRIVILEGE_LEVELS["machine"]
    for _ in range(steps):
        if machine.halted:
            break
        machine.step()
    assert machine.halted, "program did not halt"
    return machine.regs[1]


# ---------------------------------------------------------------------------
# Semantic-analysis gate and target validation
# ---------------------------------------------------------------------------

def test_generate_requires_semantically_analyzed_tree():
    tree = parse_source("int main(void) { return 0; }", filename="gate.c")
    with pytest.raises(CompileError) as exc:
        generate(tree)
    assert "requires a translation unit that passed semantic analysis" in \
        exc.value.message
    assert exc.value.location.file == "gate.c"


def test_generate_rejects_unknown_target():
    tree = analyze(parse_source("int main(void) { return 0; }"))
    with pytest.raises(CompileError) as exc:
        generate(tree, target="nova32")
    assert "unsupported code generation target 'nova32'" in exc.value.message


def test_generate_is_deterministic():
    src = PROGRAMS[0][1]
    assert compile_source(src) == compile_source(src)


# ---------------------------------------------------------------------------
# End-to-end programs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,src,expected", PROGRAMS,
                         ids=[name for name, _, _ in PROGRAMS])
def test_program_returns_expected_value(name, src, expected):
    asm = compile_source(src, filename=f"{name}.c")
    assert run_program(asm) == expected


@pytest.mark.parametrize("name,src,expected", PROGRAMS,
                         ids=[name for name, _, _ in PROGRAMS])
def test_program_assembly_is_structural(name, src, expected):
    validate_assembly(compile_source(src, filename=f"{name}.c"))


# ---------------------------------------------------------------------------
# Object, relocation, and frame contract
# ---------------------------------------------------------------------------

def test_object_uses_only_documented_relocations():
    obj, _ = assemble_and_link(compile_source(PROGRAMS[0][1]))
    kinds = {relocation.kind for relocation in obj.relocations}
    assert kinds <= {"PCREL_DISP14", "LA64", "ABS64"}


def test_function_symbol_sizes_cover_text_section():
    asm = compile_source(PROGRAMS[7][1])
    obj = assemble_object(asm)
    text = obj.sections[0]
    functions = [s for s in obj.symbols if s.type == "function"]
    assert len(functions) >= 2
    for symbol in functions:
        assert symbol.section == 0
        assert symbol.size > 0
        assert symbol.size % 4 == 0
        assert symbol.value + symbol.size <= text.size
    ordered = sorted(functions, key=lambda s: s.value)
    for previous, current in zip(ordered, ordered[1:]):
        assert previous.value + previous.size == current.value
    assert sum(s.size for s in ordered) == text.size


def test_large_frame_uses_li_and_sub_when_needed():
    # 300 int locals force a frame larger than ADDI's signed immediate range;
    # the prologue must spill the frame size through LI/SUB instead.
    body = " ".join(f"a[{i}] = {i};" for i in range(300))
    src = f"int main(void) {{ int a[300]; {body} return a[299]; }}"
    asm = compile_source(src)
    assert "LI t3, " in asm
    assert "SUB sp, sp, t3" in asm
    assert run_program(asm) == 299


def test_global_data_sections_and_string_pool():
    data_asm = compile_source(PROGRAMS[11][1])
    assert ".section .data" in data_asm
    assert ".byte 0x68, 0x69, 0x00" in data_asm

    string_asm = compile_source(PROGRAMS[6][1])
    assert ".section .rodata" in string_asm
    assert ".byte 0x68, 0x65, 0x6c, 0x6c, 0x6f, 0x00" in string_asm

    pointer_asm = compile_source(PROGRAMS[12][1])
    assert re.search(r"^\.quad \.Lcgu_str_", pointer_asm, re.MULTILINE)
    assert ".section .rodata" in pointer_asm


def test_block_scope_static_keeps_state_between_calls():
    asm = compile_source(
        """
        int next(void) { static int value = 2; value++; return value; }
        int main(void) { next(); next(); return next(); }
        """
    )
    assert ".section .data" in asm
    assert run_program(asm) == 5


def test_function_pointer_call_uses_register_jump():
    asm = compile_source(PROGRAMS[4][1])
    assert re.search(r"^LA ra, \.Lcgu_funcret_", asm, re.MULTILINE)
    assert "JMP t0" in asm
    assert "CALL " not in asm  # every call in this program is indirect


def test_frame_reserves_outgoing_argument_area():
    asm = compile_source(PROGRAMS[3][1])
    # Eight argument chunks: six in a0-a5, two in the outgoing stack area.
    assert "STD t0, [sp + 0]" in asm
    assert "STD t0, [sp + 8]" in asm
    assert run_program(asm) == 36


# ---------------------------------------------------------------------------
# Unsupported constructs are diagnosed with source locations
# ---------------------------------------------------------------------------

def test_floating_point_constructs_are_rejected_by_sema():
    with pytest.raises(CompileError) as exc:
        compile_source("double x; int main(void) { return x; }")
    assert "floating-point" in exc.value.message
    assert exc.value.location.line == 1


def test_bit_field_access_is_rejected_by_codegen():
    with pytest.raises(CompileError) as exc:
        compile_source(
            "struct S { int x : 3; };"
            "int main(void) { struct S s; s.x = 1; return 0; }"
        )
    assert "bit-field access" in exc.value.message
    assert exc.value.location.line == 1
    assert exc.value.location.column >= 1


def test_non_constant_global_initializer_is_rejected():
    with pytest.raises(CompileError) as exc:
        compile_source("int f(void) { return 1; } int x = f();")
    assert "non-constant data initializer" in exc.value.message


def test_long_calls_use_documented_la_and_bl_pseudos():
    tree = analyze(parse_source(
        "int g(void) { return 7; } int f(void) { return g(); }",
        filename="long.c",
    ))
    asm = generate(tree, long_calls=True)
    assert "CALL g" not in asm
    assert "BL g" in asm
    assert re.search(r"^LA ra, \.Lcgu_longcall_\d+$", asm, re.MULTILINE)
    obj = assemble_object(asm)
    kinds = {relocation.kind for relocation in obj.relocations}
    assert "PCREL_LONG" in kinds
    assert kinds <= {"PCREL_DISP14", "PCREL_LONG", "LA64", "ABS64"}


def test_long_branches_use_documented_bl_pseudo_and_still_execute():
    asm = generate(
        analyze(parse_source(
            """
            int main(void) {
                int total = 0;
                for (int i = 0; i < 5; i++) {
                    if (i == 2) continue;
                    total += i;
                }
                return total;
            }
            """,
            filename="longbranch.c",
        )),
        long_branches=True,
    )
    # Unconditional jumps and conditionals use the BL pseudo; the short-form
    # guaranteed-true branch is gone.
    assert "BEQ r0, r0, .Lcgu_" not in asm
    assert "BL .Lcgu_" in asm
    obj = assemble_object(asm)
    assert {relocation.kind for relocation in obj.relocations} <= \
        {"PCREL_DISP14", "PCREL_LONG", "LA64", "ABS64"}
    assert run_program(asm) == 8


def test_switch_inside_if_collects_labels_and_executes():
    asm = generate(
        analyze(parse_source(
            """
            int main(void) {
                int value = 2;
                if (value != 0) {
                    switch (value) {
                    case 1: return 1;
                    case 2: return 42;
                    default: return 3;
                    }
                }
                return 4;
            }
            """,
            filename="switchif.c",
        ))
    )
    assert run_program(asm) == 42


def test_odd_sized_aggregate_uses_partial_argument_and_return_chunks():
    asm = generate(
        analyze(parse_source(
            """
            struct Six { char a[6]; };
            struct Six make(int base) {
                struct Six s = {{1, 2, 3, 4, 5, 6}};
                if (base) {
                    s.a[0] = (char)(base + 0);
                }
                return s;
            }
            int total(struct Six s) {
                int i, sum = 0;
                for (i = 0; i < 6; i++) {
                    sum += s.a[i];
                }
                return sum;
            }
            int main(void) {
                struct Six s = make(10);
                if (total(s) != 30) return 1;
                if (total(make(0)) != 21) return 2;
                return 0;
            }
            """,
            filename="oddaggregate.c",
        ))
    )
    assert run_program(asm) == 0
