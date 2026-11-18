# Phase 1 plan — Nova64 freestanding C compiler and libnova

**Status:** active plan. This document records the compiler architecture, the
initial supported C subset, the code-generation contract, and the order in which
the compiler is built. It is kept in sync with `docs/PORTING_READINESS.md`.

The ISA remains frozen at `Nova ISA v1.0`. Phase 1 adds software only.

**Current progress:** Steps 1–3 are complete (lexer/diagnostics, hardened
preprocessor, and parser/AST with tests). Next milestone: Step 4 — type system
and semantic analysis.

## 1. Pipeline

```
C source
  -> preprocessor          nova/cc/preprocessor.py   (#include, #define, #if...)
  -> lexer                 nova/cc/lexer.py          (tokens + source locations)
  -> parser                nova/cc/parser.py         (typed AST shapes)
  -> semantic analysis     nova/cc/sema.py           (types, scopes, conversions)
  -> code generation       nova/cc/codegen.py        (Nova assembly text)
  -> assembler             nova.assembler            (.nobj, .nox metadata)
  -> linker                nova.link                 (.nob executable)
  -> emulator              nova-run                  (execution)
```

The compiler is written in Python for the same reason the assembler and linker
are: it must be inspectable, testable, and changeable while the architecture is
young. It emits **real Nova assembly** and goes through the existing tools; it
does not contain a second binary encoder.

### Why direct lowering instead of a separate SSA IR

Phase 1 uses a typed AST and a recursive lowering pass with an explicit frame
and temporary model. Every temporary is a stack slot, so the generated code is
correct by construction and easy to inspect. This is deliberately conservative:
an SSA IR, register allocation, and instruction scheduling can be added later
without changing the front end or the ABI. The lowering pass is split into
`gen_expr`, `gen_addr`, and `gen_cond`, which are the same interfaces an IR
builder would use.

## 2. Module layout

| Module | Responsibility |
| --- | --- |
| `nova/cc/diagnostics.py` | Source locations, diagnostics, `CompileError` |
| `nova/cc/preprocessor.py` | Line splicing, comments, includes, macros, conditionals |
| `nova/cc/lexer.py` | C tokens, literals, escapes, keyword classification |
| `nova/cc/ast.py` | AST node classes |
| `nova/cc/types.py` | LP64 C types, sizes, alignment, struct layout, conversions |
| `nova/cc/parser.py` | Recursive-descent parser with C declarator precedence |
| `nova/cc/sema.py` | Scopes, symbols, type checking, implicit conversions |
| `nova/cc/codegen.py` | Nova64 assembly emission, frames, calls, ABI |
| `nova/cc/driver.py` | End-to-end compile pipeline and file handling |
| `nova/cc/__main__.py` | `python -m nova.cc` |

`nova/cli.py` gains `compile_main`, exposed as the `nova-cc` console script.

## 3. Supported C subset (Phase 1 target)

This is the subset the compiler is expected to accept when Phase 1 is complete.
Anything not listed must produce a diagnostic, not silently miscompile.

### Types

- `void`
- `_Bool`
- `char` (unsigned by ABI), `signed char`, `unsigned char`
- `short`, `unsigned short`, `short int`, `unsigned short int`
- `int`, `unsigned int`, `signed`, `unsigned`
- `long`, `unsigned long`, `long long`, `unsigned long long`
- pointers, including pointers to functions and pointers to pointers
- arrays of any supported element type, including multidimensional arrays
- structures and unions with scalar, pointer, array, struct and union members
- enumerations (represented as `int`)
- `typedef` names
- `const` and `volatile` are accepted as qualifiers and ignored for code
  generation, because the base ISA has no memory-protection qualifier semantics
  beyond the MMU

Not supported in Phase 1: `float`, `double`, `long double`, bit-fields,
`restrict`, `_Atomic`, `_Complex`, `_Generic`, VLA, `inline`, `_Noreturn`.
`float`/`double` receive a clear "no base-ISA floating point" diagnostic.

### Expressions

Integer and character constants; string literals; identifiers; `sizeof`
(expression and type); unary `+ - ! ~ * & ++ --`; casts; `* / %`; `+ -`;
`<< >>`; `< <= > >=`; `== !=`; `& ^ |`; `&& ||`; `?:`; assignment
`= += -= *= /= %= <<= >>= &= ^= |=`; comma; function calls; array subscript;
member access `.` and `->`; postfix `++ --`; pointer arithmetic; address of
functions and objects.

### Statements

Compound statements; declarations anywhere a statement is allowed; expression
statements; `if`/`else`; `while`; `do`/`while`; `for`; `break`; `continue`;
`return`; `switch`/`case`/`default`; empty statement.

### Functions and program structure

