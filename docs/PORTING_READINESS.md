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
| C ABI and runtime contracts | `docs/NOVA_C_ABI.md` (frozen ABI), `docs/COMPILER_RUNTIME.md` (future helper specification), `docs/RUNTIME.md` (implemented freestanding runtime), `nova/cabi.py` |
| C compiler frontend and backend (Phase 1 milestones 1–6 complete) | `nova/cc/` lexer, hardened preprocessor, parser/AST, LP64 type system and semantic analyzer (`types.py`, `sema.py`), direct Nova64 lowering (`codegen.py`), and the `nova-cc` driver (`driver.py`, `__main__.py`, `nova.cli:compile_main`) with tests (`tests/test_cc_lexer.py`, `tests/test_cc_preprocessor.py`, `tests/test_cc_parser.py`, `tests/test_cc_sema.py`, `tests/test_cc_codegen.py`, `tests/test_cc_driver.py`, `tests/test_cli.py`); `nova-cc` emits `.i`/`.s`/`.nobj`/`.nob` and supports multi-translation-unit links |
| C compiler toolchain/runtime (Phase 1 milestone 7 complete) | `nova/runtime/` supplies freestanding startup, syscall wrappers, nine headers, and `libnova` (`mem*`/`str*`, integer conversion, scoped `printf`); variadics use the unchanged ABI and the driver links the runtime with `--runtime` |
| Verification | Full pytest suite (2245 passed; driver 55, CLI 28, codegen 59, lexer/preprocessor/parser 180, sema 85, runtime groups 31, emulator/docs/examples remainder), 18/18 conformance vectors, `compileall` clean, repeatable `scripts/phase1_validate.py` PASS |

## 2. Readiness matrix

| Target | Ready? | Why |
| --- | --- | --- |
| Define a C target / write a backend plan | **Yes** | Encoding, ABI, relocations and instruction selection are concrete; `docs/COMPILER_BACKEND.md` already maps IR ops to Nova instructions |
| Build a GCC/LLVM-class C compiler here | **No** | No Nova LLVM/GCC target in this tree, no LLVM source or build integration, and several prerequisites below are missing |
| Build a small freestanding C compiler + libc | **Yes for the documented subset** | Phase 1 now provides the compiler, startup, syscall wrappers, headers, variadics, and scoped `libnova`; full hosted ISO C is still out of scope |
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
6. **Full runtime breadth** is still absent: a scoped freestanding libc and
   startup now exist, but there are no 128-bit helpers, soft float, hosted libc,
   or full ISO C library coverage.
7. **Linker features** still lack weak symbols, linker scripts, dead-code
   elimination, PIE, and shared libraries.

### Phase 1 status (milestones 1–7 complete)

The C frontend, Nova64 lowering pass, `nova-cc` driver, and freestanding runtime
now form a working toolchain for the documented C subset: preprocessing, tokens,
syntax-level AST, LP64 type/semantic passes, direct assembly generation,
assembly, object encoding, linking, startup, syscall wrappers, and `libnova`
all have regression tests. Public entry points include
`nova.cc.parse_source(...)`, `nova.cc.analyze(tree, target="nova64")`,
`nova.cc.generate(tree, target="nova64")`, `nova.runtime.runtime_objects()`, and
the `nova-cc` / `python -m nova.cc` command line.

Generated assembly uses a fixed stack-slot frame model and the documented C ABI
(`docs/NOVA_C_ABI.md`): a 128-byte outgoing argument area, saved `ra` at
`sp + 128`, 16-byte-aligned frames, up to six register arguments, stack
arguments beyond six, indirect calls through `t0`, and small or hidden-pointer
aggregate returns. Variadic callees spill `a0`–`a5` into the ABI register save
area and use the unchanged 24-byte `va_list`. It emits only frozen ISA v1.0
instructions, the `LI`/`LA` pseudos (plus the documented `BL` pseudo under
long-call/long-branch modes), and documented directives/relocations. Every
covered program is assembled with `assemble_object`, linked with `link_objects`,
and executed in `NovaMachine`.

The driver supports `-E`, `-S`, `-c`, and link modes; `-o`, `-I`, `-D`, `-U`,
`-std=`, `-O0`, `--target=nova64`, `--long-calls`, `--long-branches`,
`-e`/`--entry`, `--runtime`, `--nostartfiles`, `--nostdlib`, `--version`, and
`--help`; multiple C inputs; `.nobj` inputs; and multi-translation-unit links.
It writes outputs atomically and reports stage-located diagnostics.
`--runtime` is link-mode only, implies long calls/branches for user sources,
and adds the built-in runtime include path behind user `-I` directories. See
`docs/NOVA_CC.md` and `docs/RUNTIME.md`.

Floating types exist only in the semantic model; floating-point lowering,
bit-field access, VLAs, and PIC/GOT remain unsupported. Variadic access and a
scoped `libnova` are implemented. All seven Phase 1 milestones are complete,
tested, documented, and committed; `scripts/phase1_validate.py` is green and
`docs/PHASE1_COMPLETION.md` is the completion report. Phase 2 remains blocked
until separately authorized.

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
| 1. Compiler + libc | Freestanding C-subset compiler emitting Nova assembly, plus a minimal `libnova` (start files, syscall wrappers, scoped printf, mem/string, conversions, variadics). **Phase 1 milestone status:** all 1–7 complete. See `docs/PHASE1_PLAN.md`, `docs/NOVA_CC.md`, `docs/RUNTIME.md`, and `docs/PHASE1_COMPLETION.md` | Compile and run a nontrivial C program with `nova-cc` (proven by the runtime example, multi-TU links, and repeatable validation) |
| 2. Nova64 kernel | Boot protocol in the emulator, machine-mode trap shim, supervisor kernel, MMU userspace, scheduler, minimal device ABI, userland libc | Run a userspace process through syscall/MMU path |
| 3. OpenWindows Nova64 slice | Nova HAL, syscall bridge, OWX loader port, using Phases 1-2 | One OpenWindows subsystem boots and runs |
| 4. OpenWindows arm64 | Independent AArch64 HAL, UEFI boot, toolchain | Separate project; not derived from Nova |
| 5. Hardware | Synthesizable core validated against the conformance vectors | RTL passes all 18 vectors |

## 7. Verdict

**The ISA is ready to design against, and Phase 1 now provides a validated
freestanding C toolchain with a runtime; it is still not a hosted C environment
or a kernel platform.** Phase 1 is 7/7 (100%) and the completion report is
`docs/PHASE1_COMPLETION.md`. A kernel still needs a boot contract, device model,
and privilege routing; OpenWindows arm64 and hardware remain later, separate
efforts. Phase 2 requires explicit authorization and has not been started.
