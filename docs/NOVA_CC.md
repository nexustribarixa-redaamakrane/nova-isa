# `nova-cc` — Nova64 C compiler driver

**Status:** implemented (Phase 1 Milestone 6). This document describes only
behaviour that exists in this tree. The ISA remains frozen at `Nova ISA v1.0`;
the driver adds no instruction, register, syscall, ABI convention, relocation
kind, or container-format change.

`nova-cc` is a thin orchestration layer over the existing compiler, assembler,
object format, and linker APIs:

```
C source
  -> nova/cc/preprocessor.py   preprocessing, includes, macros
  -> nova/cc/lexer.py          tokens with source locations
  -> nova/cc/parser.py         syntax-level AST
  -> nova/cc/sema.py           types, scopes, semantic checks
  -> nova/cc/codegen.py        direct AST -> Nova64 assembly
  -> nova.assembler            .nobj relocatable object
  -> nova.link                 .nob executable (link mode only)
```

The driver does not re-implement preprocessing, parsing, semantic analysis,
code generation, assembly, linking, or file-format encoding.

## Invocation

```
nova-cc [options] input...
python -m nova.cc [options] input...
```

The `nova-cc` console script is declared in `pyproject.toml` as
`nova-cc = "nova.cli:compile_main"`; `python -m nova.cc` runs the same
`nova.cc.driver.main` entry point.

Inputs whose suffix is `.nobj` are loaded as relocatable objects. Every other
input is compiled as C. Object inputs are accepted only in link mode.

## Compilation modes

Exactly one of `-E`, `-S`, or `-c` may be given; with none, `nova-cc` compiles
and links.

| Mode | Stops after | Default output |
| --- | --- | --- |
| `-E` | preprocessing | `source.i` |
| `-S` | Nova64 assembly generation | `source.s` |
| `-c` | assembly and object encoding | `source.nobj` |
| *(none)* | linking | `a.nob` |

Default output names replace the input suffix with `.i`, `.s`, or `.nobj` in
the input file's directory. Link mode writes `a.nob` in the current directory
unless `-o` is given. `-o` is required to place the link output anywhere else
and is rejected with multiple inputs in `-E`/`-S`/`-c` mode.

`-E` writes the text produced by the existing preprocessor, preserving
`__FILE__` and `__LINE__` behaviour. `-S` writes the assembly text that the
assembler consumes. `-c` writes a validated `.nobj`. Link mode writes a
validated `.nob` with the default image base `0x1000`.

## Options

| Option | Behaviour |
| --- | --- |
| `-E` | Preprocess only. |
| `-S` | Compile to Nova64 assembly only. |
| `-c` | Compile and assemble to `.nobj`; do not link. |
| `-o FILE` | Write the single output to `FILE`. In link mode, write the `.nob` to `FILE`. |
| `-I DIR` | Append `DIR` to the quoted/angle include search list used by the preprocessor. May be repeated. |
| `-D NAME[=VALUE]` | Define a preprocessor macro. A missing value means `1`. May be repeated. |
| `-U NAME` | Undefine a preprocessor macro after builtin and `-D` definitions. May be repeated. |
| `-std=MODE` | Select the preprocessor language mode. Default `nova-c11`. |
| `-O LEVEL` | Only `-O0` is accepted. No optimizer exists, so `-O0` is the default code-generation behaviour. |
| `--target=TARGET` | Select the code-generation target. Only `nova64` is accepted. |
| `--long-calls` | Emit layout-independent direct calls (see below). |
| `--long-branches` | Remove the signed 14-bit intra-function branch limit (see below). |
| `-e SYMBOL`, `--entry=SYMBOL` | Linker entry symbol. Default `_start`. Valid only in link mode. |
| `--version` | Print `nova-cc (Nova C compiler) <toolchain> [Nova ISA v1.0; Nova Linker v0.1]`. |
| `-h`, `--help` | Print usage, supported modes, and supported language modes/targets. |

`-D` and `-U` are applied in command-line order. A later `-D` re-defines a
macro removed by `-U`, and a later `-U` removes a macro defined by `-D`.
`-U` also removes builtin macros such as `__STDC__`; the preprocessor's normal
redefinition rules still apply.

Supported `-std=` values are `nova-c11` (default), `c11`, `gnu11`, `c99`,
`gnu99`, `c89`, and `c90`. These select the existing preprocessor macro mode;
they do not claim full ISO C conformance. Unsupported language modes, targets,
optimization levels, and unknown options are rejected before any file is read.

## Multi-input builds

`-E`, `-S`, and `-c` accept multiple C inputs and derive one output per input.
All inputs are compiled before any output is written, so a failure cannot leave
a partially completed set of artifacts. Output-path collisions are rejected.

