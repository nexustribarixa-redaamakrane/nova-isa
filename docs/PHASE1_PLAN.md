# Phase 1 plan — Nova64 freestanding C compiler and libnova

**Status:** active plan. This document records the compiler architecture, the
initial supported C subset, the code-generation contract, and the order in which
the compiler is built. It is kept in sync with `docs/PORTING_READINESS.md`.

The ISA remains frozen at `Nova ISA v1.0`. Phase 1 adds software only.

**Current progress:** Milestones 1–4 are complete (foundation, preprocessor,
parser/AST, and type system + semantic analysis). Milestones 5–7 (Nova64 code
generation, compiler driver/toolchain integration, runtime/libnova/validation)
remain pending. **Phase 2 is not authorized until all seven milestones are
complete, tested, documented, and committed.** Subtasks are internal stages and
must not become additional major milestones.

## 1. Pipeline

```
C source
  -> preprocessor          nova/cc/preprocessor.py   (#include, #define, #if...)
  -> tokens                nova/cc/lexer.py          (tokens + source locations)
  -> parser                nova/cc/parser.py         (syntax-level AST)
  -> semantic analysis     nova/cc/sema.py           (types, scopes, checks)
  -> annotated AST         nova/cc/types.py          (LP64 Nova64 type model)
  -> code generation       nova/cc/codegen.py        [Milestone 5 — pending]
  -> assembler             nova.assembler            (.nobj, .nox metadata)
  -> linker                nova.link                 (.nob executable)
  -> emulator              nova-run                  (execution)
```

The compiler is written in Python for the same reason the assembler and linker
are: it must be inspectable, testable, and changeable while the architecture is
young. The intended final product emits **real Nova assembly** and goes through
the existing tools; it does not contain a second binary encoder. No code
generation exists yet in this tree.

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
| `nova/cc/codegen.py` | Nova64 assembly emission, frames, calls, ABI | Pending (Milestone 5) |
| `nova/cc/driver.py` | End-to-end compile pipeline and file handling | Pending (Milestone 6) |
| `nova/cc/__main__.py` | `python -m nova.cc` | Pending (Milestone 6) |

`nova/cli.py` gains `compile_main`, exposed as the `nova-cc` console script, in
Milestone 6.

## 3. Supported C subset (frontend status)

This is the subset the frontend accepts today. Anything not listed must produce
a diagnostic, not be silently miscompiled. Code generation support is a separate
question: no Nova64 lowering exists yet.

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
declarations, recursion, file/function/block/prototype scopes, tag namespaces,
and `extern`/`static` storage classes are handled at the syntax/semantic level.
K&R old-style definitions are intentionally not a target.

## 4. Nova64 code-generation contract (Milestone 5)

The eventual lowering pass must obey the following; none of it is implemented
by the current frontend.

- Results are computed in `a0`; `a0` is also the return-value register.
- `t0`-`t3` and `a0`-`a5` are caller-saved scratch within a function.
- `s0`-`s3`, `gp`, `sp`, `fp` are never modified by generated code except `sp`
  for the frame; no callee-saved register is used as a scratch register.
- `ra` is saved in the frame of every non-leaf function and restored at the
  epilogue. A leaf function that never calls does not need to save `ra`.
- Every frame is 16-byte aligned. `sp` is 16-byte aligned at every `CALL`.
- Locals and compiler temporaries live at positive offsets from `sp`; incoming
  stack arguments are at `sp + frame_size + 8*(i-6)` after the prologue.
- Outgoing stack arguments occupy a fixed area at `[sp, sp + outgoing_size)`
  and are never disturbed by local or temporary allocation.
- Memory operands whose displacement does not fit the signed 14-bit field are
  materialised with `LI` + `ADD` into a scratch register.
- Calls use `CALL symbol`. The compiler can emit a long-call sequence
  (`LA t2, symbol; LA ra, .Lret; JMP t2; .Lret:`) when `--long-calls` is given.
  A `PCREL_DISP14` overflow is a linker error with an actionable diagnostic.
- Globals are addressed with `LA rd, symbol`; pointer-valued globals use
  `.quad symbol` and therefore `ABS64`.
