# Phase 1 plan — Nova64 freestanding C compiler and libnova

**Status:** completed Phase 1 plan. This document records the compiler
architecture, the supported C subset, the code-generation contract, and the
order in which the compiler was built. It is kept in sync with
`docs/PORTING_READINESS.md` and `docs/PHASE1_COMPLETION.md`.

The ISA remains frozen at `Nova ISA v1.0`. Phase 1 adds software only.

**Current progress (7 of 7 milestones complete):** Milestones 1–7 are complete
(foundation, preprocessor, parser/AST, type system + semantic analysis, Nova64
code generation, compiler driver/toolchain integration, and runtime/libnova
with end-to-end validation). **Phase 1: 7/7 (100%). Phase 2 is not authorized.**
Subtasks are internal stages and must not become additional major milestones.

## 1. Pipeline

```
C source
  -> preprocessor          nova/cc/preprocessor.py   (#include, #define, #if...)
  -> tokens                nova/cc/lexer.py          (tokens + source locations)
  -> parser                nova/cc/parser.py         (syntax-level AST)
  -> semantic analysis     nova/cc/sema.py           (types, scopes, checks)
  -> annotated AST         nova/cc/types.py          (LP64 Nova64 type model)
  -> code generation       nova/cc/codegen.py        (direct AST -> Nova64 assembly)
  -> assembler             nova.assembler            (.nobj, .nox metadata)
  -> linker                nova.link                 (.nob executable)
  -> emulator              nova-run                  (execution)
```

The compiler is written in Python for the same reason the assembler and linker
are: it must be inspectable, testable, and changeable while the architecture is
young. The current product emits **real Nova assembly** and goes through the
existing tools; it does not contain a second binary encoder. Milestone 5 adds a
complete lowering pass from the semantically analysed AST to assembly, exercised
end-to-end through `assemble_object`, `link_objects`, and the reference
emulator. Milestone 6 adds the `nova-cc` driver, which orchestrates those
existing stages and writes `.i`, `.s`, `.nobj`, and `.nob` artifacts. Milestone 7
adds the freestanding runtime, `libnova`, variadic argument access, scoped
formatted output, and repeatable validation without duplicating any existing
toolchain stage.

### Why direct lowering instead of a separate SSA IR

Phase 1 uses a typed AST and a recursive lowering pass with an explicit frame
and temporary model. Every temporary is a stack slot, so the generated code is
correct by construction and easy to inspect. This is deliberately conservative:
an SSA IR, register allocation, and instruction scheduling can be added later
without changing the front end or the ABI. The lowering pass is split into
`gen_expr`, `gen_addr`, and `gen_cond`, which are the same interfaces an IR
builder would use.

## 2. Module layout

| Module | Responsibility | Status |
| --- | --- | --- |
| `nova/cc/diagnostics.py` | Source locations, diagnostics, `CompileError` | Implemented |
| `nova/cc/preprocessor.py` | Line splicing, includes, macros, conditionals, `_Pragma` | Implemented/hardened |
| `nova/cc/lexer.py` | C tokens, literals, escapes, keyword classification | Implemented |
| `nova/cc/ast.py` | Syntax-level AST nodes | Implemented |
| `nova/cc/types.py` | LP64 C types, layout, equality, conversions | Implemented (Milestone 4) |
| `nova/cc/parser.py` | Recursive-descent parser with C declarator precedence | Implemented/hardened |
| `nova/cc/sema.py` | Scopes, symbols, type checking, AST annotations | Implemented (Milestone 4) |
| `nova/cc/codegen.py` | Nova64 assembly emission, frames, calls, ABI | Implemented (Milestone 5) |
| `nova/cc/driver.py` | End-to-end compile pipeline, file handling, runtime links | Implemented (Milestones 6–7) |
| `nova/cc/__main__.py` | `python -m nova.cc` | Implemented (Milestone 6) |
| `nova/runtime/` | Startup, syscall wrappers, headers, freestanding `libnova` | Implemented (Milestone 7) |
| `scripts/phase1_validate.py` | Ordered Phase 1 regression validation | Implemented (Milestone 7) |

`nova/cli.py` exposes `compile_main` as the `nova-cc` console script.

## 3. Supported C subset (frontend status)

