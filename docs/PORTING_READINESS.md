# Porting readiness assessment

**Question:** is the current Nova codebase ready for (a) a real C compiler and
libc, (b) a Nova64 kernel, (c) a port of the existing OpenWindows kernel to
arm64/nova64, and (d) hardware?

**Short answer:** the *ISA definition* is ready to be targeted. The *codebase*
is not yet ready to build a GCC/LLVM-class compiler or to host a kernel port.
It has a stable encoding, ABI, syscall table, MMU, exception model and a
conformance suite — but it lacks the assembler/linker/debug infrastructure a
real compiler expects, and it has no boot, device, or SMP platform contract.
OpenWindows is an x86-64/PE kernel; nothing in this repository targets arm64.

This document is an audit, not a promise of work.

## 1. What exists today (verified in this tree)

| Capability | Evidence |
| --- | --- |
| Frozen ISA v1.0, 83 instructions, 8 formats, 20 CSRs | `nova/isa.py`, `README.md` |
| Authoritative tables drive assembler, disassembler, emulator and docs | `validate_table()` at import; generated README blocks |
| 64-bit-only profile with enforcement | `PROFILE`, `EXECUTION_MODES`, import-time validation |
| MMU: 4-level paging, 4 KiB/2 MiB/1 GiB pages, stage 2, permissions, faults, TLB flush | `nova/emulator.py`, CSRs `ptbr`/`tval`/`s2ptbr` |
| Atomics, SC memory model, FENCE, aq/rl | A format, `AMO*` handlers |
| Interrupts, timer, IPI, multicore cluster | `ie`/`ip`/`ivec`/`time`/`timecmp`, `SIPI`, `NovaCluster` |
| Virtualization with two-dimensional translation | `VMLAUNCH`/`VMRESUME`/`VMEXIT`, `vpc`/`vptbr`/`s2ptbr` |
| Object capabilities + stable identities + NOBO v1 | `nova/objfmt.py`, `OBJ.*` |
| ABI v1.0 calling convention + standard syscall table | `README.md` section 15, `nova/syscalls.py` |
| Assembler directives | `.global`/`.globl`, `.section`, data directives, `.type`/`.size`, `.file`/`.loc`/`.cfi_*`, `.include`, `.macro`/`.endm` |
| Relocation kinds | `PCREL_DISP14`, `LA64`, `ABS64`, `PCREL_LONG` in `nova/object.py` |
| Containers and tools | `.nob` v1, `.nobj` v2 (v1 readable), `.nox` v1, `nova-as`/`nova-ld`/`nova-objdump`/`nova-run` |
| C ABI and runtime contracts | `docs/NOVA_C_ABI.md`, `docs/COMPILER_RUNTIME.md`, `nova/cabi.py` |
| C compiler frontend and backend (Phase 1 milestones 1–5 complete) | `nova/cc/` lexer, hardened preprocessor, parser/AST, LP64 type system and semantic analyzer (`types.py`, `sema.py`), and direct Nova64 lowering (`codegen.py`) with tests (`tests/test_cc_lexer.py`, `tests/test_cc_preprocessor.py`, `tests/test_cc_parser.py`, `tests/test_cc_sema.py`, `tests/test_cc_codegen.py`); `nova.cc.generate` emits assembly that assembles, links, and executes |
| C compiler toolchain/runtime (Phase 1 milestones 6–7 pending) | `nova-cc` driver/CLI, compiler runtime, and freestanding `libnova` are not implemented; compilation currently requires calling the Python API directly |
| Verification | Full pytest suite (2147 passed; codegen 55, lexer/preprocessor 105, parser 69, sema 81), 18/18 conformance vectors, `compileall` clean |

## 2. Readiness matrix

