# Phase 1 completion report — Milestone 7

**Result:** Milestone 7 **COMPLETE**. All seven Phase 1 milestones are
**COMPLETE**: **Phase 1 7/7 (100%)**. Phase 2 is not authorized and no eighth
milestone has been created.

This report covers the 25 required closeout items. The Milestone 6 baseline is
`396ff16`; the Milestone 7 implementation and documentation commits follow it
in `master` history. `Nova ISA v1.0` and `Nova C ABI v1.0` remain frozen.

## 1. Exact milestone structure

**Status: delivered — intentional limitation: no extra milestones.** Phase 1
still contains exactly seven milestones. Runtime, `libnova`, variadics,
formatter, driver integration, validation, and documentation are recorded under
Milestone 7, not split into new top-level milestones. `docs/PHASE1_PLAN.md` shows
all seven as COMPLETE.

## 2. ISA and ABI stability

**Status: delivered — intentional limitation: no architecture changes.**
No instruction, encoding, register, alias, CSR, privilege mode, exception code,
translation rule, relocation kind, syscall number, or container format changed.
`docs/NOVA_C_ABI.md` remains authoritative and was not modified. The 24-byte
`va_list` layout at offsets 0/4/8/16 remains unchanged.

## 3. Runtime package and compiler-runtime boundary

**Status: delivered.** `nova/runtime/` owns the startup, syscall wrappers, nine
headers, and three C sources. It reuses the existing preprocessor, parser,
sema, codegen, assembler, object system, and linker instead of duplicating
them. Objects are compiled with long forms, cached as templates, and deep-copied
per link. No `__nova_*` helper body is claimed: native `DIV`/`UDIV`/`REM`/`UREM`
are emitted and no helper symbols are referenced.

## 4. Required headers

**Status: delivered.** `stddef.h`, `stdint.h`, `stdbool.h`, `limits.h`,
`string.h`, `stdlib.h`, `stdio.h`, `stdarg.h`, and `nova.h` exist under
`nova/runtime/include`. They compile together in an executed test that checks
LP64 sizes, unsigned `char` limits, `offsetof`, and typedefs.

## 5. Freestanding startup contract

**Status: delivered — intentional limitation: not hosted startup.** `_start`
supplies `sp = 0x80000`, invokes `int main(void)` via `LA ra` and `BL`, then
executes `HALT` with `main`'s return in `a0`. There are no `argc`, `argv`,
`envp`, loader services, or process cleanup semantics. `docs/RUNTIME.md`
documents the stack supplier, entry symbol, invocation, return handling, and
termination.

## 6. Syscall wrappers and host dispatch

**Status: delivered — intentional limitation: no kernel in the reference
model.** `nova_write`/`write`, `nova_read`/`read`, `nova_time`, `nova_exit`, and
architectural `nova_halt` are implemented. The host harness dispatches
`SYSTEM_CALL`, resumes at `EPC`, and clears the exception frame. `SYS_READ` has
no reference input source and returns unsupported (`-1`); this is documented,
not hidden.

## 7. Memory and string routines

**Status: delivered.** `memcpy`, `memmove`, `memset`, `memcmp`, `strlen`,
`strcmp`, `strncmp`, `strcpy`, `strncpy`, and `strcat` are implemented and
exercised, including overlapping `memmove` in both directions and `strncpy`
padding.

## 8. Conversion and termination routines

**Status: delivered.** `atoi`, `strtol`, `strtoul`, `exit`, and `abort` are
implemented. `strtol` uses independent signed parsing with `LONG_MIN`/
`LONG_MAX` saturation; `strtoul` saturates at `ULONG_MAX`. Invalid bases and
end-pointer behaviour are tested.

## 9. Scoped printf and output helpers

**Status: delivered — intentional limitation: scoped formatter.** `printf`
supports `%c %s %d %i %u %x %X %p %%`; `putchar` and `puts` write through
`nova_write`. Unsupported flags, widths, precisions, and length modifiers are
emitted literally. There is no `snprintf`, `FILE`, locale, or input API.

## 10. ABI-compatible variadic state

**Status: delivered.** `stdarg.h` exposes the authoritative 24-byte layout.
A variadic prologue spills `a0`–`a5`, records the incoming stack pointer, and
`va_start` initializes `gp_offset`, reserved, `overflow_arg_area`, and
`reg_save_area` exactly as the ABI specifies.

## 11. Variadic frontend

**Status: delivered.** `VaArgExpression` is produced by the parser for
`__builtin_va_arg(ap, type)`; semantic analysis validates the type and applies
default argument promotions, including array/function decay. `va_start`,
`va_end`, and `va_copy` intrinsics are recognized as compiler builtins.

## 12. Variadic lowering

**Status: delivered.** `va_arg` chooses the register save area or stack area,
advances the correct cursor, supports multi-chunk aggregates, and returns
aggregates by address. Tests cover zero/multiple reads, register/stack/mixed
arguments, pointers, narrowed integers, `va_copy`, repeated and independent
lists, and 6/12/16-byte structs.