This is the subset the frontend accepts today. Anything not listed must produce
a diagnostic, not be silently miscompiled. Nova64 lowering support for this
subset is described in section 4; constructs accepted by the frontend but not
lowerable (floating point, bit-fields, VLAs) are rejected with a source-located
diagnostic before or during code generation.

### Types

- `void`, `_Bool`.
- `char` (unsigned by ABI), `signed char`, `unsigned char`.
- `short`/`unsigned short`, `int`/`unsigned int`, `long`/`unsigned long`,
  `long long`/`unsigned long long` with target-specific ranks and alignment.
- `float`, `double`, `long double` have a **semantic representation only**
  (`lowering_supported = False`). Floating constants are rejected by the lexer
  and no FP ABI lowering is claimed.
- Pointers, arrays (including multidimensional, VLA syntax and flexible array
  members), functions, and nested derived types. Pointer-to-array,
  array-of-pointer, pointer-to-function and function-returning-pointer remain
  distinct in the type model.
- Structures and unions with scalar, pointer, array, struct and union members;
  bit-field syntax is accepted with a deliberate simplified provisional layout.
- Enumerations with integer constant values and overflow checks against the
  Nova64 `int` model.
- `typedef` names are true aliases, not new independent types.
- `const`, `volatile`, `restrict` are represented as qualifiers. `_Atomic` is
  represented but has no lowering.

### Expressions

Integer and character constants; string literals; identifiers; `sizeof`
(expression and type-name forms); unary `+ - ! ~ * & ++ --`; casts and compound
literals; `_Generic` selections; `* / %`; `+ -`; `<< >>`; `< <= > >=`; `== !=`;
`& ^ |`; `&& ||`; `?:`; all assignment operators; comma; function calls; array
subscript; `.`/`->` member access; pointer arithmetic and comparisons.

Array-to-pointer and function-to-pointer decay, lvalue/modifiable-lvalue checks,
integer promotions, usual arithmetic conversions, and function-call argument
checks are applied during semantic analysis.

### Statements

Compound statements; declarations in blocks; expression and null statements;
`if`/`else`; `while`; `do`/`while`; `for`; `break`; `continue`; `return`;
`switch`/`case`/`default`; `goto` and labels.

### Functions and program structure

Function declarations/definitions, prototypes, `void` parameter lists, variadic
declarations and `stdarg.h` argument access, recursion, file/function/block/
prototype scopes, tag namespaces, and `extern`/`static` storage classes are
handled. K&R old-style definitions are intentionally not a target.

## 4. Nova64 code-generation contract (Milestone 5)

`nova.cc.generate(tree, target="nova64", long_calls=False,
long_branches=False)` lowers a semantically analysed `TranslationUnit` directly
to Nova64 assembly. It refuses a tree without `semantic_ok`/`type_context` and
any target other than `nova64`. There is no separate typed IR, no optimizer, and
no register allocator. The `nova-cc` driver calls this function; the two long
options are described below and in `docs/NOVA_CC.md`.

### Lowering model

- Every local object and compiler temporary receives a fixed slot in the current
  frame. Values are loaded into scratch registers only for the instruction that
  needs them, so no value is live in a register across a call.
- A scalar lvalue's slot holds the object's **address**; a scalar rvalue's slot
  holds its **value**. Aggregate values (structs, unions, arrays) are always
  represented by an address and are copied explicitly on assignment, argument
  passing, initialisation, and return.
- The lowerer implements `gen_expr`, `gen_addr`, and `gen_cond`; the same
  interfaces a future IR builder would use. All labels are local `.Lcgu_*`
  labels, so generated objects are deterministic and do not leak names.

### Frame, stack, and register policy

- Frame layout, from `sp` upward: `[sp, sp + 128)` outgoing stack-argument area
  (16 chunks), `sp + 128` saved `ra`, and `sp + 136` onward parameters, locals,
  and temporaries. Incoming stack arguments are read at
  `sp + frame_size + 8 * (chunk - 6)`.
- Every frame is 16-byte aligned, saves and restores `ra`, and is at least 144
  bytes. Frames too large for `ADDI`'s signed 13-bit immediate are subtracted
  with `LI t3, size` + `SUB sp, sp, t3`.