Link mode accepts any mixture of C sources and `.nobj` objects, for example:

```
nova-cc -c main.c
nova-cc -c math.c
nova-cc main.nobj math.nobj -o program.nob
```

or, directly from sources:

```
nova-cc main.c math.c -o program.nob
```

Static symbols have internal linkage; each translation unit's `.Lcgu_*` labels
and `static` objects/functions are object-local, so identically named statics in
different inputs do not collide. An external definition must appear exactly
once; duplicate and undefined symbols are reported by the linker.

The produced `.nobj` and `.nob` formats are unchanged. Each C input is compiled
to its own object, and the existing linker performs section placement,
relocation, duplicate-symbol checks, and entry-symbol resolution.

## Long calls and long branches

Direct `CALL` and direct conditional/unconditional branches encode a signed
14-bit PC-relative displacement (`PCREL_DISP14`). The default output is compact
and therefore limited to roughly ±8 KiB per call or branch target.

`--long-calls` replaces each direct symbol call with:

```asm
LA ra, .Lcgu_longcall_N
BL target
.Lcgu_longcall_N:
```

`--long-branches` replaces each intra-function unconditional jump with `BL
label`, and each conditional branch with an inverted short branch to a nearby
skip label plus `BL label`:

```asm
BNE t0, t1, .Lcgu_branch_skip_N
BL .Lcgu_target
.Lcgu_branch_skip_N:
```

`BL` is the assembler's documented long-jump pseudo: it expands to a fixed
ten-instruction `LI r15, address` + `JMP r15` sequence with a `PCREL_LONG`
relocation. `LA` is the existing fixed `LA64` block. The compiler does not rely
on `r15` elsewhere, and no new instruction or relocation kind is introduced.

Both options are opt-in. Without them, an out-of-range call or branch is an
error from the assembler or linker. `nova-cc` appends a `--long-calls` or
`--long-branches` hint to that diagnostic when the corresponding flag is absent.
There is no automatic relaxation and no linker-inserted stub.

Indirect calls through function pointers already use `LA ra, .Lcgu_funcret_N`
and `JMP t0`; they are unaffected by `--long-calls`.

## Output safety and diagnostics

- Existing output files are replaced only after the complete requested stage
  succeeds. Writes go to a temporary file in the output directory and are moved
  into place with `os.replace`.
- If several outputs are requested, all inputs are processed before the first
  output is written.
- The driver refuses to write an output over one of its input files.
- A missing input, missing output directory, unreadable source or object, or a
  directory used as an output path produces a clear error, not a traceback.
- Diagnostics carry the filename and line/column when the stage provides them,
  plus a stage label: `preprocessing`, `parsing`, `semantic analysis`, `code
  generation`, `assembly`, or `link`.
- Ordinary user errors return exit code `1`; command-line usage errors return
  `2`; internal errors return `70` and print a traceback; `Ctrl-C` returns
  `130`.

Example diagnostics:

```
nova-cc: error: main.c:2:16: error: semantic analysis: undeclared identifier 'x'
nova-cc: error: big.c: error: assembly: line 9: CALL operand 'main' has displacement 64384,
  outside the signed 14-bit range [-8192, 8191]
  note: recompile with --long-calls to remove the direct CALL distance limit
nova-cc: error: link: multiple definition of 'helper'
```

## End-to-end example

```c
/* main.c */
int helper(void);
int run(void) { return helper() + 40; }
```

```c
/* helper.c */
int helper(void) { return 2; }
```

```
nova-cc -c main.c
nova-cc -c helper.c
nova-cc main.nobj helper.nobj -o program.nob
```

A `.nob` still needs an entry point. A minimal `crt0` object can be produced
with the existing assembler:

```asm
.global _start
_start:
    LI sp, 0x80000
    CALL run
    HALT
```

```
nova-as crt0.s -o crt0.nobj
nova-cc crt0.nobj main.nobj helper.nobj -o program.nob
```

The linker's default entry symbol is `_start`; `-e`/`--entry` selects another.

## Supported and unsupported C

The driver exposes exactly the frontend and backend subset documented in
`docs/PHASE1_PLAN.md`. In particular:

- Supported: the freestanding integer/character/pointer/array/struct/union/
  enum subset, control flow, functions, recursion, direct and indirect calls,
  static/extern globals, block-scope statics, designated initialisers, and
  multiple translation units.
- Rejected with source-located diagnostics: floating-point lowering, bit-field
  access, variable-length arrays, K&R definitions, non-constant global
  initialisers, and full variadic argument access (`va_start`/`va_arg`).
- Not present: PIC/GOT, optimiser, scheduler, register allocator, hosted C
  library, runtime helpers, and `libnova`. Those belong to Phase 1 Milestone 7
  or later.