## 13. Preprocessor hardening for runtime sources

**Status: delivered.** Multi-line block comments are blanked before directive
recognition, preserve diagnostic columns, handle strings and character
constants, allow directives to continue across comment lines, and report an
unterminated comment at its start location. Regression tests were added.

## 14. Backend correctness fixes

**Status: delivered.** Odd-sized aggregate chunks now load and store only legal
widths without overwriting neighbours; incoming parameter stores use the same
helper; shifts use immediates; aggregate `va_arg` returns a pointer-valued
result; and `switch` labels nested inside `if` statements are collected from
`then_branch`. Regression tests were added.

## 15. Driver runtime integration

**Status: delivered.** `nova-cc --runtime` is link-mode only and appends
startup, syscall wrappers, and `libnova` objects. It rejects misuse in `-E`,
`-S`, and `-c`. Determinism is tested by comparing two separately linked
images byte-for-byte.

## 16. Runtime link controls

**Status: delivered.** `--nostartfiles` omits only `_start`; `--nostdlib` omits
only the three `libnova` C objects while retaining syscall wrappers. Both
require `--runtime`. Missing `main` and missing library symbols remain linker
errors with stage labels.

## 17. Built-in include path

**Status: delivered.** `nova/runtime/include` is a built-in system include
directory for every `nova-cc` mode. User `-I` paths are searched first, and a
test proves that a user `stdio.h` overrides the built-in header.

## 18. Long-call and two-step link policy

**Status: delivered — intentional limitation: no automatic relaxation.**
`--runtime` implies `--long-calls` and `--long-branches` for user sources
because the runtime image exceeds compact displacement assumptions. Plain
links remain compact and range-limited. Prebuilt `.nobj` files are never
rewritten, so two-step runtime links require compiling user objects with
`--long-calls --long-branches`; this is documented and tested.

## 19. End-to-end example

**Status: delivered.** `examples/hello_runtime.c` exercises `strcpy`, `printf`,
and `puts` through `--runtime`. Validation asserts exact bytes
`b'hello world -42 42 ff FF ! %\ndone\n'`, `a0 == 0`, and `machine.halted`.

## 20. Runtime test coverage

**Status: delivered.** `tests/test_runtime.py` adds 29 tests covering package
structure, startup, syscalls, headers, memory/string/conversion routines,
variadics, formatter edge cases, option validation, multi-TU links, and
include precedence.

## 21. Repeatable validation

**Status: delivered.** `scripts/phase1_validate.py` runs 11 ordered stages.
Latest result: frontend 180, sema 85, codegen 59, driver 55, runtime 15,
library 3, end-to-end 13, example 1, conformance 18/18, `compileall` passed,
full suite 2245 passed, overall **PASS**. See `docs/VALIDATION.md`.

## 22. Packaging

**Status: delivered.** `pyproject.toml` includes `nova*` packages and declares
the runtime headers, C sources, and assembly files as package data. The runtime
is therefore available in installed distributions, not only from a checkout.

## 23. Documentation

**Status: delivered.** `docs/RUNTIME.md`, `docs/VALIDATION.md`, and this report
are new. `docs/PHASE1_PLAN.md`, `docs/PORTING_READINESS.md`,
`docs/COMPILER_RUNTIME.md`, `docs/NOVA_CC.md`, and stale README scope statements
were updated to describe the implemented runtime and retained limitations.

## 24. Blocker and limitation classification

**Status: delivered.**

| Classification | Items |
| --- | --- |
| Must fix for Milestone 7 | **None.** Every Milestone 7 acceptance item is implemented, tested, documented, and validated. |
| Must fix for Phase 1 completion | **None after this report, roadmap update, validation run, and synchronized push.** No Phase 1 gate remains open. |
| Intentional limitation | Freestanding-only startup; fixed stack supplier; no hosted process model; no kernel vector; `SYS_READ` unsupported by the reference model; scoped `printf`; no `FILE`/input/`snprintf`; no full ISO C conformance; no FP/soft-float, bit-field access, VLAs, K&R definitions, PIC/GOT, automatic relaxation, allocator, threads, or dynamic linking. |
| Future enhancement | Full hosted libc and formatter conformance, input streams, allocation, process management, GOT/PIC and dynamic linking, linker scripts/weak symbols/dead-code elimination, GCC/LLVM-quality optimization and relaxation, 128-bit helpers, soft-float, kernel/device integration, and hardware RTL. |

## 25. Closeout state

**Status: delivered.** All seven roadmap rows are COMPLETE and Phase 1 reads
7/7 (100%). Milestone 7 changes are committed in logical groups to `master`
and pushed to `origin/master`. Final verification requires:

```sh
git status --short          # empty
git status -sb              # ## master...origin/master
python scripts/phase1_validate.py
```

The completion gate is satisfied; Phase 2 remains explicitly unauthorized.