- Generated code uses `r0` (zero), `a0`-`a5` (arguments/return), `t0`-`t3`
  (scratch), `sp`, and `ra` only. It never uses `s0`-`s3`, `gp`, or `fp`.
- Integer arguments 0–5 use `a0`-`a5`; further arguments and aggregate chunks
  use the outgoing area. A hidden pointer is passed in `a0` for aggregate
  returns larger than 16 bytes; smaller record returns use `a0`/`a1` chunks.
- Direct calls emit `CALL symbol`. Function-pointer calls emit
  `LA ra, .Lcgu_funcret_N`, load the pointer into `t0`, and `JMP t0`. Local
  unconditional jumps are real `BEQ r0, r0, label` (the ISA `JMP` takes a
  register, never a label).
- Memory operands whose displacement does not fit the immediate field are
  materialised with `LI` + `ADD` into a scratch register.
- `--long-calls` replaces each direct call with `LA ra, .Lcgu_longcall_N`,
  `BL target`, and the return label. `--long-branches` replaces unconditional
  jumps with `BL label` and conditional branches with an inverted short branch
  around `BL label`. `BL` is the assembler's documented ten-instruction
  `LI r15`/`JMP r15` pseudo with a `PCREL_LONG` relocation; `r15` is not used
  by the compiler elsewhere. Both options are opt-in and trade code size for
  range independence. There is no automatic relaxation.

### Data, sections, and relocations

- `.text` is emitted with `.section .text`; the bare `.text` spelling is not
  accepted by `assemble_object`.
- String literals go to `.rodata`; initialised global objects go to `.data`;
  zero-initialised objects go to `.bss` using `.space`.
- Pointer-valued initialisers use `.quad symbol`/`.quad .Lcgu_str_N` and
  therefore `ABS64` relocations. Symbolic `LA` blocks use `LA64`; branches and
  calls use `PCREL_DISP14`. No other relocation kind is produced.
- The lowerer emits only documented directives: `.section`, `.global`, `.type`,
  `.size`, `.byte`, `.quad`, `.space`, `.align`. (`.hword`, `.word`, and
  `.asciz` are accepted by the assembler but are not needed by the current
  lowering.)
- Function symbols carry `.type name, function` and a literal `.size`; global
  function/object symbols are emitted with `.global`.

### Instruction selection

Only frozen ISA v1.0 instructions are emitted. The set currently used is:
`ADD ADDI AND BEQ BGE BGEU BGT BGTU BLE BLEU BLT BLTU BNE CALL DIV JMP LA LDB
LDBU LDD LDH LDHU LDW LDWU LI MOV MOVI MUL NEG NOT OR REM RET SAR SARI SHL SHLI
SHR SHRI STB STD STH STW SUB SUBI UDIV UREM XOR`.

The only assembler pseudos used are `LI`, `LA`, and (only under
`--long-calls`/`--long-branches`) `BL`. In particular there is no new
instruction, register, CSR, relocation, ABI convention, or undocumented
assembler syntax. `BL` expands to existing ISA instructions and a `PCREL_LONG`
relocation that the linker already supports. Branch relaxation is not
automatic; a program that needs it must be compiled with the matching flag.

### Long calls and long branches

`PCREL_DISP14` encodes a signed 14-bit displacement, so an unmodified direct
call or branch is limited to roughly ±8 KiB. The default lowering keeps that
compact form. When `--long-calls` is selected, direct symbol calls become
`LA ra, return_label` + `BL target` + `return_label:`. When `--long-branches`
is selected, `_emit_jump` emits `BL label`, and `_emit_branch` emits an
inverted short branch to a nearby skip label followed by `BL label`:

```
BNE t0, t1, .Lcgu_branch_skip_N
BL .Lcgu_target
.Lcgu_branch_skip_N:
```

The inverted branch is itself nearby and cannot overflow. Function `.size`
accounting includes the ten words expanded from each `BL`. Indirect calls and
the existing `LA ra` call sequence are unaffected. `nova-cc` reports the
assembler/linker overflow and suggests the missing flag instead of relaxing
silently.

### Supported lowering

- Integer, character, string, enum, pointer, array, struct, union, `_Bool`, and
  function-pointer types; all integer widths and signed/unsigned divisions,
  comparisons, shifts, and conversions implemented by the ABI.