Function declarations and definitions; prototypes; `void` parameter lists;
variadic declarations (`...`) are parsed, and `stdarg.h` is provided, but
variadic *calls* are limited to the integer rules of the ABI; recursion;
multiple translation units; global variables with constant initializers;
`static` and `extern` storage classes; `main` returning `int`.

## 4. Nova64 code-generation contract

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
operating-system interface is the existing standard syscall table.

## 6. Implementation order and gates

| Step | Deliverable | Gate | Status |
| --- | --- | --- | --- |
| 1 | Architecture plan, package skeleton, lexer, diagnostics | Lexer tests pass | **Complete** (`d559f99`, `e439de2`) |
| 2 | Preprocessor hardening (macros, directives, diagnostics, modes) | Preprocessor tests pass | **Complete** (this milestone) |
| 3 | AST + recursive-descent parser | Parser tests pass | **Complete** (this milestone) |
| 4 | Type system + semantic analysis | Sema tests pass | Pending |
| 5 | Codegen: arithmetic, locals, return, first end-to-end program | `nova-run` executes a C program | Pending |
| 6 | Calls, recursion, globals, ABI tests | Recursive fib runs | Pending |
| 7 | Control flow (`if`, loops, `switch`) | Loop/switch tests pass | Pending |
| 8 | Pointers, arrays, strings, structs, enums, typedefs | Pointer/struct tests pass | Pending |
| 9 | libnova headers and functions | libnova tests pass | Pending |
| 10 | `nova-cc` CLI, `-c`/`-S`/`-E`, multi-TU linking | CLI tests pass | Pending |
| 11 | Documentation and readiness update | Full suite green | Pending |

Each step is committed separately after its tests pass.

### Step 2 implementation notes (preprocessor)

- **Pipeline/API:** `nova.cc.preprocessor` provides `Preprocessor`, `preprocess(...)`
  (text output for future `nova-cc -E`) and `preprocess_to_tokens(...)` (token
  stream for the parser). The API remains stable and accepts language/include
  configuration (`language`, `include_paths`, `system_include_paths`, `defines`,
  `directory`). Public exports are available from `nova.cc`.
- **Line processing and source locations:** backslash-newline splicing, logical-line
  directive handling, and comment stripping are implemented. Tokens carry logical
  file/line information; `#line` remapping updates the logical file/line used for
  subsequent tokens and for `__FILE__`/`__LINE__` expansion (physical source identity
  is still used for include processing where appropriate).
- **Macro expansion:** object-like and function-like macros use hide-set (blue-paint)
  expansion with rescanning that reinserts replacement tokens ahead of remaining
  input. This supports argument prescan in invocation context, direct and indirect
  recursion suppression, nested function-like macro invocations, and function-like
  invocations formed across expansion boundaries. Macro redefinition is diagnosed
  unless identical; expansion depth/termination guards prevent infinite recursion.
- **`#` / `##` / variadics:** stringification uses argument token spellings with
  whitespace normalization based on lexer `leading_space` information and escaping
  required for embedding quoted/backslash characters in string literals. `##` is
  applied in substituted replacement sequences before rescanning, supports
  consecutive pastes, handles empty operands via placemarkers in common cases, and
  diagnoses invalid pasted tokens. Variadic macros (`...`, `__VA_ARGS__`) support
  empty and nested-parenthesized arguments. GNU-style `, ## __VA_ARGS__` comma
  elision is supported as an optional extension and is not claimed as ISO C behavior.
- **Conditionals and directives:** `#if`, `#ifdef`, `#ifndef`, `#elif`, `#else`,
  `#endif`, `defined`/`defined(...)`, `#include`, `#define`, `#undef`, `#pragma once`,
  `#error`, and `#line` are implemented. Conditional expressions are evaluated by a
  dedicated integer-constant-expression parser (no Python `eval()`), with macro
  expansion and undefined identifiers treated as `0`. Unknown pragmas are ignored.
  Unterminated conditionals, malformed directives, argument-count mismatches, and
  invalid macro operations produce diagnostics.
- **Includes:** quoted and angle-bracket header names are supported, including
  computed includes after macro expansion for string-literal and `<...>` forms.
  Resolution is deterministic (including file directory for quoted includes, then
  configured include paths/system include paths), with include-cycle detection, max
  include depth, missing-file diagnostics, `#pragma once`, and rejection of
  absolute and `..`-containing include names as a safety restriction.
- **Language modes and built-ins:** `__FILE__` and `__LINE__` expand dynamically.
  The default documented mode is `nova-c11` (freestanding-oriented): `__STDC__=1`
  and `__STDC_HOSTED__=0` are predefined. `__STDC_VERSION__` is defined only for
  explicit standard-level modes (`c11` => `201112L`, `c99` => `199901L`); custom
  modes may omit these macros. No hosted-library or full ISO C conformance is claimed.