| Target | Ready? | Why |
| --- | --- | --- |
| Define a C target / write a backend plan | **Yes** | Encoding, ABI, relocations and instruction selection are concrete; `docs/COMPILER_BACKEND.md` already maps IR ops to Nova instructions |
| Build a GCC/LLVM-class C compiler here | **No** | No Nova LLVM/GCC target in this tree, no LLVM source or build integration, and several prerequisites below are missing |
| Build a small freestanding C compiler + libc | **Conditionally yes** | Possible as a scoped project after Phase 0; needs a defined C subset and a freestanding libc |
| Run a Nova64 kernel in the emulator | **Conditionally yes** | MMU, exceptions, interrupts and syscalls exist; no boot protocol, device model or SMP startup contract |
| Port OpenWindows to Nova64 | **No (later project)** | It is C99 + x86 asm, PE/OWX, `int 0x80`, PML4/ACPI/APIC, NASM; every one of those needs a Nova equivalent |
| Port OpenWindows to arm64 | **No (separate project)** | Requires an AArch64 HAL (GIC, MMU, EL exceptions), UEFI boot, aarch64 toolchain; Nova code contributes nothing directly |
| Hardware core | **No artifact yet** | Conformance suite is the contract; no RTL exists |

## 3. Compiler readiness after Phase 0

Resolved in Phase 0 (see `docs/PHASE0_PLAN.md` for commits):

- **`ABS64`** now has a linker applier, so `.quad symbol` links.
- **Object metadata:** `.nobj` v2 carries symbol type and size; v1 files remain
  readable. `.type`/`.size` are accepted by the assembler.
- **Debug information:** `.nox` v1 is a real, versioned container with source,
  location, call-frame and register-save records, driven by `.file`, `.loc`,
  `.cfi_startproc`, `.cfi_offset`, and `.cfi_endproc`.
- **Source facilities:** deterministic bounded `.include` and `.macro`/`.endm`
  expansion with parameter substitution.
- **C ABI:** `docs/NOVA_C_ABI.md` plus `nova.cabi` define data layout, argument
  and struct-return rules, variadic `va_list`, PIC `gp` convention and soft
  float; `docs/COMPILER_RUNTIME.md` specifies libgcc-equivalent helpers.
- **Branch range:** `BL` provides an explicit ten-word long jump with a
  `PCREL_LONG` relocation; the linker reports an overflow with an actionable
  suggestion to use it.

Remaining blockers for a full GCC/LLVM-class compiler:

1. **No large-immediate instruction.** `LUI` is deliberately deferred from the
   frozen v1.0 ISA; constants still need the nine-word `LI` expansion.
2. **No automatic relaxation.** An overflowing `PCREL_DISP14` is an error with a
   suggested replacement; the linker does not insert long-branch stubs
   automatically.
3. **No PIC/GOT implementation.** The C ABI documents a `gp`-based GOT model,
   but there are no GOT relocations, GOT section layout, or PIC linking yet.
4. **Relocation set still limited** to `PCREL_DISP14`, `LA64`, `ABS64`, and
   `PCREL_LONG`. No PLT, TLS, weak/copy relocations, or dynamic linker.
5. **Debug format is Nova-specific.** `.nox` is documented and implemented, but
   no DWARF bridge exists.
6. **Runtime library** is specified, not implemented: no 128-bit helpers, no
   freestanding libc, no startup files.
7. **Linker features** still lack weak symbols, linker scripts, dead-code
   elimination, PIE, and shared libraries.

### Phase 1 status (milestones 1–5 complete)

The C frontend and the Nova64 lowering pass now form a working compiler core:
preprocessing, tokens, syntax-level AST, the Nova64 LP64 type/semantic passes,
and direct AST-to-assembly code generation all have regression tests. Public
entry points include `nova.cc.parse_source(...)`, `nova.cc.analyze(tree,
target="nova64")`, and `nova.cc.generate(tree, target="nova64")`. Supported
semantic checks include scope/name resolution, typedef aliases, aggregate/enum
handling, declarator type construction, expression/conversion checking,
initializer validation, and local control-flow checks.

Generated assembly uses a fixed stack-slot frame model and the documented C ABI
(`docs/NOVA_C_ABI.md`): a 128-byte outgoing argument area, saved `ra` at
`sp + 128`, 16-byte-aligned frames, up to six register arguments, stack
arguments beyond six, indirect calls through `t0`, and small or hidden-pointer
aggregate returns. It emits only frozen ISA v1.0 instructions, the `LI`/`LA`
pseudos, and documented directives/relocations. Every covered program is
assembled with `assemble_object`, linked with `link_objects`, and executed in
`NovaMachine`.