- Global and local objects, `static` globals and block-scope statics,
  `extern` declarations, constant data initialisers (including designated
  initialisers, nested aggregates, and string/pointer initialisers), and local
  runtime initialisers.
- Function definitions, prototypes, recursion, direct and indirect calls, up to
  six register arguments plus stack arguments, and both small and hidden-pointer
  aggregate returns.
- `if`, `while`, `do`/`while`, `for`, `break`, `continue`, `switch`/`case`/
  `default` (including fall-through), `goto`/labels, compound statements, and
  block-scope declarations.
- Unary `+ - ! ~ * & ++ --` (pre and post), casts, `sizeof`, compound literals,
  `_Generic` selections, comma, arithmetic/bitwise/shift/comparison/logical
  operators, `?:`, and all assignment operators.

### Explicitly unsupported lowering (source-located diagnostics)

- Floating-point objects, expressions, parameters, returns, and initialisers:
  the semantic model represents FP types, but lowering rejects them.
- Bit-field access and bit-field initialisers: semantic analysis accepts the
  provisional layout, but the backend rejects member access and initialisation.
- Variable-length arrays: the semantic pass rejects a non-constant array bound
  with an explicit diagnostic. Objects without a static size (`void` objects,
  incomplete types, flexible array members used as objects) are likewise
  rejected.
- K&R old-style function definitions.
- Non-constant global initialisers and pointer initialisers that are not a
  symbol address or string literal.
- Unsupported variadic forms: `va_arg` of `void`, function, array, incomplete,
  or lowering-unsupported types; `va_start` in a non-variadic function.
- PIC/GOT addressing, automatic branch relaxation, and any
  optimizer/scheduler/register allocator. Long calls and long branches are
  explicit options; `--runtime` implies them for user sources.

## 5. Runtime and library boundary

| Layer | Contents |
| --- | --- |
| compiler runtime | No helper bodies are required by this compiler: division/remainder use native ISA instructions and no `__nova_*` symbol is emitted. The contracts in `docs/COMPILER_RUNTIME.md` remain future specifications. |
| libnova | Freestanding headers and implementations: `mem*`, `str*`, `atoi`/`strtol`/`strtoul`, `exit`/`abort`, `printf`, `putchar`, `puts`, and syscall wrappers |
| crt0 | `_start` supplies `sp = 0x80000`, calls `main` with `LA ra`/`BL`, then `HALT` with the return value in `a0` |

Compiler-generated code never assumes a hosted operating system. The only
operating-system interface is the existing standard syscall table. Runtime
objects are built through the existing pipeline and linked by `nova-cc
--runtime`; `--nostartfiles` and `--nostdlib` allow targeted replacements.
Startup, dispatch, supported library functions, and formatter limits are
documented in `docs/RUNTIME.md`.

## 6. Seven-milestone Phase 1 roadmap and gates

There are exactly **seven** major Phase 1 milestones. Internal implementation
stages and subtasks do not count as additional milestones.

| # | Milestone | Gate | Status |
| --- | --- | --- | --- |
| 1 | **Compiler foundation** — architecture plan, package skeleton, diagnostics, lexer | Lexer tests pass | **Complete** (`d559f99`, `e439de2`) |
| 2 | **Preprocessor** — directives, macros, conditionals, includes, language modes, regression hardening | Preprocessor tests pass | **Complete** (`cd767d4`, hardened in this milestone) |
| 3 | **Parser and AST** — recursive-descent parser, syntax-level AST, declarations/declarators/expressions/statements, diagnostics | Parser tests pass | **Complete** (`1d85cdd`, hardened in this milestone) |
| 4 | **Type system and semantic analysis** — type representation, scopes, name resolution, type checking, expression/statement semantics, annotations | Sema tests pass | **Complete** (this milestone) |
| 5 | **Nova64 code generation** — lower validated AST/IR into Nova assembly; frames, calls, control flow, supported data types; codegen tests | Generated assembly assembles and a C program runs under `nova-run` | **Complete** (this milestone) |
| 6 | **Compiler driver and toolchain integration** — `nova-cc` CLI, preprocessing/parsing/sema/codegen/assembler/linker pipeline, compilation modes, end-to-end tests | End-to-end CLI tests pass | **Complete** (`7d8c168`, `260c964`, `c4d4c77`) |
| 7 | **Runtime, libnova and end-to-end validation** — compiler runtime support, freestanding libnova, representative programs through compile/assemble/link/emulate, documented coverage, repeatable validation suite | Phase 1 validation suite green | **Complete** (this milestone) |