- **Remaining preprocessor limitations:** not a complete ISO C preprocessor. Original
  whitespace/newline layout is not fully preserved in `-E`-style text rendering,
  logical/physical location mapping is line-oriented (not fully precise for all
  splice/expansion column cases), computed include reconstruction for unusual token
  sequences may be limited, `_Pragma`, `#embed`, digraph/trigraph preprocessing
  features, and some obscure standard corner cases are not implemented. GNU
  `, ## __VA_ARGS__` is an extension. Tests: `tests/test_cc_lexer.py` and
  `tests/test_cc_preprocessor.py` cover lexing, macro expansion/prescan/recursion,
  `#`/`##`/variadics, conditionals, includes/guards, `#line`, built-ins/modes, and
  directive diagnostics.

### Step 3 implementation notes (parser and AST)

- **Pipeline/API:** `nova/cc/ast.py` defines syntax-level AST nodes with source
  locations for translation units, declarations/specifiers/qualifiers, declarators
  (identifiers, pointers, arrays, function declarators, parenthesized and abstract
  declarators), parameters, initializers, expressions, statements, and function
  definitions. `nova/cc/parser.py` implements a hand-written recursive-descent parser
  consuming preprocessed tokens. `nova.cc` exposes `Parser`, `parse(tokens, ...)`,
  and `parse_source(source, ...)` (C source → preprocessor → parser → AST) while
  keeping the preprocessor API stable.
- **Declarations and declarators:** supports declaration specifiers including
  storage classes, type qualifiers, function specifiers, primitive type sequences,
  struct/union/enum specifiers, and typedef names. Parses init-declarators, multiple
  declarators per declaration, initializers (including brace initializer lists), and
  C declarator structure for pointers, arrays, function declarators, parenthesized
  declarators, pointer-to-array, arrays of pointers, function pointers, and functions
  returning pointers. Struct/union members and enum enumerators are parsed; bit-field
  declarators are parsed when present in the member syntax.
- **Expressions:** precedence-climbing/recursive-descent expression parsing covers
  primary expressions, postfix operations (call, subscript, `.`/`->`, postfix
  `++/--`), unary operators, `sizeof` (expression and type-name forms), casts,
  multiplicative/additive/shift/relational/equality/bitwise/logical operators,
  conditional `?:`, assignment operators, and comma expressions with C associativity.
- **Statements:** parses compound statements, declarations in blocks, expression/null
  statements, `if`/`else`, `while`, `do`/`while`, `for` (including declaration init
  where supported by the parser), `switch`/`case`/`default`, `break`, `continue`,
  `return`, `goto`, and labels.
- **Diagnostics and recovery:** syntax errors are reported as source-located
  `CompileError` diagnostics (unexpected tokens, missing delimiters/`;`, malformed
  declarations/expressions/declarators, unmatched braces, unexpected EOF in tested
  cases). Basic synchronization is implemented at top-level/block boundaries to avoid
  infinite loops; the current parser fails fast with a useful diagnostic rather than
  producing a recovered multi-error parse.
- **Supported subset (syntax):** sufficient C declaration/declarator/expression/
  statement forms for early Phase 1 semantic analysis and code generation work,
  including structs/unions/enums, typedef names, pointers/arrays/functions, and core
  control flow. Not yet a complete C grammar: abstract declarator/type-name coverage
  is limited to common cast/sizeof cases, K&R old-style function definitions are not
  a target, designated initializers/compound literals/generic selections/VLAs/flexible
  array edge cases and some standard declarator corners may be incomplete, and no
  semantic/type validation is performed yet.
- **Tests:** `tests/test_cc_parser.py` covers basic translation-unit/function parsing,
  required declarator forms, expressions/precedence/associativity, statements/control
  flow, declarations/specifiers/typedefs/aggregates/enums, preprocessed source
  parsing, and representative error cases.

## 7. Known Phase 1 limitations (to be documented honestly)

- No floating point (base ISA has none).
- No bit-fields, VLAs, `_Atomic`, `_Generic`, `_Complex`.
- No PIC/GOT code generation yet; globals use absolute `LA64`/`ABS64`.
- No automatic linker relaxation; `--long-calls` is the supported workaround.
- No optimizer: every temporary is a stack slot.
- Variadic support is limited until compiler builtins for `va_start`/`va_arg`
  are added; `stdarg.h` is provided as the documented target.
- `long double` is not supported.

## 8. Definition of done

- A C source file compiles with `nova-cc` into a `.nob` that halts correctly
  under `nova-run`.
- Arithmetic, conditionals, loops, functions, recursion, arrays, pointers,
  structures, multiple translation units, and libnova calls each have
  end-to-end tests.
- Every diagnostic is source-located and deterministic.
- Existing assembler, linker, emulator, object-format, and conformance tests
  continue to pass unchanged.