Floating types exist only in the semantic model; floating-point lowering,
bit-field access, VLAs, `va_start`/`va_arg`, PIC/GOT, and long-call selection
are rejected or remain unimplemented. Milestones 6 (compiler driver and
toolchain integration) and 7 (runtime, libnova and end-to-end validation) are
pending. Phase 2 remains blocked until all seven Phase 1 milestones are complete,
tested, documented, and committed, and a Phase 1 completion report is produced.

## 4. Blocking gaps for a kernel

- **Boot contract.** No firmware/bootloader interface, no kernel image header
  beyond `.nob`, no device tree, no MMIO or I/O-device architecture.
- **Privilege routing.** All traps and interrupts enter machine privilege;
  there is no trap delegation to supervisor, so a supervisor kernel needs a
  machine-mode shim.
- **SMP.** `NovaCluster` is a cooperative host-side helper; no architectural
  startup protocol for secondary harts, no TLB shootdown semantics, no
  cache-coherence or memory-mapped interrupt controller definition.
- **I/O.** Syscalls exist, but there is no serial/framebuffer/block/network
  device ABI, no clocksource beyond `time`, and no interrupt-source identity
  beyond software/timer/external bits.
- **Userspace.** No ELF-like user executable format, dynamic loader, or process
  address-space layout convention; `.nod` is reserved but unspecified.
- **arm64 specifically.** Nothing in this repository targets arm64; porting
  OpenWindows there is an independent HAL/toolchain/bootloader project.

## 5. What is genuinely ready

The architectural contract is stable and machine-checked: instruction
encodings, CSR behavior, exception entry/return, MMU faults, atomics,
interrupts, objects, ABI and syscall numbers are all pinned by tests and by
18 hardware-independent conformance vectors. That is exactly the foundation a
compiler and kernel need *before* serious implementation begins. Starting a
kernel port now would mean designing against a moving target; that risk is
gone.

## 6. Recommended sequence

| Phase | Deliverable | Gate to proceed |
| --- | --- | --- |
| 0. Toolchain hardening | Done: symbol metadata, `.nox`, directives, includes/macros, C ABI/runtime docs, explicit long branches. Remaining: automatic relaxation, GOT/PIC, weak symbols | A generated C test object links and runs (remaining items still block full GCC/LLVM-class targets) |
| 1. Compiler + libc | Freestanding C-subset compiler emitting Nova assembly, plus a minimal `libnova` (start files, syscall wrappers, printf/mem/string). **Phase 1 milestone status:** 1 foundation, 2 preprocessor, 3 parser/AST, 4 type system/semantic analysis, and 5 Nova64 code generation are complete; 6 driver/toolchain and 7 runtime/libnova/end-to-end validation are pending. See `docs/PHASE1_PLAN.md` | Compile and run a nontrivial C program on `nova-run` (codegen core proven through the Python API; driver pending) |
| 2. Nova64 kernel | Boot protocol in the emulator, machine-mode trap shim, supervisor kernel, MMU userspace, scheduler, minimal device ABI, userland libc | Run a userspace process through syscall/MMU path |
| 3. OpenWindows Nova64 slice | Nova HAL, syscall bridge, OWX loader port, using Phases 1-2 | One OpenWindows subsystem boots and runs |
| 4. OpenWindows arm64 | Independent AArch64 HAL, UEFI boot, toolchain | Separate project; not derived from Nova |
| 5. Hardware | Synthesizable core validated against the conformance vectors | RTL passes all 18 vectors |

## 7. Verdict

**Ready to design against, and now able to compile a freestanding C subset
through the Python API; not yet ready for a driver or a kernel boot.** The
highest-value next work is finishing Phase 1 (driver, runtime, libnova), not a
direct kernel port: a kernel needs a compiler toolchain and a boot contract.
OpenWindows arm64 and hardware are explicitly later, separate efforts.