**Mandatory phase gate:** do not begin Phase 2 until all seven milestones above
are complete, tested, documented, and committed, and produce a Phase 1
completion report. Milestone 6 completion did **not** authorize Phase 2;
Milestone 7 was completed only under explicit direction. With all seven
milestones now complete, Phase 2 still requires separate explicit authorization.

### Milestone 2 implementation notes (preprocessor)

- **Pipeline/API:** `Preprocessor`, `preprocess(...)` (text output for future
  `nova-cc -E`) and `preprocess_to_tokens(...)` (token stream for the parser).
  Configuration: `language`, `include_paths`, `system_include_paths`, `defines`,
  `directory`.
- **Source mapping:** `SpliceSegment`/`LogicalLine` map logical columns back to
  physical line/column across backslash-newline splices. Token `leading_space`
  is preserved for deterministic text output and stringification.
- **Macro expansion:** hide-set (blue-paint) expansion with rescanning, argument
  prescan, recursion suppression, `#`/`##`, variadics, and GNU `, ## __VA_ARGS__`
  comma elision (explicitly an extension, not ISO C).
- **Directives:** `#if`/`#ifdef`/`#ifndef`/`#elif`/`#else`/`#endif`,
  `defined`, `#include`, `#define`, `#undef`, `#pragma once`, `#error`, `#line`,
  and the `_Pragma("...")` operator (in ordinary text and macro expansion).
  Conditional expressions use a dedicated integer-constant-expression parser.
- **Includes:** quoted and angle forms including computed includes built from
  expanded tokens (nested macros, whitespace/token-boundary variants). Resolution
  is deterministic; cycle/depth limits, `#pragma once`, and rejection of absolute
  and `..` paths are retained.
- **Language modes:** `__FILE__`/`__LINE__` are dynamic. `nova-c11` and `c11`
  define `__STDC__`/`__STDC_HOSTED__`; `__STDC_VERSION__` is defined only for
  explicit standard levels (`c11` => `201112`, `c99` => `199901`). No hosted or
  full ISO C conformance is claimed.
- **Not implemented:** `#embed`, trigraphs, digraphs, and obscure ISO corner
  cases; original whitespace/newline layout is not reproduced exactly.

### Milestone 3 implementation notes (parser and AST)

- **AST:** syntax-level nodes with source locations for declarations,
  declarators, parameters, initializers, expressions and statements. New nodes:
  `CompoundLiteral`, `GenericAssociation`, `GenericSelection`.
- **Grammar coverage:** declaration specifiers, typedef names, pointers, arrays,
  functions, parenthesized/abstract declarators, struct/union/enum members,
  bit-field syntax, designated initializers, compound literals, `_Generic`, VLA
  bounds, and flexible array members.
- **Diagnostics:** source-located `CompileError`s with bounded, deterministic
  fail-fast recovery at top-level/block boundaries. K&R definitions remain
  intentionally unsupported.

### Milestone 4 implementation notes (type system and semantic analysis)

- **Public API:** `nova.cc.analyze(tree, target="nova64")` returns the same
  annotated `TranslationUnit`; `Analyzer`, `Symbol`, `Scope`, `SemanticError` are
  available from `nova.cc.sema`. No driver/CLI entry point was added.
- **Type architecture:** `nova/cc/types.py` defines `Qualifiers`, primitive,
  pointer, array, function, struct, union and enum types, `TypeContext`, type
  equality/compatibility, completeness, size/alignment, integer promotion and
  usual arithmetic conversions. Widths/ranks follow `docs/NOVA_C_ABI.md` (LP64,
  8-byte pointers/long, 4-byte int, unsigned plain `char`, max alignment 16).
  Floating types carry `lowering_supported = False`.
- **Symbol/scope passes:** file, function, block and prototype scopes with
  separate ordinary/tag/label namespaces, shadowing, redeclarations, typedef
  aliases, forward tag declarations, aggregate/enum completion, duplicate member
  and enumerator checks, and bit-field/flexible-array constraints (simplified
  layout where the ABI is silent).