- String literals are emitted in `.rodata` and addressed with `LA`.
- Every function emits `.global`, `.type name, function`, and `.size`.

## 5. Runtime and library boundary

| Layer | Contents |
| --- | --- |
| compiler runtime | Only helpers the compiler actually emits: currently none beyond the ISA; `__nova_divdi3`/`__nova_moddi3` are reserved for builds that request trapping-free division |
| libnova | Freestanding C library: headers, `mem*`, `str*`, integer conversion, `putchar`/`puts`/`write`, and a small `printf` when varargs support lands |
| crt0 | `_start` that calls `main`, then `HALT` (exit code left in `a0`) |

Compiler-generated code never assumes a hosted operating system. The only
operating-system interface is the existing standard syscall table. None of this
runtime layer is implemented yet.

## 6. Seven-milestone Phase 1 roadmap and gates

There are exactly **seven** major Phase 1 milestones. Internal implementation
stages and subtasks do not count as additional milestones.

| # | Milestone | Gate | Status |
| --- | --- | --- | --- |
| 1 | **Compiler foundation** — architecture plan, package skeleton, diagnostics, lexer | Lexer tests pass | **Complete** (`d559f99`, `e439de2`) |
| 2 | **Preprocessor** — directives, macros, conditionals, includes, language modes, regression hardening | Preprocessor tests pass | **Complete** (`cd767d4`, hardened in this milestone) |
| 3 | **Parser and AST** — recursive-descent parser, syntax-level AST, declarations/declarators/expressions/statements, diagnostics | Parser tests pass | **Complete** (`1d85cdd`, hardened in this milestone) |
| 4 | **Type system and semantic analysis** — type representation, scopes, name resolution, type checking, expression/statement semantics, annotations | Sema tests pass | **Complete** (this milestone) |
| 5 | **Nova64 code generation** — lower validated AST/IR into Nova assembly; frames, calls, control flow, supported data types; codegen tests | Generated assembly assembles and a C program runs under `nova-run` | Pending |
| 6 | **Compiler driver and toolchain integration** — `nova-cc` CLI, preprocessing/parsing/sema/codegen/assembler/linker pipeline, compilation modes, end-to-end tests | End-to-end CLI tests pass | Pending |
| 7 | **Runtime, libnova and end-to-end validation** — compiler runtime support, freestanding libnova, representative programs through compile/assemble/link/emulate, documented coverage, repeatable validation suite | Phase 1 validation suite green | Pending |

**Mandatory phase gate:** do not begin Phase 2 until all seven milestones above
are complete, tested, documented, and committed, and produce a Phase 1
completion report. Milestone 4 completion does **not** authorize Phase 2 and does
not start Milestone 5 automatically without explicit direction.

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

## 7. Known limitations (documented honestly)

- No Nova64 code generation, executable, CLI, runtime or libnova yet.
- No floating-point lowering; floating constants are rejected by the lexer.
- Bit-field layout is provisional where `docs/NOVA_C_ABI.md` does not specify a
  rule; `sizeof` of an incomplete type is not yet diagnosed.
- Some implicit-conversion checks are deliberately conservative; diagnostics
  target the supported subset rather than full ISO C conformance.
- Not a complete ISO C preprocessor: `#embed`, trigraphs, digraphs, and some
  obscure standard corner cases are intentionally omitted. GNU
  `, ## __VA_ARGS__` remains an extension.
- No PIC/GOT code generation, linker relaxation, optimizer, or complete variadic
  call support; those belong to later milestones.
- K&R old-style function definitions are intentionally unsupported.

## 8. Definition of done (all seven milestones)

- A C source file compiles with `nova-cc` into a `.nob` that halts correctly
  under `nova-run`.
- Arithmetic, conditionals, loops, functions, recursion, arrays, pointers,
  structures, multiple translation units, and libnova calls each have
  end-to-end tests.
- Every diagnostic is source-located and deterministic.
- Existing assembler, linker, emulator, object-format, and conformance tests
  continue to pass unchanged.
- A Phase 1 completion report is produced and Phase 2 is started only after
  explicit authorization.