- **Declaration and expression passes:** specifier validation, declarator type
  construction preserving pointer/array/function nesting, parameter adjustment,
  identifier resolution, constant folding, lvalue checks, conversions, pointer
  arithmetic, casts, `sizeof`, calls, member access, compound literals and
  `_Generic`.
- **Initializer and statement passes:** scalar/aggregate/designated initializers,
  excess-element checks, return-type checks, `break`/`continue`/`case`/`default`
  context and duplicate checks, `goto` label resolution, and duplicate label
  checks. This is local validation, not a complete control-flow/data-flow
  analysis.
- **AST annotations:** `resolved_type`, `symbol`/`resolved_symbol`,
  `value_category`, `const_value`, plus `scope`, `semantic_ok`, and
  `type_context` on the translation unit. These are stable attributes for the
  future lowering pass; syntax nodes remain distinct from semantic types.
- **Tests:** `tests/test_cc_sema.py` (type model, declarators, valid programs,
  all required negative diagnostics, annotations). Targeted lexer/preprocessor
  (93), parser (69), and sema (65) tests pass; full suite 2076 passed,
  `compileall` clean, conformance 18/18.

### Milestone 5 implementation notes (Nova64 code generation)

- **Public API:** `nova.cc.generate(tree, target="nova64")` returns assembly
  text. `CodeGenerator` is available from `nova.cc.codegen`. No driver, file
  handling, or CLI was added.
- **Gate:** `generate` validates the target first, then requires
  `tree.semantic_ok is True` and a `TypeContext`; an unanalysed tree produces a
  source-located `CompileError` rather than an `AttributeError`.
- **Architecture:** direct AST-to-assembly lowering with stack-slot temporaries
  (`gen_expr`/`gen_addr`/`gen_cond`). No second IR, no SSA, no register
  allocator, no optimizer, and no instruction scheduler. Scalar lvalues carry
  addresses in their slots; scalar rvalues carry values; aggregates always carry
  addresses and are copied explicitly.
- **Code generation coverage:** all integer/pointer/array/struct/union/enum/
  `_Bool`/function-pointer types and the current statement/expression subset,
  including designated and nested initialisers, static locals, stack arguments,
  recursion, indirect calls, small and hidden-pointer aggregate returns,
  `switch` fall-through, and `goto`.
- **Determinism:** all labels are function-local `.Lcgu_*` names, so the same
  tree always produces byte-identical assembly. Function `.size` is computed
  from the real expanded instruction lengths (including `LI`/`LA` blocks).
- **Frontend fixes made while lowering:** `Type.unqualified` and
  `add_qualifiers` now preserve object identity when qualifiers are unchanged,
  so self-referential structs also complete through their pointer fields; a
  `void` call result allocates an integer scratch slot instead of a zero-sized
  `void` object. Both are regression-tested.
- **Tests:** `tests/test_cc_codegen.py` (55 tests) covers structural validation,
  target/semantic gates, allowed directives/pseudos/registers, frame layout,
  relocation kinds, object round-tripping, 21 end-to-end programs assembled,
  linked, and executed in the emulator, and source-located unsupported
  diagnostics. Frontend counts: lexer/preprocessor 105, parser 69, sema 81.
  Full suite 2147 passed; `compileall` clean; conformance 18/18.
- **Not implemented:** floating-point lowering, bit-field access, VLAs, K&R
  definitions, `va_start`/`va_arg`, PIC/GOT, long-call selection, automatic
  branch relaxation, and the `nova-cc` driver/CLI.

### Milestone 6 implementation notes (compiler driver and toolchain integration)

- **Entry points:** `nova.cc.driver.main`, `python -m nova.cc`, and the
  `nova-cc` console script (`nova.cli:compile_main`). User documentation is in
  `docs/NOVA_CC.md`.
- **Architecture:** the driver is orchestration only. It calls the existing
  `preprocess`/`preprocess_to_tokens`, `parse`, `analyze`, `generate`,
  `assemble_object`, `load_object`/`encode_object`, `link_objects`, and
  `encode_nob` APIs. No compiler, assembler, linker, or format logic is
  duplicated.
- **Modes:** `-E` stops after preprocessing (`.i`), `-S` after assembly
  (`.s`), `-c` after object encoding (`.nobj`), and the default mode links to
  `a.nob` (or the `-o` path). `-o` with multiple inputs is accepted only in
  link mode; `-e`/`--entry` is rejected outside link mode.
- **Options:** `-I`, ordered `-D`/`-U`, `-std=` (`nova-c11`, `c11`, `gnu11`,
  `c99`, `gnu99`, `c89`, `c90`), `-O0` only, `--target=nova64` only,
  `--long-calls`, `--long-branches`, `--version`, and `--help`. Unknown or
  unsupported options, targets, and optimization levels are rejected before any
  input is read.
- **Pipeline and files:** sources are read as UTF-8 (BOM tolerated), compiled
  through the real stages, and written only after the requested stage succeeds.
  Writes use a same-directory temporary file followed by `os.replace`. In
  multi-input non-link mode every input is compiled before any output is
  written. The driver refuses to overwrite an input, diagnoses missing inputs
  and output directories, and reports output-path collisions.
- **Linking and multi-TU builds:** link mode accepts C sources and `.nobj`
  inputs in any mixture. Static functions and objects carry internal linkage;
  the frontend fix records `static` on function definitions, so identically
  named statics in different translation units receive object-local symbols.
  External duplicate definitions and undefined symbols are reported by the
  existing linker. `.nobj` and `.nob` formats are unchanged.
- **Long calls/branches:** `--long-calls` emits `LA ra, .Lcgu_longcall_N` +
  `BL target` + return label. `--long-branches` emits `BL label` for
  unconditional jumps and an inverted short branch plus `BL label` for
  conditionals. `BL` is the assembler's existing ten-word `LI r15`/`JMP r15`
  pseudo with `PCREL_LONG`; no new instruction or relocation is introduced.
  Both are opt-in; without them an out-of-range displacement is an
  assembler/linker error with an actionable flag hint. There is no automatic
  relaxation.
- **Frontend hardening:** `-U` removes builtins and `-D` definitions in
  command-line order; VLAs are rejected explicitly; `sizeof` rejects
  incomplete struct/union/array, `void`, and function types while accepting
  pointers to incomplete types and completion before use; static function
  linkage conflicts are diagnosed.
- **Diagnostics:** filename/line/column where the stage supplies them, a stage
  label (`preprocessing`, `parsing`, `semantic analysis`, `code generation`,
  `assembly`, `link`), exit code `1` for user errors, `2` for usage errors,
  `70` for internal errors, and `130` for interruption. Ordinary user errors do
  not print a traceback.
- **Tests:** `tests/test_cc_driver.py` has 55 tests: 12 CLI surface, 10 output
  behaviour, 9 preprocessing, 5 assembly, 5 unsupported-construct diagnostics,
  9 object/link, and 5 emulator execution. `tests/test_cli.py` has 28 tests.
  Frontend counts: lexer/preprocessor 107, parser 69, sema 85, codegen 57.
  Full suite 2210 passed; `compileall` clean; conformance 18/18.
- **Not implemented at that milestone:** runtime helpers, `libnova`,
  crt0/start files, hosted C, full variadic argument access, PIC/GOT, automatic
  relaxation, and any optimizer/register allocator. Milestone 7 supplied the
  freestanding runtime, `libnova`, startup, and variadic access; the remaining
  items are future work.

### Milestone 7 implementation notes (runtime, libnova and validation)

- **Package:** `nova/runtime/` contains `startup.s`, `syscalls.s`, nine headers,
  and three C sources. Runtime C is compiled through the real compiler and
  assembler APIs with long calls/branches, cached as immutable templates, and
  deep-copied per link. `pyproject.toml` installs the runtime as package data.
- **Startup:** `_start` supplies `sp = 0x80000`, calls `int main(void)` via
  `LA ra`/`BL`, then `HALT`s with `main`'s return in `a0`. It is freestanding and
  does not claim hosted startup semantics. See `docs/RUNTIME.md`.
- **Syscalls:** `nova_write`/`write`, `nova_read`/`read`, `nova_time`,
  `nova_exit`, and `nova_halt` wrap the existing syscall/ISA contract. The
  reference model has no kernel vector; validation uses
  `dispatch_syscall` and resumes at `EPC`.
- **libnova:** `mem*`, `str*`, integer conversions, termination, `printf`,
  `putchar`, and `puts` are implemented. `printf` supports exactly
  `%c %s %d %i %u %x %X %p %%` and emits unsupported modifiers literally.
- **Variadics:** the parser recognizes `__builtin_va_arg(ap, type)`; codegen
  implements `va_start`, `va_end`, `va_copy`, and typed `va_arg` against the
  unchanged 24-byte ABI state. Default promotions include array/function decay.
- **Hardening:** multi-line block comments and directive continuation were
  fixed in the preprocessor; odd-sized aggregate ABI chunks, incoming parameter
  stores, aggregate `va_arg` results, immediate shifts, and switch labels nested
  in `if` statements were fixed in the backend.
- **Driver:** `--runtime`, `--nostartfiles`, and `--nostdlib` are link-mode
  options. Runtime links imply long calls/branches for user sources and add the
  built-in runtime include path behind user `-I` directories.
- **Validation:** `scripts/phase1_validate.py` runs 12 ordered stages. Latest
  result: frontend 180, sema 85, codegen 59, driver 55, runtime 15, library 1,
  end-to-end 13, emulator 1831, example 1, example output exact, conformance
  18/18, `compileall` clean, full suite 2245 passed, overall PASS. See
  `docs/VALIDATION.md` and `docs/PHASE1_COMPLETION.md`.
- **Intentional non-goals:** hosted C, full ISO C conformance, input streams,
  allocation, FP/soft-float, PIC/GOT, automatic relaxation, kernel/device
  integration, and any ISA or ABI change.

## 7. Known limitations (documented honestly)

All Milestone 7 blockers are closed. The following limitations are intentional
for Phase 1 or reserved for future work; the full four-way classification is in
`docs/PHASE1_COMPLETION.md`.

**Intentional limitations**

- The runtime is freestanding: fixed initial stack, `main(void)`, `HALT`
  termination, no hosted process model, environment, loader, or cleanup.
- The reference emulator has no kernel vector; syscall execution requires the
  documented host dispatcher. `SYS_READ` has no input source and returns `-1`.
- `printf` is scoped to `%c %s %d %i %u %x %X %p %%`; unsupported modifiers are
  literal. There is no `FILE`, input, `snprintf`, or locale support.
- No floating-point lowering; floating constants are rejected by the lexer and
  FP objects/operations are rejected with source-located diagnostics.
- Bit-field layout is provisional where `docs/NOVA_C_ABI.md` does not specify a
  rule, and the backend rejects bit-field access and initialisation.
- VLAs are rejected explicitly. Flexible-array-member objects and other objects
  without a static size are not lowerable. `sizeof` of an incomplete type is
  diagnosed.
- Some implicit-conversion checks are deliberately conservative; diagnostics
  target the supported subset rather than full ISO C conformance.
- Not a complete ISO C preprocessor: `#embed`, trigraphs, digraphs, and some
  obscure standard corner cases are intentionally omitted. GNU
  `, ## __VA_ARGS__` remains an extension.
- K&R old-style function definitions are intentionally unsupported.
- Default calls/branches remain compact and range-limited outside `--runtime`.
  There is no automatic relaxation or linker-inserted stub.

**Future enhancements (not Phase 1 blockers)**

- Full hosted libc, formatter conformance, input streams, allocation, and
  process management.
- PIC/GOT, dynamic linking, weak symbols, linker scripts, dead-code elimination,
  and an optimizer/scheduler/register allocator.
- 128-bit helpers, soft-float, kernel/device integration, and hardware RTL.

## 8. Definition of done (all seven milestones)

- A C source file compiles with `nova-cc` into a `.nob` that halts correctly
  under `nova-run`.
- Arithmetic, conditionals, loops, functions, recursion, arrays, pointers,
  structures, multiple translation units, and libnova calls each have
  end-to-end tests.
- Every diagnostic is source-located and deterministic.
- Existing assembler, linker, emulator, object-format, and conformance tests
  continue to pass unchanged.
- Freestanding startup, syscall wrappers, headers, variadics, formatted output,
  and `--runtime` linking have end-to-end tests and exact-output validation.
- A repeatable validation script reports every stage green and a Phase 1
  completion report is produced.
- Phase 1 is 7/7 (100%); Phase 2 starts only after explicit authorization.
